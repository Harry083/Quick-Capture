"""The desktop app's backend: every method is callable from the page as window.pywebview.api.<name>(...).

Nothing listens on a network port. Each method returns {"ok": True, "data": ...} or {"ok": False, "error": "..."}.
pywebview exposes every public attribute to JavaScript, so internal state is kept in `_`-prefixed names.
"""
from __future__ import annotations

import functools
import json
import os
import sys
import threading
from pathlib import Path

from . import decoders
from . import report as report_mod
from .session import Case, CaseError

try:  # the GUI toolkit isn't needed to import this module (tests use the API without a window)
    import webview

    _FD = getattr(webview, "FileDialog", None)
    OPEN_DIALOG = _FD.OPEN if _FD else webview.OPEN_DIALOG
    SAVE_DIALOG = _FD.SAVE if _FD else webview.SAVE_DIALOG
except ImportError:  # pragma: no cover
    webview = None
    OPEN_DIALOG = SAVE_DIALOG = None

CASE_FIELDS = ("case_number", "evidence_number", "examiner", "description", "notes")
DB_FILE_TYPES = ("SQLite databases (*.db;*.sqlite;*.sqlite3;*.db3;*.sqlitedb;*.storedata)", "All files (*.*)")


class ApiError(Exception):
    """An error whose message is shown to the user as-is."""


