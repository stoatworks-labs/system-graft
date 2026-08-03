"""Stoatworks Labs - About dialog for the Tkinter tools, and --about for the CLIs.

The same six things every other Stoatworks Labs product shows: the name, the
version it is actually running, its user guide, its project page, its source,
and the four ways to fund the work - over the Stoatworks Labs mark.

This file is the MASTER, in stoatworks-backend/about/python. It is vendored into
each Python repo by ../../scripts/sync-about.py - edit it THERE and re-run the
sync, never the copies. The facts come from about_data.py beside it, which is
generated from the website's projects.json.

------------------------------------------------------------------- using it

A GUI tool, from a Help menu:

    from .about_dialog import show_about
    helpmenu.add_command(label="About WSM-WWB Bridge", command=lambda: show_about(self.master))

A command-line tool, from its argument parser:

    from .about_dialog import about_text
    parser.add_argument("--about", action="store_true", help="show version and links")
    ...
    if args.about:
        print(about_text())
        return 0

--------------------------------------------------------------- the version

Pass the repo's own `__version__` - it is the thing the release is cut from,
and it is right whether the tool is installed, run from a checkout or frozen
into a one-file bundle:

    show_about(self.master, version=__version__)
    print(about_text(version=__version__))

Without it, `version()` falls back to the installed distribution's metadata and
then to VERSION_FALLBACK in about_data.py. Both are copies that go stale: an
old editable install of resolve-configurator reported 0.1.0 at the v0.1.2 tag,
which is exactly the kind of wrong the About window must not be.

------------------------------------------------------------------- the mark

`stoat_mark_png()` returns the PNG bytes, decoded from the base64 in
about_mark.py. Base64 in a module rather than a data file because these tools
are shipped as one-file bundles and a sibling asset is one path to get wrong in
each of them. Tkinter reads PNG directly (`tk.PhotoImage(data=...)`) from Tk
8.6, which is every Python this fleet supports; on anything older the dialog
simply draws without it rather than failing.
"""

from __future__ import annotations

import base64
import webbrowser

# Relative first, absolute second: most of these repos are packages, but
# system-graft is a flat directory of scripts with no package at all, where a
# relative import raises ImportError at import time rather than at use.
try:
    from . import about_data as data
    from .about_mark import STOAT_MARK_PNG_BASE64
except ImportError:  # pragma: no cover - depends on how the tool is laid out
    import about_data as data  # type: ignore[no-redef]
    from about_mark import STOAT_MARK_PNG_BASE64  # type: ignore[no-redef]

# Fleet palette, the same values as the web dialog in about/about.js.
_BG = "#0d1b2a"
_LINE = "#223c56"
_TEXT = "#e8eef5"
_DIM = "#93a8bd"
_FAINT = "#64798f"
_ACCENT = "#4cc9f0"


def version(version: str | None = None, distribution: str | None = None) -> str:
    """The version this build actually is, as a `v`-prefixed string.

    `version` is the repo's own __version__ and wins outright. See the module
    docstring for why the other two are only fallbacks.
    """
    if version:
        return "v" + str(version).lstrip("v")

    try:
        from importlib.metadata import PackageNotFoundError, version as _installed

        return "v" + _installed(distribution or data.SLUG).lstrip("v")
    except (ImportError, PackageNotFoundError, ValueError):
        return data.VERSION_FALLBACK or ""


def stoat_mark_png() -> bytes:
    return base64.b64decode(STOAT_MARK_PNG_BASE64)


def links() -> list[tuple[str, str]]:
    """The rows to show, skipping anything this product does not have.

    A guide that has not been written, or a repo that is still private, is left
    out rather than pointed at a plausible URL that 404s.
    """
    rows = [("User guide", data.GUIDE), ("Project page", data.PAGE), ("Source on GitHub", data.REPO)]
    return [(label, url) for label, url in rows if url]


