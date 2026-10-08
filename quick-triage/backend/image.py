"""Random-access readers for evidence images: EWF-E01 (EnCase 1-7 / libewf / FTK) and raw (dd).

Nothing is decompressed up front. Opening an E01 walks the section descriptors of every segment (a few
hundred bytes each) and notes where the chunk tables are. A table's offsets are loaded the first time a
chunk in it is read, and decompressed chunks are kept in a small LRU cache. Reading the handful of
megabytes triage needs from a 2 TB image takes about as long as reading them from a 2 GB one.
"""
from __future__ import annotations

import bisect
import re
from datetime import datetime, timezone
import struct
import zlib
from collections import OrderedDict
from pathlib import Path

EWF_SIGNATURE = b"EVF\x09\x0d\x0a\xff\x00"
EWF2_SIGNATURE = b"EVF2\x0d\x0a\x81\x00"
CHUNK_CACHE = 512  # decompressed chunks kept (16 MiB at the usual 32 KiB chunk)
TABLE_CACHE = 64

HEADER_FIELDS = {
    "a": "description", "c": "case_number", "n": "evidence_number", "e": "examiner", "t": "notes",
    "md": "model", "sn": "serial", "av": "acquisition_software", "ov": "acquisition_os",
    "m": "acquired", "u": "system_date", "l": "device_label", "pid": "process_id",
}


class ImageError(Exception):
    """The file isn't a readable image; the message is shown to the user."""


def segment_extension(n: int) -> str:
    """1 -> E01 ... 99 -> E99, 100 -> EAA ... EZZ, FAA ... ZZZ."""
    if n < 100:
        return f"E{n:02d}"
    n -= 100
    first, rest = divmod(n, 26 * 26)
    return chr(ord("E") + first) + chr(65 + rest // 26) + chr(65 + rest % 26)


def segment_paths(first: str | Path) -> list[Path]:
    """Every segment of a set, matching the case of the first file's extension (.E01 or .e01)."""
    first = Path(first)
    lower = first.suffix[1:2].islower()
    paths = []
    n = 1
    while True:
        ext = segment_extension(n)
        p = first.with_name(f"{first.stem}.{ext.lower() if lower else ext}")
        if not p.exists():
            break
        paths.append(p)
        n += 1
    return paths or [first]


def _adler(data) -> int:
    return zlib.adler32(data) & 0xFFFFFFFF


def _parse_header_text(text: str) -> dict:
    """header / header2 are tab-separated: a category line, then a line of keys and a line of values."""
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    out = {}
    for i, line in enumerate(lines):
        if line == "main" and i + 2 < len(lines):
            keys, values = lines[i + 1].split("\t"), lines[i + 2].split("\t")
            for k, v in zip(keys, values):
                if k in HEADER_FIELDS and v.strip():
                    out[HEADER_FIELDS[k]] = v.strip()
            break
    for key in ("acquired", "system_date"):
        value = out.get(key, "")
        if value.isdigit():  # header2 stores epoch seconds; header stores "yyyy m d h m s" local time
            out[key] = datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        elif re.fullmatch(r"\d{4}( \d{1,2}){5}", value):
            y, mo, d, h, mi, s = (int(x) for x in value.split())
            out[key] = f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d} (local)"
    return out


class _Table:
    __slots__ = ("seg", "offset", "count", "first_chunk", "entries", "base")

    def __init__(self, seg: int, offset: int, count: int, first_chunk: int) -> None:
        self.seg, self.offset, self.count, self.first_chunk = seg, offset, count, first_chunk
        self.entries = None
        self.base = 0


