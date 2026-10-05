"""A read-only, pure-Python parser for the SQLite 3 file format.

It reads the raw bytes itself rather than going through the sqlite3 library, so it can see what the library
hides: free pages, freeblocks, unallocated space inside pages, superseded WAL frames and rollback-journal
pages. The live data is still read through sqlite3 (see session.py); this module is for everything else.

Format reference: https://www.sqlite.org/fileformat2.html
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"SQLite format 3\x00"
WAL_MAGICS = (0x377F0682, 0x377F0683)
JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")

PAGE_TYPES = {
    0x02: "index_interior",
    0x05: "table_interior",
    0x0A: "index_leaf",
    0x0D: "table_leaf",
}
ENCODINGS = {1: "utf-8", 2: "utf-16-le", 3: "utf-16-be"}


class FormatError(Exception):
    """The file is not a SQLite database (or WAL / journal) we can parse."""


# ---------------------------------------------------------------- primitives
def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Decode a SQLite varint at buf[pos]. Returns (value, bytes used). Raises IndexError past the end."""
    value = 0
    for i in range(8):
        byte = buf[pos + i]
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, i + 1
    value = (value << 8) | buf[pos + 8]
    if value & (1 << 63):  # varints are signed 64-bit
        value -= 1 << 64
    return value, 9


def serial_size(serial_type: int) -> int:
    """Bytes of record body used by a serial type, or -1 for the reserved types 10 and 11."""
    if serial_type < 12:
        return (0, 1, 2, 3, 4, 6, 8, 8, 0, 0, -1, -1)[serial_type]
    return (serial_type - 12) // 2 if serial_type % 2 == 0 else (serial_type - 13) // 2


def decode_value(serial_type: int, raw: bytes, encoding: str = "utf-8", strict: bool = False):
    """Turn one record field into a Python value. Text that can't be decoded comes back as bytes unless strict,
    in which case UnicodeDecodeError propagates (the carver uses that to reject false positives)."""
    if serial_type == 0:
        return None
    if 1 <= serial_type <= 6:
        return int.from_bytes(raw, "big", signed=True)
    if serial_type == 7:
        return struct.unpack(">d", raw)[0]
    if serial_type == 8:
        return 0
    if serial_type == 9:
        return 1
    if serial_type >= 12 and serial_type % 2 == 0:
        return bytes(raw)
    if serial_type >= 13:
        if strict:
            return raw.decode(encoding)
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            return bytes(raw)
    raise ValueError(f"reserved serial type {serial_type}")


def parse_record(payload: bytes, encoding: str = "utf-8") -> tuple[list[int], list]:
    """Parse a complete record (header + body). Returns (serial types, values)."""
    header_len, n = read_varint(payload, 0)
    if header_len < 1 or header_len > len(payload):
        raise FormatError("bad record header length")
    pos, types = n, []
    while pos < header_len:
        st, n = read_varint(payload, pos)
        pos += n
        types.append(st)
    values, body = [], header_len
    for st in types:
        size = serial_size(st)
        if size < 0 or body + size > len(payload):
            raise FormatError("record body overruns payload")
        values.append(decode_value(st, payload[body:body + size], encoding))
        body += size
    return types, values


# ---------------------------------------------------------------- header
@dataclass
class DbHeader:
    page_size: int
    write_version: int
    read_version: int
    reserved: int
    change_counter: int
    page_count: int
    freelist_trunk: int
    freelist_count: int
    schema_cookie: int
    schema_format: int
    default_cache: int
    autovacuum_root: int
    text_encoding: int
    user_version: int
    incremental_vacuum: int
    application_id: int
    version_valid_for: int
    sqlite_version: int

    @property
    def usable_size(self) -> int:
        return self.page_size - self.reserved

    @property
    def encoding(self) -> str:
        return ENCODINGS.get(self.text_encoding, "utf-8")

    @property
    def journal_mode(self) -> str:
        return "wal" if self.write_version == 2 or self.read_version == 2 else "rollback"

    def as_dict(self) -> dict:
        v = self.sqlite_version
        return {
            "page_size": self.page_size,
            "page_count": self.page_count,
            "reserved_bytes": self.reserved,
            "journal_mode": self.journal_mode,
            "text_encoding": self.encoding,
            "change_counter": self.change_counter,
            "freelist_trunk": self.freelist_trunk,
            "freelist_count": self.freelist_count,
            "schema_cookie": self.schema_cookie,
            "schema_format": self.schema_format,
            "auto_vacuum": bool(self.autovacuum_root),
            "incremental_vacuum": bool(self.incremental_vacuum),
            "user_version": self.user_version,
            "application_id": self.application_id,
            "sqlite_version": f"{v // 1000000}.{v // 1000 % 1000}.{v % 1000}" if v else "",
            "version_valid_for": self.version_valid_for,
        }


