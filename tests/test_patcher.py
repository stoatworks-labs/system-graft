"""
Self-contained tests for the patch path.

Builds a synthetic "appliance image" from scratch — including root-owned files, a
setuid binary and a non-root-owned directory — using the same mksquashfs
pseudo-file mechanism the tool relies on. That means the tests run as an ordinary
user and still exercise the ownership/permission handling that is the whole point
of the tool.

Requires squashfs-tools on PATH.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import patcher  # noqa: E402
from patcher import PatchError, PatchRequest, PROFILES  # noqa: E402

KVER = "6.12.11"
VERMAGIC = f"{KVER} SMP preempt_rt mod_unload modversions"
ODD_UID = 12345  # deliberately unlikely to have a passwd entry, to test the numeric path

INIT_SCRIPT = """#!/bin/sh
#
# system - basic system init
#
. /etc/functions

case "$1" in
    start)
        cd $SCRIPT_PATH

        # Waves drivers
        Run "modprobe wsgnf"

        # Basic network
        Run "ifconfig lo up"
        ;;
    stop)
        Run "ifconfig lo down"
        ;;
esac
"""


def fake_module(name: str, vermagic: str = VERMAGIC) -> bytes:
    """A stand-in .ko: read_modinfo scans bytes, so it needs no real ELF."""
    fields = [f"name={name}", f"vermagic={vermagic}", "license=GPL", "depends="]
    return b"\x00".join(f.encode() for f in fields) + b"\x00" + os.urandom(256)


def require_squashfs():
    if not (shutil.which("mksquashfs") and shutil.which("unsquashfs")):
        raise unittest.SkipTest("squashfs-tools not installed")


class ImageFixture:
    """Builds a throwaway SquashFS image with awkward-but-realistic ownership."""

    def __init__(self, root: Path):
        self.root = root
        self.tree = root / "tree"
        self.image = root / "initrd"

    def build(self) -> Path:
        tree = self.tree
        (tree / "bin").mkdir(parents=True)
        (tree / "etc" / "init.d").mkdir(parents=True)
        (tree / f"lib/modules/{KVER}/updates").mkdir(parents=True)
        (tree / "var" / "www").mkdir(parents=True)

        (tree / "bin" / "busybox").write_bytes(b"#!/bin/false\n" + os.urandom(512))
        (tree / "etc" / "init.d" / "system").write_text(INIT_SCRIPT)
        (tree / "etc" / "functions").write_text("Run() { \"$@\"; }\n")
        (tree / f"lib/modules/{KVER}/updates/existing.ko").write_bytes(fake_module("existing"))
        (tree / f"lib/modules/{KVER}/modules.dep").write_text("updates/existing.ko:\n")

        # One entry per path — a duplicate pseudo line for the same file does not
        # override, it just loses to the first one.
        entries: dict[str, tuple[int, int, int]] = {}
        for path in sorted(tree.rglob("*")):
            rel = path.relative_to(tree).as_posix()
            entries[rel] = (0o755 if path.is_dir() else 0o644, 0, 0)
        # A setuid root binary, and one directory owned by a numeric-only uid.
        entries["bin/busybox"] = (0o4755, 0, 0)
        entries["var/www"] = (0o755, ODD_UID, ODD_UID)

        pseudo = self.root / "src.pseudo"
        pseudo.write_text("\n".join(
            f'"/{rel}" m {mode:04o} {uid} {gid}'
            for rel, (mode, uid, gid) in sorted(entries.items())) + "\n")

        subprocess.run(
            ["mksquashfs", str(tree), str(self.image), "-noappend", "-no-progress",
             "-comp", "gzip", "-b", "131072", "-no-xattrs", "-pf", str(pseudo),
             "-root-mode", "0755", "-root-uid", "0", "-root-gid", "0"],
            check=True, capture_output=True, text=True)
        return self.image


class PatcherTests(unittest.TestCase):
    def setUp(self):
        require_squashfs()
        self.tmp = Path(tempfile.mkdtemp(prefix="system-graft-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fixture = ImageFixture(self.tmp)
        self.image = self.fixture.build()
        self.module = self.tmp / "newnic.ko"
        self.module.write_bytes(fake_module("newnic"))

    # ------------------------------------------------------------------ util

    def run_patch(self, output_name="out.sqfs", modules=None, profile="sgs", **kwargs):
        request = PatchRequest(
            image=self.image,
            modules=modules if modules is not None else [self.module],
            output=self.tmp / output_name,
            profile=PROFILES[profile],
            **kwargs,
        )
        messages = list(patcher.patch(request))
        return request.output, messages

    def ownership(self, image: Path) -> dict:
        return patcher.build_ownership_map(shutil.which("unsquashfs"), image)

    def extract(self, image: Path, name="x") -> Path:
        dest = self.tmp / name
        subprocess.run(["unsquashfs", "-d", str(dest), "-no-xattrs", str(image)],
                       check=True, capture_output=True)
        return dest

    # ----------------------------------------------------------------- tests

    def test_fixture_has_the_awkward_bits(self):
        """Guard the test's own premise: the source really is hostile to a naive repack."""
        table = self.ownership(self.image)
        self.assertEqual(table["bin/busybox"], (0o4755, 0, 0))
        self.assertEqual(table["var/www"], (0o755, ODD_UID, ODD_UID))
        self.assertEqual(table["."], (0o755, 0, 0))

    def test_setuid_and_ownership_survive(self):
        output, _ = self.run_patch()
        before, after = self.ownership(self.image), self.ownership(output)
        self.assertEqual(after["bin/busybox"], (0o4755, 0, 0), "setuid bit was lost")
        self.assertEqual(after["var/www"], (0o755, ODD_UID, ODD_UID), "non-root owner was lost")
        self.assertEqual(after["."], (0o755, 0, 0), "root inode ownership was lost")
        for rel, value in before.items():
            self.assertEqual(after.get(rel), value, f"{rel} changed")

    def test_module_is_injected(self):
        output, _ = self.run_patch()
        self.assertIn(f"lib/modules/{KVER}/updates/newnic.ko", self.ownership(output))

    def test_hook_placement_and_indentation(self):
        output, _ = self.run_patch()
        script = (self.extract(output) / "etc/init.d/system").read_text()
        lines = script.splitlines()
        begin = next(i for i, l in enumerate(lines) if "system-graft BEGIN" in l)
        anchor = next(i for i, l in enumerate(lines) if 'modprobe wsgnf' in l)
        self.assertLess(begin, anchor, "hook must precede the existing modprobe")
        self.assertIn("insmod /lib/modules/6.12.11/updates/newnic.ko", script)
        indents = {len(l) - len(l.lstrip()) for l in lines[begin:anchor + 1] if l.strip()}
        self.assertEqual(indents, {8}, f"inconsistent indentation: {indents}")
        # The hook must land inside start), not before the case statement.
        case_line = next(i for i, l in enumerate(lines) if l.strip().startswith("case "))
        self.assertGreater(begin, case_line, "hook must be inside the case branch")

    def test_repatching_is_idempotent(self):
        first, _ = self.run_patch("a.sqfs")
        self.image = first
        second, _ = self.run_patch("b.sqfs")
        self.image = second
        third, _ = self.run_patch("c.sqfs")

        script = (self.extract(third, "x3") / "etc/init.d/system").read_text()
        self.assertEqual(script.count("system-graft BEGIN"), 1, "hook block duplicated")
        self.assertEqual(script.count("insmod /lib/modules/6.12.11/updates/newnic.ko"), 1)
        indents = {len(l) - len(l.lstrip()) for l in script.splitlines()
                   if l.strip() and "modprobe wsgnf" in l}
        self.assertEqual(indents, {8}, "anchor indentation drifted across re-patches")

        stripped = patcher.strip_existing_hook(script)
        self.assertEqual(stripped.strip(), INIT_SCRIPT.strip(),
                         "script differs from the original once the hook is removed")

    def test_vermagic_mismatch_blocks(self):
        bad = self.tmp / "bad.ko"
        bad.write_bytes(fake_module("bad", "6.1.0-19-amd64 SMP mod_unload modversions"))
        with self.assertRaises(PatchError) as ctx:
            self.run_patch("bad.sqfs", modules=[bad])
        self.assertIn("vermagic mismatch", str(ctx.exception))
        self.assertFalse((self.tmp / "bad.sqfs").exists(), "output written despite mismatch")

    def test_vermagic_override_allows(self):
        bad = self.tmp / "bad.ko"
        bad.write_bytes(fake_module("bad", "6.1.0-19-amd64 SMP mod_unload modversions"))
        output, messages = self.run_patch("ok.sqfs", modules=[bad],
                                          allow_vermagic_mismatch=True)
        self.assertTrue(output.is_file())
        self.assertTrue(any(level == "warn" and "vermagic mismatch" in msg
                            for _, level, msg in messages), "override must still warn")

    def test_input_is_never_modified(self):
        before = patcher.sha256(self.image)
        self.run_patch()
        self.assertEqual(patcher.sha256(self.image), before, "input image was modified")

    def test_refuses_to_overwrite_output(self):
        (self.tmp / "taken.sqfs").write_bytes(b"do not clobber me")
        with self.assertRaises(PatchError):
            self.run_patch("taken.sqfs")
        self.assertEqual((self.tmp / "taken.sqfs").read_bytes(), b"do not clobber me")

    def test_refuses_non_squashfs_input(self):
        bogus = self.tmp / "notsquashfs"
        bogus.write_bytes(b"\x1f\x8b\x08" + os.urandom(4096))
        self.image = bogus
        with self.assertRaises(PatchError) as ctx:
            self.run_patch("nope.sqfs")
        self.assertIn("not a SquashFS", str(ctx.exception))

    def test_compression_and_block_size_are_preserved(self):
        output, _ = self.run_patch()
        un = shutil.which("unsquashfs")
        before = patcher.probe_squashfs(un, self.image)
        after = patcher.probe_squashfs(un, output)
        self.assertEqual(after["compression"], before["compression"])
        self.assertEqual(after["block_size"], before["block_size"])

    def test_generic_profile_matches_bare_modprobe(self):
        tree = self.tmp / "g"
        shutil.copytree(self.fixture.tree, tree)
        script = tree / "etc/init.d/system"
        script.write_text(script.read_text().replace('Run "modprobe wsgnf"', "    modprobe wsgnf"))
        text, where = patcher.insert_hook(script.read_text(), PROFILES["generic"], ["/x.ko"])
        self.assertIn("first module load", where)
        self.assertIn("insmod /x.ko", text)


