"""
Finding a prebuilt module for a stock distro kernel.

This exists because the answer to "where do I get a module for this image?" splits
cleanly in two, and only one half is solvable:

  * **A stock distro kernel.** The distro built the kernel and the modules in the
    same build, so its own archive holds modules whose vermagic matches by
    construction. That is a lookup, and this module does it.
  * **A vendor or appliance kernel.** No prebuilt module for it exists anywhere
    except that vendor's build machine. No archive, no database, no amount of
    searching will produce one. It has to be compiled — see kernelspec.py, which
    recovers the .config needed to do that.

So the first useful thing here is telling those two apart, and saying plainly
which one you are in. Reporting "not found" for a vendor kernel would be
misleading: nothing was missed, the thing does not exist.

**On archives and rot.** Only two sources are resolved to an actual URL, and both
because they have a documented machine-readable API that can be queried rather
than scraped:

  * Debian, via snapshot.debian.org's /mr/ endpoints, which also return a SHA-1
    so a download can be verified.
  * Ubuntu, via the Launchpad API for the exact package version, which is the one
    part not derivable from the kernel release string.

Everything else — the RHEL rebuilds, Alpine, Arch — is identified and named down
to the exact package, but not resolved to a URL. Their layouts vary by rebuild
vendor and mirror, and a constructed URL that has never been tested is worse than
an instruction: it fails later, further from the cause, and looks like a bug in
this tool.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from _version import __version__

USER_AGENT = f"system-graft/{__version__} (+https://github.com/stoatworks-labs/system-graft)"
TIMEOUT = 30

DEBIAN = "debian"
UBUNTU = "ubuntu"
RHEL = "rhel"
ALPINE = "alpine"
ARCH = "arch"
VENDOR = "vendor"


class DistroError(Exception):
    """A lookup failed for a reason the user can act on."""


# --------------------------------------------------------------------------
# Identifying the kernel
# --------------------------------------------------------------------------

@dataclass
class Target:
    distro: str
    release: str                 # the full vermagic release string
    arch: str = ""               # archive-style: amd64, arm64, x86_64...
    flavour: str = ""            # generic / lowlatency / amd64 / lts ...
    base: str = ""               # 5.15.0-91
    evidence: str = ""           # why we think so

    @property
    def is_stock(self) -> bool:
        return self.distro not in (VENDOR, "")


# Ubuntu names its flavours by role. Debian names them by architecture. That is
# the whole discriminator when the compiler string is unavailable, and it holds
# because neither distro has ever crossed into the other's naming scheme.
UBUNTU_FLAVOURS = {
    "generic", "lowlatency", "aws", "azure", "gcp", "gke", "oracle", "kvm",
    "raspi", "ibm", "intel-iotg", "nvidia", "realtime", "oem", "azure-fde",
    "generic-64k", "generic-hwe", "laptop", "starfive", "xilinx",
}
DEBIAN_FLAVOUR_SUFFIXES = (
    "amd64", "arm64", "armmp", "686", "686-pae", "i386", "ppc64el", "s390x",
    "riscv64", "mipsel", "loong64",
)


def _build_host(compiler: str) -> str:
    lowered = (compiler or "").lower()
    if "ubuntu" in lowered:
        return "Ubuntu"
    if "debian" in lowered:
        return "Debian"
    if "red hat" in lowered or "redhat" in lowered:
        return "Red Hat"
    if "alpine" in lowered:
        return "Alpine"
    return ""


def identify(release: str, compiler: str = "", arch: str = "") -> Target:
    """
    Work out whether this kernel came from a distribution's archive.

    **The release string decides this, not the compiler.** The compiler string in
    the banner says what the *build host* was, which is a different question — and
    the difference is precisely the case this tool exists for. Appliance vendors
    routinely build bespoke kernels on a Debian or Ubuntu box, so a banner reading
    "gcc (Debian 12.2.0-14)" on a kernel called "6.12.11" means someone compiled
    it on Debian, not that Debian ships it. Trusting the compiler there would send
    the user hunting an archive for a package that was never published.

    So a kernel is only called stock when its release string parses as that
    distro's package naming scheme. The compiler is used to break the one genuine
    ambiguity (Debian and Ubuntu share a release shape) and to corroborate.
    """
    release = (release or "").strip()
    if not release:
        return Target(distro="", release="", evidence="no kernel release string")

    host = _build_host(compiler)
    corroborates = f", and the compiler string agrees ({host})" if host else ""

    if re.search(r"\.el\d+(_\d+)?\.", release) or release.endswith((".el7", ".el8", ".el9")):
        return Target(distro=RHEL, release=release, arch=arch or _rhel_arch(release),
                      evidence="the release string carries an .elN tag")

    match = re.fullmatch(r"(?P<base>\d+\.\d+\.\d+-\d+)-(?P<flavour>[a-z0-9-]+)", release)
    if match:
        flavour = match.group("flavour")
        if flavour in UBUNTU_FLAVOURS:
            return _ubuntu_target(release, arch,
                                  f"{flavour!r} is an Ubuntu kernel flavour{corroborates}")
        if flavour.endswith(DEBIAN_FLAVOUR_SUFFIXES):
            return _debian_target(release, arch,
                                  f"{flavour!r} is a Debian kernel flavour{corroborates}")
        if flavour in ("lts", "virt", "rpi", "edge") and host == "Alpine":
            return Target(distro=ALPINE, release=release, arch=arch,
                          evidence="Alpine release scheme, and the compiler agrees")
        if flavour in ("lts", "zen", "hardened") or "arch" in flavour:
            return Target(distro=ARCH, release=release, arch=arch,
                          evidence="the release string matches Arch's scheme")
        # Right shape, unrecognised flavour. The build host is the only evidence
        # left, and it is weak — say so rather than pretending otherwise.
        if host in ("Debian", "Ubuntu"):
            target = (_ubuntu_target if host == "Ubuntu" else _debian_target)(
                release, arch,
                f"{flavour!r} is not a flavour this tool knows, but the release has the right "
                f"shape and the kernel was built on {host} — treat this as a guess")
            return target

    if host:
        return Target(distro=VENDOR, release=release, arch=arch,
                      evidence=(f"built on {host}, but {release!r} is not a {host} package "
                                "name — this is a custom kernel compiled on a "
                                f"{host} machine, not one {host} ships"))
    return Target(distro=VENDOR, release=release, arch=arch,
                  evidence="the release string matches no distribution's naming scheme")


def _debian_target(release: str, arch: str, evidence: str) -> Target:
    match = re.fullmatch(r"(?P<base>\d+\.\d+\.\d+-\d+)-(?P<flavour>.+)", release)
    flavour = match.group("flavour") if match else ""
    # Debian's flavour ends with the architecture: "amd64", "rt-amd64", "cloud-arm64".
    guessed = ""
    for suffix in DEBIAN_FLAVOUR_SUFFIXES:
        if flavour.endswith(suffix):
            guessed = suffix
            break
    return Target(distro=DEBIAN, release=release, arch=arch or guessed, flavour=flavour,
                  base=match.group("base") if match else "", evidence=evidence)


def _ubuntu_target(release: str, arch: str, evidence: str) -> Target:
    match = re.fullmatch(r"(?P<base>\d+\.\d+\.\d+-\d+)-(?P<flavour>.+)", release)
    return Target(distro=UBUNTU, release=release, arch=arch,
                  flavour=match.group("flavour") if match else "",
                  base=match.group("base") if match else "", evidence=evidence)


def _rhel_arch(release: str) -> str:
    for arch in ("x86_64", "aarch64", "ppc64le", "s390x"):
        if release.endswith("." + arch):
            return arch
    return ""


# --------------------------------------------------------------------------
# Which packages hold the modules
# --------------------------------------------------------------------------

@dataclass
class PackageRef:
    name: str
    why: str


def packages_for(target: Target) -> list[PackageRef]:
    """The packages that would contain a module for this kernel."""
    if target.distro == DEBIAN:
        # Debian ships modules inside linux-image; there is no separate
        # modules package the way Ubuntu has one.
        return [PackageRef(f"linux-image-{target.release}",
                           "Debian ships the modules inside linux-image")]
    if target.distro == UBUNTU:
        return [
            PackageRef(f"linux-modules-extra-{target.release}",
                       "most out-of-tree-ish and less common drivers, including most NICs"),
            PackageRef(f"linux-modules-{target.release}",
                       "the core module set"),
        ]
    if target.distro == RHEL:
        version = re.sub(r"\.(x86_64|aarch64|ppc64le|s390x)$", "", target.release)
        return [
            PackageRef(f"kernel-modules-extra-{version}", "less common drivers"),
            PackageRef(f"kernel-modules-{version}", "the main module set"),
            PackageRef(f"kernel-modules-core-{version}", "core modules (RHEL 9 and later)"),
        ]
    if target.distro == ALPINE:
        return [PackageRef(f"linux-{target.flavour or 'lts'}", "Alpine ships modules in the "
                                                              "kernel package")]
    if target.distro == ARCH:
        return [PackageRef("linux (or linux-lts)", "Arch ships modules in the kernel package")]
    return []


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _open(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def _get_json(url: str) -> dict:
    try:
        with _open(url) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        exc.close()  # see the note in download(): this holds a live socket
        raise DistroError(f"{url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise DistroError(f"cannot reach {url}: {exc}") from exc


def _head(url: str) -> int | None:
    """
    Content-Length if the URL is served, None if it is not.

    The size comes free with the reachability check, and knowing a download is
    60 MB before starting it is the difference between an informed yes and a
    surprise.
    """
    request = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if not 200 <= response.status < 300:
                return None
            return int(response.headers.get("Content-Length") or 0)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Resolving to downloads
# --------------------------------------------------------------------------

@dataclass
class Download:
    url: str
    filename: str
    package: str
    size: int = 0
    sha1: str = ""       # when the archive publishes one, so the file can be checked

    @property
    def size_h(self) -> str:
        if not self.size:
            return "unknown size"
        return f"{self.size / 1_048_576:.1f} MB"


@dataclass
class Resolution:
    target: Target
    downloads: list[Download] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)


SNAPSHOT = "https://snapshot.debian.org"
LAUNCHPAD = "https://api.launchpad.net/1.0"
UBUNTU_MIRRORS = (
    "https://archive.ubuntu.com/ubuntu",
    "https://old-releases.ubuntu.com/ubuntu",
    "https://ports.ubuntu.com/ubuntu-ports",
)


def resolve(target: Target) -> Resolution:
    """
    Turn a target into concrete downloads, or into instructions when it cannot be.

    Never raises for "this kernel is not from a distro" — that is an answer, and
    the most important one this module produces.
    """
    result = Resolution(target=target)

    if target.distro == VENDOR:
        result.notes.append(
            "This is not a stock distro kernel, so no archive anywhere holds a matching "
            "module. It has to be compiled against this kernel's own source and config — "
            "run --build-spec to extract what you need.")
        return result
    if not target.is_stock:
        result.notes.append("No kernel release string to work from.")
        return result

    if target.distro == DEBIAN:
        _resolve_debian(target, result)
    elif target.distro == UBUNTU:
        _resolve_ubuntu(target, result)
    else:
        _instructions_only(target, result)
    return result


def _resolve_debian(target: Target, result: Resolution) -> None:
    for package in packages_for(target):
        try:
            versions = _get_json(f"{SNAPSHOT}/mr/binary/{package.name}/")
        except DistroError as exc:
            result.notes.append(f"{package.name}: {exc}")
            continue
        entries = versions.get("result") or []
        if not entries:
            result.notes.append(f"{package.name}: not found in the Debian snapshot archive")
            continue
        version = entries[0]["binary_version"]

        try:
            binfiles = _get_json(f"{SNAPSHOT}/mr/binary/{package.name}/{version}/binfiles")
        except DistroError as exc:
            result.notes.append(f"{package.name} {version}: {exc}")
            continue

        wanted = target.arch or "amd64"
        matches = [f for f in binfiles.get("result", []) if f.get("architecture") == wanted]
        if not matches and binfiles.get("result"):
            available = sorted({f.get("architecture", "?") for f in binfiles["result"]})
            result.notes.append(
                f"{package.name} {version}: no build for {wanted}; the archive has "
                f"{', '.join(available)}")
            continue

        for entry in matches:
            digest = entry["hash"]
            try:
                info = _get_json(f"{SNAPSHOT}/mr/file/{digest}/info")
            except DistroError as exc:
                result.notes.append(f"{package.name}: {exc}")
                continue
            first = (info.get("result") or [{}])[0]
            result.downloads.append(Download(
                url=f"{SNAPSHOT}/file/{digest}",
                filename=first.get("name") or f"{package.name}_{version}_{wanted}.deb",
                package=package.name,
                size=int(first.get("size") or 0),
                sha1=digest,
            ))


def _resolve_ubuntu(target: Target, result: Resolution) -> None:
    wanted = target.arch or "amd64"
    for package in packages_for(target):
        url = (f"{LAUNCHPAD}/ubuntu/+archive/primary?ws.op=getPublishedBinaries"
               f"&binary_name={package.name}&exact_match=true")
        try:
            payload = _get_json(url)
        except DistroError as exc:
            result.notes.append(f"{package.name}: {exc}")
            continue

        version = ""
        for entry in payload.get("entries", []):
            if entry.get("distro_arch_series_link", "").endswith("/" + wanted):
                version = entry.get("binary_package_version", "")
                break
        if not version:
            result.notes.append(
                f"{package.name}: Launchpad lists no {wanted} build "
                "(it may have been superseded and removed)")
            continue

        # The pool path is derivable; which mirror still carries it is not, so try
        # each. Older releases move to old-releases, and non-x86 lives on ports.
        filename = f"{package.name}_{version}_{wanted}.deb"
        found = False
        for mirror in UBUNTU_MIRRORS:
            candidate = f"{mirror}/pool/main/l/linux/{filename}"
            size = _head(candidate)
            if size is not None:
                result.downloads.append(Download(url=candidate, filename=filename,
                                                 package=package.name, size=size))
                found = True
                break
        if not found:
            result.notes.append(
                f"{package.name} {version}: Launchpad knows this version but no mirror "
                f"served {filename}. It may be in a -updates pocket under a different "
                "source package.")

    if result.downloads:
        result.notes.append(
            "Ubuntu's archive publishes no checksum through this API, so these downloads "
            "are not verified beyond HTTPS. Debian's are checked against a SHA-1.")


def _instructions_only(target: Target, result: Resolution) -> None:
    """Name the packages precisely, and say where to look, without guessing a URL."""
    names = ", ".join(p.name for p in packages_for(target))
    where = {
        RHEL: ("the vault for your rebuild — vault.centos.org, "
               "repo.almalinux.org/vault, or dl.rockylinux.org/vault"),
        ALPINE: "dl-cdn.alpinelinux.org/alpine/<release>/main",
        ARCH: "archive.archlinux.org/packages/l/linux",
    }.get(target.distro, "your distribution's archive")

    result.instructions.append(f"Download: {names}")
    result.instructions.append(f"From:     {where}")
    result.instructions.append(
        "Then unpack it anywhere and point --scan at that folder; every module inside "
        "will be checked against this image.")
    result.notes.append(
        f"{target.distro} archives vary by rebuild vendor and mirror, so this tool names the "
        "package but does not construct a download URL for it — an untested URL fails later "
        "and looks like a bug here.")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def download(item: Download, dest_dir: Path, progress=None) -> Path:
    """
    Fetch one file, verifying its checksum when the archive published one.

    progress is called as progress(bytes_so_far, total) if given. The file is
    written to a .part and renamed only once complete and verified, so an
    interrupted download can never be mistaken for a usable package.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / item.filename
    if final.exists():
        return final
    partial = final.with_suffix(final.suffix + ".part")

    digest = hashlib.sha1()
    seen = 0
    try:
        with _open(item.url) as response, open(partial, "wb") as handle:
            total = int(response.headers.get("Content-Length") or item.size or 0)
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                seen += len(chunk)
                if progress:
                    progress(seen, total)
    except urllib.error.HTTPError as exc:
        # An HTTPError *is* a response object holding an open socket. Letting it
        # be collected later leaks the connection, which matters in the GUI where
        # the process outlives the download by hours.
        exc.close()
        partial.unlink(missing_ok=True)
        raise DistroError(f"download failed: HTTP {exc.code} for {item.url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise DistroError(f"download failed: {exc}") from exc

    if item.sha1 and digest.hexdigest() != item.sha1:
        partial.unlink(missing_ok=True)
        raise DistroError(
            f"{item.filename} failed its checksum — expected {item.sha1}, "
            f"got {digest.hexdigest()}. The file was discarded.")

    partial.replace(final)
    return final


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def resolution_lines(result: Resolution) -> list[tuple[str, str]]:
    target = result.target
    lines: list[tuple[str, str]] = [("step", f"Kernel release: {target.release or '(unknown)'}")]

    if target.distro == VENDOR:
        lines.append(("warn", "  Not a stock distro kernel."))
    elif target.is_stock:
        label = {DEBIAN: "Debian", UBUNTU: "Ubuntu", RHEL: "Red Hat family",
                 ALPINE: "Alpine", ARCH: "Arch"}.get(target.distro, target.distro)
        lines.append(("ok", f"  Looks like a {label} kernel"
                            f"{f' ({target.arch})' if target.arch else ''}."))
    lines.append(("info", f"  {target.evidence}."))
    lines.append(("info", ""))

    if result.downloads:
        total = sum(d.size for d in result.downloads)
        lines.append(("ok", f"Found {len(result.downloads)} package(s)"
                            f"{f', {total / 1_048_576:.1f} MB total' if total else ''}:"))
        for item in result.downloads:
            lines.append(("ok", f"  {item.filename}  ({item.size_h})"))
            lines.append(("info", f"      {item.url}"))
            if item.sha1:
                lines.append(("info", f"      sha1 {item.sha1} — will be verified"))
        lines.append(("info", ""))

    for line in result.instructions:
        lines.append(("info", line))
    if result.instructions:
        lines.append(("info", ""))
    for note in result.notes:
        lines.append(("warn" if "not a stock" in note.lower() else "info", note))
    return lines


def format_resolution(result: Resolution) -> str:
    return "\n".join(line for _, line in resolution_lines(result)).rstrip() + "\n"
