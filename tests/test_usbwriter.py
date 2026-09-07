"""Which slice the FAT32 volume actually lands on.

The writer used to wait for `<node>s1` to mount. `diskutil partitionDisk <dev>
GPT ...` creates a 209.7 MB EFI System Partition as slice 1 and puts the
requested volume at slice 2 — but only above a size threshold. Measured: a
256 MB and a 2 GB image get the volume at s1; an 8 GB and a 32 GB one get EFI
at s1 and the volume at s2.

So every disk image the writer was ever tested against put the volume at s1,
and every real USB stick puts it at s2. macOS does not auto-mount the ESP, so
on real hardware the wait timed out after 30 s — and the raise happened after
`unmountDisk force` and `partitionDisk` had already erased the stick, and
before the try/finally that ejects. A wiped, empty, un-ejected device and a red
error.

These fixtures are the two layouts, so the parsing can be tested without a
device — which is the point, since the repo's own rule is not to test against
physical media.
"""

import plistlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from usbwriter import find_volume_slice, linux_devices_from_lsblk, linux_partition_node  # noqa: E402


def _plist(partitions, node="disk4"):
    return plistlib.dumps({
        "AllDisksAndPartitions": [
            {"DeviceIdentifier": node, "Partitions": partitions}
        ]
    })


SMALL_IMAGE = _plist([
    {"DeviceIdentifier": "disk4s1", "Content": "Microsoft Basic Data",
     "VolumeName": "SGTEST", "Size": 2_000_000_000},
])

REAL_STICK = _plist([
    {"DeviceIdentifier": "disk4s1", "Content": "EFI",
     "VolumeName": "EFI", "Size": 209_715_200},
    {"DeviceIdentifier": "disk4s2", "Content": "Microsoft Basic Data",
     "VolumeName": "SGTEST", "Size": 8_400_000_000},
])


def test_small_image_puts_the_volume_at_s1():
    assert find_volume_slice(SMALL_IMAGE, "disk4", "SGTEST") == ("disk4s1", 1)


def test_a_real_stick_puts_the_volume_at_s2_behind_an_esp():
    # The case that erased people's sticks: s1 is Apple's ESP and never mounts.
    assert find_volume_slice(REAL_STICK, "disk4", "SGTEST") == ("disk4s2", 2)


def test_the_esp_is_never_chosen_even_without_a_name_match():
    # Label mismatch falls back to "first slice that is not an ESP", which must
    # still skip s1 rather than picking the partition that cannot mount.
    assert find_volume_slice(REAL_STICK, "disk4", "SOMETHING ELSE") == ("disk4s2", 2)


def test_the_match_is_case_insensitive():
    assert find_volume_slice(REAL_STICK, "disk4", "sgtest") == ("disk4s2", 2)


def test_a_full_device_path_is_accepted():
    assert find_volume_slice(REAL_STICK, "/dev/disk4", "SGTEST") == ("disk4s2", 2)


def test_another_disk_is_not_searched():
    other = _plist([
        {"DeviceIdentifier": "disk9s1", "Content": "Microsoft Basic Data",
         "VolumeName": "SGTEST", "Size": 1},
    ], node="disk9")
    assert find_volume_slice(other, "disk4", "SGTEST") == (None, None)


def test_no_partitions_yet_is_not_a_guess():
    # diskutil returns before the table is readable; the caller polls, so this
    # must report "nothing yet" rather than inventing s1.
    assert find_volume_slice(_plist([]), "disk4", "SGTEST") == (None, None)


def test_unparseable_output_is_not_a_guess():
    assert find_volume_slice(b"not a plist", "disk4", "SGTEST") == (None, None)


# --------------------------------------------------------------------------
# Linux enumeration — from lsblk JSON, never from a device
# --------------------------------------------------------------------------

# What `lsblk -J -b -o NAME,SIZE,MODEL,RM,TRAN,TYPE,MOUNTPOINT,HOTPLUG` says on
# a KVM guest with a virtio root disk, one USB stick, an NVMe drive, an SD card
# and a loop-mounted disk image. The loop device is TYPE "loop" with no TRAN —
# the case the first cut of the parser silently dropped.
LSBLK = {"blockdevices": [
    {"name": "loop13", "size": 67108864, "model": None, "rm": False, "tran": None,
     "type": "loop", "mountpoint": None, "hotplug": False},
    {"name": "vda", "size": 42949672960, "model": None, "rm": False, "tran": "virtio",
     "type": "disk", "mountpoint": None, "hotplug": False,
     "children": [{"name": "vda1", "mountpoint": "/"}]},
    {"name": "nvme0n1", "size": 1000204886016, "model": "Samsung SSD", "rm": False,
     "tran": "nvme", "type": "disk", "mountpoint": None, "hotplug": False},
    {"name": "sdb", "size": 31029460992, "model": "DataTraveler", "rm": True, "tran": "usb",
     "type": "disk", "mountpoint": None, "hotplug": True,
     "children": [{"name": "sdb1", "mountpoint": "/media/lab/STICK"}]},
    {"name": "mmcblk0", "size": 15931539456, "model": None, "rm": True, "tran": None,
     "type": "disk", "mountpoint": None, "hotplug": True},
    {"name": "sr0", "size": 1073741824, "model": "QEMU DVD-ROM", "rm": True, "tran": "sata",
     "type": "rom", "mountpoint": None, "hotplug": True},
]}


def _nodes(devices):
    return sorted(d.node for d in devices)


def test_linux_only_removable_media_by_default():
    devices = linux_devices_from_lsblk(LSBLK, allow_virtual=False)
    assert _nodes(devices) == ["mmcblk0", "sdb"]
    assert all(not d.internal and not d.virtual for d in devices)


def test_linux_allow_virtual_admits_the_loop_device_and_nothing_internal():
    # The whole reason the flag exists: the write path is tested against an
    # attached disk image, and on Linux that is a loop device.
    devices = linux_devices_from_lsblk(LSBLK, allow_virtual=True)
    assert _nodes(devices) == ["loop13", "mmcblk0", "sdb"]
    loop = next(d for d in devices if d.node == "loop13")
    assert loop.virtual and not loop.internal and loop.path == "/dev/loop13"


def test_linux_internal_disks_are_never_offered():
    # vda is the guest's root disk, nvme0n1 a real internal drive: neither may
    # appear whatever the flags, and an empty TRAN must not read as "virtual".
    for allow in (False, True):
        nodes = _nodes(linux_devices_from_lsblk(LSBLK, allow_virtual=allow))
        assert "vda" not in nodes and "nvme0n1" not in nodes


def test_linux_usb_mountpoints_are_collected_from_children():
    sdb = next(d for d in linux_devices_from_lsblk(LSBLK, False) if d.node == "sdb")
    assert sdb.mountpoints == ["/media/lab/STICK"]
    assert sdb.bus == "USB"


def test_linux_partition_node_naming():
    assert linux_partition_node("/dev/sdb") == "/dev/sdb1"
    assert linux_partition_node("/dev/loop13") == "/dev/loop13p1"
    assert linux_partition_node("/dev/nvme0n1") == "/dev/nvme0n1p1"
    assert linux_partition_node("/dev/mmcblk0") == "/dev/mmcblk0p1"
