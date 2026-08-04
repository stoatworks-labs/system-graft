"""
Core logic for injecting out-of-tree kernel modules into a SquashFS initrd.

No GUI dependencies — importable, and runnable as a CLI. The GUI in gui.py drives
the same `patch()` generator.

Design rules this module exists to enforce:
  * The input image is never modified. Output is always a new file.
  * Ownership, permissions and setuid/setgid bits are restored on repack, because
    a non-root extract silently loses all three and would ship a broken image.
  * vermagic is checked against modules already in the image before injecting.
  * Nothing outside the initrd is touched — no vendor container formats, no
    checksums recomputed, no allow-lists edited.

That last rule is a scope commitment, not an implementation detail: whether
modifying any particular image is permitted is decided by that image's licence
terms and is the user's call, and this tool neither asserts an answer nor works
around a restriction.

xattrs are dropped unless req.keep_xattrs, because a macOS extract picks up host
com.apple.* attributes that have no business in a Linux image. File capabilities
(security.capability) and SELinux labels live in xattrs and go with them, so an
image relying on file caps needs keep_xattrs AND to be patched on Linux.
"""

from __future__ import annotations

import contextlib
import grp
import hashlib
import os
import platform
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from _version import __version__

import diag
import distro
import hardware
import kernelspec
import kmod
import sources

SENTINEL_BEGIN = "# >>> system-graft BEGIN (generated — safe to delete this block) >>>"
SENTINEL_END = "# <<< system-graft END <<<"

SQUASHFS_MAGIC = b"hsqs"


class PatchError(Exception):
    """Raised for any condition that should stop the patch cleanly."""


# --------------------------------------------------------------------------
# Target profiles
# --------------------------------------------------------------------------

@dataclass
class Profile:
    key: str
    label: str
    # Where to look for the initrd inside a chosen image directory.
    image_globs: list[str] = field(default_factory=lambda: ["boot/initrd", "initrd", "*.img"])
    # Init script (relative to the extracted root) to add the load hook to.
    init_script: str = "etc/init.d/system"
    # Insert the hook immediately before the first line matching this.
    anchor_regex: str = r'^\s*(Run\s+")?(modprobe|insmod)\b'
    # Fallback anchor if the first is not found.
    fallback_anchor_regex: str = r'^\s*(Run\s+")?(ifconfig|ip\s+link|ip\s+addr)\b'
    # How a module load is written in this image's init idiom.
    load_cmd: str = 'insmod {path}'


PROFILES: dict[str, Profile] = {
    "generic": Profile(
        key="generic",
        label="Generic BusyBox/initrd image",
    ),
    "sgs": Profile(
        key="sgs",
        label="Waves SGS (SoundGrid Server) — boot/initrd, etc/init.d/system",
        image_globs=["boot/initrd"],
        init_script="etc/init.d/system",
        # SGS init scripts wrap every command in Run "..." from /etc/functions,
        # so match that idiom and emit it, to stay consistent with the image.
        anchor_regex=r'^\s*Run\s+"modprobe\b',
        fallback_anchor_regex=r'^\s*Run\s+"ifconfig\b',
        load_cmd='Run "insmod {path}"',
    ),
}


# --------------------------------------------------------------------------
# Tool discovery
# --------------------------------------------------------------------------

def require_tools() -> tuple[str, str]:
    """Locate unsquashfs/mksquashfs, or explain how to get them."""
    un = shutil.which("unsquashfs")
    mk = shutil.which("mksquashfs")
    if not un or not mk:
        hint = "brew install squashfs" if platform.system() == "Darwin" else "install squashfs-tools"
        raise PatchError(f"squashfs-tools not found on PATH. Install it first: {hint}")
    return un, mk


def tool_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr).splitlines()[0].strip()
    except Exception:
        return "unknown version"


# --------------------------------------------------------------------------
# Image discovery / probing
# --------------------------------------------------------------------------

