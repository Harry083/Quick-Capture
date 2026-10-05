"""Record history from the WAL and the rollback journal.

Every WAL frame is a full copy of a page as some transaction wrote it, and frames stay in the file until a
checkpoint restarts the log (and often after: a restart rewrites the log from the start, so older frames with
the previous salt survive past the new end). A rollback journal holds the original content of each page a
transaction changed. Reading the table cells in those page images gives earlier versions of rows, including
rows deleted since.
"""
from __future__ import annotations

from .recovery import Carved, TableSig, _plausible, carve_freeblock, carve_region, live_cells, sig_for_record
from .sqlite_format import JournalFile, SqliteFile, WalFile, freeblocks, parse_btree_header


def _page_records(raw: bytes, pgno: int, db: SqliteFile, sigs: list[TableSig], owner: TableSig | None, *,
                  source: str, frame: int, note: str) -> list[Carved]:
    out: list[Carved] = []
    hdr = parse_btree_header(raw, pgno, db.usable)
    if hdr is None or hdr.kind != "table_leaf":
        return out
    for c in live_cells(raw, pgno, db):
        sig = owner if owner and owner.ncols == len(c.types) else sig_for_record(c.types, sigs)
        if not sig or not _plausible(c.values, c.types) and len(c.types) > 2:
            continue
        vals = list(c.values) + [None] * (sig.ncols - len(c.values))  # rows written before ALTER TABLE ADD
        if sig.ipk is not None:
            vals[sig.ipk] = c.rowid
        out.append(Carved(sig.name, vals, c.types, c.rowid, source, pgno, c.offset, "high", frame=frame,
                          note=note, length=c.size))
    cands = [owner] if owner else sigs
    for off, size in freeblocks(raw, hdr, db.usable):
        for sig in cands:
            got = carve_freeblock(raw, off, size, sig, db.encoding, source=source, page=pgno)
            if got:
                for r in got:
                    r.frame, r.note = frame, (note + "; deleted cell in this page image").strip("; ")
                out += got
                break
    if hdr.content_start > hdr.pointer_array_end:
        got = carve_region(raw, hdr.pointer_array_end, min(hdr.content_start, db.usable), cands, db.encoding,
                           source=source, page=pgno)
        for r in got:
            r.frame, r.note = frame, (note + "; unallocated space in this page image").strip("; ")
        out += got
    return out


def wal_records(db: SqliteFile, wal: WalFile, wal_data: bytes, sigs: list[TableSig],
                owners: dict[int, str]) -> list[Carved]:
    """Rows found in every WAL frame, valid or not. owners maps page number -> table name."""
    by_name = {s.name: s for s in sigs}
    valid = {f.index for f in wal.valid_frames}
    commit_of: dict[int, int] = {}
    n = 0
    pending: list[int] = []
    for f in wal.valid_frames:
        pending.append(f.index)
        if f.is_commit:
            n += 1
            for i in pending:
                commit_of[i] = n
            pending = []
    out: list[Carved] = []
    for f in wal.frames:
        raw = wal.frame_page(wal_data, f)
        if len(raw) < wal.page_size:
            continue
        if f.index in valid:
            note = f"commit {commit_of.get(f.index, '?')}"
        elif not f.salt_ok:
            note = "frame from an earlier checkpoint cycle (old salt)"
        else:
            note = "uncommitted or invalid frame"
        owner = by_name.get(owners.get(f.page_number, ""))
        out += _page_records(raw, f.page_number, db, sigs, owner, source="wal", frame=f.index, note=note)
    return out


def journal_records(db: SqliteFile, journal: JournalFile, journal_data: bytes, sigs: list[TableSig],
                    owners: dict[int, str]) -> list[Carved]:
    by_name = {s.name: s for s in sigs}
    out: list[Carved] = []
    for p in journal.pages:
        raw = journal.page_data(journal_data, p)
        if len(raw) < journal.page_size:
            continue
        owner = by_name.get(owners.get(p.page_number, ""))
        out += _page_records(raw, p.page_number, db, sigs, owner, source="journal", frame=p.index,
                             note="page as it was before the journalled transaction")
    return out
