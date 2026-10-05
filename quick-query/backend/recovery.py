"""Deleted-record recovery: carve records out of the parts of a database SQLite no longer points at.

Where deleted data survives:

- **Freeblocks**: when a row is deleted, its cell joins the page's freeblock chain. Only the first 4 bytes are
  overwritten (next-freeblock + size), which usually takes out the payload length, the rowid, the record
  header length and the first column's serial type. The rest of the record is intact.
- **Unallocated space**: the gap between the cell pointer array and the cell content area. Cells that were
  moved during defragmentation, and the cells of rows deleted before it, can survive here whole.
- **Freelist pages**: pages released by deletes or DROP TABLE. Leaf pages are left as they were (unless
  secure_delete is on), so their old cells are usually complete.
- **WAL frames and rollback-journal pages**: older copies of whole pages (see history.py).

Carved records are matched to a table by a signature: the column count plus the serial types each column's
declared affinity allows. Text must decode cleanly in the database's encoding. Short tables (one or two
columns) match random bytes easily, so those carves are marked low confidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sqlite_format import (
    BTreePage,
    SqliteFile,
    decode_value,
    freeblocks,
    parse_btree_header,
    read_varint,
    serial_size,
)

INT_TYPES = frozenset({0, 1, 2, 3, 4, 5, 6, 7, 8, 9})
INT_SIZE_TYPES = {0: (8, 9), 1: (1,), 2: (2,), 3: (3,), 4: (4,), 6: (5,), 8: (6, 7)}
MAX_COLUMNS = 2000


def affinity(decl_type: str) -> str:
    """SQLite's column affinity rules (https://www.sqlite.org/datatype3.html#determination_of_column_affinity)."""
    t = (decl_type or "").upper()
    if "INT" in t:
        return "INTEGER"
    if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in t or not t:
        return "BLOB"
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def type_allowed(aff: str, st: int, is_ipk: bool) -> bool:
    if st in (10, 11):
        return False
    if is_ipk:  # INTEGER PRIMARY KEY is stored as the rowid; the record holds NULL
        return st == 0
    if aff in ("INTEGER", "REAL"):
        return st in INT_TYPES
    if aff == "TEXT":  # TEXT columns can hold blobs, but allowing them lets any bytes match; carving wants text
        return st == 0 or (st >= 13 and st % 2 == 1)
    return True


@dataclass
class TableSig:
    name: str
    columns: list[str]
    affinities: list[str]
    ipk: int | None = None  # index of the INTEGER PRIMARY KEY column, if any
    without_rowid: bool = False
    root: int = 0

    @property
    def ncols(self) -> int:
        return len(self.columns)

    def allows(self, col: int, st: int) -> bool:
        return type_allowed(self.affinities[col], st, col == self.ipk)

    @classmethod
    def from_columns(cls, name: str, columns: list[dict], sql: str = "", root: int = 0) -> "TableSig":
        """columns: rows of PRAGMA table_info (name, type, pk)."""
        names = [c["name"] for c in columns]
        affs = [affinity(c.get("type", "")) for c in columns]
        pks = [i for i, c in enumerate(columns) if c.get("pk")]
        ipk = None
        if len(pks) == 1 and (columns[pks[0]].get("type") or "").strip().upper() == "INTEGER":
            ipk = pks[0]
        without_rowid = bool(re.search(r"\)\s*WITHOUT\s+ROWID\s*;?\s*$", sql or "", re.I))
        if without_rowid:
            ipk = None
        return cls(name, names, affs, ipk, without_rowid, root)


@dataclass
class Carved:
    table: str
    values: list
    types: list[int]
    rowid: int | None
    source: str  # freeblock | unallocated | freelist | wal | journal
    page: int
    offset: int
    confidence: str  # high | medium | low
    inferred: list[int] = field(default_factory=list)  # columns whose serial type was guessed
    frame: int | None = None  # WAL frame / journal record index
    note: str = ""
    length: int = 0  # bytes of the page the carved record spans (from offset)
    also: list[str] = field(default_factory=list)  # other places the same record was found

    def where(self) -> str:
        if self.source == "wal":
            return f"wal frame {self.frame}"
        if self.source == "journal":
            return f"journal record {self.frame}"
        return f"{self.source} page {self.page}" if self.page else self.source

    def key(self):
        vals = tuple(v if not isinstance(v, float) else round(v, 9) for v in self.values)
        return (self.table, self.rowid, vals)


