# Quick Capture

A local web app for **fast forensic imaging** of a disk, partition or volume to **E01** (EnCase 6) or **DD**
(raw). It checks the device for bad sectors before imaging. A healthy drive is imaged straight away. If the
drive has problems, you're alerted and asked whether to continue.

Built in the same style as [Frame Guard](../README.md): a FastAPI backend and a vanilla HTML/CSS/JS frontend,
with no build step.

## Requirements

- Python 3.10+
- **Administrator (Windows) or root (Linux/macOS)**, needed to open physical devices
- Optional: [`smartmontools`](https://www.smartmontools.org/) (`smartctl`) for SMART health checks during the
  scan. Without it, the scan relies on reading sectors alone.

## Setup

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

From an **elevated** command prompt:

```bash
.venv\Scripts\python.exe run.py
```

Then open http://localhost:8757. It uses a different port from Frame Guard, so both can run at once.

## Workflow

The page has three boxes: **Source** and **Scan** side by side, and **Image details** below them.

1. **Source**: choose a device from the list, or type a path. The **Physical / Logical** switch filters the
   list: physical shows whole drives, logical shows partitions and volumes (drive letters on Windows).
   Removable and OS drives are badged. You can type paths such as `\\.\PhysicalDrive1`, `\\.\E:`,
   `/dev/sdb` or `/dev/rdisk2`, or pick an existing image file.
2. **Scan** (optional): choose Quick, Thorough or Full surface.
   - The **Scan** button in the box runs just the check, which is handy for sorting a pile of drives.
   - Tick **Scan automatically before imaging** (off by default) to have the **Image** button scan first.
   - The scan reads SMART data (reallocated, pending and uncorrectable sectors, NVMe media errors), runs a
     short sequential read to estimate imaging time, then reads sectors.
   - If the result needs **attention** (unreadable sectors or SMART defects), imaging pauses and shows what
     was found. You choose **Image Anyway** or **Abort**.
3. **Image details**: the output folder and image name, then the format (E01 or DD), E01 compression, split
   size, read block size, reads in flight, hash algorithms and verify.
4. **Image**: when it finishes you get the hashes, speed and duration, a report (HTML/JSON), and an
   acquisition log (`<name>.txt`) written next to the image.

### Scan modes

| Mode | What it reads | Typical time |
|---|---|---|
| Quick (default) | SMART + 512 samples spread evenly across the whole LBA range | seconds |
| Thorough | SMART + 8,192 samples | under a minute on an HDD |
| Full surface | every sector, with no hashing or writing | about as long as imaging |

Sampled scans can miss isolated bad sectors, but SMART usually flags a drive that's degrading. The imager
does not stop or retry on bad sectors either way (see below).

## Why it's fast

- **Several reads in flight.** The reader keeps two 8 MiB reads queued at the drive by default, each on its
  own OS handle, so the drive never sits idle between requests. On this test VM going from one read in
  flight to two gave about 50% more read throughput. Try 4 or 8 for NVMe ("Reads in flight").
- **One read, fanned out.** Each
  block goes to one thread per hash algorithm and to the writer through bounded queues. `hashlib` and `zlib`
  release the GIL, so MD5, SHA-1, SHA-256 and compression run on separate cores at the same time as the read.
- **Hashes are computed during acquisition**, so there's no second pass. Verify-after is optional.
- **Parallel E01 compression.** Chunks are compressed on a thread pool. The next block compresses while the
  previous one is written, and each block's chunks go to disk in one gathered write rather than hundreds of
  small ones.
- **Cheap handling of empty and encrypted data.** All-zero chunks are compressed once and reused. In "fast"
  mode, chunks that look incompressible (BitLocker/FileVault volumes, media files) are detected from a 2 KiB
  sample and stored raw. A full zlib attempt on that kind of data runs at about 60 MB/s per core.
- **No retries on bad sectors.** A block that fails to read is re-read in 64 KiB pieces, then one sector at a
  time. Sectors that still fail are zero-filled and logged, and recorded in the E01 `error2` section. If a
  long run fails outright (the device dropped off the bus), the job aborts instead of crawling.

Measured on a 4-core cloud VM, reading from and writing to RAM so the pipeline is the only limit:

| Output | Hashes | Throughput |
|---|---|---|
| DD | MD5 + SHA-1 | ~600 MB/s |
| E01, fast | MD5 + SHA-1 | ~580 MB/s |
| E01, fast | SHA-1 only | ~840 MB/s |
| DD | SHA-256 only | ~820 MB/s |

**Every run reports what limited it.** The results card and report show "Limited by": the source drive,
MD5/SHA hashing, or E01 compression plus the destination write. That tells you which setting (or which
hardware) to change. If it says the source drive, no tool can image faster. That is also why FTK Imager
ends up close on the same drive.

**MD5 is the ceiling.** It can't be parallelised and runs at about 600–900 MB/s per core, depending on the
CPU. That is faster than any HDD, SATA SSD or USB 3 bridge, so in practice the source drive is the limit. For
NVMe sources, untick MD5 and use SHA-1 or SHA-256 alone (both are hardware-accelerated on modern CPUs).

## Output formats

- **E01**: a native writer (`backend/ewf.py`) that produces the same section layout as libewf's
  `ewfacquire -f encase6`. It has 32 KiB chunks, the header/header2 case metadata, per-chunk Adler-32 or zlib
  checksums, an MD5 `hash` section, an MD5+SHA-1 `digest` section and an `error2` bad-sector list. It supports
  segment splitting (`.E01`, `.E02`, …, `.EAA`). Output is checked with libewf's `ewfverify` in the test suite.
  E01 can only embed MD5 and SHA-1. Any other hash is recorded in the log and report.
- **DD**: a raw image. It is written as `<name>.dd`, or as `<name>.001`, `.002`, … when a split size is set.

Existing files are never overwritten. If a run is cancelled or fails, its partial image is deleted.

## Forensic notes

- Sources are opened **read-only**, and nothing is ever written to them. A hardware write blocker is still
  recommended.
- Imaging the disk that holds the running OS is allowed but flagged, because its contents change while it is
  being read.
- Unreadable sectors are zero-filled (EnCase's behaviour too). For a genuinely failing drive, use an
  error-tolerant, multi-pass tool such as `ddrescue`.

## Project structure

```
quick-capture/
├── backend/
│   ├── main.py           FastAPI app & routes
│   ├── devices.py        device enumeration (Windows/Linux/macOS) and read-only raw access
│   ├── triage.py         the scan: SMART, speed probe, sampled / full read
│   ├── imager.py         threaded read → hash → write pipeline, bad-sector handling, verification
│   ├── ewf.py            E01 (EnCase 6) writer + reader
│   ├── jobs.py           background job manager (scan → decision → image → verify)
│   ├── report.py         HTML/JSON report and the .txt acquisition log
│   └── file_browser.py   server-side directory listing for the folder/file pickers
├── frontend/             vanilla HTML/CSS/JS UI
├── tests/                pytest round-trip tests (python -m pytest tests)
├── run.py                entry point (uvicorn, port 8757)
└── requirements.txt
```

## License

MIT — see [LICENSE](../LICENSE).