def parse_header(data: bytes) -> DbHeader:
    if len(data) < 100 or data[:16] != MAGIC:
        raise FormatError("Not a SQLite 3 database (missing 'SQLite format 3' header)")
    f = struct.unpack(">HBBBBBBIIIIIIIIIIII20xII", data[16:100])
    page_size = 65536 if f[0] == 1 else f[0]
    if page_size < 512 or page_size & (page_size - 1):
        raise FormatError(f"Invalid page size {page_size}")
    return DbHeader(
        page_size=page_size, write_version=f[1], read_version=f[2], reserved=f[3],
        change_counter=f[7], page_count=f[8], freelist_trunk=f[9], freelist_count=f[10],
        schema_cookie=f[11], schema_format=f[12], default_cache=f[13], autovacuum_root=f[14],
        text_encoding=f[15], user_version=f[16], incremental_vacuum=f[17], application_id=f[18],
        version_valid_for=f[19], sqlite_version=f[20],
    )


# ---------------------------------------------------------------- b-tree pages
@dataclass
class Cell:
    offset: int  # within the page
    size: int  # bytes the cell occupies on this page
    left_child: int | None = None
    rowid: int | None = None
    payload_size: int = 0
    payload: bytes = b""  # the complete payload (overflow followed), or as much as was readable
    overflow_page: int = 0
    types: list = field(default_factory=list)
    values: list = field(default_factory=list)
    error: str = ""


@dataclass
class BTreePage:
    number: int
    kind: str
    header_offset: int
    first_freeblock: int
    cell_count: int
    content_start: int
    fragmented: int
    right_child: int | None
    cell_pointers: list[int]

    @property
    def is_leaf(self) -> bool:
        return self.kind.endswith("leaf")

    @property
    def is_table(self) -> bool:
        return self.kind.startswith("table")

    @property
    def header_size(self) -> int:
        return 8 if self.is_leaf else 12

    @property
    def pointer_array_end(self) -> int:
        return self.header_offset + self.header_size + 2 * self.cell_count


def parse_btree_header(page: bytes, number: int, usable: int) -> BTreePage | None:
    """Parse a b-tree page header; None if the bytes don't look like one."""
    off = 100 if number == 1 else 0
    if len(page) < off + 8:
        return None
    kind = PAGE_TYPES.get(page[off])
    if kind is None:
        return None
    first_fb, ncells, content, frag = struct.unpack(">HHHB", page[off + 1:off + 8])
    content = 65536 if content == 0 else content
    right = None
    hsize = 8
    if not kind.endswith("leaf"):
        right = struct.unpack(">I", page[off + 8:off + 12])[0]
        hsize = 12
    ptr_end = off + hsize + 2 * ncells
    if ptr_end > usable or content > usable + 1 or (ncells and content < ptr_end):
        return None
    if first_fb and not (ptr_end <= first_fb < usable):
        return None
    ptrs = list(struct.unpack(f">{ncells}H", page[off + hsize:ptr_end])) if ncells else []
    return BTreePage(number, kind, off, first_fb, ncells, content, frag, right, ptrs)


def local_payload_size(payload_size: int, usable: int, is_table_leaf: bool) -> int:
    """How much of a payload is stored on the b-tree page itself (the rest spills to overflow pages)."""
    max_local = usable - 35 if is_table_leaf else (usable - 12) * 64 // 255 - 23
    if payload_size <= max_local:
        return payload_size
    min_local = (usable - 12) * 32 // 255 - 23
    k = min_local + (payload_size - min_local) % (usable - 4)
    return k if k <= max_local else min_local


def freeblocks(page: bytes, hdr: BTreePage, usable: int) -> list[tuple[int, int]]:
    """The page's freeblock chain as (offset, size). Stops on loops or out-of-range links."""
    out, seen, off = [], set(), hdr.first_freeblock
    while off and off not in seen and off + 4 <= usable:
        seen.add(off)
        nxt, size = struct.unpack(">HH", page[off:off + 4])
        if size < 4 or off + size > usable:
            break
        out.append((off, size))
        off = nxt
    return out


