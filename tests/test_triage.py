"""Tests for the Triage tab. Run with:  python -m pytest tests

The full-disk tests build a GPT disk with Windows (NTFS), Linux (ext4) and BitLocker partitions. The NTFS
part needs mkntfs + ntfs-3g and root (FUSE mount); without them those checks are skipped. E01 files are
written by Quick Capture's own writer and, if installed, by libewf's ewfacquire.
"""
from __future__ import annotations

import hashlib
import random
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

import triage_fixtures as fixtures  # noqa: E402
import hivegen as h  # noqa: E402
from backend import ewf  # noqa: E402
from backend import image as image_mod  # noqa: E402
from backend.regf import Hive  # noqa: E402
from backend.triage import TriageCancelled, run_triage  # noqa: E402

@pytest.fixture(scope="session")
def disk(tmp_path_factory) -> Path:
    if not fixtures.can_build_ext():
        pytest.skip("mkfs.ext4 not available")
    return fixtures.build_disk(tmp_path_factory.mktemp("disk"))


@pytest.fixture(scope="session")
def e01(disk: Path) -> Path:
    data = disk.read_bytes()
    w = ewf.EwfWriter(disk.with_name("qc"), len(data), segment_size=24 * 1024 * 1024, compression="fast",
                      case_info={"case_number": "C-1", "examiner": "HS"}, device_info={"model": "Test SSD", "serial": "SN1"})
    for i in range(0, len(data), 4 * 1024 * 1024):
        w.write(data[i:i + 4 * 1024 * 1024])
    w.finalize(hashlib.md5(data).digest(), hashlib.sha1(data).digest())
    return disk.with_name("qc.E01")


def _triage(path: Path) -> dict:
    return run_triage(str(path), lambda p: None, threading.Event())


# ---------------------------------------------------------------- image readers
def test_e01_reads_back_exactly(disk: Path, e01: Path) -> None:
    raw = disk.read_bytes()
    with image_mod.open_image(e01) as im:
        assert im.size == len(raw)
        assert im.md5 == hashlib.md5(raw).hexdigest()
        assert im.header["case_number"] == "C-1" and im.header["serial"] == "SN1"
        rng = random.Random(1)
        for _ in range(300):
            off = rng.randrange(len(raw))
            n = rng.randrange(1, 300_000)
            assert im.read(off, n) == raw[off:off + n]
        assert hashlib.md5(im.read(0, im.size)).hexdigest() == hashlib.md5(raw).hexdigest()


@pytest.mark.skipif(not shutil.which("ewfacquire"), reason="libewf ewfacquire not installed")
@pytest.mark.parametrize("fmt,compression", [("encase6", "deflate:fast"), ("encase5", "none"), ("encase2", "none"), ("ftk", "deflate:best")])
def test_libewf_images(disk: Path, tmp_path: Path, fmt: str, compression: str) -> None:
    target = tmp_path / "lib"
    subprocess.run(["ewfacquire", "-q", "-u", "-t", str(target), "-f", fmt, "-c", compression, "-S", "20MiB",
                    "-C", "CASE-9", "-e", "Examiner", "-m", "fixed", "-M", "physical", str(disk)],
                   check=True, capture_output=True)
    raw = disk.read_bytes()
    with image_mod.open_image(tmp_path / "lib.E01") as im:
        assert hashlib.md5(im.read(0, im.size)).hexdigest() == hashlib.md5(raw).hexdigest()
        assert im.header.get("case_number") == "CASE-9"


def test_raw_split(disk: Path, tmp_path: Path) -> None:
    raw = disk.read_bytes()
    piece = 10 * 1024 * 1024
    for i in range(0, len(raw), piece):
        (tmp_path / f"img.{i // piece + 1:03d}").write_bytes(raw[i:i + piece])
    with image_mod.open_image(tmp_path / "img.001") as im:
        assert im.size == len(raw)
        assert im.read(piece - 100, 300) == raw[piece - 100:piece + 200]


def test_not_an_image(tmp_path: Path) -> None:
    p = tmp_path / "bad.E01"
    p.write_bytes(b"hello" * 100)
    with pytest.raises(image_mod.ImageError):
        image_mod.open_image(p)


