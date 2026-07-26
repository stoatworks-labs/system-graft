"""
Write a patched boot tree to removable media as a UEFI-bootable volume.

This module can destroy data. Everything in it is built around not doing that to
the wrong device:

  * Enumeration only ever returns devices the OS reports as external.
  * `assert_safe()` refuses internal disks, anything mounted at a system path,
    and anything not removable/ejectable, and is called again immediately before
    the destructive step rather than trusted from enumeration time.
  * Virtual devices (attached disk images) are excluded unless explicitly asked
    for, which is how this module is tested without sacrificing a USB stick.

Bootability model: UEFI only. The volume boots through the removable-media
fallback path \\EFI\\BOOT\\BOOTX64.EFI, which needs no NVRAM entry and no boot
sector. Legacy BIOS boot would require installing a boot sector, which is not
done here — see the README.
"""

from __future__ import annotations

import hashlib
import os
import platform
import plistlib
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

# Paths that, if a device is mounted there, mean "this is the running system".
_SYSTEM_MOUNTS = ("/", "/System", "/System/Volumes/Data", "/private/var", "/boot", "/boot/efi")

SKIP_NAMES = {".DS_Store", "._.DS_Store", ".Spotlight-V100", ".fseventsd", ".Trashes"}


class USBError(Exception):
    """Raised for any condition that should stop before touching a device."""


