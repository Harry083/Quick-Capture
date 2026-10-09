"""Runs a filter chain on a frame, non-destructively, with caching.

A chain is a list of steps, as stored by the page and in project files:
    [{"id": "levels", "enabled": true, "params": {...}}, ...]

Rendering step k of frame i means running steps 1..k on source frame i. Temporal filters ask for other
frames through Context.frame(j), which renders steps 1..k-1 of frame j, so a frame-integration step after a
deinterlace step integrates deinterlaced frames. Every intermediate result is cached by (frame, the exact
steps that produced it), so moving one slider only re-runs the steps after it.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

from .filters import FILTERS, Context, FilterError
from .media import Source

CACHE_BYTES = 768 * 1024 * 1024


class ChainError(ValueError):
    """The chain itself is malformed; the message is shown to the user."""


@dataclass
class Step:
    index: int  # position in the chain as the user sees it (disabled steps included)
    id: str
    params: dict
    enabled: bool = True

    @property
    def filter(self):
        return FILTERS[self.id]


def clean_chain(raw) -> list[Step]:
    if not isinstance(raw, list):
        raise ChainError("The filter chain must be a list")
    steps = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or item.get("id") not in FILTERS:
            raise ChainError(f"Step {i + 1}: unknown filter {item.get('id') if isinstance(item, dict) else item!r}")
        f = FILTERS[item["id"]]
        steps.append(Step(i, f.id, f.clean(item.get("params")), bool(item.get("enabled", True))))
    return steps


def chain_json(steps: list[Step]) -> list[dict]:
    return [{"id": s.id, "enabled": s.enabled, "params": s.params} for s in steps]


@dataclass
class StageResult:
    step: Step
    notes: list[str] = field(default_factory=list)
    shape: tuple[int, int] | None = None
    ms: float = 0.0
    error: str = ""
    image: np.ndarray | None = field(default=None, repr=False)  # filled when render(stages=True)


class Pipeline:
    def __init__(self, source: Source) -> None:
        self.source = source
        self._cache: OrderedDict[tuple, tuple[np.ndarray, list[str], float]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    # ---------- cache ----------
    @staticmethod
    def _signature(active: list[Step], k: int) -> str:
        blob = json.dumps([(s.id, s.params) for s in active[:k]], sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()

    def _get(self, key):
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
        return hit

    def _put(self, key, value) -> None:
        self._cache[key] = value
        self._bytes += value[0].nbytes
        while self._bytes > CACHE_BYTES and len(self._cache) > 1:
            _, old = self._cache.popitem(last=False)
            self._bytes -= old[0].nbytes

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._bytes = 0

    # ---------- evaluation ----------
    def _eval(self, active: list[Step], k: int, index: int) -> tuple[np.ndarray, list[str], float]:
        """Output of the first k active steps for one frame → (image, notes from step k, ms for step k)."""
        index = min(max(index, 0), self.source.count - 1)
        if k == 0:
            return self.source.frame(index), [], 0.0
        key = (self._signature(active, k), index)
        hit = self._get(key)
        if hit is not None:
            return hit
        img, _, _ = self._eval(active, k - 1, index)
        step = active[k - 1]
        ctx = Context(
            index=index, count=self.source.count, fps=self.source.fps,
            frame=lambda j: self._eval(active, k - 1, j)[0],
            key=f"{id(self.source)}:{self._signature(active, k - 1)}",
        )
        t0 = time.perf_counter()
        out = step.filter.fn(img, step.params, ctx)
        out = np.clip(np.nan_to_num(np.asarray(out, np.float32), nan=0.0), 0, 1)
        if out.ndim != 3 or out.shape[2] != 3:
            raise FilterError(f"{step.filter.name} produced an unexpected image shape {out.shape}")
        out.flags.writeable = False
        value = (out, ctx.notes, (time.perf_counter() - t0) * 1000)
        self._put(key, value)
        return value

    def render(self, steps: list[Step], index: int, upto: int | None = None,
               stages: bool = False) -> tuple[np.ndarray, list[StageResult]]:
        """Render frame `index` through the chain (or only the steps before chain position `upto`).

        Returns (image, one StageResult per chain step, in chain order). A step that fails stops the chain
        there: the image is the last good stage and the failing step carries the error."""
        with self._lock:
            limit = len(steps) if upto is None else max(0, min(upto, len(steps)))
            active = [s for s in steps[:limit] if s.enabled]
            results = {s.index: StageResult(s) for s in steps}
            img, _, _ = self._eval(active, 0, index)
            stage_images = []
            for k, step in enumerate(active, start=1):
                try:
                    img, notes, ms = self._eval(active, k, index)
                except (FilterError, ValueError) as exc:
                    results[step.index].error = str(exc)
                    break
                except MemoryError:
                    results[step.index].error = "Not enough memory for this step"
                    break
                results[step.index].notes, results[step.index].ms = list(notes), ms
                results[step.index].shape = (img.shape[1], img.shape[0])
                if stages:
                    stage_images.append((step.index, img))
            out = [results[s.index] for s in steps]
            for i, im in stage_images:
                results[i].image = im
            return img, out
