# System Graft — Interfaces

Two CLIs, a Python API, and a profile system.

| § | Interface | Source |
|---|---|---|
| [1](#1-patcher-cli) | Patcher CLI | `patcher.py` |
| [2](#2-usb-writer-cli) | USB writer CLI — **destructive** | `usbwriter.py` |
| [3](#3-python-api) | Python API | `patcher.py`, `usbwriter.py` |
| [4](#4-profiles) | Profiles | `patcher.py` |

**The vermagic constraint, the bootability model, the safety model and the scope statement are
in the [README](../README.md)** and are not restated here.

> **⚠ `usbwriter.py` repartitions and erases whole block devices.** Point it at the wrong device
> and it destroys a disk.

---

## 1. Patcher CLI

```
system-graft <image_dir> [-m MODULE]... [-o OUTPUT] [-p PROFILE]
             [--allow-vermagic-mismatch] [--keep-xattrs]
```

| Flag | Default | Notes |
|---|---|---|
| `image_dir` | — | directory containing the OS image; the profile's globs find the initrd inside it |
| `-m` / `--module` | — | **repeatable**; a `.ko` to inject |
| `-o` / `--output` | derived | output initrd path |
| `-p` / `--profile` | `generic` | see §4 |
| `--allow-vermagic-mismatch` | off | **the override — see below** |
| `--keep-xattrs` | off | **see below** |
| `--version` | | |

### `--allow-vermagic-mismatch`

Off by default, and the default is the point. A module only loads into a kernel whose vermagic
matches **exactly**; a mismatch is refused **silently, at boot, on a machine you may not have a
console on.** So the tool reads the vermagic of every `.ko` already in the image and every `.ko`
being added, and **blocks on mismatch**.

> **If `CONFIG_MODVERSIONS` is in the vermagic, matching the string is necessary but not
> sufficient — the symbol CRCs must match too.** Passing this flag with matching strings and
> mismatched CRCs still produces a module that won't load. Build against the same kernel source
> *and config*.

The override "exists for people who know why they want it". It is not offered as a way past an
inconvenient error.

### `--keep-xattrs`

**xattrs are dropped by default**, because on macOS the extract picks up host `com.apple.*`
attributes and baking those into a Linux image is at best noise.

Two consequences:

- **File capabilities (`security.capability`) live in xattrs and are dropped with them.** An
  image relying on file caps rather than setuid needs this flag — **and needs to be run on
  Linux**, since the macOS extract is what introduces the junk in the first place.
- SELinux labels likewise.

### What it will not do

- **SquashFS only.** cpio/gzip initramfs images are **detected and rejected with a clear
  message** rather than corrupted.
- **The kernel is not touched** — only the initrd. If the driver can't be built as a module
  against that kernel, this tool can't help.
- **Nothing outside the initrd is touched** — no vendor container formats opened, no checksums
  recomputed, no compatibility lists edited.

---

## 2. USB writer CLI

> **⚠ This repartitions and erases the target device.**

```
usbwriter --list
usbwriter --device disk4 --image-dir <tree> [--replace SRC=DST]...
          [--label BOOT] [--allow-virtual] [--yes-destroy-device]
```

| Flag | Notes |
|---|---|
| `--list` | list eligible devices and exit |
| `--device` | device node, e.g. `disk4` |
| `--image-dir` | the boot tree to write |
| `--replace` | **repeatable** — substitute a file in the tree (this is how the patched initrd gets in) |
| `--label` | volume label, default `BOOT` |
| `--allow-virtual` | **see below** |
| `--yes-destroy-device` | skips the confirmation prompt |

### The safety behaviours, and what they mean for a caller

- **Enumeration only ever returns devices the OS reports as external.** Internal disks are
  filtered out before you can see them, **and rejected again if named explicitly** — so
  `--device` on an internal disk fails rather than working.
- **Virtual devices (attached disk images) are excluded unless `--allow-virtual` is passed.**
  That flag exists so the write path can be tested against a throwaway disk image instead of a
  real stick. **It is deliberately not offered in the GUI.**
- **The device is re-read immediately before the destructive step**, so a stale selection can't
  be acted on. If the device changed or was removed, the write aborts.
- **Anything mounted at a system path is refused** outright.
- **Every written file is verified by sha256** against its source before the volume is ejected.

`--yes-destroy-device` skips the confirmation. **It exists for automation, and using it removes
the last check between a typo and a destroyed disk.** The GUI has no equivalent — it requires
you to *type the device node* after showing model, size, bus and current mount points.

### Bootability

**UEFI only**, via the removable-media fallback path `\EFI\BOOT\BOOTX64.EFI` — no NVRAM boot
entry, no boot sector. **If your tree doesn't contain that file, the tool warns before writing**
— it does not refuse, so a warned-past write produces a non-bootable stick.

GPT, single FAT32 volume. If `sgdisk` is available **and can elevate**, the partition type is set
to EFI System (`ef00`); otherwise it stays Microsoft Basic Data, which virtually all firmware
accepts on removable media. So the partition type silently depends on whether elevation worked.

**Legacy BIOS is not supported** — that would need a boot sector (`syslinux --install`), which
this tool does not install. Nothing stops you doing it afterwards.

**macOS is the tested path** (`diskutil`). **Linux is implemented and untested**
(`sgdisk`/`mkfs.vfat`/`mount`, requires root). **Windows is not supported.**

---

## 3. Python API

### `patcher`

```python
@dataclass
class PatchRequest:
    image: Path
    modules: list[Path]
    output: Path
    profile: Profile
    allow_vermagic_mismatch: bool = False
    keep_xattrs: bool = False
    add_to_modules_dep: bool = True

def patch(req: PatchRequest)  # generator
```

**`patch()` is a generator yielding `(fraction, level, message)`** as it proceeds, where `level`
is one of `info`, `step`, `ok`, `warn`, `error`, `cmd`. That's what lets the GUI show live
progress. **It raises `PatchError` on any condition that should stop the run** — so a caller must
both iterate *and* handle the exception; draining the generator without catching won't do.

`add_to_modules_dep` defaults to `True` and isn't exposed on the CLI.

Useful helpers: `require_tools()`, `tool_version()`, `is_squashfs()`, `sniff_format()`,
`find_images()`, `probe_squashfs()`, `read_modinfo()`, `find_image_modules()`,
`kernel_versions()`, `build_ownership_map()`, `sha256()`.

### `usbwriter`

```python
@dataclass
class Device:
    node: str; path: str; name: str; size: int
    internal: bool; removable: bool; ejectable: bool; virtual: bool
    bus: str; mountpoints: list[str]
    # .size_h  -> human-readable size
    # .label   -> "disk4  —  SanDisk Ultra  —  28.9 GB  —  usb"  (+ "VIRTUAL")

def list_devices(allow_virtual: bool = False) -> list[Device]
def write_bootable(device, image_dir, replacements, ...)
```

`USBError` is the failure type. `list_devices()` is the only sanctioned way to obtain a
`Device` — **constructing one by hand bypasses the external-only filtering.**

---

## 4. Profiles

A profile tells the patcher where the initrd is, which init script to hook, and **how a module
load is written in that image's idiom**.

```python
@dataclass
class Profile:
    key: str
    label: str
    image_globs: list[str]          = ["boot/initrd", "initrd", "*.img"]
    init_script: str                = "etc/init.d/system"
    anchor_regex: str               = r'^\s*(Run\s+")?(modprobe|insmod)\b'
    fallback_anchor_regex: str      = r'^\s*(Run\s+")?(ifconfig|ip\s+link|ip\s+addr)\b'
    load_cmd: str                   = 'insmod {path}'
```

**The hook is inserted immediately before the first line matching `anchor_regex`**, falling back
to `fallback_anchor_regex` if that isn't found — i.e. before the image's own module loading, or
failing that before it brings up networking.

Shipped profiles:

| Key | For |
|---|---|
| `generic` | Generic BusyBox/initrd image |
| `sgs` | Waves SGS (SoundGrid Server) — `boot/initrd`, `etc/init.d/system` |

**The `sgs` profile emits `Run "..."` wrapping**, because SGS init scripts wrap every command in
`Run "…"` from `/etc/functions`. Matching the image's own idiom is the point: a hook that looks
unlike its surroundings is both harder to review and likelier to behave differently.

Adding a profile is a new `Profile` entry — no other code changes.

`strip_existing_hook()` removes a previously inserted hook, so re-patching an
already-patched image replaces rather than stacks.

---

## See also

- [USER-GUIDE.md](USER-GUIDE.md) — the workflow, and what's actually proven
- [DEVELOPING.md](DEVELOPING.md) — the safety rules for working on this
- [README](../README.md) — vermagic, bootability, safety model, scope
