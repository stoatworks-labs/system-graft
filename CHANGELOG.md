# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

Finding the right driver, not just injecting one you already have.

### Added

- **Hardware coverage reports** (`--hardware`). Give it a device listing from the target
  machine — `lspci -nn`, `lspci -nnmm`, `lsusb`, modalias strings or bare `vendor:device`
  pairs, mixed freely — and it reports per device whether this image can drive it. Built
  from the image's own `modules.alias`, each module's `alias=` entries, and
  `modules.builtin.modinfo`, so hardware driven by a **compiled-in** driver is recognised
  rather than reported as missing.
- **`--alias-db`**, to name drivers for hardware the image does not cover, using a real
  `modules.alias` from any Linux system instead of a guess. Without one, a coarse family
  hint is offered and labelled as such.
- **Candidate scanning** (`--scan`, `--want`). Ranks every `.ko` in a directory or archive
  against the image's vermagic, so a driver pack containing builds for a dozen kernels
  becomes a file pick. Reads directories, tarballs, zips and `.deb`s, and the
  `.ko.xz`/`.ko.gz`/`.ko.zst` forms distros actually ship.
- **`--report`**, describing the image: kernel, decomposed vermagic, module count, how many
  are signed, built-in driver count, firmware.
- **Reading the kernel image itself** (`kernelspec.py`). The kernel beside the initrd
  carries a build banner naming the **exact compiler** — which is not part of vermagic, so a
  mismatch is never caught at load time — and, when built with `CONFIG_IKCONFIG`, its entire
  `.config` gzipped between `IKCFG_ST` and `IKCFG_ED`. Both normally sit inside a compressed
  payload, so the extractor scans for gzip/xz/lzma/bzip2/zstd signatures and decompresses
  from each match, as the kernel's own `scripts/extract-ikconfig` does. lz4 and lzo are not
  supported and say so.
- **`--build-spec [DIR]`**, writing the recovered `.config`, a `build-spec.json` and a
  `HOW-TO-BUILD.md` with the exact steps and the checks to run afterwards.
- Config settings that change what a module must be are now surfaced during a patch:
  `CONFIG_MODVERSIONS` (symbol CRCs must match too), `CONFIG_TRIM_UNUSED_KSYMS` (symbols
  present in the source may have been trimmed from this build), randstruct (per-build seed
  required), and `CONFIG_CFI_CLANG`.
- **Three new checks for failures that otherwise happen silently at boot** — unsigned
  modules going into a kernel that requires signatures (blocks; `--allow-unsigned`),
  missing firmware blobs (warns), and missing or misordered dependencies (blocks when the
  image ships a `modules.builtin` to check against, warns otherwise;
  `--allow-missing-deps`). Injected modules are now ordered so each loads after the ones it
  depends on, which `insmod` — unlike `modprobe` — will not do for you.

- **Finding a prebuilt module in a distribution's archive** (`distro.py`, `--find-drivers`,
  `--fetch-drivers`, and the GUI's **Find online…**). Works out whether the image runs a
  stock distro kernel — in which case the distro's own archive holds modules matching by
  construction — or a vendor kernel, for which no prebuilt module exists anywhere and the
  honest answer is to compile. Debian and Ubuntu resolve to real URLs through their
  machine-readable APIs, and Debian's SHA-1 is verified on download. The RHEL rebuilds,
  Alpine and Arch are named down to the exact package without a constructed URL, because
  their mirror layouts vary and an untested URL fails later and looks like a bug here.
  `--fetch-drivers` feeds what it downloads straight into `--scan`.
- Module architecture is now read from the `.ko` ELF header, because Ubuntu's kernel release
  string does not carry one and fetching the wrong architecture's package can only ever
  produce a module that will not load.
- **The GUI exposes all of it.** Stage 1 gained five **Inspect** buttons — Report,
  Hardware…, Find modules…, Find online…, Build spec… — placed above the module list,
  because they are how you decide which module to add. A paste box collects the device listing, since it
  comes off a screen or a console far more often than it exists as a file. The two new
  overrides are checkboxes alongside the existing ones, worded to say what will happen
  rather than what is permitted.

### Changed

- `patcher.inspect()` is now a generator yielding `(fraction, level, message)`, the same
  contract as `patch()`, and the CLI is a thin consumer of it. One implementation behind
  both front ends rather than a printing one and a displaying one that drift apart. The
  report formatters return `(level, line)` pairs so the CLI and the GUI log colour the same
  output the same way without either re-deriving severity from the text.