def is_squashfs(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == SQUASHFS_MAGIC
    except OSError:
        return False


def sniff_format(path: Path) -> str:
    """Identify what an initrd-shaped file actually is, so we fail clearly."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return "unreadable"
    if head[:4] == SQUASHFS_MAGIC:
        return "squashfs"
    if head[:2] == b"\x1f\x8b":
        return "gzip (cpio initramfs?)"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd"
    if head[:6] in (b"070701", b"070702", b"070707"):
        return "cpio"
    return "unknown"


def find_images(image_dir: Path, profile: Profile) -> list[Path]:
    """Find SquashFS images under an image directory, profile hints first."""
    found: list[Path] = []
    for pattern in profile.image_globs:
        for candidate in sorted(image_dir.glob(pattern)):
            if candidate.is_file() and is_squashfs(candidate) and candidate not in found:
                found.append(candidate)
    if not found:
        # Fall back to a bounded recursive sweep.
        for candidate in sorted(image_dir.rglob("*")):
            if len(found) >= 16:
                break
            if candidate.is_file() and candidate.stat().st_size > 65536 and is_squashfs(candidate):
                found.append(candidate)
    return found


def probe_squashfs(unsquashfs: str, image: Path) -> dict:
    """Read the superblock so the repack can match the original's parameters."""
    out = subprocess.run([unsquashfs, "-s", str(image)], capture_output=True, text=True)
    if out.returncode != 0:
        raise PatchError(f"unsquashfs -s failed on {image.name}: {out.stderr.strip()}")
    text = out.stdout
    info: dict = {"raw": text}
    m = re.search(r"^Compression\s+(\S+)", text, re.M)
    info["compression"] = m.group(1) if m else "gzip"
    m = re.search(r"^Block size\s+(\d+)", text, re.M)
    info["block_size"] = int(m.group(1)) if m else 131072
    m = re.search(r"SQUASHFS\s+(\d+):(\d+)", text)
    info["version"] = f"{m.group(1)}.{m.group(2)}" if m else "?"
    info["exportable"] = "exportable via NFS" in text
    info["compressed_inodes"] = "Inodes are compressed" in text
    return info


# --------------------------------------------------------------------------
# Module metadata (.ko)
# --------------------------------------------------------------------------

def read_modinfo(ko: Path) -> dict:
    """
    Flat single-value modinfo, as this module has always returned it.

    The parsing itself now lives in kmod.py, which reads every value of every key
    and locates the real .modinfo ELF section instead of scanning the whole file.
    This wrapper stays because the GUI wants a plain dict, and because collapsing
    multi-valued keys (alias, firmware, parm) to their first entry is exactly
    wrong for anything that actually needs them — those callers use kmod.read.
    """
    info = kmod.read(ko)
    if info.error:
        raise PatchError(info.error)
    return info.flat()


def find_image_modules(root: Path) -> list[kmod.ModInfo]:
    """Every module already inside the extracted image."""
    results: list[kmod.ModInfo] = []
    modules_dir = root / "lib" / "modules"
    if not modules_dir.is_dir():
        return results
    for ko in sorted(modules_dir.rglob("*.ko*")):
        if ko.suffix in (".ko", ".xz", ".gz", ".zst"):
            results.append(kmod.read(ko))
    return results


def kernel_versions(root: Path) -> list[str]:
    modules_dir = root / "lib" / "modules"
    if not modules_dir.is_dir():
        return []
    return sorted(d.name for d in modules_dir.iterdir() if d.is_dir())


def _builtin_modules(root: Path, kver: str) -> set[str] | None:
    """
    Names of drivers compiled into the kernel, from modules.builtin.

    Needed before declaring a dependency missing: a module that depends on, say,
    `mdio` is perfectly happy if `mdio` is builtin, and refusing the patch in
    that case would be a false alarm.

    None means the image does not say — no modules.builtin at all, so nothing can
    be ruled out and the caller must downgrade to a warning. An empty *set* is a
    different and much stronger claim: the image says nothing is built in.
    """
    listing = root / "lib" / "modules" / kver / "modules.builtin"
    if not listing.is_file():
        return None
    names = set()
    for line in listing.read_text(errors="replace").splitlines():
        stem = line.strip().split("/")[-1]
        if stem.endswith(".ko"):
            names.add(stem[:-3].replace("-", "_"))
    return names


def _init_loads(root: Path, profile: Profile) -> set[str]:
    """Module names the image's init script appears to load already."""
    script = root / profile.init_script
    if not script.is_file():
        return set()
    try:
        text = script.read_text(errors="replace")
    except OSError:
        return set()
    found = set()
    for match in re.finditer(r"\b(?:modprobe|insmod)\s+(?:-\S+\s+)*(\S+)", text):
        name = match.group(1).strip('"\'')
        name = name.split("/")[-1]
        for suffix in (".zst", ".xz", ".gz"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        if name.endswith(".ko"):
            name = name[:-3]
        found.add(name.replace("-", "_"))
    return found


@contextlib.contextmanager
def extracted(image: Path, keep_xattrs: bool = False):
    """
    Extract an image to a temp directory for read-only inspection, then clean up.

    The patch path does its own extract inline because it has to write into the
    tree; the report paths only read, and this keeps them from duplicating the
    tool discovery and the cleanup.
    """
    unsquashfs, _ = require_tools()
    if not is_squashfs(image):
        raise PatchError(f"{image.name} is not a SquashFS image (detected: {sniff_format(image)})")
    workdir = Path(tempfile.mkdtemp(prefix="system-graft-inspect-"))
    root = workdir / "root"
    try:
        cmd = [unsquashfs, "-d", str(root), "-no-progress"]
        if not keep_xattrs:
            cmd.append("-no-xattrs")
        cmd.append(str(image))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise PatchError(f"unsquashfs failed: {proc.stderr.strip() or proc.stdout.strip()}")
        yield root
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def image_vermagic(root: Path) -> str:
    """The vermagic a module must carry to load into this image, if knowable."""
    for info in find_image_modules(root):
        if info.vermagic:
            return info.vermagic
    return ""


# --------------------------------------------------------------------------
# Ownership / permission preservation
# --------------------------------------------------------------------------

_LLS_RE = re.compile(
    r"^(?P<mode>[-dlbcps][-rwxsStT]{9})\s+"
    r"(?P<owner>\S+)/(?P<group>\S+)\s+"
    r"(?P<size>[\d,]+)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2})\s+"
    r"(?P<path>.+?)(?: -> .*)?$"
)

_MODE_BITS = [
    (stat.S_IRUSR, "r"), (stat.S_IWUSR, "w"),
    (stat.S_IRGRP, "r"), (stat.S_IWGRP, "w"),
    (stat.S_IROTH, "r"), (stat.S_IWOTH, "w"),
]


def _symbolic_to_octal(mode_str: str) -> int:
    """Convert an ls-style mode string (10 chars) to a numeric mode."""
    if len(mode_str) != 10:
        raise ValueError(f"bad mode string: {mode_str!r}")
    perms = mode_str[1:]
    bits = 0
    mapping = [
        (0, stat.S_IRUSR, "r"), (1, stat.S_IWUSR, "w"),
        (3, stat.S_IRGRP, "r"), (4, stat.S_IWGRP, "w"),
        (6, stat.S_IROTH, "r"), (7, stat.S_IWOTH, "w"),
    ]
    for idx, bit, char in mapping:
        if perms[idx] == char:
            bits |= bit
    # Execute / setuid / setgid / sticky are encoded in positions 2, 5, 8.
    if perms[2] in ("x", "s"):
        bits |= stat.S_IXUSR
    if perms[2] in ("s", "S"):
        bits |= stat.S_ISUID
    if perms[5] in ("x", "s"):
        bits |= stat.S_IXGRP
    if perms[5] in ("s", "S"):
        bits |= stat.S_ISGID
    if perms[8] in ("x", "t"):
        bits |= stat.S_IXOTH
    if perms[8] in ("t", "T"):
        bits |= stat.S_ISVTX
    return bits


def _name_to_uid(name: str) -> int:
    if name.isdigit():
        return int(name)
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return 0


def _name_to_gid(name: str) -> int:
    if name.isdigit():
        return int(name)
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return 0


def build_ownership_map(unsquashfs: str, image: Path) -> dict[str, tuple[int, int, int]]:
    """
    Map every path in the source image to (mode, uid, gid).

    This is the whole reason the tool can run without root: unsquashfs as a normal
    user cannot restore ownership or setuid bits, so we read them from the source
    and hand them back to mksquashfs as a pseudo-file.
    """
    out = subprocess.run([unsquashfs, "-lls", str(image)], capture_output=True, text=True)
    if out.returncode != 0:
        raise PatchError(f"unsquashfs -lls failed: {out.stderr.strip()}")

    table: dict[str, tuple[int, int, int]] = {}
    for line in out.stdout.splitlines():
        match = _LLS_RE.match(line.rstrip())
        if not match:
            continue
        path = match.group("path")
        if not path.startswith("squashfs-root"):
            continue
        rel = path[len("squashfs-root"):].lstrip("/")
        # The root directory itself is not addressable via a pseudo-file entry;
        # it is stored under "." and applied with -root-uid/-root-gid/-root-mode.
        # Without this it silently ends up owned by whoever ran the extract.
        rel = rel or "."
        try:
            mode = _symbolic_to_octal(match.group("mode"))
        except ValueError:
            continue
        table[rel] = (
            mode,
            _name_to_uid(match.group("owner")),
            _name_to_gid(match.group("group")),
        )
    return table


def write_pseudo_file(table: dict[str, tuple[int, int, int]], extra: dict[str, tuple[int, int, int]],
                      dest: Path) -> int:
    """Emit a mksquashfs pseudo-file restoring mode/uid/gid for every entry."""
    merged = dict(table)
    merged.update(extra)
    lines = []
    for rel in sorted(merged):
        if rel == ".":
            continue  # applied via -root-uid/-root-gid/-root-mode instead
        mode, uid, gid = merged[rel]
        # mksquashfs pseudo syntax: "filename m mode uid gid"
        lines.append(f'"/{rel}" m {mode & 0o7777:04o} {uid} {gid}')
    dest.write_text("\n".join(lines) + "\n")
    return len(lines)


# --------------------------------------------------------------------------
# Init hook
# --------------------------------------------------------------------------

def strip_existing_hook(text: str) -> str:
    """Remove a previously generated block so patching is idempotent."""
    # The leading whitespace of the BEGIN line must be consumed too. Leaving it
    # behind merges it into the following line, so each re-patch would push the
    # anchor line further right.
    pattern = re.compile(
        r"[ \t]*" + re.escape(SENTINEL_BEGIN) + r".*?" + re.escape(SENTINEL_END) + r"[ \t]*\n?",
        re.S,
    )
    return pattern.sub("", text)


def build_hook(profile: Profile, module_paths: list[str], indent: str = "") -> str:
    """Render the load block, indented to match the line it is inserted above."""
    body = [indent + SENTINEL_BEGIN]
    for path in module_paths:
        body.append(indent + profile.load_cmd.format(path=path))
    body.append(indent + SENTINEL_END)
    return "\n".join(body)


def insert_hook(script_text: str, profile: Profile, module_paths: list[str]) -> tuple[str, str]:
    """
    Insert the module-load block before the image's own first module load.

    Returns (new_text, description_of_where). Anchoring before an existing
    modprobe keeps the hook inside whatever case branch already loads modules,
    which matters: inserting after the shebang would run it on stop as well.
    """
    text = strip_existing_hook(script_text)
    lines = text.splitlines()

    for regex, why in ((profile.anchor_regex, "before the image's first module load"),
                       (profile.fallback_anchor_regex, "before the image's first network setup")):
        pattern = re.compile(regex)
        for idx, line in enumerate(lines):
            if pattern.search(line):
                indent = line[:len(line) - len(line.lstrip())]
                lines.insert(idx, build_hook(profile, module_paths, indent))
                return "\n".join(lines) + "\n", f"{why} (line {idx + 1})"

    # Last resort: after the shebang. Flagged loudly by the caller.
    insert_at = 1 if lines and lines[0].startswith("#!") else 0
    lines.insert(insert_at, build_hook(profile, module_paths))
    return "\n".join(lines) + "\n", f"after the shebang (line {insert_at + 1}) — NO ANCHOR FOUND"


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# The patch operation
# --------------------------------------------------------------------------

@dataclass
class PatchRequest:
    image: Path
    modules: list[Path]
    output: Path
    profile: Profile
    allow_vermagic_mismatch: bool = False
    keep_xattrs: bool = False
    add_to_modules_dep: bool = True
    # Each of these turns off a check for a failure that otherwise happens at
    # boot, not here. Same reasoning as allow_vermagic_mismatch: the override
    # exists, it is off, and turning it on does not make the kernel any more
    # forgiving.
    allow_unsigned: bool = False
    allow_missing_deps: bool = False


def patch(req: PatchRequest):
    """
    Generator yielding (fraction, level, message) as the patch proceeds.

    level is one of: "info", "step", "ok", "warn", "error", "cmd".
    Raises PatchError on any condition that should stop the run.

    Callers must BOTH iterate and handle PatchError -- draining the generator
    without catching will lose the failure. The generator shape is what lets the
    GUI show live progress without patcher.py knowing anything about a GUI;
    don't collapse it into a callback or a return value.

    The vermagic check is the safety property this whole module exists for, and
    req.allow_vermagic_mismatch turns it off. Left on, a module built against
    the wrong kernel is refused here, now, with both strings printed. Turned
    off, the kernel refuses it instead -- SILENTLY, AT BOOT, on a machine that
    may have no console. Anything that makes the override easier to reach
    (flipping the default, widening it, downgrading the refusal to a warning)
    reverses the tool's main purpose.

    Note too that matching the vermagic string is necessary but not sufficient:
    with CONFIG_MODVERSIONS the symbol CRCs must match as well, so an override
    can pass this check and still produce a module that will not load.
    """
    unsquashfs, mksquashfs = require_tools()

    yield 0.00, "step", "Checking tools"
    yield 0.01, "info", f"  {tool_version(unsquashfs)}"
    yield 0.02, "info", f"  {tool_version(mksquashfs)}"

    if not req.image.is_file():
        raise PatchError(f"image not found: {req.image}")
    if not req.modules:
        raise PatchError("no kernel modules selected — this tool does not ship any")
    for ko in req.modules:
        if not ko.is_file():
            raise PatchError(f"module not found: {ko}")

    if req.output.exists():
        raise PatchError(f"output already exists, refusing to overwrite: {req.output}")
    if req.output.resolve() == req.image.resolve():
        raise PatchError("output must not be the input image — the input is never modified")

    yield 0.04, "step", f"Probing {req.image.name}"
    fmt = sniff_format(req.image)
    if fmt != "squashfs":
        raise PatchError(
            f"{req.image.name} is not a SquashFS image (detected: {fmt}). "
            "This tool only handles SquashFS initrds."
        )
    info = probe_squashfs(unsquashfs, req.image)
    yield 0.06, "info", (f"  SquashFS {info['version']}, compression={info['compression']}, "
                         f"block={info['block_size']}")
    yield 0.07, "info", f"  sha256(in)  {sha256(req.image)}"

    workdir = Path(tempfile.mkdtemp(prefix="system-graft-"))
    root = workdir / "root"
    try:
        yield 0.09, "step", "Reading ownership and permission table from source"
        table = build_ownership_map(unsquashfs, req.image)
        setuid_count = sum(1 for m, _, _ in table.values() if m & (stat.S_ISUID | stat.S_ISGID))
        yield 0.13, "info", f"  {len(table)} entries, {setuid_count} setuid/setgid"
        if setuid_count:
            yield 0.13, "info", "  (these would be lost by a plain non-root repack — they will be restored)"

        yield 0.15, "step", "Extracting image"
        cmd = [unsquashfs, "-d", str(root), "-no-progress"]
        if not req.keep_xattrs:
            cmd.append("-no-xattrs")
        cmd.append(str(req.image))
        yield 0.16, "cmd", "  $ " + " ".join(cmd)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise PatchError(f"unsquashfs failed: {proc.stderr.strip() or proc.stdout.strip()}")
        yield 0.35, "ok", f"  extracted to {root}"

        versions = kernel_versions(root)
        if not versions:
            raise PatchError("no /lib/modules/<version> directory in this image — nothing to inject into")
        if len(versions) > 1:
            yield 0.36, "warn", f"  multiple kernel versions present: {', '.join(versions)}; using {versions[0]}"
        kver = versions[0]
        yield 0.37, "info", f"  kernel modules directory: /lib/modules/{kver}"

        yield 0.39, "step", "Checking vermagic compatibility"
        existing = find_image_modules(root)
        image_vermagics = {mi.vermagic for mi in existing if mi.vermagic}
        for mi in existing:
            yield 0.40, "info", f"  image has {mi.name}: vermagic={mi.vermagic or '?'!r}"
        if not image_vermagics:
            yield 0.41, "warn", "  no existing module found to compare against — cannot verify vermagic"

        incoming = [kmod.read(ko) for ko in req.modules]
        for mi in incoming:
            if mi.error:
                raise PatchError(mi.error)
            yield 0.42, "info", (f"  {mi.path.name}: vermagic={mi.vermagic!r} "
                                 f"depends={','.join(mi.depends) or '(none)'}")
            if image_vermagics and mi.vermagic not in image_vermagics:
                reasons: list[str] = []
                for candidate in sorted(image_vermagics):
                    _, why = kmod.compare_vermagic(mi.vermagic, candidate)
                    reasons.extend(why)
                detail = "".join(f"\n      - {reason}" for reason in dict.fromkeys(reasons))
                msg = (f"vermagic mismatch for {mi.path.name}:\n"
                       f"      module: {mi.vermagic!r}\n"
                       f"      image:  {', '.join(repr(v) for v in sorted(image_vermagics))}"
                       f"{detail}\n"
                       "    The kernel will refuse to load this module.")
                if req.allow_vermagic_mismatch:
                    yield 0.43, "warn", "  " + msg + "\n    Continuing anyway (override enabled)."
                else:
                    raise PatchError(
                        msg + "\n    Rebuild the module against a matching kernel tree, or enable "
                        "the override if you know what you are doing."
                    )
            elif image_vermagics:
                yield 0.43, "ok", f"  {mi.path.name}: vermagic matches the image"

        # ------------------------------------------------------------------
        # Three ways a vermagic-perfect module still fails, silently, at boot.
        # ------------------------------------------------------------------

        yield 0.435, "step", "Reading the kernel's own build settings"
        kernel = kernelspec.find_kernel(req.image.parent.parent, req.image.parent, root)
        spec = kernelspec.analyse(kernel) if kernel else None
        if spec is None:
            yield 0.435, "info", ("  no kernel image found next to the initrd — falling back to "
                                  "inference from the modules in the image")
        else:
            yield 0.435, "info", f"  {kernel}"
            if spec.release:
                yield 0.435, "info", f"  release {spec.release}, built with {spec.compiler}"
            for level, message in kernelspec.implications(spec):
                # The signing implication is acted on below rather than merely
                # printed, so don't say it twice.
                if "SIG" in message[:40]:
                    continue
                yield 0.435, level if level != "error" else "warn", "  " + message
            for note in spec.notes:
                yield 0.435, "info", f"  {note}"

        yield 0.44, "step", "Checking module signing"
        signed_existing = [mi for mi in existing if mi.signed]
        unsigned = [mi for mi in incoming if not mi.signed]

        # Two routes to the same question, and they are not equally good. If the
        # kernel embedded its config we can read CONFIG_MODULE_SIG_FORCE and know.
        # Otherwise the only evidence is that every module the vendor shipped is
        # signed, which is a strong hint and nothing more. Keep the distinction
        # visible in the log: a refusal the user cannot check is worth less than
        # one they can.
        enforced = spec.is_set("CONFIG_MODULE_SIG_FORCE") if (spec and spec.has_config) else None

        if enforced is True:
            yield 0.44, "info", "  CONFIG_MODULE_SIG_FORCE=y — read from the kernel's own config"
            if unsigned:
                names = ", ".join(mi.path.name for mi in unsigned)
                msg = (f"unsigned module(s) for a kernel that requires signatures: {names}\n"
                       "    CONFIG_MODULE_SIG_FORCE=y in this kernel's embedded config. It will\n"
                       "    refuse an unsigned module with ENOKEY no matter how well the vermagic\n"
                       "    matches, and there is no way around that without the signing key.")
                if req.allow_unsigned:
                    yield 0.44, "warn", "  " + msg + "\n    Continuing anyway (override enabled)."
                else:
                    raise PatchError(msg + "\n    This one is not a judgement call — the module "
                                           "cannot load. Get a signed build.")
            else:
                yield 0.44, "ok", "  all injected modules are signed"
        elif enforced is False:
            yield 0.44, "ok", ("  this kernel does not enforce module signatures "
                               "(CONFIG_MODULE_SIG_FORCE is not set)")
        elif existing and len(signed_existing) == len(existing):
            yield 0.44, "info", f"  every module in this image is signed ({len(existing)}/{len(existing)})"
            if unsigned:
                names = ", ".join(mi.path.name for mi in unsigned)
                msg = (f"unsigned module(s) for an image whose modules are all signed: {names}\n"
                       "    The kernel's config could not be read, so this is inferred, not known.\n"
                       "    If it was built with CONFIG_MODULE_SIG_FORCE it will refuse them with\n"
                       "    ENOKEY no matter how well the vermagic matches.")
                if req.allow_unsigned:
                    yield 0.44, "warn", "  " + msg + "\n    Continuing anyway (override enabled)."
                else:
                    raise PatchError(
                        msg + "\n    Get a signed build, or enable the override if you know this\n"
                        "    kernel does not enforce signatures."
                    )
            else:
                yield 0.44, "ok", "  all injected modules are signed too"
        elif signed_existing:
            yield 0.44, "info", (f"  {len(signed_existing)} of {len(existing)} image modules are signed; "
                                 "signature enforcement is unlikely")
        else:
            yield 0.44, "ok", "  no signed modules in this image — signatures are not enforced"

        yield 0.45, "step", "Checking firmware"
        firmware_root = root / "lib" / "firmware"
        missing_firmware: list[str] = []
        for mi in incoming:
            for blob in mi.firmware:
                if not (firmware_root / blob).exists():
                    missing_firmware.append(f"{mi.name} needs /lib/firmware/{blob}")
        if missing_firmware:
            for entry in missing_firmware:
                yield 0.45, "warn", f"  missing: {entry}"
            yield 0.45, "warn", ("  A driver whose firmware is absent loads and then fails to bring "
                                 "the device up. Copy the blobs into the image's /lib/firmware, or "
                                 "confirm they are optional for your part.")
        elif any(mi.firmware for mi in incoming):
            yield 0.45, "ok", "  every firmware blob the modules ask for is present in the image"
        else:
            yield 0.45, "ok", "  no modules request firmware"

        yield 0.46, "step", "Checking dependencies"
        index_names = {kmod._base_module_name(mi.path).replace("-", "_") for mi in existing}
        declared_builtin = _builtin_modules(root, kver)
        builtin = declared_builtin or set()
        incoming_names = {mi.name.replace("-", "_") for mi in incoming}
        unresolved: list[str] = []
        for mi in incoming:
            for dep in mi.depends:
                key = dep.replace("-", "_")
                if key in incoming_names or key in index_names or key in builtin:
                    continue
                unresolved.append(f"{mi.name} depends on {dep}, which is not in this image")
        if unresolved:
            for entry in unresolved:
                yield 0.46, "warn", f"  {entry}"
            confident = declared_builtin is not None
            msg = ("unresolved module dependencies:\n" +
                   "".join(f"      - {entry}\n" for entry in unresolved) +
                   "    insmod does not resolve dependencies the way modprobe does — it fails\n"
                   "    outright on an unresolved symbol.")
            if not confident:
                yield 0.46, "warn", ("  This image has no modules.builtin, so a dependency compiled "
                                     "into the kernel cannot be ruled out — treating this as a warning.")
            elif req.allow_missing_deps:
                yield 0.46, "warn", "  " + msg + "    Continuing anyway (override enabled)."
            else:
                raise PatchError(
                    msg + "    Inject the dependencies too, or enable the override.")
        else:
            yield 0.46, "ok", "  every dependency is present in the image or being injected"

        incoming, order_notes = kmod.order_by_depends(incoming)
        for note in order_notes:
            yield 0.47, "warn" if "cycle" in note else "info", f"  {note}"

        loaded_by_init = _init_loads(root, req.profile)
        for mi in incoming:
            for dep in mi.depends:
                key = dep.replace("-", "_")
                if key in incoming_names or key in builtin:
                    continue
                if key in index_names and key not in loaded_by_init:
                    yield 0.47, "warn", (
                        f"  {mi.name} depends on {dep}, which is present in the image but does not "
                        f"appear to be loaded by {req.profile.init_script} — if it is not already "
                        "loaded when the hook runs, insmod will fail")

        yield 0.48, "step", "Injecting modules"
        updates_dir = root / "lib" / "modules" / kver / "updates"
        updates_dir.mkdir(parents=True, exist_ok=True)
        injected_rel: list[str] = []
        extra_perms: dict[str, tuple[int, int, int]] = {}
        for mi in incoming:
            ko = mi.path
            dest = updates_dir / ko.name
            shutil.copy2(ko, dest)
            os.chmod(dest, 0o644)
            rel = str(dest.relative_to(root))
            injected_rel.append("/" + rel)
            extra_perms[rel] = (0o644, 0, 0)
            yield 0.49, "ok", f"  + /{rel}  ({ko.stat().st_size} bytes)"

        # Ensure the parent directory exists in the pseudo table with sane ownership.
        extra_perms[str(updates_dir.relative_to(root))] = (0o755, 0, 0)

        if req.add_to_modules_dep:
            dep = root / "lib" / "modules" / kver / "modules.dep"
            if dep.is_file():
                text = dep.read_text()
                added = []
                for mi in incoming:
                    line = f"updates/{mi.path.name}:"
                    if line not in text:
                        text = text.rstrip("\n") + "\n" + line + "\n"
                        added.append(line)
                if added:
                    dep.write_text(text)
                    yield 0.50, "ok", f"  modules.dep += {', '.join(added)}"
                    yield 0.50, "info", ("  (note: modules.dep.bin is not regenerated — the load hook "
                                         "uses insmod with an absolute path, which does not consult it)")

        yield 0.52, "step", "Adding load hook to init"
        script = root / req.profile.init_script
        if not script.is_file():
            raise PatchError(
                f"init script not found in image: {req.profile.init_script}\n"
                "Pick a different profile, or point the profile at the right script."
            )
        original = script.read_text()
        new_text, where = insert_hook(original, req.profile, injected_rel)
        script.write_text(new_text)
        mode = table.get(req.profile.init_script, (0o755, 0, 0))
        extra_perms[req.profile.init_script] = mode
        if "NO ANCHOR FOUND" in where:
            yield 0.55, "warn", f"  inserted {where}"
            yield 0.55, "warn", "  Review this script before booting — the hook may run on stop as well."
        else:
            yield 0.55, "ok", f"  inserted {where}"
        for line in build_hook(req.profile, injected_rel).splitlines():
            yield 0.56, "info", "    | " + line.strip()

        yield 0.58, "step", "Writing ownership pseudo-file"
        pseudo = workdir / "ownership.pseudo"
        count = write_pseudo_file(table, extra_perms, pseudo)
        yield 0.60, "ok", f"  {count} entries -> {pseudo.name}"

        yield 0.62, "step", "Repacking image"
        req.output.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            mksquashfs, str(root), str(req.output),
            "-comp", info["compression"],
            "-b", str(info["block_size"]),
            "-noappend",
            "-no-progress",
            "-pf", str(pseudo),
        ]
        root_entry = table.get(".")
        if root_entry:
            r_mode, r_uid, r_gid = root_entry
            cmd += ["-root-mode", f"{r_mode & 0o7777:04o}",
                    "-root-uid", str(r_uid), "-root-gid", str(r_gid)]
        if not req.keep_xattrs:
            # On macOS the extract picks up host xattrs (com.apple.*); baking those
            # into a Linux image is at best noise. Explicitly drop them.
            cmd.append("-no-xattrs")
        yield 0.63, "cmd", "  $ " + " ".join(cmd)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise PatchError(f"mksquashfs failed: {proc.stderr.strip() or proc.stdout.strip()}")
        for line in (proc.stdout or "").splitlines():
            if line.strip():
                yield 0.85, "info", "  " + line.strip()

        yield 0.90, "step", "Verifying output"
        if not is_squashfs(req.output):
            raise PatchError("output is not a valid SquashFS image")
        out_info = probe_squashfs(unsquashfs, req.output)
        yield 0.92, "info", (f"  SquashFS {out_info['version']}, compression={out_info['compression']}, "
                             f"block={out_info['block_size']}")

        out_table = build_ownership_map(unsquashfs, req.output)
        restored = sum(1 for m, _, _ in out_table.values() if m & (stat.S_ISUID | stat.S_ISGID))
        yield 0.94, "info", f"  {len(out_table)} entries, {restored} setuid/setgid"
        if restored != setuid_count:
            yield 0.94, "warn", (f"  setuid/setgid count changed ({setuid_count} -> {restored}); "
                                 "inspect before use")
        else:
            yield 0.94, "ok", "  ownership and setuid bits preserved"

        mismatches = [rel for rel, val in table.items()
                      if rel in out_table and out_table[rel] != val]
        if mismatches:
            yield 0.96, "warn", f"  {len(mismatches)} entries differ from source, e.g. {mismatches[:3]}"
        else:
            yield 0.96, "ok", "  permission table matches source exactly"

        for mi in incoming:
            expect = f"lib/modules/{kver}/updates/{mi.path.name}"
            if expect in out_table:
                yield 0.97, "ok", f"  present in output: /{expect}"
            else:
                raise PatchError(f"injected module missing from output: /{expect}")

        yield 0.99, "info", f"  sha256(out) {sha256(req.output)}"
        size_in = req.image.stat().st_size
        size_out = req.output.stat().st_size
        yield 1.00, "ok", (f"Done. {req.output}  "
                           f"({size_in:,} -> {size_out:,} bytes, {size_out - size_in:+,})")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(
        description="Inject out-of-tree kernel modules into a SquashFS initrd. "
                    "You supply the image and the modules; this tool ships neither.")
    # Optional so that --collect-diagnostics works on its own: someone asking
    # for diagnostics has usually just had a run fail and has no image path
    # to hand. Required-ness is enforced below instead.
    ap.add_argument("image_dir", type=Path, nargs="?",
                    help="directory containing the OS image")
    ap.add_argument("-m", "--module", type=Path, action="append", default=[],
                    help="kernel module (.ko) to inject; repeatable")
    ap.add_argument("-o", "--output", type=Path, default=None, help="output initrd path")
    ap.add_argument("-p", "--profile", choices=sorted(PROFILES), default="generic")
    ap.add_argument("--allow-vermagic-mismatch", action="store_true")
    ap.add_argument("--allow-unsigned", action="store_true",
                    help="inject unsigned modules into an image whose modules are all signed")
    ap.add_argument("--allow-missing-deps", action="store_true",
                    help="inject even when a declared dependency is absent from the image")
    ap.add_argument("--keep-xattrs", action="store_true")

    inspect = ap.add_argument_group(
        "inspection (no image is written)",
        "Answering which driver is needed, and which build of it will load.")
    inspect.add_argument("--report", action="store_true",
                         help="describe the image: kernel, vermagic, modules, firmware, signing")
    inspect.add_argument("--hardware", type=Path, metavar="FILE",
                         help="device listing from the target machine ('-' for stdin); accepts "
                              "lspci -nn, lspci -nnmm, lsusb, modalias strings or vendor:device pairs")
    inspect.add_argument("--alias-db", type=Path, metavar="FILE",
                         help="a modules.alias from any Linux system, used to name drivers for "
                              "hardware this image does not cover")
    inspect.add_argument("--scan", type=Path, action="append", default=[], metavar="PATH",
                         help="directory or archive of candidate .ko files to rank against this "
                              "image; repeatable")
    inspect.add_argument("--build-spec", type=Path, nargs="?", const=Path("."), metavar="DIR",
                         help="read the kernel's build settings; with a directory, write its "
                              ".config, a build-spec.json and build instructions there")
    inspect.add_argument("--find-drivers", action="store_true",
                         help="work out whether this is a stock distro kernel and, if so, "
                              "which packages hold matching modules. Network lookup, "
                              "metadata only — downloads nothing")
    inspect.add_argument("--fetch-drivers", type=Path, metavar="DIR",
                         help="download the packages --find-drivers located into DIR and rank "
                              "what is inside them. Downloads tens of MB over the network")
    inspect.add_argument("--want", action="append", default=[], metavar="NAME",
                         help="restrict --scan to these module names; repeatable")
    inspect.add_argument("--limit", type=int, default=5000,
                         help="maximum modules to read during --scan (default 5000)")

    ap.add_argument("--version", action="version", version=f"system-graft {__version__}")
    ap.add_argument("--collect-diagnostics", action="store_true",
                    help="write a diagnostics bundle and exit")
    args = ap.parse_args(argv)

    # Before anything that can fail, so a failure is logged and captured.
    diag.init(app="system-graft", env_prefix="SYSTEM_GRAFT",
              version=__version__, config=vars(args))

    if args.collect_diagnostics:
        # stdout, so it can be used in a script; logging went to stderr.
        print(diag.collect_diagnostics())
        return

    if args.image_dir is None:
        ap.error("image_dir is required")

    profile = PROFILES[args.profile]
    images = find_images(args.image_dir, profile)
    if not images:
        raise SystemExit(f"no SquashFS image found under {args.image_dir}")
    image = images[0]

    if (args.report or args.hardware or args.scan or args.build_spec is not None
            or args.find_drivers or args.fetch_drivers is not None):
        try:
            return inspect_image(image, args)
        except PatchError as exc:
            raise SystemExit(f"\nERROR: {exc}")

    output = args.output or image.with_name(image.name + ".patched")

    req = PatchRequest(
        image=image,
        modules=args.module,
        output=output,
        profile=profile,
        allow_vermagic_mismatch=args.allow_vermagic_mismatch,
        allow_unsigned=args.allow_unsigned,
        allow_missing_deps=args.allow_missing_deps,
        keep_xattrs=args.keep_xattrs,
    )
    try:
        for frac, level, message in patch(req):
            prefix = {"step": "==>", "ok": "  ok", "warn": "  !!", "error": " ERR",
                      "cmd": "   $", "info": "    "}.get(level, "    ")
            print(f"[{frac * 100:5.1f}%] {prefix} {message}")
    except PatchError as exc:
        raise SystemExit(f"\nERROR: {exc}")


@dataclass
class InspectRequest:
    """What to ask of an image, without writing anything to it."""

    image: Path
    profile: Profile
    report: bool = False
    build_spec: Path | None = None
    hardware_text: str = ""
    alias_db: Path | None = None
    scan_paths: list[Path] = field(default_factory=list)
    want: list[str] = field(default_factory=list)
    limit: int = 5000
    # Network. find_drivers is a metadata lookup; fetch_drivers downloads tens of
    # megabytes, so it is a separate opt-in rather than a consequence of the first.
    find_drivers: bool = False
    fetch_drivers: Path | None = None


def inspect(req: InspectRequest):
    """
    Generator yielding (fraction, level, message), same contract as patch().

    Same shape for the same reason: it is what lets the GUI show a report as it
    arrives without patcher.py knowing a GUI exists, and it keeps one
    implementation behind both front ends rather than a printing one and a
    displaying one that drift apart.

    Everything asked for is answered from a single extract. unsquashfs on a large
    initrd is the slow part, and there is no reason to pay for it three times.
    """
    with extracted(req.image) as root:
        versions = kernel_versions(root)
        if not versions:
            raise PatchError("no /lib/modules/<version> directory in this image")
        kver = versions[0]
        modules = find_image_modules(root)
        vermagic = image_vermagic(root)
        yield 0.15, "info", ""

        if req.report:
            signed = sum(1 for m in modules if m.signed)
            firmware_root = root / "lib" / "firmware"
            blobs = sum(1 for _ in firmware_root.rglob("*")) if firmware_root.is_dir() else 0
            yield 0.18, "step", f"Image:    {req.image}"
            yield 0.18, "info", (f"Kernel:   {kver}" +
                                 (f"  (also present: {', '.join(versions[1:])})"
                                  if len(versions) > 1 else ""))
            yield 0.19, "info", f"vermagic: {vermagic or '(no module to read it from)'}"
            decomposed = kmod.parse_vermagic(vermagic)
            if decomposed.flags:
                yield 0.19, "info", (f"          release {decomposed.release}, flags: "
                                     f"{' '.join(sorted(decomposed.flags))}")
            yield 0.20, "info", f"Modules:  {len(modules)} in the image, {signed} signed"
            if modules and signed == len(modules):
                yield 0.20, "warn", ("          every module is signed — this kernel may enforce "
                                     "CONFIG_MODULE_SIG_FORCE, in which case an unsigned module "
                                     "cannot be made to load at all.")
            builtin = _builtin_modules(root, kver)
            yield 0.21, "info", (f"Built-in: {len(builtin)} drivers compiled into the kernel"
                                 if builtin is not None
                                 else "Built-in: unknown (no modules.builtin in this image)")
            yield 0.21, "info", (f"Firmware: {blobs} files under /lib/firmware" if blobs
                                 else "Firmware: no /lib/firmware in this image")
            yield 0.22, "info", f"Init:     {req.profile.key} profile -> /{req.profile.init_script}"
            yield 0.22, "info", ""

        if req.report or req.build_spec is not None:
            kernel = kernelspec.find_kernel(req.image.parent.parent, req.image.parent, root)
            if kernel is None:
                yield 0.30, "warn", (
                    "No kernel image found next to the initrd, so its build settings and "
                    "embedded .config could not be read. Looked for vmlinuz/vmlinux/bzImage/"
                    "Image beside and above the initrd.")
                yield 0.30, "info", ""
            else:
                spec = kernelspec.analyse(kernel)
                for level, line in kernelspec.spec_lines(spec):
                    yield 0.40, level, line
                if req.build_spec is not None:
                    written = kernelspec.write_build_spec(spec, vermagic, req.build_spec)
                    yield 0.45, "ok", "Wrote:"
                    for path in written:
                        yield 0.45, "ok", f"  {path}"
                    if not spec.config_text:
                        yield 0.45, "warn", ("  (no config — this kernel was built without "
                                             "CONFIG_IKCONFIG)")
                    yield 0.45, "info", ""

        if req.hardware_text.strip():
            devices, notes = hardware.parse_devices(req.hardware_text)
            for note in notes:
                yield 0.55, "warn", f"note: {note}"
            if devices:
                index = hardware.build_index(root, kver)
                if req.alias_db:
                    count = hardware.load_alias_db(req.alias_db, index)
                    yield 0.65, "ok", f"Loaded {count} aliases from {req.alias_db.name}"
                for level, line in hardware.report_lines(hardware.assess(devices, index), index):
                    yield 0.75, level, line

        scan_paths = list(req.scan_paths)

        if req.find_drivers or req.fetch_drivers is not None:
            kernel = kernelspec.find_kernel(req.image.parent.parent, req.image.parent, root)
            spec = kernelspec.analyse(kernel) if kernel else None
            arch = next((mi.arch for mi in modules if mi.arch), "")
            release = kmod.parse_vermagic(vermagic).release
            if spec and spec.release:
                release = spec.release
            target = distro.identify(release, spec.compiler if spec else "", arch)
            result = distro.resolve(target)
            for level, line in distro.resolution_lines(result):
                yield 0.80, level, line

            if req.fetch_drivers is not None and result.downloads:
                total = sum(d.size for d in result.downloads)
                yield 0.82, "step", (f"Downloading {len(result.downloads)} package(s), "
                                     f"{total / 1_048_576:.1f} MB, into {req.fetch_drivers}")
                for item in result.downloads:
                    yield 0.85, "info", f"  {item.url}"
                    try:
                        path = distro.download(item, req.fetch_drivers)
                    except distro.DistroError as exc:
                        yield 0.85, "warn", f"  {exc}"
                        continue
                    yield 0.88, "ok", f"  {path.name} ({path.stat().st_size:,} bytes)" + (
                        " — checksum verified" if item.sha1 else "")
                    scan_paths.append(path)
            elif req.fetch_drivers is not None:
                yield 0.85, "warn", "  Nothing to download — see above."

        if scan_paths:
            candidates, notes = sources.scan(scan_paths, vermagic,
                                             want=req.want, limit=req.limit)
            for level, line in sources.scan_lines(candidates, vermagic, notes):
                yield 0.95, level, line

    yield 1.00, "ok", "Inspection complete. No image was written."


def inspect_image(image: Path, args) -> None:
    """CLI adapter: drive inspect() and print what it yields."""
    import sys

    hardware_text = ""
    if args.hardware:
        hardware_text = (sys.stdin.read() if str(args.hardware) == "-"
                         else args.hardware.read_text(errors="replace"))

    req = InspectRequest(
        image=image,
        profile=PROFILES[args.profile],
        report=args.report,
        build_spec=args.build_spec,
        hardware_text=hardware_text,
        alias_db=args.alias_db,
        scan_paths=list(args.scan),
        want=args.want,
        limit=args.limit,
        find_drivers=args.find_drivers,
        fetch_drivers=args.fetch_drivers,
    )
    for _frac, level, message in inspect(req):
        prefix = {"warn": "  !!", "cmd": "   $"}.get(level, "")
        print(f"{prefix} {message}" if prefix and message.strip() else message)


if __name__ == "__main__":
    main()
