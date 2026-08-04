"""
Finding a .ko that will actually load into a given image.

The patch path can tell you a module does not match. This tells you which of the
ones you already have does — because the usual situation is not "I have one
module and it is wrong", it is "I have a vendor driver pack, or an unpacked
distro kernel package, containing several hundred modules built for a dozen
kernels, and one of them might be right".

So: point this at a directory or an archive, give it the image's vermagic, and it
ranks everything it finds. That turns a rebuild into a file pick whenever a
matching build already exists on disk.

Archives are read because that is the shape driver packs actually arrive in.
tar/zip/deb are handled with the stdlib plus a small `ar` reader; rpm needs
`rpm2cpio` on PATH, and says so plainly when it is missing rather than silently
finding nothing.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import kmod

MODULE_SUFFIXES = (".ko", ".ko.xz", ".ko.gz", ".ko.zst")
ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2",
                    ".tbz2", ".tar.zst", ".zip", ".deb", ".udeb", ".rpm")

# Ranked best first; the report groups by these.
EXACT = "exact"
SAME_RELEASE = "same-release"
OTHER_RELEASE = "other-release"
UNREADABLE = "unreadable"

_RANK = {EXACT: 0, SAME_RELEASE: 1, OTHER_RELEASE: 2, UNREADABLE: 3}


@dataclass
class Candidate:
    info: kmod.ModInfo
    origin: str          # where it came from, including the archive path
    verdict: str
    reasons: list[str]

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def vermagic(self) -> str:
        return self.info.vermagic


def _is_module(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in MODULE_SUFFIXES)


def _is_archive(path: Path) -> bool:
    lowered = path.name.lower()
    return any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


# --------------------------------------------------------------------------
# Archives
# --------------------------------------------------------------------------

def _read_ar_members(path: Path) -> list[tuple[str, bytes]]:
    """
    Minimal `ar` reader, enough for a .deb.

    A .deb is an ar archive of debian-binary, control.tar.*, data.tar.* — the
    modules live in the last one. Parsing 60-byte headers here avoids depending
    on binutils `ar`, which is not on a stock macOS.
    """
    blob = path.read_bytes()
    if blob[:8] != b"!<arch>\n":
        return []
    members: list[tuple[str, bytes]] = []
    offset = 8
    while offset + 60 <= len(blob):
        header = blob[offset:offset + 60]
        name = header[0:16].decode("ascii", "replace").strip()
        try:
            size = int(header[48:58].decode("ascii", "replace").strip() or 0)
        except ValueError:
            break
        start = offset + 60
        members.append((name.rstrip("/"), blob[start:start + size]))
        offset = start + size + (size % 2)
    return members


def _safe_extract_tar(archive: tarfile.TarFile, dest: Path) -> list[Path]:
    out: list[Path] = []
    for member in archive.getmembers():
        if not member.isfile() or not _is_module(member.name):
            continue
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest.resolve())):
            continue  # path traversal; skip rather than trust the archive
        target.parent.mkdir(parents=True, exist_ok=True)
        extracted = archive.extractfile(member)
        if extracted is None:
            continue
        target.write_bytes(extracted.read())
        out.append(target)
    return out


def _extract_modules(path: Path, dest: Path, notes: list[str]) -> list[tuple[Path, str]]:
    """Pull just the module files out of an archive. Returns (file, origin label)."""
    lowered = path.name.lower()
    results: list[tuple[Path, str]] = []
    try:
        if lowered.endswith((".deb", ".udeb")):
            for name, blob in _read_ar_members(path):
                if not name.startswith("data.tar"):
                    continue
                inner = dest / name
                inner.write_bytes(blob)
                try:
                    with tarfile.open(inner) as archive:
                        for extracted in _safe_extract_tar(archive, dest):
                            results.append((extracted, f"{path.name}!{extracted.name}"))
                except tarfile.TarError as exc:
                    notes.append(f"{path.name}: cannot read {name}: {exc}")
                inner.unlink(missing_ok=True)

        elif lowered.endswith(".rpm"):
            rpm2cpio = shutil.which("rpm2cpio")
            cpio = shutil.which("cpio") or shutil.which("bsdcpio")
            if not rpm2cpio or not cpio:
                notes.append(f"{path.name}: skipped — reading rpm needs rpm2cpio and cpio on PATH")
                return results
            stage = dest / "rpm"
            stage.mkdir(parents=True, exist_ok=True)
            piped = subprocess.run(f'"{rpm2cpio}" "{path}" | "{cpio}" -idm --quiet',
                                   shell=True, cwd=stage, capture_output=True, text=True)
            if piped.returncode != 0:
                notes.append(f"{path.name}: rpm extraction failed: {piped.stderr.strip()}")
                return results
            for found in stage.rglob("*"):
                if found.is_file() and _is_module(found.name):
                    results.append((found, f"{path.name}!{found.relative_to(stage)}"))

        elif lowered.endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    if not _is_module(member):
                        continue
                    target = (dest / member).resolve()
                    if not str(target).startswith(str(dest.resolve())):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(member))
                    results.append((target, f"{path.name}!{member}"))

        else:
            with tarfile.open(path) as archive:
                for extracted in _safe_extract_tar(archive, dest):
                    results.append((extracted, f"{path.name}!{extracted.name}"))

    except (tarfile.TarError, zipfile.BadZipFile, OSError, struct.error) as exc:
        notes.append(f"{path.name}: cannot read archive: {exc}")
    return results


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def _classify(info: kmod.ModInfo, target_vermagic: str) -> tuple[str, list[str]]:
    if info.error:
        return UNREADABLE, [info.error]
    if not info.vermagic:
        return UNREADABLE, ["no vermagic in this file — it may not be a kernel module"]
    if not target_vermagic:
        return OTHER_RELEASE, ["no image vermagic to compare against"]
    matches, reasons = kmod.compare_vermagic(info.vermagic, target_vermagic)
    if matches:
        return EXACT, []
    same_release = (kmod.parse_vermagic(info.vermagic).release
                    == kmod.parse_vermagic(target_vermagic).release)
    return (SAME_RELEASE if same_release else OTHER_RELEASE), reasons


def scan(paths: list[Path], target_vermagic: str, want: list[str] | None = None,
         limit: int = 5000) -> tuple[list[Candidate], list[str]]:
    """
    Find and rank every module under the given files/directories/archives.

    `want` optionally filters to specific module names. `limit` bounds the walk,
    because an unpacked kernel tree holds thousands of modules and reading every
    one of them to answer "is igb here" wastes the user's time.
    """
    candidates: list[Candidate] = []
    notes: list[str] = []
    wanted = {w.replace("-", "_") for w in (want or [])}
    workdir = Path(tempfile.mkdtemp(prefix="system-graft-scan-"))
    examined = 0

    try:
        queue: list[tuple[Path, str]] = []
        for path in paths:
            if not path.exists():
                notes.append(f"{path}: not found")
                continue
            if path.is_dir():
                for found in sorted(path.rglob("*")):
                    if not found.is_file():
                        continue
                    if _is_module(found.name):
                        queue.append((found, str(found)))
                    elif _is_archive(found):
                        queue.extend(_extract_modules(found, workdir, notes))
            elif _is_archive(path):
                queue.extend(_extract_modules(path, workdir, notes))
            elif _is_module(path.name):
                queue.append((path, str(path)))
            else:
                notes.append(f"{path.name}: not a module or a recognised archive")

        for file_path, origin in queue:
            if examined >= limit:
                notes.append(f"stopped after {limit} modules — narrow the scan or raise --limit")
                break
            if wanted and kmod._base_module_name(file_path).replace("-", "_") not in wanted:
                continue
            examined += 1
            info = kmod.read(file_path)
            verdict, reasons = _classify(info, target_vermagic)
            candidates.append(Candidate(info=info, origin=origin, verdict=verdict, reasons=reasons))
    finally:
        # Keep any exact match alive past the temp dir: copying it out is the
        # whole point of finding it.
        keep = [c for c in candidates if c.verdict == EXACT and str(workdir) in str(c.info.path)]
        if keep:
            notes.append(f"matches extracted from archives are in {workdir} "
                         "(copy them out before this directory is cleaned up)")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    candidates.sort(key=lambda c: (_RANK[c.verdict], c.name, c.origin))
    if examined == 0 and not notes:
        notes.append("no kernel modules found — looked for " + ", ".join(MODULE_SUFFIXES))
    return candidates, notes


_VERDICT_LEVEL = {EXACT: "ok", SAME_RELEASE: "warn", OTHER_RELEASE: "info", UNREADABLE: "warn"}


def scan_lines(candidates: list[Candidate], target_vermagic: str,
               notes: list[str]) -> list[tuple[str, str]]:
    """(level, line) for the whole scan result."""
    lines: list[tuple[str, str]] = [
        ("step", f"Looking for modules matching: {target_vermagic or '(unknown)'}"),
        ("info", ""),
    ]

    headings = {
        EXACT: "MATCH — vermagic is identical; these will be accepted by the kernel",
        SAME_RELEASE: "same kernel release, different build flags — will NOT load as-is",
        OTHER_RELEASE: "different kernel — will not load",
        UNREADABLE: "could not be read",
    }
    for verdict, heading in headings.items():
        group = [c for c in candidates if c.verdict == verdict]
        if not group:
            continue
        level = _VERDICT_LEVEL[verdict]
        lines.append((level, f"{heading}  ({len(group)})"))
        # A different kernel entirely is rarely worth listing in full.
        shown = group if verdict != OTHER_RELEASE else group[:20]
        for candidate in shown:
            lines.append((level, f"  {candidate.name:<24} {candidate.origin}"))
            if candidate.vermagic and verdict != EXACT:
                lines.append(("info", f"      vermagic: {candidate.vermagic}"))
            for reason in candidate.reasons[:2]:
                lines.append(("info", f"      {reason}"))
            if candidate.info.signed:
                lines.append(("info", "      signed"))
        if len(group) > len(shown):
            lines.append(("info", f"  ... and {len(group) - len(shown)} more"))
        lines.append(("info", ""))

    if not any(c.verdict == EXACT for c in candidates):
        lines.append(("warn", "No exact match. The module has to be built against this "
                              "image's kernel."))
        lines.append(("info", ""))
    for note in notes:
        lines.append(("info", f"note: {note}"))
    return lines


def format_scan(candidates: list[Candidate], target_vermagic: str,
                notes: list[str]) -> str:
    return "\n".join(line for _, line in scan_lines(candidates, target_vermagic, notes)).rstrip() + "\n"
