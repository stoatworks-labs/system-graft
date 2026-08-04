"""
Reading the kernel itself: what it was built from, and what that means for a module.

Everything else in this tool infers the kernel's properties from the modules
sitting next to it. The kernel image can be asked directly, and it answers two
questions nothing else can:

  * **The build banner.** Every kernel carries "Linux version <release> (<builder>)
    (<compiler>) <build>" in its rodata. That is the exact compiler and the exact
    build flags, neither of which appears in vermagic.
  * **The config.** A kernel built with CONFIG_IKCONFIG embeds its entire .config,
    gzipped, between the markers IKCFG_ST and IKCFG_ED. When it is there, the
    guesswork stops: CONFIG_MODULE_SIG_FORCE, CONFIG_MODVERSIONS and
    CONFIG_TRIM_UNUSED_KSYMS can be read rather than inferred.

Getting at either usually means decompressing the kernel first, because a bzImage
is a small setup stub wrapping a compressed payload. So the approach is the one
the kernel's own scripts/extract-ikconfig takes: try the file as it is, then scan
it for the signature of each compression format, try to decompress from every
offset that matches, and look again in the result.

That sounds brute-force because it is. There is no index and no header pointing
at the payload, and the alternative — asking the user to decompress their kernel
by hand first — is how you get a feature nobody uses.
"""

from __future__ import annotations

import bz2
import gzip
import json
import lzma
import re
import shutil
import subprocess
import zlib
from dataclasses import dataclass, field
from pathlib import Path

IKCFG_START = b"IKCFG_ST"
IKCFG_END = b"IKCFG_ED"

# Refuse to hold more than this from any one decompression attempt. A kernel
# decompresses to well under 200 MB; anything larger is a malformed file or a
# false-positive magic match, and neither deserves the memory.
MAX_DECOMPRESSED = 256 * 1024 * 1024

# Bounded because a magic like gzip's three bytes occurs by chance in any large
# binary, and each false positive costs a decompression attempt.
MAX_ATTEMPTS_PER_FORMAT = 48

# Where a kernel image tends to live next to its initrd. Ordered most specific
# first so a versioned vmlinuz wins over a symlinked one.
KERNEL_GLOBS = (
    "boot/vmlinuz-*", "boot/vmlinux-*", "boot/bzImage-*",
    "boot/vmlinuz", "boot/vmlinux", "boot/bzImage", "boot/kernel", "boot/Image",
    "vmlinuz-*", "vmlinuz", "bzImage", "kernel", "Image",
)


@dataclass
class KernelSpec:
    """What the kernel image says about how it was built."""

    path: Path | None = None
    release: str = ""
    builder: str = ""
    compiler: str = ""
    build: str = ""            # "#1 SMP PREEMPT_RT Thu Jan 1 ..."
    config: dict[str, str] = field(default_factory=dict)
    config_text: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def has_config(self) -> bool:
        return bool(self.config)

    def get(self, key: str) -> str | None:
        """Config value, or None when the key is absent or the config is."""
        return self.config.get(key)

    def is_set(self, key: str) -> bool:
        return self.config.get(key) in ("y", "m")


# --------------------------------------------------------------------------
# Finding the kernel
# --------------------------------------------------------------------------

def find_kernel(*search_dirs: Path) -> Path | None:
    """
    Locate a kernel image near the initrd.

    Searched in the image directory first and the extracted initrd second: an
    appliance normally boots vmlinuz and initrd from the same directory, and only
    some images carry a second copy inside.
    """
    for directory in search_dirs:
        if directory is None or not directory.is_dir():
            continue
        for pattern in KERNEL_GLOBS:
            for candidate in sorted(directory.glob(pattern)):
                # The size floor only exists to skip placeholder files and dangling
                # symlinks that happen to carry a kernel's name. It is deliberately
                # low: guessing a *plausible* minimum kernel size would just be a
                # second way to miss a real one.
                if candidate.is_file() and candidate.stat().st_size > 4096:
                    return candidate
    return None


# --------------------------------------------------------------------------
# Decompression
# --------------------------------------------------------------------------

# Every decompressor takes a memoryview of the tail, never a copy of it. With up
# to 48 attempts per format across five formats, slicing bytes here would copy
# the rest of a 10 MB kernel a couple of hundred times over for no benefit.

def _gunzip_from(data: memoryview) -> bytes | None:
    try:
        obj = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = obj.decompress(data, MAX_DECOMPRESSED)
        return out or None
    except zlib.error:
        return None


def _lzma_from(data: memoryview, fmt: int) -> bytes | None:
    try:
        obj = lzma.LZMADecompressor(format=fmt)
        out = obj.decompress(data, MAX_DECOMPRESSED)
        return out or None
    except lzma.LZMAError:
        return None


