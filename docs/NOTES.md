# Notes

Working notes for this repo: status, decisions, and the traps that have actually bitten.
Migrated out of Claude Code's memory on 2026-08-24, so they are written in the first
person and dated by when each thing was learned — that date is usually the useful part.

Cross-cutting notes that are not specific to this repo live in
[fleet-notes](https://github.com/stoatworks-labs/fleet-notes).

*System Graft — public tool injecting kernel modules into SquashFS initrds + writing bootable UEFI USB media; deliberately named to stay general-purpose*

`~/Projects/system-graft` — Python/Tkinter + CLI tool: inject out-of-tree `.ko` modules into
a SquashFS initrd, hook them into init, repack, and write the boot tree to removable media as
a UEFI-bootable volume. **GitHub PUBLIC**, MIT. Created 2026-07-26.

Grew out of [soundgrid protocol](https://github.com/stoatworks-labs/soundgrid-protocol/blob/main/docs/NOTES.md) (`soundgrid-protocol`) (the `sgs` profile targets Waves SGS images), but
is deliberately a general appliance-Linux tool: ships no vendor bytes, never opens a vendor
container or recomputes its checksum, user supplies image + modules.

**Naming was a deliberate legal-hygiene decision.** Originally requested as "SG driver
injector" (SG = System Graft); flagged that in a public repo containing an `sgs` profile,
"SG" reads as SoundGrid and a backronym looks like consciousness of guilt — under *Grokster*
a tool's name and framing are the evidence of its object. Renamed to **System Graft spelled
out**. Keep it that way: never abbreviate the project to "SG" in public-facing text.

Non-obvious engineering the tool exists to encode (all learned the hard way in testing):
- Non-root `unsquashfs`→`mksquashfs` silently drops **ownership and setuid** (busybox is
  `-rwsr-xr-x`); fixed by replaying the source's mode/uid/gid through a mksquashfs `-pf`
  pseudo-file.
- A pseudo-file **cannot reach the root inode** — needs `-root-uid/-root-gid/-root-mode`.
- Strip-regex for the init hook must consume the **whitespace before** the sentinel, or each
  re-patch shifts the anchor line further right.

macOS is the tested path; Linux implemented but untested. *(Superseded 2026-09-07 — see below.)*

**Run against several real firmware images and driver sets** (Allan, 2026-08-02) —
the images it produced **appear to boot**. Note the hedge: that is Allan's word and
the website repeats it verbatim. The USB writer has still only ever been pointed at
an attached disk image, never a physical stick.

**v0.1.0 released 2026-07-26.** 16 unit tests (build a synthetic appliance image from
scratch via pseudo-file, so they run non-root), CI green on ubuntu-latest + macos-14.
Deliberately **no frozen .app** — it would still need `brew install squashfs`, and an
unsigned bundle with a nested interpreter hits **macos gatekeeper nested binaries** (working-practice note, kept in Claude memory).

Useful: **GitHub Actions is free on public repos**, so CI works here even though
**openstage no ci** (working-practice note, kept in Claude memory) reports the quota exhausted for private ones.

## 2026-09-07 — a produced image booted, on Linux and from Windows

First time anything this tool wrote was actually booted. Waves SGS 16.5 (kernel 6.12.11,
Buildroot gcc 12.2, `CONFIG_MODVERSIONS=y`, config embedded in `vmlinuz` and recovered by
`--build-spec`) in a fresh KVM guest on lilnasX: Q35, OVMF `pure-efi`, three NICs. The kernel
already has e1000e, igb, igc, r8169 and virtio_net **built in**, so the emulated NICs that
exercise injection are QEMU's rtl8139 and vmxnet3. Built `mii.ko`, `8139cp.ko` and
`vmxnet3.ko` on kde-lab against the recovered config; all three loaded, `eth1`/`eth2` appeared
and pinged the host at 0% loss. The unpatched image was the control: `wsgnf` only, `eth0` only.

**Building modules against an appliance kernel with MODVERSIONS:** `make modules_prepare` is
not enough — the symbol CRCs live in `Module.symvers`, which only exists after `make vmlinux`
*and* `make modules`. The SGS config bakes `CONFIG_EXTRA_FIRMWARE="intel-ucode/..."`, which
breaks the build until cleared (`scripts/config --set-str EXTRA_FIRMWARE ""`). Drivers the
config leaves off still build with `M=<dir> CONFIG_8139CP=m` on the command line; `8139cp`
needs `mii` (`KBUILD_EXTRA_SYMBOLS`). gcc 12.4 against a 12.2 kernel matched every CRC.

**Three bugs it found, all fixed the same day:**
- `--hardware --alias-db` said `8086:10d3` was NOT COVERED after resolving it to `e1000e`,
  which is in `modules.builtin`. Pre-6.13 kernels ship no device-table aliases for built-ins;
  external providers were never checked against what the image holds.
- The Linux USB writer could not see a loop device at all (`TYPE loop`, dropped before
  `--allow-virtual` applied; then marked internal). Attach with `losetup -fP` — without
  `-P` the partition node never appears, which the writer now says rather than sleeping 2 s.
- On MSYS2, `unsquashfs -lls` prints uid 33 as `WRITE RESTRICTED/WRITE RESTRICTED`; the
  listing regex skipped it on both read and verify, so `/var/www` came out owned by the
  extracting user under a green "permission table matches source exactly". Now `-lln`, and
  an unparseable entry is an error.

**Windows:** the patch path runs under MSYS2 (`pacman -S squashfs-tools python`) with
`MSYS=winsymlinks:lnk` exported — the default symlink emulation fails on dangling targets and
silently loses 55 of the initrd's 337 links. The initrd patched there booted identically. The
USB writer stays unsupported. Plain Windows Python (the embeddable 3.12 zip) ran `--report`
once MSYS2's `usr\bin` was on `PATH`; patching from it was not tried. Note the embeddable
build's `python312._pth` *is* `sys.path`, so the repo directory has to be listed in it.
