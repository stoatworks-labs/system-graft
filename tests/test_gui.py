"""
Tests for the Tkinter front end.

Skipped wholesale when Tk cannot open a display, which is the case on a headless
CI runner — so these run locally and on any runner with one, and never turn a
missing display into a failure.

What is worth testing here is the wiring, not the widgets: that the inspection
buttons build the right request and drive the same generator the CLI does, that
the override checkboxes actually reach PatchRequest, and that every button is
dead while a job runs. Rendering is not something a test can usefully assert.

The worker thread is joined directly rather than pumped through the Tk event
loop; _poll_queue only moves finished items from the queue into the log widget,
and the queue is what carries the evidence.
"""

from __future__ import annotations

import queue
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import patcher  # noqa: E402
from test_discovery import (  # noqa: E402
    KVER, VERMAGIC, ImageBuilder, elf_module, require_squashfs,
)
from test_kernelspec import BASE_CONFIG, kernel_blob  # noqa: E402

try:
    import tkinter as tk
except ImportError:  # pragma: no cover - Tk missing entirely
    tk = None


def require_tk():
    if tk is None:
        raise unittest.SkipTest("tkinter not available")
    try:
        root = tk.Tk()
    except Exception as exc:  # tk.TclError, and whatever a broken install raises
        raise unittest.SkipTest(f"no display: {exc}")
    root.withdraw()
    return root


