"""
Tests for the "which driver, and which build of it" half of the tool.

Covers kmod.py (reading a .ko), hardware.py (matching devices against what an
image can drive) and sources.py (ranking candidate modules), plus the three
boot-time failure checks those made possible in patcher.py.

Like test_patcher.py, everything is built from scratch and runs as an ordinary
user. Two fixtures matter:

  * `elf_module` builds a *real*, minimal ELF64 object with a genuine .modinfo
    section. Without it these tests would only ever exercise the byte-scan
    fallback, and the ELF path — the one that runs against every real module —
    would be untested.
  * `ImageBuilder` produces an image carrying the metadata a real appliance
    initrd has and the old fixture did not: modules.alias, modules.builtin,
    modules.builtin.modinfo and /lib/firmware.
"""

from __future__ import annotations

import lzma
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hardware  # noqa: E402
import kmod  # noqa: E402
import patcher  # noqa: E402
import sources  # noqa: E402
from patcher import PROFILES, PatchError, PatchRequest  # noqa: E402

KVER = "6.12.11"
VERMAGIC = f"{KVER} SMP preempt_rt mod_unload modversions"

INIT_SCRIPT = """#!/bin/sh
. /etc/functions

case "$1" in
    start)
        Run "modprobe wsgnf"
        Run "ifconfig lo up"
        ;;
esac
"""


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def elf_module(fields: dict[str, list[str] | str]) -> bytes:
    """
    A minimal but genuine ELF64 relocatable object with a .modinfo section.

    Three sections: NULL, .modinfo, .shstrtab. Enough for the section-table walk
    in kmod._elf_section to have real work to do.
    """
    items: list[bytes] = []
    for key, value in fields.items():
        for one in ([value] if isinstance(value, str) else value):
            items.append(f"{key}={one}".encode())
    modinfo = b"\x00".join(items) + b"\x00"

    shstrtab = b"\x00.modinfo\x00.shstrtab\x00"
    name_modinfo = 1
    name_shstrtab = 10

    ehdr_size = 64
    modinfo_off = ehdr_size
    shstrtab_off = modinfo_off + len(modinfo)
    shoff = shstrtab_off + len(shstrtab)
    shoff += (8 - shoff % 8) % 8  # keep the section headers aligned

    header = bytearray(64)
    header[0:16] = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    struct.pack_into("<HHI", header, 0x10, 1, 0x3E, 1)      # e_type=REL, x86-64, v1
    struct.pack_into("<QQQ", header, 0x18, 0, 0, shoff)     # entry, phoff, shoff
    struct.pack_into("<I", header, 0x30, 0)                 # e_flags
    struct.pack_into("<HHHHHH", header, 0x34, 64, 0, 0, 64, 3, 2)

    def section(name: int, stype: int, offset: int, size: int) -> bytes:
        sh = bytearray(64)
        struct.pack_into("<II", sh, 0x00, name, stype)
        struct.pack_into("<QQ", sh, 0x18, offset, size)
        return bytes(sh)

    body = bytearray()
    body += header
    body += modinfo
    body += shstrtab
    body += b"\x00" * (shoff - len(body))
    body += section(0, 0, 0, 0)
    body += section(name_modinfo, 1, modinfo_off, len(modinfo))
    body += section(name_shstrtab, 3, shstrtab_off, len(shstrtab))
    return bytes(body)


def sign(blob: bytes, signer: str = "test-key") -> bytes:
    """Append a well-formed module signature footer."""
    signature = b"\x00" * 64
    signer_bytes = signer.encode()
    footer = struct.pack("BBBBB", 1, 2, 2, len(signer_bytes), 0) + b"\x00" * 3
    footer += struct.pack(">I", len(signature))
    return blob + signer_bytes + signature + footer + kmod.SIG_MAGIC


def fake_module(name: str, vermagic: str = VERMAGIC, **extra) -> bytes:
    """Non-ELF stand-in, to keep the byte-scan fallback under test too."""
    fields = [f"name={name}", f"vermagic={vermagic}"]
    for key, value in extra.items():
        for one in ([value] if isinstance(value, str) else value):
            fields.append(f"{key}={one}")
    return b"\x00".join(f.encode() for f in fields) + b"\x00" + os.urandom(128)


