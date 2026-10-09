"""Builds a small dual-boot test disk: GPT with a Windows NTFS partition, a Linux ext4 partition and a
BitLocker-looking volume. Needs mkntfs + ntfs-3g (FUSE) for the NTFS part, and mkfs.ext4 for the ext4 part."""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
from pathlib import Path

import hivegen as h

MiB = 1024 * 1024
EPOCH_FT = 116444736000000000

# Fixed timestamps (Unix seconds) so assertions are exact.
T_INSTALL = 1_700_000_000      # 2023-11-14 22:13:20 UTC
T_REPORT = 1_730_000_000       # 2024-10-27
T_NOTES = 1_735_000_000        # 2024-12-24 00:26:40 UTC  -> alice's last saved
T_PHOTO = 1_736_000_000        # 2025-01-04 14:13:20 UTC  -> bob's, and the newest user file
T_NOISE = 1_740_000_000        # newer, but AppData / desktop.ini / Windows: must be ignored
T_LINUX = 1_737_000_000        # 2025-01-16 04:00:00 UTC  -> dave's newest
T_LINUX_HIDDEN = 1_741_000_000

NTFS_SIZE = 48 * MiB
EXT_SIZE = 24 * MiB
BL_SIZE = 2 * MiB
DISK_SIZE = 1 * MiB + NTFS_SIZE + EXT_SIZE + BL_SIZE + 1 * MiB


def ft(t: int) -> int:
    return int(t * 1e7) + EPOCH_FT


def can_build_ntfs() -> bool:
    return bool(shutil.which("mkntfs") and shutil.which("ntfs-3g") and os.path.exists("/dev/fuse")) and os.geteuid() == 0


def can_build_ext() -> bool:
    return bool(shutil.which("mkfs.ext4"))


def hives() -> dict[str, bytes]:
    system = h.build(h.key(
        "ROOT", {},
        h.key("Select", {"Current": h.dword(1)}),
        h.key("ControlSet001", {},
              h.key("Control", {},
                    h.key("ComputerName", {}, h.key("ComputerName", {"ComputerName": h.sz("DESKTOP-TRIAGE")})),
                    h.key("TimeZoneInformation", {"TimeZoneKeyName": h.sz("GMT Standard Time"),
                                                  "ActiveTimeBias": h.dword(0xFFFFFFC4)}),
                    h.key("Windows", {"ShutdownTime": h.binary(struct.pack("<Q", ft(T_NOISE + 100)))}),
                    h.key("SystemInformation", {"SystemManufacturer": h.sz("Dell Inc."),
                                                "SystemProductName": h.sz("Latitude 7420"),
                                                "BIOSVersion": h.sz("1.31.0")}),
                    h.key("Session Manager", {}, h.key("Environment", {"PROCESSOR_ARCHITECTURE": h.sz("AMD64")}))),
              h.key("Services", {},
                    h.key("Tcpip", {},
                          h.key("Parameters", {"Domain": h.sz("corp.local")},
                                h.key("Interfaces", {},
                                      h.key("{1b2c3d4e-0000-0000-0000-000000000001}",
                                            {"DhcpIPAddress": h.sz("192.168.1.50")}),
                                      h.key("{1b2c3d4e-0000-0000-0000-000000000002}",
                                            {"IPAddress": h.multi("10.0.0.5")})))))),
    ))
    software = h.build(h.key(
        "ROOT", {},
        h.key("Microsoft", {},
              h.key("Windows NT", {},
                    h.key("CurrentVersion", {
                        "ProductName": h.sz("Windows 10 Pro"), "EditionID": h.sz("Professional"),
                        "DisplayVersion": h.sz("23H2"), "CurrentBuildNumber": h.sz("22631"),
                        "UBR": h.dword(3880), "InstallDate": h.dword(T_INSTALL),
                        "RegisteredOwner": h.sz("alice"), "CurrentVersion": h.sz("6.3"),
                    },
                        h.key("ProfileList", {},
                              h.key("S-1-5-18", {"ProfileImagePath": h.sz(r"%systemroot%\system32\config\systemprofile")}),
                              h.key("S-1-5-21-111-222-333-1001", {"ProfileImagePath": h.sz(r"C:\Users\alice")}),
                              h.key("S-1-5-21-111-222-333-1002", {"ProfileImagePath": h.sz(r"C:\Users\bob")}),
                              h.key("S-1-12-1-444-555-666-777", {"ProfileImagePath": h.sz(r"C:\Users\carol")})))))))

    def f_value(last_logon: int, logons: int, acb: int) -> bytes:
        f = bytearray(80)
        struct.pack_into("<QQQQ", f, 8, ft(last_logon) if last_logon else 0, ft(T_INSTALL), 0x7FFFFFFFFFFFFFFF, 0)
        struct.pack_into("<H", f, 56, acb)
        struct.pack_into("<HH", f, 64, 0, logons)
        return bytes(f)

    def v_value(username: str, full: str) -> bytes:
        strings = [b"", username.encode("utf-16-le"), full.encode("utf-16-le"), b""]
        table = bytearray(0xCC)
        data = bytearray()
        for i, s in enumerate(strings):
            struct.pack_into("<III", table, 12 * i, len(data), len(s), 0)
            data += s + bytes((-len(s)) % 4)
        return bytes(table + data)

    sam = h.build(h.key(
        "ROOT", {},
        h.key("SAM", {}, h.key("Domains", {}, h.key("Account", {}, h.key(
            "Users", {},
            h.key("Names", {},
                  h.key("Administrator", raw_values=[("", 0x1F4, b"")]),
                  h.key("alice", raw_values=[("", 0x3E9, b"")]),
                  h.key("bob", raw_values=[("", 0x3EA, b"")])),
            h.key("000001F4", {"F": h.binary(f_value(0, 0, 0x0011)), "V": h.binary(v_value("Administrator", ""))}),
            h.key("000003E9", {"F": h.binary(f_value(T_NOTES + 60, 42, 0x0010)), "V": h.binary(v_value("alice", "Alice Archer"))}),
            h.key("000003EA", {"F": h.binary(f_value(T_PHOTO - 60, 7, 0x0010)), "V": h.binary(v_value("bob", ""))}),
        )))),
    ))

    def mru(name: str) -> bytes:
        return (name + "\0").encode("utf-16-le") + b"\x14\x00shellitem"

    ntuser = h.build(h.key(
        "ROOT", {},
        h.key("Software", {}, h.key("Microsoft", {}, h.key("Windows", {}, h.key("CurrentVersion", {}, h.key(
            "Explorer", {}, h.key("RecentDocs", {
                "MRUListEx": h.binary(struct.pack("<4I", 1, 0, 2, 0xFFFFFFFF)),
                "0": h.binary(mru("report.docx")), "1": h.binary(mru("notes.txt")), "2": h.binary(mru("old.pdf")),
            }, ts=ft(T_NOTES)))))))))
    return {"SYSTEM": system, "SOFTWARE": software, "SAM": sam, "NTUSER": ntuser}


