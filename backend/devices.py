"""Enumerate attached storage devices and open them read-only for imaging.

Windows is the primary target (\\\\.\\PhysicalDriveN / \\\\.\\E:), with Linux (/dev/sdX, /dev/nvme0n1)
and macOS (/dev/rdiskN) also supported. A regular file (e.g. an existing .dd) can be used as a source too.
"""
from __future__ import annotations

import ctypes
import json
import os
import plistlib
import struct
import subprocess
import sys
import threading
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return False
    return os.geteuid() == 0


def _run(cmd: list[str], timeout: float = 20) -> str:
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
    return proc.stdout


# ---------------------------------------------------------------- device listing

def list_devices() -> list[dict]:
    """Return physical disks (and their partitions/volumes) as flat dicts.

    Each entry: path, name, model, serial, size, sector_size, removable, kind (disk|partition|volume),
    parent, mountpoints, system (True if it holds the running OS — imaging it is allowed but flagged).
    """
    if IS_WINDOWS:
        return _list_windows()
    if IS_MAC:
        return _list_mac()
    return _list_linux()


def _read_sys(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return default


def _linux_mounts() -> dict[str, list[str]]:
    mounts: dict[str, list[str]] = {}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith("/dev/"):
                    mounts.setdefault(os.path.realpath(parts[0]), []).append(parts[1].replace("\\040", " "))
    except OSError:
        pass
    return mounts


def _list_linux() -> list[dict]:
    mounts = _linux_mounts()
    devices = []
    sys_block = Path("/sys/block")
    if not sys_block.is_dir():
        return devices
    for dev in sorted(sys_block.iterdir()):
        name = dev.name
        if name.startswith(("ram", "zram", "loop", "fd", "sr")):
            continue
        size = int(_read_sys(dev / "size", "0")) * 512  # sysfs always counts 512-byte units
        if size == 0:
            continue
        sector = int(_read_sys(dev / "queue" / "logical_block_size", "512") or 512)
        vendor = _read_sys(dev / "device" / "vendor")
        model = " ".join(x for x in (vendor, _read_sys(dev / "device" / "model")) if x) or name
        serial = _read_sys(dev / "device" / "serial") or _read_sys(dev / "serial")
        removable = _read_sys(dev / "removable") == "1"

        parts = []
        for child in sorted(dev.iterdir()):
            if (child / "partition").exists():
                pdev = f"/dev/{child.name}"
                mps = mounts.get(pdev, [])
                parts.append({
                    "path": pdev,
                    "name": child.name,
                    "model": model,
                    "serial": serial,
                    "size": int(_read_sys(child / "size", "0")) * 512,
                    "sector_size": sector,
                    "removable": removable,
                    "kind": "partition",
                    "parent": f"/dev/{name}",
                    "mountpoints": mps,
                    "system": any(m in ("/", "/boot", "/boot/efi") for m in mps),
                })
        disk_mps = mounts.get(f"/dev/{name}", [])
        devices.append({
            "path": f"/dev/{name}",
            "name": name,
            "model": model,
            "serial": serial,
            "size": size,
            "sector_size": sector,
            "removable": removable,
            "kind": "disk",
            "parent": None,
            "mountpoints": disk_mps,
            "system": any(p["system"] for p in parts) or "/" in disk_mps,
        })
        devices.extend(parts)
    return devices


def _powershell_json(cmd: str) -> list[dict]:
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd + " | ConvertTo-Json -Compress"])
    if not out.strip():
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def _list_windows() -> list[dict]:
    devices = []
    system_drive = os.environ.get("SystemDrive", "C:").upper()

    # Map each physical disk to the drive letters on it, so the disk holding the OS can be flagged.
    letters_by_disk: dict[int, list[str]] = {}
    try:
        for row in _powershell_json(
            "Get-Partition | Where-Object DriveLetter | Select-Object DiskNumber,DriveLetter"
        ):
            letters_by_disk.setdefault(int(row["DiskNumber"]), []).append(f"{row['DriveLetter']}:")
    except Exception:  # noqa: BLE001  (Get-Partition needs Windows 8+; the flag is a nicety)
        pass

    try:
        disks = _powershell_json(
            "Get-CimInstance Win32_DiskDrive | Select-Object Index,DeviceID,Model,SerialNumber,Size,"
            "BytesPerSector,InterfaceType,MediaType"
        )
    except Exception:  # noqa: BLE001
        disks = []
    for d in sorted(disks, key=lambda x: x.get("Index") or 0):
        idx = int(d.get("Index") or 0)
        letters = letters_by_disk.get(idx, [])
        media = (d.get("MediaType") or "").lower()
        devices.append({
            "path": d.get("DeviceID") or f"\\\\.\\PHYSICALDRIVE{idx}",
            "name": f"PhysicalDrive{idx}",
            "model": (d.get("Model") or "").strip(),
            "serial": (d.get("SerialNumber") or "").strip(),
            "size": int(d.get("Size") or 0),  # WMI rounds down to whole cylinders; exact size is read on open
            "sector_size": int(d.get("BytesPerSector") or 512),
            "removable": "removable" in media or (d.get("InterfaceType") or "").upper() == "USB",
            "kind": "disk",
            "parent": None,
            "mountpoints": letters,
            "system": system_drive in letters,
            "bus": d.get("InterfaceType") or "",
        })

    try:
        vols = _powershell_json(
            "Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -in 2,3 } | "
            "Select-Object DeviceID,VolumeName,Size,DriveType,FileSystem"
        )
    except Exception:  # noqa: BLE001
        vols = []
    for v in vols:
        letter = v.get("DeviceID") or ""
        devices.append({
            "path": f"\\\\.\\{letter}",
            "name": f"{letter} {v.get('VolumeName') or ''}".strip(),
            "model": v.get("FileSystem") or "",
            "serial": "",
            "size": int(v.get("Size") or 0),
            "sector_size": 512,
            "removable": v.get("DriveType") == 2,
            "kind": "volume",
            "parent": None,
            "mountpoints": [letter],
            "system": letter.upper() == system_drive,
        })
    return devices


