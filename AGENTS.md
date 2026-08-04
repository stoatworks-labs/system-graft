# AGENTS.md — bringing an LLM up to speed on System Graft

Orientation for an AI assistant (or a new human) picking this project up cold. There is no
`CLAUDE.md` here; this is the entry point.

**Read §2 before running or modifying anything. This tool destroys data.**

---

## 1. What this is

**System Graft** — a driver injector for SquashFS initrd images. It injects kernel modules
into a SquashFS initrd and writes a bootable UEFI USB stick.

Python. **Public repo, MIT licensed.**

**Never abbreviate the name to "SG" in public-facing text.** The full name is deliberate.

## 2. This tool repartitions and erases block devices

`usbwriter.py` writes bootable media. That means it **repartitions and erases whole block
devices**. Point it at the wrong device and it destroys a disk.

Rules for working on this repo:

- **Never run the USB write path against a physical device to "test" a change.** Use an
  attached disk image — that is how every existing test has been run.
- **Never weaken or bypass a target-confirmation prompt** to make an automated run smoother.
  Those prompts are the last line of defence between a typo and someone's drive.
- Treat any change to device enumeration, target selection or partitioning as
  safety-critical: it deserves more scrutiny than the feature it enables.

## 3. Status — precisely

Three separate claims, often collapsed into one:

- **The patch path** (injecting modules into the initrd) is covered by **118 tests, run on
  Ubuntu and macOS in CI**, and has been exercised against a **real appliance firmware
  image**. This part is genuinely well-evidenced.
- **The archive lookups** were verified by hand against the live Debian snapshot and
  Launchpad APIs, down to confirming the resolved download URL serves and its size matches
  what the API reported. The **bulk download itself has only ever run against localhost**.
- **The GUI tests skip on a headless runner**, so in CI they prove nothing. Their value is
  local. The screenshots in the README and user guide predate the inspection controls and
  no longer show the current window.
- **The discovery features** (`--report`, `--build-spec`, `--hardware`, `--scan`) are
  tested against synthetic images and synthetic kernels built by the suite. They have
  **never been run against a real appliance's `modules.alias`, a real vendor driver pack,
  or a real vmlinuz** — the formats are stable and documented, but that is not the same as
  having been tried. In particular the IKCFG extractor has only ever unwrapped payloads
  this repo's own tests compressed.
- **The USB writer** has only ever been run against an **attached disk image, never a
  physical stick**.
- **No image produced by this tool has ever been booted on real hardware.**
- **The Linux write path is implemented but completely untested.**

So: the transformation is well tested, the delivery mechanism largely isn't, and the end
result has never been proven to boot. Don't let a summary compress that into "tested".

## 4. Layout

```
patcher.py     The initrd patch path - SquashFS unpack, module injection, repack.
               This is the well-tested part. Also hosts the read-only --report /
               --hardware / --scan modes, which extract but never write an image.
kmod.py        Reading a .ko: modinfo (via the real ELF .modinfo section), aliases,
               firmware, signature, vermagic decomposition, dependency ordering.
hardware.py    "Which driver does this machine need?" - modalias matching, device
               listings, and what an image can drive (modules AND built-ins).
sources.py     "Which build of it will load?" - ranks candidate .ko files found in
               directories and archives against an image's vermagic.
kernelspec.py  Reads the kernel image beside the initrd: build banner (the exact
               compiler, which is NOT in vermagic) and the embedded .config, when
               CONFIG_IKCONFIG put one there. Also writes the --build-spec output.
distro.py      "Is this a stock distro kernel, and if so where is the matching
               module package?" The ONLY module that touches the network.
usbwriter.py   Bootable UEFI USB writing. DESTRUCTIVE. Lightly tested.
gui.py         Desktop UI. Stage 1 carries the inspection buttons; HardwareDialog
               collects a pasted device listing.
_version.py
tests/test_patcher.py     the patch path
tests/test_discovery.py   kmod / hardware / sources, and the boot-time checks
tests/test_kernelspec.py  kernel banner/config extraction, and fact-vs-inference
tests/test_gui.py         GUI wiring; skips itself when Tk has no display
tests/test_distro.py      archive lookups (stubbed) and downloads (localhost)
```

