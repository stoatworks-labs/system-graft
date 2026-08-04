"""
Tests for reading the kernel image itself.

The fixtures build synthetic kernels rather than shipping one: a blob carrying a
"Linux version" banner and an IKCFG_ST/IKCFG_ED config payload, then the same
thing wrapped in each compression format a real bzImage might use. That second
form is the one that matters — a real kernel is a stub around a compressed
payload, so the extractor's whole job is finding and unwrapping it, and a test
that only ever fed it an uncompressed blob would not exercise that at all.
"""

from __future__ import annotations

import bz2
import gzip
import json
import lzma
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kernelspec  # noqa: E402
import patcher  # noqa: E402
from patcher import PROFILES, PatchError, PatchRequest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_discovery import (  # noqa: E402
    KVER, VERMAGIC, ImageBuilder, elf_module, require_squashfs, sign,
)

BANNER = (f"Linux version {KVER} (builder@buildhost) "
          "(gcc (Debian 12.2.0-14) 12.2.0, GNU ld (GNU Binutils for Debian) 2.40) "
          "#1 SMP PREEMPT_RT Thu Jan 1 00:00:00 UTC 2026")

BASE_CONFIG = """#
# Automatically generated file; DO NOT EDIT.
#
CONFIG_SMP=y
CONFIG_MODULES=y
CONFIG_MODULE_UNLOAD=y
CONFIG_LOCALVERSION=""
CONFIG_CC_VERSION_TEXT="gcc (Debian 12.2.0-14) 12.2.0"
# CONFIG_MODULE_SIG is not set
# CONFIG_MODVERSIONS is not set
# CONFIG_TRIM_UNUSED_KSYMS is not set
"""


def kernel_blob(config: str | None = BASE_CONFIG, banner: str = BANNER) -> bytes:
    """An uncompressed 'vmlinux': padding, a banner, and an IKCFG payload."""
    body = bytearray(os.urandom(8192))
    body += b"\x00" + banner.encode() + b"\x00"
    body += os.urandom(4096)
    if config is not None:
        body += kernelspec.IKCFG_START + gzip.compress(config.encode()) + kernelspec.IKCFG_END
    body += os.urandom(4096)
    return bytes(body)


def wrap(blob: bytes, how: str) -> bytes:
    """Wrap a kernel in a compressed payload, the way a bzImage does."""
    stub = bytes(os.urandom(1024))
    if how == "gzip":
        return stub + gzip.compress(blob)
    if how == "xz":
        return stub + lzma.compress(blob, format=lzma.FORMAT_XZ)
    if how == "lzma":
        return stub + lzma.compress(blob, format=lzma.FORMAT_ALONE)
    if how == "bzip2":
        return stub + bz2.compress(blob)
    raise ValueError(how)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sg-kspec-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name: str, blob: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(blob)
        return path

    def test_reads_banner_and_config_uncompressed(self):
        spec = kernelspec.analyse(self.write("vmlinux", kernel_blob()))
        self.assertEqual(spec.release, KVER)
        self.assertEqual(spec.builder, "builder@buildhost")
        self.assertIn("gcc (Debian 12.2.0-14) 12.2.0", spec.compiler)
        self.assertIn("#1 SMP PREEMPT_RT", spec.build)
        self.assertTrue(spec.has_config)
        self.assertEqual(spec.get("CONFIG_SMP"), "y")

    def test_reads_through_each_compression_wrapper(self):
        for how in ("gzip", "xz", "lzma", "bzip2"):
            with self.subTest(compression=how):
                path = self.write(f"bzImage-{how}", wrap(kernel_blob(), how))
                spec = kernelspec.analyse(path)
                self.assertEqual(spec.release, KVER, f"{how}: banner not recovered")
                self.assertTrue(spec.has_config, f"{how}: config not recovered")

    def test_missing_config_is_reported_not_fatal(self):
        spec = kernelspec.analyse(self.write("vmlinux", kernel_blob(config=None)))
        self.assertEqual(spec.release, KVER, "the banner must still be read")
        self.assertFalse(spec.has_config)
        self.assertTrue(any("CONFIG_IKCONFIG" in note for note in spec.notes))

    def test_not_a_kernel_is_reported_not_fatal(self):
        spec = kernelspec.analyse(self.write("random", os.urandom(200_000)))
        self.assertEqual(spec.release, "")
        self.assertFalse(spec.has_config)
        self.assertTrue(any("Linux version" in note for note in spec.notes))

    def test_unreadable_path_is_reported(self):
        spec = kernelspec.analyse(self.tmp / "nope")
        self.assertTrue(any("cannot read" in note for note in spec.notes))

    def test_config_distinguishes_unset_from_absent(self):
        config = parse = kernelspec.parse_config(BASE_CONFIG)
        self.assertEqual(config["CONFIG_MODULE_SIG"], "n", "'is not set' must be recorded")
        self.assertNotIn("CONFIG_CFI_CLANG", parse, "an absent key must stay absent")
        self.assertEqual(config["CONFIG_LOCALVERSION"], "", "quotes must be stripped")

    def test_find_kernel_prefers_boot_directory(self):
        (self.tmp / "boot").mkdir()
        (self.tmp / "boot" / "vmlinuz-6.12.11").write_bytes(os.urandom(100_000))
        (self.tmp / "boot" / "initrd").write_bytes(os.urandom(100_000))
        found = kernelspec.find_kernel(self.tmp)
        self.assertEqual(found.name, "vmlinuz-6.12.11")

    def test_find_kernel_ignores_tiny_files(self):
        (self.tmp / "boot").mkdir()
        (self.tmp / "boot" / "vmlinuz").write_bytes(b"too small to be a kernel")
        self.assertIsNone(kernelspec.find_kernel(self.tmp))