def _list_mac() -> list[dict]:
    devices = []
    try:
        listing = plistlib.loads(_run(["diskutil", "list", "-plist"]).encode())
    except Exception:  # noqa: BLE001
        return devices
    for disk in listing.get("AllDisksAndPartitions", []):
        ident = disk.get("DeviceIdentifier")
        if not ident:
            continue
        try:
            info = plistlib.loads(_run(["diskutil", "info", "-plist", ident]).encode())
        except Exception:  # noqa: BLE001
            info = {}
        parts = disk.get("Partitions", []) or disk.get("APFSVolumes", [])
        mps = [p.get("MountPoint") for p in parts if p.get("MountPoint")]
        devices.append({
            "path": f"/dev/r{ident}",  # raw node — much faster than the buffered /dev/diskN
            "name": ident,
            "model": info.get("MediaName") or info.get("IORegistryEntryName") or ident,
            "serial": "",
            "size": int(disk.get("Size") or info.get("TotalSize") or 0),
            "sector_size": int(info.get("DeviceBlockSize") or 512),
            "removable": bool(info.get("RemovableMediaOrExternalDevice") or info.get("Removable")),
            "kind": "disk",
            "parent": None,
            "mountpoints": mps,
            "system": "/" in mps or bool(info.get("SystemImage")),
        })
        for p in parts:
            pid = p.get("DeviceIdentifier")
            if not pid:
                continue
            devices.append({
                "path": f"/dev/r{pid}",
                "name": pid,
                "model": p.get("VolumeName") or p.get("Content") or "",
                "serial": "",
                "size": int(p.get("Size") or 0),
                "sector_size": 512,
                "removable": devices[-1]["removable"] if devices else False,
                "kind": "partition",
                "parent": f"/dev/r{ident}",
                "mountpoints": [p["MountPoint"]] if p.get("MountPoint") else [],
                "system": p.get("MountPoint") == "/",
            })
    return devices