# ---------------------------------------------------------------- WAL
@dataclass
class WalFrame:
    index: int  # 0-based position in the file
    offset: int  # file offset of the frame header
    page_number: int
    commit_size: int  # db size in pages after this commit; 0 if not a commit frame
    salt1: int
    salt2: int
    checksum_ok: bool
    salt_ok: bool  # salts match the WAL header (frames from before the last restart don't)

    @property
    def valid(self) -> bool:
        return self.checksum_ok and self.salt_ok

    @property
    def is_commit(self) -> bool:
        return self.commit_size > 0


@dataclass
class WalFile:
    path: str
    page_size: int
    checkpoint_seq: int
    salt1: int
    salt2: int
    big_endian_checksum: bool
    frames: list[WalFrame]
    size: int

    def frame_page(self, data: bytes, frame: WalFrame) -> bytes:
        start = frame.offset + 24
        return data[start:start + self.page_size]

    @property
    def valid_frames(self) -> list[WalFrame]:
        """Frames SQLite would actually read: the checksum chain from the start, up to the last commit."""
        last_commit = -1
        for f in self.frames:
            if not f.valid:
                break
            if f.is_commit:
                last_commit = f.index
        return self.frames[:last_commit + 1]

    def commits(self) -> list[dict]:
        """Group the valid frames into transactions, one entry per commit frame."""
        out, pages, start = [], [], 0
        for f in self.valid_frames:
            pages.append(f.page_number)
            if f.is_commit:
                out.append({"commit": len(out) + 1, "first_frame": start, "last_frame": f.index,
                            "frames": f.index - start + 1, "pages": sorted(set(pages)), "db_pages": f.commit_size})
                pages, start = [], f.index + 1
        return out