class ImageBuilder:
    """A SquashFS image with the metadata a real appliance initrd carries."""

    def __init__(self, root: Path):
        self.root = root
        self.tree = root / "tree"
        self.image = root / "initrd"
        self.modules: dict[str, bytes] = {"existing.ko": fake_module("existing")}
        self.aliases: list[tuple[str, str]] = []
        self.builtin: list[str] | None = None
        self.builtin_aliases: list[tuple[str, str]] = []
        self.firmware: list[str] = []

    def build(self) -> Path:
        tree = self.tree
        moddir = tree / "lib" / "modules" / KVER
        (moddir / "updates").mkdir(parents=True)
        (tree / "etc" / "init.d").mkdir(parents=True)
        (tree / "etc" / "init.d" / "system").write_text(INIT_SCRIPT)
        (tree / "etc" / "functions").write_text('Run() { "$@"; }\n')

        for name, blob in self.modules.items():
            (moddir / "updates" / name).write_bytes(blob)
        (moddir / "modules.dep").write_text(
            "".join(f"updates/{name}:\n" for name in self.modules))

        if self.aliases:
            (moddir / "modules.alias").write_text(
                "".join(f"alias {pattern} {module}\n" for pattern, module in self.aliases))
        if self.builtin is not None:
            (moddir / "modules.builtin").write_text(
                "".join(f"kernel/drivers/{name}.ko\n" for name in self.builtin))
        if self.builtin_aliases:
            blob = b"\x00".join(
                f"{module}.alias={pattern}".encode() for pattern, module in self.builtin_aliases)
            (moddir / "modules.builtin.modinfo").write_bytes(blob + b"\x00")
        for blob_name in self.firmware:
            target = tree / "lib" / "firmware" / blob_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"firmware")

        entries = {path.relative_to(tree).as_posix():
                   (0o755 if path.is_dir() else 0o644, 0, 0)
                   for path in sorted(tree.rglob("*"))}
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


def require_squashfs():
    if not (shutil.which("mksquashfs") and shutil.which("unsquashfs")):
        raise unittest.SkipTest("squashfs-tools not installed")


# --------------------------------------------------------------------------
# kmod
# --------------------------------------------------------------------------

class KmodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sg-kmod-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name: str, blob: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(blob)
        return path

    def test_reads_real_elf_modinfo_section(self):
        path = self.write("igb.ko", elf_module({
            "name": "igb", "vermagic": VERMAGIC, "license": "GPL",
            "alias": ["pci:v00008086d000010C9sv*sd*bc*sc*i*",
                      "pci:v00008086d00001521sv*sd*bc*sc*i*"],
            "firmware": "intel/igb.bin",
            "depends": "dca,i2c-algo-bit",
        }))
        info = kmod.read(path)
        self.assertTrue(info.is_elf)
        self.assertEqual(info.name, "igb")
        self.assertEqual(info.vermagic, VERMAGIC)
        self.assertEqual(len(info.aliases), 2, "both aliases must survive")
        self.assertEqual(info.firmware, ["intel/igb.bin"])
        self.assertEqual(info.depends, ["dca", "i2c-algo-bit"])

    def test_elf_section_scan_ignores_data_outside_modinfo(self):
        """A string in the module's data must not be mistaken for an alias."""
        blob = bytearray(elf_module({"name": "clean", "vermagic": VERMAGIC}))
        blob += b"\x00alias=pci:v0000DEADd0000BEEFsv*sd*bc*sc*i*\x00"
        info = kmod.read(self.write("clean.ko", bytes(blob)))
        self.assertEqual(info.aliases, [], "alias outside .modinfo must be ignored")

    def test_byte_scan_fallback_for_non_elf(self):
        info = kmod.read(self.write("fake.ko", fake_module(
            "fake", alias=["pci:v00001AF4d00001000sv*sd*bc*sc*i*"])))
        self.assertFalse(info.is_elf)
        self.assertEqual(info.name, "fake")
        self.assertEqual(len(info.aliases), 1)

    def test_detects_signature(self):
        unsigned = kmod.read(self.write("a.ko", elf_module({"name": "a"})))
        signed = kmod.read(self.write("b.ko", sign(elf_module({"name": "b"}))))
        self.assertFalse(unsigned.signed)
        self.assertTrue(signed.signed)
        self.assertEqual(signed.signature["id_type"], "PKCS#7")
        self.assertEqual(signed.signature["signer"], "test-key")

    def test_reads_xz_compressed_module(self):
        raw = elf_module({"name": "squeezed", "vermagic": VERMAGIC})
        info = kmod.read(self.write("squeezed.ko.xz", lzma.compress(raw)))
        self.assertEqual(info.name, "squeezed")
        self.assertEqual(info.vermagic, VERMAGIC)

    def test_unreadable_file_is_reported_not_raised(self):
        info = kmod.read(self.tmp / "does-not-exist.ko")
        self.assertIsNotNone(info.error)
        self.assertEqual(info.vermagic, "")

    def test_vermagic_comparison_names_the_difference(self):
        ok, reasons = kmod.compare_vermagic(VERMAGIC, VERMAGIC)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

        _, reasons = kmod.compare_vermagic(
            f"{KVER} SMP mod_unload modversions", VERMAGIC)
        joined = " ".join(reasons)
        self.assertIn("preempt_rt", joined, "must name the differing flag")
        self.assertNotIn("kernel release differs", joined,
                         "same release must not be reported as a version difference")

        _, reasons = kmod.compare_vermagic("6.1.0-19-amd64 SMP", VERMAGIC)
        self.assertIn("kernel release differs", " ".join(reasons))

    def test_dependency_ordering(self):
        infos = []
        for name, deps in (("child", "parent"), ("parent", ""), ("loner", "")):
            path = self.write(f"{name}.ko", elf_module(
                {"name": name, "vermagic": VERMAGIC, "depends": deps}))
            infos.append(kmod.read(path))
        ordered, notes = kmod.order_by_depends(infos)
        names = [i.name for i in ordered]
        self.assertLess(names.index("parent"), names.index("child"))
        self.assertTrue(any("reordered" in note for note in notes))

    def test_dependency_cycle_is_reported_not_reordered(self):
        infos = []
        for name, deps in (("a", "b"), ("b", "a")):
            path = self.write(f"{name}.ko", elf_module(
                {"name": name, "vermagic": VERMAGIC, "depends": deps}))
            infos.append(kmod.read(path))
        ordered, notes = kmod.order_by_depends(infos)
        self.assertEqual([i.name for i in ordered], ["a", "b"], "input order must be kept")
        self.assertTrue(any("cycle" in note for note in notes))


# --------------------------------------------------------------------------
# hardware — alias matching and device parsing
# --------------------------------------------------------------------------

