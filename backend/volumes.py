"""Partition tables (MBR with extended partitions, GPT) and filesystem detection by signature."""
from __future__ import annotations

import struct
import uuid

GPT_TYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI system",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft reserved",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Basic data",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows recovery",
    "5808c8aa-7e8f-42e0-85d2-e1e90434cfb3": "LDM metadata",
    "af9b60a0-1431-4f62-bc68-3311714a69ad": "LDM data",
    "e75caf8f-f680-4cee-afa3-b001e56efc2d": "Storage Spaces",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "4f68bce3-e8cd-4db1-96e7-fbcaf984b709": "Linux root (x86-64)",
    "933ac7e1-2eb4-4f13-b844-0e14e2aef915": "Linux /home",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
    "a19d880f-05fc-4d3b-a006-743f0f84911e": "Linux RAID",
    "ca7d7ccb-63ed-4c53-861c-1742536059cc": "Linux LUKS",
    "bc13c2ff-59e6-4262-a352-b275fd6f7172": "Linux extended boot",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
    "7c3457ef-0000-11aa-aa11-00306543ecac": "Apple APFS",
    "48465300-0000-11aa-aa11-00306543ecac": "Apple HFS+",
    "426f6f74-0000-11aa-aa11-00306543ecac": "Apple boot",
    "52637672-7900-11aa-aa11-00306543ecac": "Apple recovery",
}

MBR_TYPES = {
    0x01: "FAT12", 0x04: "FAT16", 0x06: "FAT16", 0x07: "NTFS / exFAT", 0x0B: "FAT32", 0x0C: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)", 0x11: "Hidden FAT12", 0x14: "Hidden FAT16", 0x17: "Hidden NTFS", 0x1B: "Hidden FAT32",
    0x1C: "Hidden FAT32", 0x27: "Windows recovery", 0x42: "Windows dynamic", 0x82: "Linux swap",
    0x83: "Linux", 0x8E: "Linux LVM", 0xA5: "FreeBSD", 0xA8: "Apple UFS", 0xAF: "Apple HFS+",
    0xEE: "GPT protective", 0xEF: "EFI system", 0xFD: "Linux RAID",
}
EXTENDED = (0x05, 0x0F, 0x85)


def detect_fs(read) -> str:
    """Name the filesystem at the start of a volume. `read(offset, size)` reads from the volume."""
    head = read(0, 4096)
    if len(head) < 512:
        return "unknown"
    oem = head[3:11]
    if oem == b"NTFS    ":
        return "NTFS"
    if oem == b"-FVE-FS-":
        return "BitLocker"
    if oem == b"EXFAT   ":
        return "exFAT"
    if oem == b"ReFS\0\0\0\0":
        return "ReFS"
    if head[82:90] == b"FAT32   ":
        return "FAT32"
    if head[54:59] in (b"FAT12", b"FAT16", b"FAT  "):
        return "FAT"
    if head[:6] == b"LUKS\xba\xbe":
        return "LUKS"
    if head[:4] == b"XFSB":
        return "XFS"
    if head[32:36] == b"NXSB":
        return "APFS"
    if head[512:520] == b"LABELONE" or head[0:8] == b"LABELONE":
        return "LVM"
    if len(head) >= 1024 + 58 and head[1024 + 56:1024 + 58] == b"\x53\xef":
        compat, incompat = struct.unpack_from("<I I", head, 1024 + 92)
        if incompat & 0x40 or incompat & 0x200:  # extents / flex_bg
            return "ext4"
        return "ext3" if compat & 0x4 else "ext2"
    if head[1024:1026] in (b"H+", b"HX"):
        return "HFS+"
    if head[4086:4096] == b"SWAPSPACE2":
        return "swap"
    btrfs = read(0x10040, 8)
    if btrfs == b"_BHRfS_M":
        return "Btrfs"
    return "unknown"


def _guid(raw: bytes) -> str:
    return str(uuid.UUID(bytes_le=raw))


def _gpt(image, sector: int) -> list[dict] | None:
    hdr = image.read(sector, 92)
    if hdr[:8] != b"EFI PART":
        return None
    entries_lba, count, esize = struct.unpack_from("<QII", hdr, 72)
    count = min(count, 256)
    table = image.read(entries_lba * sector, count * esize)
    parts = []
    for i in range(count):
        e = table[i * esize:(i + 1) * esize]
        if len(e) < 128 or not any(e[:16]):
            continue
        type_guid = _guid(e[:16])
        first, last = struct.unpack_from("<QQ", e, 32)
        name = e[56:128].decode("utf-16-le", "replace").split("\0")[0]
        parts.append({
            "index": len(parts) + 1, "scheme": "GPT", "offset": first * sector, "size": (last - first + 1) * sector,
            "type": GPT_TYPES.get(type_guid, type_guid), "name": name,
        })
    return parts


def _mbr(image, sector: int) -> list[dict] | None:
    mbr = image.read(0, 512)
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
        return None
    raw = [struct.unpack_from("<B3xB3xII", mbr, 446 + 16 * i) for i in range(4)]
    if not any(t for _, t, _, n in raw if n):
        return None
    # A boot sector also ends in 55 AA; a volume without a partition table has a filesystem name here.
    if mbr[3:11] in (b"NTFS    ", b"-FVE-FS-", b"EXFAT   ") or mbr[82:87] == b"FAT32":
        return None
    parts = []
    for status, ptype, start, count in raw:
        if not count or not ptype:
            continue
        if ptype in EXTENDED:
            ebr_base = start
            ebr = start
            for _ in range(128):  # chain of logical partitions inside the extended one
                rec = image.read(ebr * sector, 512)
                if rec[510:512] != b"\x55\xaa":
                    break
                _, t1, s1, n1 = struct.unpack_from("<B3xB3xII", rec, 446)
                _, t2, s2, _ = struct.unpack_from("<B3xB3xII", rec, 462)
                if n1 and t1:
                    parts.append({
                        "index": len(parts) + 1, "scheme": "MBR (logical)", "offset": (ebr + s1) * sector,
                        "size": n1 * sector, "type": MBR_TYPES.get(t1, f"0x{t1:02X}"), "name": "",
                    })
                if not s2:
                    break
                ebr = ebr_base + s2
            continue
        parts.append({
            "index": len(parts) + 1, "scheme": "MBR", "offset": start * sector, "size": count * sector,
            "type": MBR_TYPES.get(ptype, f"0x{ptype:02X}"), "name": "", "bootable": status == 0x80,
        })
    return parts


def list_volumes(image) -> tuple[str, list[dict]]:
    """Returns (scheme, volumes). An image of a single volume (no partition table) is one volume at 0."""
    for sector in (image.bytes_per_sector, 512, 4096):
        gpt = _gpt(image, sector)
        if gpt is not None:
            scheme, parts = "GPT", gpt
            break
    else:
        mbr = _mbr(image, image.bytes_per_sector)
        if mbr is not None:
            scheme, parts = "MBR", mbr
        else:
            scheme, parts = "none", [{
                "index": 1, "scheme": "none", "offset": 0, "size": image.size, "type": "Whole image", "name": "",
            }]
    for p in parts:
        p["size"] = max(0, min(p["size"], image.size - p["offset"]))
        off = p["offset"]
        p["fs"] = detect_fs(lambda o, n, off=off: image.read(off + o, n)) if p["size"] else "unknown"
    return scheme, parts
