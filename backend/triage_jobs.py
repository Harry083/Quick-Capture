"""In-memory triage jobs (separate from imaging jobs): each triage runs on its own thread and the page polls for progress."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from . import triage as triage_mod
from .image import ImageError


@dataclass
class Job:
    id: str
    path: str
    status: str = "running"  # running | done | error | cancelled
    stage: str = "starting"
    percent: float = 0.0
    error: Optional[str] = None
    result: Optional[dict] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public_dict(self) -> dict:
        return {
            "id": self.id, "path": self.path, "status": self.status, "stage": self.stage,
            "percent": self.percent, "error": self.error, "result": self.result,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def active(self) -> Optional[Job]:
        return next((j for j in self._jobs.values() if j.status == "running"), None)

    def start(self, path: str) -> Job:
        with self._lock:
            job = Job(id=uuid.uuid4().hex[:12], path=path)
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), name=f"triage-{job.id}", daemon=True).start()
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status != "running":
            return False
        job.cancel_event.set()
        return True

    def _run(self, job: Job) -> None:
        def on_progress(p: triage_mod.Progress) -> None:
            job.stage, job.percent, job.updated_at = p.stage, p.percent, time.time()

        try:
            job.result = triage_mod.run_triage(job.path, on_progress, job.cancel_event)
            job.status, job.stage, job.percent = "done", "done", 100.0
        except triage_mod.TriageCancelled:
            job.status = job.stage = "cancelled"
        except (ImageError, OSError, ValueError) as exc:
            job.status = job.stage = "error"
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001
            job.status = job.stage = "error"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.updated_at = time.time()


triage_jobs = JobManager()
