"""
Answering "which driver does this machine need, and does the image already have it?"

The image knows far more about itself than the patch path ever asked it. Every
`.ko` carries `alias=` entries naming the PCI/USB IDs it drives; depmod collects
those into `lib/modules/<kver>/modules.alias`; and — critically — drivers compiled
*into* the kernel have their aliases in `modules.builtin.modinfo`. Read all three
and you can take a device list from the target machine and say, per device,
whether this image can drive it.

Getting the builtin case right is the difference between a useful report and a
misleading one: without `modules.builtin.modinfo`, hardware that works perfectly
because its driver is compiled in gets reported as unsupported, and the user goes
off building a module they did not need.

**On matching.** A modalias pattern is a glob, but this does not fnmatch the whole
string, it compares field by field. The reason is missing information: `lspci -nn`
without `-mm` gives no subsystem IDs, and a whole-string glob has no way to say "I
cannot tell" — it just returns no match, which reads as "unsupported". Field-wise
matching can distinguish "this pattern requires a subsystem ID I do not have" from
"this pattern does not match", so an uncertain answer is reported as uncertain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import kmod

# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------


@dataclass
class Device:
    """One piece of hardware, however much of it we managed to learn."""

    bus: str                       # "pci" or "usb"
    vendor: str                    # 4 hex digits, lowercase
    device: str
    subsystem_vendor: str | None = None
    subsystem_device: str | None = None
    class_code: str | None = None  # 6 hex digits for PCI: base, sub, prog-if
    description: str = ""
    slot: str = ""

    def label(self) -> str:
        ids = f"{self.vendor}:{self.device}"
        if self.description:
            return f"{ids}  {self.description}"
        return ids

    def fields(self) -> dict[str, str | None]:
        """Modalias fields, with None meaning "not known from the input"."""
        if self.bus == "pci":
            cls = self.class_code or ""
            return {
                "v": self.vendor.upper().zfill(8),
                "d": self.device.upper().zfill(8),
                "sv": self.subsystem_vendor.upper().zfill(8) if self.subsystem_vendor else None,
                "sd": self.subsystem_device.upper().zfill(8) if self.subsystem_device else None,
                "bc": cls[0:2].upper() if len(cls) >= 2 else None,
                "sc": cls[2:4].upper() if len(cls) >= 4 else None,
                "i": cls[4:6].upper() if len(cls) >= 6 else None,
            }
        return {
            "v": self.vendor.upper().zfill(4),
            "p": self.device.upper().zfill(4),
            "d": None, "dc": None, "dsc": None, "dp": None,
            "ic": None, "isc": None, "ip": None, "in": None,
        }


# --------------------------------------------------------------------------
# Parsing device lists
# --------------------------------------------------------------------------

# lspci -nn:
#   00:1f.6 Ethernet controller [0200]: Intel Corporation I219-V [8086:15b8] (rev 31)
_LSPCI_NN = re.compile(
    r"^(?P<slot>[0-9a-fA-F:.]+)\s+"
    r"(?P<desc>.*?)\s*\[(?P<cls>[0-9a-fA-F]{4})\]:\s*"
    r"(?P<name>.*?)\s*\[(?P<vendor>[0-9a-fA-F]{4}):(?P<device>[0-9a-fA-F]{4})\]"
)

# lspci -nnmm gives the subsystem as two further quoted fields.
_LSPCI_MM = re.compile(
    r'^(?P<slot>\S+)\s+"(?P<clsname>[^"]*)\[(?P<cls>[0-9a-fA-F]{4})\]"\s+'
    r'"(?P<vname>[^"]*)\[(?P<vendor>[0-9a-fA-F]{4})\]"\s+'
    r'"(?P<dname>[^"]*)\[(?P<device>[0-9a-fA-F]{4})\]"'
    r'(?P<rest>.*)$'
)
_LSPCI_MM_SUB = re.compile(
    r'"(?P<svname>[^"]*)\[(?P<sv>[0-9a-fA-F]{4})\]"\s+"(?P<sdname>[^"]*)\[(?P<sd>[0-9a-fA-F]{4})\]"'
)

# lsusb:
#   Bus 001 Device 003: ID 046d:c52b Logitech, Inc. Unifying Receiver
_LSUSB = re.compile(
    r"^Bus\s+\d+\s+Device\s+\d+:\s+ID\s+"
    r"(?P<vendor>[0-9a-fA-F]{4}):(?P<device>[0-9a-fA-F]{4})\s*(?P<desc>.*)$"
)

# A bare "8086:15b8" list, optionally with trailing text.
_BARE = re.compile(r"^(?P<vendor>[0-9a-fA-F]{4}):(?P<device>[0-9a-fA-F]{4})\b\s*(?P<desc>.*)$")

# A raw modalias line, as found in /sys/bus/*/devices/*/modalias.
_MODALIAS_LINE = re.compile(r"^(pci|usb):[vpd]")


def parse_devices(text: str) -> tuple[list[Device], list[str]]:
    """
    Parse whatever device listing the user pasted.

    Accepts `lspci -nn`, `lspci -nnmm`, `lsusb`, raw modalias strings from
    /sys, and a bare list of vendor:device pairs. Mixed input is fine — an
    appliance's NIC and its USB console adapter usually come from two different
    commands, and asking someone to run them separately just loses information.

    Returns (devices, notes).
    """
    devices: list[Device] = []
    notes: list[str] = []
    unparsed = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        match = _LSPCI_MM.match(line)
        if match:
            sub = _LSPCI_MM_SUB.search(match.group("rest") or "")
            devices.append(Device(
                bus="pci",
                vendor=match.group("vendor").lower(),
                device=match.group("device").lower(),
                subsystem_vendor=sub.group("sv").lower() if sub else None,
                subsystem_device=sub.group("sd").lower() if sub else None,
                class_code=match.group("cls").lower() + "00",
                description=f"{match.group('vname').strip()} {match.group('dname').strip()}".strip(),
                slot=match.group("slot"),
            ))
            continue

        match = _LSPCI_NN.match(line)
        if match:
            devices.append(Device(
                bus="pci",
                vendor=match.group("vendor").lower(),
                device=match.group("device").lower(),
                class_code=match.group("cls").lower() + "00",
                description=f"{match.group('desc').strip()}: {match.group('name').strip()}".strip(": "),
                slot=match.group("slot"),
            ))
            continue

        match = _LSUSB.match(line)
        if match:
            devices.append(Device(
                bus="usb",
                vendor=match.group("vendor").lower(),
                device=match.group("device").lower(),
                description=match.group("desc").strip(),
            ))
            continue

        if _MODALIAS_LINE.match(line):
            device = _device_from_modalias(line)
            if device:
                devices.append(device)
                continue

        match = _BARE.match(line)
        if match:
            devices.append(Device(
                bus="pci",
                vendor=match.group("vendor").lower(),
                device=match.group("device").lower(),
                description=match.group("desc").strip(),
            ))
            continue

        unparsed += 1

    if unparsed:
        notes.append(f"{unparsed} line(s) not recognised as a device listing and ignored")
    if not devices:
        notes.append("no devices found — expected `lspci -nn`, `lsusb`, "
                     "modalias strings, or a list of vendor:device pairs")
    return devices, notes


def _device_from_modalias(line: str) -> Device | None:
    bus, _, body = line.partition(":")
    fields = _split_modalias_fields(body)
    if bus == "pci" and "v" in fields and "d" in fields:
        cls = (fields.get("bc", "") or "") + (fields.get("sc", "") or "") + (fields.get("i", "") or "")
        return Device(
            bus="pci",
            vendor=fields["v"][-4:].lower(),
            device=fields["d"][-4:].lower(),
            subsystem_vendor=(fields["sv"][-4:].lower() if fields.get("sv", "*") != "*" else None),
            subsystem_device=(fields["sd"][-4:].lower() if fields.get("sd", "*") != "*" else None),
            class_code=cls.lower() if len(cls) == 6 and "*" not in cls else None,
        )
    if bus == "usb" and "v" in fields and "p" in fields:
        return Device(bus="usb", vendor=fields["v"][-4:].lower(), device=fields["p"][-4:].lower())
    return None


# --------------------------------------------------------------------------
# modalias matching
# --------------------------------------------------------------------------

# Field names are lowercase, values are uppercase hex or "*". That is what makes
# a modalias unambiguously tokenisable: "d000015B8" splits at the lowercase "d"
# even though "B" is a hex digit.
_ALIAS_TOKEN = re.compile(r"([a-z]+)([0-9A-F*]*)")


def _split_modalias_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name, value in _ALIAS_TOKEN.findall(body):
        fields[name] = value
    return fields


@dataclass(frozen=True)
class AliasPattern:
    raw: str
    bus: str
    fields: dict[str, str]

    def __hash__(self):
        return hash(self.raw)


def parse_alias(alias: str) -> AliasPattern | None:
    bus, sep, body = alias.partition(":")
    if not sep or bus not in ("pci", "usb"):
        return None
    return AliasPattern(raw=alias, bus=bus, fields=_split_modalias_fields(body))


MATCH_NO = 0
MATCH_YES = 1
MATCH_UNKNOWN = 2


def match_alias(pattern: AliasPattern, device: Device) -> int:
    """
    Field-wise comparison of a device against one alias pattern.

    MATCH_UNKNOWN means every field we *could* compare agreed, but the pattern
    constrains a field the input did not tell us — typically a subsystem ID
    missing because `lspci` was run without `-mm`. Reporting that honestly beats
    calling it a miss.
    """
    if pattern.bus != device.bus:
        return MATCH_NO
    known = device.fields()
    uncertain = False
    for name, want in pattern.fields.items():
        if want in ("", "*"):
            continue
        have = known.get(name)
        if have is None:
            uncertain = True
            continue
        if have.lstrip("0").upper() != want.lstrip("0").upper():
            return MATCH_NO
    return MATCH_UNKNOWN if uncertain else MATCH_YES


# --------------------------------------------------------------------------
# The image's side: what it can drive
# --------------------------------------------------------------------------

@dataclass
class Provider:
    """One module (loadable or builtin) that claims some hardware."""

    module: str
    origin: str          # "module", "builtin", or "external"
    path: str = ""

    def describe(self) -> str:
        if self.origin == "builtin":
            return f"{self.module} (built into the kernel)"
        if self.origin == "external":
            return f"{self.module} (from the alias database)"
        return self.module


@dataclass
class CoverageIndex:
    """Every alias this image can satisfy, and who satisfies it."""

    kver: str = ""
    entries: list[tuple[AliasPattern, Provider]] = field(default_factory=list)
    modules: dict[str, str] = field(default_factory=dict)     # name -> path in image
    builtin: set[str] = field(default_factory=set)
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, alias: str, provider: Provider) -> None:
        pattern = parse_alias(alias)
        if pattern is not None:
            self.entries.append((pattern, provider))

    def lookup(self, device: Device) -> tuple[list[Provider], list[Provider]]:
        """(certain matches, uncertain matches)."""
        certain: list[Provider] = []
        uncertain: list[Provider] = []
        for pattern, provider in self.entries:
            verdict = match_alias(pattern, device)
            if verdict == MATCH_YES and provider not in certain:
                certain.append(provider)
            elif verdict == MATCH_UNKNOWN and provider not in uncertain:
                uncertain.append(provider)
        return certain, [p for p in uncertain if p not in certain]

    def has_module(self, name: str) -> bool:
        return name in self.modules or name in self.builtin


def _normalise(name: str) -> str:
    return name.replace("-", "_")


def build_index(root: Path, kver: str) -> CoverageIndex:
    """
    Build the coverage index from an extracted image root.

    Reads, in order of authority: modules.alias (depmod's own output),
    modules.builtin.modinfo (aliases of compiled-in drivers), and finally each
    .ko's own alias entries — the last as a backstop for images whose
    modules.alias is stale or missing, which does happen in hand-built initrds.
    """
    index = CoverageIndex(kver=kver)
    moddir = root / "lib" / "modules" / kver

    for ko in sorted(moddir.rglob("*.ko*")):
        if ko.suffix not in (".ko", ".xz", ".gz", ".zst"):
            continue
        info = kmod.read(ko)
        index.modules[_normalise(info.name)] = str(ko.relative_to(root))

    alias_file = moddir / "modules.alias"
    if alias_file.is_file():
        count = 0
        for line in alias_file.read_text(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "alias":
                name = _normalise(parts[2])
                index.add(parts[1], Provider(module=name, origin="module",
                                             path=index.modules.get(name, "")))
                count += 1
        index.sources.append(f"modules.alias ({count} aliases)")
    else:
        index.notes.append("no modules.alias in the image — falling back to per-module alias entries")

    builtin_list = moddir / "modules.builtin"
    if builtin_list.is_file():
        for line in builtin_list.read_text(errors="replace").splitlines():
            stem = line.strip().split("/")[-1]
            if stem.endswith(".ko"):
                index.builtin.add(_normalise(stem[:-3]))
        index.sources.append(f"modules.builtin ({len(index.builtin)} built-in drivers)")

    builtin_modinfo = moddir / "modules.builtin.modinfo"
    if builtin_modinfo.is_file():
        count = 0
        for item in builtin_modinfo.read_bytes().split(b"\x00"):
            if not item:
                continue
            text = item.decode("utf-8", "replace")
            key, _, value = text.partition("=")
            module, _, field_name = key.partition(".")
            if field_name == "alias" and value:
                index.add(value, Provider(module=_normalise(module), origin="builtin"))
                count += 1
            if module:
                index.builtin.add(_normalise(module))
        index.sources.append(f"modules.builtin.modinfo ({count} built-in aliases)")
    elif index.builtin:
        index.notes.append(
            "this image lists built-in drivers but has no modules.builtin.modinfo "
            "(kernels before 5.2 do not ship one), so hardware driven by a compiled-in "
            "driver may be reported as uncovered")
    else:
        index.notes.append(
            "no modules.builtin.modinfo — hardware driven by a driver compiled into "
            "the kernel cannot be detected and may be reported as uncovered")

    from_modules = 0
    for name, rel in index.modules.items():
        info = kmod.read(root / rel)
        for alias in info.aliases:
            index.add(alias, Provider(module=name, origin="module", path=rel))
            from_modules += 1
    if from_modules:
        index.sources.append(f"per-module alias entries ({from_modules})")

    return index


def load_alias_db(path: Path, index: CoverageIndex) -> int:
    """
    Fold an external modules.alias into the index as "external" providers.

    This is the honest answer to "what module drives this device?" for hardware
    the image does *not* support: the mapping only exists in some kernel's
    modules.alias, so point the tool at one — from any Linux box
    (/lib/modules/$(uname -r)/modules.alias) or an unpacked distro kernel
    package. Anything found there names a real driver, rather than guessing.
    """
    if not path.is_file():
        raise FileNotFoundError(f"alias database not found: {path}")
    count = 0
    for line in path.read_text(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "alias":
            index.add(parts[1], Provider(module=_normalise(parts[2]), origin="external"))
            count += 1
    index.sources.append(f"{path.name} ({count} external aliases)")
    return count


# --------------------------------------------------------------------------
# Hints, for when there is no alias database to consult
# --------------------------------------------------------------------------

# Deliberately coarse and explicitly a hint. A precise vendor:device -> module
# table is exactly what modules.alias already is, and shipping a stale partial
# copy of one would be worse than saying "point me at a real one". These entries
# only narrow the search to a driver family.
_VENDOR_CLASS_HINTS: dict[tuple[str, str], str] = {
    ("8086", "02"): "Intel Ethernet — one of e1000e, igb, igc, ixgbe, i40e, ice depending on the part",
    ("8086", "01"): "Intel storage — ahci, or nvme for an NVMe part",
    ("8086", "28"): "Intel wireless — iwlwifi",
    ("10ec", "02"): "Realtek Ethernet — r8169 (or Realtek's out-of-tree r8168)",
    ("14e4", "02"): "Broadcom Ethernet — tg3, bnx2, bnx2x or bnxt_en depending on the part",
    ("15b3", "02"): "Mellanox Ethernet/IB — mlx4_core or mlx5_core",
    ("1d6a", "02"): "Aquantia/Marvell Ethernet — atlantic",
    ("1969", "02"): "Atheros Ethernet — atl1c / alx",
    ("11ab", "02"): "Marvell Ethernet — sky2",
    ("1077", "0c"): "QLogic HBA — qla2xxx",
    ("1000", "01"): "Broadcom/LSI storage — megaraid_sas or mpt3sas",
    ("1af4", ""):   "virtio device — virtio_net / virtio_blk / virtio_pci",
    ("1b36", ""):   "QEMU virtual device",
}

_CLASS_HINTS: dict[str, str] = {
    "02": "network controller",
    "01": "storage controller",
    "03": "display controller",
    "0c": "serial bus controller",
    "04": "multimedia controller",
}


def hint_for(device: Device) -> str:
    """A coarse family hint. Never presented as an answer."""
    base = (device.class_code or "")[:2]
    specific = _VENDOR_CLASS_HINTS.get((device.vendor, base))
    if specific:
        return specific
    vendor_any = _VENDOR_CLASS_HINTS.get((device.vendor, ""))
    if vendor_any:
        return vendor_any
    if base in _CLASS_HINTS:
        return f"unrecognised {_CLASS_HINTS[base]}"
    return ""


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

COVERED_MODULE = "module"
COVERED_BUILTIN = "builtin"
UNCERTAIN = "uncertain"
UNCOVERED = "uncovered"


@dataclass
class Finding:
    device: Device
    status: str
    providers: list[Provider] = field(default_factory=list)
    hint: str = ""


def assess(devices: list[Device], index: CoverageIndex) -> list[Finding]:
    findings: list[Finding] = []
    for device in devices:
        certain, uncertain = index.lookup(device)
        in_image = [p for p in certain if p.origin != "external"]
        if in_image:
            status = COVERED_BUILTIN if all(p.origin == "builtin" for p in in_image) else COVERED_MODULE
            findings.append(Finding(device=device, status=status, providers=in_image))
            continue
        maybe = [p for p in uncertain if p.origin != "external"]
        if maybe:
            findings.append(Finding(device=device, status=UNCERTAIN, providers=maybe))
            continue
        external = [p for p in certain + uncertain if p.origin == "external"]
        findings.append(Finding(device=device, status=UNCOVERED, providers=external,
                                hint=hint_for(device)))
    return findings


# Each report line carries the level it should be rendered at, so the CLI and the
# GUI log colour the same output the same way without either of them re-deriving
# severity by sniffing at the text.
_STATUS_LEVEL = {
    UNCOVERED: "warn",
    UNCERTAIN: "warn",
    COVERED_MODULE: "ok",
    COVERED_BUILTIN: "ok",
}


def report_lines(findings: list[Finding], index: CoverageIndex) -> list[tuple[str, str]]:
    """(level, line) for the whole coverage report."""
    lines: list[tuple[str, str]] = [("step", f"Hardware coverage for kernel {index.kver}")]
    if index.sources:
        lines.append(("info", "  index built from: " + "; ".join(index.sources)))
    lines.append(("info", ""))

    buckets = {
        UNCOVERED: "NOT COVERED — no driver in this image claims these",
        UNCERTAIN: "UNCERTAIN — matched except for fields your input did not include",
        COVERED_MODULE: "covered by a module in the image",
        COVERED_BUILTIN: "covered by a driver built into the kernel",
    }
    for status, heading in buckets.items():
        group = [f for f in findings if f.status == status]
        if not group:
            continue
        level = _STATUS_LEVEL[status]
        lines.append((level, f"{heading}  ({len(group)})"))
        for finding in group:
            lines.append((level, f"  {finding.device.label()}"))
            if finding.providers:
                lines.append(("info", "      " + ", ".join(p.describe() for p in finding.providers)))
            if finding.hint:
                lines.append(("info", f"      hint: {finding.hint}"))
            if status == UNCERTAIN:
                lines.append(("info", "      re-run with `lspci -nnmm` to include subsystem "
                                      "IDs and settle this"))
        lines.append(("info", ""))

    if any(f.status == UNCOVERED and not f.providers for f in findings) and \
            not any("external aliases" in s for s in index.sources):
        lines.append(("info", "To name the driver for uncovered devices, supply a modules.alias "
                              "from any Linux system"))
        lines.append(("info", "  (CLI: --alias-db /lib/modules/$(uname -r)/modules.alias)"))
        lines.append(("info", ""))

    for note in index.notes:
        lines.append(("info", f"note: {note}"))
    return lines


def format_report(findings: list[Finding], index: CoverageIndex) -> str:
    """Plain text, because this ends up in a log, an issue, or an email."""
    return "\n".join(line for _, line in report_lines(findings, index)).rstrip() + "\n"
