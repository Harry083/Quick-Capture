"""Turning a LevelDB directory into something the rest of Deep Table can work with.

The decoded records are written into a SQLite database Deep Table builds itself (never touching the
evidence), so the same tables, search, SQL, BLOB viewer, bookmarks and reports work on browser storage:

- one table per decoded store (local_storage, session_storage, indexeddb_records), holding the live records,
  with `seq` (the LevelDB sequence number) and `location` (file and offset) columns;
- lookup tables (local_storage_origins, indexeddb_databases, indexeddb_object_stores);
- leveldb_records: every raw record from every file, with its state, including tombstones.

Records that aren't live (older versions and deleted values) become the case's recovered records.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import chromium
from .leveldb import TYPE_DELETE, LevelDb, Record


def key_text(raw: bytes) -> str:
    """A LevelDB key as text: printable ASCII as is, everything else as \\xNN."""
    return "".join(chr(b) if 32 <= b < 127 and b != 92 else f"\\x{b:02x}" for b in raw)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class Decoded:
    def __init__(self, db: LevelDb):
        self.db = db
        self.kind = chromium.kind_of(db)
        self.out = chromium.DECODERS[self.kind](db)
        # every decoded record, by identity, so the timeline can find which table a raw record belongs to
        self.by_record: dict[int, tuple[str, dict]] = {}
        for table, t in self.out["tables"].items():
            for r, row in t["rows"]:
                self.by_record[id(r)] = (table, row)

    def table_columns(self, table: str) -> list[str]:
        if table == "leveldb_records":
            return RAW_COLUMNS
        return self.out["tables"][table]["columns"] + ["seq", "location"]

    def row_values(self, table: str, r: Record, row: dict | None) -> list:
        if table == "leveldb_records":
            return raw_values(r)
        cols = self.out["tables"][table]["columns"]
        return [row.get(c) for c in cols] + [r.seq, r.where]


RAW_COLUMNS = ["seq", "state", "op", "key_text", "value_text", "key", "value", "file", "offset", "crc_ok",
               "also_found_in"]


def raw_values(r: Record) -> list:
    text = None
    if r.value:
        try:
            text = r.value.decode("utf-8")
            if any(ord(c) < 32 and c not in "\t\r\n" for c in text):
                text = None
        except UnicodeDecodeError:
            pass
    return [r.seq, r.state, "delete" if r.kind == TYPE_DELETE else "put", key_text(r.key), text, r.key, r.value,
            r.file, r.offset, int(r.crc_ok), ", ".join(r.also) or None]


def build_database(dec: Decoded, tmp: Path) -> bytes:
    path = tmp / f"leveldb-build-{time.monotonic_ns()}.sqlite"
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        for table, t in dec.out["tables"].items():
            cols = dec.table_columns(table)
            types = {"value_raw": "BLOB", "seq": "INTEGER", "value_bytes": "INTEGER"}
            conn.execute(f"CREATE TABLE {_quote(table)} ({', '.join(f'{_quote(c)} {types.get(c, chr(32))}'.strip() for c in cols)})")
            conn.executemany(f"INSERT INTO {_quote(table)} VALUES ({', '.join('?' * len(cols))})",
                             [dec.row_values(table, r, row) for r, row in sorted(t["rows"], key=lambda x: x[0].seq)
                              if r.state == "live"])
        for table, t in dec.out["extra"].items():
            cols = t["columns"]
            conn.execute(f"CREATE TABLE {_quote(table)} ({', '.join(_quote(c) for c in cols)})")
            conn.executemany(f"INSERT INTO {_quote(table)} VALUES ({', '.join('?' * len(cols))})", t["rows"])
        conn.execute("CREATE TABLE leveldb_records (seq INTEGER, state TEXT, op TEXT, key_text TEXT, value_text TEXT, "
                     "key BLOB, value BLOB, file TEXT, offset INTEGER, crc_ok INTEGER, also_found_in TEXT)")
        conn.executemany("INSERT INTO leveldb_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [raw_values(r) for r in sorted(dec.db.records, key=lambda r: r.seq)])
        conn.commit()
    finally:
        conn.close()
    data = path.read_bytes()
    path.unlink()
    return data


def recovered(dec: Decoded) -> list[dict]:
    """Older versions and deleted values, in the shape the Recovered tab uses. Stores Deep Table can't decode
    fall back to raw key/value records."""
    out = []
    decoded_any = bool(dec.out["tables"])
    sources = dec.out["tables"].items() if decoded_any else [("leveldb_records", {"rows": [(r, None) for r in dec.db.records]})]
    for table, t in sources:
        cols = dec.table_columns(table)
        for r, row in t["rows"]:
            if r.state not in ("deleted", "older version"):
                continue
            out.append({"table": table, "columns": cols, "values": dec.row_values(table, r, row), "rowid": None,
                        "source": f"leveldb {r.file_kind}", "page": 0, "offset": r.offset, "frame": None,
                        "confidence": "high" if r.crc_ok else "medium", "inferred": [],
                        "note": f"sequence {r.seq} in {r.file}" + ("" if r.crc_ok else "; checksum mismatch"),
                        "also": list(r.also), "status": r.state, "seq": r.seq})
    order = {"deleted": 0, "older version": 1}
    out.sort(key=lambda x: (order[x["status"]], x["table"], x["seq"]))
    for i, rec in enumerate(out):
        rec["id"] = i
    return out


def timeline(dec: Decoded, render, max_events: int = 20000) -> dict:
    """Every surviving operation in sequence-number order: what was written (insert / update) or deleted, with
    the value before and after. Compactions drop superseded records, so this is what is left, not all history."""
    records = sorted(dec.db.records, key=lambda r: r.seq)
    last: dict[bytes, tuple[Record, str, dict | None]] = {}
    events, truncated = [], False
    counts = {"insert": 0, "update": 0, "delete": 0}
    for r in records:
        if r.kind == TYPE_DELETE:
            prev = last.pop(r.key, None)
            if not prev:
                continue  # a tombstone for a value compacted away: nothing to show
            table, row = prev[1], prev[2]
            op, before, after = "delete", prev[0], None
        else:
            hit = dec.by_record.get(id(r))
            if not hit:
                continue  # metadata (index entries, store bookkeeping)
            table, row = hit
            prev = last.get(r.key)
            op, before, after = ("update" if prev else "insert"), (prev[0] if prev else None), r
            last[r.key] = (r, table, row)
        counts[op] += 1
        if len(events) >= max_events:
            truncated = True
            continue
        cols = dec.table_columns(table)
        bvals = dec.row_values(table, before, dec.by_record.get(id(before), (None, None))[1]) if before else None
        avals = dec.row_values(table, after, row) if after else None
        events.append({
            "kind": "leveldb", "commit": r.seq, "seq": r.seq, "file": r.file, "first_frame": r.file, "last_frame": r.file,
            "table": table, "op": op, "rowid": None, "columns": cols,
            "changed": [c for c, x, y in zip(cols, bvals, avals) if x != y and c not in ("seq", "location")]
            if bvals and avals else [],
            "before": render(cols, [bvals])[0] if bvals else None,
            "after": render(cols, [avals])[0] if avals else None,
            "ts": "", "ts_column": "",
        })
    return {"events": events, "commits": [{"commit": "all", **counts}], "truncated": truncated, "kind": "leveldb"}


def summary(dec: Decoded) -> dict:
    db = dec.db
    states: dict[str, int] = {}
    for r in db.records:
        states[r.state] = states.get(r.state, 0) + 1
    m = db.manifest
    labels = {"localstorage": "Chromium Local Storage", "sessionstorage": "Chromium Session Storage",
              "indexeddb": "Chromium IndexedDB", "leveldb": "LevelDB (generic)"}
    return {
        "store": dec.kind, "store_label": labels[dec.kind], "comparator": db.comparator, "current": db.current,
        "last_sequence": m.last_sequence if m else None, "log_number": m.log_number if m else None,
        "next_file": m.next_file if m else None, "files": db.files, "errors": db.errors,
        "records": len(db.records), "states": states,
        "decoded": {t: sum(1 for r, _ in v["rows"] if r.state == "live") for t, v in dec.out["tables"].items()},
    }
