"""The desktop app's backend: every method is callable from the page as window.pywebview.api.<name>(...).

Nothing listens on a network port. pywebview passes calls straight from the window's JavaScript to this
object. Each method returns {"ok": True, "data": ...} or {"ok": False, "error": "..."}.

pywebview exposes every public attribute to JavaScript, so internal state is kept in `_`-prefixed names.
"""
from __future__ import annotations

import functools
import json
import os
import sys
from pathlib import Path

import webview

from . import report as report_mod
from .image import ImageError, open_image
from .jobs import job_manager

# pywebview renamed its dialog constants in 5.x; support both spellings.
_FD = getattr(webview, "FileDialog", None)
OPEN_DIALOG = _FD.OPEN if _FD else webview.OPEN_DIALOG
SAVE_DIALOG = _FD.SAVE if _FD else webview.SAVE_DIALOG
IMAGE_TYPES = ("Evidence images (*.E01;*.e01;*.dd;*.raw;*.img;*.001;*.bin)", "All files (*.*)")


class ApiError(Exception):
    """An error whose message is shown to the user as-is."""


def _result(fn):
    """Wrap an API method so it always returns {"ok", "data"|"error"} instead of raising."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return {"ok": True, "data": json.loads(json.dumps(fn(self, *args, **kwargs), default=str))}
        except (ApiError, ImageError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return wrapper


def _first_path(result) -> str:
    """create_file_dialog returns a tuple, a list or a plain string depending on platform and dialog."""
    if not result:
        return ""
    path = result if isinstance(result, str) else result[0]
    return os.path.normpath(path) if path else ""


class Api:
    def __init__(self) -> None:
        self._window = None

    def _attach(self, window) -> None:
        self._window = window

    def _finished_job(self, job_id: str):
        job = job_manager.get(job_id)
        if job is None:
            raise ApiError("Job not found")
        if job.status != "done" or not job.result:
            raise ApiError(f"No triage result for this job (status: {job.status})")
        return job

    @_result
    def health(self):
        return {"ok": True, "platform": sys.platform, "cpu_count": os.cpu_count()}

    @_result
    def image_info(self, path: str):
        path = str(path or "").strip().strip('"')
        if not path:
            raise ApiError("Choose an image first")
        with open_image(path) as image:
            info = image.info()
        return info

    @_result
    def pick(self, start: str = ""):
        """Open the operating system's own file dialog; returns {"path": ""} if cancelled."""
        start_dir = start if os.path.isdir(start) else os.path.dirname(start)
        chosen = self._window.create_file_dialog(
            OPEN_DIALOG, directory=start_dir if os.path.isdir(start_dir) else "", file_types=IMAGE_TYPES,
        )
        return {"path": _first_path(chosen)}

    @_result
    def triage(self, path: str):
        path = str(path or "").strip().strip('"')
        if not path:
            raise ApiError("Choose an image first")
        if not Path(path).is_file():
            raise ApiError(f"File not found: {path}")
        if job_manager.active():
            raise ApiError("A triage is already running")
        job = job_manager.start(path)
        return {"job_id": job.id}

    @_result
    def job(self, job_id: str):
        job = job_manager.get(job_id)
        if job is None:
            raise ApiError("Job not found")
        return job.public_dict()

    @_result
    def cancel(self, job_id: str):
        if not job_manager.cancel(job_id):
            raise ApiError("Job cannot be cancelled")
        return {"cancelled": True}

    @_result
    def view_report(self, job_id: str):
        job = self._finished_job(job_id)
        webview.create_window(
            f"Quick Triage Report — {Path(job.path).name}",
            html=report_mod.generate_report_html(job),
            width=1000, height=900, background_color="#1c2023",
        )
        return {"opened": True}

    @_result
    def save_report(self, job_id: str, kind: str = "html"):
        """Ask where to save the HTML or JSON report, then write it. Returns {"path": ""} if cancelled."""
        job = self._finished_job(job_id)
        if kind == "json":
            content = json.dumps(report_mod.generate_report_json(job), indent=2, default=str)
        else:
            kind, content = "html", report_mod.generate_report_html(job)
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
