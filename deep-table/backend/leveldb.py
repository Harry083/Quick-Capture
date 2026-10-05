"""A read-only, pure-Python reader for LevelDB directories (Chromium's Local Storage, Session Storage and
IndexedDB, and many apps besides).

LevelDB never updates in place. Every put and delete is appended to the write-ahead log (*.log) with a sequence
number, and later compacted into sorted tables (*.ldb / *.sst). An older value lingers until a compaction
drops it, so a directory usually holds several generations of a key: the live value, older versions, and
values that were deleted (followed by a tombstone). This module reads every record from every file and leaves
deciding which is live to `resolve()`.

Formats: https://github.com/google/leveldb/blob/main/doc/log_format.md and table_format.md
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .transforms import TransformError, snappy_raw

BLOCK = 32768
TABLE_MAGIC = 0xDB4775248B80FB57
TYPE_DELETE, TYPE_VALUE = 0, 1


class LevelDbError(Exception):
    pass


# ---------------------------------------------------------------- primitives
def varint(buf: bytes, pos: int) -> tuple[int, int]:
    """LevelDB varint (little-endian base 128). Returns (value, new position)."""
    result = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise LevelDbError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def length_prefixed(buf: bytes, pos: int) -> tuple[bytes, int]:
    n, pos = varint(buf, pos)
    if pos + n > len(buf):
        raise LevelDbError("truncated slice")
    return buf[pos:pos + n], pos + n


def _crc32c_table():
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC_TABLE = _crc32c_table()


def crc32c(data: bytes) -> int:
    c = 0xFFFFFFFF
    t = _CRC_TABLE
    for b in data:
        c = t[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


def unmask(crc: int) -> int:
    rot = (crc - 0xA282EAD8) & 0xFFFFFFFF
    return ((rot >> 17) | (rot << 15)) & 0xFFFFFFFF


# ---------------------------------------------------------------- records
@dataclass
class Record:
    key: bytes  # user key
    value: bytes | None  # None for a deletion
    seq: int
    kind: int  # TYPE_VALUE or TYPE_DELETE
    file: str
    file_kind: str  # log | table
    offset: int  # file offset of the log batch / table block
    crc_ok: bool = True
    state: str = ""  # live | older version | deleted | tombstone (set by resolve)
    also: list[str] = field(default_factory=list)

    @property
    def where(self) -> str:
        return f"{self.file} @{self.offset}"


def read_log_records(data: bytes) -> list[tuple[int, bytes, bool]]:
    """Reassemble the physical log into logical records: [(file offset, payload, checksum ok)]."""
    out, pending, start, ok = [], bytearray(), None, True
    pos = 0
    while pos + 7 <= len(data):
        left = BLOCK - pos % BLOCK
        if left < 7:  # block trailer
            pos += left
            continue
        crc, length, kind = struct.unpack("<IHB", data[pos:pos + 7])
        if kind == 0 and length == 0:  # preallocated / zeroed space
            pos += left
            continue
        payload = data[pos + 7:pos + 7 + length]
        if len(payload) < length or length > left - 7:
            break  # torn write at the end of the log
        good = unmask(crc) == crc32c(bytes([kind]) + payload)
        if kind == 1:  # FULL
            out.append((pos, payload, good))
            pending, start = bytearray(), None
        elif kind == 2:  # FIRST
            pending, start, ok = bytearray(payload), pos, good
        elif kind == 3 and start is not None:  # MIDDLE
            pending += payload
            ok = ok and good
        elif kind == 4 and start is not None:  # LAST
            pending += payload
            out.append((start, bytes(pending), ok and good))
            pending, start = bytearray(), None
        pos += 7 + length
    return out


def parse_write_batch(payload: bytes, file: str, offset: int, crc_ok: bool) -> list[Record]:
    if len(payload) < 12:
        return []
    seq, count = struct.unpack("<QI", payload[:12])
    pos, out = 12, []
    try:
        for i in range(count):
            tag = payload[pos]
            pos += 1
            key, pos = length_prefixed(payload, pos)
            value = None
            if tag == TYPE_VALUE:
                value, pos = length_prefixed(payload, pos)
            elif tag != TYPE_DELETE:
                break
            out.append(Record(key, value, seq + i, tag, file, "log", offset, crc_ok))
    except (IndexError, LevelDbError):
        pass  # keep what parsed: a torn batch still holds evidence
    return out


def read_log(path: Path) -> list[Record]:
    data = path.read_bytes()
    out = []
    for off, payload, ok in read_log_records(data):
        out += parse_write_batch(payload, path.name, off, ok)
    return out


# ---------------------------------------------------------------- tables
def _block_entries(block: bytes):
    if len(block) < 4:
        return
    restarts = struct.unpack("<I", block[-4:])[0]
    end = len(block) - 4 - 4 * restarts
    if end < 0:
        raise LevelDbError("bad block restart count")
    pos, key = 0, b""
    while pos < end:
        shared, pos = varint(block, pos)
        non_shared, pos = varint(block, pos)
        vlen, pos = varint(block, pos)
        key = key[:shared] + block[pos:pos + non_shared]
        pos += non_shared
        yield key, block[pos:pos + vlen]
        pos += vlen


def _read_block(data: bytes, offset: int, size: int) -> tuple[bytes, bool]:
    raw = data[offset:offset + size]
    trailer = data[offset + size:offset + size + 5]
    if len(raw) < size or len(trailer) < 5:
        raise LevelDbError("block runs past the end of the file")
    kind = trailer[0]
    ok = unmask(struct.unpack("<I", trailer[1:5])[0]) == crc32c(raw + trailer[:1])
    if kind == 0:
        return raw, ok
    if kind == 1:
        try:
            return snappy_raw(raw), ok
        except TransformError as exc:
            raise LevelDbError(f"snappy block: {exc}") from exc
    raise LevelDbError(f"unsupported block compression {kind} (zstd?)")


def read_table(path: Path) -> list[Record]:
    data = path.read_bytes()
    if len(data) < 48 or struct.unpack("<Q", data[-8:])[0] != TABLE_MAGIC:
        raise LevelDbError(f"{path.name}: not a LevelDB table (bad magic)")
    footer = data[-48:]
    pos = 0
    _meta_off, pos = varint(footer, pos)
    _meta_size, pos = varint(footer, pos)
    idx_off, pos = varint(footer, pos)
    idx_size, pos = varint(footer, pos)
    index, _ = _read_block(data, idx_off, idx_size)
    out = []
    for _sep, handle in _block_entries(index):
        off, p = varint(handle, 0)
        size, _ = varint(handle, p)
        try:
            block, ok = _read_block(data, off, size)
        except LevelDbError:
            continue
        for ikey, value in _block_entries(block):
            if len(ikey) < 8:
                continue
            packed = struct.unpack("<Q", ikey[-8:])[0]
            kind = packed & 0xFF
            out.append(Record(ikey[:-8], value if kind == TYPE_VALUE else None, packed >> 8, kind,
                              path.name, "table", off, ok))
    return out


# ---------------------------------------------------------------- manifest
@dataclass
class Manifest:
    name: str
    comparator: str = ""
    log_number: int = 0
    next_file: int = 0
    last_sequence: int = 0
    live_files: dict[int, dict] = field(default_factory=dict)  # number -> {level, size, smallest, largest}
    deleted_files: set[int] = field(default_factory=set)


def read_manifest(path: Path) -> Manifest:
    m = Manifest(path.name)
    for _off, edit, _ok in read_log_records(path.read_bytes()):
        pos = 0
        try:
            while pos < len(edit):
                tag, pos = varint(edit, pos)
                if tag == 1:
                    name, pos = length_prefixed(edit, pos)
                    m.comparator = name.decode("utf-8", "replace")
                elif tag in (2, 3, 4, 9):
                    val, pos = varint(edit, pos)
                    if tag == 2:
                        m.log_number = val
                    elif tag == 3:
                        m.next_file = val
                    elif tag == 4:
                        m.last_sequence = val
                elif tag == 5:
                    _level, pos = varint(edit, pos)
                    _key, pos = length_prefixed(edit, pos)
                elif tag == 6:
                    _level, pos = varint(edit, pos)
                    num, pos = varint(edit, pos)
                    m.live_files.pop(num, None)
                    m.deleted_files.add(num)
                elif tag == 7:
                    level, pos = varint(edit, pos)
                    num, pos = varint(edit, pos)
                    size, pos = varint(edit, pos)
                    small, pos = length_prefixed(edit, pos)
                    large, pos = length_prefixed(edit, pos)
                    m.live_files[num] = {"level": level, "size": size, "smallest": small, "largest": large}
                else:
                    break  # unknown tag: stop reading this edit
        except LevelDbError:
            continue
    return m


# ---------------------------------------------------------------- directory
FILE_RE = re.compile(r"^(\d{6})\.(log|ldb|sst)$")


def is_leveldb_dir(path: Path) -> bool:
    return path.is_dir() and (path / "CURRENT").is_file() or any(
        path.is_dir() and FILE_RE.match(p.name) for p in (path.iterdir() if path.is_dir() else []))


@dataclass
class LevelDb:
    path: str
    manifest: Manifest | None
    current: str
    records: list[Record]
    files: list[dict]
    errors: list[str]

    @property
    def comparator(self) -> str:
        return self.manifest.comparator if self.manifest else ""


def open_dir(path: str | Path) -> LevelDb:
    """Read every log and table in a LevelDB directory (a copy of it: nothing is written)."""
    d = Path(path)
    errors, files, records = [], [], []
    current = ""
    manifest = None
    if (d / "CURRENT").is_file():
        current = (d / "CURRENT").read_text("utf-8", "replace").strip()
        if (d / current).is_file():
            try:
                manifest = read_manifest(d / current)
            except (LevelDbError, OSError) as exc:
                errors.append(f"{current}: {exc}")
        else:
            errors.append(f"CURRENT names {current}, which is missing")
    for p in sorted(d.iterdir()):
        m = FILE_RE.match(p.name)
        if not m:
            continue
        num, ext = int(m.group(1)), m.group(2)
        info = {"name": p.name, "size": p.stat().st_size, "kind": "log" if ext == "log" else "table",
                "number": num, "records": 0, "error": ""}
        if manifest and ext != "log":
            info["in_version"] = num in manifest.live_files
            info["level"] = manifest.live_files.get(num, {}).get("level")
        elif manifest:
            info["in_version"] = num >= manifest.log_number  # older logs are obsolete leftovers
        try:
            got = read_log(p) if ext == "log" else read_table(p)
            info["records"] = len(got)
            records += got
        except (LevelDbError, OSError, struct.error) as exc:
            info["error"] = str(exc)
            errors.append(f"{p.name}: {exc}")
        files.append(info)
    if not files and not manifest:
        raise LevelDbError(f"{d} doesn't look like a LevelDB directory (no CURRENT, .log or .ldb files)")
    return LevelDb(str(d), manifest, current, resolve(records), files, errors)


def resolve(records: list[Record]) -> list[Record]:
    """Work out each record's state. Copies of the same record (one put moved from the log into a table by a
    compaction keeps its sequence number) are merged, keeping every location. For each key the record with
    the highest sequence number wins: a put is live, a deletion makes the key deleted. Earlier puts are older
    versions, or deleted values if the key's last record is a tombstone."""
    merged: dict[tuple, Record] = {}
    for r in records:
        k = (r.key, r.seq, r.kind)
        if k in merged:
            m = merged[k]
            if r.where not in m.also and len(m.also) < 20:
                m.also.append(r.where)
        else:
            merged[k] = r
    by_key: dict[bytes, list[Record]] = {}
    for r in merged.values():
        by_key.setdefault(r.key, []).append(r)
    out = []
    for key, recs in by_key.items():
        recs.sort(key=lambda r: r.seq, reverse=True)
        newest = recs[0]
        newest.state = "live" if newest.kind == TYPE_VALUE else "tombstone"
        for r in recs[1:]:
            if r.kind == TYPE_DELETE:
                r.state = "tombstone"
            else:
                r.state = "deleted" if newest.kind == TYPE_DELETE else "older version"
        out += recs
    out.sort(key=lambda r: (r.key, -r.seq))
    return out


def find_root(path: str | Path) -> Path:
    """The LevelDB directory for a path: the directory itself, or the one holding a chosen file."""
    p = Path(path)
    return p if p.is_dir() else p.parent


def looks_like_leveldb(path: str | Path) -> bool:
    p = Path(path)
    root = find_root(p)
    if not root.is_dir():
        return False
    if p.is_file() and not (FILE_RE.match(p.name) or p.name in ("CURRENT", "LOCK", "LOG", "LOG.old")
                            or p.name.startswith("MANIFEST-")):
        return False
    return (root / "CURRENT").is_file() or any(FILE_RE.match(n) for n in os.listdir(root))
