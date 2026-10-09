"""Evidence sources: a still image, a sequence of images, or a video. All are read-only.

Frames come out as float32 BGR in 0..1 and are marked read-only, so no filter can change the decoded
evidence by accident. The source file is only ever opened for reading; its MD5 and SHA-256 are computed on
a background thread when it is opened, for the report.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".jp2", ".pgm", ".ppm", ".pnm", ".exr", ".hdr"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp",
              ".flv", ".webm", ".dav", ".h264", ".264", ".265", ".hevc", ".asf", ".vob", ".mjpg", ".mjpeg"}
FRAME_CACHE_BYTES = 384 * 1024 * 1024


class MediaError(Exception):
    """The source can't be opened or read; the message is shown to the user."""


def natural_key(path: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", os.path.basename(path))]


def read_image(path: str) -> tuple[np.ndarray, dict]:
    """Decode an image file exactly as stored (no EXIF auto-rotation) → (float BGR 0..1, details)."""
    try:
        data = np.fromfile(path, np.uint8)  # imread can't open non-ASCII paths on Windows; imdecode can
    except OSError as exc:
        raise MediaError(f"Cannot read {path}: {exc}") from exc
    raw = cv2.imdecode(data, cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH)
    if raw is None:
        raise MediaError(f"Not a supported image: {os.path.basename(path)}")
    details = {"dtype": str(raw.dtype), "channels": 1 if raw.ndim == 2 else raw.shape[2]}
    if raw.dtype == np.uint8:
        img = raw.astype(np.float32) / 255
    elif raw.dtype == np.uint16:
        img = raw.astype(np.float32) / 65535
    else:
        img = np.clip(raw.astype(np.float32), 0, 1)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        img = img[..., :3].copy()
        details["alpha_dropped"] = True
    elif img.shape[2] == 2:
        img = cv2.cvtColor(img[..., 0].copy(), cv2.COLOR_GRAY2BGR)
    details["bit_depth"] = {"uint8": 8, "uint16": 16}.get(details["dtype"], 32)
    return np.ascontiguousarray(img), details


def file_hashes(paths: list[str], progress=None) -> dict:
    """MD5 and SHA-256 of each file, read in 4 MiB chunks."""
    out, total, done = {}, sum(os.path.getsize(p) for p in paths) or 1, 0
    for p in paths:
        md5, sha = hashlib.md5(), hashlib.sha256()
        with open(p, "rb") as fh:
            while chunk := fh.read(4 * 1024 * 1024):
                md5.update(chunk)
                sha.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done / total)
        out[p] = {"md5": md5.hexdigest(), "sha256": sha.hexdigest()}
    return out


def _file_entry(path: str) -> dict:
    st = os.stat(path)
    return {
        "path": os.path.abspath(path), "name": os.path.basename(path), "size": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
    }


class Source:
    kind = "image"

    def __init__(self, paths: list[str]) -> None:
        self.paths = [os.path.abspath(p) for p in paths]
        self.files = [_file_entry(p) for p in self.paths]
        self.width = self.height = 0
        self.count = 1
        self.fps = 0.0
        self.details: dict = {}
        self.notes: list[str] = []
        self.hashes: dict | None = None
        self.hash_progress = 0.0
        self.hash_error = ""
        self._hash_thread: threading.Thread | None = None

    # ---------- frames ----------
    def frame(self, index: int) -> np.ndarray:
        raise NotImplementedError

    def time_of(self, index: int) -> float | None:
        return index / self.fps if self.fps else None

    def close(self) -> None:
        pass

    # ---------- integrity ----------
    def start_hashing(self) -> None:
        def run():
            try:
                self.hashes = file_hashes(self.paths, lambda f: setattr(self, "hash_progress", f))
            except OSError as exc:
                self.hash_error = str(exc)

        self._hash_thread = threading.Thread(target=run, name="source-hash", daemon=True)
        self._hash_thread.start()

    def wait_hashes(self, timeout: float | None = None) -> dict | None:
        if self._hash_thread:
            self._hash_thread.join(timeout)
        return self.hashes

    def info(self) -> dict:
        files = [{**f, **(self.hashes or {}).get(f["path"], {})} for f in self.files]
        return {
            "kind": self.kind, "path": self.paths[0], "name": os.path.basename(self.paths[0]),
            "files": files if len(files) <= 200 else files[:200], "file_count": len(files),
            "width": self.width, "height": self.height, "count": self.count, "fps": self.fps,
            "duration": self.count / self.fps if self.fps else None,
            "details": self.details, "notes": self.notes,
            "hashed": self.hashes is not None, "hash_progress": self.hash_progress, "hash_error": self.hash_error,
            "total_size": sum(f["size"] for f in self.files),
        }


class ImageSource(Source):
    kind = "image"

    def __init__(self, path: str) -> None:
        super().__init__([path])
        img, self.details = read_image(self.paths[0])
        img.flags.writeable = False
        self._img = img
        self.height, self.width = img.shape[:2]
        if self.details.get("alpha_dropped"):
            self.notes.append("The alpha (transparency) channel is ignored.")
        self.notes.append("Pixels are shown as stored: EXIF orientation is not applied.")

    def frame(self, index: int) -> np.ndarray:
        return self._img


