"""The imaging pipeline, built for throughput.

    reader thread ──► queue ──► md5 thread
          │          queue ──► sha1 thread       (hashlib and zlib release the GIL, so these
          │          queue ──► sha256 thread      genuinely run in parallel on separate cores)
          └────────► queue ──► writer thread ──► DD file(s)  or  E01 writer (+ zlib thread pool)

The reader keeps several large reads in flight at once (default 2 x 8 MiB, each on its own OS handle), so
SSD/NVMe sources always have queued work and never idle between requests. Blocks are handed on strictly
in order, and the reader never waits on hashing or compression unless a bounded queue is full. Time spent
in each stage is measured, so every run reports what actually limited its speed. Bad sectors are not retried: a failed block is re-read in 64 KiB pieces,
then per sector, and anything still unreadable is zero-filled and logged (and recorded in the E01 error2
section) so the run keeps moving.
"""
from __future__ import annotations

import hashlib
import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import ewf
from .devices import RawSource

HASH_ALGOS = ("md5", "sha1", "sha256")
QUEUE_DEPTH = 8
SUB_BLOCK = 64 * 1024
# If this many consecutive 64 KiB pieces fail outright, the device has most likely dropped off the bus.
MAX_CONSECUTIVE_FAILED_PIECES = 256


class ImagingCancelled(Exception):
    pass


class ImagingError(Exception):
    pass


@dataclass
class ImagingProgress:
    stage: str
    bytes_done: int
    total: int
    speed: float  # bytes/s, recent window
    avg_speed: float
    eta: Optional[float]
    bad_sectors: int = 0

    @property
    def percent(self) -> float:
        return 100.0 * self.bytes_done / self.total if self.total else 0.0


