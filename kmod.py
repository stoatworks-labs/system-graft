"""
Reading a kernel module file (.ko) — metadata, aliases, signature, dependencies.

Split out of patcher.py because "what is in this .ko" outgrew what the patch path
needed. Hardware coverage (hardware.py) and candidate scanning (sources.py) both
want this, and neither should have to import the SquashFS machinery to get it.

Two constraints carried over from patcher.py, both deliberate:

  * **No readelf/objdump.** The tool must work anywhere Python does, so the ELF
    section table is walked in-process. When the input is not ELF at all — a
    truncated download, or the synthetic fixtures the tests build — parsing
    degrades to the byte scan patcher.py always used, rather than failing.
  * **Reading is total.** A malformed or truncated .ko yields whatever could be
    recovered, not an exception. This code runs against files the user found
    somewhere; "I could not parse it" is a finding to report, not a crash.

Why the ELF path matters even though the byte scan works: a scan of the whole
file for `alias=` also hits any such string in the module's *data*, and drivers
embed plenty of strings. Narrowing to the real .modinfo section is the difference
between an alias list you can act on and one you have to second-guess.
"""

from __future__ import annotations

import gzip
import lzma
import re
import struct
import subprocess
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Keys worth recovering when we are reduced to scanning the whole file. With a
# real .modinfo section every key is read, so this list only bounds the fallback.
SCAN_KEYS = (
    "name", "vermagic", "srcversion", "license", "description", "depends",
    "author", "alias", "firmware", "parm", "parmtype", "intree", "version",
    "softdep", "import_ns", "retpoline", "staging",
)

# A signed module carries this at the very end of the file, after a 12-byte
# struct module_signature. Kernels built with CONFIG_MODULE_SIG_FORCE refuse
# anything without it — regardless of how perfectly the vermagic matches.
SIG_MAGIC = b"~Module signature appended~\n"
SIG_STRUCT_LEN = 12
_SIG_ID_TYPES = {0: "PGP", 1: "X509", 2: "PKCS#7"}


class ModuleReadError(Exception):
    """Raised only when the file cannot be read at all."""


# --------------------------------------------------------------------------
# ELF
# --------------------------------------------------------------------------

def _elf_section(blob: bytes, want: str) -> bytes | None:
    """
    Return the contents of a named ELF section, or None if unavailable.

    Handles 32- and 64-bit, both endiannesses. Returns None rather than raising
    on anything malformed — the caller has a working fallback.
    """
    if len(blob) < 64 or blob[:4] != b"\x7fELF":
        return None
    try:
        is64 = blob[4] == 2
        end = "<" if blob[5] == 1 else ">"

        if is64:
            shoff = struct.unpack_from(end + "Q", blob, 0x28)[0]
            shentsize, shnum, shstrndx = struct.unpack_from(end + "HHH", blob, 0x3A)
            off_field, size_field = 0x18, 0x20
            addr_fmt = "Q"
        else:
            shoff = struct.unpack_from(end + "I", blob, 0x20)[0]
            shentsize, shnum, shstrndx = struct.unpack_from(end + "HHH", blob, 0x2E)
            off_field, size_field = 0x10, 0x14
            addr_fmt = "I"

        if not shoff or not shnum or shstrndx >= shnum:
            return None
        if shoff + shentsize * shnum > len(blob):
            return None

        def header(idx: int) -> tuple[int, int, int]:
            base = shoff + idx * shentsize
            name = struct.unpack_from(end + "I", blob, base)[0]
            offset = struct.unpack_from(end + addr_fmt, blob, base + off_field)[0]
            size = struct.unpack_from(end + addr_fmt, blob, base + size_field)[0]
            return name, offset, size

        _, str_off, str_size = header(shstrndx)
        strtab = blob[str_off:str_off + str_size]
        target = want.encode()

        for idx in range(shnum):
            name_off, offset, size = header(idx)
            if name_off >= len(strtab):
                continue
            name_end = strtab.find(b"\x00", name_off)
            if strtab[name_off:name_end] != target:
                continue
            if offset + size > len(blob):
                return None
            return blob[offset:offset + size]
    except (struct.error, IndexError, ValueError):
        return None
    return None


# --------------------------------------------------------------------------
# Reading module bytes, including the compressed forms distros ship
# --------------------------------------------------------------------------

# ELF e_machine -> the architecture name package archives use. Only the ones an
# appliance plausibly runs; anything else comes back as the raw number so the
# caller can report it rather than silently guessing wrong.
_ELF_MACHINES = {
    3: "i386", 20: "powerpc", 21: "ppc64", 22: "s390x", 40: "armhf",
    62: "amd64", 183: "arm64", 243: "riscv64",
}


