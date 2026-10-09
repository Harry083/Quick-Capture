"""A small read-only NTFS reader: enough to find files by path, read them, and sweep the MFT.

It understands MFT records (with update-sequence fixups), resident and non-resident attributes, data runs
(including sparse runs), attribute lists, and $I30 directory indexes. It doesn't do LZNT1-compressed or
EFS-encrypted files; those are reported as unreadable instead of returned wrong.
"""
from __future__ import annotations

import struct
from typing import Callable, Iterator

AT_STANDARD_INFORMATION = 0x10
AT_ATTRIBUTE_LIST = 0x20
AT_FILE_NAME = 0x30
AT_DATA = 0x80
AT_INDEX_ROOT = 0x90
AT_INDEX_ALLOCATION = 0xA0
AT_BITMAP = 0xB0
AT_END = 0xFFFFFFFF

ROOT = 5
MAX_READ = 512 * 1024 * 1024  # never pull more than this into memory for one file

_u16 = struct.Struct("<H").unpack_from
_u32 = struct.Struct("<I").unpack_from
_u64 = struct.Struct("<Q").unpack_from


class NtfsError(Exception):
    pass


def filetime(ft: int):
    """FILETIME (100 ns since 1601) -> Unix seconds, or None for 0 / nonsense values."""
    if not ft or ft >= 0x7FFFFFFFFFFFFFFF:
        return None
    return (ft - 116444736000000000) / 1e7


def _fixup(buf: bytearray, stride: int = 512) -> bool:
    usa_off, usa_count = _u16(buf, 4)[0], _u16(buf, 6)[0]
    if usa_off + 2 * usa_count > len(buf):
        return False
    usn = buf[usa_off:usa_off + 2]
    for i in range(1, usa_count):
        pos = i * stride - 2
        if pos + 2 > len(buf):
            break
        if buf[pos:pos + 2] != usn:
            return False  # torn write: the record isn't trustworthy
        buf[pos:pos + 2] = buf[usa_off + 2 * i:usa_off + 2 * i + 2]
    return True


def decode_runs(data: bytes, pos: int) -> list[tuple[int, int | None]]:
    """Data runs -> [(cluster count, starting LCN or None for sparse)]."""
    runs = []
    lcn = 0
    while pos < len(data):
        head = data[pos]
        if head == 0:
            break
        len_size, off_size = head & 0x0F, head >> 4
        pos += 1
        count = int.from_bytes(data[pos:pos + len_size], "little")
        pos += len_size
        if off_size:
            lcn += int.from_bytes(data[pos:pos + off_size], "little", signed=True)
            runs.append((count, lcn))
        else:
            runs.append((count, None))
        pos += off_size
    return runs


class Attribute:
    __slots__ = ("type", "name", "resident", "content", "runs", "size", "start_vcn", "flags", "init_size")

    def __init__(self, rec: bytes, off: int) -> None:
        self.type, length = struct.unpack_from("<II", rec, off)
        nonres, name_len = rec[off + 8], rec[off + 9]
        name_off, self.flags = struct.unpack_from("<HH", rec, off + 10)
        self.name = rec[off + name_off:off + name_off + 2 * name_len].decode("utf-16-le", "replace")
        self.resident = not nonres
        if self.resident:
            size, coff = struct.unpack_from("<IH", rec, off + 16)
            self.content = bytes(rec[off + coff:off + coff + size])
            self.size = self.init_size = size
            self.runs = []
            self.start_vcn = 0
        else:
            self.content = None
            self.start_vcn = _u64(rec, off + 16)[0]
            run_off = _u16(rec, off + 32)[0]
            self.size, self.init_size = struct.unpack_from("<QQ", rec, off + 48)
            self.runs = decode_runs(bytes(rec[off:off + length]), run_off)