class _Consumer(threading.Thread):
    """Drains a bounded queue of blocks into `fn`; any exception is kept and re-raised by the reader."""

    def __init__(self, name: str, fn: Callable[[bytes], None]) -> None:
        super().__init__(name=name, daemon=True)
        self.q: queue.Queue = queue.Queue(maxsize=QUEUE_DEPTH)
        self.fn = fn
        self.error: Optional[BaseException] = None
        self.busy = 0.0  # seconds spent inside fn — used to name the bottleneck

    def run(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                return
            if self.error is None:
                t0 = time.perf_counter()
                try:
                    self.fn(item)
                except BaseException as exc:  # noqa: BLE001
                    self.error = exc
                self.busy += time.perf_counter() - t0

    def put(self, item, cancel: threading.Event) -> None:
        while True:
            if self.error is not None:
                raise self.error
            if cancel.is_set():
                raise ImagingCancelled()
            try:
                self.q.put(item, timeout=0.2)
                return
            except queue.Full:
                continue


class RawWriter:
    """Plain dd output: one `.dd` file, or `.001`, `.002`, ... when a segment size is set."""

    def __init__(self, base_path: Path, segment_size: Optional[int]) -> None:
        self.base_path = base_path
        self.segment_size = segment_size
        self.paths: list[str] = []
        self._f = None
        self._in_segment = 0
        self._open_next()

    def _open_next(self) -> None:
        if self._f is not None:
            self._f.close()
        if self.segment_size:
            path = self.base_path.with_name(f"{self.base_path.name}.{len(self.paths) + 1:03d}")
        else:
            path = self.base_path.with_name(f"{self.base_path.name}.dd")
        self._f = open(path, "xb", buffering=0)  # noqa: SIM115  (never overwrite existing evidence)
        self.paths.append(str(path))
        self._in_segment = 0

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self.segment_size and self._in_segment >= self.segment_size:
                self._open_next()
            n = len(view) if not self.segment_size else min(len(view), self.segment_size - self._in_segment)
            written = 0
            while written < n:
                written += self._f.write(view[written:n])
            self._in_segment += n
            view = view[n:]

    def finalize(self) -> list[str]:
        self._f.close()
        return self.paths

    def abort(self) -> None:
        if self._f is not None:
            self._f.close()


def _merge_ranges(lbas: list[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for lba in sorted(set(lbas)):
        if ranges and ranges[-1][0] + ranges[-1][1] == lba:
            ranges[-1] = (ranges[-1][0], ranges[-1][1] + 1)
        else:
            ranges.append((lba, 1))
    return ranges


def _read_block(src: RawSource, offset: int, length: int, bad: list[int]) -> bytes:
    try:
        return src.pread(length, offset)
    except OSError:
        pass
    # Slow path, only for blocks containing an error: narrow it down, zero-fill what can't be read.
    buf = bytearray(length)
    sector = src.sector_size
    failed_run = 0
    for sub in range(0, length, SUB_BLOCK):
        n = min(SUB_BLOCK, length - sub)
        try:
            buf[sub:sub + n] = src.pread(n, offset + sub)
            failed_run = 0
            continue
        except OSError:
            pass
        any_ok = False
        for s in range(sub, sub + n, sector):
            m = min(sector, sub + n - s)
            try:
                buf[s:s + m] = src.pread(m, offset + s)
                any_ok = True
            except OSError:
                bad.append((offset + s) // sector)
        failed_run = 0 if any_ok else failed_run + 1
        if failed_run >= MAX_CONSECUTIVE_FAILED_PIECES:
            raise ImagingError(
                f"Device stopped responding around offset {offset + sub:,} — "
                f"{failed_run * SUB_BLOCK // 1024 // 1024} MiB in a row unreadable. Aborting."
            )
    return bytes(buf)


def _hash_files(paths: list[str], algos: list[str], block: int, total: int,
                on_progress: Callable[[ImagingProgress], None], cancel: threading.Event,
                reader=None) -> dict[str, str]:
    """Re-hash an image (verification) using the same one-thread-per-algorithm fan-out."""
    hashers = {a: hashlib.new(a) for a in algos}
    consumers = [_Consumer(f"verify-{a}", hashers[a].update) for a in algos]
    for c in consumers:
        c.start()
    started = time.perf_counter()
    done = 0

    def blocks():
        if reader is not None:
            yield from reader
            return
        for p in paths:
            with open(p, "rb", buffering=0) as f:
                while True:
                    data = f.read(block)
                    if not data:
                        break
                    yield data

    try:
        last_emit = 0.0
        for data in blocks():
            if done + len(data) > total:  # E01 media may carry sector padding past the source size
                data = data[: total - done]
            for c in consumers:
                c.put(data, cancel)
            done += len(data)
            now = time.perf_counter()
            if now - last_emit > 0.25:
                last_emit = now
                avg = done / max(1e-6, now - started)
                on_progress(ImagingProgress("verifying", done, total, avg, avg, (total - done) / avg if avg else None))
            if done >= total:
                break
    finally:
        for c in consumers:
            c.q.put(None)
        for c in consumers:
            c.join()
    for c in consumers:
        if c.error:
            raise c.error
    return {a: h.hexdigest() for a, h in hashers.items()}


STAGE_LABELS = {
    "read": "source drive read speed",
    "hash-md5": "MD5 hashing (single core)",
    "hash-sha1": "SHA-1 hashing (single core)",
    "hash-sha256": "SHA-256 hashing (single core)",
    "writer": "destination write speed",
}


def _bottleneck(elapsed: float, read_wait: float, consumers: list[_Consumer], fmt: str) -> dict:
    """Name the stage that limited throughput: whichever was busy for the largest share of the run. For the
    source that is the time the pipeline sat waiting on reads; for hashing/writing, time inside the stage."""
    stages = {c.name: round(c.busy / elapsed, 3) if elapsed else 0.0 for c in consumers}
    stages["read"] = round(read_wait / elapsed, 3) if elapsed else 0.0
    limiter = max(stages, key=stages.get)
    label = STAGE_LABELS.get(limiter, limiter)
    if limiter == "writer" and fmt == "e01":
        label = "E01 compression + destination write"
    return {"stage": limiter, "label": label, "utilisation": stages}


def acquire(
    *,
    source: str,
    output_dir: str,
    name: str,
    fmt: str,
    hashes: list[str],
    block_size: int,
    compression: str,
    segment_size: Optional[int],
    case_info: dict,
    io_depth: int = 2,
    device_info: dict,
    verify: bool,
    on_progress: Callable[[ImagingProgress], None],
    cancel: threading.Event,
) -> dict:
    if fmt not in ("e01", "dd"):
        raise ValueError(f"Unknown format: {fmt}")
    algos = [a for a in HASH_ALGOS if a in hashes]
    if not algos:
        raise ValueError("At least one hash algorithm is required")
    base = Path(output_dir) / name

    io_depth = max(1, int(io_depth))
    src = RawSource(source)
    total = src.size
    try:
        if total <= 0:
            raise ImagingError("Source reports a size of 0 bytes")
        if fmt == "e01":
            writer = ewf.EwfWriter(
                base, total, bytes_per_sector=src.sector_size if src.sector_size in (512, 4096) else 512,
                compression=compression, segment_size=segment_size, case_info=case_info,
                device_info=device_info, removable=bool(device_info.get("removable")),
                physical=device_info.get("kind", "disk") == "disk",
            )
        else:
            writer = RawWriter(base, segment_size)
    except BaseException:
        src.close()
        raise

    # One handle per reader thread: Windows serialises I/O on a single synchronous handle, and separate
    # handles also keep positional reads independent on POSIX.
    bad: list[int] = []
    extra = [RawSource(source) for _ in range(io_depth - 1)]
    handles: queue.Queue = queue.Queue()
    for h in [src, *extra]:
        handles.put(h)

    def read_at(offset: int, length: int) -> bytes:
        h = handles.get()
        try:
            return _read_block(h, offset, length, bad)
        finally:
            handles.put(h)

    read_pool = ThreadPoolExecutor(max_workers=io_depth, thread_name_prefix="reader")

    hashers = {a: hashlib.new(a) for a in algos}
    consumers = [_Consumer(f"hash-{a}", hashers[a].update) for a in algos]
    consumers.append(_Consumer("writer", writer.write))
    for c in consumers:
        c.start()

    started = time.perf_counter()
    started_wall = time.time()
    window: list[tuple[float, int]] = [(started, 0)]
    pos = 0
    next_off = 0
    inflight: deque = deque()
    read_wait = 0.0
    ok = False
    try:
        last_emit = 0.0
        while pos < total:
            if cancel.is_set():
                raise ImagingCancelled()
            while next_off < total and len(inflight) < io_depth:
                n = min(block_size, total - next_off)
                inflight.append((n, read_pool.submit(read_at, next_off, n)))
                next_off += n
            n, fut = inflight.popleft()
            t0 = time.perf_counter()
            data = fut.result()
            read_wait += time.perf_counter() - t0
            for c in consumers:
                c.put(data, cancel)
            pos += n
            now = time.perf_counter()
            if now - last_emit > 0.25 or pos >= total:
                last_emit = now
                window.append((now, pos))
                while len(window) > 2 and now - window[0][0] > 3.0:
                    window.pop(0)
                t0, b0 = window[0]
                recent = (pos - b0) / (now - t0) if now > t0 else 0.0
                avg = pos / max(1e-6, now - started)
                on_progress(ImagingProgress(
                    "imaging", pos, total, recent, avg, (total - pos) / recent if recent else None, len(bad)
                ))
        for c in consumers:
            c.put(None, cancel)
        for c in consumers:
            c.join()
        for c in consumers:
            if c.error:
                raise c.error
        ok = True
    finally:
        for f in inflight:
            f[1].cancel()
        read_pool.shutdown(wait=True, cancel_futures=True)
        for h in [src, *extra]:
            h.close()
        if not ok:
            for c in consumers:
                while True:  # discard queued blocks so each thread sees the stop sentinel promptly
                    try:
                        c.q.get_nowait()
                    except queue.Empty:
                        break
                c.q.put(None)
            for c in consumers:
                c.join(timeout=30)
            writer.abort()
            for p in writer.paths:  # a partial image is not evidence; don't leave it looking like one
                try:
                    os.remove(p)
                except OSError:
                    pass

    read_seconds = time.perf_counter() - started
    bottleneck = _bottleneck(read_seconds, read_wait, consumers, fmt)
    digests = {a: h.hexdigest() for a, h in hashers.items()}
    bad_ranges = _merge_ranges(bad)
    if fmt == "e01":
        on_progress(ImagingProgress("finalising", total, total, 0, total / read_seconds, 0, len(bad)))
        # E01 can only store MD5 (hash section) and MD5+SHA-1 (digest section); others live in the log/report.
        paths = writer.finalize(
            bytes.fromhex(digests["md5"]) if "md5" in digests else None,
            bytes.fromhex(digests["sha1"]) if "sha1" in digests else None,
            bad_ranges,
        )
    else:
        paths = writer.finalize()
    finished_wall = time.time()
    elapsed = time.perf_counter() - started

    result = {
        "paths": paths,
        "format": fmt,
        "total_bytes": total,
        "sector_size": src.sector_size,
        "image_bytes": sum(os.path.getsize(p) for p in paths),
        "hashes": digests,
        "bad_sectors": len(bad),
        "bad_ranges": bad_ranges[:1000],
        "started_at": started_wall,
        "finished_at": finished_wall,
        "duration": elapsed,
        "avg_speed": total / elapsed if elapsed else 0.0,
        "block_size": block_size,
        "io_depth": io_depth,
        "bottleneck": bottleneck,
        "compression": compression if fmt == "e01" else None,
        "segment_size": segment_size,
        "verify": None,
    }

    if verify:
        v_started = time.perf_counter()
        reader = ewf.iter_media(paths[0]) if fmt == "e01" else None
        v_hashes = _hash_files(paths, algos, block_size, total, on_progress, cancel, reader=reader)
        result["verify"] = {
            "hashes": v_hashes,
            "match": all(v_hashes[a] == digests[a] for a in algos),
            "duration": time.perf_counter() - v_started,
        }
    return result
