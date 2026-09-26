from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import devices, file_browser, imager
from . import report as report_mod
from . import triage as triage_mod
from .ewf import COMPRESSION_LEVELS
from .jobs import job_manager

app = FastAPI(title="Quick Capture")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
BLOCK_SIZES_MB = (1, 2, 4, 8, 16, 32)
IO_DEPTHS = (1, 2, 4, 8)
MIN_SEGMENT_MB = 16
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()\-]{0,120}$")


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "admin": devices.is_admin(),
        "platform": sys.platform,
        "smartctl": triage_mod.find_smartctl(),
        "cpu_count": os.cpu_count(),
    }


@app.get("/api/options")
async def api_options():
    return {
        "triage_modes": {k: v["label"] for k, v in triage_mod.TRIAGE_MODES.items()},
        "block_sizes_mb": BLOCK_SIZES_MB,
        "io_depths": IO_DEPTHS,
        "compression": list(COMPRESSION_LEVELS),
        "hashes": list(imager.HASH_ALGOS),
    }


@app.get("/api/devices")
async def api_devices():
    try:
        return {"devices": await asyncio.to_thread(devices.list_devices)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Could not list devices: {exc}") from exc


@app.get("/api/source-info")
async def api_source_info(path: str = Query(...)):
    try:
        return await asyncio.to_thread(devices.source_info, path)
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail=f"Permission denied opening {path} — run Quick Capture as Administrator/root"
        ) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot open {path}: {exc}") from exc


@app.get("/api/browse")
async def api_browse(path: str = Query(default=""), mode: str = Query(default="dir")):
    try:
        return file_browser.browse(path or None, mode)
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class CaseInfo(BaseModel):
    case_number: str = ""
    evidence_number: str = ""
    examiner: str = ""
    description: str = ""
    notes: str = ""


class AcquireRequest(BaseModel):
    source: str
    output_dir: str
    name: str
    format: str = "e01"
    hashes: list[str] = Field(default_factory=lambda: ["md5", "sha1"])
    block_size_mb: int = 8
    io_depth: int = 2  # reads kept in flight at once
    compression: str = "fast"
    segment_size_mb: int = 0  # 0 = no split
    triage_mode: str = "quick"
    triage_only: bool = False
    verify: bool = False
    case: CaseInfo = Field(default_factory=CaseInfo)


def _expected_outputs(req: AcquireRequest) -> list[Path]:
    base = Path(req.output_dir) / req.name
    first = {"e01": ".E01", "dd": ".001" if req.segment_size_mb else ".dd"}[req.format]
    return [base.with_name(base.name + first), base.with_name(base.name + ".txt")]


@app.post("/api/acquire")
async def api_acquire(req: AcquireRequest):
    if job_manager.active():
        raise HTTPException(status_code=409, detail="Another acquisition is already running")
    if req.format not in ("e01", "dd"):
        raise HTTPException(status_code=400, detail=f"Unknown format: {req.format}")
    if req.triage_mode not in triage_mod.TRIAGE_MODES:
        raise HTTPException(status_code=400, detail=f"Unknown scan mode: {req.triage_mode}")
    if req.block_size_mb not in BLOCK_SIZES_MB:
        raise HTTPException(status_code=400, detail=f"Block size must be one of {BLOCK_SIZES_MB} MiB")
    if req.io_depth not in IO_DEPTHS:
        raise HTTPException(status_code=400, detail=f"Read queue depth must be one of {IO_DEPTHS}")
    if req.compression not in COMPRESSION_LEVELS:
        raise HTTPException(status_code=400, detail=f"Unknown compression: {req.compression}")
    if req.segment_size_mb and req.segment_size_mb < MIN_SEGMENT_MB:
        raise HTTPException(status_code=400, detail=f"Segment size must be at least {MIN_SEGMENT_MB} MiB")
    if req.triage_only and req.triage_mode == "skip":
        req.triage_mode = "quick"
    unknown = set(req.hashes) - set(imager.HASH_ALGOS)
    if not req.hashes:
        raise HTTPException(status_code=400, detail="Select at least one hash algorithm")
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown hash algorithm(s): {', '.join(sorted(unknown))}")

    try:
        info = await asyncio.to_thread(devices.source_info, req.source)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Permission denied opening the source — run as Administrator/root") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot open source: {exc}") from exc

    if not req.triage_only:
        if not SAFE_NAME.match(req.name):
            raise HTTPException(status_code=400, detail="Image name may only contain letters, digits, spaces and . _ ( ) -")
        out_dir = Path(req.output_dir)
        if not out_dir.is_dir():
            raise HTTPException(status_code=400, detail=f"Output folder not found: {req.output_dir}")
        for p in _expected_outputs(req):
            if p.exists():
                raise HTTPException(status_code=400, detail=f"Refusing to overwrite existing file: {p}")
        if not info["is_device"] and Path(req.source).resolve().parent == out_dir.resolve() \
                and Path(req.source).name.startswith(req.name + "."):
            raise HTTPException(status_code=400, detail="Output would collide with the source file")
        free = shutil.disk_usage(out_dir).free
        needs_full_space = req.format == "dd" or req.compression == "none"
        if needs_full_space and free < info["size"]:
            raise HTTPException(
                status_code=400,
                detail=f"Not enough free space: need {report_mod.fmt_bytes(info['size'])}, "
                f"have {report_mod.fmt_bytes(free)} in {req.output_dir}",
            )

    device = None
    try:
        listed = await asyncio.to_thread(devices.list_devices)
        device = next((d for d in listed if d["path"].lower() == req.source.lower()), None)
    except Exception:  # noqa: BLE001  (device details only enrich the report / E01 header)
        pass
    if device is None:
        device = {"path": req.source, "kind": "disk" if info["is_device"] else "file", "model": "", "serial": ""}

    options = req.model_dump()
    job = job_manager.create(options, device)
    asyncio.create_task(job_manager.run(job.id))
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
async def api_job_status(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.public_dict()


@app.post("/api/jobs/{job_id}/cancel")
async def api_job_cancel(job_id: str):
    if not job_manager.cancel(job_id):
        raise HTTPException(status_code=400, detail="Job cannot be cancelled")
    return {"cancelled": True}


@app.post("/api/jobs/{job_id}/proceed")
async def api_job_proceed(job_id: str):
    if not job_manager.decide(job_id, proceed=True):
        raise HTTPException(status_code=400, detail="Job is not waiting for a decision")
    return {"proceeding": True}


def _get_finished_job(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "done" or not job.result:
        raise HTTPException(status_code=400, detail=f"No acquisition result for this job (status: {job.status})")
    return job


@app.get("/api/jobs/{job_id}/report.html")
async def api_job_report_html(job_id: str):
    job = _get_finished_job(job_id)
    return HTMLResponse(report_mod.generate_report_html(job))


@app.get("/api/jobs/{job_id}/report.json")
async def api_job_report_json(job_id: str):
    job = _get_finished_job(job_id)
    return JSONResponse(
        report_mod.generate_report_json(job),
        headers={"Content-Disposition": f'attachment; filename="quick-capture-report-{job_id}.json"'},
    )


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))
