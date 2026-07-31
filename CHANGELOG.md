# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

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
