"""A read-only Windows registry hive (regf) parser: keys, values, and their last-written times.

It reads the primary hive file only. Recent changes still sitting in the .LOG1/.LOG2 transaction logs
aren't replayed, which is fine for triage: names, versions and accounts rarely live only in the log.
"""
from __future__ import annotations

import struct
from typing import Iterator

from .ntfs import filetime

REG_SZ, REG_EXPAND_SZ, REG_BINARY, REG_DWORD, REG_DWORD_BE, REG_LINK, REG_MULTI_SZ, REG_QWORD = 1, 2, 3, 4, 5, 6, 7, 11


class HiveError(Exception):
    pass


class Hive:
    def __init__(self, data: bytes) -> None:
        if len(data) < 4096 + 32 or data[:4] != b"regf":
            raise HiveError("Not a registry hive")
        self.data = data
        self.minor = struct.unpack_from("<I", data, 24)[0]
        root = struct.unpack_from("<I", data, 36)[0]
        self.root = Key(self, root)

    def cell(self, offset: int) -> bytes:
        pos = 4096 + offset
        if offset in (0, 0xFFFFFFFF) or pos + 4 > len(self.data):
            raise HiveError(f"Cell offset out of range: {offset:#x}")
        size = abs(struct.unpack_from("<i", self.data, pos)[0])
        return self.data[pos + 4:pos + max(4, size)]

    def key(self, path: str) -> "Key | None":
        k = self.root
        for part in [p for p in path.split("\\") if p]:
            k = k.subkey(part)
            if k is None:
                return None
        return k


class Key:
    def __init__(self, hive: Hive, offset: int) -> None:
        self.hive = hive
        c = hive.cell(offset)
        if c[:2] != b"nk":
            raise HiveError(f"Expected a key at {offset:#x}")
        self._c = c
        flags = struct.unpack_from("<H", c, 2)[0]
        self.last_written = filetime(struct.unpack_from("<Q", c, 4)[0])
        self.subkey_count, = struct.unpack_from("<I", c, 20)
        self._subkeys, = struct.unpack_from("<I", c, 28)
        self.value_count, self._values = struct.unpack_from("<II", c, 36)
        name_len = struct.unpack_from("<H", c, 72)[0]
        raw = c[76:76 + name_len]
        self.name = raw.decode("latin-1") if flags & 0x20 else raw.decode("utf-16-le", "replace")

    # ---------------------------------------------------------------- subkeys
    def _list_offsets(self, offset: int, depth: int = 0) -> Iterator[int]:
        c = self.hive.cell(offset)
        sig, count = c[:2], struct.unpack_from("<H", c, 2)[0]
        if sig in (b"lf", b"lh"):
            for i in range(count):
                yield struct.unpack_from("<I", c, 4 + 8 * i)[0]
        elif sig == b"li":
            for i in range(count):
                yield struct.unpack_from("<I", c, 4 + 4 * i)[0]
        elif sig == b"ri" and depth < 4:
            for i in range(count):
                yield from self._list_offsets(struct.unpack_from("<I", c, 4 + 4 * i)[0], depth + 1)

    def subkeys(self) -> Iterator["Key"]:
        if not self.subkey_count or self._subkeys == 0xFFFFFFFF:
            return
        for off in self._list_offsets(self._subkeys):
            try:
                yield Key(self.hive, off)
            except (HiveError, struct.error):
                continue

    def subkey(self, name: str) -> "Key | None":
        want = name.casefold()
        return next((k for k in self.subkeys() if k.name.casefold() == want), None)

    # ---------------------------------------------------------------- values
    def values(self) -> Iterator[tuple[str, int, object]]:
        if not self.value_count or self._values == 0xFFFFFFFF:
            return
        try:
            lst = self.hive.cell(self._values)
        except HiveError:
            return
        for i in range(min(self.value_count, len(lst) // 4)):
            try:
                yield self._value(struct.unpack_from("<I", lst, 4 * i)[0])
            except (HiveError, struct.error, UnicodeDecodeError):
                continue

    def _value(self, offset: int) -> tuple[str, int, object]:
        c = self.hive.cell(offset)
        if c[:2] != b"vk":
            raise HiveError("Expected a value")
        name_len, size, data_off, vtype, flags = struct.unpack_from("<HIIIH", c, 2)
        raw_name = c[20:20 + name_len]
        name = raw_name.decode("latin-1") if flags & 1 else raw_name.decode("utf-16-le", "replace")
        if size & 0x80000000:  # four bytes or fewer live in the offset field itself
            data = struct.pack("<I", data_off)[:size & 0x7FFFFFFF]
        else:
            cell = self.hive.cell(data_off)
            if size > 16344 and self.hive.minor >= 4 and cell[:2] == b"db":
                count, seg_list = struct.unpack_from("<HI", cell, 2)
                segs = self.hive.cell(seg_list)
                data = b"".join(self.hive.cell(struct.unpack_from("<I", segs, 4 * i)[0])[:16344] for i in range(count))
            else:
                data = cell
            data = data[:size]
        return name, vtype, _decode(vtype, data)

    def value(self, name: str, default=None):
        want = name.casefold()
        for vname, _, data in self.values():
            if vname.casefold() == want:
                return data
        return default


def _decode(vtype: int, data: bytes):
    if vtype in (REG_SZ, REG_EXPAND_SZ, REG_LINK):
        if len(data) % 2:
            data = data[:-1]
        return data.decode("utf-16-le", "replace").split("\0")[0]
    if vtype == REG_MULTI_SZ:
        if len(data) % 2:
            data = data[:-1]
        return [s for s in data.decode("utf-16-le", "replace").split("\0") if s]
    if vtype == REG_DWORD and len(data) >= 4:
        return struct.unpack_from("<I", data)[0]
    if vtype == REG_DWORD_BE and len(data) >= 4:
        return struct.unpack_from(">I", data)[0]
    if vtype == REG_QWORD and len(data) >= 8:
        return struct.unpack_from("<Q", data)[0]
    return bytes(data)
