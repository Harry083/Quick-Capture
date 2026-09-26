"""In-memory background job manager: triage → (pause for a decision if needed) → image → verify."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from . import imager
from . import triage as triage_mod


@dataclass
class Job:
    id: str
    options: dict
    status: str = "queued"  # queued | running | awaiting | done | error | cancelled
    stage: str = "queued"
    percent: float = 0.0
    bytes_done: int = 0
    total_bytes: Optional[int] = None
    speed: float = 0.0
    avg_speed: float = 0.0
    eta: Optional[float] = None
    bad_sectors: int = 0
    detail: str = ""
    error: Optional[str] = None
    triage: Optional[dict] = None
    result: Optional[dict] = None
    device: Optional[dict] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    decision_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    proceed: bool = False

    def public_dict(self) -> dict:
        return {
            "id": self.id,
            "options": self.options,
            "status": self.status,
            "stage": self.stage,
            "percent": self.percent,
            "bytes_done": self.bytes_done,
            "total_bytes": self.total_bytes,
            "speed": self.speed,
            "avg_speed": self.avg_speed,
            "eta": self.eta,
            "bad_sectors": self.bad_sectors,
            "detail": self.detail,
            "error": self.error,
            "triage": self.triage,
            "result": self.result,
            "device": self.device,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self, options: dict, device: Optional[dict]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], options=options, device=device)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def active(self) -> Optional[Job]:
        return next((j for j in self._jobs.values() if j.status in ("queued", "running", "awaiting")), None)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status not in ("queued", "running", "awaiting"):
            return False
        job.cancel_event.set()
        job.decision_event.set()
        return True

    def decide(self, job_id: str, proceed: bool) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status != "awaiting":
            return False
        if not proceed:
            return self.cancel(job_id)
        job.proceed = True
        job.decision_event.set()
        return True

    async def run(self, job_id: str) -> None:
        job = self._jobs[job_id]
        opts = job.options
        loop = asyncio.get_running_loop()
        job.status = "running"

        def touch() -> None:
            job.updated_at = time.time()

        def on_triage(p: triage_mod.TriageProgress) -> None:
            job.stage, job.percent, job.detail = p.stage, p.percent, p.detail
            loop.call_soon_threadsafe(touch)

        def on_image(p: imager.ImagingProgress) -> None:
            job.stage = p.stage
            job.percent = p.percent
            job.bytes_done, job.total_bytes = p.bytes_done, p.total
            job.speed, job.avg_speed, job.eta = p.speed, p.avg_speed, p.eta
            job.bad_sectors = p.bad_sectors
            loop.call_soon_threadsafe(touch)

        try:
            if opts["triage_mode"] != "skip":
                job.stage = "scan"
                job.triage = await asyncio.to_thread(
                    triage_mod.run_triage, opts["source"], opts["triage_mode"], on_triage, job.cancel_event
                )
                if job.cancel_event.is_set():
                    raise imager.ImagingCancelled()

                if opts.get("triage_only"):
                    job.status, job.stage, job.percent = "done", "done", 100.0
                    return

                if job.triage["verdict"] != "clear":
                    job.status = "awaiting"
                    job.stage = "awaiting decision"
                    touch()
                    await job.decision_event.wait()
                    if job.cancel_event.is_set() or not job.proceed:
                        raise imager.ImagingCancelled()
                    job.status = "running"

            job.stage, job.percent, job.detail = "imaging", 0.0, ""
            job.result = await asyncio.to_thread(
                imager.acquire,
                source=opts["source"],
                output_dir=opts["output_dir"],
                name=opts["name"],
                fmt=opts["format"],
                hashes=opts["hashes"],
                block_size=opts["block_size_mb"] * 1024 * 1024,
                compression=opts["compression"],
                segment_size=opts["segment_size_mb"] * 1024 * 1024 if opts["segment_size_mb"] else None,
                case_info=opts["case"],
                io_depth=opts["io_depth"],
                device_info=job.device or {},
                verify=opts["verify"],
                on_progress=on_image,
                cancel=job.cancel_event,
            )
            job.status, job.stage, job.percent = "done", "done", 100.0
            job.bad_sectors = job.result["bad_sectors"]
            try:
                from .report import write_acquisition_log

                job.result["log_path"] = write_acquisition_log(job)
            except OSError as exc:
                job.result["log_error"] = str(exc)
        except (imager.ImagingCancelled, triage_mod.TriageCancelled):
            job.status = job.stage = "cancelled"
        except (imager.ImagingError, OSError, ValueError) as exc:
            job.status = job.stage = "error"
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001
            job.status = job.stage = "error"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            touch()


job_manager = JobManager()