class AliasTests(unittest.TestCase):
    def test_tokenises_uppercase_hex_values(self):
        """The lowercase-name/uppercase-value split is what makes this parseable."""
        pattern = hardware.parse_alias("pci:v00008086d000015B8sv*sd*bc02sc00i*")
        self.assertEqual(pattern.fields["v"], "00008086")
        self.assertEqual(pattern.fields["d"], "000015B8", "the 'B' must not split the token")
        self.assertEqual(pattern.fields["sv"], "*")
        self.assertEqual(pattern.fields["bc"], "02")

    def test_usb_alias_tokenises(self):
        pattern = hardware.parse_alias("usb:v046DpC52Bd0012dc00dsc00dp00ic03isc01ip01in00")
        self.assertEqual(pattern.fields["v"], "046D")
        self.assertEqual(pattern.fields["p"], "C52B")
        self.assertEqual(pattern.fields["ic"], "03")

    def test_match_yes_no_unknown(self):
        pattern = hardware.parse_alias("pci:v00008086d000015B8sv*sd*bc*sc*i*")
        device = hardware.Device(bus="pci", vendor="8086", device="15b8")
        self.assertEqual(hardware.match_alias(pattern, device), hardware.MATCH_YES)

        other = hardware.Device(bus="pci", vendor="8086", device="1533")
        self.assertEqual(hardware.match_alias(pattern, other), hardware.MATCH_NO)

        subsys = hardware.parse_alias("pci:v00008086d000015B8sv00001028sd00000962bc*sc*i*")
        self.assertEqual(hardware.match_alias(subsys, device), hardware.MATCH_UNKNOWN,
                         "a pattern needing a subsystem ID we lack is uncertain, not a miss")

        known = hardware.Device(bus="pci", vendor="8086", device="15b8",
                                subsystem_vendor="1028", subsystem_device="0962")
        self.assertEqual(hardware.match_alias(subsys, known), hardware.MATCH_YES)

    def test_bus_mismatch_never_matches(self):
        pattern = hardware.parse_alias("usb:v046DpC52B")
        device = hardware.Device(bus="pci", vendor="046d", device="c52b")
        self.assertEqual(hardware.match_alias(pattern, device), hardware.MATCH_NO)


class DeviceParsingTests(unittest.TestCase):
    def test_lspci_nn(self):
        devices, _ = hardware.parse_devices(
            "00:1f.6 Ethernet controller [0200]: Intel Corporation "
            "Ethernet Connection (2) I219-V [8086:15b8] (rev 31)")
        self.assertEqual(len(devices), 1)
        self.assertEqual((devices[0].vendor, devices[0].device), ("8086", "15b8"))
        self.assertEqual(devices[0].class_code, "020000")
        self.assertIsNone(devices[0].subsystem_vendor)

    def test_lspci_nnmm_recovers_the_subsystem(self):
        devices, _ = hardware.parse_devices(
            '00:1f.6 "Ethernet controller [0200]" "Intel Corporation [8086]" '
            '"Ethernet Connection (2) I219-V [15b8]" -r31 "Dell [1028]" "Device [0962]"')
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].subsystem_vendor, "1028")
        self.assertEqual(devices[0].subsystem_device, "0962")

    def test_lsusb_and_bare_pairs_and_mixed_input(self):
        devices, notes = hardware.parse_devices(
            "Bus 001 Device 003: ID 046d:c52b Logitech, Inc. Unifying Receiver\n"
            "8086:1533 an i210 someone typed in\n"
            "\n"
            "total nonsense line\n")
        buses = sorted(d.bus for d in devices)
        self.assertEqual(buses, ["pci", "usb"])
        self.assertTrue(any("not recognised" in note for note in notes))

    def test_modalias_line(self):
        devices, _ = hardware.parse_devices("pci:v00008086d00001533sv*sd*bc02sc00i00")
        self.assertEqual((devices[0].vendor, devices[0].device), ("8086", "1533"))


# --------------------------------------------------------------------------
# hardware — coverage against a real image
# --------------------------------------------------------------------------

