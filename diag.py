"""Logging and crash diagnostics.

Three artefacts, because a failure on site needs different things at different
moments:

1. A **rotating human-readable log**, so an operator can see what happened.
2. A **machine-readable crash report** written when the program dies, carrying
   the build identity, the platform, the redacted config, the last few hundred
   log lines and a traceback — enough to diagnose without a reproduction.
3. A **single-file diagnostics bundle** on demand, so "send me your
   diagnostics" is one instruction.

Self-contained and stdlib-only so it can be copied into the other Python repos
unchanged.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

#: Identifies the document shape to anything reading it later.
SCHEMA = "stoatworks.diagnostics/1"

#: Lines held in memory for crash reports.
RING_CAPACITY = 500
#: Days of rotated logs kept on disk.
KEEP_LOG_FILES = 7

#: Bundle caps, so one runaway log cannot make a bundle unusable.
MAX_LOG_FILES = 3
MAX_LINES_PER_FILE = 5_000
MAX_CRASH_REPORTS = 5

_SENSITIVE = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "credential",
    "auth",
    "private",
)

log = logging.getLogger("system_graft")

_state: _State | None = None


class _State:
    def __init__(self, app: str, version: str, directory: Path, ring: _Ring) -> None:
        self.app = app
        self.version = version
        self.dir = directory
        self.ring = ring
        self.git_rev = _git_rev()
        self.started_at = _now_iso()
        self.started_monotonic = time.monotonic()
        self.config: object = None


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def log_dir(app: str, env_prefix: str) -> Path:
    """Where logs, crash reports and bundles live.

    Platform convention rather than a directory next to the script: the app is
    often run from a read-only location, and a log that cannot be written is
    worse than no log because nobody finds out until they need it.

    ``{PREFIX}_LOG_DIR`` overrides it.
    """
    override = os.environ.get(f"{env_prefix}_LOG_DIR")
    if override:
        return Path(override)

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / app
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / app / "logs"
    # XDG puts logs under state, not cache: a cache directory may be cleared at
    # any time, and the point of a crash report is to outlive the crash.
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / app / "logs"


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def redact(value, _seen=None):
    """Replace values whose key looks like a secret, at any depth.

    Deliberately over-eager: a redacted port number costs nothing, a token left
    in a file that gets forwarded to a mailing list costs a great deal.
    """
    if _seen is None:
        _seen = set()
    if isinstance(value, dict):
        if id(value) in _seen:
            return "<circular>"
        _seen.add(id(value))
        return {
            key: "<redacted>" if _is_sensitive(str(key)) else redact(item, _seen)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        if id(value) in _seen:
            return "<circular>"
        _seen.add(id(value))
        return [redact(item, _seen) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _is_sensitive(key: str) -> bool:
    flat = key.lower().replace("-", "").replace("_", "")
    return any(word.replace("_", "") in flat for word in _SENSITIVE)


# --------------------------------------------------------------------------
# In-memory ring
# --------------------------------------------------------------------------


class _Formatter(logging.Formatter):
    """Use the same level names as the Rust and Node repos.

    Python says ``WARNING`` and ``CRITICAL`` where the rest of the fleet says
    ``WARN`` and ``FATAL``. Aligning them means one ``grep WARN`` works across
    every log in a bundle, whatever produced it.
    """

    _NAMES = {"WARNING": "WARN", "CRITICAL": "FATAL"}

    def format(self, record: logging.LogRecord) -> str:
        record.levelname = self._NAMES.get(record.levelname, record.levelname)
        return super().format(record)


class _Ring(logging.Handler):
    """Keeps the most recent formatted log lines in memory.

    A crash report is only useful if it says what the program was doing on the
    way down, and re-reading the log file is not reliable at that moment.
    """

    def __init__(self, capacity: int) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never raise
            pass

    def snapshot(self) -> list[str]:
        return list(self.lines)


# --------------------------------------------------------------------------
# Init
# --------------------------------------------------------------------------


def init(
    app: str,
    env_prefix: str,
    version: str,
    default_level: str = "INFO",
    config: object = None,
) -> logging.Logger:
    """Install logging and the crash handlers.

    Call once, as early as possible — before anything that can fail, so a
    failure during startup is logged and captured like any other.
    """
    global _state
    if _state is not None:
        raise RuntimeError("diag.init called twice")

    directory = log_dir(app, env_prefix)
    directory.mkdir(parents=True, exist_ok=True)

    level = os.environ.get(f"{env_prefix}_LOG", default_level).upper()
    formatter = _Formatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    file_handler = logging.handlers.TimedRotatingFileHandler(
        directory / f"{app}.log",
        when="midnight",
        backupCount=KEEP_LOG_FILES,
        encoding="utf-8",
        delay=False,
    )
    # Name rotated files `<app>.YYYY-MM-DD.log` rather than the stdlib's
    # `<app>.log.YYYY-MM-DD`, so they match the other repos and so a file
    # manager still recognises them as logs.
    file_handler.namer = lambda name: _rename_rotated(name, app)
    file_handler.setFormatter(formatter)

    # Console output goes to stderr, never stdout: anything on stdout is
    # program output.
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)

    ring = _Ring(RING_CAPACITY)
    ring.setFormatter(formatter)

    log.setLevel(level)
    log.handlers.clear()
    for handler in (file_handler, stream_handler, ring):
        log.addHandler(handler)
    log.propagate = False

    _state = _State(app, version, directory, ring)
    if config is not None:
        set_config(config)

    _install_excepthooks()

    log.info(
        "logging started version=%s git_rev=%s log_dir=%s level=%s",
        version,
        _state.git_rev,
        directory,
        level,
    )
    return log


def _rename_rotated(default_name: str, app: str) -> str:
    # default_name looks like `/path/<app>.log.2026-07-28`
    path = Path(default_name)
    if path.suffix.startswith(".") and path.name.count(".") >= 2:
        stamp = path.suffix.lstrip(".")
        return str(path.with_name(f"{app}.{stamp}.log"))
    return default_name


def set_config(config: object) -> None:
    """Attach the effective configuration to crash reports and bundles.

    Separate from :func:`init` so logging can be up *before* the config is
    read — otherwise a fault while parsing it happens before there is anywhere
    to record it. Secret-looking keys are redacted here.
    """
    _required().config = redact(config)


def log_directory() -> Path:
    """The directory logs, crash reports and bundles are written to."""
    return _required().dir


def _required() -> _State:
    if _state is None:
        raise RuntimeError("diag.init has not been called")
    return _state


# --------------------------------------------------------------------------
# Crash reports
# --------------------------------------------------------------------------


def _install_excepthooks() -> None:
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        write_crash_report("uncaught-exception", exc_type, exc, tb)
        previous(exc_type, exc, tb)

    sys.excepthook = hook

    # Exceptions in worker threads never reach sys.excepthook.
    def thread_hook(args):
        write_crash_report(
            f"thread-exception:{args.thread.name if args.thread else '?'}",
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
        )

    threading.excepthook = thread_hook


def install_tk_excepthook(widget) -> None:
    """Route exceptions raised inside Tk callbacks into a crash report.

    Tkinter catches exceptions thrown by callbacks itself and sends them to
    ``Tk.report_callback_exception``, which prints to stderr and carries on.
    They never reach ``sys.excepthook``, so without this every fault in a
    button handler — which is most of them in a GUI — would be invisible to
    the diagnostics.
    """

    def report(exc_type, exc, tb):
        path = write_crash_report("tk-callback-exception", exc_type, exc, tb)
        try:
            from tkinter import messagebox

            messagebox.showerror(
                "Something went wrong",
                f"{exc}\n\nA diagnostic report was written to:\n{path}\n\n"
                "Send that file with your bug report.",
            )
        except Exception:  # noqa: BLE001 - the dialog is a courtesy, not a duty
            pass

    widget.report_callback_exception = report


def write_crash_report(trigger: str, exc_type, exc, tb) -> Path | None:
    """Write a crash report and return its path."""
    state = _state
    if state is None:
        traceback.print_exception(exc_type, exc, tb)
        return None

    report = {
        "schema": SCHEMA,
        "kind": "crash-report",
        "generated_at": _now_iso(),
        "trigger": trigger,
        "app": _app_info(state),
        "platform": _platform_info(),
        "process": _process_info(state),
        "config": state.config,
        "error": {
            "type": getattr(exc_type, "__name__", str(exc_type)),
            "message": str(exc),
            "traceback": [
                line.rstrip("\n")
                for line in traceback.format_exception(exc_type, exc, tb)
            ],
        },
        "recent_log": state.ring.snapshot(),
    }

    name = f"{state.app}-crash-{_stamp_compact()}.json"
    path = _write_json(state.dir, name, report)
    if path is not None:
        sys.stderr.write(
            f"\n{state.app} crashed ({trigger}). A diagnostic report was written to:\n"
            f"  {path}\nSend that file with your bug report — it contains the build, the "
            "configuration (secrets removed), the last log lines and a traceback.\n\n"
        )
    return path


# --------------------------------------------------------------------------
# Bundle
# --------------------------------------------------------------------------


def collect_diagnostics() -> Path:
    """Assemble a single-file diagnostics bundle and return its path."""
    state = _required()
    warnings: list[str] = []
    entries = _newest_first(state.dir, warnings)

    crash_reports = []
    for path in [p for p in entries if "-crash-" in p.name and p.suffix == ".json"][
        :MAX_CRASH_REPORTS
    ]:
        try:
            crash_reports.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception as err:  # noqa: BLE001
            warnings.append(f"{path.name}: {err}")

    logs = []
    for path in [p for p in entries if p.suffix == ".log"][:MAX_LOG_FILES]:
        try:
            all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            logs.append(
                {
                    "file": path.name,
                    "bytes": path.stat().st_size,
                    "truncated": len(all_lines) > MAX_LINES_PER_FILE,
                    # Keep the tail: whatever went wrong happened at the end.
                    "lines": all_lines[-MAX_LINES_PER_FILE:],
                }
            )
        except Exception as err:  # noqa: BLE001
            warnings.append(f"{path.name}: {err}")

    bundle = {
        "schema": SCHEMA,
        "kind": "diagnostics-bundle",
        "generated_at": _now_iso(),
        "app": _app_info(state),
        "platform": _platform_info(),
        "process": _process_info(state),
        "config": state.config,
        "log_dir": str(state.dir),
        "crash_reports": crash_reports,
        "logs": logs,
        "recent_log": state.ring.snapshot(),
        "collection_warnings": warnings,
    }

    path = _write_json(
        state.dir, f"{state.app}-diagnostics-{_stamp_compact()}.json", bundle
    )
    if path is None:
        raise OSError(f"could not write a diagnostics bundle into {state.dir}")
    return path


def _newest_first(directory: Path, warnings: list[str]) -> list[Path]:
    try:
        return sorted(
            (p for p in directory.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError as err:
        warnings.append(f"could not read {directory}: {err}")
        return []


# --------------------------------------------------------------------------
# Shared pieces
# --------------------------------------------------------------------------


def _app_info(state: _State) -> dict:
    return {"name": state.app, "version": state.version, "git_rev": state.git_rev}


def _platform_info() -> dict:
    return {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "python": sys.version.split()[0],
        "hostname": socket.gethostname(),
        "cpus": os.cpu_count() or 0,
        # Tk version matters: a GUI fault is often a Tk build difference
        # between machines rather than anything in this code.
        "tk": _tk_version(),
    }


def _tk_version() -> str:
    try:
        import tkinter

        return str(tkinter.TkVersion)
    except Exception:  # noqa: BLE001
        return "unavailable"


def _process_info(state: _State) -> dict:
    info = {
        "pid": os.getpid(),
        "argv": list(sys.argv),
        "started_at": state.started_at,
        "uptime_seconds": round(time.monotonic() - state.started_monotonic),
    }
    try:
        import resource

        # ru_maxrss is bytes on macOS, kilobytes on Linux. Report the unit
        # rather than guessing wrong and reading it as the other one later.
        info["max_rss"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        info["max_rss_unit"] = "bytes" if sys.platform == "darwin" else "kilobytes"
    except Exception:  # noqa: BLE001 - not available on Windows
        pass
    return info


def _git_rev() -> str:
    """Short git revision, or ``unknown``.

    Python ships as source, so unlike a compiled binary this can be read at
    runtime. Still best-effort: an installed copy has no ``.git``.
    """
    root = Path(__file__).resolve().parent
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception:  # noqa: BLE001
        return "unknown"


def _write_json(directory: Path, name: str, value: object) -> Path | None:
    """Write JSON, falling back to the temp directory.

    A report that cannot be written because the log directory vanished is the
    one case where writing somewhere unexpected beats writing nowhere.
    """
    import tempfile

    for candidate in (directory, Path(tempfile.gettempdir())):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            path = candidate / name
            path.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
            return path
        except Exception:  # noqa: BLE001
            continue
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _stamp_compact() -> str:
    """``20260729T141500Z`` — safe in a filename on Windows, where ``:`` is not."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