def _bzip2_from(data: memoryview) -> bytes | None:
    try:
        obj = bz2.BZ2Decompressor()
        out = obj.decompress(data, MAX_DECOMPRESSED)
        return out or None
    except (OSError, ValueError):
        return None


def _zstd_from(data: memoryview) -> bytes | None:
    try:
        import compression.zstd as czstd  # Python 3.14+
    except ImportError:
        return _zstd_external(data)
    try:
        return czstd.decompress(data) or None
    except Exception:
        # A truncated frame is expected when the payload is followed by other
        # data; fall back to the streaming tool, which keeps what it decoded.
        return _zstd_external(data)


def _zstd_external(data: memoryview) -> bytes | None:
    exe = shutil.which("zstd") or shutil.which("unzstd")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-dc"], input=bytes(data), capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout or None


# (magic, decompressor). Order matters only for speed.
_FORMATS: tuple[tuple[bytes, object], ...] = (
    (b"\x1f\x8b\x08", _gunzip_from),
    (b"\xfd7zXZ\x00", lambda d: _lzma_from(d, lzma.FORMAT_XZ)),
    (b"\x5d\x00\x00", lambda d: _lzma_from(d, lzma.FORMAT_ALONE)),
    (b"BZh", _bzip2_from),
    (b"\x28\xb5\x2f\xfd", _zstd_from),
)


def _decompression_candidates(blob: bytes):
    """
    Yield the raw blob, then every payload we can decompress out of it.

    Yielding the raw blob first matters: an uncompressed vmlinux, or a kernel
    whose config sits outside the compressed payload, needs no work at all.
    """
    yield blob
    view = memoryview(blob)
    for magic, decompress in _FORMATS:
        attempts = 0
        offset = blob.find(magic)
        while offset != -1 and attempts < MAX_ATTEMPTS_PER_FORMAT:
            attempts += 1
            result = decompress(view[offset:])
            if result and len(result) > 4096:
                yield result
            offset = blob.find(magic, offset + 1)


# --------------------------------------------------------------------------
# The two things worth extracting
# --------------------------------------------------------------------------

def extract_config(blob: bytes) -> str | None:
    """Pull the gzipped .config out of a CONFIG_IKCONFIG kernel."""
    start = blob.find(IKCFG_START)
    if start == -1:
        return None
    start += len(IKCFG_START)
    end = blob.find(IKCFG_END, start)
    payload = blob[start:end] if end != -1 else blob[start:]
    try:
        text = gzip.decompress(payload).decode("utf-8", "replace")
    except (OSError, EOFError, zlib.error):
        # Truncated tail: decompress what there is rather than losing the lot.
        try:
            obj = zlib.decompressobj(16 + zlib.MAX_WBITS)
            text = obj.decompress(payload, MAX_DECOMPRESSED).decode("utf-8", "replace")
        except zlib.error:
            return None
    return text if "CONFIG_" in text else None


_BANNER = re.compile(
    rb"Linux version (?P<release>[^\s]{1,128}) "
    rb"\((?P<builder>[^)\x00]{0,128})\) "
    rb"\((?P<compiler>.{0,256}?)\) "
    rb"(?P<build>#\d{1,6}[^\x00\n]{0,256})"
)


def extract_banner(blob: bytes) -> dict | None:
    """Parse the "Linux version ..." string every kernel carries."""
    match = _BANNER.search(blob)
    if not match:
        return None
    return {
        key: match.group(key).decode("utf-8", "replace").strip()
        for key in ("release", "builder", "compiler", "build")
    }


def parse_config(text: str) -> dict[str, str]:
    """
    CONFIG_X=y / ="string" / =123 into a dict, with `# CONFIG_X is not set` kept
    as an explicit "n" — the difference between "off" and "absent" matters when
    deciding whether a check can be trusted.
    """
    config: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# ") and line.endswith(" is not set"):
            key = line[2:-len(" is not set")].strip()
            if key.startswith("CONFIG_"):
                config[key] = "n"
            continue
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.startswith("CONFIG_"):
            config[key] = value.strip().strip('"')
    return config


# --------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------

def analyse(path: Path) -> KernelSpec:
    """Read everything obtainable from a kernel image."""
    spec = KernelSpec(path=path)
    try:
        blob = path.read_bytes()
    except OSError as exc:
        spec.notes.append(f"cannot read {path}: {exc}")
        return spec

    banner: dict | None = None
    config_text: str | None = None
    layers = 0

    for candidate in _decompression_candidates(blob):
        layers += 1
        if banner is None:
            banner = extract_banner(candidate)
        if config_text is None:
            config_text = extract_config(candidate)
        if banner and config_text:
            break

    if banner:
        spec.release = banner["release"]
        spec.builder = banner["builder"]
        spec.compiler = banner["compiler"]
        spec.build = banner["build"]
    else:
        spec.notes.append(
            "no 'Linux version' banner found — this may not be a kernel image, or it "
            "uses a compressor this tool cannot unpack (lz4 and lzo are not supported)")

    if config_text:
        spec.config_text = config_text
        spec.config = parse_config(config_text)
    else:
        spec.notes.append(
            "no embedded .config — this kernel was built without CONFIG_IKCONFIG. "
            "Signing and symbol-trimming behaviour can only be inferred, not read.")

    return spec