class Record:
    def __init__(self, number: int, buf: bytearray) -> None:
        self.number = number
        self.flags = _u16(buf, 22)[0]
        self.base = _u64(buf, 32)[0] & 0xFFFFFFFFFFFF
        self.attributes: list[Attribute] = []
        off = _u16(buf, 20)[0]
        used = min(_u32(buf, 24)[0], len(buf))
        while off + 16 <= used:
            atype, length = struct.unpack_from("<II", buf, off)
            if atype == AT_END or length < 16 or off + length > used:
                break
            self.attributes.append(Attribute(buf, off))
            off += length

    @property
    def in_use(self) -> bool:
        return bool(self.flags & 1)

    @property
    def is_dir(self) -> bool:
        return bool(self.flags & 2)

    def find(self, atype: int, name: str = "") -> Attribute | None:
        return next((a for a in self.attributes if a.type == atype and a.name == name), None)


def parse_file_name(c: bytes) -> dict:
    parent = _u64(c, 0)[0] & 0xFFFFFFFFFFFF
    created, modified, _, accessed, alloc, size = struct.unpack_from("<QQQQQQ", c, 8)
    flags = _u32(c, 56)[0]
    n, ns = c[64], c[65]
    return {
        "parent": parent, "name": c[66:66 + 2 * n].decode("utf-16-le", "replace"), "namespace": ns,
        "created": filetime(created), "modified": filetime(modified), "accessed": accessed,
        "size": size, "is_dir": bool(flags & 0x10000000),
    }