def architecture(blob: bytes) -> str:
    """
    The architecture a module was built for, from its ELF header.

    Needed because a kernel release string does not always carry the
    architecture: Debian's "6.1.0-19-amd64" does, Ubuntu's "5.15.0-91-generic"
    does not, and picking the wrong architecture's package is a download that
    can only ever produce a module that will not load.
    """
    if len(blob) < 20 or blob[:4] != b"\x7fELF":
        return ""
    end = "<" if blob[5] == 1 else ">"
    try:
        machine = struct.unpack_from(end + "H", blob, 0x12)[0]
    except struct.error:
        return ""
    return _ELF_MACHINES.get(machine, f"elf-machine-{machine}")


def read_module_bytes(path: Path) -> bytes:
    """
    Read a .ko, transparently decompressing the forms distros ship.

    Debian and Ubuntu ship .ko.xz, Fedora .ko.xz, Arch .ko.zst. A scanner that
    only understands bare .ko silently finds nothing in an unpacked distro
    kernel package — which is precisely where a matching module is most likely
    to be.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ModuleReadError(f"cannot read {path}: {exc}") from exc

    suffix = path.suffix.lower()
    try:
        if suffix == ".xz" or raw[:6] == b"\xfd7zXZ\x00":
            return lzma.decompress(raw)
        if suffix == ".gz" or raw[:2] == b"\x1f\x8b":
            return gzip.decompress(raw)
        if suffix == ".zst" or raw[:4] == b"\x28\xb5\x2f\xfd":
            return _zstd_decompress(raw, path)
    except ModuleReadError:
        raise
    except Exception as exc:
        raise ModuleReadError(f"cannot decompress {path.name}: {exc}") from exc
    return raw


def _zstd_decompress(raw: bytes, path: Path) -> bytes:
    """zstd is not in the stdlib before 3.14, so shell out when we must."""
    try:
        import compression.zstd as czstd  # Python 3.14+
        return czstd.decompress(raw)
    except ImportError:
        pass
    exe = shutil.which("zstd") or shutil.which("unzstd")
    if not exe:
        raise ModuleReadError(
            f"{path.name} is zstd-compressed and this Python has no zstd support. "
            "Install zstd (brew install zstd) to read it.")
    out = subprocess.run([exe, "-dc", str(path)], capture_output=True)
    if out.returncode != 0:
        raise ModuleReadError(f"zstd failed on {path.name}: {out.stderr.decode(errors='replace').strip()}")
    return out.stdout


# --------------------------------------------------------------------------
# modinfo
# --------------------------------------------------------------------------

def _parse_modinfo_section(data: bytes) -> dict[str, list[str]]:
    """.modinfo is a run of NUL-separated key=value strings."""
    out: dict[str, list[str]] = {}
    for item in data.split(b"\x00"):
        if not item or b"=" not in item:
            continue
        key, _, value = item.partition(b"=")
        name = key.decode("utf-8", "replace").strip()
        # Guard against a section that is not what we think it is.
        if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name):
            continue
        out.setdefault(name, []).append(value.decode("utf-8", "replace").strip())
    return out


def _scan_modinfo(blob: bytes) -> dict[str, list[str]]:
    """Fallback for non-ELF input: targeted search for the keys we know."""
    out: dict[str, list[str]] = {}
    for key in SCAN_KEYS:
        pattern = re.compile(rb"(?:^|\x00)" + key.encode() + rb"=([^\x00]{0,512})")
        for match in pattern.finditer(blob):
            value = match.group(1).decode("utf-8", "replace").strip()
            if value:
                out.setdefault(key, []).append(value)
    return out


@dataclass
class ModInfo:
    """Everything recoverable from one .ko."""

    path: Path
    fields: dict[str, list[str]] = field(default_factory=dict)
    signed: bool = False
    signature: dict | None = None
    is_elf: bool = False
    arch: str = ""
    size: int = 0
    error: str | None = None

    def get(self, key: str, default: str = "") -> str:
        values = self.fields.get(key)
        return values[0] if values else default

    def all(self, key: str) -> list[str]:
        return list(self.fields.get(key, ()))

    @property
    def name(self) -> str:
        return self.get("name") or _base_module_name(self.path)

    @property
    def vermagic(self) -> str:
        return self.get("vermagic")

    @property
    def aliases(self) -> list[str]:
        return self.all("alias")

    @property
    def firmware(self) -> list[str]:
        return self.all("firmware")

    @property
    def depends(self) -> list[str]:
        raw = self.get("depends")
        return [d for d in raw.split(",") if d] if raw else []

    def flat(self) -> dict[str, str]:
        """The single-value view patcher.read_modinfo has always returned."""
        out = {k: v[0] for k, v in self.fields.items() if v}
        out.setdefault("name", self.name)
        return out


def _base_module_name(path: Path) -> str:
    name = path.name
    for suffix in (".zst", ".xz", ".gz"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if name.endswith(".ko"):
        name = name[:-3]
    return name


def signature_info(blob: bytes) -> dict | None:
    """Describe the appended module signature, or None if unsigned."""
    if not blob.endswith(SIG_MAGIC):
        return None
    start = len(blob) - len(SIG_MAGIC) - SIG_STRUCT_LEN
    if start < 0:
        return {"id_type": "unknown", "sig_len": 0, "signer": ""}
    algo, hash_, id_type, signer_len, key_id_len = struct.unpack_from("BBBBB", blob, start)
    sig_len = struct.unpack_from(">I", blob, start + 8)[0]
    signer = ""
    signer_start = start - sig_len - key_id_len - signer_len
    if signer_start >= 0 and signer_len:
        signer = blob[signer_start:signer_start + signer_len].decode("utf-8", "replace")
    return {
        "id_type": _SIG_ID_TYPES.get(id_type, f"type {id_type}"),
        "sig_len": sig_len,
        "signer": signer,
        "algo": algo,
        "hash": hash_,
    }


def read(path: Path) -> ModInfo:
    """Read one module file. Never raises for a parse problem — see .error."""
    info = ModInfo(path=path)
    try:
        info.size = path.stat().st_size
    except OSError:
        pass
    try:
        blob = read_module_bytes(path)
    except ModuleReadError as exc:
        info.error = str(exc)
        return info

    info.is_elf = blob[:4] == b"\x7fELF"
    info.arch = architecture(blob)
    section = _elf_section(blob, ".modinfo")
    info.fields = _parse_modinfo_section(section) if section is not None else _scan_modinfo(blob)
    info.signature = signature_info(blob)
    info.signed = info.signature is not None
    return info


# --------------------------------------------------------------------------
# vermagic
# --------------------------------------------------------------------------

@dataclass
class Vermagic:
    raw: str
    release: str
    flags: frozenset[str]

    @property
    def ok(self) -> bool:
        return bool(self.release)


def parse_vermagic(text: str) -> Vermagic:
    """
    Split a vermagic string into its kernel release and its flag set.

    Worth doing because "they differ" is a much less useful thing to tell someone
    than "same kernel, but the image is preempt_rt and your module is not" — the
    first sends them hunting, the second names the build flag to change.
    """
    parts = (text or "").split()
    if not parts:
        return Vermagic(raw=text or "", release="", flags=frozenset())
    return Vermagic(raw=text, release=parts[0], flags=frozenset(parts[1:]))


def compare_vermagic(module: str, image: str) -> tuple[bool, list[str]]:
    """
    (matches, human-readable reasons it does not).

    The kernel compares the whole string, so any difference is fatal; the reasons
    exist purely to make the fix obvious.
    """
    if module == image:
        return True, []
    a, b = parse_vermagic(module), parse_vermagic(image)
    reasons: list[str] = []
    if a.release != b.release:
        reasons.append(f"kernel release differs: module {a.release or '?'!r} vs image {b.release or '?'!r}")
    only_module = sorted(a.flags - b.flags)
    only_image = sorted(b.flags - a.flags)
    if only_module:
        reasons.append(f"module was built with {', '.join(only_module)}; the image's kernel was not")
    if only_image:
        reasons.append(f"the image's kernel has {', '.join(only_image)}; the module was not built with it")
    if not reasons:
        reasons.append(f"strings differ: {module!r} vs {image!r}")
    return False, reasons


# --------------------------------------------------------------------------
# Dependency ordering
# --------------------------------------------------------------------------

def order_by_depends(infos: list[ModInfo]) -> tuple[list[ModInfo], list[str]]:
    """
    Sort modules so that each is preceded by any of its dependencies in the set.

    insmod does not resolve dependencies — unlike modprobe, it fails outright on
    an unresolved symbol. So when two injected modules depend on each other, the
    order they are written into the init script is the difference between a
    working boot and one that quietly loses a driver.

    Returns (ordered, notes). A cycle is reported and the input order kept, since
    guessing at that point helps nobody.
    """
    by_name = {info.name: info for info in infos}
    ordered: list[ModInfo] = []
    seen: set[str] = set()
    visiting: set[str] = set()
    notes: list[str] = []

    def visit(info: ModInfo) -> None:
        if info.name in seen:
            return
        if info.name in visiting:
            notes.append(f"dependency cycle involving {info.name}; leaving the given order alone")
            return
        visiting.add(info.name)
        for dep in info.depends:
            target = by_name.get(dep)
            if target is not None and target is not info:
                visit(target)
        visiting.discard(info.name)
        if info.name not in seen:
            seen.add(info.name)
            ordered.append(info)

    for info in infos:
        visit(info)

    if any("cycle" in note for note in notes):
        return list(infos), notes
    if [i.name for i in ordered] != [i.name for i in infos]:
        notes.append("reordered to satisfy dependencies: " + " -> ".join(i.name for i in ordered))
    return ordered, notes