- The module list now reports whether each added `.ko` is signed and what firmware it needs,
  not just its vermagic.
- Module parsing moved to `kmod.py` and now locates the real ELF `.modinfo` section instead
  of scanning the whole file, so an `alias=` string in a driver's *data* is no longer
  mistaken for a declared alias. Falls back to the old byte scan for non-ELF input. All
  values of multi-valued keys (`alias`, `firmware`, `parm`) are kept.
- A vermagic mismatch now names what differs — the kernel release, or the specific build
  flag — rather than printing two strings for you to diff by eye.
- The signing check reads `CONFIG_MODULE_SIG_FORCE` from the kernel when it can, and says so.
  It falls back to the all-modules-are-signed inference only when the kernel's config cannot
  be read, and the log distinguishes the two: a readable config saying signing is *off* now
  correctly overrules an image whose modules all happen to be signed.

## [0.1.1] — 2026-07-30

A packaging and documentation release. No patching or USB-writing behaviour changed.

### Added

- **A release workflow that ships installable artefacts.** v0.1.0 was a source-only tag;
  there is now something to download.
- **Built-in logging and crash diagnostics** through the vendored `diag` module, so a
  failed patch or write can be reported with the run's log attached.
- User, developer and interface documentation under `docs/`.
- AGENTS.md — onboarding for LLMs and newcomers.
- GUI screenshots in the README, and the AI-assisted project disclaimer.
- Sponsor button configuration (GitHub Sponsors and Liberapay).

### Changed

- The destructive and safety-critical paths now carry comments explaining *why* each guard
  is there — the device re-read before writing, the external-only filter, and the
  permission-table diff are easy to "tidy" away without them.
- GitHub URLs throughout the docs now point at the `stoatworks-labs` account.

## [0.1.0] — 2026-07-26

First release.

### Patching

- Inject out-of-tree `.ko` modules into a SquashFS initrd and repack it, matching the
  source's compressor and block size.
- Restore ownership, permissions and **setuid/setgid** bits by replaying the source's
  mode/uid/gid table through a `mksquashfs` pseudo-file, so the tool works as an ordinary
  user without silently producing a broken image.
- Restore the root inode's ownership via `-root-uid/-root-gid/-root-mode`, which a
  pseudo-file cannot reach.
- Verify the result by re-reading the output and diffing its permission table against the
  source; success is not reported unless they match exactly.
- Check `vermagic` against modules already present in the image and refuse on mismatch by
  default, with an opt-in override.
- Insert the module-load hook immediately before the image's first existing
  `modprobe`/`insmod`, matching that line's indentation, so it lands inside the correct
  init branch and runs before the network is brought up.
- Sentinel-fenced, idempotent hook: re-patching strips the previous block first.
- Load by absolute path with `insmod`, avoiding any dependency on regenerating
  `modules.dep.bin`, which cannot be done portably off-target.
- Never modify the input; refuse to overwrite an existing output.
- Reject non-SquashFS initrds with a clear message rather than corrupting them.

### USB writing

- Write the whole boot tree to removable media as a UEFI-bootable volume (GPT + FAT32),
  substituting the patched initrd in flight.
- Enumeration returns only devices the OS reports as external; internal disks are filtered
  out and rejected again if named explicitly.
- Virtual devices (attached disk images) are excluded unless explicitly requested — that
  flag exists for testing and is not offered in the GUI.
- The device is re-read immediately before the destructive step, so a stale selection
  cannot be acted on.
- Refuse anything mounted at a system path.
- The GUI requires the device node to be typed to confirm, after showing model, size, bus
  and current mount points.
- Every written file is verified by sha256 before the volume is ejected.

### Interface

- Tkinter GUI with two stages sharing one progress bar and a terminal-style log.
- CLI equivalents for both stages.
- Profiles: `generic` and `sgs`.

### Known limitations

- macOS is the tested path. Linux is implemented but **untested**. Windows is unsupported.
- UEFI boot only; no BIOS boot sector is installed.
- xattrs are dropped by default (see README).

[0.1.1]: https://github.com/stoatworks-labs/system-graft/releases/tag/v0.1.1
[0.1.0]: https://github.com/stoatworks-labs/system-graft/releases/tag/v0.1.0
