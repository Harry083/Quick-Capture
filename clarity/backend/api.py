"""The desktop app's backend: every method is callable from the page as window.pywebview.api.<name>(...).

As in Quick Capture, nothing listens on a network port: pywebview passes calls straight from the window's
JavaScript to this object, each on its own thread. Every method returns {"ok": True, "data": ...} or
{"ok": False, "error": "..."}.

The page owns the filter chain and sends it with each call; this object owns the open source, its render
cache, and the list of exports. pywebview exposes every public attribute, so internal state is `_`-prefixed.
"""
from __future__ import annotations

import base64
import functools
import json
import os
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from . import report as report_mod
from .export import IMAGE_FORMATS, VIDEO_FORMATS, ExportError, export_image, export_jobs, export_video
from .filters import FilterError, catalogue
from .media import IMAGE_EXTS, VIDEO_EXTS, MediaError, open_source
from .pipeline import ChainError, Pipeline, chain_json, clean_chain

PREVIEW_MAX_SIDE = 4096
PROJECT_EXT = ".clarity.json"

try:  # the tests drive this class without a window
    import webview

    _FD = getattr(webview, "FileDialog", None)
    OPEN_DIALOG = _FD.OPEN if _FD else webview.OPEN_DIALOG
    FOLDER_DIALOG = _FD.FOLDER if _FD else webview.FOLDER_DIALOG
    SAVE_DIALOG = _FD.SAVE if _FD else webview.SAVE_DIALOG
except ImportError:  # pragma: no cover
    webview = None
    OPEN_DIALOG, FOLDER_DIALOG, SAVE_DIALOG = 10, 20, 30

_media = sorted(e.lstrip(".") for e in IMAGE_EXTS | VIDEO_EXTS)
MEDIA_TYPES = (
    "Images and video (" + ";".join(f"*.{e};*.{e.upper()}" for e in _media) + ")",
    "All files (*.*)",
)
IMAGE_TYPES = ("Images (" + ";".join(f"*.{e.lstrip('.')};*.{e.lstrip('.').upper()}" for e in sorted(IMAGE_EXTS)) + ")", "All files (*.*)")
PROJECT_TYPES = ("Clarity project (*.clarity.json;*.json)", "All files (*.*)")


class ApiError(Exception):
    """An error whose message is shown to the user as-is."""