class SequenceSource(Source):
    """Several still images treated as the frames of one clip (e.g. photos of the same object)."""
    kind = "sequence"

    def __init__(self, paths: list[str]) -> None:
        super().__init__(sorted(paths, key=natural_key))
        self.count = len(self.paths)
        first, self.details = read_image(self.paths[0])
        self.height, self.width = first.shape[:2]
        self._cache: OrderedDict[int, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()
        self.details["frames_from"] = "files in natural name order"

    def frame(self, index: int) -> np.ndarray:
        index = min(max(index, 0), self.count - 1)
        with self._lock:
            if index in self._cache:
                self._cache.move_to_end(index)
                return self._cache[index]
        img, _ = read_image(self.paths[index])
        if img.shape[:2] != (self.height, self.width):
            img = cv2.resize(img, (self.width, self.height), interpolation=cv2.INTER_CUBIC)
            note = f"{os.path.basename(self.paths[index])} has a different size and is resized to {self.width}×{self.height}."
            if note not in self.notes:
                self.notes.append(note)
        img.flags.writeable = False
        with self._lock:
            self._cache[index] = img
            while len(self._cache) * img.nbytes > FRAME_CACHE_BYTES and len(self._cache) > 1:
                self._cache.popitem(last=False)
        return img


class VideoSource(Source):
    kind = "video"

    def __init__(self, path: str) -> None:
        super().__init__([path])
        self._cap = cv2.VideoCapture(self.paths[0])
        if not self._cap.isOpened():
            raise MediaError(f"Cannot decode {os.path.basename(path)}. If it is a proprietary DVR format, export or "
                             "convert it to a standard container first (e.g. ffmpeg -i in.dav -c copy out.mkv).")
        self._lock = threading.Lock()
        self._cache: OrderedDict[int, np.ndarray] = OrderedDict()
        self._next = 0  # frame number the decoder will return next
        self.fps = float(self._cap.get(cv2.CAP_PROP_FPS) or 0)
        if not (0 < self.fps < 1000):
            self.fps = 0.0
            self.notes.append("The container has no usable frame rate; times are not shown.")
        reported = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fourcc = int(self._cap.get(cv2.CAP_PROP_FOURCC) or 0)
        codec = "".join(chr((fourcc >> 8 * i) & 0xFF) for i in range(4)).strip("\x00 ") if fourcc else ""
        ok, first = self._cap.read()
        if not ok:
            raise MediaError(f"{os.path.basename(path)} opened but no frame could be decoded")
        self._next = 1
        self.height, self.width = first.shape[:2]
        self._store(0, first)
        self.count = reported if reported > 0 else self._count_frames()
        self.details = {"codec": codec, "reported_frames": reported, "backend": self._cap.getBackendName()}

    def _count_frames(self) -> int:
        self.notes.append("The container doesn't state its frame count; frames were counted by decoding.")
        n = self._next
        while self._cap.grab():
            n += 1
        self._seek(0)
        return max(n, 1)

    def _seek(self, index: int) -> None:
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        self._next = index

    def _store(self, index: int, bgr8: np.ndarray) -> np.ndarray:
        img = (bgr8.astype(np.float32) / 255) if bgr8.dtype == np.uint8 else bgr8.astype(np.float32) / 65535
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        img.flags.writeable = False
        self._cache[index] = img
        while len(self._cache) * img.nbytes > FRAME_CACHE_BYTES and len(self._cache) > 1:
            self._cache.popitem(last=False)
        return img

    def frame(self, index: int) -> np.ndarray:
        index = min(max(index, 0), self.count - 1)
        with self._lock:
            if index in self._cache:
                self._cache.move_to_end(index)
                return self._cache[index]
            # reading forward is far cheaper than seeking; seek only for jumps backwards or far ahead
            if not (self._next <= index <= self._next + 30):
                self._seek(index)
            img = None
            while self._next <= index:
                ok, raw = self._cap.read()
                if not ok:
                    break
                img = self._store(self._next, raw)
                self._next += 1
            if img is None or index not in self._cache:
                # past the real end (frame counts in headers can be optimistic): repeat the last decodable frame
                last = max(self._cache) if self._cache else 0
                if index > last:
                    self.count = max(last + 1, 1)
                    note = f"The video ends at frame {self.count - 1}, earlier than its header states."
                    if note not in self.notes:
                        self.notes.append(note)
                    return self._cache[last]
                raise MediaError(f"Frame {index} could not be decoded")
            return self._cache[index]

    def close(self) -> None:
        with self._lock:
            self._cap.release()


def open_source(paths: list[str] | str) -> Source:
    if isinstance(paths, str):
        paths = [paths]
    paths = [p for p in (str(p).strip().strip('"') for p in paths) if p]
    if not paths:
        raise MediaError("Choose an image or video first")
    for p in paths:
        if not os.path.isfile(p):
            raise MediaError(f"File not found: {p}")
    if len(paths) > 1:
        src: Source = SequenceSource(paths)
    else:
        ext = Path(paths[0]).suffix.lower()
        if ext in VIDEO_EXTS:
            src = VideoSource(paths[0])
        else:
            try:
                src = ImageSource(paths[0])
            except MediaError:
                if ext in IMAGE_EXTS:
                    raise
                src = VideoSource(paths[0])  # unknown extension: let the video decoder try
    src.opened_at = time.time()
    src.start_hashing()
    return src
