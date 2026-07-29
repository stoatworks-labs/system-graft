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

- **The patch path** (injecting modules into the initrd) is covered by **16 tests, run on
  Ubuntu and macOS in CI**, and has been exercised against a **real appliance firmware
  image**. This part is genuinely well-evidenced.
- **The USB writer** has only ever been run against an **attached disk image, never a
  physical stick**.
- **No image produced by this tool has ever been booted on real hardware.**
- **The Linux write path is implemented but completely untested.**

So: the transformation is well tested, the delivery mechanism largely isn't, and the end
result has never been proven to boot. Don't let a summary compress that into "tested".

## 4. Layout

```
patcher.py     The initrd patch path - SquashFS unpack, module injection, repack.
               This is the well-tested part.
usbwriter.py   Bootable UEFI USB writing. DESTRUCTIVE. Lightly tested.
gui.py         Desktop UI
_version.py
tests/test_patcher.py
```

Note the test coverage maps onto exactly the split in §3: `patcher.py` has tests,
`usbwriter.py` does not.

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