class EwfImage:
    format = "E01"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.paths = segment_paths(self.path)
        self._files = []
        self._tables: list[_Table] = []
        self._bounds: list[tuple[list[int], list[int]]] = []  # per segment: section starts and ends
        self._table_starts: list[int] = []
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._loaded: OrderedDict[int, _Table] = OrderedDict()
        self.header: dict = {}
        self.md5 = self.sha1 = None
        self.chunk_size = 32 * 1024
        self.bytes_per_sector = 512
        self.sector_count = 0
        self.chunk_count = 0
        self.errors: list[tuple[int, int]] = []
        self.bad_chunks = 0
        try:
            self._open()
        except (OSError, struct.error, zlib.error, ValueError) as exc:
            self.close()
            if isinstance(exc, ImageError):
                raise
            raise ImageError(f"Cannot read {self.path.name} as E01: {exc}") from exc

    # ---------------------------------------------------------------- open
    def _open(self) -> None:
        chunk = 0
        have_volume = False
        headers: dict[bytes, dict] = {}
        for seg, path in enumerate(self.paths):
            f = open(path, "rb")  # noqa: SIM115  (kept open for the life of the image)
            self._files.append(f)
            head = f.read(13)
            if head[:8] == EWF2_SIGNATURE:
                raise ImageError("Ex01 (EWF2) images aren't supported yet. Convert to E01 or raw first.")
            if head[:8] != EWF_SIGNATURE:
                raise ImageError(f"{path.name} is not an E01 segment (bad signature)")
            f.seek(0, 2)
            file_size = f.tell()
            sections = []
            off = 13
            while off + 76 <= file_size:
                f.seek(off)
                desc = f.read(76)
                kind, nxt, size = struct.unpack_from("<16sQQ", desc)
                kind = kind.rstrip(b"\0")
                sections.append((kind, off, size, nxt))
                if kind in (b"done", b"next") or nxt <= off:
                    break
                off = nxt
            # A section's data runs to the next section (EnCase 1 put chunks inside the table section).
            starts = [off for _, off, _, _ in sections]
            self._bounds.append((starts, starts[1:] + [file_size]))
            for i, (kind, off, size, nxt) in enumerate(sections):
                end = sections[i + 1][1] if i + 1 < len(sections) else file_size
                if kind in (b"header2", b"header") and kind not in headers:
                    f.seek(off + 76)
                    raw = zlib.decompressobj().decompress(f.read(min(end, off + size) - off - 76))
                    text = raw.decode("utf-16" if kind == b"header2" else "latin-1", "replace")
                    headers[kind] = _parse_header_text(text)
                elif kind in (b"volume", b"disk") and not have_volume:
                    f.seek(off + 76)
                    data = f.read(min(1052, size - 76))
                    if len(data) >= 1052 or size - 76 >= 1052:
                        self.chunk_count, spc, bps, sectors = struct.unpack_from("<IIIQ", data, 4)
                    else:  # the 94-byte EnCase 1 / SMART volume section: 32-bit sector count
                        self.chunk_count, spc, bps, sectors = struct.unpack_from("<IIII", data, 4)
                    self.bytes_per_sector = bps or 512
                    self.chunk_size = (spc or 64) * self.bytes_per_sector
                    self.sector_count = sectors
                    have_volume = True
                elif kind == b"table":
                    f.seek(off + 76)
                    count = struct.unpack("<I", f.read(4))[0]
                    if count:
                        self._tables.append(_Table(seg, off, count, chunk))
                        self._table_starts.append(chunk)
                        chunk += count
                elif kind == b"hash":
                    f.seek(off + 76)
                    self.md5 = f.read(16).hex()
                elif kind == b"digest":
                    f.seek(off + 76)
                    d = f.read(36)
                    self.md5 = d[:16].hex() if any(d[:16]) else self.md5
                    self.sha1 = d[16:36].hex() if any(d[16:36]) else None
                elif kind == b"error2":
                    f.seek(off + 76)
                    n = struct.unpack("<I", f.read(4))[0]
                    f.seek(off + 76 + 520)
                    for _ in range(min(n, 10_000)):
                        self.errors.append(struct.unpack("<II", f.read(8)))
            if any(k == b"done" for k, *_ in sections):
                break
        if not have_volume:
            raise ImageError("No volume section: the first segment is missing or damaged")
        if not self._tables:
            raise ImageError("No chunk tables found: the image has no data")
        self.chunk_count = chunk
        self.size = self.sector_count * self.bytes_per_sector or chunk * self.chunk_size
        self.header = headers.get(b"header2") or headers.get(b"header") or {}

    def close(self) -> None:
        for f in self._files:
            f.close()
        self._files = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- chunks
    def _table_for(self, chunk: int) -> _Table:
        t = self._tables[bisect.bisect_right(self._table_starts, chunk) - 1]
        if t.entries is None:
            f = self._files[t.seg]
            f.seek(t.offset + 76)
            head = f.read(24)
            count, base = struct.unpack_from("<I4xQ", head)
            raw = f.read(4 * count)
            t.entries = struct.unpack(f"<{len(raw) // 4}I", raw)
            # EnCase 6+ stores offsets relative to a base; older versions store file offsets (base 0).
            t.base = base
            self._loaded[id(t)] = t
            if len(self._loaded) > TABLE_CACHE:
                _, old = self._loaded.popitem(last=False)
                old.entries = None
        else:
            self._loaded.move_to_end(id(t))
        return t

    def _chunk(self, n: int) -> bytes:
        cached = self._cache.get(n)
        if cached is not None:
            self._cache.move_to_end(n)
            return cached
        t = self._table_for(n)
        i = n - t.first_chunk
        e = t.entries[i]
        start = t.base + (e & 0x7FFFFFFF)
        if i + 1 < len(t.entries):
            end = t.base + (t.entries[i + 1] & 0x7FFFFFFF)
        else:  # the last chunk of a table runs to the end of the section holding it
            starts, ends = self._bounds[t.seg]
            end = ends[max(0, bisect.bisect_right(starts, start) - 1)]
        if end <= start or end - start > self.chunk_size + 1024:
            end = start + self.chunk_size + 4  # damaged table: assume a stored chunk
        f = self._files[t.seg]
        f.seek(start)
        raw = f.read(end - start)
        want = self.chunk_size if n < self.chunk_count - 1 else self.size - n * self.chunk_size
        want = max(0, min(self.chunk_size, want))
        try:
            data = zlib.decompressobj().decompress(raw, self.chunk_size) if e & 0x80000000 else raw[:want]
        except zlib.error:
            data = b""
        if len(data) < want:  # unreadable chunk: zero-fill, the way EnCase reads an acquisition error
            self.bad_chunks += 1
            data = data + bytes(want - len(data))
        data = data[:want]
        self._cache[n] = data
        if len(self._cache) > CHUNK_CACHE:
            self._cache.popitem(last=False)
        return data

    def read(self, offset: int, size: int) -> bytes:
        if offset >= self.size or size <= 0:
            return b""
        size = min(size, self.size - offset)
        cs = self.chunk_size
        first, last = offset // cs, (offset + size - 1) // cs
        if first == last:
            c = self._chunk(first)
            return c[offset - first * cs: offset - first * cs + size]
        parts = [self._chunk(n) for n in range(first, last + 1)]
        parts[0] = parts[0][offset - first * cs:]
        buf = b"".join(parts)
        return buf[:size]

    def info(self) -> dict:
        return {
            "format": self.format,
            "path": str(self.path),
            "segments": [str(p) for p in self.paths],
            "size": self.size,
            "bytes_per_sector": self.bytes_per_sector,
            "chunk_size": self.chunk_size,
            "header": self.header,
            "md5": self.md5,
            "sha1": self.sha1,
            "acquisition_errors": len(self.errors),
        }


