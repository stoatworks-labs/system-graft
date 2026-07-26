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
"""

from __future__ import annotations

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

_MODINFO_KEYS = ("name", "vermagic", "srcversion", "license", "description", "depends", "author")


def read_modinfo(ko: Path) -> dict:
    """
    Extract .modinfo key=value pairs by scanning the file bytes.

    Deliberately avoids readelf/objdump so the tool works anywhere Python does.
    The .modinfo section is a run of NUL-separated key=value strings, so a byte
    scan is both sufficient and portable.
    """
    try:
        blob = ko.read_bytes()
    except OSError as exc:
        raise PatchError(f"cannot read {ko}: {exc}") from exc

    info: dict[str, str] = {}
    for key in _MODINFO_KEYS:
        pattern = re.compile(rb"(?:^|\x00)(" + key.encode() + rb"=)([^\x00]{0,512})")
        for match in pattern.finditer(blob):
            value = match.group(2).decode("utf-8", "replace").strip()
            if value:
                info.setdefault(key, value)
                break
    if not info.get("name"):
        info["name"] = ko.stem
    return info


def find_image_modules(root: Path) -> list[tuple[Path, dict]]:
    """Every .ko already inside the extracted image, with its modinfo."""
    results = []
    modules_dir = root / "lib" / "modules"
    if not modules_dir.is_dir():
        return results
    for ko in sorted(modules_dir.rglob("*.ko")):
        results.append((ko, read_modinfo(ko)))
    return results


def kernel_versions(root: Path) -> list[str]:
    modules_dir = root / "lib" / "modules"
    if not modules_dir.is_dir():
        return []
    return sorted(d.name for d in modules_dir.iterdir() if d.is_dir())


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


def patch(req: PatchRequest):
    """
    Generator yielding (fraction, level, message) as the patch proceeds.

    level is one of: "info", "step", "ok", "warn", "error", "cmd".
    Raises PatchError on any condition that should stop the run.
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
        image_vermagics = {mi.get("vermagic", "") for _, mi in existing if mi.get("vermagic")}
        for ko_path, mi in existing:
            yield 0.40, "info", (f"  image has {mi.get('name', ko_path.name)}: "
                                 f"vermagic={mi.get('vermagic', '?')!r}")
        if not image_vermagics:
            yield 0.41, "warn", "  no existing module found to compare against — cannot verify vermagic"

        for ko in req.modules:
            mi = read_modinfo(ko)
            vm = mi.get("vermagic", "")
            yield 0.42, "info", f"  {ko.name}: vermagic={vm!r} depends={mi.get('depends', '') or '(none)'}"
            if image_vermagics and vm not in image_vermagics:
                msg = (f"vermagic mismatch for {ko.name}:\n"
                       f"      module: {vm!r}\n"
                       f"      image:  {', '.join(repr(v) for v in sorted(image_vermagics))}\n"
                       "    The kernel will refuse to load this module.")
                if req.allow_vermagic_mismatch:
                    yield 0.43, "warn", "  " + msg + "\n    Continuing anyway (override enabled)."
                else:
                    raise PatchError(
                        msg + "\n    Rebuild the module against a matching kernel tree, or enable "
                        "the override if you know what you are doing."
                    )
            elif image_vermagics:
                yield 0.43, "ok", f"  {ko.name}: vermagic matches the image"

        yield 0.45, "step", "Injecting modules"
        updates_dir = root / "lib" / "modules" / kver / "updates"
        updates_dir.mkdir(parents=True, exist_ok=True)
        injected_rel: list[str] = []
        extra_perms: dict[str, tuple[int, int, int]] = {}
        for ko in req.modules:
            dest = updates_dir / ko.name
            shutil.copy2(ko, dest)
            os.chmod(dest, 0o644)
            rel = str(dest.relative_to(root))
            injected_rel.append("/" + rel)
            extra_perms[rel] = (0o644, 0, 0)
            yield 0.48, "ok", f"  + /{rel}  ({ko.stat().st_size} bytes)"

        # Ensure the parent directory exists in the pseudo table with sane ownership.
        extra_perms[str(updates_dir.relative_to(root))] = (0o755, 0, 0)

        if req.add_to_modules_dep:
            dep = root / "lib" / "modules" / kver / "modules.dep"
            if dep.is_file():
                text = dep.read_text()
                added = []
                for ko in req.modules:
                    line = f"updates/{ko.name}:"
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

        for ko in req.modules:
            expect = f"lib/modules/{kver}/updates/{ko.name}"
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
    ap.add_argument("image_dir", type=Path, help="directory containing the OS image")
    ap.add_argument("-m", "--module", type=Path, action="append", default=[],
                    help="kernel module (.ko) to inject; repeatable")
    ap.add_argument("-o", "--output", type=Path, default=None, help="output initrd path")
    ap.add_argument("-p", "--profile", choices=sorted(PROFILES), default="generic")
    ap.add_argument("--allow-vermagic-mismatch", action="store_true")
    ap.add_argument("--keep-xattrs", action="store_true")
    args = ap.parse_args(argv)

    profile = PROFILES[args.profile]
    images = find_images(args.image_dir, profile)
    if not images:
        raise SystemExit(f"no SquashFS image found under {args.image_dir}")
    image = images[0]
    output = args.output or image.with_name(image.name + ".patched")

    req = PatchRequest(
        image=image,
        modules=args.module,
        output=output,
        profile=profile,
        allow_vermagic_mismatch=args.allow_vermagic_mismatch,
        keep_xattrs=args.keep_xattrs,
    )
    try:
        for frac, level, message in patch(req):
            prefix = {"step": "==>", "ok": "  ok", "warn": "  !!", "error": " ERR",
                      "cmd": "   $", "info": "    "}.get(level, "    ")
            print(f"[{frac * 100:5.1f}%] {prefix} {message}")
    except PatchError as exc:
        raise SystemExit(f"\nERROR: {exc}")


if __name__ == "__main__":
    main()
