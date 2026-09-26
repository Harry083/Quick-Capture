"""Pre-imaging triage: is this device healthy enough to image at full speed?

Three signals, cheapest first:
  1. SMART (via smartctl, if installed): reallocated / pending / uncorrectable sector counts, NVMe media
     errors, overall health. Instant, and catches drives that have already started failing.
  2. A throughput probe: a short sequential read to estimate imaging time.
  3. A read scan: "quick" and "thorough" read evenly-spread samples across the whole LBA range (seconds),
     "full" reads every sector with no hashing or writing (as long as imaging, but proves readability).

Any unreadable sector or SMART defect produces an "attention" verdict so the user decides whether to
continue — the imager itself zero-fills unreadable sectors rather than retrying them.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .devices import IS_MAC, IS_WINDOWS, RawSource

TRIAGE_MODES = {
    "quick": {"label": "Quick (SMART + 512 samples)", "samples": 512},
    "thorough": {"label": "Thorough (SMART + 8,192 samples)", "samples": 8192},
    "full": {"label": "Full surface read", "samples": None},
    "skip": {"label": "Skip scan", "samples": 0},
}
SAMPLE_SIZE = 64 * 1024
PROBE_BYTES = 64 * 1024 * 1024
FULL_BLOCK = 8 * 1024 * 1024
MAX_BAD_RECORDED = 10_000

# ATA attributes whose raw value should be zero on a healthy drive.
SMART_WATCH = {
    5: "Reallocated sectors",
    187: "Reported uncorrectable errors",
    196: "Reallocation events",
    197: "Current pending sectors",
    198: "Offline uncorrectable sectors",
}


class TriageCancelled(Exception):
    pass


@dataclass
class TriageProgress:
    stage: str
    percent: float
    detail: str = ""


def find_smartctl() -> Optional[str]:
    found = shutil.which("smartctl")
    if found:
        return found
    if IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
            candidate = Path(base) / "smartmontools" / "bin" / "smartctl.exe"
            if base and candidate.is_file():
                return str(candidate)
    return None


def _smart_device_arg(path: str) -> str:
    """Translate an imaging path into the device name smartctl expects (always the whole disk)."""
    if IS_WINDOWS:
        m = re.match(r"^\\\\\.\\PHYSICALDRIVE(\d+)$", path, re.IGNORECASE)
        if m:
            return f"/dev/pd{m.group(1)}"
        m = re.match(r"^\\\\\.\\([A-Za-z]):$", path)
        if m:
            return f"{m.group(1)}:"  # smartctl resolves a drive letter to the disk holding it
        return path
    if IS_MAC:
        m = re.match(r"^/dev/r?(disk\d+)", path)
        return f"/dev/{m.group(1)}" if m else path
    real = os.path.realpath(path)
    name = os.path.basename(real)
    part = Path("/sys/class/block") / name / "partition"
    if part.exists():
        return "/dev/" + Path(os.path.realpath(Path("/sys/class/block") / name)).parent.name
    return real


def read_smart(path: str) -> dict:
    smartctl = find_smartctl()
    if not smartctl:
        return {"available": False, "reason": "smartctl not installed (install smartmontools for SMART checks)"}
    device = _smart_device_arg(path)
    kwargs = {"creationflags": 0x08000000} if IS_WINDOWS else {}
    try:
        proc = subprocess.run(
            [smartctl, "-j", "-a", device], capture_output=True, text=True, timeout=30, **kwargs
        )
        data = json.loads(proc.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        return {"available": False, "reason": f"smartctl failed: {exc}"}

    has_health = any(
        k in data for k in ("smart_status", "ata_smart_attributes", "nvme_smart_health_information_log")
    )
    if not has_health:
        msgs = "; ".join(m.get("string", "") for m in data.get("smartctl", {}).get("messages", []))
        return {"available": False, "reason": msgs or "SMART not supported by this device/bridge", "device": device}

    attributes, issues = [], []
    passed = data.get("smart_status", {}).get("passed")
    if passed is False:
        issues.append("SMART overall health self-assessment: FAILED")

    for attr in data.get("ata_smart_attributes", {}).get("table", []):
        if attr.get("id") in SMART_WATCH:
            raw = int(attr.get("raw", {}).get("value", 0) or 0)
            attributes.append({"name": SMART_WATCH[attr["id"]], "id": attr["id"], "value": raw, "flag": raw > 0})
            if raw > 0:
                issues.append(f"{SMART_WATCH[attr['id']]}: {raw}")

    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        for key, label in (("media_errors", "Media and data integrity errors"), ("critical_warning", "Critical warning")):
            val = int(nvme.get(key, 0) or 0)
            attributes.append({"name": label, "value": val, "flag": val > 0})
            if val > 0:
                issues.append(f"{label}: {val}")
        if "percentage_used" in nvme:
            attributes.append({"name": "Percentage used", "value": nvme["percentage_used"], "flag": False})

    if "scsi_grown_defect_list" in data:
        val = int(data["scsi_grown_defect_list"] or 0)
        attributes.append({"name": "Grown defect list", "value": val, "flag": val > 0})
        if val > 0:
            issues.append(f"Grown defects: {val}")

    if "power_on_time" in data:
        attributes.append({"name": "Power-on hours", "value": data["power_on_time"].get("hours"), "flag": False})

    return {
        "available": True,
        "device": device,
        "model": data.get("model_name") or data.get("scsi_model_name") or "",
        "serial": data.get("serial_number") or "",
        "passed": passed,
        "attributes": attributes,
        "issues": issues,
    }


def locate_bad_sectors(src: RawSource, offset: int, length: int, bad: list[int], limit: int = MAX_BAD_RECORDED) -> None:
    """Re-read a failed range one sector at a time and record the unreadable LBAs."""
    sector = src.sector_size
    for pos in range(offset, offset + length, sector):
        if len(bad) >= limit:
            return
        try:
            src.pread(min(sector, offset + length - pos), pos)
        except OSError:
            bad.append(pos // sector)


def _sample_offsets(size: int, samples: int, align: int) -> list[int]:
    if size <= SAMPLE_SIZE:
        return [0]
    last = (size - SAMPLE_SIZE) // align * align
    offsets = {0, last}
    for i in range(samples):
        offsets.add((last * i // max(1, samples - 1)) // align * align)
    return sorted(offsets)


def run_triage(
    path: str,
    mode: str,
    on_progress: Callable[[TriageProgress], None],
    cancel: threading.Event,
) -> dict:
    if mode not in TRIAGE_MODES:
        raise ValueError(f"Unknown scan mode: {mode}")
    started = time.time()
    result: dict = {"mode": mode, "findings": [], "bad_sectors": [], "smart": None}
    findings = result["findings"]

    on_progress(TriageProgress("scan: SMART", 0.0, "Reading SMART data"))
    smart = read_smart(path)
    result["smart"] = smart
    if not smart["available"]:
        findings.append({"level": "info", "title": "SMART unavailable", "detail": smart["reason"]})
    elif smart["issues"]:
        for issue in smart["issues"]:
            findings.append({"level": "bad", "title": "SMART defect", "detail": issue})
    else:
        findings.append({"level": "ok", "title": "SMART healthy", "detail": "No reallocated, pending or uncorrectable sectors reported"})

    with RawSource(path) as src:
        result["size"] = src.size
        result["sector_size"] = src.sector_size
        bad: list[int] = result["bad_sectors"]

        # Throughput probe — drop any cached pages first so the number reflects the device, not RAM.
        probe = min(PROBE_BYTES, src.size)
        src.drop_cache(0, probe)
        t0 = time.perf_counter()
        read = 0
        while read < probe:
            if cancel.is_set():
                raise TriageCancelled()
            n = min(FULL_BLOCK, probe - read)
            try:
                src.pread(n, read)
            except OSError:
                locate_bad_sectors(src, read, n, bad)
            read += n
            on_progress(TriageProgress("scan: speed probe", 5 * read / max(1, probe), f"{read >> 20} MiB"))
        elapsed = time.perf_counter() - t0
        speed = probe / elapsed if elapsed > 0 and probe else 0.0
        result["read_speed"] = speed
        result["estimated_seconds"] = src.size / speed if speed else None

        samples = TRIAGE_MODES[mode]["samples"]
        if samples is None:
            scan_total = src.size
            pos = probe  # the speed probe already read everything before this
            while pos < src.size:
                if cancel.is_set():
                    raise TriageCancelled()
                n = min(FULL_BLOCK, src.size - pos)
                try:
                    src.pread(n, pos)
                except OSError:
                    locate_bad_sectors(src, pos, n, bad)
                pos += n
                on_progress(TriageProgress(
                    "scan: full surface read", 5 + 95 * pos / scan_total, f"{len(bad)} bad sector(s)"
                ))
            result["scanned_bytes"] = src.size
        elif samples:
            offsets = _sample_offsets(src.size, samples, max(src.sector_size, 4096))
            for i, off in enumerate(offsets):
                if cancel.is_set():
                    raise TriageCancelled()
                n = min(SAMPLE_SIZE, src.size - off)
                try:
                    src.pread(n, off)
                except OSError:
                    locate_bad_sectors(src, off, n, bad)
                if i % 16 == 0 or i == len(offsets) - 1:
                    on_progress(TriageProgress(
                        "scan: sampling surface", 5 + 95 * (i + 1) / len(offsets), f"{len(bad)} bad sector(s)"
                    ))
            result["scanned_bytes"] = len(offsets) * SAMPLE_SIZE
            result["samples"] = len(offsets)

    bad[:] = sorted(set(bad))  # the probe and a sample can cover the same sectors
    if bad:
        findings.append({
            "level": "bad",
            "title": "Unreadable sectors found",
            "detail": f"{len(bad)} bad sector(s), first at LBA {bad[0]}"
            + (" (sampled scan — there are likely more)" if samples else ""),
        })
    elif mode != "skip":
        findings.append({"level": "ok", "title": "Read scan clean", "detail": "Every sector read in the scan was readable"})

    result["verdict"] = "attention" if any(f["level"] == "bad" for f in findings) else "clear"
    result["duration"] = time.time() - started
    on_progress(TriageProgress("scan: done", 100.0))
    return result
