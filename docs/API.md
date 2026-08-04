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
             [--allow-vermagic-mismatch] [--allow-unsigned]
             [--allow-missing-deps] [--keep-xattrs]

system-graft <image_dir> [--report] [--build-spec [DIR]] [--hardware FILE]
             [--alias-db FILE] [--scan PATH]... [--want NAME]... [--limit N]
```

| Flag | Default | Notes |
|---|---|---|
| `image_dir` | — | directory containing the OS image; the profile's globs find the initrd inside it |
| `-m` / `--module` | — | **repeatable**; a `.ko` to inject |
| `-o` / `--output` | derived | output initrd path |
| `-p` / `--profile` | `generic` | see §4 |
| `--allow-vermagic-mismatch` | off | **the override — see below** |
| `--allow-unsigned` | off | inject unsigned modules into an image whose modules are all signed |
| `--allow-missing-deps` | off | inject with a declared dependency absent from the image |
| `--keep-xattrs` | off | **see below** |
| `--version` | | |

### Inspection mode

Any of `--report`, `--hardware` or `--scan` puts the tool in a read-only mode: it extracts
the image, answers every question asked in one pass, and **writes no output image**. `-m` is
not required there.

| Flag | Notes |
|---|---|
| `--report` | kernel version, decomposed vermagic, module count, how many are signed, built-in driver count, firmware, and the kernel image's own build settings |
| `--build-spec [DIR]` | read the kernel's build banner and embedded `.config`; given a directory, write `config`, `build-spec.json` and `HOW-TO-BUILD.md` into it |
| `--hardware FILE` | device listing from the target machine; `-` reads stdin. Accepts `lspci -nn`, `lspci -nnmm`, `lsusb`, raw modalias strings and bare `vendor:device` pairs, mixed |
| `--alias-db FILE` | a `modules.alias` from any Linux system, used to name drivers for hardware this image does not cover |
| `--find-drivers` | identify the kernel and locate matching module packages. **Network**, metadata only — downloads nothing |
| `--fetch-drivers DIR` | download those packages into DIR and rank what is inside them. **Network**, tens of MB |
| `--scan PATH` | **repeatable**; directory or archive of candidate modules to rank against this image |
| `--want NAME` | **repeatable**; restrict `--scan` to these module names |
| `--limit N` | cap on modules read during `--scan` (default 5000) |

`--hardware` reports three states, not two. A pattern that matches everything the input
provided but requires a subsystem ID the input lacked — which is every `lspci -nn` run
without `-mm` — is reported **UNCERTAIN**, because reporting it as a miss would turn "I
cannot tell" into "unsupported".

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
    allow_unsigned: bool = False
    allow_missing_deps: bool = False

def patch(req: PatchRequest)  # generator
```

**`patch()` is a generator yielding `(fraction, level, message)`** as it proceeds, where `level`
is one of `info`, `step`, `ok`, `warn`, `error`, `cmd`. That's what lets the GUI show live
progress. **It raises `PatchError` on any condition that should stop the run** — so a caller must
both iterate *and* handle the exception; draining the generator without catching won't do.

`add_to_modules_dep` defaults to `True` and isn't exposed on the CLI.

Useful helpers: `require_tools()`, `tool_version()`, `is_squashfs()`, `sniff_format()`,
`find_images()`, `probe_squashfs()`, `read_modinfo()`, `find_image_modules()`,
`kernel_versions()`, `build_ownership_map()`, `sha256()`, `extracted()`, `image_vermagic()`.

```python
@dataclass
class InspectRequest:
    image: Path
    profile: Profile
    report: bool = False
    build_spec: Path | None = None
    hardware_text: str = ""
    alias_db: Path | None = None
    scan_paths: list[Path] = field(default_factory=list)
    want: list[str] = field(default_factory=list)
    limit: int = 5000

def inspect(req: InspectRequest)  # generator
```

**`inspect()` yields `(fraction, level, message)` exactly as `patch()` does**, and writes no
image. That shared shape is the whole contract between the core and any front end — the GUI
drives either through one code path, and the CLI prints either. A new long-running operation
should be a generator of the same shape rather than something that prints; the moment it
prints, the GUI cannot show it.

Everything requested is answered from a single extract, because `unsquashfs` on a large
initrd is the slow part.

`extracted(image)` is a context manager yielding a temporary extracted root and cleaning up
after itself — the read-only counterpart to the patch path's inline extract.

`read_modinfo()` still returns a flat single-value dict. Anything needing *all* values of a
multi-valued key (`alias`, `firmware`, `parm`) must use `kmod.read()` instead;
`find_image_modules()` now returns `kmod.ModInfo` objects rather than `(Path, dict)` pairs.

