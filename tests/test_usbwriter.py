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

from usbwriter import find_volume_slice  # noqa: E402


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
