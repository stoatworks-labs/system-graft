#!/usr/bin/env python3
"""
Tkinter front end.

Two stages, sharing one progress bar and one log:
  1. Patch    — inject modules into the initrd, write a new image file.
  2. Write    — put the boot tree (with the patched initrd substituted) onto
                removable media as a UEFI-bootable volume.

All work runs on a worker thread; the UI only ever drains a queue.
"""

from __future__ import annotations

import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import patcher
import usbwriter
from _version import __version__
from patcher import PatchError, PatchRequest, PROFILES
from usbwriter import USBError

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

        logframe = ttk.Frame(self)
        logframe.grid(row=4, column=0, sticky="nsew")
        logframe.columnconfigure(0, weight=1)
        logframe.rowconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)
        self.log = tk.Text(logframe, height=16, wrap="none", background=BG, foreground=FG,
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
        ttk.Checkbutton(opts, text="Allow vermagic mismatch (module will not load)",
                        variable=self.allow_mismatch).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Keep xattrs", variable=self.keep_xattrs).grid(
            row=1, column=0, sticky="w")
        row += 1

        self.patch_button = ttk.Button(frame, text="Patch", command=self.start_patch)
        self.patch_button.grid(row=row, column=0, sticky="w", pady=(10, 0))
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
            info = patcher.read_modinfo(path)
            self.module_list.insert("end", f"{path.name}   [{info.get('vermagic', 'no vermagic')}]")
            self._append("info", f"Added {path}")
            self._append("info", f"    vermagic = {info.get('vermagic', '(none)')!r}")
            if info.get("depends"):
                self._append("warn", f"    depends on: {info['depends']} — those must already "
                                     "be in the image")

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

    def _run_job(self, factory, button: ttk.Button, title: str):
        self.patch_button.state(["disabled"])
        self.write_button.state(["disabled"])
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
            allow_vermagic_mismatch=self.allow_mismatch.get(), keep_xattrs=self.keep_xattrs.get())
        self.last_output = output
        self._run_job(lambda: patcher.patch(request), self.patch_button, f"Patch {image.name}")

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
            self.write_button, f"Write {device.node}")

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
                    self.patch_button.state(["!disabled"])
                    self.write_button.state(["!disabled"])
        except queue.Empty:
            pass
        self.after(50, self._poll_queue)


def main():
    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("940x860")
    root.minsize(820, 640)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
