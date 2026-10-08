# Quick Triage

A desktop application for **quick triage of E01 images**. Open an image and within seconds you see:

- **OS**: name, edition, version and build, install date, registered owner (Windows), or distribution, version
  and installed kernels (Linux).
- **Device**: computer name, domain, manufacturer and model, BIOS, IP addresses, time zone and last shutdown.
- **System users**: every local account (SAM) and profile (including domain and Microsoft Entra ID accounts),
  with SID, last logon, logon count and disabled state. On Linux, login accounts from `/etc/passwd` with their
  last login from `wtmp`.
- **Last saved file**: the most recently modified file in a user folder, overall and per user, plus a short list
  of recent files. On Windows each user's newest RecentDocs entry is shown too.

It's built for speed rather than completeness. **It never indexes or hashes the whole image.** It reads the
partition table, the registry hives and one sequential pass over the MFT. On Linux it reads a few files in
`/etc` and walks the home folders, capped at 200,000 entries.

It's the companion to [Quick Capture](../README.md) and works the same way. It opens in its own native window
through [pywebview](https://pywebview.flowrl.com/). **No web server runs and no network port is opened.** It
uses the same vanilla HTML/CSS/JS style kit, has no build step, and needs only the Python standard library
plus pywebview. libewf, pytsk and other native libraries aren't required.

## Requirements

- Python 3.10+
- A system web engine (see Quick Capture's README): Edge WebView2 on Windows; nothing extra on macOS; GTK +
  WebKit2GTK or `pywebview[qt]` on Linux.
- **No Administrator or root needed**: it only reads image files.

## Setup and run

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py              # Linux/macOS: .venv/bin/python app.py
```

Pass `--debug` to enable the web inspector.

## Build a standalone app

```bash
python -m pip install pyinstaller
python -m PyInstaller --clean QuickTriage.spec
```

This builds `dist/QuickTriage.exe` (`dist/QuickTriage` on Linux/macOS). On Linux, copy it with
`quicktriage.png` to `/opt/QuickTriage/` and install `quick-triage.desktop` into `~/.local/share/applications/`.
The icon files are currently copies of Quick Capture's icon. To use a different one, replace them and rebuild.

## Workflow

1. **Image**: type a path or click **Browse…**, then pick the first segment (`.E01`). The rest of the set
   (`.E02` … `.EAA` …) is found automatically. Raw `dd` / `.img` / `.raw` files and split `.001` sets also
   work. The status line shows the size and segment count. Badges show the acquisition header: case,
   examiner, source drive and acquired date.
2. **Triage**: one click. Progress shows which partition is being read.
3. **Results**: summary cards (OS, device, users, last saved), then the system, users and last-saved panels.
   Findings list encrypted volumes, damaged chunks and anything that wasn't examined. The image panel shows
   the stored hashes, acquisition header and partition table. A dual-boot image gets one tab per OS.
4. **View Report** opens the HTML report in its own window. **Save HTML** / **Save JSON** ask where to save it.

## What it reads

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

### Formats

- **E01**: EnCase 1–7, FTK, linen and libewf output, compressed or not, any number of segments. Ex01 (EWF2)
  isn't supported yet. Chunks are decompressed on demand and cached, so only the parts triage reads are ever
  inflated. The stored MD5/SHA-1 are shown as recorded. They are **not** recomputed.
- **Raw**: single files or `.001`, `.002` … sets.
- **Partitions**: GPT, MBR (including extended/logical partitions), or a volume image with no partition table.
- **Filesystems examined**: NTFS and ext2/3/4. BitLocker and LUKS are detected and flagged as encrypted. APFS,
  HFS+, XFS, Btrfs, ReFS and LVM are named but not examined.

## Forensic notes

- Images are opened **read-only**, and nothing is written next to them unless you save a report.
- Times are shown in **UTC**. The system's own time zone and UTC offset are in the Device panel.
- "Last saved" is the `$STANDARD_INFORMATION` modified time, the one Explorer shows. It can be changed by
  timestomping, so confirm anything important with a full examination.
- Registry hives are read without replaying their `.LOG1`/`.LOG2` transaction logs. Values changed moments
  before shutdown may be newer in the logs.
- NTFS-compressed or EFS-encrypted hives can't be read, and are reported as such.

## Tests

```bash
python -m pytest tests
```

The tests build a GPT disk with Windows (NTFS + generated registry hives), Linux (ext4) and BitLocker
partitions. They write it as E01 with Quick Capture's writer and check every field above. If libewf's
`ewfacquire` is installed, its EnCase 2/5/6 and FTK output is read back byte-for-byte too. The NTFS part needs
`mkntfs`, `ntfs-3g` and root (FUSE). Without them those checks are skipped.

## Project structure

```
quick-triage/
├── backend/
│   ├── api.py       the methods the window calls (window.pywebview.api.*)
│   ├── jobs.py      background triage jobs (one thread each, polled by the page)
│   ├── triage.py    the triage itself: image → partitions → OS volumes → findings
│   ├── image.py     random-access E01 and raw readers
│   ├── volumes.py   GPT / MBR parsing and filesystem detection
│   ├── ntfs.py      read-only NTFS: records, runs, attribute lists, $I30 indexes, MFT sweep
│   ├── regf.py      read-only registry hive parser
│   ├── windows.py   OS, device, users and last saved file from a Windows volume
│   ├── ext.py       read-only ext2/3/4: inodes, extents, block maps, directories, symlinks
│   ├── linux.py     OS, device, users and last saved file from a Linux root volume
│   └── report.py    HTML and JSON reports
├── frontend/        vanilla HTML/CSS/JS UI; styles.css + fonts/ are the shared tool style kit
├── tests/           pytest tests plus fixture builders (hivegen.py, fixtures.py)
├── app.py           entry point: opens the native window (no server, no port)
├── QuickTriage.spec PyInstaller one-file build
└── requirements.txt
```

## License

MIT — see [LICENSE](../LICENSE).