class CoverageTests(unittest.TestCase):
    def setUp(self):
        require_squashfs()
        self.tmp = Path(tempfile.mkdtemp(prefix="sg-cover-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        builder = ImageBuilder(self.tmp)
        builder.modules["igb.ko"] = elf_module({
            "name": "igb", "vermagic": VERMAGIC,
            "alias": "pci:v00008086d00001533sv*sd*bc*sc*i*"})
        builder.aliases = [("pci:v00008086d00001533sv*sd*bc*sc*i*", "igb")]
        builder.builtin = ["net/ethernet/e1000e"]
        builder.builtin_aliases = [("pci:v00008086d000015B8sv*sd*bc*sc*i*", "e1000e")]
        self.image = builder.build()

    def assess(self, text: str, alias_db: Path | None = None):
        with patcher.extracted(self.image) as root:
            index = hardware.build_index(root, KVER)
            if alias_db:
                hardware.load_alias_db(alias_db, index)
            devices, _ = hardware.parse_devices(text)
            return hardware.assess(devices, index), index

    def test_module_builtin_and_uncovered(self):
        findings, _ = self.assess(
            "8086:1533 i210\n"       # module in the image
            "8086:15b8 I219-V\n"     # compiled into the kernel
            "10ec:8168 Realtek\n"    # neither
        )
        by_device = {f.device.device: f for f in findings}
        self.assertEqual(by_device["1533"].status, hardware.COVERED_MODULE)
        self.assertEqual(by_device["1533"].providers[0].module, "igb")
        self.assertEqual(by_device["15b8"].status, hardware.COVERED_BUILTIN)
        self.assertEqual(by_device["15b8"].providers[0].module, "e1000e")
        self.assertEqual(by_device["8168"].status, hardware.UNCOVERED)

    def test_builtin_driver_is_not_reported_as_missing(self):
        """The regression this whole builtin path exists to prevent."""
        findings, _ = self.assess("8086:15b8 I219-V")
        self.assertNotEqual(findings[0].status, hardware.UNCOVERED)

    def test_external_alias_db_names_the_driver(self):
        db = self.tmp / "modules.alias"
        db.write_text("alias pci:v000010ECd00008168sv*sd*bc*sc*i* r8169\n")
        findings, index = self.assess("10ec:8168 Realtek", alias_db=db)
        self.assertEqual(findings[0].status, hardware.UNCOVERED)
        self.assertEqual(findings[0].providers[0].module, "r8169")
        self.assertEqual(findings[0].providers[0].origin, "external")
        report = hardware.format_report(findings, index)
        self.assertIn("r8169", report)

    def test_hint_when_nothing_else_is_known(self):
        findings, _ = self.assess(
            "01:00.0 Ethernet controller [0200]: Realtek Semiconductor Co., Ltd. "
            "RTL8111 [10ec:8168] (rev 15)")
        self.assertEqual(findings[0].status, hardware.UNCOVERED)
        self.assertIn("r8169", findings[0].hint)

    def test_report_mentions_the_alias_db_when_it_would_help(self):
        findings, index = self.assess("10ec:8168 Realtek")
        self.assertIn("--alias-db", hardware.format_report(findings, index))


# --------------------------------------------------------------------------
# sources — ranking candidates
# --------------------------------------------------------------------------

class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sg-scan-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pack = self.tmp / "driverpack"
        self.pack.mkdir()
        (self.pack / "igb.ko").write_bytes(elf_module({"name": "igb", "vermagic": VERMAGIC}))
        (self.pack / "igb-wrongflags.ko").write_bytes(elf_module(
            {"name": "igb", "vermagic": f"{KVER} SMP mod_unload modversions"}))
        (self.pack / "igb-oldkernel.ko").write_bytes(elf_module(
            {"name": "igb", "vermagic": "6.1.0-19-amd64 SMP mod_unload modversions"}))

    def test_ranks_by_vermagic(self):
        candidates, _ = sources.scan([self.pack], VERMAGIC)
        self.assertEqual(candidates[0].verdict, sources.EXACT)
        self.assertEqual(Path(candidates[0].origin).name, "igb.ko")
        verdicts = {Path(c.origin).name: c.verdict for c in candidates}
        self.assertEqual(verdicts["igb-wrongflags.ko"], sources.SAME_RELEASE)
        self.assertEqual(verdicts["igb-oldkernel.ko"], sources.OTHER_RELEASE)

    def test_same_release_explains_the_flag_difference(self):
        candidates, _ = sources.scan([self.pack], VERMAGIC)
        near = next(c for c in candidates if c.verdict == sources.SAME_RELEASE)
        self.assertIn("preempt_rt", " ".join(near.reasons))

    def test_finds_modules_inside_a_tarball(self):
        archive = self.tmp / "drivers.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(self.pack / "igb.ko", arcname="pack/lib/modules/igb.ko")
        candidates, _ = sources.scan([archive], VERMAGIC)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].verdict, sources.EXACT)
        self.assertIn("drivers.tar.gz!", candidates[0].origin)

    def test_finds_compressed_modules(self):
        (self.pack / "extra.ko.xz").write_bytes(
            lzma.compress(elf_module({"name": "extra", "vermagic": VERMAGIC})))
        candidates, _ = sources.scan([self.pack], VERMAGIC, want=["extra"])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].verdict, sources.EXACT)

    def test_want_filters_by_name(self):
        (self.pack / "ixgbe.ko").write_bytes(elf_module({"name": "ixgbe", "vermagic": VERMAGIC}))
        candidates, _ = sources.scan([self.pack], VERMAGIC, want=["ixgbe"])
        self.assertEqual([c.name for c in candidates], ["ixgbe"])

    def test_report_says_so_when_nothing_matches(self):
        candidates, notes = sources.scan([self.pack], "9.9.9 SMP")
        text = sources.format_scan(candidates, "9.9.9 SMP", notes)
        self.assertIn("No exact match", text)