def _put(root: Path, rel: str, data: bytes, mtime: int) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))


def build_ntfs(path: Path, work: Path) -> None:
    with open(path, "wb") as f:
        f.truncate(NTFS_SIZE)
    subprocess.run(["mkntfs", "-F", "-Q", "-q", "-p", str(MiB // 512), "-H", "255", "-S", "63", str(path)],
                   check=True, capture_output=True)
    mnt = work / "ntfs-mnt"
    mnt.mkdir()
    subprocess.run(["ntfs-3g", str(path), str(mnt)], check=True, capture_output=True)
    try:
        hv = hives()
        cfg = "Windows/System32/config/"
        for name in ("SYSTEM", "SOFTWARE", "SAM"):
            _put(mnt, cfg + name, hv[name], T_NOISE)
        _put(mnt, "Windows/System32/drivers/etc/hosts", b"127.0.0.1 localhost\n", T_INSTALL)
        _put(mnt, "Windows/Temp/newest.log", b"noise", T_NOISE + 50)
        _put(mnt, "Users/alice/NTUSER.DAT", hv["NTUSER"], T_NOISE)
        _put(mnt, "Users/alice/Documents/report.docx", b"PK report", T_REPORT)
        _put(mnt, "Users/alice/Desktop/notes.txt", b"notes " * 100, T_NOTES)
        _put(mnt, "Users/alice/AppData/Local/Cache/cache.bin", b"cache", T_NOISE)
        _put(mnt, "Users/bob/Pictures/photo.jpg", b"\xff\xd8\xff" + bytes(5000), T_PHOTO)
        _put(mnt, "Users/bob/Desktop/desktop.ini", b"[.ShellClassInfo]", T_NOISE)
        # Enough files to spill directory indexes into INDX blocks and the MFT past a few blocks.
        for i in range(400):
            _put(mnt, f"Users/bob/Documents/archive/file{i:04d}.txt", b"x" * 10, T_REPORT - i)
        for i in range(300):
            _put(mnt, f"Windows/System32/dll{i:04d}.dll", b"MZ", T_INSTALL)
    finally:
        subprocess.run(["umount", str(mnt)], check=False, capture_output=True)


def build_ext(path: Path, work: Path) -> None:
    root = work / "ext-root"
    _put(root, "etc/os-release", b'NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\nVERSION_ID="24.04"\n'
                                 b'VERSION_CODENAME=noble\nID=ubuntu\n', T_INSTALL)
    _put(root, "etc/hostname", b"ubuntu-box\n", T_INSTALL)
    _put(root, "etc/passwd", b"root:x:0:0:root:/root:/bin/bash\n"
                             b"daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
                             b"dave:x:1000:1000:Dave Diaz,,,:/home/dave:/bin/bash\n"
                             b"nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n", T_INSTALL)
    (root / "usr/share/zoneinfo/Europe").mkdir(parents=True)
    (root / "usr/share/zoneinfo/Europe/London").write_bytes(b"TZif")
    os.symlink("/usr/share/zoneinfo/Europe/London", root / "etc/localtime")
    (root / "boot").mkdir()
    (root / "boot/vmlinuz-6.8.0-45-generic").write_bytes(b"k")
    _put(root, "home/dave/projects/plan.md", b"# plan", T_LINUX)
    _put(root, "home/dave/old.txt", b"old", T_REPORT)
    _put(root, "home/dave/.cache/thumb.png", b"noise", T_LINUX_HIDDEN)
    _put(root, "root/.bashrc", b"noise", T_LINUX_HIDDEN)
    wtmp = bytearray(384 * 2)
    for i, (user, t) in enumerate((("dave", T_LINUX - 3600), ("reboot", T_LINUX))):
        struct.pack_into("<h", wtmp, 384 * i, 7 if user != "reboot" else 2)
        wtmp[384 * i + 44:384 * i + 44 + len(user)] = user.encode()
        struct.pack_into("<i", wtmp, 384 * i + 340, t)
    _put(root, "var/log/wtmp", bytes(wtmp), T_LINUX)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-L", "rootfs", "-d", str(root), str(path), f"{EXT_SIZE // 1024}k"],
                   check=True, capture_output=True)


def _gpt(disk: bytearray, parts: list[tuple[str, int, int, str]]) -> None:
    import uuid
    import zlib

    sector = 512
    total = len(disk) // sector
    entries = bytearray(128 * 128)
    for i, (type_guid, first, last, name) in enumerate(parts):
        e = uuid.UUID(type_guid).bytes_le + uuid.uuid4().bytes_le + struct.pack("<QQQ", first, last, 0)
        entries[128 * i:128 * i + 56] = e
        n = name.encode("utf-16-le")[:72]
        entries[128 * i + 56:128 * i + 56 + len(n)] = n
    hdr = bytearray(92)
    hdr[0:8] = b"EFI PART"
    struct.pack_into("<IIII", hdr, 8, 0x10000, 92, 0, 0)
    struct.pack_into("<QQQQ", hdr, 24, 1, total - 1, 34, total - 34)
    hdr[56:72] = uuid.uuid4().bytes_le
    struct.pack_into("<QIII", hdr, 72, 2, 128, 128, zlib.crc32(entries))
    struct.pack_into("<I", hdr, 16, zlib.crc32(hdr))
    disk[sector:sector + 92] = hdr
    disk[2 * sector:2 * sector + len(entries)] = entries
    # protective MBR
    disk[446:462] = struct.pack("<B3sB3sII", 0, b"\0\x02\0", 0xEE, b"\xff\xff\xff", 1, min(total - 1, 0xFFFFFFFF))
    disk[510:512] = b"\x55\xaa"


def build_disk(work: Path) -> Path:
    """Returns a raw disk image. Partitions: 1 NTFS (Windows), 2 ext4 (Linux), 3 BitLocker signature."""
    work.mkdir(parents=True, exist_ok=True)
    disk = bytearray(DISK_SIZE)
    layout = []
    pos = MiB
    if can_build_ntfs():
        nt = work / "ntfs.img"
        build_ntfs(nt, work)
        disk[pos:pos + NTFS_SIZE] = nt.read_bytes()
        layout.append(("ebd0a0a2-b9e5-4433-87c0-68b6b72699c7", pos // 512, (pos + NTFS_SIZE) // 512 - 1, "Basic data partition"))
    pos += NTFS_SIZE
    if can_build_ext():
        ex = work / "ext.img"
        build_ext(ex, work)
        disk[pos:pos + EXT_SIZE] = ex.read_bytes()
        layout.append(("0fc63daf-8483-4772-8e79-3d69d8477de4", pos // 512, (pos + EXT_SIZE) // 512 - 1, "linux"))
    pos += EXT_SIZE
    disk[pos + 3:pos + 11] = b"-FVE-FS-"
    disk[pos + 510:pos + 512] = b"\x55\xaa"
    layout.append(("ebd0a0a2-b9e5-4433-87c0-68b6b72699c7", pos // 512, (pos + BL_SIZE) // 512 - 1, "Data"))
    _gpt(disk, layout)
    out = work / "disk.raw"
    out.write_bytes(disk)
    return out