# --------------------------------------------------------------------------
# What it means for a module
# --------------------------------------------------------------------------

def implications(spec: KernelSpec) -> list[tuple[str, str]]:
    """
    (level, message) for each config setting that changes what a module must be.

    Only settings that alter whether a module *loads* or how it must be *built*
    are here. This is not a config dump — the config is written out in full by
    --build-spec for anyone who wants the rest.
    """
    out: list[tuple[str, str]] = []
    if not spec.has_config:
        return out

    if spec.is_set("CONFIG_MODULE_SIG_FORCE"):
        out.append(("error",
                    "CONFIG_MODULE_SIG_FORCE=y — this kernel loads ONLY signed modules. "
                    "An unsigned module is refused with ENOKEY however well its vermagic "
                    "matches, and there is no way around it without the signing key."))
    elif spec.is_set("CONFIG_MODULE_SIG"):
        out.append(("info",
                    "CONFIG_MODULE_SIG=y but not SIG_FORCE — unsigned modules load, and are "
                    "recorded as tainting the kernel."))
    else:
        out.append(("ok", "module signatures are not enforced by this kernel"))

    if spec.is_set("CONFIG_MODVERSIONS"):
        out.append(("warn",
                    "CONFIG_MODVERSIONS=y — matching the vermagic string is not enough. Every "
                    "symbol the module imports must have a matching CRC, so it has to be built "
                    "against this exact kernel source and config, not merely the same version."))

    if spec.is_set("CONFIG_TRIM_UNUSED_KSYMS"):
        out.append(("warn",
                    "CONFIG_TRIM_UNUSED_KSYMS=y — this kernel exports only the symbols its own "
                    "built-in code and modules use. An out-of-tree driver can fail to resolve a "
                    "symbol that exists in the source but was trimmed from this build."))

    if spec.is_set("CONFIG_GCC_PLUGIN_RANDSTRUCT") or spec.is_set("CONFIG_RANDSTRUCT") \
            or spec.is_set("CONFIG_RANDSTRUCT_FULL"):
        out.append(("error",
                    "randstruct is enabled — structure layouts were randomised with a per-build "
                    "seed. Without that seed file from the original build tree, a rebuilt module "
                    "will disagree with the kernel about memory layout even if it loads."))

    if spec.is_set("CONFIG_CFI_CLANG"):
        out.append(("warn",
                    "CONFIG_CFI_CLANG=y — this kernel was built with Clang CFI. The module must "
                    "be built by Clang with matching CFI settings, not GCC."))

    compressed = [key for key in spec.config
                  if key.startswith("CONFIG_MODULE_COMPRESS_") and spec.is_set(key)
                  and not key.endswith("_NONE")]
    if compressed:
        which = ", ".join(k.rsplit("_", 1)[-1].lower() for k in compressed)
        out.append(("info",
                    f"modules in this image are expected {which}-compressed — an uncompressed "
                    ".ko still loads via insmod, but keep the naming consistent."))

    local = spec.get("CONFIG_LOCALVERSION")
    if local:
        out.append(("info", f"CONFIG_LOCALVERSION={local!r} — this must be set identically in "
                            "any kernel tree you build against, or the release string differs."))
    return out


def spec_lines(spec: KernelSpec) -> list[tuple[str, str]]:
    """(level, line) describing the kernel and what it demands of a module."""
    lines: list[tuple[str, str]] = [("step", f"Kernel image: {spec.path}")]
    if spec.release:
        lines.append(("info", f"  release:  {spec.release}"))
        lines.append(("info", f"  built by: {spec.builder}"))
        lines.append(("info", f"  compiler: {spec.compiler}"))
        lines.append(("info", f"  build:    {spec.build}"))
    if spec.has_config:
        lines.append(("ok", f"  config:   embedded, {len(spec.config)} settings recovered"))
    lines.append(("info", ""))

    findings = implications(spec)
    if findings:
        lines.append(("info", "What this means for a module you inject:"))
        for level, message in findings:
            # The level carries the severity; the text carries only content. Baking
            # a marker into the string as well gets it rendered twice, once here
            # and once by whichever front end is prefixing by level.
            # "error" here means "this kernel will refuse the module", not "the
            # tool failed" — render it as a warning so it does not read as a crash.
            lines.append(("warn" if level == "error" else level, f"  {message}"))
        lines.append(("info", ""))
    for note in spec.notes:
        lines.append(("info", f"note: {note}"))
    return lines


