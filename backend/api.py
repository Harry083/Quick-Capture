"""The desktop app's backend: every method is callable from the page as window.pywebview.api.<name>(...).

Nothing listens on a network port. pywebview passes calls straight from the window's JavaScript to this
object. Each method returns {"ok": True, "data": ...} or {"ok": False, "error": "..."}, so the frontend
gets the same error text the old HTTP API sent as `detail`.

pywebview exposes every public attribute to JavaScript, so internal state is kept in `_`-prefixed names.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shutil
import sys
from pathlib import Path

import webview

from . import devices, file_browser, imager
from . import report as report_mod
from . import scan as scan_mod
from . import triage_report
from .ewf import COMPRESSION_LEVELS
from .image import ImageError, open_image
from .jobs import job_manager
from .triage_jobs import triage_jobs

BLOCK_SIZES_MB = (1, 2, 4, 8, 16, 32)
IO_DEPTHS = (1, 2, 4, 8)
MIN_SEGMENT_MB = 16
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()\-]{0,120}$")
CASE_FIELDS = ("case_number", "evidence_number", "examiner", "description", "notes")
ACQUIRE_DEFAULTS = {
    "format": "e01",
    "hashes": ["md5", "sha1"],
    "block_size_mb": 8,
    "io_depth": 2,  # reads kept in flight at once
    "compression": "fast",
    "segment_size_mb": 0,  # 0 = no split
    "scan_mode": "quick",
    "scan_only": False,
    "verify": False,
}

# pywebview renamed its dialog constants in 5.x; support both spellings.
_FD = getattr(webview, "FileDialog", None)
OPEN_DIALOG = _FD.OPEN if _FD else webview.OPEN_DIALOG
FOLDER_DIALOG = _FD.FOLDER if _FD else webview.FOLDER_DIALOG
SAVE_DIALOG = _FD.SAVE if _FD else webview.SAVE_DIALOG
IMAGE_TYPES = ("Evidence images (*.E01;*.e01;*.dd;*.raw;*.img;*.001;*.bin)", "All files (*.*)")


class ApiError(Exception):
    """An error whose message is shown to the user as-is."""


def _result(fn):
    """Wrap an API method so it always returns {"ok", "data"|"error"} instead of raising."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            # round-trip through JSON so anything the old API serialised (paths, tuples) arrives the same way
            return {"ok": True, "data": json.loads(json.dumps(fn(self, *args, **kwargs), default=str))}
        except (ApiError, ImageError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return wrapper


def _expected_outputs(req: dict) -> list[Path]:
    base = Path(req["output_dir"]) / req["name"]
    first = {"e01": ".E01", "dd": ".001" if req["segment_size_mb"] else ".dd"}[req["format"]]
    return [base.with_name(base.name + first), base.with_name(base.name + ".txt")]


def _first_path(result) -> str:
    """create_file_dialog returns a tuple, a list or a plain string depending on platform and dialog."""
    if not result:
        return ""
    path = result if isinstance(result, str) else result[0]
    return os.path.normpath(path) if path else ""


class Api:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop  # background event loop that runs jobs (jobs.py is asyncio-based)
        self._window = None

    def _attach(self, window) -> None:
        self._window = window

    def _on_loop(self, fn, *args):
        """Run a job-manager call on the event loop thread (asyncio.Event isn't thread-safe) and wait."""

        async def call():
            return fn(*args)

        return asyncio.run_coroutine_threadsafe(call(), self._loop).result(timeout=10)

    def _finished_job(self, job_id: str):
        job = job_manager.get(job_id)
        if job is None:
            raise ApiError("Job not found")
        if job.status != "done" or not job.result:
            raise ApiError(f"No acquisition result for this job (status: {job.status})")
        return job

    # ---------- status / options ----------
    @_result
    def health(self):
        return {
            "ok": True,
            "admin": devices.is_admin(),
            "platform": sys.platform,
            "smartctl": scan_mod.find_smartctl(),
            "cpu_count": os.cpu_count(),
        }

    @_result
    def options(self):
        return {
            "scan_modes": {k: v["label"] for k, v in scan_mod.SCAN_MODES.items()},
            "block_sizes_mb": BLOCK_SIZES_MB,
            "io_depths": IO_DEPTHS,
            "compression": list(COMPRESSION_LEVELS),
            "hashes": list(imager.HASH_ALGOS),
        }

    # ---------- sources / destinations ----------
    @_result
    def devices(self):
        try:
            return {"devices": devices.list_devices()}
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"Could not list devices: {exc}") from exc

    @_result
    def source_info(self, path: str):
        try:
            return devices.source_info(path)
        except PermissionError as exc:
            raise ApiError(f"Permission denied opening {path} — run Quick Capture as Administrator/root") from exc
        except OSError as exc:
            raise ApiError(f"Cannot open {path}: {exc}") from exc

    @_result
    def folder_info(self, path: str):
        try:
            return file_browser.browse(path or None, "dir")
        except NotADirectoryError as exc:
            raise ApiError(str(exc)) from exc

    @_result
    def pick(self, mode: str = "file", start: str = ""):
        """Open the operating system's own file or folder dialog; returns {"path": ""} if cancelled.
        mode "image" is a file dialog filtered to evidence images (the Triage tab)."""
        start_dir = start if os.path.isdir(start) else os.path.dirname(start)
        kind = FOLDER_DIALOG if mode == "folder" else OPEN_DIALOG
        extra = {"file_types": IMAGE_TYPES} if mode == "image" else {}
        chosen = self._window.create_file_dialog(kind, directory=start_dir if os.path.isdir(start_dir) else "", **extra)
        return {"path": _first_path(chosen)}

    # ---------- acquisition ----------
    @_result
    def acquire(self, body: dict):
        req = {**ACQUIRE_DEFAULTS, **(body or {})}
        for key in ("source", "output_dir", "name"):
            req[key] = str(req.get(key) or "")
        if not req["source"]:
            raise ApiError("Choose a source first")
        req["case"] = {k: str((req.get("case") or {}).get(k, "")) for k in CASE_FIELDS}
        for key in ("block_size_mb", "io_depth", "segment_size_mb"):
            req[key] = int(req[key])

        if job_manager.active():
            raise ApiError("Another acquisition is already running")
        if req["format"] not in ("e01", "dd"):
            raise ApiError(f"Unknown format: {req['format']}")
        if req["scan_mode"] not in scan_mod.SCAN_MODES:
            raise ApiError(f"Unknown scan mode: {req['scan_mode']}")
        if req["block_size_mb"] not in BLOCK_SIZES_MB:
            raise ApiError(f"Block size must be one of {BLOCK_SIZES_MB} MiB")
        if req["io_depth"] not in IO_DEPTHS:
            raise ApiError(f"Read queue depth must be one of {IO_DEPTHS}")
        if req["compression"] not in COMPRESSION_LEVELS:
            raise ApiError(f"Unknown compression: {req['compression']}")
        if req["segment_size_mb"] and req["segment_size_mb"] < MIN_SEGMENT_MB:
            raise ApiError(f"Segment size must be at least {MIN_SEGMENT_MB} MiB")
        if req["scan_only"] and req["scan_mode"] == "skip":
            req["scan_mode"] = "quick"
        if not req["hashes"]:
            raise ApiError("Select at least one hash algorithm")
        unknown = set(req["hashes"]) - set(imager.HASH_ALGOS)
        if unknown:
            raise ApiError(f"Unknown hash algorithm(s): {', '.join(sorted(unknown))}")

        try:
            info = devices.source_info(req["source"])
        except PermissionError as exc:
            raise ApiError("Permission denied opening the source — run as Administrator/root") from exc
        except OSError as exc:
            raise ApiError(f"Cannot open source: {exc}") from exc

        if not req["scan_only"]:
            if not SAFE_NAME.match(req["name"]):
                raise ApiError("Image name may only contain letters, digits, spaces and . _ ( ) -")
            out_dir = Path(req["output_dir"])
            if not req["output_dir"] or not out_dir.is_dir():
                raise ApiError(f"Output folder not found: {req['output_dir']}")
            for p in _expected_outputs(req):
                if p.exists():
                    raise ApiError(f"Refusing to overwrite existing file: {p}")
            if not info["is_device"] and Path(req["source"]).resolve().parent == out_dir.resolve() \
                    and Path(req["source"]).name.startswith(req["name"] + "."):
                raise ApiError("Output would collide with the source file")
            free = shutil.disk_usage(out_dir).free
            needs_full_space = req["format"] == "dd" or req["compression"] == "none"
            if needs_full_space and free < info["size"]:
                raise ApiError(
                    f"Not enough free space: need {report_mod.fmt_bytes(info['size'])}, "
                    f"have {report_mod.fmt_bytes(free)} in {req['output_dir']}"
                )

        device = None
        try:
            device = next((d for d in devices.list_devices() if d["path"].lower() == req["source"].lower()), None)
        except Exception:  # noqa: BLE001  (device details only enrich the report / E01 header)
            pass
        if device is None:
            device = {"path": req["source"], "kind": "disk" if info["is_device"] else "file", "model": "", "serial": ""}

        options = {k: req[k] for k in ("source", "output_dir", "name", *ACQUIRE_DEFAULTS, "case")}
        job = self._on_loop(job_manager.create, options, device)
        asyncio.run_coroutine_threadsafe(job_manager.run(job.id), self._loop)
        return {"job_id": job.id}

    @_result
    def job(self, job_id: str):
        job = job_manager.get(job_id)
        if job is None:
            raise ApiError("Job not found")
        return job.public_dict()

    @_result
    def cancel(self, job_id: str):
        if not self._on_loop(job_manager.cancel, job_id):
            raise ApiError("Job cannot be cancelled")
        return {"cancelled": True}

    @_result
    def proceed(self, job_id: str):
        if not self._on_loop(job_manager.decide, job_id, True):
            raise ApiError("Job is not waiting for a decision")
        return {"proceeding": True}

    # ---------- reports ----------
    @_result
    def view_report(self, job_id: str):
        job = self._finished_job(job_id)
        webview.create_window(
            f"Quick Capture Report — {job.id}",
            html=report_mod.generate_report_html(job),
            width=1000, height=900, background_color="#1c2023",
        )
        return {"opened": True}

    @_result
    def save_report(self, job_id: str, kind: str = "html"):
        """Ask where to save the HTML or JSON report, then write it. Returns {"path": ""} if cancelled."""
        job = self._finished_job(job_id)
        if kind == "json":
            content = json.dumps(report_mod.generate_report_json(job), indent=2)
        else:
            kind, content = "html", report_mod.generate_report_html(job)
        default_dir = job.options.get("output_dir") or ""
        filename = f"{job.options.get('name') or 'quick-capture'}-report.{kind}"
        chosen = self._window.create_file_dialog(
            SAVE_DIALOG,
            directory=default_dir if os.path.isdir(default_dir) else "",
            save_filename=filename,
            file_types=(f"{kind.upper()} file (*.{kind})", "All files (*.*)"),
        )
        path = _first_path(chosen)
        if not path:
            return {"path": ""}
        try:
            Path(path).write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ApiError(f"Could not save the report: {exc}") from exc
        return {"path": path}

    # ---------- triage (the Triage tab: OS, device, users and last saved file from an image) ----------
    def _finished_triage(self, job_id: str):
        job = triage_jobs.get(job_id)
        if job is None:
            raise ApiError("Triage job not found")
        if job.status != "done" or not job.result:
            raise ApiError(f"No triage result for this job (status: {job.status})")
        return job

    @_result
    def image_info(self, path: str):
        path = str(path or "").strip().strip('"')
        if not path:
            raise ApiError("Choose an image first")
        with open_image(path) as image:
            return image.info()

    @_result
    def triage(self, path: str):
        path = str(path or "").strip().strip('"')
        if not path:
            raise ApiError("Choose an image first")
        if not Path(path).is_file():
            raise ApiError(f"File not found: {path}")
        if triage_jobs.active():
            raise ApiError("A triage is already running")
        return {"job_id": triage_jobs.start(path).id}

    @_result
    def triage_job(self, job_id: str):
        job = triage_jobs.get(job_id)
        if job is None:
            raise ApiError("Triage job not found")
        return job.public_dict()

    @_result
    def triage_cancel(self, job_id: str):
        if not triage_jobs.cancel(job_id):
            raise ApiError("Triage cannot be cancelled")
        return {"cancelled": True}

    @_result
    def triage_view_report(self, job_id: str):
        job = self._finished_triage(job_id)
        webview.create_window(
            f"Quick Capture Triage — {Path(job.path).name}",
            html=triage_report.generate_report_html(job),
            width=1000, height=900, background_color="#1c2023",
        )
        return {"opened": True}

    @_result
    def triage_save_report(self, job_id: str, kind: str = "html"):
        """Ask where to save the triage report, then write it. Returns {"path": ""} if cancelled."""
        job = self._finished_triage(job_id)
        if kind == "json":
            content = json.dumps(triage_report.generate_report_json(job), indent=2, default=str)
        else:
            kind, content = "html", triage_report.generate_report_html(job)
        src = Path(job.path)
        chosen = self._window.create_file_dialog(
            SAVE_DIALOG,
            directory=str(src.parent) if src.parent.is_dir() else "",
            save_filename=f"{src.stem}-triage.{kind}",
            file_types=(f"{kind.upper()} file (*.{kind})", "All files (*.*)"),
        )
        path = _first_path(chosen)
        if not path:
            return {"path": ""}
        try:
            Path(path).write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ApiError(f"Could not save the report: {exc}") from exc
        return {"path": path}