class RawImage:
    format = "Raw"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        # .001, .002 ... are one image split into pieces; read them as one.
        m = re.fullmatch(r"(.*)\.(\d{3})", self.path.name)
        if m and int(m.group(2)) == 1:
            parts, n = [], 1
            while (p := self.path.with_name(f"{m.group(1)}.{n:03d}")).exists():
                parts.append(p)
                n += 1
        else:
            parts = [self.path]
        self.paths = parts
        self._files = [open(p, "rb") for p in parts]  # noqa: SIM115
        self._starts = []
        total = 0
        for f in self._files:
            self._starts.append(total)
            f.seek(0, 2)
            total += f.tell()
        self.size = total
        self.bytes_per_sector = 512
        self.bad_chunks = 0

    def close(self) -> None:
        for f in self._files:
            f.close()
        self._files = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def read(self, offset: int, size: int) -> bytes:
        out = []
        while size > 0 and offset < self.size:
            i = bisect.bisect_right(self._starts, offset) - 1
            f = self._files[i]
            f.seek(offset - self._starts[i])
            end = self._starts[i + 1] if i + 1 < len(self._starts) else self.size
            data = f.read(min(size, end - offset))
            if not data:
                break
            out.append(data)
            offset += len(data)
            size -= len(data)
        return b"".join(out)

    def info(self) -> dict:
        return {
            "format": self.format, "path": str(self.path), "segments": [str(p) for p in self.paths],
            "size": self.size, "bytes_per_sector": self.bytes_per_sector, "header": {},
            "md5": None, "sha1": None, "acquisition_errors": 0,
        }


class Slice:
    """A window onto part of an image (one partition), addressed from 0."""

    def __init__(self, image, offset: int, size: int) -> None:
        self.image, self.offset, self.size = image, offset, size

    def read(self, offset: int, size: int) -> bytes:
        if offset >= self.size:
            return b""
        return self.image.read(self.offset + offset, min(size, self.size - offset))


def open_image(path: str | Path):
    p = Path(path)
    if not p.is_file():
        raise ImageError(f"File not found: {path}")
    with open(p, "rb") as f:
        sig = f.read(8)
    if sig in (EWF_SIGNATURE, EWF2_SIGNATURE):
        return EwfImage(p)
    if p.suffix.lower() in (".e01", ".ex01", ".s01", ".l01"):
        raise ImageError(f"{p.name} doesn't start with an EWF signature: it may be damaged or not the first segment")
    return RawImage(p)
