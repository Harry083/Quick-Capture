"""Builds small but well-formed registry hives (regf) for tests, so no real Windows hive is needed."""
from __future__ import annotations

import struct

REG_SZ, REG_BINARY, REG_DWORD, REG_MULTI_SZ, REG_QWORD = 1, 3, 4, 7, 11


def sz(s: str) -> tuple[int, bytes]:
    return REG_SZ, (s + "\0").encode("utf-16-le")


def dword(n: int) -> tuple[int, bytes]:
    return REG_DWORD, struct.pack("<I", n)


def qword(n: int) -> tuple[int, bytes]:
    return REG_QWORD, struct.pack("<Q", n)


def binary(b: bytes) -> tuple[int, bytes]:
    return REG_BINARY, b


def multi(*items: str) -> tuple[int, bytes]:
    return REG_MULTI_SZ, ("\0".join(items) + "\0\0").encode("utf-16-le")


def key(name: str, values: dict | None = None, *subkeys, ts: int = 0, raw_values: list | None = None) -> dict:
    """values: {name: (type, data)}. raw_values: [(name, type, data)] for odd cases (type-as-RID values)."""
    vals = [(n, t, d) for n, (t, d) in (values or {}).items()] + list(raw_values or [])
    return {"name": name, "values": vals, "subkeys": list(subkeys), "ts": ts}


class _Builder:
    def __init__(self) -> None:
        self.buf = bytearray()

    def cell(self, data: bytes) -> int:
        size = (len(data) + 4 + 7) & ~7
        off = len(self.buf)
        self.buf += struct.pack("<i", -size) + data + bytes(size - 4 - len(data))
        return off

    def patch(self, off: int, pos: int, fmt: str, *vals) -> None:
        struct.pack_into(fmt, self.buf, off + 4 + pos, *vals)


def build(root: dict) -> bytes:
    b = _Builder()

    def write_key(node: dict, parent: int) -> int:
        name = node["name"].encode("latin-1")
        nk = bytearray(76 + len(name))
        nk[0:2] = b"nk"
        struct.pack_into("<HQ", nk, 2, 0x20 | (0x04 if parent == 0xFFFFFFFF else 0), node["ts"])
        struct.pack_into("<I", nk, 16, parent)
        struct.pack_into("<IIIIII", nk, 28, 0xFFFFFFFF, 0xFFFFFFFF, 0, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
        struct.pack_into("<HH", nk, 72, len(name), 0)
        nk[76:] = name
        off = b.cell(bytes(nk))

        if node["values"]:
            offs = []
            for vname, vtype, data in node["values"]:
                n = vname.encode("latin-1")
                if len(data) <= 4:
                    size, doff = len(data) | 0x80000000, int.from_bytes(data.ljust(4, b"\0"), "little")
                else:
                    size, doff = len(data), b.cell(data)
                vk = b"vk" + struct.pack("<HIIIHH", len(n), size, doff, vtype, 1, 0) + n
                offs.append(b.cell(vk))
            vlist = b.cell(struct.pack(f"<{len(offs)}I", *offs))
            b.patch(off, 36, "<II", len(offs), vlist)

        if node["subkeys"]:
            kids = sorted(node["subkeys"], key=lambda k: k["name"].upper())
            koffs = [write_key(k, off) for k in kids]
            entries = b"".join(
                struct.pack("<I4s", ko, k["name"].encode("latin-1")[:4].ljust(4, b"\0")) for ko, k in zip(koffs, kids)
            )
            lf = b.cell(b"lf" + struct.pack("<H", len(koffs)) + entries)
            b.patch(off, 20, "<I", len(koffs))
            b.patch(off, 28, "<I", lf)
        return off

    # cells are addressed relative to the first hbin; reserve its 32-byte header first
    b.buf += bytes(32)
    root_off = write_key(root, 0xFFFFFFFF)
    data_size = (len(b.buf) + 4095) & ~4095
    free = data_size - len(b.buf)
    if free:
        b.buf += struct.pack("<i", free) + bytes(free - 4)
    b.buf[0:32] = b"hbin" + struct.pack("<III", 0, data_size, 0) + bytes(16)

    base = bytearray(4096)
    base[0:4] = b"regf"
    struct.pack_into("<IIQIIIII", base, 4, 1, 1, 0, 1, 5, 0, 1, root_off)
    struct.pack_into("<II", base, 40, data_size, 1)
    checksum = 0
    for i in range(127):
        checksum ^= struct.unpack_from("<I", base, 4 * i)[0]
    struct.pack_into("<I", base, 508, checksum)
    return bytes(base) + bytes(b.buf)
