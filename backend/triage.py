"""Quick triage of an evidence image: what OS is on it, what device it came from, who used it, and what
was saved last. It reads the partition table, the filesystem metadata and a few small system files; it
never hashes or indexes the whole image."""
from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import linux, windows
from .ext import Ext, ExtError
from .image import ImageError, Slice, open_image
from .ntfs import Ntfs, NtfsError
from .regf import HiveError
from .volumes import list_volumes

UNSUPPORTED = {"APFS", "HFS+", "XFS", "Btrfs", "ReFS", "LVM"}


class TriageCancelled(Exception):
    pass


@dataclass
class Progress:
    stage: str
    percent: float
    detail: str = ""


def run_triage(path: str, on_progress: Callable[[Progress], None], cancel: threading.Event) -> dict:
    started = time.time()
    findings: list[dict] = []
    on_progress(Progress("opening image", 0.0))
    image = open_image(path)
    try:
        info = image.info()
        if info["format"] == "E01" and info.get("acquisition_errors"):
            findings.append({"level": "bad", "title": "Acquisition errors recorded",
                             "detail": f"{info['acquisition_errors']} unreadable range(s) were zero-filled when this image was made"})

        on_progress(Progress("reading partitions", 2.0))
        scheme, volumes = list_volumes(image)
        systems = []
        candidates = [v for v in volumes if v["fs"] in ("NTFS", "ext2", "ext3", "ext4")]
        share = 96.0 / max(1, len(candidates))
        for i, vol in enumerate(candidates):
            if cancel.is_set():
                raise TriageCancelled()
            base = 2.0 + share * i
            label = f"partition {vol['index']} ({vol['fs']})"

            def progress(frac, detail="", base=base, label=label):
                if cancel.is_set():
                    raise TriageCancelled()
                on_progress(Progress(f"{label}: {detail}" if detail else label, base + share * frac))

            progress(0.0, "looking for an OS")
            result = _analyse_volume(image, vol, progress, cancel, findings)
            if result:
                result["volume"] = {k: vol.get(k) for k in ("index", "offset", "size", "fs", "type", "name")}
                vol["os"] = (result.get("os") or {}).get("name") or result["kind"].title()
                systems.append(result)

        for vol in volumes:
            if vol["fs"] in ("BitLocker", "LUKS"):
                findings.append({"level": "bad", "title": f"Encrypted volume ({vol['fs']})",
                                 "detail": f"Partition {vol['index']} ({_size(vol['size'])}) can't be read without its key"})
            elif vol["fs"] in UNSUPPORTED:
                findings.append({"level": "info", "title": f"{vol['fs']} not examined",
                                 "detail": f"Partition {vol['index']} uses {vol['fs']}, which triage can't read yet"})
        if not systems:
            findings.append({"level": "info", "title": "No operating system found",
                             "detail": "No Windows (NTFS) or Linux (ext) system volume was found in this image"})
        for s in systems:
            findings.extend(s.pop("notes", []))
        if getattr(image, "bad_chunks", 0):
            findings.append({"level": "bad", "title": "Damaged chunks",
                             "detail": f"{image.bad_chunks} chunk(s) couldn't be decompressed and were read as zeros"})
    finally:
        image.close()

    on_progress(Progress("done", 100.0))
    return {
        "image": info,
        "partition_scheme": scheme,
        "volumes": volumes,
        "systems": systems,
        "findings": findings,
        "duration": time.time() - started,
    }


def _analyse_volume(image, vol: dict, progress, cancel, findings: list) -> dict | None:
    sl = Slice(image, vol["offset"], vol["size"])
    try:
        if vol["fs"] == "NTFS":
            fs = Ntfs(sl)
            if not windows.is_windows(fs):
                return None
            return windows.analyse(fs, progress=progress, cancel=cancel)
        fs = Ext(sl)
        if not linux.is_linux(fs):
            return None
        return linux.analyse(fs, progress=progress, cancel=cancel)
    except TriageCancelled:
        raise
    except (NtfsError, ExtError, HiveError, ImageError, struct.error, ValueError, IndexError) as exc:
        findings.append({"level": "bad", "title": f"Partition {vol['index']} couldn't be read",
                         "detail": f"{vol['fs']}: {exc}"})
        return None


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n} B"
