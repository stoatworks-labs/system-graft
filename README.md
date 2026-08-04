# System Graft

### Driver injector for SquashFS initrd images

> **AI-assisted project.** This codebase was created with [Claude](https://claude.com/claude-code)
> (Anthropic), directed and reviewed by a human author. The patch path is covered by 16
> tests run on Ubuntu and macOS in CI, and has been exercised against a real appliance
> firmware image. The USB writer has only ever been run against an **attached disk image,
> never a physical stick**, and **no image produced by this tool has been booted on real
> hardware**. The Linux write path is implemented but **untested**. This tool repartitions
> and erases block devices — read the safety notes and confirm the target twice before
> using it on anything you care about.

Inject out-of-tree kernel modules into a SquashFS initrd, add a load hook to the image's
init scripts, and write the result to bootable removable media.

You supply the image. You supply the modules. **This tool ships neither**, contains no
third-party firmware or binaries, and never modifies its input — it always writes a new
file.

Typical use: an appliance-Linux image ships a monolithic kernel with a narrow built-in
driver set, and you need a driver for hardware you own that the vendor's build didn't
include. This unpacks the initrd, drops in a `.ko` you built, wires it into the init
sequence, and repacks it faithfully.

![System Graft GUI after a completed patch. Stage 1 shows the selected OS image directory, the detected SquashFS image with its compression and block size, the Generic BusyBox profile, one module igb.ko listed with its vermagic string, and the output path. Stage 2 shows the bootable-USB controls. The log below reports the verification pass: ownership and setuid bits preserved, permission table matches source exactly, the injected module present in the output, and a sha256 of the result](docs/screenshots/patch-complete.png)

<!-- downloads:start -->

## Download

**[v0.1.1](https://github.com/stoatworks-labs/system-graft/releases/tag/v0.1.1)** — prebuilt for macOS and Linux. Pick your platform:

<details>
<summary><b>macOS</b> — Apple Silicon</summary>

| Build | Download | Size |
| --- | --- | --- |
| Apple Silicon · .dmg disk image | [`system-graft-0.1.1-macos-arm64.dmg`](https://github.com/stoatworks-labs/system-graft/releases/download/v0.1.1/system-graft-0.1.1-macos-arm64.dmg) | 25 MB |
| Apple Silicon · .pkg installer | [`system-graft-0.1.1-macos-arm64.pkg`](https://github.com/stoatworks-labs/system-graft/releases/download/v0.1.1/system-graft-0.1.1-macos-arm64.pkg) | 11 MB |

</details>

<details>
<summary><b>Linux</b> — x64, ARM64</summary>

| Build | Download | Size |
| --- | --- | --- |
| x64 · .deb package (Debian/Ubuntu) | [`system-graft_0.1.1_amd64.deb`](https://github.com/stoatworks-labs/system-graft/releases/download/v0.1.1/system-graft_0.1.1_amd64.deb) | 25 MB |
| ARM64 · .deb package (Debian/Ubuntu) | [`system-graft_0.1.1_arm64.deb`](https://github.com/stoatworks-labs/system-graft/releases/download/v0.1.1/system-graft_0.1.1_arm64.deb) | 24 MB |
| x64 · .rpm package (Fedora/RHEL) | [`system-graft-0.1.1-1.x86_64.rpm`](https://github.com/stoatworks-labs/system-graft/releases/download/v0.1.1/system-graft-0.1.1-1.x86_64.rpm) | 26 MB |
| ARM64 · .rpm package (Fedora/RHEL) | [`system-graft-0.1.1-1.aarch64.rpm`](https://github.com/stoatworks-labs/system-graft/releases/download/v0.1.1/system-graft-0.1.1-1.aarch64.rpm) | 25 MB |
| x64 · .tar.gz archive | [`system-graft-0.1.1-linux-x86_64.tar.gz`](https://github.com/stoatworks-labs/system-graft/releases/download/v0.1.1/system-graft-0.1.1-linux-x86_64.tar.gz) | 25 MB |
| ARM64 · .tar.gz archive | [`system-graft-0.1.1-linux-aarch64.tar.gz`](https://github.com/stoatworks-labs/system-graft/releases/download/v0.1.1/system-graft-0.1.1-linux-aarch64.tar.gz) | 24 MB |

</details>

All builds, checksums and release notes: [github.com/stoatworks-labs/system-graft/releases](https://github.com/stoatworks-labs/system-graft/releases).

<!-- downloads:end -->

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

![System Graft GUI showing a blocked patch. A module built for Debian's 6.1.0-19-amd64 kernel has been selected against an image whose kernel is 6.12.11 with PREEMPT_RT. The log shows the vermagic of the module already in the image, the vermagic of the module being added, and a red refusal printing both strings side by side with the advice to rebuild against a matching kernel tree. The status line reads "Failed — see log" and no output was written](docs/screenshots/vermagic-blocked.png)

There's an override, off by default. It exists for people who know why they want it.

If `CONFIG_MODVERSIONS` is in the vermagic, matching the string is necessary but not
sufficient — the symbol CRCs must match too. Build against the same kernel source and
config as the image's kernel.

## Documentation

| Doc | Contents |
|---|---|
| [docs/USER-GUIDE.md](docs/USER-GUIDE.md) | The workflow, the vermagic wall, what gets silently dropped, writing a stick, troubleshooting |
| [docs/API.md](docs/API.md) | Both CLIs, the Python API, and the profile system |
| [docs/DEVELOPING.md](docs/DEVELOPING.md) | The safety rules, the status split, and the design decisions to preserve |

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

Stage 1 also carries four **Inspect** buttons, sitting above the module list because they
are how you work out *which* module you need in the first place. None of them write
anything:

| Button | Does |
|---|---|
| **Report** | describes the image and reads the kernel's build settings and embedded `.config` |
| **Hardware…** | opens a box to paste `lspci -nn` / `lsusb` output from the target, and reports coverage per device |
| **Find modules…** | pick a folder of modules or driver archives; every `.ko` in it is ranked against this image |
| **Find online…** | is this a stock distro kernel? which package holds the modules? Asks before downloading anything |
| **Build spec…** | pick a destination; writes the recovered `.config` and build instructions there |

The **Hardware…** box takes a paste rather than a file because of where that data comes
from — a machine that will not boot. The listing gets read off a screen or copied out of a
console far more often than it exists as a file on the disk running this tool.

The log records every command run, every check made and every file touched. "Save log…"
writes the whole transcript out.

> The screenshots below predate the Inspect controls and show the older window.

### CLI

```bash
python3 patcher.py /path/to/image-dir -m ./igb.ko -p sgs
```

```
  -m, --module PATH             module to inject (repeatable)
  -o, --output PATH             output image (default: <image>.patched)
  -p, --profile {generic,sgs}   target profile
      --allow-vermagic-mismatch inject anyway; the module will not load
      --allow-unsigned          inject unsigned modules into an all-signed image
      --allow-missing-deps      inject with a dependency absent from the image
      --keep-xattrs             preserve xattrs (see note below)

  inspection (writes no image):
      --report                  kernel, vermagic, module count, signing, firmware
      --build-spec [DIR]        read the kernel's build settings; with DIR, write
                                its .config and build instructions there
      --hardware FILE           device listing from the target ("-" for stdin)
      --alias-db FILE           a modules.alias to name drivers this image lacks
      --scan PATH               rank candidate .ko files/archives (repeatable)
      --want NAME               restrict --scan to these module names
      --find-drivers            is this a stock distro kernel? which package has
                                the modules? (network, metadata only)
      --fetch-drivers DIR       download those packages into DIR and rank them
```

## Which driver do I need, and which build of it?

Injecting the right module is the easy half. The tool answers the other half from data the
image already carries.

**Which driver.** Every `.ko` declares the PCI/USB IDs it drives; `depmod` collects those
into `modules.alias`, and drivers compiled *into* the kernel put theirs in
`modules.builtin.modinfo`. Read all three, hand it a device listing from the target machine,
and it reports per device whether this image can drive it:

```bash
lspci -nnmm > target.txt          # on the target, or any Linux booted on that hardware
python3 patcher.py ./image-dir --hardware target.txt
```

```
NOT COVERED — no driver in this image claims these  (1)
  10ec:8168  Ethernet controller: Realtek Semiconductor Co., Ltd. RTL8111/8168
      hint: Realtek Ethernet — r8169 (or Realtek's out-of-tree r8168)

covered by a module in the image  (1)
  8086:1533  Ethernet controller: Intel Corporation I210 Gigabit Network Connection
      igb

covered by a driver built into the kernel  (1)
  8086:15b8  Ethernet controller: Intel Corporation Ethernet Connection (2) I219-V
      e1000e (built into the kernel)
```

Reading `modules.builtin.modinfo` is what keeps that third case honest: without it, hardware
that works fine because its driver is compiled in gets reported as unsupported, and you go
off building a module you never needed.

Naming the driver for hardware the image *doesn't* cover needs a mapping the image has no
reason to contain. `--alias-db` points at a real one from any Linux system, which beats
guessing:

```bash
python3 patcher.py ./image-dir --hardware target.txt \
    --alias-db /lib/modules/$(uname -r)/modules.alias
```

Without one you get a coarse family hint, labelled as a hint.

**Which build.** Vendor driver packs and unpacked kernel packages contain builds for many
kernels. Point `--scan` at one and it ranks everything it finds against this image —
directories, tarballs, zips and `.deb`s, including `.ko.xz`/`.ko.gz`/`.ko.zst`:

```bash
python3 patcher.py ./image-dir --scan ~/Downloads/driverpack --want r8169
```

```
MATCH — vermagic is identical; these will be accepted by the kernel  (1)
  r8169                    driverpack/r8169.ko

same kernel release, different build flags — will NOT load as-is  (1)
  ixgbe                    driverpack/ixgbe.ko
      vermagic: 6.12.11 SMP mod_unload modversions
      the image's kernel has preempt_rt; the module was not built with it
```

That last line is the point: a mismatch now names the build flag to change, instead of
leaving you to diff two strings by eye.

## Is there a prebuilt module at all?

This question has two answers and only one of them is "go looking":

- **A stock distro kernel.** The distro built the kernel and its modules in one build, so
  its archive holds modules whose vermagic matches *by construction*. That is a lookup.
- **A vendor or appliance kernel.** No prebuilt module exists anywhere except that vendor's
  build machine. No archive, no database, no amount of searching will produce one — it has
  to be compiled.

`--find-drivers` tells you which of the two you are in, over the network but metadata only:

```
Kernel release: 6.1.0-19-amd64
  Looks like a Debian kernel (amd64).
  'amd64' is a Debian kernel flavour, and the compiler string agrees (Debian).

Found 1 package(s), 65.6 MB total:
  linux-image-6.1.0-19-amd64_6.1.82-1_amd64.deb  (65.6 MB)
      https://snapshot.debian.org/file/2ee9caba092c6e95ac73c886d04b83af7559b2df
      sha1 2ee9caba092c6e95ac73c886d04b83af7559b2df — will be verified
```

`--fetch-drivers DIR` then downloads those packages and runs `--scan` over what is inside
them, so the answer lands as a ranked list of modules rather than a folder of `.deb` files.

**The release string decides this, not the compiler.** The banner's compiler says what the
*build host* was, which is a different question — appliance vendors routinely build bespoke
kernels on Debian boxes, so `gcc (Debian 12.2.0-14)` on a kernel called `6.12.11` means
someone compiled it on Debian, not that Debian ships it. Getting that backwards would send
you hunting an archive for a package that was never published, so the tool says it plainly:

```
Kernel release: 6.12.11
  !! Not a stock distro kernel.
  built on Debian, but '6.12.11' is not a Debian package name — this is a custom
  kernel compiled on a Debian machine, not one Debian ships.
```

Only Debian and Ubuntu are resolved to actual URLs, because both publish a machine-readable
API that can be queried rather than scraped — Debian's also returns a SHA-1, which the
download is checked against. The RHEL rebuilds, Alpine and Arch are identified and named
down to the exact package, but no URL is constructed for them: their layouts vary by
rebuild vendor and mirror, and an untested URL fails later, further from the cause, and
looks like a bug in this tool.

## Asking the kernel instead of guessing

The initrd only implies what its kernel expects. The kernel image itself — normally sitting
right next to the initrd as `boot/vmlinuz` — states it outright, in two places:

- **The build banner.** Every kernel carries `Linux version <release> (<builder>)
  (<compiler>) <build>` in its rodata. That is the **exact compiler**, which does not appear
  in vermagic at all, so a compiler mismatch is never caught at load time.
- **The embedded config.** A kernel built with `CONFIG_IKCONFIG` carries its whole `.config`,
  gzipped, between the markers `IKCFG_ST` and `IKCFG_ED`.

Both usually sit inside a compressed payload, so the tool does what the kernel's own
`scripts/extract-ikconfig` does: scan for each compression format's signature, decompress
from every offset that matches, and look again in the result. gzip, xz, lzma, bzip2 and zstd
are handled; lz4 and lzo are not.

```bash
python3 patcher.py ./image-dir --report
```

```
Kernel image: demo/boot/vmlinuz-6.12.11
  release:  6.12.11
  compiler: gcc (Debian 12.2.0-14) 12.2.0, GNU ld 2.40
  config:   embedded, 9 settings recovered

What this means for a module you inject:
  !! CONFIG_MODULE_SIG_FORCE=y — this kernel loads ONLY signed modules...
  !  CONFIG_MODVERSIONS=y — matching the vermagic string is not enough...
  !  CONFIG_TRIM_UNUSED_KSYMS=y — this kernel exports only the symbols its own...
```

`CONFIG_TRIM_UNUSED_KSYMS` is the one people lose days to: the kernel exports only the
symbols its own built-in code and modules use, so an out-of-tree driver can fail to resolve a
symbol that plainly exists in the source.

`--build-spec DIR` writes the recovered `.config`, a `build-spec.json`, and a
`HOW-TO-BUILD.md` with the exact steps. The `.config` is the valuable artefact — it is the
one thing that cannot be reconstructed from anywhere else, and without it "build against a
matching kernel tree" is advice rather than an instruction.

## Three more ways a module fails at boot

Matching the vermagic is necessary, not sufficient. Alongside that check, the patch path now
refuses or flags:

- **Unsigned into a kernel that requires signatures.** An unsigned module is refused with
  `ENOKEY` however well the vermagic matches, and there is no fixing that without the signing
  key. When the kernel's config can be read this is a **fact** — `CONFIG_MODULE_SIG_FORCE=y`.
  When it can't, it falls back to an **inference**: if every module the vendor shipped is
  signed, the kernel very likely enforces it. The log says which of the two you got, because
  a refusal you can check is worth more than one you can't. Blocks; `--allow-unsigned`
  overrides.
- **Missing firmware.** A driver declaring `firmware=` whose blob is absent from
  `/lib/firmware` loads and then fails to bring the device up. Warns, and names the blob.
- **Missing or misordered dependencies.** `insmod` does not resolve dependencies the way
  `modprobe` does — it fails outright on an unresolved symbol. Dependencies are checked
  against the image's modules *and* its built-in drivers, and the injected modules are
  ordered so each is loaded after the ones it needs. Blocks when the image ships a
  `modules.builtin` to check against, warns when it doesn't; `--allow-missing-deps` overrides.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite builds a synthetic appliance image from scratch — root-owned files, a **setuid**
binary, and a directory owned by a uid with no passwd entry — then patches it and asserts
that all of it survives. It runs as an ordinary user and needs no fixtures in the repo.

Covered: setuid/ownership/root-inode preservation, module injection, hook placement and
indentation, hook placement *inside* the correct init branch, idempotency across three
consecutive re-patches, vermagic blocking and override, input immutability, output-overwrite
refusal, non-SquashFS rejection, and compressor/block-size preservation.

The discovery suite builds a real (if minimal) ELF object with a genuine `.modinfo` section,
rather than only the byte-scan stand-in, so the parsing path that runs against every real
module is the one under test. Covered: modinfo/alias/firmware extraction, signature
detection, compressed modules, modalias tokenising and three-state matching, `lspci -nn` /
`lspci -nnmm` / `lsusb` / modalias / bare-pair parsing, module-vs-built-in-vs-uncovered
verdicts, external alias databases, candidate ranking through directories and archives, and
each of the three boot-time checks with its override.

The kernel-spec suite builds synthetic kernels carrying a banner and an `IKCFG` payload, and
runs the extractor against each of them wrapped in gzip, xz, lzma and bzip2 — a real kernel
is a stub around a compressed payload, so a test that only fed it an uncompressed blob would
never exercise the part that does the work. It also asserts the fact-beats-inference
behaviour in both directions: a config saying signing is *off* must overrule an all-signed
image, and with no kernel present the inference must still apply.

CI runs the suite on Ubuntu and macOS on every push.

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

## Unsigned builds — macOS Gatekeeper

The release binaries are **not code-signed or notarized** — that needs paid Apple
and Microsoft developer certificates this project doesn't carry. The downloads are
fine; the OS just can't identify the publisher, so it warns you the first time.

- **macOS** — *"cannot be opened because the developer cannot be verified"*.
  Right-click the app → **Open** → **Open**, or clear the flag:
  `xattr -dr com.apple.quarantine "/Applications/System Graft.app"`
- **Linux** — no signing gate.

Per-artifact steps, self-signing and checksum verification:
**[docs/UNSIGNED.md](docs/UNSIGNED.md)**.

## Scope

This is a general-purpose SquashFS initrd tool. It reads an image you provide and modules
you provide and writes a new image. Whether modifying any particular image is permitted is
determined by that image's licence terms, and is your call to make — not something this
tool asserts or works around.
