# System Graft

### Driver injector for SquashFS initrd images

Inject out-of-tree kernel modules into a SquashFS initrd, add a load hook to the image's
init scripts, and write the result to bootable removable media.

You supply the image. You supply the modules. **This tool ships neither**, contains no
third-party firmware or binaries, and never modifies its input — it always writes a new
file.

Typical use: an appliance-Linux image ships a monolithic kernel with a narrow built-in
driver set, and you need a driver for hardware you own that the vendor's build didn't
include. This unpacks the initrd, drops in a `.ko` you built, wires it into the init
sequence, and repacks it faithfully.

## Why this isn't just `unsquashfs` + `mksquashfs`

Because doing it by hand silently produces a broken image. Three things bite:

1. **Ownership is lost.** `unsquashfs` run as a normal user cannot restore uid/gid.
   Repack and every file belongs to *you*, not root.
2. **setuid/setgid bits are dropped.** In the image this was written against, `/bin/busybox`
   is `-rwsr-xr-x`. A non-root round-trip silently returns it as `-rwxr-xr-x`.
3. **The root directory escapes both.** Even a careful pseudo-file misses the root inode,
   which ends up owned by whoever ran the extract.

This tool reads the full mode/uid/gid table from the *source* image, hands it back to
`mksquashfs` as a pseudo-file, applies the root inode via `-root-uid/-root-gid/-root-mode`,
and then **re-reads the output and diffs it against the source** — refusing to claim success
unless the permission table matches exactly.

It also matches the source's compression and block size, so the repack isn't quietly
different from what the bootloader expects.

## The vermagic constraint

A module only loads into a kernel whose **vermagic** it matches exactly — kernel version,
SMP, preemption model, `mod_unload`, `modversions`. If the image's kernel is
`6.12.11 SMP preempt_rt mod_unload modversions`, a module built against stock Debian
`6.1.0-19-amd64 SMP mod_unload modversions` will be refused at load time, silently, at boot,
on a machine you may not have a console on.

So before injecting, the tool reads the vermagic of every `.ko` already in the image, reads
the vermagic of every `.ko` you're adding, and **blocks on mismatch by default**:

```
ERROR: vermagic mismatch for fakenic.ko:
      module: '6.1.0-19-amd64 SMP mod_unload modversions'
      image:  '6.12.11 SMP preempt_rt mod_unload modversions'
    The kernel will refuse to load this module.
    Rebuild the module against a matching kernel tree, or enable the override
    if you know what you are doing.
```

There's an override, off by default. It exists for people who know why they want it.

If `CONFIG_MODVERSIONS` is in the vermagic, matching the string is necessary but not
sufficient — the symbol CRCs must match too. Build against the same kernel source and
config as the image's kernel.

## Requirements

- Python 3.10+ with Tkinter (for the GUI; the CLI needs neither Tk nor any third-party package)
- `squashfs-tools` — `brew install squashfs` on macOS, your package manager elsewhere.
  The build must support the image's compressor (zstd, commonly).

## Usage

### GUI

```bash
python3 gui.py
```

Two stages sharing one progress bar and one log:

1. **Patch initrd** — pick the OS image directory, choose a profile, add your `.ko` files,
   press **Patch**.
2. **Write bootable USB** — pick a removable device, confirm, and the boot tree is written
   with the patched initrd substituted.

The log records every command run, every check made and every file touched. "Save log…"
writes the whole transcript out.

### CLI

```bash
python3 patcher.py /path/to/image-dir -m ./igb.ko -p sgs
```

```
  -m, --module PATH             module to inject (repeatable)
  -o, --output PATH             output image (default: <image>.patched)
  -p, --profile {generic,sgs}   target profile
      --allow-vermagic-mismatch inject anyway; the module will not load
      --keep-xattrs             preserve xattrs (see note below)
```

## Profiles

A profile says where the initrd lives, which init script to hook, where in that script to
insert, and what a module load looks like in that image's idiom.

| Profile | Image | Init script | Load line |
|---|---|---|---|
| `generic` | `boot/initrd`, `initrd`, `*.img` | `etc/init.d/system` | `insmod <path>` |
| `sgs` | `boot/initrd` | `etc/init.d/system` | `Run "insmod <path>"` |