class GuiTestCase(unittest.TestCase):
    def setUp(self):
        require_squashfs()
        self.root = require_tk()
        self.addCleanup(self.root.destroy)
        import gui
        self.gui = gui

        self.tmp = Path(tempfile.mkdtemp(prefix="sg-gui-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        staging = self.tmp / "staging"
        staging.mkdir()
        builder = ImageBuilder(staging)
        builder.modules["igb.ko"] = elf_module({
            "name": "igb", "vermagic": VERMAGIC,
            "alias": "pci:v00008086d00001533sv*sd*bc*sc*i*"})
        builder.aliases = [("pci:v00008086d00001533sv*sd*bc*sc*i*", "igb")]
        builder.builtin = ["net/e1000e"]
        initrd = builder.build()

        self.image_dir = self.tmp / "imagedir"
        (self.image_dir / "boot").mkdir(parents=True)
        shutil.move(str(initrd), str(self.image_dir / "boot" / "initrd"))
        (self.image_dir / "boot" / "vmlinuz").write_bytes(kernel_blob(BASE_CONFIG))

        self.app = self.gui.App(self.root)
        self.app.image_dir.set(str(self.image_dir))
        self.app.profile_key.set("sgs")
        self.app._rescan()

    # ------------------------------------------------------------------ util

    def join_worker(self, timeout: float = 60.0):
        self.assertIsNotNone(self.app.worker, "no job was started")
        self.app.worker.join(timeout)
        self.assertFalse(self.app.worker.is_alive(), "job did not finish in time")

    def drain(self, timeout: float = 60.0) -> list[tuple]:
        """Wait for the worker and collect everything it queued."""
        self.join_worker(timeout)
        items = []
        while True:
            try:
                items.append(self.app.queue.get_nowait())
            except queue.Empty:
                return items

    def messages(self, items) -> str:
        return "\n".join(str(item[3]) for item in items)

    # ----------------------------------------------------------------- tests

    def test_image_is_discovered(self):
        self.assertTrue(self.app.images, "no image found in the fixture directory")
        self.assertEqual(self.app._current_image().name, "initrd")

    def test_report_runs_and_reaches_the_queue(self):
        self.app.run_report()
        text = self.messages(self.drain())
        self.assertIn(f"Kernel:   {KVER}", text)
        self.assertIn("built by: builder@buildhost", text)
        self.assertIn("No image was written", text)

    def test_hardware_coverage_uses_the_pasted_listing(self):
        # Real lspci output, because the class code is what a family hint keys
        # off — a bare vendor:device pair carries none, and correctly gets none.
        self.app._start_inspection(
            self.app._current_image(), "Hardware coverage",
            hardware_text=(
                "02:00.0 Ethernet controller [0200]: Intel Corporation I210 [8086:1533]\n"
                "03:00.0 Ethernet controller [0200]: Realtek RTL8111 [10ec:8168] (rev 15)\n"))
        text = self.messages(self.drain())
        self.assertIn("covered by a module in the image", text)
        self.assertIn("igb", text)
        self.assertIn("NOT COVERED", text)
        self.assertIn("r8169", text, "the hint for the uncovered device should appear")

    def test_bare_id_pairs_get_no_class_based_hint(self):
        """The other side of the above: no class code, so no family hint."""
        self.app._start_inspection(self.app._current_image(), "Hardware coverage",
                                   hardware_text="10ec:8168 a Realtek")
        text = self.messages(self.drain())
        self.assertIn("NOT COVERED", text)
        self.assertNotIn("hint:", text)

    def test_scan_ranks_candidates(self):
        pack = self.tmp / "pack"
        pack.mkdir()
        (pack / "r8169.ko").write_bytes(elf_module({"name": "r8169", "vermagic": VERMAGIC}))
        (pack / "old.ko").write_bytes(elf_module({"name": "old", "vermagic": "6.1.0-19-amd64 SMP"}))
        self.app._start_inspection(self.app._current_image(), "Find modules",
                                   scan_paths=[pack])
        text = self.messages(self.drain())
        self.assertIn("MATCH — vermagic is identical", text)
        self.assertIn("r8169", text)
        self.assertIn("different kernel — will not load", text)

    def test_build_spec_writes_files(self):
        dest = self.tmp / "spec"
        self.app._start_inspection(self.app._current_image(), "Build spec", build_spec=dest)
        self.drain()
        for name in ("build-spec.json", "config", "HOW-TO-BUILD.md"):
            self.assertTrue((dest / name).is_file(), f"{name} not written")

    def test_inspection_writes_no_image(self):
        self.app.run_report()
        self.drain()
        self.assertFalse((self.image_dir / "boot" / "initrd.patched").exists())

    def test_every_button_is_disabled_while_a_job_runs(self):
        # Asserted by label rather than by count: a bare number goes stale every
        # time a button is added and says nothing about what went missing.
        labels = {button["text"] for button in self.app._job_buttons}
        self.assertEqual(labels, {"Report", "Hardware…", "Find modules…", "Find online…",
                                  "Build spec…", "Patch", "Write to USB…"})
        self.app.run_report()
        # The job is started synchronously, so the buttons are already down.
        for button in self.app._job_buttons:
            self.assertIn("disabled", button.state(), f"{button['text']} stayed enabled")
        # _poll_queue is what re-enables them, and it does so by draining the
        # queue — so it has to see the queue, not a drained one.
        self.join_worker()
        self.app._poll_queue()
        for button in self.app._job_buttons:
            self.assertNotIn("disabled", button.state(), f"{button['text']} stayed disabled")

    def test_overrides_reach_the_patch_request(self):
        captured = {}

        def fake_patch(request):
            captured["request"] = request
            yield 1.0, "ok", "stub"

        self.app.modules = [self.tmp / "any.ko"]
        (self.tmp / "any.ko").write_bytes(elf_module({"name": "any", "vermagic": VERMAGIC}))
        self.app.allow_mismatch.set(True)
        self.app.allow_unsigned.set(True)
        self.app.allow_missing_deps.set(True)
        self.app.keep_xattrs.set(True)
        self.app.output_path.set(str(self.tmp / "never-written.sqfs"))

        original = patcher.patch
        patcher.patch = fake_patch
        try:
            self.app.start_patch()
            self.drain()
        finally:
            patcher.patch = original

        request = captured["request"]
        self.assertTrue(request.allow_vermagic_mismatch)
        self.assertTrue(request.allow_unsigned)
        self.assertTrue(request.allow_missing_deps)
        self.assertTrue(request.keep_xattrs)

    def test_module_list_reports_signing_firmware_and_deps(self):
        path = self.tmp / "needy.ko"
        path.write_bytes(elf_module({
            "name": "needy", "vermagic": VERMAGIC,
            "firmware": "intel/needy.bin", "depends": "mdio"}))
        self.app.modules.append(path)
        import kmod
        info = kmod.read(path)
        self.assertEqual(info.firmware, ["intel/needy.bin"])
        self.assertEqual(info.depends, ["mdio"])
        self.assertFalse(info.signed)


class HardwareDialogTests(unittest.TestCase):
    """The dialog's validation, which is the part with logic in it."""

    def setUp(self):
        self.root = require_tk()
        self.addCleanup(self.root.destroy)
        import gui
        self.gui = gui
        self.tmp = Path(tempfile.mkdtemp(prefix="sg-dlg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_accepts_text_and_an_alias_db(self):
        db = self.tmp / "modules.alias"
        db.write_text("alias pci:v000010ECd00008168sv*sd*bc*sc*i* r8169\n")
        dialog = self.gui.HardwareDialog(self.root, "8086:1533", str(db))
        dialog._accept()
        self.assertIsNotNone(dialog.result)
        text, alias_db = dialog.result
        self.assertEqual(text, "8086:1533")
        self.assertEqual(alias_db, db)

    def test_prefilled_text_survives(self):
        dialog = self.gui.HardwareDialog(self.root, "8086:1533 prefilled", "")
        self.assertIn("prefilled", dialog.text.get("1.0", "end"))
        dialog.destroy()

    def test_no_alias_db_is_fine(self):
        dialog = self.gui.HardwareDialog(self.root, "8086:1533", "")
        dialog._accept()
        self.assertEqual(dialog.result, ("8086:1533", None))

    def test_a_missing_alias_db_is_refused(self):
        dialog = self.gui.HardwareDialog(self.root, "8086:1533", str(self.tmp / "nope"))
        shown = []
        original = self.gui.messagebox.showerror
        self.gui.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            dialog._accept()
        finally:
            self.gui.messagebox.showerror = original
        self.assertIsNone(dialog.result, "must not accept a nonexistent alias database")
        self.assertTrue(shown, "the user must be told why")
        dialog.destroy()

    def test_empty_listing_is_refused(self):
        dialog = self.gui.HardwareDialog(self.root, "   \n  ", "")
        shown = []
        original = self.gui.messagebox.showwarning
        self.gui.messagebox.showwarning = lambda *a, **k: shown.append(a)
        try:
            dialog._accept()
        finally:
            self.gui.messagebox.showwarning = original
        self.assertIsNone(dialog.result)
        self.assertTrue(shown)
        dialog.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