# ---------------------------------------------------------------- record matching helpers
def _read_types(buf: bytes, pos: int, end: int, count: int) -> tuple[list[int], int] | None:
    types = []
    try:
        while len(types) < count:
            if pos >= end:
                return None
            st, n = read_varint(buf, pos)
            if st < 0:
                return None
            types.append(st)
            pos += n
    except IndexError:
        return None
    return types, pos


def _decode_body(buf: bytes, pos: int, types: list[int], encoding: str) -> list | None:
    values = []
    for st in types:
        size = serial_size(st)
        try:
            values.append(decode_value(st, buf[pos:pos + size], encoding, strict=True))
        except (UnicodeDecodeError, ValueError):
            return None
        pos += size
    return values


def _plausible(values: list, types: list[int]) -> bool:
    """Reject records that are almost certainly noise: all NULL/0/1, or text full of control characters."""
    if all(t in (0, 8, 9) for t in types):
        return False
    for v, t in zip(values, types):
        if t >= 13 and t % 2 and v:
            bad = sum(1 for ch in v if ord(ch) < 32 and ch not in "\t\r\n")
            if bad > len(v) // 20 or "\x00" in v:
                return False
    return True


def _confidence(sig: TableSig, types: list[int], exact: bool) -> str:
    informative = sum(1 for t in types if t not in (0, 8, 9))
    if sig.ncols <= 2 and not exact:
        return "low"
    if exact or informative >= 3:
        return "high"
    return "medium" if informative >= 2 else "low"


def _lookback_rowid(buf: bytes, pos: int, lower: int, payload_len: int) -> int | None:
    """If a full cell header (payload length, rowid) ends right at pos, recover the rowid."""
    for start in range(max(lower, pos - 18), pos - 1):
        try:
            plen, n1 = read_varint(buf, start)
            rowid, n2 = read_varint(buf, start + n1)
        except IndexError:
            continue
        if start + n1 + n2 == pos and plen == payload_len:
            return rowid
    return None


def match_header(buf: bytes, pos: int, end: int, sigs: list[TableSig], encoding: str):
    """Try to read a complete record (header + body) at pos for any signature. Returns
    (sig, types, values, record_end) or None."""
    try:
        hlen, n = read_varint(buf, pos)
    except IndexError:
        return None
    if hlen < 2 or pos + hlen > end:
        return None
    for sig in sigs:
        if hlen > 1 + 9 * sig.ncols or hlen - n < sig.ncols:
            continue
        got = _read_types(buf, pos + n, pos + hlen, sig.ncols)
        if not got or got[1] != pos + hlen:
            continue
        types = got[0]
        if not all(sig.allows(i, st) for i, st in enumerate(types)):
            continue
        body = sum(serial_size(t) for t in types)
        rec_end = pos + hlen + body
        if rec_end > end:
            continue
        values = _decode_body(buf, pos + hlen, types, encoding)
        if values is None or not _plausible(values, types):
            continue
        return sig, types, values, rec_end
    return None


def carve_region(buf: bytes, start: int, end: int, sigs: list[TableSig], encoding: str, *, source: str,
                 page: int, lookback_floor: int | None = None) -> list[Carved]:
    """Scan buf[start:end] byte by byte for complete records."""
    out, pos = [], start
    sigs = [s for s in sigs if 0 < s.ncols <= MAX_COLUMNS]
    while pos < end - 1:
        hit = match_header(buf, pos, end, sigs, encoding)
        if not hit:
            pos += 1
            continue
        sig, types, values, rec_end = hit
        rowid = None
        if not sig.without_rowid:
            rowid = _lookback_rowid(buf, pos, lookback_floor if lookback_floor is not None else start,
                                    rec_end - pos)
        if sig.ipk is not None and rowid is not None:
            values[sig.ipk] = rowid
        out.append(Carved(sig.name, values, types, rowid, source, page, pos,
                          _confidence(sig, types, rowid is not None), length=rec_end - pos))
        pos = rec_end
    return out