# ---------------------------------------------------------------- raw source

def _win_ioctl(handle: int, code: int, out_size: int) -> bytes:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(out_size)
    returned = wintypes.DWORD(0)
    ok = kernel32.DeviceIoControl(
        wintypes.HANDLE(handle), wintypes.DWORD(code), None, 0, buf, out_size, ctypes.byref(returned), None
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return buf.raw[: returned.value]


class RawSource:
    """Read-only, positional access to a device or image file.

    Reads go straight to the OS with no Python-level buffering. On POSIX they use pread so a failed read
    never leaves the file position in an unknown state; on Windows (no pread) seek+read is serialised.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.sector_size = 512
        self.is_device = False
        self._lock = threading.Lock()
        if IS_WINDOWS:
            self._f = open(path, "rb", buffering=0)  # noqa: SIM115
            self.fd = self._f.fileno()
            self.is_device = path.startswith("\\\\.\\")
            if self.is_device:
                import msvcrt

                handle = msvcrt.get_osfhandle(self.fd)
                self.size = struct.unpack("<q", _win_ioctl(handle, 0x0007405C, 8))[0]  # IOCTL_DISK_GET_LENGTH_INFO
                try:
                    geo = _win_ioctl(handle, 0x00070000, 24)  # IOCTL_DISK_GET_DRIVE_GEOMETRY
                    self.sector_size = struct.unpack_from("<I", geo, 20)[0] or 512
                except OSError:
                    pass
            else:
                self.size = os.fstat(self.fd).st_size
        else:
            self._f = None
            self.fd = os.open(path, os.O_RDONLY)
            st = os.fstat(self.fd)
            import stat as stat_mod

            self.is_device = stat_mod.S_ISBLK(st.st_mode) or stat_mod.S_ISCHR(st.st_mode)
            if not self.is_device:
                self.size = st.st_size
            elif IS_MAC:
                import fcntl

                bs = struct.unpack("<I", fcntl.ioctl(self.fd, 0x40046418, b"\0" * 4))[0]  # DKIOCGETBLOCKSIZE
                count = struct.unpack("<Q", fcntl.ioctl(self.fd, 0x40086419, b"\0" * 8))[0]  # DKIOCGETBLOCKCOUNT
                self.sector_size, self.size = bs, bs * count
            else:
                self.size = os.lseek(self.fd, 0, os.SEEK_END)
                try:
                    import fcntl

                    self.sector_size = struct.unpack("<i", fcntl.ioctl(self.fd, 0x1268, b"\0" * 4))[0]  # BLKSSZGET
                except OSError:
                    pass
            if hasattr(os, "posix_fadvise"):
                try:
                    os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
                except OSError:
                    pass

    def pread(self, length: int, offset: int) -> bytes:
        """Read exactly `length` bytes at `offset`, raising OSError on a read error or unexpected EOF."""
        if IS_WINDOWS:
            with self._lock:
                self._f.seek(offset)
                data = self._f.read(length)
                while data is not None and len(data) < length:
                    more = self._f.read(length - len(data))
                    if not more:
                        break
                    data += more
        else:
            data = os.pread(self.fd, length, offset)
            while len(data) < length:
                more = os.pread(self.fd, length - len(data), offset + len(data))
                if not more:
                    break
                data += more
        if data is None or len(data) < length:
            raise OSError(f"Short read at offset {offset}: got {0 if data is None else len(data)} of {length} bytes")
        return data

    def drop_cache(self, offset: int, length: int) -> None:
        if not IS_WINDOWS and hasattr(os, "posix_fadvise"):
            try:
                os.posix_fadvise(self.fd, offset, length, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
        else:
            os.close(self.fd)

    def __enter__(self) -> "RawSource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_source(path: str) -> RawSource:
    return RawSource(path)


def source_info(path: str) -> dict:
    with open_source(path) as src:
        return {"path": path, "size": src.size, "sector_size": src.sector_size, "is_device": src.is_device}