class ModinfoTests(unittest.TestCase):
    def test_reads_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            ko = Path(tmp) / "m.ko"
            ko.write_bytes(fake_module("mymod"))
            info = patcher.read_modinfo(ko)
            self.assertEqual(info["name"], "mymod")
            self.assertEqual(info["vermagic"], VERMAGIC)

    def test_falls_back_to_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            ko = Path(tmp) / "anon.ko"
            ko.write_bytes(os.urandom(128))
            self.assertEqual(patcher.read_modinfo(ko)["name"], "anon")


class ModeParsingTests(unittest.TestCase):
    def test_symbolic_modes(self):
        cases = {
            "-rwxr-xr-x": 0o755,
            "-rw-r--r--": 0o644,
            "-rwsr-xr-x": 0o4755,
            "drwxr-xr-x": 0o755,
            "-rwxr-sr-x": 0o2755,
            "drwxrwxrwt": 0o1777,
            "lrwxrwxrwx": 0o777,
        }
        for text, expected in cases.items():
            self.assertEqual(patcher._symbolic_to_octal(text), expected, text)

    def test_setuid_bit_is_detected(self):
        self.assertTrue(patcher._symbolic_to_octal("-rwsr-xr-x") & stat.S_ISUID)


if __name__ == "__main__":
    unittest.main(verbosity=2)