**No test touches the network, and it must stay that way.** `distro.py`'s tests stub the
archive replies and run the download against a throwaway HTTP server on localhost. A test
that fails when snapshot.debian.org is slow tells you nothing about this code. The stubs
are recorded from real replies; if an archive changes shape, the fix is to re-record them
by hand, not to make CI depend on a third party being up.

Note the test coverage maps onto exactly the split in §3: `patcher.py`, `kmod.py`,
`hardware.py`, `sources.py` and `kernelspec.py` have tests; `usbwriter.py` does not.

Both `patch()` and `inspect()` are generators yielding `(fraction, level, message)`.
That is the entire contract between the core and any front end: `gui.py::_run_job`
drives either without knowing which, and the CLI prints either. **A new long-running
operation should be a generator of the same shape**, not a callback and not a
function that prints — the moment one of them prints, the GUI cannot show it.

## 4a. What the analysis can and cannot know

The checks in the patch path are inferences from what the initrd carries, and the
strength of each one is different. Do not flatten them:

- **vermagic** is read directly from modules in the image. Certain.
- **built-in drivers** come from `modules.builtin`; absent that file, nothing can be
  ruled out, and the dependency check deliberately downgrades from error to warning.
  `_builtin_modules` returns `None` (image does not say) distinctly from an empty set
  (image says nothing is built in) for exactly this reason — do not collapse them.
- **signature enforcement** has two routes and they are *not* interchangeable. If the
  kernel image is present and embedded its config, `CONFIG_MODULE_SIG_FORCE` is read and
  the answer is a fact. Otherwise "every module here is signed" stands in for it — a
  strong signal, not a fact. Both block, both have the same override, and the log states
  which one fired. **Do not collapse these into one message.** A user who is told the
  config says so can go and check; a user told "it looks signed" cannot, and deserves to
  know the difference. When the config is readable it *overrules* the inference in both
  directions — an all-signed image whose kernel says signing is off must not be refused.
- **the compiler version** comes only from the kernel banner, and is **not part of
  vermagic**. A mismatch is therefore never caught at load time. Report it; never treat
  it as something the vermagic check covers.
- **the compiler string says what the BUILD HOST was, not who ships the kernel.** This
  distinction is the whole point of `distro.py` and it is easy to get backwards. Appliance
  vendors routinely build bespoke kernels on Debian or Ubuntu machines, so a banner
  reading `gcc (Debian 12.2.0-14)` on a kernel called `6.12.11` means someone *compiled*
  it on Debian — Debian has never shipped it and its archive holds nothing. An earlier
  draft trusted the compiler and would have sent users hunting for a package that does
  not exist, which is worse than saying nothing. **The release string decides whether a
  kernel is stock; the compiler only corroborates, or breaks the genuine Debian/Ubuntu
  ambiguity, and a decision resting on it alone must be labelled a guess.**
- **hardware coverage** is three-state on purpose. `lspci -nn` without `-mm` has no
  subsystem IDs, so a pattern requiring one is reported UNCERTAIN rather than as a
  miss. Collapsing that to a boolean would turn "I cannot tell" into "unsupported".

## 5. Working on it

```bash
python -m pytest tests/
```

If you extend the writer, the highest-value contribution would be **tests against disk
images** that assert partition layout and bootability markers — closing the gap between the
two halves of the project — rather than new features on an unverified path.

## 6. Conventions

- Public repo, MIT. "Commit" means commit **and** push.
- Multi-platform release CI; cross-compile macOS x86_64 on `macos-14` — never `macos-13`.

## Diagnostics

Log via `diag.log`, not `print`. `diag.init(...)` goes before anything that can fail. Tk
apps must also call `diag.install_tk_excepthook(root)` before any callback can run —
Tkinter swallows callback exceptions, so without it a fault in a button handler never
reaches the crash handler. See [docs/diagnostics.md](docs/diagnostics.md).