# --------------------------------------------------------------------------
# patcher — the three boot-time failures
# --------------------------------------------------------------------------

class BootFailureCheckTests(unittest.TestCase):
    def setUp(self):
        require_squashfs()
        self.tmp = Path(tempfile.mkdtemp(prefix="sg-checks-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def build(self, **kwargs) -> ImageBuilder:
        builder = ImageBuilder(self.tmp)
        for key, value in kwargs.items():
            setattr(builder, key, value)
        return builder

    def run_patch(self, image: Path, modules: list[Path], name="out.sqfs", **kwargs):
        request = PatchRequest(
            image=image, modules=modules, output=self.tmp / name,
            profile=PROFILES["sgs"], **kwargs)
        return list(patcher.patch(request))

    def module(self, name: str, blob: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(blob)
        return path

    # -------------------------------------------------------------- signing

    def test_unsigned_module_blocked_when_image_is_fully_signed(self):
        builder = self.build()
        builder.modules = {"existing.ko": sign(elf_module(
            {"name": "existing", "vermagic": VERMAGIC}))}
        image = builder.build()
        new = self.module("new.ko", elf_module({"name": "new", "vermagic": VERMAGIC}))
        with self.assertRaises(PatchError) as ctx:
            self.run_patch(image, [new])
        self.assertIn("unsigned", str(ctx.exception))
        self.assertIn("ENOKEY", str(ctx.exception))
        self.assertFalse((self.tmp / "out.sqfs").exists())

    def test_unsigned_override_allows_but_warns(self):
        builder = self.build()
        builder.modules = {"existing.ko": sign(elf_module(
            {"name": "existing", "vermagic": VERMAGIC}))}
        image = builder.build()
        new = self.module("new.ko", elf_module({"name": "new", "vermagic": VERMAGIC}))
        messages = self.run_patch(image, [new], allow_unsigned=True)
        self.assertTrue((self.tmp / "out.sqfs").is_file())
        self.assertTrue(any(level == "warn" and "unsigned" in msg
                            for _, level, msg in messages))

    def test_signed_module_into_signed_image_is_fine(self):
        builder = self.build()
        builder.modules = {"existing.ko": sign(elf_module(
            {"name": "existing", "vermagic": VERMAGIC}))}
        image = builder.build()
        new = self.module("new.ko", sign(elf_module({"name": "new", "vermagic": VERMAGIC})))
        self.run_patch(image, [new])
        self.assertTrue((self.tmp / "out.sqfs").is_file())

    def test_unsigned_image_does_not_trigger_the_check(self):
        image = self.build().build()
        new = self.module("new.ko", elf_module({"name": "new", "vermagic": VERMAGIC}))
        messages = self.run_patch(image, [new])
        self.assertTrue(any(level == "ok" and "signatures are not enforced" in msg
                            for _, level, msg in messages))

    # ------------------------------------------------------------- firmware

    def test_missing_firmware_warns_and_names_the_blob(self):
        image = self.build().build()
        new = self.module("new.ko", elf_module(
            {"name": "new", "vermagic": VERMAGIC, "firmware": "intel/new-1.bin"}))
        messages = self.run_patch(image, [new])
        self.assertTrue(any(level == "warn" and "intel/new-1.bin" in msg
                            for _, level, msg in messages))
        self.assertTrue((self.tmp / "out.sqfs").is_file(), "missing firmware must not block")

    def test_present_firmware_passes(self):
        builder = self.build()
        builder.firmware = ["intel/new-1.bin"]
        image = builder.build()
        new = self.module("new.ko", elf_module(
            {"name": "new", "vermagic": VERMAGIC, "firmware": "intel/new-1.bin"}))
        messages = self.run_patch(image, [new])
        self.assertTrue(any(level == "ok" and "firmware blob" in msg
                            for _, level, msg in messages))

    # --------------------------------------------------------- dependencies

    def test_missing_dependency_blocks_when_builtins_are_known(self):
        builder = self.build()
        builder.builtin = ["net/e1000e"]
        image = builder.build()
        new = self.module("new.ko", elf_module(
            {"name": "new", "vermagic": VERMAGIC, "depends": "mdio"}))
        with self.assertRaises(PatchError) as ctx:
            self.run_patch(image, [new])
        self.assertIn("mdio", str(ctx.exception))
        self.assertIn("insmod does not resolve dependencies", str(ctx.exception))

    def test_builtin_dependency_is_accepted(self):
        builder = self.build()
        builder.builtin = ["net/mdio"]
        image = builder.build()
        new = self.module("new.ko", elf_module(
            {"name": "new", "vermagic": VERMAGIC, "depends": "mdio"}))
        self.run_patch(image, [new])
        self.assertTrue((self.tmp / "out.sqfs").is_file())

    def test_missing_dependency_only_warns_without_modules_builtin(self):
        image = self.build().build()  # builder.builtin stays None
        new = self.module("new.ko", elf_module(
            {"name": "new", "vermagic": VERMAGIC, "depends": "mdio"}))
        messages = self.run_patch(image, [new])
        self.assertTrue((self.tmp / "out.sqfs").is_file())
        self.assertTrue(any(level == "warn" and "cannot be ruled out" in msg
                            for _, level, msg in messages))

    def test_dependency_satisfied_by_another_injected_module(self):
        builder = self.build()
        builder.builtin = []
        image = builder.build()
        parent = self.module("parent.ko", elf_module({"name": "parent", "vermagic": VERMAGIC}))
        child = self.module("child.ko", elf_module(
            {"name": "child", "vermagic": VERMAGIC, "depends": "parent"}))
        self.run_patch(image, [child, parent])
        self.assertTrue((self.tmp / "out.sqfs").is_file())

    def test_injected_modules_are_ordered_by_dependency(self):
        builder = self.build()
        builder.builtin = []
        image = builder.build()
        parent = self.module("parent.ko", elf_module({"name": "parent", "vermagic": VERMAGIC}))
        child = self.module("child.ko", elf_module(
            {"name": "child", "vermagic": VERMAGIC, "depends": "parent"}))
        # Deliberately the wrong way round: insmod would fail in this order.
        self.run_patch(image, [child, parent])

        dest = self.tmp / "extracted"
        subprocess.run(["unsquashfs", "-d", str(dest), "-no-xattrs", str(self.tmp / "out.sqfs")],
                       check=True, capture_output=True)
        script = (dest / "etc/init.d/system").read_text()
        self.assertLess(script.index("parent.ko"), script.index("child.ko"),
                        "parent must be insmod'ed before child")

    def test_image_dependency_not_loaded_by_init_is_flagged(self):
        builder = self.build()
        builder.builtin = []
        builder.modules["helper.ko"] = elf_module({"name": "helper", "vermagic": VERMAGIC})
        image = builder.build()
        new = self.module("new.ko", elf_module(
            {"name": "new", "vermagic": VERMAGIC, "depends": "helper"}))
        messages = self.run_patch(image, [new])
        self.assertTrue(any(level == "warn" and "does not appear to be loaded" in msg
                            for _, level, msg in messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