def format_spec(spec: KernelSpec) -> str:
    return "\n".join(line for _, line in spec_lines(spec)).rstrip() + "\n"


# --------------------------------------------------------------------------
# Writing the spec out
# --------------------------------------------------------------------------

def write_build_spec(spec: KernelSpec, vermagic: str, dest: Path) -> list[Path]:
    """
    Write everything needed to reproduce a matching build.

    The .config is the valuable artefact — it is the one thing that cannot be
    reconstructed from anywhere else, and without it "build against a matching
    kernel tree" is advice rather than an instruction.
    """
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    payload = {
        "kernel_image": str(spec.path) if spec.path else None,
        "release": spec.release,
        "vermagic": vermagic,
        "compiler": spec.compiler,
        "builder": spec.builder,
        "build": spec.build,
        "config_embedded": spec.has_config,
        "config_settings": len(spec.config),
        "notable_config": {
            key: spec.config[key] for key in (
                "CONFIG_MODULE_SIG", "CONFIG_MODULE_SIG_FORCE", "CONFIG_MODULE_SIG_HASH",
                "CONFIG_MODVERSIONS", "CONFIG_TRIM_UNUSED_KSYMS", "CONFIG_LOCALVERSION",
                "CONFIG_GCC_PLUGIN_RANDSTRUCT", "CONFIG_RANDSTRUCT_FULL", "CONFIG_CFI_CLANG",
                "CONFIG_CC_VERSION_TEXT", "CONFIG_SMP", "CONFIG_PREEMPT_RT",
            ) if key in spec.config
        },
        "implications": [{"level": level, "message": message}
                         for level, message in implications(spec)],
        "notes": spec.notes,
    }
    spec_path = dest / "build-spec.json"
    spec_path.write_text(json.dumps(payload, indent=2) + "\n")
    written.append(spec_path)

    if spec.config_text:
        config_path = dest / "config"
        config_path.write_text(spec.config_text)
        written.append(config_path)

    notes_path = dest / "HOW-TO-BUILD.md"
    notes_path.write_text(_build_notes(spec, vermagic))
    written.append(notes_path)
    return written


def _build_notes(spec: KernelSpec, vermagic: str) -> str:
    release = spec.release or "<the image's kernel release>"
    lines = [
        "# Building a module for this image",
        "",
        "Generated by System Graft from the kernel image itself. Everything here was read",
        "out of the kernel, not guessed.",
        "",
        "## What has to match",
        "",
        f"- **Kernel release:** `{release}`",
        f"- **vermagic:** `{vermagic or '(unknown)'}`",
    ]
    if spec.compiler:
        lines.append(f"- **Compiler:** `{spec.compiler}`")
        lines.append("  The compiler version is *not* part of vermagic, so a mismatch is not")
        lines.append("  caught at load time — it just produces a module that may misbehave.")
    if spec.get("CONFIG_LOCALVERSION"):
        lines.append(f"- **CONFIG_LOCALVERSION:** `{spec.get('CONFIG_LOCALVERSION')}`")
    lines.append("")

    findings = implications(spec)
    if findings:
        lines.append("## What the config says")
        lines.append("")
        for level, message in findings:
            prefix = {"error": "**Blocker.** ", "warn": "**Caution.** "}.get(level, "")
            lines.append(f"- {prefix}{message}")
        lines.append("")

    lines += [
        "## Steps",
        "",
        "1. Get the kernel source for this exact release. For a stock distro kernel that is",
        "   the distro's own source package; for an appliance kernel the vendor is obliged to",
        "   publish it under the GPL, and it is usually on their support site.",
        "",
    ]
    if spec.config_text:
        lines += [
            "2. Copy the `config` file next to this document into the source tree as `.config`",
            "   — it is this kernel's real configuration, extracted from the image.",
            "",
            "   ```sh",
            "   cp config /usr/src/linux-" + release + "/.config",
            "   cd /usr/src/linux-" + release,
            "   make olddefconfig && make modules_prepare",
            "   ```",
            "",
        ]
    else:
        lines += [
            "2. Obtain the kernel's `.config`. This kernel was built without CONFIG_IKCONFIG,",
            "   so it is not recoverable from the image — it has to come from the vendor, or",
            "   from `/proc/config.gz` on a running unit.",
            "",
        ]
    lines += [
        "3. Build the module out-of-tree against that prepared tree:",
        "",
        "   ```sh",
        "   make -C /usr/src/linux-" + release + " M=$PWD modules",
        "   ```",
        "",
        "4. Check the result before writing any media:",
        "",
        "   ```sh",
        "   python3 patcher.py <image-dir> --scan .",
        "   ```",
        "",
        "   It must come back as an exact vermagic match. If it does not, the tree you built",
        "   against is not the one this image's kernel came from.",
        "",
    ]
    return "\n".join(lines)
