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

macOS is the tested path; Linux implemented but untested.

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