@dataclass
class Device:
    node: str
    path: str
    name: str
    size: int
    internal: bool = False
    removable: bool = False
    ejectable: bool = False
    virtual: bool = False
    bus: str = ""
    mountpoints: list[str] = field(default_factory=list)

    @property
    def size_h(self) -> str:
        size = float(self.size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return f"{size:.1f} TB"

    @property
    def label(self) -> str:
        bits = [self.node, self.name or "unknown", self.size_h]
        if self.bus:
            bits.append(self.bus)
        if self.virtual:
            bits.append("VIRTUAL")
        return "  —  ".join(bits)


# ---------------------------------------------------------------- enumeration

def _run(cmd: list[str], check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise USBError(f"{' '.join(cmd[:3])}… failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def _mac_devices(allow_virtual: bool) -> list[Device]:
    selector = ["external"] if allow_virtual else ["external", "physical"]
    proc = _run(["diskutil", "list", "-plist"] + selector, check=False)
    try:
        listing = plistlib.loads(proc.stdout.encode())
    except Exception as exc:
        raise USBError(f"could not parse diskutil output: {exc}") from exc

    devices: list[Device] = []
    for node in listing.get("WholeDisks", []):
        info_proc = _run(["diskutil", "info", "-plist", node], check=False)
        try:
            info = plistlib.loads(info_proc.stdout.encode())
        except Exception:
            continue
        mounts = []
        for entry in listing.get("AllDisksAndPartitions", []):
            if entry.get("DeviceIdentifier") != node:
                continue
            for part in entry.get("Partitions", []):
                if part.get("MountPoint"):
                    mounts.append(part["MountPoint"])
        if info.get("MountPoint"):
            mounts.append(info["MountPoint"])
        devices.append(Device(
            node=node,
            path=f"/dev/{node}",
            name=(info.get("MediaName") or "").strip(),
            size=int(info.get("TotalSize") or 0),
            internal=bool(info.get("Internal")),
            removable=bool(info.get("RemovableMedia")),
            ejectable=bool(info.get("Ejectable")),
            virtual=(info.get("VirtualOrPhysical") == "Virtual"),
            bus=(info.get("BusProtocol") or "").strip(),
            mountpoints=sorted(set(mounts)),
        ))
    return devices


def _linux_devices(allow_virtual: bool) -> list[Device]:
    proc = _run(["lsblk", "-J", "-b", "-o",
                 "NAME,SIZE,MODEL,RM,TRAN,TYPE,MOUNTPOINT,HOTPLUG"], check=False)
    import json
    try:
        tree = json.loads(proc.stdout or "{}")
    except Exception as exc:
        raise USBError(f"could not parse lsblk output: {exc}") from exc

    devices: list[Device] = []
    for entry in tree.get("blockdevices", []):
        if entry.get("type") != "disk":
            continue
        removable = bool(entry.get("rm")) or bool(entry.get("hotplug"))
        transport = (entry.get("tran") or "")
        if not removable and transport != "usb" and not allow_virtual:
            continue
        mounts = [c["mountpoint"] for c in entry.get("children", []) if c.get("mountpoint")]
        if entry.get("mountpoint"):
            mounts.append(entry["mountpoint"])
        devices.append(Device(
            node=entry["name"],
            path=f"/dev/{entry['name']}",
            name=(entry.get("model") or "").strip(),
            size=int(entry.get("size") or 0),
            internal=not removable and transport != "usb",
            removable=removable,
            ejectable=removable,
            virtual=transport in ("", "loop"),
            bus=transport.upper(),
            mountpoints=sorted(set(mounts)),
        ))
    return devices


def list_devices(allow_virtual: bool = False) -> list[Device]:
    if IS_MAC:
        devices = _mac_devices(allow_virtual)
    elif IS_LINUX:
        devices = _linux_devices(allow_virtual)
    else:
        raise USBError(f"USB writing is not implemented on {platform.system()}")
    if not allow_virtual:
        devices = [d for d in devices if not d.virtual]
    return [d for d in devices if not d.internal]


def assert_safe(device: Device, allow_virtual: bool = False) -> None:
    """Last line of defence. Called again immediately before writing."""
    if device.internal:
        raise USBError(f"{device.node} is an internal disk — refusing.")
    if device.virtual and not allow_virtual:
        raise USBError(f"{device.node} is a virtual device — refusing.")
    if not (device.removable or device.ejectable or device.bus.upper() == "USB" or device.virtual):
        raise USBError(
            f"{device.node} is not reported as removable or ejectable — refusing. "
            "This tool only writes to removable media.")
    for mount in device.mountpoints:
        if mount in _SYSTEM_MOUNTS or mount.startswith("/System/"):
            raise USBError(f"{device.node} is mounted at {mount} — that is a system volume. Refusing.")
    if device.size <= 0:
        raise USBError(f"{device.node} reports zero size — refusing.")


def refresh_device(node: str, allow_virtual: bool = False) -> Device:
    """Re-read a device's state so stale enumeration cannot be acted on."""
    for device in list_devices(allow_virtual=allow_virtual):
        if device.node == node:
            return device
    raise USBError(f"{node} is no longer present — refusing to write.")


# ------------------------------------------------------------------- helpers

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_copy(image_dir: Path, replacements: dict[str, Path]) -> list[tuple[Path, str]]:
    """
    Build the list of (source_file, relative_destination) to write.

    `replacements` maps a relative path in the tree to a different source file,
    which is how the patched initrd is written without mutating the input tree.
    """
    plan: list[tuple[Path, str]] = []
    for src in sorted(image_dir.rglob("*")):
        if not src.is_file() or src.name in SKIP_NAMES:
            continue
        rel = src.relative_to(image_dir).as_posix()
        plan.append((replacements.get(rel, src), rel))
    seen = {rel for _, rel in plan}
    for rel, src in replacements.items():
        if rel not in seen:
            plan.append((src, rel))
    return plan


def check_bootable(plan: list[tuple[Path, str]]) -> list[str]:
    """Warn about anything that would stop the written volume from booting."""
    rels = {rel.lower() for _, rel in plan}
    problems = []
    if "efi/boot/bootx64.efi" not in rels:
        problems.append(
            "EFI/BOOT/BOOTX64.EFI is missing — UEFI firmware will not find a bootloader "
            "on removable media without it.")
    if not any(r.endswith("vmlinuz") or "/kernel" in r for r in rels):
        problems.append("no kernel image (vmlinuz) found in the tree.")
    return problems


# --------------------------------------------------------------- the write

def write_bootable(device: Device, image_dir: Path, replacements: dict[str, Path],
                   volume_label: str = "BOOT", allow_virtual: bool = False,
                   set_esp_type: bool = True):
    """
    Generator yielding (fraction, level, message).

    Partitions the device GPT, formats one FAT32 volume, copies the tree with
    replacements applied, verifies by hash, and ejects.
    """
    if not image_dir.is_dir():
        raise USBError(f"image directory not found: {image_dir}")

    yield 0.00, "step", "Re-checking target device"
    device = refresh_device(device.node, allow_virtual=allow_virtual)
    assert_safe(device, allow_virtual=allow_virtual)
    yield 0.02, "info", f"  {device.label}"
    if device.mountpoints:
        yield 0.02, "info", f"  currently mounted at: {', '.join(device.mountpoints)}"
    yield 0.03, "warn", f"  ALL DATA ON {device.path} WILL BE DESTROYED"

    yield 0.05, "step", "Planning copy"
    plan = plan_copy(image_dir, replacements)
    total_bytes = sum(src.stat().st_size for src, _ in plan)
    yield 0.06, "info", f"  {len(plan)} files, {total_bytes:,} bytes"
    for rel, src in sorted(replacements.items()):
        yield 0.06, "info", f"  substituting {rel} <- {src.name}"
    for problem in check_bootable(plan):
        yield 0.07, "warn", f"  {problem}"
    if total_bytes > device.size:
        raise USBError(f"tree is {total_bytes:,} bytes; {device.node} holds {device.size:,}")

    label = (volume_label or "BOOT").upper()[:11]

    if IS_MAC:
        yield 0.10, "step", f"Unmounting {device.path}"
        proc = _run(["diskutil", "unmountDisk", "force", device.path], check=False)
        yield 0.12, "info", "  " + (proc.stdout or proc.stderr).strip()

        yield 0.15, "step", "Partitioning (GPT) and formatting FAT32"
        cmd = ["diskutil", "partitionDisk", device.path, "GPT", "MS-DOS FAT32", label, "100%"]
        yield 0.16, "cmd", "  $ " + " ".join(cmd)
        proc = _run(cmd, check=False, timeout=300)
        if proc.returncode != 0:
            raise USBError(f"partitionDisk failed: {(proc.stderr or proc.stdout).strip()}")
        for line in (proc.stdout or "").splitlines():
            if line.strip():
                yield 0.30, "info", "  " + line.strip()

        if set_esp_type and shutil.which("sgdisk"):
            yield 0.33, "step", "Marking partition as EFI System"
            proc = _run(["sudo", "-n", "sgdisk", "-t", "1:ef00", device.path], check=False)
            if proc.returncode == 0:
                yield 0.34, "ok", "  partition type set to ef00"
            else:
                yield 0.34, "info", ("  skipped (needs sudo); Microsoft Basic Data works on "
                                     "almost all firmware for removable media")
        elif set_esp_type:
            yield 0.34, "info", ("  sgdisk not installed — leaving partition type as Microsoft "
                                 "Basic Data (fine for removable media)")

        mount_point = _wait_for_mount(device.node)
        yield 0.36, "ok", f"  mounted at {mount_point}"

    elif IS_LINUX:
        if os.geteuid() != 0:
            raise USBError("partitioning and formatting on Linux requires root — re-run with sudo.")
        yield 0.10, "step", "Unmounting any mounted partitions"
        for mount in device.mountpoints:
            _run(["umount", mount], check=False)
        yield 0.15, "step", "Partitioning (GPT) and formatting FAT32"
        _run(["sgdisk", "--zap-all", device.path], check=False)
        _run(["sgdisk", "-n", "1:0:0", "-t", "1:ef00", device.path])
        part = f"{device.path}p1" if device.node[-1].isdigit() else f"{device.path}1"
        time.sleep(2)
        _run(["mkfs.vfat", "-F", "32", "-n", label, part])
        mount_point = "/mnt/system-graft"
        Path(mount_point).mkdir(parents=True, exist_ok=True)
        _run(["mount", part, mount_point])
        yield 0.36, "ok", f"  mounted at {mount_point}"
    else:
        raise USBError(f"USB writing is not implemented on {platform.system()}")

    root = Path(mount_point)
    try:
        yield 0.38, "step", "Copying boot tree"
        written = 0
        for index, (src, rel) in enumerate(plan):
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            written += src.stat().st_size
            frac = 0.38 + 0.42 * (written / max(total_bytes, 1))
            yield frac, "ok", f"  {rel}  ({src.stat().st_size:,} bytes)"

        yield 0.81, "step", "Flushing to media"
        _run(["sync"], check=False)

        yield 0.84, "step", "Verifying written files"
        mismatched = []
        for src, rel in plan:
            dest = root / rel
            if not dest.is_file():
                mismatched.append(f"{rel} (missing)")
                continue
            if dest.stat().st_size != src.stat().st_size:
                mismatched.append(f"{rel} (size)")
                continue
            if sha256(dest) != sha256(src):
                mismatched.append(f"{rel} (hash)")
        if mismatched:
            raise USBError("verification failed for: " + ", ".join(mismatched))
        yield 0.95, "ok", f"  all {len(plan)} files verified by sha256"

        for rel in sorted(replacements):
            yield 0.96, "ok", f"  patched file present on media: {rel}"
    finally:
        if IS_MAC:
            _run(["diskutil", "eject", device.path], check=False)
        elif IS_LINUX:
            _run(["umount", str(root)], check=False)

    yield 1.00, "ok", (f"Done. {device.node} is a UEFI-bootable volume labelled {label}. "
                       "Safe to remove.")


def _wait_for_mount(node: str, timeout: float = 30.0) -> str:
    """diskutil returns before the new volume is mounted; wait for it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = _run(["diskutil", "info", "-plist", f"{node}s1"], check=False)
        try:
            info = plistlib.loads(proc.stdout.encode())
        except Exception:
            info = {}
        mount = info.get("MountPoint")
        if mount:
            return mount
        time.sleep(0.5)
    raise USBError(f"{node}s1 did not mount within {timeout:.0f}s")


# ------------------------------------------------------------------- CLI

def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="Write a boot tree to removable media (UEFI).")
    ap.add_argument("--list", action="store_true", help="list eligible devices and exit")
    ap.add_argument("--device", help="device node, e.g. disk4")
    ap.add_argument("--image-dir", type=Path, help="boot tree to write")
    ap.add_argument("--replace", action="append", default=[],
                    metavar="REL=PATH", help="substitute a file, e.g. boot/initrd=./initrd.patched")
    ap.add_argument("--label", default="BOOT")
    ap.add_argument("--allow-virtual", action="store_true",
                    help="include attached disk images (for testing)")
    ap.add_argument("--yes-destroy-device", action="store_true",
                    help="required to actually write")
    args = ap.parse_args(argv)

    if args.list or not args.device:
        devices = list_devices(allow_virtual=args.allow_virtual)
        if not devices:
            print("No eligible removable devices found.")
            return
        for device in devices:
            print(f"  {device.label}")
            if device.mountpoints:
                print(f"      mounted: {', '.join(device.mountpoints)}")
        return

    try:
        device = refresh_device(args.device, allow_virtual=args.allow_virtual)
        assert_safe(device, allow_virtual=args.allow_virtual)
    except USBError as exc:
        raise SystemExit(f"ERROR: {exc}")
    if not args.yes_destroy_device:
        raise SystemExit(f"Refusing to write to {device.label} without --yes-destroy-device")

    replacements = {}
    for item in args.replace:
        rel, _, path = item.partition("=")
        replacements[rel] = Path(path)

    try:
        for frac, level, message in write_bootable(
                device, args.image_dir, replacements,
                volume_label=args.label, allow_virtual=args.allow_virtual):
            prefix = {"step": "==>", "ok": "  ok", "warn": "  !!",
                      "cmd": "   $", "info": "    "}.get(level, "    ")
            print(f"[{frac * 100:5.1f}%] {prefix} {message}")
    except USBError as exc:
        raise SystemExit(f"\nERROR: {exc}")


if __name__ == "__main__":
    main()