def _result(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return {"ok": True, "data": json.loads(json.dumps(fn(self, *args, **kwargs), default=str))}
        except (ApiError, CaseError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return wrapper


def _first_path(result) -> str:
    if not result:
        return ""
    path = result if isinstance(result, str) else result[0]
    return os.path.normpath(path) if path else ""


class Api:
    def __init__(self) -> None:
        self._window = None
        self._case: Case | None = None
        self._lock = threading.Lock()

    def _attach(self, window) -> None:
        self._window = window

    @property
    def _c(self) -> Case:
        if self._case is None:
            raise ApiError("Open a database first")
        return self._case

    def _save_dialog(self, filename: str, kind: str) -> str:
        if self._window is None:
            raise ApiError("No window to show a save dialog in")
        start = os.path.dirname(self._case.evidence["database"].original) if self._case else ""
        chosen = self._window.create_file_dialog(
            SAVE_DIALOG, directory=start if os.path.isdir(start) else "", save_filename=filename,
            file_types=(f"{kind.upper()} file (*.{kind})", "All files (*.*)"))
        return _first_path(chosen)

    def _write(self, path: str, content, binary: bool = False) -> dict:
        if not path:
            return {"path": ""}
        evidence = {str(Path(e.original).resolve()) for e in self._case.evidence.values()} if self._case else set()
        if str(Path(path).resolve()) in evidence:
            raise ApiError("Refusing to overwrite an evidence file")
        try:
            if binary:
                Path(path).write_bytes(content)
            else:
                Path(path).write_text(content, encoding="utf-8", newline="")
        except OSError as exc:
            raise ApiError(f"Could not save: {exc}") from exc
        return {"path": path}

    def _stem(self) -> str:
        return Path(self._c.evidence["database"].original).name.replace(".", "_")

    # ---------- status ----------
    @_result
    def health(self):
        return {"platform": sys.platform, "version": report_mod.APP_VERSION, "open": self._case is not None}

    @_result
    def options(self):
        return {"timestamp_formats": {k: v[0] for k, v in decoders.TIMESTAMP_FORMATS.items()},
                "timestamp_sql": {k: v[3] for k, v in decoders.TIMESTAMP_FORMATS.items()}}

    # ---------- case ----------
    @_result
    def pick_database(self, start: str = ""):
        if self._window is None:
            raise ApiError("No window to show a file dialog in")
        start_dir = start if os.path.isdir(start) else os.path.dirname(start)
        chosen = self._window.create_file_dialog(OPEN_DIALOG, directory=start_dir if os.path.isdir(start_dir) else "",
                                                 file_types=DB_FILE_TYPES)
        return {"path": _first_path(chosen)}

    @_result
    def open_case(self, path: str, case_info: dict | None = None):
        path = str(path or "").strip().strip('"')
        if not path:
            raise ApiError("Choose a database file first")
        info = {k: str((case_info or {}).get(k, "")) for k in CASE_FIELDS}
        with self._lock:
            new = Case(path, info)
            old, self._case = self._case, new
        if old:
            old.close()
        return self._case.summary()

    @_result
    def close_case(self):
        with self._lock:
            old, self._case = self._case, None
        if old:
            old.close()
        return {"closed": True}

    @_result
    def set_case_info(self, case_info: dict):
        self._c.case_info = {k: str((case_info or {}).get(k, "")) for k in CASE_FIELDS}
        return self._c.case_info

    @_result
    def summary(self):
        return self._c.summary()

    # ---------- browse ----------
    @_result
    def objects(self, view: str = "current"):
        return {"objects": self._c.objects(view)}

    @_result
    def rows(self, view: str, table: str, offset: int = 0, limit: int = 200, order: str = "", desc: bool = False,
             search: str = ""):
        limit = max(1, min(int(limit), 2000))
        return self._c.rows(view, table, max(0, int(offset)), limit, order, bool(desc), str(search or ""))

    @_result
    def set_format(self, scope: str, column: str, fmt: str):
        self._c.set_format(scope, column, fmt)
        return {"ok": True}

    @_result
    def timestamp_readings(self, value):
        try:
            num = float(value) if "." in str(value) else int(value)
        except (TypeError, ValueError) as exc:
            raise ApiError("Not a number") from exc
        return {"readings": decoders.all_timestamp_readings(num)}

    # ---------- SQL ----------
    @_result
    def query(self, view: str, sql: str, limit: int = 5000):
        return self._c.query(view, sql, limit)

    @_result
    def cancel_query(self):
        if self._case:
            self._case.cancel()
        return {"cancelled": True}

    @_result
    def suggest_joins(self, view: str = "current"):
        return {"joins": self._c.suggest_joins(view)}

    @_result
    def save_query(self, name: str, sql: str, view: str = "current"):
        return self._c.save_query(name, sql, view)

    @_result
    def delete_query(self, name: str):
        self._c.delete_query(name)
        return {"queries": self._c.saved_queries}

    @_result
    def saved_queries(self):
        return {"queries": self._c.saved_queries}

    # ---------- recovery ----------
    @_result
    def recovered(self, filters: dict | None = None):
        f = filters or {}
        if f.get("rerun"):
            self._c.recover(force=True)
        return self._c.recovered_view(str(f.get("table") or ""), str(f.get("status") or ""),
                                      str(f.get("source") or ""), str(f.get("search") or ""),
                                      max(0, int(f.get("offset") or 0)), max(1, min(int(f.get("limit") or 500), 5000)))

    # ---------- WAL / pages ----------
    @_result
    def wal_frames(self):
        return self._c.wal_frames()

    @_result
    def page_map(self, view: str = "current"):
        return {"pages": self._c.page_map(view)}

    @_result
    def page(self, view: str, number: int, source: str = "db", frame=None):
        return self._c.page_detail(view, int(number or 0), source, None if frame in (None, "") else int(frame))

    # ---------- blobs ----------
    @_result
    def blob(self, blob_id: str):
        return self._c.blob(blob_id)

    @_result
    def save_blob(self, blob_id: str):
        data = self._c.blob_bytes(blob_id)
        info = decoders.detect_blob(data)
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
               "application/x-bplist": "plist", "application/xml": "plist", "application/pdf": "pdf",
               "application/zip": "zip", "application/gzip": "gz", "application/json": "json",
               "text/plain": "txt", "image/heic": "heic", "video/mp4": "mp4", "application/vnd.sqlite3": "sqlite",
               "audio/mp4": "m4a", "audio/amr": "amr"}.get(info["mime"], "bin")
        return self._write(self._save_dialog(f"blob-{blob_id[:10]}.{ext}", ext), data, binary=True)

    # ---------- bookmarks ----------
    @_result
    def add_tag(self, item: dict):
        return self._c.add_tag(item or {})

    @_result
    def update_tag(self, tag_id: int, label=None, note=None):
        return self._c.update_tag(int(tag_id), label, note)

    @_result
    def delete_tag(self, tag_id: int):
        self._c.delete_tag(int(tag_id))
        return {"tags": self._c.tags}

    @_result
    def tags(self):
        return {"tags": self._c.tags}

    # ---------- export / report ----------
    @_result
    def export_csv(self, kind: str, params: dict | None = None):
        p = params or {}
        c = self._c
        view = str(p.get("view") or "current")
        if kind == "table":
            table = str(p.get("table") or "")
            cols, rows = c.table_raw(view, table)
            formats = c.detect_formats(table, cols, rows)
            name = f"{self._stem()}-{table}.csv"
        elif kind == "query":
            cols, rows, _ = c.query_raw(view, str(p.get("sql") or ""))
            formats = c.detect_formats("query", cols, rows)
            name = f"{self._stem()}-query.csv"
        elif kind == "recovered":
            recs = c.recover()
            table = str(p.get("table") or "")
            status = str(p.get("status") or "")
            source = str(p.get("source") or "")
            sel = [r for r in recs if (not table or r["table"] == table) and (not status or r["status"] == status)
                   and (not source or r["source"] == source)]
            width = max((len(r["columns"]) for r in sel), default=0)
            if table and sel:
                vcols = sel[0]["columns"]
            else:
                vcols = [f"value_{i + 1}" for i in range(width)]
            cols = ["table", "status", "source", "rowid", "page", "offset", "frame", "confidence", "note", *vcols]
            rows = [[r["table"], r["status"], r["source"], r["rowid"], r["page"], r["offset"], r["frame"],
                     r["confidence"], r["note"], *r["values"], *[None] * (width - len(r["values"]))] for r in sel]
            formats = {}
            name = f"{self._stem()}-recovered{'-' + table if table else ''}.csv"
        else:
            raise ApiError(f"Unknown export: {kind}")
        return self._write(self._save_dialog(name, "csv"), report_mod.to_csv(cols, rows, formats))

    @_result
    def view_report(self):
        html = report_mod.generate_html(self._c)
        if webview is None:
            raise ApiError("No window toolkit")
        webview.create_window(f"Quick Query Report — {self._c.evidence['database'].original}", html=html,
                              width=1100, height=900, background_color="#1c2023")
        return {"opened": True}

    @_result
    def save_report(self, kind: str = "html"):
        if kind == "json":
            content = json.dumps(report_mod.generate_json(self._c), indent=2, default=str)
        else:
            kind, content = "html", report_mod.generate_html(self._c)
        return self._write(self._save_dialog(f"{self._stem()}-report.{kind}", kind), content)
