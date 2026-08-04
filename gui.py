#!/usr/bin/env python3
"""
Tkinter front end.

Two stages, sharing one progress bar and one log:
  1. Patch    — inject modules into the initrd, write a new image file.
  2. Write    — put the boot tree (with the patched initrd substituted) onto
                removable media as a UEFI-bootable volume.

Stage 1 also carries the read-only inspection tools — coverage, module search,
build spec — which answer "which driver do I need, and which build of it" before
there is anything to inject. They write nothing.

All work runs on a worker thread; the UI only ever drains a queue. Both
patcher.patch() and patcher.inspect() are generators yielding
(fraction, level, message), so _run_job drives either without caring which.
"""

from __future__ import annotations

import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import kmod
import patcher
import usbwriter
from _version import __version__
from patcher import PatchError, PatchRequest, PROFILES
from usbwriter import USBError

import diag
from about_dialog import show_about

APP_TITLE = f"System Graft {__version__}"

BG = "#12141a"
FG = "#d6dae3"
MUTED = "#8b93a7"
LEVEL_COLOURS = {
    "step": "#7fd6ff",
    "ok": "#7ee787",
    "warn": "#f0c674",
    "error": "#ff7b72",
    "cmd": "#8b93a7",
    "info": "#d6dae3",
}
MONO = ("SF Mono", 11) if sys.platform == "darwin" else ("Consolas", 10)
MONO_BOLD = MONO + ("bold",)


