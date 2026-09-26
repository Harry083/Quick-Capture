"""Native EWF-E01 (EnCase 6 layout) writer and reader.

The writer mirrors the section layout libewf's `ewfacquire -f encase6` produces:

    segment 1:  header2, header2, header, volume, [sectors, table, table2]..., (data), [error2], [digest], hash, done
    segment N:  data, [sectors, table, table2]..., next | ... done

Chunks are 32 KiB (64 x 512-byte sectors). Compression runs on a thread pool — zlib releases the GIL, so
this scales across cores — and the next block is compressed while the previous one is being written.
All-zero chunks are compressed once and reused, which makes empty disk space almost free, and in "fast"
mode chunks that look incompressible (encrypted volumes, media files) are stored raw without a full
compression attempt.
"""
from __future__ import annotations

import os
import platform
import struct
import time
import uuid
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SIGNATURE = b"EVF\x09\x0d\x0a\xff\x00"
SECTORS_PER_CHUNK = 64
MAX_TABLE_ENTRIES = 16375  # the classic EnCase limit; keeps every table readable by older tools
COMPRESSION_LEVELS = {"none": None, "fast": 1, "best": 9}
VOLUME_COMPRESSION_BYTE = {"none": 0, "fast": 1, "best": 2}
HEADER_COMPRESSION_CHAR = {"none": "n", "fast": "f", "best": "b"}
# Room kept free at the end of a segment for the table/table2 pair and the closing sections.
SEGMENT_TAIL_RESERVE = 2 * (76 + 24 + 4 * MAX_TABLE_ENTRIES + 4) + 64 * 1024

APP_VERSION = "QC 1.0"


