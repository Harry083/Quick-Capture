# Quick Capture

A desktop application for **fast forensic imaging** of a disk, partition or volume to **E01** (EnCase 6) or
**DD** (raw). It checks the device for bad sectors before imaging. A healthy drive is imaged straight away. If
the drive has problems, you're alerted and asked whether to continue.

A second tab, **Triage**, opens any E01 or raw image, whether Quick Capture made it or not. Within seconds it
shows the **OS**, the **device** it came from, the **system users** and the **last file each user saved**. It
reads metadata only and never indexes or hashes the whole image. See [Triage](#triage) below.

It opens in its own native window, using the operating system's web engine through
[pywebview](https://pywebview.flowrl.com/) (Edge WebView2 on Windows, WebKit on macOS, WebKitGTK or Qt on
Linux). **No web server runs and no network port is opened**: the window's JavaScript calls the Python
backend directly. The UI is the same vanilla HTML/CSS/JS in the same style as
[Frame Guard](../README.md), with no build step.

## Requirements

- Python 3.10+
- **Administrator (Windows) or root (Linux/macOS)**, needed to open physical devices
- A system web engine:
  - Windows 10/11: Edge WebView2, which is already installed.
  - macOS: nothing extra.
  - Linux: GTK and WebKit2GTK (e.g. `sudo apt install python3-gi gir1.2-webkit2-4.1`), or
    `pip install "pywebview[qt]"`.
- Optional: [`smartmontools`](https://www.smartmontools.org/) (`smartctl`) for SMART health checks during the
  scan. Without it, the scan relies on reading sectors alone.

## Setup

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run from source

```bash
.venv\Scripts\python.exe app.py
```

On Windows, if you aren't already elevated, Quick Capture relaunches itself through the UAC prompt. If you
decline, it still opens, but shows a banner saying physical devices can't be opened. Pass `--no-elevate` to
skip the prompt, or `--debug` to enable the web inspector.

On Linux/macOS, start it as root:

```bash
sudo -E .venv/bin/python app.py      # -E keeps DISPLAY / XAUTHORITY so the window can open
```

**Browse…** opens the operating system's own file or folder dialog, attached to the app window.

## Build a standalone app

```bash
python -m pip install pyinstaller
python -m PyInstaller --clean QuickCapture.spec
```

This builds a single file, `dist/QuickCapture.exe` (`dist/QuickCapture` on Linux/macOS), with the Quick Capture
icon. It runs on another machine without Python installed. Each launch unpacks the app to a temp folder first,
so it takes a second or two to open.

- **Windows:** run `QuickCapture.exe`. Its manifest requests Administrator, so UAC prompts on every launch.
  Pin it to the Start menu or taskbar like any other program.
- **macOS:** raw disk access still needs root, so launch it with `sudo dist/QuickCapture`.
- **Linux:** copy `dist/QuickCapture` and `quickcapture.png` to `/opt/QuickCapture/`, then install
  `quick-capture.desktop` into `~/.local/share/applications/`. It launches through `pkexec`, which asks for
  the root password.

The icon lives in `quickcapture.ico` (every Windows size, 16–256 px) and `quickcapture.png` (1024 px). To use a
different one, replace those two files and rebuild.

PyInstaller builds for the OS it runs on, so build the Windows `.exe` on Windows.

## Workflow

The two tabs under the title switch between **Capture** (imaging a device) and **Triage** (examining an
image). The app remembers which tab you used last.

### Capture

The Capture tab has three boxes: **Source** and **Scan** side by side, and **Image details** below them.

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
4. **Image**: when it finishes you get the hashes, speed and duration, and an acquisition log
   (`<name>.txt`) written next to the image. **View Report** opens the HTML report in its own window.
   **Save HTML** / **Save JSON** ask where to save it.

When it finishes, **Triage this image** opens the new image in the Triage tab and starts straight away.

### Scan modes

| Mode | What it reads | Typical time |
|---|---|---|
| Quick (default) | SMART + 512 samples spread evenly across the whole LBA range | seconds |
| Thorough | SMART + 8,192 samples | under a minute on an HDD |
| Full surface | every sector, with no hashing or writing | about as long as imaging |

Sampled scans can miss isolated bad sectors, but SMART usually flags a drive that's degrading. The imager
does not stop or retry on bad sectors either way (see below).

## Triage

The Triage tab is for a fast first look at an image. It only reads image files, so it works even if you
declined the Administrator prompt.

1. **Image**: type a path or click **Browse…**, then pick the first segment (`.E01`). The rest of the set
   (`.E02` … `.EAA` …) is found automatically. Raw `dd` / `.img` / `.raw` files and split `.001` sets also
   work, from any tool. The status line shows the size and segment count. Badges show the acquisition header:
   case, examiner, source drive and acquired date.
2. **Triage**: one click. Progress shows which partition is being read.
3. **Results**: summary cards (OS, device, users, last saved), then the system, users and last-saved panels.
   Findings list encrypted volumes, damaged chunks and anything that wasn't examined. The image panel shows
   the stored hashes, acquisition header and partition table. A dual-boot image gets one tab per OS.
   **View Report** / **Save HTML** / **Save JSON** work as they do for an acquisition.

### What it reads

| Source | Windows (NTFS) | Linux (ext2/3/4) |
|---|---|---|
| OS | `SOFTWARE\Microsoft\Windows NT\CurrentVersion` (build ≥ 22000 is reported as Windows 11) | `/etc/os-release`, `/etc/lsb-release`, `/boot/vmlinuz-*` |
| Device | `SYSTEM`: ComputerName, Tcpip, TimeZoneInformation, Windows\ShutdownTime, SystemInformation / HardwareConfig | `/etc/hostname`, `/etc/timezone` or the `/etc/localtime` link, superblock mount/write times |
| Users | `SAM` (names, F and V records), `SOFTWARE\...\ProfileList`, each `NTUSER.DAT` RecentDocs | `/etc/passwd` (uid 0 and 1000+), `/var/log/wtmp` |
| Last saved | one pass over the MFT: `$STANDARD_INFORMATION` modified time, under `\Users\<name>\` | walk of each home folder: inode modified time |

**Noise is filtered out.** That covers anything under `AppData`, `Default` / `All Users` profiles, dot-files and
dot-folders on Linux, and files that the OS or apps rewrite on their own: `NTUSER.DAT*`, `desktop.ini`,
`thumbs.db`, Office `~$` lock files, `.tmp`, transaction logs. What's left is what a user saved.

The MFT pass keeps only folder names and the newest three files per folder, so memory grows with the number
of folders, not files. It runs at about 250,000 records per second, so a typical Windows volume takes a few
seconds.

### Image formats

- **E01**: EnCase 1–7, FTK, linen and libewf output, compressed or not, any number of segments. Ex01 (EWF2)
  isn't supported yet. Chunks are decompressed on demand and cached, so only the parts triage reads are ever
  inflated. The stored MD5/SHA-1 are shown as recorded. They are **not** recomputed.
- **Raw**: single files or `.001`, `.002` … sets.
- **Partitions**: GPT, MBR (including extended/logical partitions), or a volume image with no partition table.
- **Filesystems examined**: NTFS and ext2/3/4. BitLocker and LUKS are detected and flagged as encrypted. APFS,
  HFS+, XFS, Btrfs, ReFS and LVM are named but not examined.

### Triage notes

- Images are opened **read-only**, and nothing is written next to them unless you save a report.
- Times are shown in **UTC**. The system's own time zone and UTC offset are in the Device panel.
- "Last saved" is the `$STANDARD_INFORMATION` modified time, the one Explorer shows. It can be changed by
  timestomping, so confirm anything important with a full examination.
- Registry hives are read without replaying their `.LOG1`/`.LOG2` transaction logs. Values changed moments
  before shutdown may be newer in the logs.
- NTFS-compressed or EFS-encrypted hives can't be read, and are reported as such.

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
│   ├── api.py            the methods the window calls (window.pywebview.api.*), with input validation
│   ├── devices.py        device enumeration (Windows/Linux/macOS) and read-only raw access
│   ├── scan.py           the pre-imaging scan: SMART, speed probe, sampled / full read
│   ├── imager.py         threaded read → hash → write pipeline, bad-sector handling, verification
│   ├── ewf.py            E01 (EnCase 6) writer + reader
│   ├── jobs.py           background job manager (scan → decision → image → verify)
│   ├── report.py         HTML/JSON report and the .txt acquisition log
│   ├── file_browser.py   folder lookup (free space) for the output folder
│   │
│   │   Triage tab
│   ├── triage.py         the triage itself: image → partitions → OS volumes → findings
│   ├── triage_jobs.py    background triage jobs (one thread each, polled by the page)
│   ├── triage_report.py  triage HTML/JSON report
│   ├── image.py          random-access E01 and raw readers
│   ├── volumes.py        GPT / MBR parsing and filesystem detection
│   ├── ntfs.py           read-only NTFS: records, runs, attribute lists, $I30 indexes, MFT sweep
│   ├── regf.py           read-only registry hive parser
│   ├── windows.py        OS, device, users and last saved file from a Windows volume
│   ├── ext.py            read-only ext2/3/4: inodes, extents, block maps, directories, symlinks
│   └── linux.py          OS, device, users and last saved file from a Linux root volume
├── frontend/             vanilla HTML/CSS/JS UI (app.js = Capture tab, triage.js = Triage tab);
│                         styles.css + fonts/ are the shared tool style kit
├── tests/                pytest tests (python -m pytest tests); triage tests build a GPT disk with Windows,
│                         Linux and BitLocker partitions (NTFS part needs mkntfs + ntfs-3g + root)
├── app.py                entry point: opens the native window (no server, no port)
├── QuickCapture.spec     PyInstaller one-file build (Windows .exe requests Administrator)
├── quickcapture.ico/.png the app icon
├── quick-capture.desktop Linux menu launcher (via pkexec)
└── requirements.txt
```

## License

MIT — see [LICENSE](../LICENSE).