def _wal_checksum(data: bytes, s0: int, s1: int, big: bool) -> tuple[int, int]:
    words = struct.unpack((">" if big else "<") + f"{len(data) // 4}I", data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def parse_wal(data: bytes, path: str = "") -> WalFile:
    if len(data) < 32:
        raise FormatError("WAL file is too short")
    magic, version, page_size, ckpt, salt1, salt2, c1, c2 = struct.unpack(">8I", data[:32])
    if magic not in WAL_MAGICS:
        raise FormatError("Not a SQLite WAL file (bad magic)")
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise FormatError(f"WAL has an invalid page size {page_size}")
    big = magic == 0x377F0683
    s0, s1 = _wal_checksum(data[:24], 0, 0, big)
    header_ok = (s0, s1) == (c1, c2)
    frames: list[WalFrame] = []
    chain_ok = header_ok
    off, frame_size = 32, 24 + page_size
    while off + frame_size <= len(data):
        pg, commit, fs1, fs2, fc1, fc2 = struct.unpack(">6I", data[off:off + 24])
        salt_ok = (fs1, fs2) == (salt1, salt2)
        ck_ok = False
        if chain_ok and salt_ok:
            n0, n1 = _wal_checksum(data[off:off + 8], s0, s1, big)
            n0, n1 = _wal_checksum(data[off + 24:off + frame_size], n0, n1, big)
            ck_ok = (n0, n1) == (fc1, fc2)
            if ck_ok:
                s0, s1 = n0, n1
            else:
                chain_ok = False
        frames.append(WalFrame(len(frames), off, pg, commit, fs1, fs2, ck_ok, salt_ok))
        off += frame_size
    return WalFile(path, page_size, ckpt, salt1, salt2, big, frames, len(data))


# ---------------------------------------------------------------- rollback journal
@dataclass
class JournalPage:
    index: int
    offset: int
    page_number: int


@dataclass
class JournalFile:
    path: str
    record_count: int
    nonce: int
    initial_pages: int
    sector_size: int
    page_size: int
    pages: list[JournalPage]
    zeroed_header: bool  # journal_mode=PERSIST zeroes the header but leaves the pages behind

    def page_data(self, data: bytes, page: JournalPage) -> bytes:
        return data[page.offset + 4:page.offset + 4 + self.page_size]


def parse_journal(data: bytes, page_size_hint: int = 0, path: str = "") -> JournalFile:
    """Parse a rollback journal. Each record is the original content of a page before a transaction changed
    it, so a journal left behind (hot, or kept by PERSIST/TRUNCATE modes) is a window onto older data."""
    if len(data) < 28:
        raise FormatError("Journal file is too short")
    zeroed = data[:8] != JOURNAL_MAGIC
    if zeroed and any(data[:28]):
        raise FormatError("Not a SQLite rollback journal (bad magic)")
    count, nonce, initial, sector, page_size = struct.unpack(">5I", data[8:28])
    if zeroed or not page_size:
        page_size, sector = page_size_hint, sector or 512
    if not page_size or page_size & (page_size - 1):
        raise FormatError("Journal page size is unknown; open it next to its database")
    sector = sector if sector and not sector & (sector - 1) else 512
    pages, off, rec = [], sector, 4 + page_size + 4
    while off + rec <= len(data):
        pg = struct.unpack(">I", data[off:off + 4])[0]
        if pg == 0 or pg > 0x7FFFFFFF:
            break
        pages.append(JournalPage(len(pages), off, pg))
        off += rec
        if not zeroed and count != 0xFFFFFFFF and len(pages) >= count and count:
            # a hot journal can hold more than one segment; each new one starts on a sector boundary
            off = -(-off // sector) * sector
            if off + 28 <= len(data) and data[off:off + 8] == JOURNAL_MAGIC:
                count = struct.unpack(">I", data[off + 8:off + 12])[0]
                off += sector
            else:
                break
    return JournalFile(path, count, nonce, initial, sector, page_size, pages, zeroed)


# ---------------------------------------------------------------- the database
class SqliteFile:
    """Raw access to a database image, optionally overlaid with WAL frames.

    `overlay` maps page number -> page bytes; it lets the same code walk the database as it stood at any WAL
    commit, or the main file alone."""

    def __init__(self, data: bytes, overlay: dict[int, bytes] | None = None, page_count: int | None = None):
        self.data = data
        self.overlay = overlay or {}
        page1 = self.overlay.get(1, data[:100])
        self.header = parse_header(page1 if len(page1) >= 100 else data[:100])
        self.page_size = self.header.page_size
        self.usable = self.header.usable_size
        self.encoding = self.header.encoding
        in_file = len(data) // self.page_size
        # the header's page count is only trusted when "version-valid-for" matches the change counter
        hdr_count = self.header.page_count if self.header.version_valid_for == self.header.change_counter else 0
        self.page_count = page_count or max(hdr_count, in_file, max(self.overlay, default=0))

    @classmethod
    def open(cls, path: str | Path) -> "SqliteFile":
        return cls(Path(path).read_bytes())

    def page(self, number: int) -> bytes:
        if number in self.overlay:
            return self.overlay[number]
        if number < 1 or number > self.page_count:
            raise FormatError(f"page {number} out of range")
        start = (number - 1) * self.page_size
        chunk = self.data[start:start + self.page_size]
        return chunk.ljust(self.page_size, b"\x00")

    # ---- cells
    def read_overflow(self, first: int, needed: int, max_pages: int = 100000) -> tuple[bytes, list[int]]:
        out, pages, pg = bytearray(), [], first
        while pg and len(out) < needed and len(pages) < max_pages:
            if pg in pages or pg > self.page_count:
                break
            pages.append(pg)
            raw = self.page(pg)
            out += raw[4:self.usable][: needed - len(out)]
            pg = struct.unpack(">I", raw[:4])[0]
        return bytes(out), pages

    def parse_cell(self, page: bytes, hdr: BTreePage, offset: int, decode: bool = True) -> Cell:
        cell = Cell(offset=offset, size=0)
        pos = offset
        try:
            if not hdr.is_leaf:
                cell.left_child = struct.unpack(">I", page[pos:pos + 4])[0]
                pos += 4
            if hdr.kind == "table_interior":
                cell.rowid, n = read_varint(page, pos)
                cell.size = pos + n - offset
                return cell
            cell.payload_size, n = read_varint(page, pos)
            pos += n
            if hdr.kind == "table_leaf":
                cell.rowid, n = read_varint(page, pos)
                pos += n
            local = local_payload_size(cell.payload_size, self.usable, hdr.kind == "table_leaf")
            payload = page[pos:pos + local]
            pos += local
            if local < cell.payload_size:
                cell.overflow_page = struct.unpack(">I", page[pos:pos + 4])[0]
                pos += 4
                rest, _ = self.read_overflow(cell.overflow_page, cell.payload_size - local)
                payload += rest
            cell.size = pos - offset
            cell.payload = payload
            if decode:
                cell.types, cell.values = parse_record(payload, self.encoding)
        except (IndexError, FormatError, ValueError, struct.error) as exc:
            cell.error = str(exc) or type(exc).__name__
        return cell

    def btree_header(self, number: int) -> BTreePage | None:
        return parse_btree_header(self.page(number), number, self.usable)

    # ---- tree walks
    def walk(self, root: int, visit_cells: bool = True):
        """Yield (BTreePage, page bytes, [cells]) for every page of the b-tree rooted at `root`."""
        stack, seen = [root], set()
        while stack:
            num = stack.pop()
            if num in seen or num < 1 or num > self.page_count:
                continue
            seen.add(num)
            raw = self.page(num)
            hdr = parse_btree_header(raw, num, self.usable)
            if hdr is None:
                continue
            cells = []
            for ptr in hdr.cell_pointers:
                if ptr < hdr.pointer_array_end or ptr >= self.usable:
                    continue
                if visit_cells or not hdr.is_leaf:
                    cells.append(self.parse_cell(raw, hdr, ptr, decode=hdr.is_leaf))
            yield hdr, raw, cells
            if not hdr.is_leaf:
                children = [c.left_child for c in cells if c.left_child]
                if hdr.right_child:
                    children.append(hdr.right_child)
                stack.extend(reversed(children))

    def table_rows(self, root: int):
        """Yield every live row of a table b-tree as a Cell (rowid, types, values)."""
        for hdr, _raw, cells in self.walk(root):
            if hdr.kind == "table_leaf":
                yield from cells

    def schema(self) -> list[dict]:
        """sqlite_master, read from page 1's b-tree."""
        out = []
        for cell in self.table_rows(1):
            v = cell.values
            if cell.error or len(v) < 5:
                continue
            out.append({"type": v[0], "name": v[1], "tbl_name": v[2], "rootpage": v[3] or 0, "sql": v[4] or ""})
        return out

    def freelist(self) -> tuple[list[int], list[int]]:
        """(trunk pages, leaf pages) of the freelist."""
        trunks, leaves, pg = [], [], self.header.freelist_trunk
        while pg and pg not in trunks and pg <= self.page_count:
            trunks.append(pg)
            raw = self.page(pg)
            nxt, count = struct.unpack(">II", raw[:8])
            count = min(count, (self.usable - 8) // 4)
            for i in range(count):
                leaf = struct.unpack(">I", raw[8 + 4 * i:12 + 4 * i])[0]
                if 0 < leaf <= self.page_count:
                    leaves.append(leaf)
            pg = nxt
        return trunks, leaves

    def page_map(self, schema: list[dict] | None = None) -> list[dict]:
        """Classify every page: which b-tree owns it, its type, and how much free space it has."""
        schema = schema if schema is not None else self.schema()
        info: dict[int, dict] = {}

        def mark(num, **kw):
            info.setdefault(num, {"page": num}).update(kw)

        roots = [("sqlite_master", "table", 1)] + [
            (s["name"], s["type"], s["rootpage"]) for s in schema if s["rootpage"]
        ]
        for name, typ, root in roots:
            for hdr, raw, cells in self.walk(root, visit_cells=True):
                free = sum(size for _, size in freeblocks(raw, hdr, self.usable))
                unalloc = max(0, hdr.content_start - hdr.pointer_array_end)
                mark(hdr.number, type=hdr.kind, owner=name, owner_type=typ, cells=hdr.cell_count,
                     free_bytes=free + unalloc + hdr.fragmented, freeblocks=free, unallocated=unalloc)
                for c in cells:
                    if c.overflow_page:
                        _, ov = self.read_overflow(c.overflow_page, c.payload_size)
                        for p in ov:
                            mark(p, type="overflow", owner=name, owner_type=typ)
        trunks, leaves = self.freelist()
        for p in trunks:
            mark(p, type="freelist_trunk", owner="", owner_type="")
        for p in leaves:
            mark(p, type="freelist_leaf", owner="", owner_type="")
        if self.header.autovacuum_root:
            # pointer-map pages: page 2, then every (usable/5 + 1) pages
            step = self.usable // 5 + 1
            p = 2
            while p <= self.page_count:
                mark(p, type="ptrmap", owner="", owner_type="")
                p += step
        lock_page = 1073741824 // self.page_size + 1
        if lock_page <= self.page_count:
            mark(lock_page, type="lock_byte", owner="", owner_type="")
        out = []
        for num in range(1, self.page_count + 1):
            out.append(info.get(num) or {"page": num, "type": "unknown", "owner": "", "owner_type": ""})
        return out


def wal_overlay(wal: WalFile, wal_data: bytes, upto_commit: int | None = None) -> tuple[dict[int, bytes], int | None]:
    """The page images the WAL holds as of a commit (None = the last one), plus that commit's db size."""
    overlay: dict[int, bytes] = {}
    pending: dict[int, bytes] = {}
    size, commits = None, 0
    for f in wal.valid_frames:
        pending[f.page_number] = wal.frame_page(wal_data, f)
        if f.is_commit:
            overlay.update(pending)
            pending = {}
            size = f.commit_size
            commits += 1
            if upto_commit is not None and commits >= upto_commit:
                break
    return overlay, size
