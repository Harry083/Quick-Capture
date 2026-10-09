"""Writing processed results: a single frame as an image, or a frame range as a video or numbered PNGs.

Exports run on their own thread and the page polls for progress, as Quick Capture does for imaging. Every
file written is hashed (MD5 + SHA-256) and the export, including the exact chain used, is recorded for the
report. The source is never written to.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .media import file_hashes
from .pipeline import Pipeline, Step, chain_json

IMAGE_FORMATS = {".png": "PNG (lossless)", ".tif": "TIFF (lossless)", ".tiff": "TIFF (lossless)", ".jpg": "JPEG (lossy)", ".jpeg": "JPEG (lossy)", ".bmp": "BMP (lossless)"}
VIDEO_FORMATS = {
    "mkv_ffv1": {"label": "Lossless MKV (FFV1)", "ext": ".mkv", "fourcc": "FFV1", "lossless": True},
    "avi_mjpg": {"label": "AVI (Motion JPEG, high quality)", "ext": ".avi", "fourcc": "MJPG", "lossless": False},
    "mp4": {"label": "MP4 (MPEG-4 Part 2, widely playable)", "ext": ".mp4", "fourcc": "mp4v", "lossless": False},
    "png_seq": {"label": "Numbered PNG frames (lossless)", "ext": "", "fourcc": "", "lossless": True},
}


class ExportError(Exception):
    pass


class ExportCancelled(Exception):
    pass


def encode(img: np.ndarray, ext: str, bit_depth: int = 8) -> bytes:
    ext = ext.lower()
    if bit_depth == 16 and ext in (".png", ".tif", ".tiff"):
        data = (np.clip(img, 0, 1) * 65535 + 0.5).astype(np.uint16)
    else:
        data = (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8)
    params = [cv2.IMWRITE_JPEG_QUALITY, 95] if ext in (".jpg", ".jpeg") else []
    ok, buf = cv2.imencode(ext, data, params)
    if not ok:
        raise ExportError(f"Could not encode {ext} image")
    return buf.tobytes()


def write_image(path: str, img: np.ndarray, bit_depth: int = 8) -> None:
    ext = Path(path).suffix.lower()
    if ext not in IMAGE_FORMATS:
        raise ExportError(f"Unsupported image type {ext or '(none)'}: use .png, .tif, .jpg or .bmp")
    Path(path).write_bytes(encode(img, ext, bit_depth))  # imwrite can't handle non-ASCII paths on Windows


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"  # running | done | error | cancelled
    stage: str = "starting"
    percent: float = 0.0
    done_frames: int = 0
    total_frames: int = 0
    error: Optional[str] = None
    result: Optional[dict] = None
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public_dict(self) -> dict:
        elapsed = time.time() - self.started_at
        eta = elapsed / self.percent * (100 - self.percent) if 0 < self.percent < 100 else None
        return {"id": self.id, "kind": self.kind, "status": self.status, "stage": self.stage,
                "percent": self.percent, "done_frames": self.done_frames, "total_frames": self.total_frames,
                "error": self.error, "result": self.result, "elapsed": elapsed, "eta": eta}


def _record(kind: str, paths: list[str], pipeline: Pipeline, steps: list[Step], **extra) -> dict:
    hashes = file_hashes(paths)
    return {
        "kind": kind, "paths": paths, "path": paths[0] if paths else "",
        "hashes": hashes, "size": sum(os.path.getsize(p) for p in paths),
        "chain": chain_json(steps), "source": pipeline.source.paths[0],
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), **extra,
    }


def export_image(job: Job, pipeline: Pipeline, steps: list[Step], index: int, path: str, bit_depth: int) -> dict:
    job.stage, job.total_frames = "rendering", 1
    img, _ = pipeline.render(steps, index)
    job.stage = "writing"
    write_image(path, img, bit_depth)
    job.done_frames, job.percent = 1, 100.0
    lossy = Path(path).suffix.lower() in (".jpg", ".jpeg")
    return _record("image", [path], pipeline, steps, frame=index, time=pipeline.source.time_of(index),
                   width=img.shape[1], height=img.shape[0], format=IMAGE_FORMATS[Path(path).suffix.lower()],
                   bit_depth=16 if bit_depth == 16 and not lossy else 8, lossy=lossy)


def export_video(job: Job, pipeline: Pipeline, steps: list[Step], start: int, end: int, path: str, fmt: str) -> dict:
    spec = VIDEO_FORMATS[fmt]
    src = pipeline.source
    start, end = max(0, start), min(src.count - 1, end)
    if end < start:
        raise ExportError("The frame range is empty")
    job.total_frames = end - start + 1
    fps = src.fps or 25.0
    writer, size, written = None, None, []
    resized = False
    seq_dir = Path(path) if fmt == "png_seq" else None
    if seq_dir:
        seq_dir.mkdir(parents=True, exist_ok=True)
    try:
        for n, i in enumerate(range(start, end + 1)):
            if job.cancel_event.is_set():
                raise ExportCancelled()
            job.stage = f"frame {i}"
            img, results = pipeline.render(steps, i)
            failed = next((r for r in results if r.error), None)
            if failed:
                raise ExportError(f"Frame {i}: {failed.step.filter.name} failed: {failed.error}")
            if size is None:
                size = (img.shape[1], img.shape[0])
            elif (img.shape[1], img.shape[0]) != size:
                img, resized = cv2.resize(img, size, interpolation=cv2.INTER_CUBIC), True
            if seq_dir:
                p = seq_dir / f"{seq_dir.name}_{i:06d}.png"
                p.write_bytes(encode(img, ".png"))
                written.append(str(p))
            else:
                if writer is None:
                    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*spec["fourcc"]), fps, size)
                    if fmt == "avi_mjpg":
                        writer.set(cv2.VIDEOWRITER_PROP_QUALITY, 100)
                    if not writer.isOpened():
                        raise ExportError(f"This system can't write {spec['label']}; choose another format")
                writer.write((np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8))
            job.done_frames = n + 1
            job.percent = 100.0 * (n + 1) / job.total_frames
            job.updated_at = time.time()
    except BaseException:
        if writer is not None:
            writer.release()
        if not seq_dir and os.path.exists(path):
            os.remove(path)  # don't leave a half-written video that looks complete
        raise
    if writer is not None:
        writer.release()
    job.stage = "hashing"
    if seq_dir:
        manifest = seq_dir / "SHA256SUMS.txt"
        lines = [f"{hashlib.sha256(Path(p).read_bytes()).hexdigest()}  {Path(p).name}" for p in written]
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        record = _record("frames", [str(manifest)], pipeline, steps, folder=str(seq_dir), file_count=len(written))
    else:
        record = _record("video", [path], pipeline, steps)
    record.update(format=spec["label"], lossless=spec["lossless"], start=start, end=end, frames=job.total_frames,
                  fps=fps, width=size[0], height=size[1], resized=resized,
                  fps_note="" if src.fps else "The source has no frame rate; 25 fps was used.")
    return record


class ExportJobs:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self.history: list[dict] = []  # every completed export this session, for the report

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def active(self) -> Optional[Job]:
        return next((j for j in self._jobs.values() if j.status == "running"), None)

    def start(self, kind: str, fn, *args) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job, fn, args), name=f"export-{job.id}", daemon=True).start()
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status != "running":
            return False
        job.cancel_event.set()
        return True

    def _run(self, job: Job, fn, args) -> None:
        try:
            job.result = fn(job, *args)
            self.history.append(job.result)
            job.status, job.stage, job.percent = "done", "done", 100.0
        except ExportCancelled:
            job.status = job.stage = "cancelled"
        except (ExportError, OSError, ValueError) as exc:
            job.status = job.stage = "error"
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001
            job.status = job.stage = "error"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.updated_at = time.time()


export_jobs = ExportJobs()