# ---------------------------------------------------------------- registry
def test_hive_parser() -> None:
    data = h.build(h.key("ROOT", {}, h.key("A", {"s": h.sz("x" * 40), "d": h.dword(7), "m": h.multi("p", "q"),
                                                  "big": h.binary(bytes(range(256)) * 10)}, ts=fixtures.ft(1_700_000_000))))
    hive = Hive(data)
    k = hive.key("a")
    assert k.value("S") == "x" * 40 and k.value("d") == 7 and k.value("m") == ["p", "q"]
    assert k.value("big") == bytes(range(256)) * 10
    assert k.last_written == 1_700_000_000
    if shutil.which("hivexget"):  # an independent reader agrees the generated hive is well-formed
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".hive", delete=False) as f:
            f.write(data)
        out = subprocess.run(["hivexget", f.name, "A", "s"], capture_output=True, text=True, check=True).stdout
        assert out.strip() == "x" * 40


# ---------------------------------------------------------------- triage
def test_partitions_and_encryption(e01: Path) -> None:
    r = _triage(e01)
    assert r["partition_scheme"] == "GPT"
    assert [v["fs"] for v in r["volumes"]][-1] == "BitLocker"
    assert any("BitLocker" in f["title"] for f in r["findings"])


@pytest.mark.skipif(not fixtures.can_build_ntfs(), reason="needs mkntfs, ntfs-3g and root to build the NTFS fixture")
def test_windows(e01: Path) -> None:
    r = _triage(e01)
    win = next(s for s in r["systems"] if s["kind"] == "windows")
    assert win["os"]["name"] == "Windows 11 Pro"  # build 22631 overrides the "Windows 10" ProductName
    assert win["os"]["build"] == "22631.3880"
    assert win["os"]["installed"] == "2023-11-14 22:13:20 UTC"
    dev = win["device"]
    assert dev["computer_name"] == "DESKTOP-TRIAGE"
    assert dev["model"] == "Latitude 7420" and dev["manufacturer"] == "Dell Inc."
    assert dev["ip_addresses"] == ["192.168.1.50", "10.0.0.5"]
    assert dev["utc_offset"] == "UTC+01:00"

    users = {u["username"]: u for u in win["users"]}
    assert set(users) == {"alice", "bob", "carol", "Administrator"}
    assert users["alice"]["full_name"] == "Alice Archer" and users["alice"]["logon_count"] == 42
    assert users["carol"]["account"] == "Microsoft Entra ID"
    assert users["Administrator"]["disabled"] is True and users["Administrator"]["last_logon"] is None
    assert users["alice"]["recent_doc"]["name"] == "notes.txt"

    # newest user file wins; AppData, desktop.ini, NTUSER.DAT and \Windows are ignored even though newer
    assert win["last_saved"]["path"] == "\\Users\\bob\\Pictures\\photo.jpg"
    assert win["last_saved"]["modified"] == "2025-01-04 14:13:20 UTC"
    assert users["alice"]["last_saved"]["path"] == "\\Users\\alice\\Desktop\\notes.txt"
    assert not any("AppData" in f["path"] or f["name"] == "desktop.ini" for f in win["recent_files"])


def test_linux(e01: Path) -> None:
    r = _triage(e01)
    lin = next(s for s in r["systems"] if s["kind"] == "linux")
    assert lin["os"]["name"] == "Ubuntu 24.04.1 LTS"
    assert lin["os"]["kernel_versions"] == ["6.8.0-45-generic"]
    assert lin["device"]["computer_name"] == "ubuntu-box"
    assert lin["device"]["time_zone"] == "Europe/London"  # via the /etc/localtime symlink
    users = {u["username"]: u for u in lin["users"]}
    assert set(users) == {"root", "dave"}  # daemon / nobody are service accounts
    assert users["dave"]["last_logon"] == "2025-01-16 03:00:00 UTC"
    assert lin["last_saved"]["path"] == "/home/dave/projects/plan.md"  # .cache is skipped


def test_cancel(e01: Path) -> None:
    ev = threading.Event()
    ev.set()
    with pytest.raises(TriageCancelled):
        run_triage(str(e01), lambda p: None, ev)
