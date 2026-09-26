"""Round-trip tests for the imaging pipeline. Run with:  python -m pytest tests

If libewf's `ewfverify` is on PATH, the E01 output is also checked by an independent implementation.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import devices, ewf, imager, triage  # noqa: E402

MiB = 1024 * 1024


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    """~21 MiB: random data, a run of zeros (exercises the zero-chunk path) and a non-chunk-aligned tail."""
    p = tmp_path / "source.raw"
    rnd = os.urandom(12 * MiB)
    p.write_bytes(rnd + bytes(9 * MiB) + rnd[:3 * 512])
    return p


def _acquire(src: Path, out: Path, **kw) -> dict:
    opts = dict(
        source=str(src), output_dir=str(out), name="img", fmt="e01", hashes=["md5", "sha1", "sha256"],
        block_size=4 * MiB, compression="fast", segment_size=None, case_info={"case_number": "T-1"},
        device_info={"model": "Test", "serial": "S1", "kind": "disk"}, verify=True,
        on_progress=lambda p: None, cancel=threading.Event(),
    )
    opts.update(kw)
    return imager.acquire(**opts)


def _ewfverify(path: str) -> None:
    if shutil.which("ewfverify"):
        proc = subprocess.run(["ewfverify", "-q", path], capture_output=True, text=True)
        assert "SUCCESS" in proc.stdout + proc.stderr, proc.stdout + proc.stderr


@pytest.mark.parametrize("compression", ["fast", "none", "best"])
def test_e01_roundtrip(source: Path, tmp_path: Path, compression: str) -> None:
    out = tmp_path / "out"
    out.mkdir()
    res = _acquire(source, out, compression=compression)
    data = source.read_bytes()
    assert res["hashes"]["md5"] == hashlib.md5(data).hexdigest()
    assert res["hashes"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert res["verify"]["match"]
    assert res["bad_sectors"] == 0
    _ewfverify(res["paths"][0])


def test_e01_segmented(source: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    res = _acquire(source, out, compression="none", segment_size=4 * MiB)
    assert len(res["paths"]) > 3
    assert res["paths"][1].endswith(".E02")
    assert res["verify"]["match"]
    _ewfverify(res["paths"][0])


def test_dd_single_and_split(source: Path, tmp_path: Path) -> None:
    data = source.read_bytes()
    for seg, expect in ((None, ["img.dd"]), (5 * MiB, ["img.001", "img.002", "img.003", "img.004", "img.005"])):
        out = tmp_path / f"dd-{seg}"
        out.mkdir()
        res = _acquire(source, out, fmt="dd", segment_size=seg)
        assert [Path(p).name for p in res["paths"]] == expect
        assert b"".join(Path(p).read_bytes() for p in res["paths"]) == data
        assert res["verify"]["match"]


def test_refuses_to_overwrite(source: Path, tmp_path: Path) -> None:
    (tmp_path / "img.dd").write_bytes(b"existing evidence")
    with pytest.raises(FileExistsError):
        _acquire(source, tmp_path, fmt="dd")
    assert (tmp_path / "img.dd").read_bytes() == b"existing evidence"


def test_bad_sectors_are_zero_filled_and_logged(source: Path, tmp_path: Path, monkeypatch) -> None:
    bad_lbas = {5000, 5001, 20000}
    real_pread = devices.RawSource.pread

    def flaky_pread(self, length, offset):
        first, last = offset // 512, (offset + length - 1) // 512
        if any(first <= lba <= last for lba in bad_lbas):
            raise OSError(5, "Input/output error")
        return real_pread(self, length, offset)

    monkeypatch.setattr(devices.RawSource, "pread", flaky_pread)
    out = tmp_path / "out"
    out.mkdir()
    res = _acquire(source, out, verify=False)
    assert res["bad_sectors"] == 3
    assert res["bad_ranges"] == [(5000, 2), (20000, 1)]

    expected = bytearray(source.read_bytes())
    for lba in bad_lbas:
        expected[lba * 512:(lba + 1) * 512] = bytes(512)
    assert b"".join(ewf.iter_media(res["paths"][0]))[: len(expected)] == bytes(expected)
    assert res["hashes"]["md5"] == hashlib.md5(expected).hexdigest()
    _ewfverify(res["paths"][0])

    t = triage.run_triage(str(source), "full", lambda p: None, threading.Event())
    assert t["verdict"] == "attention"
    assert sorted(t["bad_sectors"]) == sorted(bad_lbas)


def test_triage_clear(source: Path) -> None:
    t = triage.run_triage(str(source), "quick", lambda p: None, threading.Event())
    assert t["verdict"] == "clear"
    assert t["bad_sectors"] == []
    assert t["read_speed"] > 0


def test_cancel_removes_partial_image(source: Path, tmp_path: Path) -> None:
    cancel = threading.Event()

    def on_progress(p):
        if p.bytes_done >= 8 * MiB:
            cancel.set()

    with pytest.raises(imager.ImagingCancelled):
        _acquire(source, tmp_path, fmt="dd", block_size=1 * MiB, on_progress=on_progress, cancel=cancel)
    assert not (tmp_path / "img.dd").exists()