def _guess_types(sig: TableSig, col: int, size: int) -> list[int]:
    """Serial types for a column whose type byte was overwritten, given the body size it must fill."""
    if col == sig.ipk:
        return [0] if size == 0 else []
    aff = sig.affinities[col]
    ints = list(INT_SIZE_TYPES.get(size, ()))
    if size == 0:
        ints = [0, 8, 9]
    text, blob = 13 + 2 * size, 12 + 2 * size
    # a guessed BLOB accepts any bytes at all, so it's never offered for a TEXT column
    order = {"INTEGER": ints, "REAL": ints, "TEXT": [text] if size else [0, text],
             "NUMERIC": ints + [text, blob], "BLOB": ints + [text, blob]}[aff]
    return [t for t in order if sig.allows(col, t)]


def carve_freeblock(buf: bytes, off: int, size: int, sig: TableSig, encoding: str, *, source: str,
                    page: int) -> list[Carved]:
    """Recover a deleted cell from a freeblock whose first 4 bytes have been overwritten.

    The cell was [payload len][rowid][header len][type 1..N][body]. We look for the surviving serial types
    (for columns j+1..N, j = 0..2 lost) a few bytes into the block, then infer the lost types from the body
    size left over. A record that ends exactly at the end of the freeblock is strong evidence."""
    end = off + size
    best = None
    for c in range(off + 4, min(off + 16, end)):
        for j in (0, 1, 2):
            if j >= sig.ncols or (j == 2 and sig.ipk != 0):
                continue
            got = _read_types(buf, c, end, sig.ncols - j)
            if not got:
                continue
            known, hdr_end = got
            if not all(sig.allows(j + i, st) for i, st in enumerate(known)):
                continue
            known_body = sum(serial_size(t) for t in known)
            # A lost INTEGER PRIMARY KEY column is NULL (0 bytes), so it costs nothing to infer. At most one
            # other lost column can be inferred: it has to fill exactly what's left of the freeblock.
            unknown = [i for i in range(j) if i != sig.ipk]
            if len(unknown) > 1:
                continue
            derived = bool(unknown)
            if not derived:
                rec_end = hdr_end + known_body
                if rec_end > end:
                    continue
                candidates = [[0] * j]
            else:
                remaining = end - hdr_end - known_body
                if remaining < 0:
                    continue
                candidates = [[0 if i == sig.ipk else t for i in range(j)] for t in _guess_types(sig, unknown[0], remaining)]
                rec_end = end
            for lost in candidates:
                types = lost + known
                values = _decode_body(buf, hdr_end, types, encoding)
                if values is None or not _plausible(values, types):
                    continue
                if not any(buf[hdr_end:rec_end]):  # zeroed by secure_delete: nothing to recover
                    continue
                # Ending exactly at the freeblock's end only counts as evidence when the end wasn't assumed.
                exact = rec_end == end and not derived
                rank = (exact, not derived, -j, -c)
                if best is None or rank > best[0]:
                    best = (rank, types, values, unknown, rec_end, exact or derived)
            if best and best[0][0] and j == 0:
                break
    if not best:
        return []
    _, types, values, inferred, rec_end, exact = best
    if (not exact or inferred) and sig.ncols <= 2:
        return []
    rec = Carved(sig.name, values, types, None, source, page, off, _confidence(sig, types, exact),
                 inferred=inferred, note="" if exact else "record shorter than freeblock", length=rec_end - off)
    out = [rec]
    if end - rec_end >= 6:  # coalesced freeblocks: carry on in what's left
        out += carve_freeblock(buf, rec_end, end - rec_end, sig, encoding, source=source, page=page)
    return out