class Ntfs:
    def __init__(self, volume) -> None:
        """`volume` has .read(offset, size) addressed from the start of the NTFS volume."""
        self.vol = volume
        boot = volume.read(0, 512)
        if boot[3:11] != b"NTFS    ":
            raise NtfsError("Not an NTFS volume")
        bps, spc = _u16(boot, 11)[0], boot[13]
        if spc > 0x80:  # very large clusters are stored as a negative power of two
            spc = 1 << (256 - spc)
        self.cluster = bps * spc
        self.total_sectors, mft_lcn = struct.unpack_from("<QQ", boot, 40)
        cpr = struct.unpack_from("<b", boot, 64)[0]
        self.record_size = 1 << -cpr if cpr < 0 else cpr * self.cluster
        cpi = struct.unpack_from("<b", boot, 68)[0]
        self.index_size = 1 << -cpi if cpi < 0 else cpi * self.cluster
        self.serial = _u64(boot, 72)[0]
        self.size = self.total_sectors * bps
        if not self.cluster or not self.record_size or self.record_size > 65536:
            raise NtfsError("Damaged NTFS boot sector")

        first = self._parse(0, bytearray(volume.read(mft_lcn * self.cluster, self.record_size)))
        if first is None:
            raise NtfsError("Can't read the $MFT record")
        data = self._data_attrs(first)
        if not data:
            raise NtfsError("$MFT has no data")
        self._mft_runs = self._merge_runs(data)
        self.mft_size = data[0].size
        self.record_count = self.mft_size // self.record_size

    # ---------------------------------------------------------------- records
    def _parse(self, number: int, buf: bytearray) -> Record | None:
        if len(buf) < self.record_size or buf[:4] != b"FILE" or not _fixup(buf):
            return None
        try:
            return Record(number, buf)
        except (struct.error, IndexError):
            return None

    def _mft_read(self, offset: int, size: int) -> bytes:
        return self._read_runs(self._mft_runs, offset, size)

    def record(self, number: int) -> Record | None:
        buf = bytearray(self._mft_read(number * self.record_size, self.record_size))
        return self._parse(number, buf)

    def _read_runs(self, runs: list[tuple[int, int | None]], offset: int, size: int) -> bytes:
        out = []
        cl = self.cluster
        vcn_byte = 0
        end = offset + size
        for count, lcn in runs:
            run_bytes = count * cl
            run_end = vcn_byte + run_bytes
            if run_end > offset and vcn_byte < end:
                a = max(offset, vcn_byte)
                b = min(end, run_end)
                if lcn is None:
                    out.append(bytes(b - a))
                else:
                    out.append(self.vol.read(lcn * cl + (a - vcn_byte), b - a))
            vcn_byte = run_end
            if vcn_byte >= end:
                break
        return b"".join(out)

    @staticmethod
    def _merge_runs(attrs: list[Attribute]) -> list[tuple[int, int | None]]:
        runs = []
        for a in sorted(attrs, key=lambda a: a.start_vcn):
            runs.extend(a.runs)
        return runs

    def _data_attrs(self, rec: Record, atype: int = AT_DATA, name: str = "") -> list[Attribute]:
        """Every piece of one attribute, following $ATTRIBUTE_LIST into extension records if needed."""
        found = [a for a in rec.attributes if a.type == atype and a.name == name]
        alist = rec.find(AT_ATTRIBUTE_LIST)
        if alist is None or (found and found[0].resident):
            return found
        content = alist.content if alist.resident else self._read_runs(alist.runs, 0, alist.size)[:alist.size]
        pos, others = 0, []
        while pos + 26 <= len(content):
            t, length = struct.unpack_from("<IH", content, pos)
            if length == 0:
                break
            nlen, noff = content[pos + 6], content[pos + 7]
            ref = _u64(content, pos + 16)[0] & 0xFFFFFFFFFFFF
            aname = content[pos + noff:pos + noff + 2 * nlen].decode("utf-16-le", "replace")
            if t == atype and aname == name and ref != rec.number and ref not in others:
                others.append(ref)
            pos += length
        pieces = list(found)
        for ref in others:
            ext = self.record(ref)
            if ext is not None:
                pieces.extend(a for a in ext.attributes if a.type == atype and a.name == name)
        # the attribute's true size lives in the piece that starts at VCN 0
        pieces.sort(key=lambda a: a.start_vcn)
        return pieces

    def read_attr(self, rec: Record, atype: int = AT_DATA, name: str = "", limit: int = MAX_READ) -> bytes:
        pieces = self._data_attrs(rec, atype, name)
        if not pieces:
            raise NtfsError(f"No {'$DATA' if atype == AT_DATA else hex(atype)} attribute")
        if pieces[0].resident:
            return pieces[0].content[:limit]
        if pieces[0].flags & 0x0001:
            raise NtfsError("File is NTFS-compressed (not supported)")
        if pieces[0].flags & 0x4000:
            raise NtfsError("File is EFS-encrypted")
        size = pieces[0].size
        if size > limit:
            raise NtfsError(f"File too large to read for triage ({size:,} bytes)")
        data = self._read_runs(self._merge_runs(pieces), 0, size)
        init = pieces[0].init_size
        if init < len(data):  # past the initialised size the file reads as zeros
            data = data[:init] + bytes(len(data) - init)
        return data

    # ---------------------------------------------------------------- directories
    def list_dir(self, rec: Record) -> Iterator[tuple[str, int, dict]]:
        """(name, record number, FILE_NAME fields) for every live entry, skipping DOS 8.3 aliases."""
        root = rec.find(AT_INDEX_ROOT, "$I30")
        if root is None:
            return
        yield from self._index_entries(root.content, 16)
        alloc = self._data_attrs(rec, AT_INDEX_ALLOCATION, "$I30")
        if not alloc:
            return
        bitmap_attr = rec.find(AT_BITMAP, "$I30")
        bitmap = b""
        if bitmap_attr is not None:
            bitmap = bitmap_attr.content if bitmap_attr.resident else self._read_runs(bitmap_attr.runs, 0, bitmap_attr.size)
        runs = self._merge_runs(alloc)
        total = alloc[0].size
        isz = self.index_size
        for i in range(total // isz):
            if bitmap and not (i // 8 < len(bitmap) and bitmap[i // 8] >> (i % 8) & 1):
                continue
            buf = bytearray(self._read_runs(runs, i * isz, isz))
            if buf[:4] != b"INDX" or not _fixup(buf):
                continue
            yield from self._index_entries(buf, 24)

    @staticmethod
    def _index_entries(buf, node: int) -> Iterator[tuple[str, int, dict]]:
        start, end = struct.unpack_from("<II", buf, node)
        pos, end = node + start, min(len(buf), node + end)
        while pos + 16 <= end:
            ref, length, clen, flags = struct.unpack_from("<QHHI", buf, pos)
            if length < 16:
                break
            if clen >= 66 and not flags & 2:
                fn = parse_file_name(bytes(buf[pos + 16:pos + 16 + clen]))
                if fn["namespace"] != 2:
                    yield fn["name"], ref & 0xFFFFFFFFFFFF, fn
            if flags & 2:
                break
            pos += length

    def lookup(self, path: str) -> Record | None:
        """Find a file or folder by path (\\Windows\\System32\\config\\SYSTEM), ignoring case."""
        rec = self.record(ROOT)
        for part in [p for p in path.replace("/", "\\").split("\\") if p]:
            if rec is None or not rec.is_dir:
                return None
            want = part.casefold()
            match = next((n for name, n, _ in self.list_dir(rec) if name.casefold() == want), None)
            rec = self.record(match) if match is not None else None
        return rec

    def read_file(self, path: str, limit: int = MAX_READ) -> bytes | None:
        rec = self.lookup(path)
        if rec is None or rec.is_dir:
            return None
        return self.read_attr(rec, limit=limit)

    # ---------------------------------------------------------------- MFT sweep
    def sweep(self, on_record: Callable[[int, int, int, str, int, float | None, int], None],
              progress: Callable[[float], None] | None = None, cancel=None, block_records: int = 4096) -> None:
        """Read the MFT front to back once, calling on_record(number, flags, parent, name, modified FILETIME,
        size) for every in-use base record. Only $STANDARD_INFORMATION, $FILE_NAME and the unnamed $DATA
        header are decoded; it's one sequential read, not an index."""
        rs = self.record_size
        total = self.record_count
        n = 0
        while n < total:
            if cancel is not None and cancel.is_set():
                return
            count = min(block_records, total - n)
            block = self._mft_read(n * rs, count * rs)
            for i in range(len(block) // rs):
                off = i * rs
                if block[off:off + 4] != b"FILE":
                    continue
                flags = _u16(block, off + 22)[0]
                if not flags & 1 or _u64(block, off + 32)[0] & 0xFFFFFFFFFFFF:
                    continue  # free, or an extension record of some other file
                buf = bytearray(block[off:off + rs])
                if not _fixup(buf):
                    continue
                self._sweep_record(n + i, flags, buf, on_record)
            n += count
            if progress is not None:
                progress(n / total)

    @staticmethod
    def _sweep_record(number, flags, buf, on_record) -> None:
        pos = _u16(buf, 20)[0]
        used = min(_u32(buf, 24)[0], len(buf))
        modified = 0
        name, parent, best_ns = None, 0, 99
        size = 0
        try:
            while pos + 16 <= used:
                atype, length = struct.unpack_from("<II", buf, pos)
                if atype == AT_END or length < 16:
                    break
                if atype == AT_STANDARD_INFORMATION and not buf[pos + 8]:
                    coff = _u16(buf, pos + 20)[0]
                    modified = _u64(buf, pos + coff + 8)[0]
                elif atype == AT_FILE_NAME and not buf[pos + 8]:
                    coff = _u16(buf, pos + 20)[0]
                    c = pos + coff
                    ns = buf[c + 65]
                    # prefer the Win32 (1) or POSIX (0) name over the DOS 8.3 alias (2)
                    rank = {1: 0, 3: 0, 0: 1, 2: 2}.get(ns, 3)
                    if rank < best_ns:
                        best_ns = rank
                        parent = _u64(buf, c)[0] & 0xFFFFFFFFFFFF
                        n = buf[c + 64]
                        name = buf[c + 66:c + 66 + 2 * n].decode("utf-16-le", "replace")
                        if not modified:
                            modified = _u64(buf, c + 16)[0]
                elif atype == AT_DATA and not buf[pos + 9]:
                    if buf[pos + 8]:
                        if not _u64(buf, pos + 16)[0]:
                            size = _u64(buf, pos + 48)[0]
                    else:
                        size = _u32(buf, pos + 16)[0]
                pos += length
        except (struct.error, IndexError):
            return
        if name is not None:
            on_record(number, flags, parent, name, modified, size)