def segment_extension(n: int) -> str:
    """1 -> E01 ... 99 -> E99, 100 -> EAA ... EZZ, FAA ... ZZZ."""
    if n < 100:
        return f"E{n:02d}"
    n -= 100
    first, rest = divmod(n, 26 * 26)
    if first > ord("Z") - ord("E"):
        raise ValueError("Too many segment files")
    return chr(ord("E") + first) + chr(65 + rest // 26) + chr(65 + rest % 26)


def _adler(data) -> bytes:
    return struct.pack("<I", zlib.adler32(data) & 0xFFFFFFFF)


def _descriptor(kind: bytes, offset: int, size: int, next_offset: int | None = None) -> bytes:
    nxt = offset + size if next_offset is None else next_offset
    body = struct.pack("<16sQQ40x", kind, nxt, size)
    return body + _adler(body)


def _clean(value) -> str:
    return " ".join(str(value or "").replace("\t", " ").splitlines()).strip()


def _looks_incompressible(chunk: bytes, level: int) -> bool:
    """Compress a 2 KiB sample (start + middle) instead of the whole chunk. Encrypted or already-compressed
    data costs zlib ~60 MB/s per core for no gain; the probe spots it at a fraction of that cost."""
    mid = len(chunk) // 2
    sample = chunk[:1024] + chunk[mid:mid + 1024]
    return len(zlib.compress(sample, level)) >= len(sample) * 0.97


def _pack_chunks(
    chunks: list[bytes], level: int | None, zero_chunk: bytes, zero_packed: bytes, probe: bool
) -> list[tuple[bytes, bytes, bool]]:
    """Returns (payload, trailing checksum, compressed) per chunk. The checksum of a raw chunk is kept
    separate so the chunk is never copied just to append 4 bytes."""
    out = []
    for chunk in chunks:
        if len(chunk) == len(zero_chunk) and chunk == zero_chunk:
            out.append((zero_packed, b"", True))
            continue
        if level is not None and not (probe and _looks_incompressible(chunk, level)):
            packed = zlib.compress(chunk, level)
            # Per the spec a chunk is stored raw when compressing doesn't make it smaller.
            if len(packed) < len(chunk):
                out.append((packed, b"", True))
                continue
        out.append((chunk, _adler(chunk), False))
    return out


class EwfWriter:
    def __init__(
        self,
        base_path: str | Path,
        media_size: int,
        *,
        bytes_per_sector: int = 512,
        compression: str = "fast",
        segment_size: int | None = None,
        case_info: dict | None = None,
        device_info: dict | None = None,
        removable: bool = False,
        physical: bool = True,
        workers: int | None = None,
    ) -> None:
        if compression not in COMPRESSION_LEVELS:
            raise ValueError(f"Unknown compression: {compression}")
        self.base_path = Path(base_path)
        self.media_size = media_size
        self.bytes_per_sector = bytes_per_sector
        self.chunk_size = SECTORS_PER_CHUNK * bytes_per_sector
        self.sector_count = -(-media_size // bytes_per_sector)
        self.chunk_count = -(-self.sector_count // SECTORS_PER_CHUNK)
        self.compression = compression
        self.level = COMPRESSION_LEVELS[compression]
        self.segment_size = segment_size
        self.case_info = case_info or {}
        self.device_info = device_info or {}
        self.removable = removable
        self.physical = physical
        self.set_id = uuid.uuid4().bytes

        self._workers = workers or max(1, (os.cpu_count() or 2))
        self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="ewf-zlib")
        self._zero_chunk = bytes(self.chunk_size)
        self._zero_packed = zlib.compress(self._zero_chunk, self.level if self.level is not None else 1)
        self._inflight: deque = deque()
        self._pending = b""
        self._parts: list[bytes] = []  # chunk data queued for one gathered write per block

        self.paths: list[str] = []
        self._f = None
        self._pos = 0
        self._seg_no = 0
        self._sectors_start: int | None = None
        self._entries: list[int] = []
        self._chunks_in_segment = 0
        self.chunks_written = 0
        self.bytes_written = 0
        self._acquired_at = time.localtime()
        self._open_segment()

    # ------------------------------------------------------------ public API

    def write(self, data: bytes) -> None:
        if self._pending:
            data = self._pending + data
            self._pending = b""
        cs = self.chunk_size
        whole = len(data) - len(data) % cs
        if whole < len(data):
            self._pending = data[whole:]
        if whole:
            self._submit([data[i:i + cs] for i in range(0, whole, cs)])

    def finalize(self, md5: bytes | None, sha1: bytes | None = None, errors: list[tuple[int, int]] | None = None) -> list[str]:
        if self._pending:
            tail = self._pending
            self._pending = b""
            rem = len(tail) % self.bytes_per_sector
            if rem:  # E01 stores whole sectors; pad a non-sector-aligned source with zeros
                tail += bytes(self.bytes_per_sector - rem)
            self._submit([tail])
        while self._inflight:
            self._drain_one()
        self._pool.shutdown(wait=True)

        self._close_sectors_group()
        if self._seg_no == 1:
            self._write_section(b"data", self._volume_data())
        if errors:
            self._write_error2(errors)
        if md5 is not None and sha1 is not None:
            self._write_section(b"digest", md5 + sha1 + bytes(40), checksum=True)
        if md5 is not None:
            self._write_section(b"hash", md5 + bytes(16), checksum=True)
        self._f.write(_descriptor(b"done", self._pos, 0, next_offset=self._pos))
        self._pos += 76
        self._f.close()
        self._f = None
        return self.paths

    def abort(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._f is not None:
            self._f.close()
            self._f = None

    # ------------------------------------------------------------ chunk pipeline

    def _submit(self, chunks: list[bytes]) -> None:
        n = len(chunks)
        groups = max(1, min(self._workers, n))
        step = -(-n // groups)
        futures = [
            self._pool.submit(
                _pack_chunks, chunks[i:i + step], self.level, self._zero_chunk, self._zero_packed,
                self.compression == "fast",
            )
            for i in range(0, n, step)
        ]
        self._inflight.append(futures)
        # Keep one block compressing in the background while the previous one is written.
        while len(self._inflight) > 2:
            self._drain_one()

    def _drain_one(self) -> None:
        for fut in self._inflight.popleft():
            for payload, checksum, compressed in fut.result():
                self._write_chunk(payload, checksum, compressed)
        self._flush()

    def _flush(self) -> None:
        # One large write per block instead of hundreds of 32 KiB ones: fewer syscalls and Python calls,
        # and writes bigger than the file buffer skip its extra copy.
        if self._parts:
            self._f.write(b"".join(self._parts))
            self._parts = []

    def _write_chunk(self, payload: bytes, checksum: bytes, compressed: bool) -> None:
        size = len(payload) + len(checksum)
        if len(self._entries) >= MAX_TABLE_ENTRIES:
            self._close_sectors_group()
        if (
            self.segment_size
            and self._chunks_in_segment
            and self._pos + size + SEGMENT_TAIL_RESERVE > self.segment_size
        ):
            self._close_sectors_group()
            self._f.write(_descriptor(b"next", self._pos, 0, next_offset=self._pos))
            self._f.close()
            self._open_segment()
        if self._sectors_start is None:
            self._sectors_start = self._pos
            self._parts.append(bytes(76))  # descriptor is patched in once the section's size is known
            self._pos += 76
        self._entries.append((self._pos - self._sectors_start) | (0x80000000 if compressed else 0))
        self._parts.append(payload)
        if checksum:
            self._parts.append(checksum)
        self._pos += size
        self._chunks_in_segment += 1
        self.chunks_written += 1
        self.bytes_written += size

    # ------------------------------------------------------------ sections

    def _open_segment(self) -> None:
        self._seg_no += 1
        path = self.base_path.with_name(f"{self.base_path.name}.{segment_extension(self._seg_no)}")
        self._f = open(path, "xb", buffering=4 * 1024 * 1024)  # noqa: SIM115  (never overwrite evidence)
        self.paths.append(str(path))
        self._pos = 0
        self._chunks_in_segment = 0
        self._f.write(SIGNATURE + b"\x01" + struct.pack("<H", self._seg_no) + b"\x00\x00")
        self._pos = 13
        if self._seg_no == 1:
            h2 = self._header2()
            self._write_section(b"header2", h2)
            self._write_section(b"header2", h2)
            self._write_section(b"header", self._header())
            self._write_section(b"volume", self._volume_data())
        else:
            self._write_section(b"data", self._volume_data())

    def _write_section(self, kind: bytes, data: bytes, checksum: bool = False) -> None:
        self._flush()
        if checksum:
            data = data + _adler(data)
        size = 76 + len(data)
        self._f.write(_descriptor(kind, self._pos, size))
        self._f.write(data)
        self._pos += size

    def _close_sectors_group(self) -> None:
        self._flush()
        if self._sectors_start is None:
            return
        start, end = self._sectors_start, self._pos
        self._f.seek(start)
        self._f.write(_descriptor(b"sectors", start, end - start))
        self._f.seek(end)

        n = len(self._entries)
        header = struct.pack("<I4xQ4x", n, start)
        entries = struct.pack(f"<{n}I", *self._entries)
        table = header + _adler(header) + entries + _adler(entries)
        self._write_section(b"table", table)
        self._write_section(b"table2", table)
        self._sectors_start = None
        self._entries = []

    def _write_error2(self, errors: list[tuple[int, int]]) -> None:
        header = struct.pack("<I", len(errors)) + bytes(512)
        entries = b"".join(struct.pack("<II", start, count) for start, count in errors)
        self._write_section(b"error2", header + _adler(header) + entries + _adler(entries))

    def _volume_data(self) -> bytes:
        v = bytearray(1052)
        v[0] = 0x00 if self.removable else 0x01
        struct.pack_into("<IIIQ", v, 4, self.chunk_count, SECTORS_PER_CHUNK, self.bytes_per_sector, self.sector_count)
        v[36] = 0x01 | (0x02 if self.physical else 0)
        v[52] = VOLUME_COMPRESSION_BYTE[self.compression]
        struct.pack_into("<I", v, 56, SECTORS_PER_CHUNK)  # error granularity
        v[64:80] = self.set_id
        v[1048:1052] = _adler(bytes(v[:1048]))
        return bytes(v)

    def _header(self) -> bytes:
        c = self.case_info
        t = self._acquired_at
        date = f"{t.tm_year} {t.tm_mon} {t.tm_mday} {t.tm_hour} {t.tm_min} {t.tm_sec}"
        values = [
            _clean(c.get("case_number")), _clean(c.get("evidence_number")), _clean(c.get("description")),
            _clean(c.get("examiner")), _clean(c.get("notes")), APP_VERSION, platform.system(), date, date, "0",
        ]
        text = "1\r\nmain\r\nc\tn\ta\te\tt\tav\tov\tm\tu\tp\r\n" + "\t".join(values) + "\r\n\r\n"
        return zlib.compress(text.encode("ascii", "replace"))

    def _header2(self) -> bytes:
        c = self.case_info
        epoch = str(int(time.mktime(self._acquired_at)))
        values = [
            _clean(c.get("description")), _clean(c.get("case_number")), _clean(c.get("evidence_number")),
            _clean(c.get("examiner")), _clean(c.get("notes")), _clean(self.device_info.get("model")),
            _clean(self.device_info.get("serial")), APP_VERSION, platform.system(), epoch, epoch, "0", "",
        ]
        text = (
            "3\nmain\na\tc\tn\te\tt\tmd\tsn\tav\tov\tm\tu\tp\tdc\n" + "\t".join(values) + "\n\n"
            "srce\n0\t1\np\tn\tid\tev\ttb\tlo\tpo\tah\tgu\taq\n0\t0\n\t\t\t\t\t-1\t-1\t\t\t\n\n"
            "sub\n0\t1\np\tn\tid\tnu\tco\tgu\n0\t0\n\t\t\t\t1\t\n\n"
        )
        return zlib.compress(b"\xff\xfe" + text.encode("utf-16-le"))


# ---------------------------------------------------------------- reader (used for verification)

def segment_paths(first: str | Path) -> list[Path]:
    first = Path(first)
    stem = first.with_suffix("")
    paths = []
    n = 1
    while True:
        p = stem.with_name(f"{stem.name}.{segment_extension(n)}")
        if not p.exists():
            break
        paths.append(p)
        n += 1
    return paths


def _read_section_list(f) -> list[tuple[bytes, int, int]]:
    f.seek(0)
    head = f.read(13)
    if head[:8] != SIGNATURE:
        raise ValueError("Not an EWF-E01 segment file")
    sections = []
    off = 13
    while True:
        f.seek(off)
        desc = f.read(76)
        if len(desc) < 76:
            break
        kind, nxt, size = struct.unpack("<16sQQ", desc[:32])
        if _adler(desc[:72]) != desc[72:76]:
            raise ValueError(f"Section descriptor checksum mismatch at offset {off}")
        kind = kind.rstrip(b"\0")
        sections.append((kind, off, size))
        if kind in (b"done", b"next") or nxt == off:
            break
        off = nxt
    return sections


def iter_media(first_segment: str | Path, workers: int | None = None, batch: int = 256):
    """Yield the media data of an E01 image in order, decompressing chunks on a thread pool."""
    pool = ThreadPoolExecutor(max_workers=workers or (os.cpu_count() or 2))

    def unpack(item):
        raw, compressed = item
        if compressed:
            return zlib.decompress(raw)
        data, stored = raw[:-4], raw[-4:]
        if _adler(data) != stored:
            raise ValueError("Chunk checksum mismatch")
        return data

    try:
        for path in segment_paths(first_segment):
            with open(path, "rb") as f:
                sections = _read_section_list(f)
                sectors_end = None
                for kind, off, size in sections:
                    if kind == b"sectors":
                        sectors_end = off + size
                    elif kind == b"table":
                        f.seek(off + 76)
                        n, base = struct.unpack("<I4xQ", f.read(16))
                        f.seek(off + 76 + 24)
                        entries = struct.unpack(f"<{n}I", f.read(4 * n))
                        items = []
                        for i, e in enumerate(entries):
                            start = base + (e & 0x7FFFFFFF)
                            end = base + (entries[i + 1] & 0x7FFFFFFF) if i + 1 < n else (sectors_end or off)
                            f.seek(start)
                            items.append((f.read(end - start), bool(e & 0x80000000)))
                            if len(items) >= batch:
                                yield from pool.map(unpack, items)
                                items = []
                        yield from pool.map(unpack, items)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def media_size(first_segment: str | Path) -> int:
    with open(first_segment, "rb") as f:
        for kind, off, _ in _read_section_list(f):
            if kind in (b"volume", b"disk"):
                f.seek(off + 76)
                _, _, bps, sectors = struct.unpack_from("<IIIQ", f.read(24), 4)
                return bps * sectors
    raise ValueError("No volume section found")