# ---------------------------------------------------------------- page-level recovery
def recover_page(raw: bytes, number: int, db: SqliteFile, sigs: list[TableSig], *, source: str,
                 owner: TableSig | None) -> list[Carved]:
    """Deleted records in one b-tree page: its freeblocks and its unallocated gap.

    Interior and index pages are searched too: when a leaf splits, the page that becomes the interior node
    keeps the old leaf's cells in what is now its unallocated space."""
    hdr: BTreePage | None = parse_btree_header(raw, number, db.usable)
    if hdr is None:
        return []
    # Old content in a table page is that table's; an index page could have been anything before.
    cands = [owner] if owner and hdr.is_table else sigs
    out: list[Carved] = []
    for off, size in freeblocks(raw, hdr, db.usable) if hdr.kind == "table_leaf" else []:
        for sig in cands:
            got = carve_freeblock(raw, off, size, sig, db.encoding, source=source, page=number)
            if got:
                out += got
                break
    if hdr.content_start > hdr.pointer_array_end:
        a, b = hdr.pointer_array_end, min(hdr.content_start, db.usable)
        src = source if source != "freeblock" else "unallocated"
        out += carve_region(raw, a, b, cands, db.encoding, source=src, page=number)
        # cells that were freed and then left behind when the page was defragmented keep their freeblock header
        out += scan_orphan_freeblocks(raw, a, b, cands, db.encoding, source=src, page=number, page_end=db.usable)
    return out


def live_cells(raw: bytes, number: int, db: SqliteFile) -> list:
    """The cells a table-leaf page image points at (used for WAL / journal / freelist page images)."""
    hdr = parse_btree_header(raw, number, db.usable)
    if hdr is None or hdr.kind != "table_leaf":
        return []
    cells = []
    for ptr in hdr.cell_pointers:
        if hdr.pointer_array_end <= ptr < db.usable:
            c = db.parse_cell(raw, hdr, ptr)
            if not c.error:
                cells.append(c)
    return cells


def sig_for_record(types: list[int], sigs: list[TableSig]) -> TableSig | None:
    for sig in sigs:
        if sig.ncols == len(types) and all(sig.allows(i, t) for i, t in enumerate(types)):
            return sig
    return None