class ImplicationTests(unittest.TestCase):
    def spec_for(self, extra: str) -> kernelspec.KernelSpec:
        spec = kernelspec.KernelSpec(release=KVER)
        spec.config = kernelspec.parse_config(BASE_CONFIG + extra)
        return spec

    def levels(self, spec) -> dict[str, str]:
        return {message: level for level, message in kernelspec.implications(spec)}

    def test_sig_force_is_an_error(self):
        spec = self.spec_for("CONFIG_MODULE_SIG=y\nCONFIG_MODULE_SIG_FORCE=y\n")
        found = [m for m in self.levels(spec) if "SIG_FORCE" in m]
        self.assertTrue(found)
        self.assertEqual(self.levels(spec)[found[0]], "error")
        self.assertIn("ENOKEY", found[0])

    def test_sig_without_force_is_only_informational(self):
        spec = self.spec_for("CONFIG_MODULE_SIG=y\n# CONFIG_MODULE_SIG_FORCE is not set\n")
        found = [m for m in self.levels(spec) if "SIG_FORCE" in m or "not SIG_FORCE" in m]
        self.assertTrue(found)
        self.assertEqual(self.levels(spec)[found[0]], "info")

    def test_no_signing_reports_ok(self):
        spec = self.spec_for("")
        self.assertTrue(any(level == "ok" and "not enforced" in message
                            for level, message in kernelspec.implications(spec)))

    def test_modversions_and_trim_and_randstruct_are_flagged(self):
        spec = self.spec_for("CONFIG_MODVERSIONS=y\nCONFIG_TRIM_UNUSED_KSYMS=y\n"
                             "CONFIG_GCC_PLUGIN_RANDSTRUCT=y\n")
        messages = " ".join(self.levels(spec))
        self.assertIn("CRC", messages)
        self.assertIn("trimmed", messages)
        self.assertIn("randstruct", messages)

    def test_nothing_claimed_without_a_config(self):
        self.assertEqual(kernelspec.implications(kernelspec.KernelSpec(release=KVER)), [])


class BuildSpecOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sg-bspec-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_writes_config_json_and_instructions(self):
        path = self.tmp / "vmlinux"
        path.write_bytes(kernel_blob(BASE_CONFIG + "CONFIG_MODVERSIONS=y\n"))
        spec = kernelspec.analyse(path)
        written = kernelspec.write_build_spec(spec, VERMAGIC, self.tmp / "out")
        names = sorted(p.name for p in written)
        self.assertEqual(names, ["HOW-TO-BUILD.md", "build-spec.json", "config"])

        payload = json.loads((self.tmp / "out" / "build-spec.json").read_text())
        self.assertEqual(payload["release"], KVER)
        self.assertEqual(payload["vermagic"], VERMAGIC)
        self.assertTrue(payload["config_embedded"])
        self.assertEqual(payload["notable_config"]["CONFIG_MODVERSIONS"], "y")

        config = (self.tmp / "out" / "config").read_text()
        self.assertIn("CONFIG_MODVERSIONS=y", config)
        self.assertIn("CONFIG_MODULES=y", config, "the whole config must be written, not a subset")

        notes = (self.tmp / "out" / "HOW-TO-BUILD.md").read_text()
        self.assertIn(KVER, notes)
        self.assertIn("gcc (Debian 12.2.0-14)", notes)
        self.assertIn("--scan", notes, "must tell the user how to check the result")

    def test_instructions_say_so_when_there_is_no_config(self):
        path = self.tmp / "vmlinux"
        path.write_bytes(kernel_blob(config=None))
        spec = kernelspec.analyse(path)
        kernelspec.write_build_spec(spec, VERMAGIC, self.tmp / "out")
        notes = (self.tmp / "out" / "HOW-TO-BUILD.md").read_text()
        self.assertIn("CONFIG_IKCONFIG", notes)
        self.assertFalse((self.tmp / "out" / "config").exists())