def about_text(version_string: str | None = None, distribution: str | None = None) -> str:
    """The same content as the dialog, for a terminal."""
    out = [f"{data.NAME} {version(version_string, distribution)}".rstrip()]
    if data.HOOK:
        out.append(data.HOOK)
    if data.LICENCE:
        out.append(f"{data.LICENCE} licensed")
    out.append("")

    width = max((len(label) for label, _ in links()), default=0)
    for label, url in links():
        out.append(f"  {label:<{width}}  {url}")

    out.append("")
    out.append("Support the work:")
    width = max(len(name) for name, _ in data.FUNDING)
    for name, url in data.FUNDING:
        out.append(f"  {name:<{width}}  {url}")

    out.append("")
    out.append(f"{data.ORG} - {data.TAGLINE}")
    out.append(data.HOME)
    return "\n".join(out)


def show_about(parent=None, version_string: str | None = None,
               distribution: str | None = None):
    """Open the About window over `parent`, and return it.

    Imported lazily: a CLI that only ever calls about_text() must not need
    Tkinter present, and on a headless Linux box it will not be.
    """
    import tkinter as tk

    win = tk.Toplevel(parent) if parent is not None else tk.Tk()
    win.title(f"About {data.NAME}")
    win.configure(bg=_BG)
    win.resizable(False, False)
    if parent is not None:
        win.transient(parent)

    frame = tk.Frame(win, bg=_BG, padx=26, pady=22)
    frame.pack(fill="both", expand=True)

    # The mark goes behind the text, so it is placed rather than packed and the
    # content is placed over it. A Tk that cannot read PNG simply skips it.
    try:
        mark = tk.PhotoImage(data=STOAT_MARK_PNG_BASE64)
        # Tk has no opacity, so the mark is subsampled down and drawn on the
        # background colour - it reads as a watermark by size, not by alpha.
        label = tk.Label(frame, image=mark.subsample(3, 3), bg=_BG, borderwidth=0)
        label.image = mark  # keep a reference or Tk garbage-collects it
        label.place(relx=0.5, rely=0.5, anchor="center")
    except tk.TclError:
        pass

    tk.Label(frame, text=data.NAME, bg=_BG, fg=_TEXT,
             font=("Helvetica", 20, "bold"), anchor="w").pack(fill="x")

    meta = tk.Frame(frame, bg=_BG)
    meta.pack(fill="x", pady=(6, 0))
    tk.Label(meta, text=version(version_string, distribution), bg=_BG, fg=_ACCENT,
             font=("Courier", 12)).pack(side="left")
    if data.LICENCE:
        tk.Label(meta, text=f"{data.LICENCE} licensed", bg=_BG, fg=_FAINT,
                 font=("Helvetica", 11)).pack(side="left", padx=(10, 0))

    if data.HOOK:
        tk.Label(frame, text=data.HOOK, bg=_BG, fg=_DIM,
                 font=("Helvetica", 12), anchor="w").pack(fill="x", pady=(10, 0))

    def section(title):
        tk.Label(frame, text=title, bg=_BG, fg=_FAINT, anchor="w",
                 font=("Helvetica", 9, "bold")).pack(fill="x", pady=(16, 4))

    def link(parent_widget, text, url):
        widget = tk.Label(parent_widget, text=text, bg=_BG, fg=_TEXT,
                          font=("Helvetica", 12, "underline"), cursor="hand2", anchor="w")
        widget.bind("<Button-1>", lambda _e: webbrowser.open(url))
        return widget

    if links():
        section("DOCUMENTATION")
        for label, url in links():
            link(frame, label, url).pack(fill="x", pady=1)

    section("SUPPORT THE WORK")
    chips = tk.Frame(frame, bg=_BG)
    chips.pack(fill="x")
    for name, url in data.FUNDING:
        link(chips, name, url).pack(side="left", padx=(0, 14))

    tk.Frame(frame, bg=_LINE, height=1).pack(fill="x", pady=(18, 8))
    tk.Label(frame, text=f"{data.ORG} - {data.TAGLINE}", bg=_BG, fg=_FAINT,
             font=("Helvetica", 10), anchor="w").pack(fill="x")

    win.bind("<Escape>", lambda _e: win.destroy())
    win.focus_set()
    return win