### `kmod`

```python
def read(path: Path) -> ModInfo          # never raises for a parse problem; see .error
def parse_vermagic(text: str) -> Vermagic
def compare_vermagic(module: str, image: str) -> tuple[bool, list[str]]
def order_by_depends(infos: list[ModInfo]) -> tuple[list[ModInfo], list[str]]
```

`ModInfo` exposes `.name`, `.vermagic`, `.aliases`, `.firmware`, `.depends`, `.signed`,
`.signature`, `.is_elf`, `.error`, plus `.get(key)` / `.all(key)` / `.flat()`.

Reading is total: a malformed, truncated or non-ELF file yields whatever could be recovered
with `.error` set, rather than raising. These files come from wherever the user found them,
so "I could not parse it" is a finding to report.

### `hardware`

```python
def parse_devices(text: str) -> tuple[list[Device], list[str]]
def build_index(root: Path, kver: str) -> CoverageIndex
def load_alias_db(path: Path, index: CoverageIndex) -> int
def assess(devices: list[Device], index: CoverageIndex) -> list[Finding]
def format_report(findings: list[Finding], index: CoverageIndex) -> str
```

`Finding.status` is one of `COVERED_MODULE`, `COVERED_BUILTIN`, `UNCERTAIN`, `UNCOVERED`.
`match_alias()` returns `MATCH_YES` / `MATCH_NO` / `MATCH_UNKNOWN` — see the three-state note
in §1.

`report_lines()` returns `(level, line)` pairs and `format_report()` joins them.
`sources.scan_lines()` and `kernelspec.spec_lines()` do the same. Rendering severity is
decided once, where the content is produced — never by a front end sniffing at the text.

### `sources`

```python
def scan(paths, target_vermagic, want=None, limit=5000) -> tuple[list[Candidate], list[str]]
def format_scan(candidates, target_vermagic, notes) -> str
```

`Candidate.verdict` is one of `EXACT`, `SAME_RELEASE`, `OTHER_RELEASE`, `UNREADABLE`.
Modules extracted from archives live in a temp directory that is kept only when an exact
match was found in one; the returned notes say where.

### `kernelspec`

```python
def find_kernel(*search_dirs: Path) -> Path | None
def analyse(path: Path) -> KernelSpec
def implications(spec: KernelSpec) -> list[tuple[str, str]]
def write_build_spec(spec: KernelSpec, vermagic: str, dest: Path) -> list[Path]
```

`KernelSpec` carries `.release`, `.builder`, `.compiler`, `.build`, `.config` (a dict),
`.config_text` and `.notes`, plus `.has_config` / `.get(key)` / `.is_set(key)`.

`parse_config()` records `# CONFIG_X is not set` as `"n"` rather than dropping it. **The
difference between "off" and "absent" is load-bearing**: `is_set("CONFIG_MODULE_SIG_FORCE")`
returning `False` because the config says it is off is a fact, whereas the key being missing
means the config could not be read at all. `patch()` distinguishes these — see the signing
check — and anything reading `.config` must too.

`implications()` returns `(level, message)` pairs for settings that change whether a module
loads or how it must be built, and returns nothing at all when there is no config. It is not
a config dump; the full config is written by `write_build_spec()`.

`analyse()` never raises. A file that is not a kernel, or one compressed with lz4/lzo, comes
back with empty fields and an explanatory note.

### `distro`

**The only module that touches the network.** Nothing else in the tool makes an outbound
request, and both entry points are opt-in per invocation.

```python
def identify(release: str, compiler: str = "", arch: str = "") -> Target
def packages_for(target: Target) -> list[PackageRef]
def resolve(target: Target) -> Resolution          # network: metadata only
def download(item: Download, dest_dir: Path, progress=None) -> Path
```

`Target.is_stock` is false for a vendor kernel, and `resolve()` returns no downloads with a
note explaining that none exist — that is an answer, not a failure, and the most important
one here.

**`identify()` decides from the release string, not the compiler.** The compiler names the
*build host*; a bespoke kernel compiled on a Debian machine is not a Debian kernel and its
archive holds nothing. The compiler only corroborates or breaks the Debian/Ubuntu
ambiguity, and any verdict resting on it alone is labelled a guess in `Target.evidence`.

`download()` writes to a `.part` and renames only once complete and checksum-verified, so an
interrupted or corrupted download can never be mistaken for a usable package. It returns an
existing file untouched rather than refetching.

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