The `sgs` profile exists because that image wraps every init command in `Run "…"` from
`/etc/functions`; emitting the same idiom keeps the patched script consistent with the rest
of the file and keeps its logging behaviour.

Profiles are a dozen lines in `patcher.py` — adding one for another image is trivial.

## Where the hook goes

Immediately **before the image's first existing `modprobe`/`insmod`**, matching that line's
indentation. That anchor matters: it puts the load inside whatever case branch already loads
modules (so it doesn't also run on `stop`), and it runs before the network is brought up.

Falls back to the first `ifconfig`/`ip link`, then to just after the shebang — the last of
which is flagged loudly in the log, because it is probably wrong for your image.

The block is fenced with sentinels:

```sh
        # >>> system-graft BEGIN (generated — safe to delete this block) >>>
        Run "insmod /lib/modules/6.12.11/updates/igb.ko"
        # <<< system-graft END <<<
```

Re-patching an already-patched image strips the old block first, so it's idempotent —
verified over three consecutive passes, with the init script coming out byte-identical to
the original apart from the block itself.

Loading is by **absolute path with `insmod`**, not `modprobe`, so it doesn't depend on
`modules.dep.bin` being regenerated — which cannot be done portably off-target.
`modules.dep` still gets a text entry for tidiness.

## Writing a bootable USB

After patching, stage 2 writes the **whole boot tree** to removable media as a UEFI-bootable
volume, substituting the patched initrd for the original. The input tree is never modified —
the substitution happens in flight.

```bash
python3 usbwriter.py --list
python3 usbwriter.py --device disk4 --image-dir /path/to/image-dir \
    --replace boot/initrd=/path/to/initrd.patched \
    --label BOOT --yes-destroy-device
```

### Bootability model

**UEFI only.** The volume boots through the removable-media fallback path
`\EFI\BOOT\BOOTX64.EFI`, which requires no NVRAM boot entry and no boot sector. If your
tree contains that file, the written volume is bootable; if it doesn't, the tool warns you
before writing.

Legacy BIOS boot would need a boot sector installed (`syslinux --install`), which this tool
does not do. Nothing stops you doing it afterwards.

The disk is partitioned GPT with a single FAT32 volume. If `sgdisk` is available and can
elevate, the partition type is set to EFI System (`ef00`); otherwise it is left as Microsoft
Basic Data, which virtually all firmware accepts on removable media.

### Safety model

This code can destroy a disk, so:

- **Enumeration only ever returns devices the OS reports as external.** Internal disks are
  filtered out before you can see them, and rejected again if named explicitly.
- **Virtual devices (attached disk images) are excluded** unless `--allow-virtual` is passed.
  That flag exists so the write path can be tested against a throwaway disk image instead of
  a real stick — it is not offered in the GUI.
- **The device is re-read immediately before the destructive step**, so a stale selection
  cannot be acted on. If the device has been removed or changed, the write aborts.
- **Anything mounted at a system path is refused** outright.
- **The GUI requires you to type the device node** (`disk4`) to confirm, after showing model,
  size, bus and current mount points.
- **Every written file is verified by sha256** against its source before the volume is
  ejected.

### Platform support

macOS is the tested path (`diskutil`). Linux is implemented (`sgdisk`/`mkfs.vfat`/`mount`,
requires root) but **has not been tested** — review before trusting it. Windows is not
supported.

## Notes and limits

- **SquashFS only.** cpio/gzip initramfs images are detected and rejected with a clear
  message rather than being corrupted.
- **xattrs are dropped by default.** On macOS the extract picks up host `com.apple.*`
  attributes, and baking those into a Linux image is at best noise. `--keep-xattrs` if your
  image genuinely uses them (SELinux labels, capabilities) — but then run on Linux.
- **File capabilities** (`security.capability`) live in xattrs and are therefore dropped with
  them. If your image relies on file caps rather than setuid, use `--keep-xattrs` on Linux.
- **The kernel is not touched.** Only the initrd. If the driver you need can't be built as a
  module against that kernel, this tool can't help you.
- **Nothing outside the initrd is touched** — no vendor container formats are opened, no
  checksums recomputed, no compatibility lists edited.

## Scope

This is a general-purpose SquashFS initrd tool. It reads an image you provide and modules
you provide and writes a new image. Whether modifying any particular image is permitted is
determined by that image's licence terms, and is your call to make — not something this
tool asserts or works around.