class SigningFactVsInferenceTests(unittest.TestCase):
    """The check the kernel config exists to upgrade."""

    def setUp(self):
        require_squashfs()
        self.tmp = Path(tempfile.mkdtemp(prefix="sg-sigfact-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def build_image(self, config: str | None, signed_modules: bool) -> Path:
        """An image directory holding boot/initrd and boot/vmlinuz."""
        staging = self.tmp / "staging"
        staging.mkdir()
        builder = ImageBuilder(staging)
        module = elf_module({"name": "existing", "vermagic": VERMAGIC})
        builder.modules = {"existing.ko": sign(module) if signed_modules else module}
        initrd = builder.build()

        image_dir = self.tmp / "imagedir"
        (image_dir / "boot").mkdir(parents=True)
        shutil.move(str(initrd), str(image_dir / "boot" / "initrd"))
        if config is not None:
            (image_dir / "boot" / "vmlinuz").write_bytes(kernel_blob(config))
        return image_dir / "boot" / "initrd"

    def run_patch(self, image: Path, module: Path, **kwargs):
        request = PatchRequest(image=image, modules=[module], output=self.tmp / "out.sqfs",
                              profile=PROFILES["sgs"], **kwargs)
        return list(patcher.patch(request))

    def unsigned_module(self) -> Path:
        path = self.tmp / "new.ko"
        path.write_bytes(elf_module({"name": "new", "vermagic": VERMAGIC}))
        return path

    def test_config_says_enforced_so_unsigned_is_refused_as_fact(self):
        image = self.build_image(
            BASE_CONFIG + "CONFIG_MODULE_SIG=y\nCONFIG_MODULE_SIG_FORCE=y\n",
            signed_modules=False)
        with self.assertRaises(PatchError) as ctx:
            self.run_patch(image, self.unsigned_module())
        message = str(ctx.exception)
        self.assertIn("CONFIG_MODULE_SIG_FORCE=y in this kernel's embedded config", message)
        self.assertIn("not a judgement call", message)

    def test_config_says_not_enforced_so_signed_image_does_not_block(self):
        """The inference would have refused this; the config overrules it."""
        image = self.build_image(BASE_CONFIG, signed_modules=True)
        self.run_patch(image, self.unsigned_module())
        self.assertTrue((self.tmp / "out.sqfs").is_file(),
                        "a readable config saying signing is off must beat the inference")

    def test_without_a_kernel_the_inference_still_applies(self):
        image = self.build_image(None, signed_modules=True)
        with self.assertRaises(PatchError) as ctx:
            self.run_patch(image, self.unsigned_module())
        self.assertIn("inferred, not known", str(ctx.exception))

    def test_config_implications_appear_in_the_patch_log(self):
        image = self.build_image(
            BASE_CONFIG + "CONFIG_MODVERSIONS=y\nCONFIG_TRIM_UNUSED_KSYMS=y\n",
            signed_modules=False)
        messages = self.run_patch(image, self.unsigned_module())
        text = " ".join(msg for _, _, msg in messages)
        self.assertIn("CRC", text)
        self.assertIn("trimmed", text)
        self.assertIn("built with gcc (Debian 12.2.0-14) 12.2.0", text)


class InspectCliTests(unittest.TestCase):
    def setUp(self):
        require_squashfs()
        self.tmp = Path(tempfile.mkdtemp(prefix="sg-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        staging = self.tmp / "staging"
        staging.mkdir()
        builder = ImageBuilder(staging)
        initrd = builder.build()
        self.image_dir = self.tmp / "imagedir"
        (self.image_dir / "boot").mkdir(parents=True)
        shutil.move(str(initrd), str(self.image_dir / "boot" / "initrd"))
        (self.image_dir / "boot" / "vmlinuz").write_bytes(
            kernel_blob(BASE_CONFIG + "CONFIG_MODVERSIONS=y\n"))

    def run_cli(self, *args) -> str:
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, str(root / "patcher.py"), str(self.image_dir), "-p", "sgs", *args],
            capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_report_includes_the_kernel(self):
        out = self.run_cli("--report")
        self.assertIn(f"Kernel:   {KVER}", out)
        self.assertIn("gcc (Debian 12.2.0-14) 12.2.0", out)
        self.assertIn("CRC", out, "MODVERSIONS implication must be surfaced")

    def test_build_spec_writes_files(self):
        out = self.run_cli("--build-spec", str(self.tmp / "spec"))
        self.assertIn("Wrote:", out)
        for name in ("build-spec.json", "config", "HOW-TO-BUILD.md"):
            self.assertTrue((self.tmp / "spec" / name).is_file(), f"{name} not written")

    def test_no_image_is_written_in_inspection_mode(self):
        self.run_cli("--report")
        self.assertFalse((self.image_dir / "boot" / "initrd.patched").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