class HardwareDialog(tk.Toplevel):
    """
    Collect a device listing from the target machine, and optionally an alias DB.

    A paste box rather than a file picker, because of where this data comes from:
    a machine that will not boot. The listing gets read off a screen, or copied
    out of a serial console, or produced by a live USB stick — it very often
    exists as text in a terminal window and never as a file on the disk running
    this tool. Demanding a file first would make the common case the awkward one.
    """

    PLACEHOLDER = (
        "Run one of these on the target machine (or on any Linux booted on that\n"
        "hardware — a live USB is fine) and paste the output here:\n"
        "\n"
        "    lspci -nnmm        preferred: includes subsystem IDs\n"
        "    lspci -nn          fine, but some matches will come back UNCERTAIN\n"
        "    lsusb              for USB devices\n"
        "\n"
        "A bare list of 8086:1533 pairs works too, and you can mix all of them."
    )

    @classmethod
    def ask(cls, master, initial_text: str = "",
            initial_alias_db: str = "") -> tuple[str, Path | None] | None:
        """Show the dialog modally and return its result, or None if cancelled."""
        dialog = cls(master, initial_text, initial_alias_db)
        dialog.grab_set()
        dialog.wait_window(dialog)
        return dialog.result

    def __init__(self, master, initial_text: str = "", initial_alias_db: str = ""):
        # The modal wait deliberately lives in ask(), not here: a constructor that
        # blocks until the user answers cannot be built in a test, and the input
        # validation below is exactly the part worth testing.
        super().__init__(master)
        self.title("Hardware coverage")
        self.result: tuple[str, Path | None] | None = None
        self.transient(master)
        self.resizable(True, True)

        frame = ttk.Frame(self, padding=10)
        frame.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text=self.PLACEHOLDER, foreground=MUTED, justify="left",
                  font=MONO).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self.text = tk.Text(frame, width=88, height=14, wrap="none", font=MONO,
                            background=BG, foreground=FG, insertbackground=FG, relief="flat")
        self.text.grid(row=1, column=0, columnspan=2, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        scroll.grid(row=1, column=2, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)
        if initial_text:
            self.text.insert("1.0", initial_text)

        ttk.Label(frame, text="Alias database (optional)").grid(
            row=2, column=0, sticky="w", pady=(10, 0))
        self.alias_db = tk.StringVar(value=initial_alias_db)
        ttk.Entry(frame, textvariable=self.alias_db).grid(
            row=3, column=0, sticky="ew", pady=(2, 0))
        ttk.Button(frame, text="Browse…", command=self._pick_alias_db).grid(
            row=3, column=1, columnspan=2, padx=(6, 0), pady=(2, 0))
        ttk.Label(
            frame,
            text=("A modules.alias from any Linux system — /lib/modules/$(uname -r)/modules.alias. "
                  "Without one, drivers for hardware this image lacks can only be hinted at."),
            wraplength=620, foreground=MUTED,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="Check coverage", command=self._accept).grid(row=0, column=1)

        self.text.focus_set()

    def _pick_alias_db(self):
        path = filedialog.askopenfilename(
            title="Select a modules.alias file", parent=self,
            filetypes=[("modules.alias", "modules.alias"), ("All files", "*")])
        if path:
            self.alias_db.set(path)

    def _accept(self):
        text = self.text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning(APP_TITLE, "Paste a device listing first.", parent=self)
            return
        raw = self.alias_db.get().strip()
        alias_db = Path(raw) if raw else None
        if alias_db and not alias_db.is_file():
            messagebox.showerror(APP_TITLE, f"Not a file: {alias_db}", parent=self)
            return
        self.result = (text, alias_db)
        self.destroy()


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.image_dir = tk.StringVar()
        self.selected_image = tk.StringVar()
        self.output_path = tk.StringVar()
        self.profile_key = tk.StringVar(value="sgs")
        self.allow_mismatch = tk.BooleanVar(value=False)
        self.allow_unsigned = tk.BooleanVar(value=False)
        self.allow_missing_deps = tk.BooleanVar(value=False)
        self.keep_xattrs = tk.BooleanVar(value=False)
        self.device_choice = tk.StringVar()
        self.volume_label = tk.StringVar(value="BOOT")
        self.status = tk.StringVar(value="Ready.")

        self.images: list[Path] = []
        self.modules: list[Path] = []
        self.devices: list[usbwriter.Device] = []
        self.last_output: Path | None = None
        self.queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        # Every button that must be dead while a job runs. Collected as they are
        # built rather than named individually, so adding one cannot forget it.
        self._job_buttons: list[ttk.Button] = []
        # Remembered so re-running a coverage check after fixing something does
        # not mean pasting the whole listing again.
        self._hardware_text = ""
        self._alias_db = ""

        self._build()
        self._poll_queue()
        self.refresh_devices(quiet=True)

    # ------------------------------------------------------------------ UI

    def _build(self):
        ttk.Label(
            self,
            text=("You supply the image and the modules. This tool ships neither, never "
                  "modifies the input, and always writes new output."),
            wraplength=880, foreground=MUTED,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        self._build_patch_section().grid(row=1, column=0, sticky="ew")
        self._build_usb_section().grid(row=2, column=0, sticky="ew", pady=(10, 0))

        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, sticky="ew", pady=(10, 4))
        bar.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(bar, mode="determinate", maximum=100.0)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="Save log…", command=self.save_log).grid(row=0, column=1, padx=(10, 0))
        # Vendored from stoatworks-backend/about - see about_dialog.py.
        ttk.Button(bar, text="About", command=lambda: show_about(self.master, __version__)).grid(
            row=0, column=2, padx=(6, 0)
        )

        logframe = ttk.Frame(self)
        logframe.grid(row=4, column=0, sticky="nsew")
        logframe.columnconfigure(0, weight=1)
        logframe.rowconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)
        # The log is the only row with weight, so it is what absorbs a resize.
        # Kept modest by default because stage 1 now carries the inspection
        # controls too, and the window should still open on a 900px-tall screen.
        self.log = tk.Text(logframe, height=12, wrap="none", background=BG, foreground=FG,
                           insertbackground=FG, relief="flat", font=MONO)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(logframe, orient="vertical", command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set, state="disabled")
        for level, colour in LEVEL_COLOURS.items():
            self.log.tag_configure(level, foreground=colour)
        self.log.tag_configure("step", foreground=LEVEL_COLOURS["step"], font=MONO_BOLD)

        ttk.Label(self, textvariable=self.status, foreground=MUTED).grid(
            row=5, column=0, sticky="w", pady=(6, 0))

        self._append("info", f"{APP_TITLE} — ready.")
        try:
            un, mk = patcher.require_tools()
            self._append("ok", patcher.tool_version(un))
            self._append("ok", patcher.tool_version(mk))
        except PatchError as exc:
            self._append("error", str(exc))

    def _build_patch_section(self) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(self, text=" 1 · Patch initrd ", padding=10)
        frame.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(frame, text="OS image directory").grid(row=row, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.image_dir).grid(row=row, column=1, sticky="ew", padx=6)
        ttk.Button(frame, text="Browse…", command=self.pick_dir).grid(row=row, column=2)
        row += 1

        ttk.Label(frame, text="Image").grid(row=row, column=0, sticky="w", pady=(6, 0))
        self.image_combo = ttk.Combobox(frame, textvariable=self.selected_image, state="readonly")
        self.image_combo.grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=(6, 0))
        self.image_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_image_change())
        row += 1

        self.image_info = ttk.Label(frame, text="—", foreground=MUTED)
        self.image_info.grid(row=row, column=1, columnspan=2, sticky="w", padx=6)
        row += 1

        ttk.Label(frame, text="Profile").grid(row=row, column=0, sticky="w", pady=(6, 0))
        self.profile_combo = ttk.Combobox(
            frame, state="readonly", values=[PROFILES[k].label for k in sorted(PROFILES)])
        self.profile_combo.grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=(6, 0))
        self.profile_combo.set(PROFILES[self.profile_key.get()].label)
        self.profile_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._set_profile_from_label(self.profile_combo.get()))
        row += 1

        # Before the module list on purpose: these are the tools for working out
        # *which* module you need, so they belong upstream of choosing one.
        ttk.Label(frame, text="Inspect").grid(row=row, column=0, sticky="w", pady=(10, 0))
        inspect_bar = ttk.Frame(frame)
        inspect_bar.grid(row=row, column=1, columnspan=2, sticky="w", padx=6, pady=(10, 0))
        for index, (label, command) in enumerate((
                ("Report", self.run_report),
                ("Hardware…", self.run_hardware),
                ("Find modules…", self.run_scan),
                ("Find online…", self.run_find_drivers),
                ("Build spec…", self.run_build_spec))):
            button = ttk.Button(inspect_bar, text=label, command=command)
            button.grid(row=0, column=index, padx=(0, 6))
            self._job_buttons.append(button)
        row += 1

        ttk.Label(frame, text="None of these write anything — results go to the log below.",
                  foreground=MUTED).grid(row=row, column=1, columnspan=2, sticky="w", padx=6)
        row += 1

        ttk.Label(frame, text="Modules (.ko)").grid(row=row, column=0, sticky="nw", pady=(6, 0))
        listframe = ttk.Frame(frame)
        listframe.grid(row=row, column=1, sticky="ew", padx=6, pady=(6, 0))
        listframe.columnconfigure(0, weight=1)
        self.module_list = tk.Listbox(listframe, height=3, activestyle="none")
        self.module_list.grid(row=0, column=0, sticky="ew")
        ttk.Scrollbar(listframe, orient="vertical",
                      command=self.module_list.yview).grid(row=0, column=1, sticky="ns")

        btns = ttk.Frame(frame)
        btns.grid(row=row, column=2, sticky="n", pady=(6, 0))
        ttk.Button(btns, text="Add…", command=self.add_modules).grid(row=0, column=0, sticky="ew")
        ttk.Button(btns, text="Remove", command=self.remove_module).grid(row=1, column=0, pady=3)
        row += 1

        ttk.Label(frame, text="Output").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frame, textvariable=self.output_path).grid(
            row=row, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(frame, text="Save as…", command=self.pick_output).grid(
            row=row, column=2, pady=(6, 0))
        row += 1

        opts = ttk.Frame(frame)
        opts.grid(row=row, column=1, columnspan=2, sticky="w", padx=6, pady=(6, 0))
        # Each of these turns off a check for something the kernel will refuse at
        # boot. They are worded to say what happens, not what is permitted.
        ttk.Checkbutton(opts, text="Allow vermagic mismatch (module will not load)",
                        variable=self.allow_mismatch).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Allow unsigned modules (a signing kernel will refuse them)",
                        variable=self.allow_unsigned).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Allow missing dependencies (insmod will fail on them)",
                        variable=self.allow_missing_deps).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Keep xattrs", variable=self.keep_xattrs).grid(
            row=3, column=0, sticky="w")
        row += 1

        self.patch_button = ttk.Button(frame, text="Patch", command=self.start_patch)
        self.patch_button.grid(row=row, column=0, sticky="w", pady=(10, 0))
        self._job_buttons.append(self.patch_button)
        return frame

    def _build_usb_section(self) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(self, text=" 2 · Write bootable USB ", padding=10)
        frame.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(
            frame,
            text=("Writes the whole boot tree, substituting the patched initrd. UEFI boot only. "
                  "The selected device is erased completely."),
            wraplength=860, foreground=MUTED,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1

        ttk.Label(frame, text="Target device").grid(row=row, column=0, sticky="w")
        self.device_combo = ttk.Combobox(frame, textvariable=self.device_choice, state="readonly")
        self.device_combo.grid(row=row, column=1, sticky="ew", padx=6)
        ttk.Button(frame, text="Refresh", command=self.refresh_devices).grid(row=row, column=2)
        row += 1

        ttk.Label(frame, text="Volume label").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frame, textvariable=self.volume_label, width=16).grid(
            row=row, column=1, sticky="w", padx=6, pady=(6, 0))
        row += 1

        self.write_button = ttk.Button(frame, text="Write to USB…", command=self.start_write)
        self.write_button.grid(row=row, column=0, sticky="w", pady=(10, 0))
        self._job_buttons.append(self.write_button)
        return frame

    # -------------------------------------------------------------- helpers

    def _append(self, level: str, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", level)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _profile(self):
        return PROFILES[self.profile_key.get()]

    def _set_profile_from_label(self, label: str):
        for key, prof in PROFILES.items():
            if prof.label == label:
                self.profile_key.set(key)
                self._rescan()
                return

    def _busy(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    # ---------------------------------------------------------------- events

    def pick_dir(self):
        path = filedialog.askdirectory(title="Select the OS image directory")
        if path:
            self.image_dir.set(path)
            self._rescan()

    def _rescan(self):
        directory = self.image_dir.get().strip()
        if not directory:
            return
        base = Path(directory)
        if not base.is_dir():
            self._append("error", f"not a directory: {base}")
            return
        self.images = patcher.find_images(base, self._profile())
        if not self.images:
            self.image_combo["values"] = []
            self.selected_image.set("")
            self.image_info.configure(text="no SquashFS image found here")
            self._append("warn", f"No SquashFS image found under {base}")
            return
        self.image_combo["values"] = [str(p.relative_to(base)) for p in self.images]
        self.selected_image.set(str(self.images[0].relative_to(base)))
        self._append("ok", f"Found {len(self.images)} image(s) under {base}")
        self._on_image_change()

    def _current_image(self) -> Path | None:
        rel = self.selected_image.get()
        return Path(self.image_dir.get()) / rel if rel else None

    def _on_image_change(self):
        image = self._current_image()
        if not image or not image.is_file():
            return
        try:
            un, _ = patcher.require_tools()
            info = patcher.probe_squashfs(un, image)
            self.image_info.configure(
                text=(f"SquashFS {info['version']} · {info['compression']} · "
                      f"block {info['block_size']} · {image.stat().st_size:,} bytes"))
        except PatchError as exc:
            self.image_info.configure(text=str(exc))
        self.output_path.set(str(image.with_name(image.name + ".patched")))

    def add_modules(self):
        for raw in filedialog.askopenfilenames(
                title="Select kernel modules",
                filetypes=[("Kernel modules", "*.ko"), ("All files", "*")]):
            path = Path(raw)
            if path in self.modules:
                continue
            self.modules.append(path)
            info = kmod.read(path)
            if info.error:
                self._append("error", f"{path.name}: {info.error}")
            self.module_list.insert("end", f"{path.name}   [{info.vermagic or 'no vermagic'}]")
            self._append("info", f"Added {path}")
            self._append("info", f"    vermagic = {info.vermagic or '(none)'!r}")
            self._append("info", f"    {'signed' if info.signed else 'not signed'}")
            if info.depends:
                self._append("warn", f"    depends on: {', '.join(info.depends)} — those must "
                                     "already be in the image, or be injected too")
            if info.firmware:
                self._append("warn", f"    needs firmware: {', '.join(info.firmware)}")

    def remove_module(self):
        for index in reversed(list(self.module_list.curselection())):
            self.module_list.delete(index)
            del self.modules[index]

    def pick_output(self):
        path = filedialog.asksaveasfilename(title="Write patched image as", defaultextension="")
        if path:
            self.output_path.set(path)

    def save_log(self):
        path = filedialog.asksaveasfilename(title="Save log", defaultextension=".log")
        if path:
            Path(path).write_text(self.log.get("1.0", "end"))
            self.status.set(f"Log saved to {path}")

    def refresh_devices(self, quiet: bool = False):
        try:
            self.devices = usbwriter.list_devices()
        except USBError as exc:
            self.devices = []
            if not quiet:
                self._append("error", str(exc))
        self.device_combo["values"] = [d.label for d in self.devices]
        if self.devices:
            self.device_choice.set(self.devices[0].label)
            if not quiet:
                self._append("ok", f"Found {len(self.devices)} removable device(s)")
        else:
            self.device_choice.set("")
            if not quiet:
                self._append("warn", "No removable devices found. Insert a USB drive and Refresh.")

    def _selected_device(self) -> usbwriter.Device | None:
        for device in self.devices:
            if device.label == self.device_choice.get():
                return device
        return None

    # ----------------------------------------------------------- the work

    def _run_job(self, factory, title: str):
        for button in self._job_buttons:
            button.state(["disabled"])
        self.progress["value"] = 0
        self.status.set(f"{title}…")
        self._append("step", "")
        self._append("step", f"=== {title} ===")

        def run():
            try:
                for frac, level, message in factory():
                    self.queue.put(("log", frac, level, message))
                self.queue.put(("done", 1.0, "ok", "Finished."))
            except (PatchError, USBError) as exc:
                self.queue.put(("fail", 0.0, "error", str(exc)))
            except Exception:
                self.queue.put(("fail", 0.0, "error", traceback.format_exc()))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    # ------------------------------------------------------------- inspection

    def _inspect_target(self) -> Path | None:
        """The image to inspect, or None with the user already told why not."""
        if self._busy():
            return None
        image = self._current_image()
        if not image or not image.is_file():
            messagebox.showerror(APP_TITLE, "Select an OS image directory first.")
            return None
        return image

    def _start_inspection(self, image: Path, title: str, **kwargs):
        request = patcher.InspectRequest(image=image, profile=self._profile(), **kwargs)
        self._run_job(lambda: patcher.inspect(request), title)

    def run_report(self):
        image = self._inspect_target()
        if image:
            self._start_inspection(image, f"Inspect {image.name}", report=True)

    def run_hardware(self):
        image = self._inspect_target()
        if not image:
            return
        answer = HardwareDialog.ask(self.master, self._hardware_text, self._alias_db)
        if answer is None:
            return
        text, alias_db = answer
        self._hardware_text = text
        self._alias_db = str(alias_db) if alias_db else ""
        self._start_inspection(image, "Hardware coverage",
                               hardware_text=text, alias_db=alias_db)

    def run_scan(self):
        image = self._inspect_target()
        if not image:
            return
        # A directory rather than a file: the scan walks it and opens any driver
        # archives it finds inside, so pointing at a Downloads folder works
        # whether the driver arrived loose or in a tarball.
        path = filedialog.askdirectory(
            title="Select a folder of modules, or of driver archives to look inside")
        if not path:
            return
        self._start_inspection(image, f"Find modules in {Path(path).name}",
                               scan_paths=[Path(path)])

    def run_find_drivers(self):
        image = self._inspect_target()
        if not image:
            return
        # The lookup itself is metadata only, so it runs without asking. Actually
        # downloading tens of megabytes is a separate question, asked separately —
        # and only offered once, before anything has been fetched.
        if messagebox.askyesno(
                APP_TITLE,
                "Look up whether this is a stock distro kernel, and which packages "
                "hold matching modules?\n\n"
                "This contacts the distribution's archive over the network.\n\n"
                "Answer No to look up only; answer Yes to also choose a folder and "
                "download what it finds."):
            folder = filedialog.askdirectory(title="Where should the packages be downloaded?")
            if not folder:
                return
            self._start_inspection(image, "Find drivers online",
                                   find_drivers=True, fetch_drivers=Path(folder))
        else:
            self._start_inspection(image, "Find drivers online", find_drivers=True)

    def run_build_spec(self):
        image = self._inspect_target()
        if not image:
            return
        path = filedialog.askdirectory(title="Where should the build spec be written?")
        if not path:
            return
        self._start_inspection(image, "Build spec", report=False, build_spec=Path(path))

    # ------------------------------------------------------------- the stages

    def start_patch(self):
        if self._busy():
            return
        image = self._current_image()
        if not image:
            messagebox.showerror(APP_TITLE, "Select an OS image directory first.")
            return
        if not self.modules:
            messagebox.showerror(APP_TITLE, "Add at least one kernel module (.ko) to inject.")
            return
        output = Path(self.output_path.get().strip())
        if output.exists():
            if not messagebox.askyesno(APP_TITLE, f"{output.name} exists. Delete and rebuild it?"):
                return
            output.unlink()

        request = PatchRequest(
            image=image, modules=list(self.modules), output=output, profile=self._profile(),
            allow_vermagic_mismatch=self.allow_mismatch.get(), keep_xattrs=self.keep_xattrs.get(),
            allow_unsigned=self.allow_unsigned.get(),
            allow_missing_deps=self.allow_missing_deps.get())
        self.last_output = output
        self._run_job(lambda: patcher.patch(request), f"Patch {image.name}")

    def start_write(self):
        if self._busy():
            return
        device = self._selected_device()
        if not device:
            messagebox.showerror(APP_TITLE, "Select a target device. Insert a USB drive and Refresh.")
            return
        base = self.image_dir.get().strip()
        if not base:
            messagebox.showerror(APP_TITLE, "Select an OS image directory first.")
            return

        replacements: dict[str, Path] = {}
        image = self._current_image()
        patched = Path(self.output_path.get().strip()) if self.output_path.get().strip() else None
        if image and patched and patched.is_file():
            rel = image.relative_to(Path(base)).as_posix()
            replacements[rel] = patched
        else:
            if not messagebox.askyesno(
                    APP_TITLE,
                    "No patched image found at the Output path.\n\n"
                    "Write the tree unmodified, exactly as it is on disk?"):
                return

        try:
            usbwriter.assert_safe(device)
        except USBError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        detail = (f"Device:   {device.path}\n"
                  f"Model:    {device.name or 'unknown'}\n"
                  f"Size:     {device.size_h}\n"
                  f"Bus:      {device.bus or 'unknown'}\n")
        if device.mountpoints:
            detail += f"Mounted:  {', '.join(device.mountpoints)}\n"
        if replacements:
            detail += "\nSubstituting: " + ", ".join(replacements) + "\n"

        typed = simpledialog.askstring(
            "Confirm destructive write",
            f"{detail}\nEVERYTHING ON THIS DEVICE WILL BE ERASED.\n\n"
            f"Type the device name ({device.node}) to confirm:",
            parent=self)
        if typed is None:
            return
        if typed.strip() != device.node:
            messagebox.showinfo(APP_TITLE, "Device name did not match. Nothing was written.")
            return

        label = self.volume_label.get().strip() or "BOOT"
        self._run_job(
            lambda: usbwriter.write_bootable(device, Path(base), replacements, volume_label=label),
            f"Write {device.node}")

    def _poll_queue(self):
        try:
            while True:
                kind, frac, level, message = self.queue.get_nowait()
                if message:
                    self._append(level, message)
                self.progress["value"] = frac * 100
                if kind in ("done", "fail"):
                    self.status.set("Done." if kind == "done" else "Failed — see log.")
                    if kind == "fail":
                        self.progress["value"] = 0
                    for button in self._job_buttons:
                        button.state(["!disabled"])
        except queue.Empty:
            pass
        self.after(50, self._poll_queue)


def main():
    diag.init(app="system-graft", env_prefix="SYSTEM_GRAFT", version=__version__)
    root = tk.Tk()
    # Tk swallows exceptions raised inside callbacks; without this a fault in
    # any button handler never reaches the crash handler.
    diag.install_tk_excepthook(root)
    root.title(APP_TITLE)
    # Fixed content below the log measures ~725px now that stage 1 carries the
    # inspection controls, so the floor is set above that with room for a few log
    # lines — below it Tk starts clipping the controls rather than the log.
    root.geometry("940x900")
    root.minsize(820, 800)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
