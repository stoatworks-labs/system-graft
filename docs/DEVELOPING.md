# System Graft — Developing

Python. Public repo, MIT licensed.

**Never abbreviate the name to "SG" in public-facing text.** The full name is deliberate.

---

## 1. ⚠ This tool repartitions and erases block devices

`usbwriter.py` writes bootable media. That means it **repartitions and erases whole block
devices.** Point it at the wrong device and it destroys a disk.

Rules for working on this repo:

- **Never run the USB write path against a physical device to "test" a change.** Use an attached
  disk image — that is how every existing test has been run, and why `--allow-virtual` exists.
- **Never weaken or bypass a target-confirmation prompt** to make an automated run smoother.
  **Those prompts are the last line of defence between a typo and someone's drive.**
- **Treat any change to device enumeration, target selection or partitioning as safety-critical:
  it deserves more scrutiny than the feature it enables.**

The guards that must keep working:

| Guard | Why it can't be relaxed |
|---|---|
| Enumeration returns **external devices only** | Internal disks are filtered before display **and rejected again if named explicitly** — the second check is what protects the CLI |
| **Virtual devices excluded** unless `--allow-virtual` | And that flag is deliberately **not offered in the GUI** |
| **Device re-read immediately before the destructive step** | A stale selection can't be acted on |
| **Anything mounted at a system path refused** | |
| **GUI requires typing the device node** | After showing model, size, bus and mount points |
| **Every file verified by sha256** before eject | |

`--yes-destroy-device` exists for automation and removes the confirmation. Don't extend its reach.

---

## 2. Status — precisely

Three separate claims, often collapsed into one:

- **The patch path** (injecting modules into the initrd) is covered by **16 tests, run on Ubuntu
  and macOS in CI**, and has been exercised against a **real appliance firmware image**. This
  part is genuinely well-evidenced.
- **The USB writer** has only ever been run against an **attached disk image, never a physical
  stick.**
- **No image produced by this tool has ever been booted on real hardware.**
- **The Linux write path is implemented but completely untested.**

> So: **the transformation is well tested, the delivery mechanism largely isn't, and the end
> result has never been proven to boot. Don't let a summary compress that into "tested".**

Note the test coverage maps exactly onto that split: `patcher.py` has tests, `usbwriter.py` does
not.

---

## 3. Layout and tests

```
patcher.py     The initrd patch path — SquashFS unpack, module injection, repack.
               THIS IS THE WELL-TESTED PART.
usbwriter.py   Bootable UEFI USB writing. DESTRUCTIVE. Lightly tested.
gui.py         Desktop UI
_version.py
tests/test_patcher.py
```

```bash
python -m pytest tests/
```

> **If you extend the writer, the highest-value contribution would be tests against disk images
> that assert partition layout and bootability markers** — closing the gap between the two halves
> of the project — **rather than new features on an unverified path.**

---

## 4. Design decisions to preserve

**`patch()` is a generator** yielding `(fraction, level, message)` with `level` in
`info|step|ok|warn|error|cmd`, and raises `PatchError` on anything that should stop the run.
That's what gives the GUI live progress without the patcher knowing about the GUI. Keep the
generator shape; don't collapse it into a callback or a return value.

**Vermagic blocking is on by default.** The override exists "for people who know why they want
it". Any change that makes the block easier to skip — a default flip, a broader override, a
warning instead of a refusal — reverses the tool's main safety property, because the failure it
prevents is **silent, at boot, on a machine with no console.**

**xattrs are dropped by default** because macOS extraction picks up host `com.apple.*`
attributes. That default is right; the `--keep-xattrs` escape hatch is documented as needing
Linux for the same reason.

**cpio/gzip initramfs images are detected and rejected**, not attempted. Refusing beats
corrupting.

**Profiles are data, not code.** Adding support for another image family should be one new
`Profile` entry — where the initrd lives, which init script to hook, the anchor regexes, and the
`load_cmd` idiom. If a new image needs code, look again at whether the profile fields can express
it.

**`strip_existing_hook()` means re-patching replaces rather than stacks.** Keep that — a stacked
hook would load modules twice and be hard to spot in a diff.

**The hook matches the image's own idiom** (the `sgs` profile emits `Run "…"` because SGS init
scripts do). A hook that looks unlike its surroundings is harder to review and likelier to behave
differently.

---

## 5. Scope

This is a **general-purpose SquashFS initrd tool.** It reads an image you provide and modules you
provide and writes a new image.

> Whether modifying any particular image is permitted is determined by **that image's licence
> terms, and is the user's call to make** — not something this tool asserts or works around.

Keep that posture. Don't add anything that opens vendor container formats, recomputes vendor
checksums, or edits compatibility lists — the README states plainly that nothing outside the
initrd is touched, and that statement is load-bearing.

---

## 6. Conventions

- Public repo, MIT. "Commit" means commit **and** push.
- Multi-platform release CI; **cross-compile macOS x86_64 on `macos-14` — never `macos-13`.**
- **Never abbreviate the project name to "SG" in public-facing text.**

---

## See also

- [API.md](API.md) — both CLIs, the Python API, the profile system
- [USER-GUIDE.md](USER-GUIDE.md) — the operator view
- [README](../README.md) — vermagic, bootability, safety model, scope
- [`AGENTS.md`](../AGENTS.md) — LLM onboarding