def _result(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return {"ok": True, "data": json.loads(json.dumps(fn(self, *args, **kwargs), default=str))}
        except (ApiError, MediaError, ChainError, FilterError, ExportError) as exc:
            return {"ok": False, "error": str(exc)}
        except MemoryError:
            return {"ok": False, "error": "Not enough memory: crop first or use fewer frames"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return wrapper


def _paths(result) -> list[str]:
    """create_file_dialog returns a tuple, a list or a plain string depending on platform and dialog."""
    if not result:
        return []
    items = [result] if isinstance(result, str) else list(result)
    return [os.path.normpath(p) for p in items if p]


def _jpeg_uri(img: np.ndarray, quality: int = 92) -> str:
    ok, buf = cv2.imencode(".jpg", (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ApiError("Could not encode the preview")
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def _fit(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    return cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else img


def _histogram(img: np.ndarray) -> dict:
    small = _fit(img, 512)
    u8 = (np.clip(small, 0, 1) * 255).astype(np.uint8)
    hist = lambda ch: np.bincount((ch.ravel() >> 2), minlength=64).tolist()  # noqa: E731  64 bins
    return {"r": hist(u8[..., 2]), "g": hist(u8[..., 1]), "b": hist(u8[..., 0]),
            "l": hist(cv2.cvtColor(u8, cv2.COLOR_BGR2GRAY))}


class Api:
    def __init__(self) -> None:
        self._window = None
        self._source = None
        self._pipeline: Pipeline | None = None
        self._expected: dict[str, str] = {}  # path → SHA-256 recorded in a loaded project
        self._preview_gen = 0
        self._preview_session = ""  # the page's id; a reloaded page starts counting again
        self._gen_lock = threading.Lock()

    def _attach(self, window) -> None:
        self._window = window

    def _need_source(self) -> Pipeline:
        if self._pipeline is None:
            raise ApiError("Open an image or video first")
        return self._pipeline

    def _dialog(self, kind, **kwargs) -> list[str]:
        if self._window is None:
            raise ApiError("No window to show a dialog in")
        return _paths(self._window.create_file_dialog(kind, **kwargs))

    def _start_dir(self) -> str:
        return os.path.dirname(self._source.paths[0]) if self._source else ""

    def _guard_output(self, path: str) -> None:
        if self._source and any(os.path.abspath(path) == p for p in self._source.paths):
            raise ApiError("Refusing to overwrite the source evidence")

    # ---------- status ----------
    @_result
    def health(self):
        return {
            "platform": sys.platform, "version": report_mod.APP_VERSION, "opencv": cv2.__version__,
            "numpy": np.__version__, "cpu_count": os.cpu_count(),
            "video_formats": {k: v["label"] for k, v in VIDEO_FORMATS.items()},
            "image_formats": sorted(set(IMAGE_FORMATS.values())),
        }

    @_result
    def catalogue(self):
        from .filters import CATEGORIES

        return {"categories": [list(c) for c in CATEGORIES], "filters": catalogue()}

    # ---------- source ----------
    @_result
    def pick(self, mode: str = "media"):
        """Native open dialog. mode: media (one image or video), sequence (several images)."""
        start = self._start_dir()
        if mode == "sequence":
            return {"paths": self._dialog(OPEN_DIALOG, directory=start, allow_multiple=True, file_types=IMAGE_TYPES)}
        return {"paths": self._dialog(OPEN_DIALOG, directory=start, file_types=MEDIA_TYPES)}

    @_result
    def open_source(self, paths, expected: dict | None = None):
        src = open_source(paths)
        if self._source is not None:
            self._source.close()
        self._source, self._pipeline = src, Pipeline(src)
        self._expected = {os.path.abspath(k): v for k, v in (expected or {}).items()}
        return self._info()

    def _info(self) -> dict:
        info = self._source.info()
        if self._expected and info["hashed"]:
            got = {f["path"]: f.get("sha256") for f in self._source.info()["files"]}
            info["hash_match"] = all(got.get(p) == h for p, h in self._expected.items() if p in got) and \
                any(p in got for p in self._expected)
        return info

    @_result
    def source_info(self):
        if self._source is None:
            return None
        return self._info()

    # ---------- preview ----------
    @_result
    def preview(self, req: dict):
        """Render one frame. req: chain, index, upto (render only the steps before this position), original,
        gen (request counter) and session (the page's id)."""
        pipeline = self._need_source()
        gen = int(req.get("gen") or 0)
        with self._gen_lock:
            if req.get("session", "") != self._preview_session:
                self._preview_session, self._preview_gen = req.get("session", ""), 0
            self._preview_gen = max(self._preview_gen, gen)
        steps = clean_chain(req.get("chain") or [])
        index = int(req.get("index") or 0)
        upto = req.get("upto")
        t0 = time.perf_counter()
        with pipeline._lock:
            if gen < self._preview_gen:
                return {"stale": True}  # the user has moved on; don't spend time on an old request
            img, results = pipeline.render(steps, index, None if upto is None else int(upto))
        out = {
            "gen": gen, "index": index, "width": img.shape[1], "height": img.shape[0],
            "image": _jpeg_uri(_fit(img, PREVIEW_MAX_SIDE)),
            "steps": [{"position": r.step.index, "notes": r.notes, "error": r.error, "ms": round(r.ms, 1),
                       "size": r.shape} for r in results],
            "histogram": _histogram(img), "ms": round((time.perf_counter() - t0) * 1000),
            "time": pipeline.source.time_of(index),
        }
        if req.get("original"):
            orig = pipeline.source.frame(index)
            out.update(original=_jpeg_uri(_fit(orig, PREVIEW_MAX_SIDE)), original_width=orig.shape[1], original_height=orig.shape[0])
        return out

    # ---------- export ----------
    def _stem(self) -> str:
        return Path(self._source.paths[0]).stem if self._source else "clarity"

    @_result
    def export_image(self, req: dict):
        pipeline = self._need_source()
        if export_jobs.active():
            raise ApiError("An export is already running")
        steps = clean_chain(req.get("chain") or [])
        index = int(req.get("index") or 0)
        ext = req.get("ext") if req.get("ext") in IMAGE_FORMATS else ".png"
        suffix = f"_f{index:06d}" if pipeline.source.count > 1 else ""
        chosen = self._dialog(SAVE_DIALOG, directory=self._start_dir(), save_filename=f"{self._stem()}{suffix}_enhanced{ext}",
                              file_types=(f"{IMAGE_FORMATS[ext]} (*{ext})", "All files (*.*)"))
        if not chosen:
            return {"job_id": None}
        path = chosen[0] if Path(chosen[0]).suffix else chosen[0] + ext
        self._guard_output(path)
        bit_depth = 16 if int(req.get("bit_depth") or 8) == 16 else 8
        return {"job_id": export_jobs.start("image", export_image, pipeline, steps, index, path, bit_depth).id}

    @_result
    def export_video(self, req: dict):
        pipeline = self._need_source()
        if pipeline.source.count < 2:
            raise ApiError("Video export needs a video or image sequence")
        if export_jobs.active():
            raise ApiError("An export is already running")
        steps = clean_chain(req.get("chain") or [])
        fmt = req.get("format") if req.get("format") in VIDEO_FORMATS else "mkv_ffv1"
        spec = VIDEO_FORMATS[fmt]
        start, end = int(req.get("start") or 0), int(req.get("end") if req.get("end") is not None else pipeline.source.count - 1)
        name = f"{self._stem()}_enhanced{'_frames' if fmt == 'png_seq' else spec['ext']}"
        types = ("Folder for the frames (*.*)",) if fmt == "png_seq" else (f"{spec['label']} (*{spec['ext']})", "All files (*.*)")
        chosen = self._dialog(SAVE_DIALOG, directory=self._start_dir(), save_filename=name, file_types=types)
        if not chosen:
            return {"job_id": None}
        path = chosen[0]
        if fmt != "png_seq" and Path(path).suffix.lower() != spec["ext"]:
            path += spec["ext"]
        if fmt == "png_seq" and os.path.isdir(path) and any(Path(path).iterdir()):
            raise ApiError(f"The folder {path} already has files in it; choose a new name")
        self._guard_output(path)
        return {"job_id": export_jobs.start("video", export_video, pipeline, steps, start, end, path, fmt).id}

    @_result
    def job(self, job_id: str):
        job = export_jobs.get(job_id)
        if job is None:
            raise ApiError("Export not found")
        return job.public_dict()

    @_result
    def cancel(self, job_id: str):
        if not export_jobs.cancel(job_id):
            raise ApiError("This export can't be cancelled")
        return {"cancelled": True}

    @_result
    def exports(self):
        return {"exports": export_jobs.history}

    # ---------- reports ----------
    def _report_ctx(self, req: dict, with_images: bool = True) -> dict:
        pipeline = self._need_source()
        steps = clean_chain(req.get("chain") or [])
        exports = [e for e in export_jobs.history if e["source"] == pipeline.source.paths[0]]
        return report_mod.build_context(pipeline, steps, int(req.get("index") or 0), req.get("case"),
                                        req.get("measurements"), exports, with_images)

    def _save_text(self, filename: str, kind: str, content: str) -> dict:
        chosen = self._dialog(SAVE_DIALOG, directory=self._start_dir(), save_filename=filename,
                              file_types=(f"{kind.upper()} file (*.{kind})", "All files (*.*)"))
        if not chosen:
            return {"path": ""}
        self._guard_output(chosen[0])
        try:
            Path(chosen[0]).write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ApiError(f"Could not save: {exc}") from exc
        return {"path": chosen[0]}

    @_result
    def view_report(self, req: dict):
        ctx = self._report_ctx(req)
        if webview is None:
            raise ApiError("No window system")
        webview.create_window(f"Clarity Report — {ctx['source']['name']}", html=report_mod.generate_report_html(ctx),
                              width=1100, height=900, background_color="#1c2023")
        return {"opened": True}

    @_result
    def save_report(self, req: dict, kind: str = "html"):
        if kind == "json":
            content = json.dumps(report_mod.generate_report_json(self._report_ctx(req, with_images=False)), indent=2, default=str)
        else:
            kind, content = "html", report_mod.generate_report_html(self._report_ctx(req))
        return self._save_text(f"{self._stem()}-enhancement-report.{kind}", kind, content)

    @_result
    def view_reference(self):
        if webview is None:
            raise ApiError("No window system")
        webview.create_window("Clarity — Filter reference", html=report_mod.generate_reference_html(),
                              width=1100, height=900, background_color="#1c2023")
        return {"opened": True}

    @_result
    def save_reference(self):
        return self._save_text("clarity-filter-reference.html", "html", report_mod.generate_reference_html())

    # ---------- projects ----------
    @_result
    def save_project(self, project: dict):
        self._need_source()
        src = self._source.info()
        doc = {
            "app": "Clarity", "format": 1, "saved": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": {"paths": self._source.paths, "sha256": {f["path"]: f.get("sha256") for f in src["files"] if f.get("sha256")}},
            "chain": chain_json(clean_chain(project.get("chain") or [])),
            "index": int(project.get("index") or 0),
            "case": project.get("case") or {}, "measurements": project.get("measurements") or {},
        }
        return self._save_text(f"{self._stem()}{PROJECT_EXT}", "json", json.dumps(doc, indent=2))

    @_result
    def load_project(self, path: str = ""):
        if not path:
            chosen = self._dialog(OPEN_DIALOG, directory=self._start_dir(), file_types=PROJECT_TYPES)
            if not chosen:
                return {"cancelled": True}
            path = chosen[0]
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ApiError(f"Not a readable project file: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("app") != "Clarity":
            raise ApiError("Not a Clarity project file")
        chain = chain_json(clean_chain(doc.get("chain") or []))
        paths = (doc.get("source") or {}).get("paths") or []
        missing = [p for p in paths if not os.path.isfile(p)]
        return {"path": path, "chain": chain, "index": int(doc.get("index") or 0), "case": doc.get("case") or {},
                "measurements": doc.get("measurements") or {}, "source_paths": paths, "missing": missing,
                "expected": (doc.get("source") or {}).get("sha256") or {}}