def recover_database(db: SqliteFile, sigs: list[TableSig], page_map: list[dict]) -> list[Carved]:
    """Walk every table page and every free page of a database image and carve what's left behind."""
    by_name = {s.name: s for s in sigs}
    out: list[Carved] = []
    for info in page_map:
        num, kind = info["page"], info.get("type")
        if kind == "table_leaf" and info.get("owner") in by_name:
            out += recover_page(db.page(num), num, db, sigs, source="freeblock", owner=by_name[info["owner"]])
        elif kind in ("table_interior", "index_leaf", "index_interior"):
            out += recover_page(db.page(num), num, db, sigs, source="unallocated", owner=by_name.get(info.get("owner")))
        elif kind in ("freelist_leaf", "freelist_trunk"):
            raw = db.page(num)
            start = 0
            if kind == "freelist_trunk":
                count = min(int.from_bytes(raw[4:8], "big"), (db.usable - 8) // 4)
                start = 8 + 4 * count
            # a freelist leaf is often an untouched old table leaf: read its cells directly first
            covered = []
            if kind == "freelist_leaf":
                for c in live_cells(raw, num, db):
                    sig = sig_for_record(c.types, sigs)
                    if sig and _plausible(c.values, c.types):
                        vals = list(c.values)
                        if sig.ipk is not None:
                            vals[sig.ipk] = c.rowid
                        out.append(Carved(sig.name, vals, c.types, c.rowid, "freelist", num, c.offset, "high",
                                          note="intact cell on a freed page", length=c.size))
                        covered.append((c.offset, c.offset + c.size))
                out += recover_page(raw, num, db, sigs, source="freelist", owner=None)
            for a, b in _gaps(start, db.usable, covered):
                out += carve_region(raw, a, b, sigs, db.encoding, source="freelist", page=num, lookback_floor=a)
            if parse_btree_header(raw, num, db.usable) is None:
                out += scan_orphan_freeblocks(raw, start, db.usable, sigs, db.encoding, source="freelist", page=num)
    return dedupe(resolve_overlaps(out))


CONF_RANK = {"high": 2, "medium": 1, "low": 0}


def resolve_overlaps(records: list[Carved]) -> list[Carved]:
    """Two carves can't both own the same bytes of a page: keep the stronger one (confidence, then fewer
    guessed columns, then more columns)."""
    ranked = sorted(records, key=lambda r: (-CONF_RANK[r.confidence], len(r.inferred), -len(r.types)))
    taken: dict[tuple, list[tuple[int, int]]] = {}
    keep = set()
    for r in ranked:
        span = (r.offset, r.offset + max(r.length, 1))
        spans = taken.setdefault((r.source, r.page, r.frame), [])
        if any(a < span[1] and span[0] < b for a, b in spans):
            continue
        spans.append(span)
        keep.add(id(r))
    return [r for r in records if id(r) in keep]


def scan_orphan_freeblocks(raw: bytes, start: int, end: int, sigs: list[TableSig], encoding: str, *,
                           source: str, page: int, page_end: int | None = None) -> list[Carved]:
    """Freeblocks nothing points at any more: on a page whose b-tree header was overwritten (a leaf that
    became a freelist trunk), or in unallocated space after the page was defragmented. With no chain to
    follow, look for anything shaped like a freeblock header (a plausible next-offset and size) and try to
    carve a record behind it. A block whose recorded size runs past the region is cut at the region's end,
    and then only records whose length came from their own header are accepted."""
    page_end = page_end or end
    out, pos = [], start
    while pos + 8 < end:
        nxt, size = int.from_bytes(raw[pos:pos + 2], "big"), int.from_bytes(raw[pos + 2:pos + 4], "big")
        best = None
        if 8 <= size <= page_end - pos and (nxt == 0 or pos + size <= nxt < page_end):
            span = min(size, end - pos)
            for sig in sigs:
                if sig.ncols < 3:
                    continue
                got = carve_freeblock(raw, pos, span, sig, encoding, source=source, page=page)
                if got and not got[0].inferred:
                    score = (got[0].confidence == "high", sum(t not in (0, 8, 9) for t in got[0].types))
                    if best is None or score > best[0]:
                        best = (score, got)
        if best:
            for r in best[1]:
                r.confidence = "medium" if r.confidence == "high" else "low"
                r.note = (r.note + "; " if r.note else "") + "freeblock no longer linked into the page"
            out += [r for r in best[1] if not r.inferred]
            pos += 4
        else:
            pos += 1
    return out


def _gaps(start: int, end: int, covered: list[tuple[int, int]]):
    pos = start
    for a, b in sorted(covered):
        if a > pos:
            yield pos, a
        pos = max(pos, b)
    if pos < end:
        yield pos, end


def dedupe(records: list[Carved]) -> list[Carved]:
    """Keep one copy of each record, noting where else it was found. Records with a rowid are matched on
    (table, rowid, values). A record whose rowid was lost (a freeblock carve) matches a kept record of the same
    table that agrees with it on every value it has (its INTEGER PRIMARY KEY column is NULL, the other's isn't)."""
    by_key: dict[tuple, Carved] = {}
    kept: list[Carved] = []
    index: dict[tuple, list[tuple[tuple, Carved]]] = {}

    def anchor(r: Carved):
        first = next((v for v in r.values if isinstance(v, (str, bytes)) and v), None)
        return (r.table, len(r.values), first)

    for r in sorted(records, key=lambda x: x.rowid is None):  # stable: keeps the original order otherwise
        k = r.key()
        match = by_key.get(k)
        if match is None and r.rowid is None:
            match = next((rec for vals, rec in index.get(anchor(r), [])
                          if all(a is None or a == b for a, b in zip(k[2], vals))), None)
        if match is not None:
            if len(match.also) < 50 and r.where() not in match.also and r.where() != match.where():
                match.also.append(r.where())
            continue
        by_key[k] = r
        index.setdefault(anchor(r), []).append((k[2], r))
        kept.append(r)
    order = {id(r): i for i, r in enumerate(records)}
    return sorted(kept, key=lambda r: order[id(r)])
