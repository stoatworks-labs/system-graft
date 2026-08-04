"""
Tests for locating a prebuilt module in a distribution's archive.

**No test here touches the network.** Archive responses are stubbed and the
download path runs against a throwaway HTTP server on localhost. That is not
squeamishness about network tests — it is that a test which fails when
snapshot.debian.org is slow tells you nothing about this code, and a test which
downloads 65 MB to assert a rename is a bad trade.

What genuinely needs the real archives is the *shape* of their responses, and
that was checked against them by hand while writing this; the stubs below are
recorded from those real replies.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import distro  # noqa: E402

# Recorded from snapshot.debian.org and api.launchpad.net.
DEBIAN_VERSIONS = {"result": [{"binary_version": "6.1.82-1",
                               "name": "linux-image-6.1.0-19-amd64",
                               "source": "linux-signed-amd64"}]}
DEBIAN_BINFILES = {"result": [{"architecture": "amd64",
                               "hash": "2ee9caba092c6e95ac73c886d04b83af7559b2df"}]}
DEBIAN_FILEINFO = {"result": [{"archive_name": "debian",
                               "name": "linux-image-6.1.0-19-amd64_6.1.82-1_amd64.deb",
                               "path": "/pool/main/l/linux-signed-amd64",
                               "size": 68781560}]}
LAUNCHPAD_BINARIES = {"entries": [
    {"binary_package_name": "linux-modules-extra-5.15.0-91-generic",
     "binary_package_version": "5.15.0-91.101",
     "distro_arch_series_link": "https://api.launchpad.net/1.0/ubuntu/jammy/s390x"},
    {"binary_package_name": "linux-modules-extra-5.15.0-91-generic",
     "binary_package_version": "5.15.0-91.101",
     "distro_arch_series_link": "https://api.launchpad.net/1.0/ubuntu/jammy/amd64"},
]}


class IdentifyTests(unittest.TestCase):
    def test_release_string_decides_and_compiler_corroborates(self):
        target = distro.identify("6.1.0-19-amd64", "gcc (Debian 12.2.0-14) 12.2.0")
        self.assertEqual(target.distro, distro.DEBIAN)
        self.assertEqual(target.arch, "amd64")
        self.assertIn("flavour", target.evidence)
        self.assertIn("Debian", target.evidence)

    def test_a_custom_kernel_built_on_debian_is_not_a_debian_kernel(self):
        """
        The case this tool exists for. Appliance vendors build bespoke kernels on
        Debian boxes; the banner says Debian and the archive has nothing.
        """
        target = distro.identify("6.12.11", "gcc (Debian 12.2.0-14) 12.2.0")
        self.assertEqual(target.distro, distro.VENDOR)
        self.assertFalse(target.is_stock)
        self.assertIn("custom kernel", target.evidence)
        self.assertIn("Debian", target.evidence, "should still say where it was built")

    def test_compiler_breaks_a_genuine_debian_ubuntu_ambiguity(self):
        target = distro.identify("5.15.0-91-weirdflavour",
                                 "gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0")
        self.assertEqual(target.distro, distro.UBUNTU)
        self.assertIn("guess", target.evidence, "a weak inference must be labelled")

    def test_ubuntu_from_compiler(self):
        target = distro.identify("5.15.0-91-generic",
                                 "gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0", "amd64")
        self.assertEqual(target.distro, distro.UBUNTU)
        self.assertEqual(target.flavour, "generic")
        self.assertEqual(target.base, "5.15.0-91")

    def test_flavour_discriminates_without_a_compiler(self):
        """Debian names flavours after architectures; Ubuntu after roles."""
        self.assertEqual(distro.identify("5.15.0-91-generic").distro, distro.UBUNTU)
        self.assertEqual(distro.identify("6.1.0-19-amd64").distro, distro.DEBIAN)
        self.assertEqual(distro.identify("6.1.0-19-rt-amd64").distro, distro.DEBIAN)

    def test_rhel_from_release_tag(self):
        target = distro.identify("5.14.0-362.8.1.el9_3.x86_64")
        self.assertEqual(target.distro, distro.RHEL)
        self.assertEqual(target.arch, "x86_64")

    def test_appliance_kernel_is_named_as_such(self):
        """The most important answer this module gives."""
        target = distro.identify("6.12.11")
        self.assertEqual(target.distro, distro.VENDOR)
        self.assertFalse(target.is_stock)

    def test_empty_release(self):
        self.assertEqual(distro.identify("").distro, "")


class PackageTests(unittest.TestCase):
    def test_debian_modules_live_in_linux_image(self):
        names = [p.name for p in
                 distro.packages_for(distro.identify("6.1.0-19-amd64"))]
        self.assertEqual(names, ["linux-image-6.1.0-19-amd64"])

    def test_ubuntu_lists_extra_first(self):
        """-extra holds most NIC drivers, so it is the one worth fetching first."""
        names = [p.name for p in
                 distro.packages_for(distro.identify("5.15.0-91-generic"))]
        self.assertEqual(names[0], "linux-modules-extra-5.15.0-91-generic")
        self.assertIn("linux-modules-5.15.0-91-generic", names)

    def test_rhel_strips_the_arch_suffix(self):
        names = [p.name for p in
                 distro.packages_for(distro.identify("5.14.0-362.8.1.el9_3.x86_64"))]
        self.assertTrue(all(not n.endswith(".x86_64") for n in names), names)
        self.assertIn("kernel-modules-extra-5.14.0-362.8.1.el9_3", names)

    def test_vendor_kernel_has_no_packages(self):
        self.assertEqual(distro.packages_for(distro.identify("6.12.11")), [])


class ResolveTests(unittest.TestCase):
    """Resolution against recorded archive replies."""

    def setUp(self):
        self.get_json = distro._get_json
        self.head = distro._head
        self.addCleanup(self._restore)

    def _restore(self):
        distro._get_json = self.get_json
        distro._head = self.head

    def test_vendor_kernel_explains_rather_than_failing(self):
        result = distro.resolve(distro.identify("6.12.11"))
        self.assertEqual(result.downloads, [])
        joined = " ".join(result.notes)
        self.assertIn("not a stock distro kernel", joined)
        self.assertIn("--build-spec", joined, "must point at the thing that does help")

    def test_debian_resolves_with_a_checksum(self):
        def fake_json(url):
            if url.endswith("/binfiles"):
                return DEBIAN_BINFILES
            if "/mr/file/" in url:
                return DEBIAN_FILEINFO
            return DEBIAN_VERSIONS
        distro._get_json = fake_json

        result = distro.resolve(distro.identify("6.1.0-19-amd64",
                                                "gcc (Debian 12.2.0-14) 12.2.0"))
        self.assertEqual(len(result.downloads), 1)
        item = result.downloads[0]
        self.assertTrue(item.url.startswith("https://snapshot.debian.org/file/"))
        self.assertEqual(item.sha1, "2ee9caba092c6e95ac73c886d04b83af7559b2df")
        self.assertEqual(item.size, 68781560)
        self.assertIn("65.6 MB", item.size_h)

    def test_debian_reports_a_missing_architecture(self):
        def fake_json(url):
            if url.endswith("/binfiles"):
                return {"result": [{"architecture": "arm64", "hash": "deadbeef"}]}
            return DEBIAN_VERSIONS
        distro._get_json = fake_json

        result = distro.resolve(distro.identify("6.1.0-19-amd64",
                                                "gcc (Debian 12.2.0-14) 12.2.0"))
        self.assertEqual(result.downloads, [])
        self.assertIn("no build for amd64", " ".join(result.notes))

    def test_ubuntu_picks_the_right_architecture(self):
        distro._get_json = lambda url: LAUNCHPAD_BINARIES
        distro._head = lambda url: 61_000_000
        result = distro.resolve(distro.identify("5.15.0-91-generic", "", "amd64"))
        self.assertTrue(result.downloads)
        self.assertIn("5.15.0-91.101_amd64.deb", result.downloads[0].filename)
        self.assertNotIn("s390x", result.downloads[0].filename)
        self.assertEqual(result.downloads[0].size, 61_000_000)

    def test_ubuntu_says_when_no_mirror_serves_it(self):
        distro._get_json = lambda url: LAUNCHPAD_BINARIES
        distro._head = lambda url: None
        result = distro.resolve(distro.identify("5.15.0-91-generic", "", "amd64"))
        self.assertEqual(result.downloads, [])
        self.assertIn("no mirror served", " ".join(result.notes))

    def test_ubuntu_flags_the_lack_of_a_checksum(self):
        distro._get_json = lambda url: LAUNCHPAD_BINARIES
        distro._head = lambda url: 10
        result = distro.resolve(distro.identify("5.15.0-91-generic", "", "amd64"))
        self.assertIn("not verified", " ".join(result.notes))

    def test_rhel_gives_instructions_not_a_guessed_url(self):
        result = distro.resolve(distro.identify("5.14.0-362.8.1.el9_3.x86_64"))
        self.assertEqual(result.downloads, [])
        text = " ".join(result.instructions)
        self.assertIn("kernel-modules-extra", text)
        self.assertIn("vault", text)
        self.assertIn("--scan", text, "must say what to do with the download")

    def test_a_failing_lookup_is_reported_not_raised(self):
        def boom(url):
            raise distro.DistroError("archive unreachable")
        distro._get_json = boom
        result = distro.resolve(distro.identify("6.1.0-19-amd64",
                                                "gcc (Debian 12.2.0) 12.2.0"))
        self.assertEqual(result.downloads, [])
        self.assertIn("archive unreachable", " ".join(result.notes))


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class DownloadTests(unittest.TestCase):
    """The fetch path, against a throwaway server on localhost."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sg-dl-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.served = self.tmp / "served"
        self.served.mkdir()
        self.payload = b"a kernel module package, honestly" * 500
        (self.served / "pkg.deb").write_bytes(self.payload)
        self.sha1 = hashlib.sha1(self.payload).hexdigest()

        handler = lambda *a, **k: _QuietHandler(*a, directory=str(self.served), **k)  # noqa: E731
        self.server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def item(self, sha1: str = "", name: str = "pkg.deb") -> distro.Download:
        return distro.Download(url=f"{self.base}/pkg.deb", filename=name,
                               package="test", size=len(self.payload), sha1=sha1)

    def test_downloads_and_verifies(self):
        seen = []
        path = distro.download(self.item(self.sha1), self.tmp / "out",
                               progress=lambda n, t: seen.append((n, t)))
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertTrue(seen, "progress callback was never called")
        self.assertEqual(seen[-1][0], len(self.payload))

    def test_a_bad_checksum_discards_the_file(self):
        with self.assertRaises(distro.DistroError) as ctx:
            distro.download(self.item("0" * 40), self.tmp / "out")
        self.assertIn("failed its checksum", str(ctx.exception))
        self.assertFalse((self.tmp / "out" / "pkg.deb").exists(),
                         "a file that failed verification must not be left behind")
        self.assertFalse(list((self.tmp / "out").glob("*.part")), "partial file left behind")

    def test_no_checksum_still_downloads(self):
        path = distro.download(self.item(), self.tmp / "out")
        self.assertEqual(path.read_bytes(), self.payload)

    def test_existing_file_is_not_refetched(self):
        dest = self.tmp / "out"
        dest.mkdir()
        (dest / "pkg.deb").write_bytes(b"already here")
        path = distro.download(self.item(self.sha1), dest)
        self.assertEqual(path.read_bytes(), b"already here")

    def test_a_missing_url_is_reported_not_raised_as_urlerror(self):
        item = distro.Download(url=f"{self.base}/nope.deb", filename="nope.deb", package="t")
        with self.assertRaises(distro.DistroError):
            distro.download(item, self.tmp / "out")
        self.assertFalse(list((self.tmp / "out").glob("*")), "nothing should be left behind")


if __name__ == "__main__":
    unittest.main(verbosity=2)
