"""A case: one evidence database (plus its -wal / -journal), opened safely, with everything the UI works on.

Evidence handling:
- The originals are only ever read. They're hashed (MD5, SHA-1, SHA-256), copied into a private temp folder,
  and the copies are re-hashed to prove they match. Everything after that works on the copies.
- The sqlite3 library never sees the evidence files or even the pristine copies. For each "view" of the
  database (main file only, main file + WAL, or the database as of a given WAL commit) Deep Table builds its
  own page image, marks it as a rollback-journal database so SQLite won't look for a WAL, and opens that
  image read-only with writes and ATTACH refused. So nothing can checkpoint, truncate or alter the evidence.
"""
from __future__ import annotations

import hashlib
import itertools
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import decoders, transforms
from .history import journal_records, wal_records
from .recovery import Carved, TableSig, dedupe, recover_database
from .sqlite_format import (
    FormatError,
    JournalFile,
    SqliteFile,
    WalFile,
    freeblocks,
    parse_btree_header,
    parse_journal,
    parse_wal,
    wal_overlay,
)

HASHES = ("md5", "sha1", "sha256")
QUERY_TIMEOUT_S = 60
MAX_RESULT_ROWS = 100000
BLOB_STORE_BYTES = 256 * 1024 * 1024


class CaseError(Exception):
    """Shown to the user as-is."""


def hash_file(path: Path) -> dict:
    hs = {name: hashlib.new(name) for name in HASHES}
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            for h in hs.values():
                h.update(chunk)
    return {name: h.hexdigest() for name, h in hs.items()}


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class Evidence:
    role: str  # database | wal | journal | shm
    original: str
    copy: Path
    size: int
    hashes: dict
    modified: str

    def public(self) -> dict:
        return {"role": self.role, "path": self.original, "name": os.path.basename(self.original),
                "size": self.size, "hashes": self.hashes, "modified": self.modified, "verified": True}


@dataclass
class View:
    key: str
    label: str
    path: Path
    image: SqliteFile
    conn: sqlite3.Connection
    lock: threading.Lock = field(default_factory=threading.Lock)


class BlobStore:
    """Blobs shown in the grid, kept server-side so the page only gets a short preview and an id."""

    def __init__(self, limit: int = BLOB_STORE_BYTES):
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._limit = limit
        self._lock = threading.Lock()

    def put(self, data: bytes) -> str:
        key = hashlib.sha1(data).hexdigest()[:20]
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return key
            self._items[key] = data
            self._size += len(data)
            while self._size > self._limit and len(self._items) > 1:
                _, old = self._items.popitem(last=False)
                self._size -= len(old)
        return key

    def get(self, key: str) -> bytes:
        with self._lock:
            if key not in self._items:
                raise CaseError("That BLOB is no longer cached; reload the rows to view it again")
            return self._items[key]


# PRAGMAs that change state. Reading ones (table_info, foreign_key_list, ...) stay allowed.
SETTING_PRAGMAS = {
    "journal_mode", "writable_schema", "query_only", "locking_mode", "schema_version", "user_version",
    "application_id", "secure_delete", "auto_vacuum", "page_size", "max_page_count", "journal_size_limit",
    "synchronous", "temp_store", "mmap_size", "foreign_keys", "recursive_triggers", "trusted_schema",
    "wal_autocheckpoint", "cache_spill", "cell_size_check", "legacy_alter_table", "ignore_check_constraints",
}
ACTION_PRAGMAS = {"wal_checkpoint", "incremental_vacuum", "optimize", "shrink_memory", "integrity_check_fix"}


def _deny_writes(action, arg1=None, arg2=None, *_args):
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        if name in ACTION_PRAGMAS or (name in SETTING_PRAGMAS and arg2 is not None):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    # Belt and braces with mode=ro and query_only: no ATTACH (it could open other files), no writes.
    denied = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE,
              sqlite3.SQLITE_DELETE, sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
              sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TRIGGER,
              sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_INDEX,
              sqlite3.SQLITE_DROP_TRIGGER}
    return sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK


class Case:
    def __init__(self, path: str, case_info: dict | None = None):
        src = Path(path).expanduser()
        if not src.is_file():
            raise CaseError(f"File not found: {path}")
        self.opened_at = _now()
        self.case_info = dict(case_info or {})
        self.tmp = Path(tempfile.mkdtemp(prefix="deep-table-"))
        self.blobs = BlobStore()
        self.tags: list[dict] = []
        self.saved_queries: list[dict] = []
        self.query_log: list[dict] = []
        self.column_formats: dict[tuple[str, str], str] = {}  # (table or "query", column) -> fmt / "none"
        self._tag_ids = itertools.count(1)
        self._views: dict[str, View] = {}
        self._views_lock = threading.Lock()
        self._objects: dict[str, list[dict]] = {}
        self._recovered: list[dict] | None = None
        self._recover_lock = threading.Lock()
        self._cancel = threading.Event()
        try:
            self.evidence = self._acquire(src)
            self.data = self.evidence["database"].copy.read_bytes()
            self.main = SqliteFile(self.data)
        except FormatError as exc:
            self.close()
            raise CaseError(str(exc)) from exc
        except BaseException:
            self.close()
            raise
        self.wal: WalFile | None = None
        self.wal_data = b""
        self.wal_error = ""
        if "wal" in self.evidence:
            self.wal_data = self.evidence["wal"].copy.read_bytes()
            try:
                self.wal = parse_wal(self.wal_data, self.evidence["wal"].original) if self.wal_data else None
            except FormatError as exc:
                self.wal_error = str(exc)
        self.journal: JournalFile | None = None
        self.journal_data = b""
        self.journal_error = ""
        if "journal" in self.evidence:
            self.journal_data = self.evidence["journal"].copy.read_bytes()
            try:
                self.journal = parse_journal(self.journal_data, self.main.page_size, self.evidence["journal"].original) \
                    if self.journal_data else None
            except FormatError as exc:
                self.journal_error = str(exc)

    # ------------------------------------------------------------ evidence
    def _acquire(self, src: Path) -> dict[str, Evidence]:
        files = {"database": src}
        for role, suffix in (("wal", "-wal"), ("journal", "-journal"), ("shm", "-shm")):
            companion = src.with_name(src.name + suffix)
            if companion.is_file():
                files[role] = companion
        with open(src, "rb") as fh:
            if fh.read(16) != b"SQLite format 3\x00":
                raise CaseError(f"{src.name} is not a SQLite 3 database (no 'SQLite format 3' header)")
        out = {}
        pristine = self.tmp / "pristine"
        pristine.mkdir()
        for role, path in files.items():
            st = path.stat()
            before = hash_file(path)
            dest = pristine / path.name
            shutil.copyfile(path, dest)
            after = hash_file(dest)
            if after != before:
                raise CaseError(f"Working copy of {path.name} doesn't match the original (was it being written?)")
            os.chmod(dest, 0o444)
            out[role] = Evidence(role, str(path.resolve()), dest, st.st_size, before,
                                 datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
        return out

    def close(self) -> None:
        for v in list(getattr(self, "_views", {}).values()):
            try:
                v.conn.close()
            except sqlite3.Error:
                pass
        tmp = getattr(self, "tmp", None)
        if tmp:
            def make_writable(func, path, _exc):  # the copies are read-only; Windows won't delete them as-is
                os.chmod(path, stat.S_IWRITE)
                func(path)

            try:
                shutil.rmtree(tmp, onerror=make_writable)
            except OSError:
                pass  # a temp folder left behind holds only copies; never fail closing a case over it

    # ------------------------------------------------------------ views
    def view_options(self) -> list[dict]:
        out = []
        if self.wal and self.wal.valid_frames:
            commits = self.wal.commits()
            out.append({"key": "current", "label": f"Current (database + WAL, {len(commits)} commits)"})
            out.append({"key": "main", "label": "Main file only (WAL ignored)"})
            for c in commits:
                out.append({"key": f"commit:{c['commit']}",
                            "label": f"As of WAL commit {c['commit']} (frames {c['first_frame']}–{c['last_frame']})"})
        else:
            out.append({"key": "current", "label": "Database"})
        if self.journal and self.journal.pages:
            out.append({"key": "journal", "label": "Before the journalled transaction (rolled back)"})
        out.append({"key": "recovered", "label": "Recovered records (rebuilt as a database)"})
        return out

    def _image_for(self, key: str) -> tuple[bytes, SqliteFile, str]:
        overlay, size = {}, None
        label = "Database"
        if key == "current":
            if self.wal:
                overlay, size = wal_overlay(self.wal, self.wal_data)
                label = "Current (database + WAL)"
        elif key == "main":
            label = "Main file only"
        elif key.startswith("commit:"):
            if not self.wal:
                raise CaseError("No WAL file")
            n = int(key.split(":", 1)[1])
            overlay, size = wal_overlay(self.wal, self.wal_data, n)
            label = f"As of WAL commit {n}"
        elif key == "journal":
            if not self.journal:
                raise CaseError("No rollback journal")
            for p in self.journal.pages:  # the first copy of each page in the journal is the original
                overlay.setdefault(p.page_number, self.journal.page_data(self.journal_data, p))
            size = self.journal.initial_pages or None
            label = "Before the journalled transaction"
        else:
            raise CaseError(f"Unknown view: {key}")
        ps = self.main.page_size
        pages = size or max(len(self.data) // ps, max(overlay, default=0))
        buf = bytearray(self.data[:pages * ps].ljust(pages * ps, b"\x00"))
        for pg, raw in overlay.items():
            if pg <= pages:
                buf[(pg - 1) * ps:pg * ps] = raw
        return bytes(buf), SqliteFile(bytes(buf), page_count=pages), label

    def view(self, key: str = "current") -> View:
        rebuilt = None
        if key == "recovered" and key not in self._views:
            rebuilt = self._build_recovered_db(self.recover())  # outside the lock: recovery opens views itself
        with self._views_lock:
            if key in self._views:
                return self._views[key]
            if key == "recovered":
                raw, image, label = rebuilt, SqliteFile(rebuilt), "Recovered records"
            else:
                raw, image, label = self._image_for(key)
            work = bytearray(raw)
            work[18:20] = b"\x01\x01"  # rollback-journal mode: SQLite won't look for or create a WAL
            path = self.tmp / f"view-{key.replace(':', '-')}.sqlite"
            path.write_bytes(work)
            os.chmod(path, 0o444)
            conn = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True, check_same_thread=False)
            conn.execute("PRAGMA query_only = ON")
            conn.set_authorizer(_deny_writes)
            conn.text_factory = self._text_factory
            v = View(key, label, path, image, conn)
            self._views[key] = v
            return v

    @staticmethod
    def _text_factory(raw: bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw  # invalid UTF-8 shows as a blob rather than failing the whole query

    def _run(self, view: View, sql: str, params=(), limit: int | None = None):
        start = time.monotonic()
        self._cancel.clear()

        def progress():
            return 1 if self._cancel.is_set() or time.monotonic() - start > QUERY_TIMEOUT_S else 0

        with view.lock:
            view.conn.set_progress_handler(progress, 20000)
            try:
                cur = view.conn.execute(sql, params)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(limit) if limit else cur.fetchall()
                more = bool(limit) and cur.fetchone() is not None
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc):
                    raise CaseError("Query stopped (cancelled or took longer than "
                                    f"{QUERY_TIMEOUT_S} s)") from exc
                raise CaseError(f"SQL error: {exc}") from exc
            except sqlite3.DatabaseError as exc:
                raise CaseError(f"SQL error: {exc}") from exc
            finally:
                view.conn.set_progress_handler(None, 0)
        return cols, rows, more, time.monotonic() - start

    def cancel(self) -> None:
        self._cancel.set()

    # ------------------------------------------------------------ overview
    def summary(self) -> dict:
        v = self.view("current")
        hdr = self.main.header.as_dict()
        objects = self.objects("current")
        trunks, leaves = self.main.freelist()
        out = {
            "file": self.evidence["database"].public(),
            "evidence": [e.public() for e in self.evidence.values()],
            "header": hdr,
            "opened_at": self.opened_at,
            "page_count": v.image.page_count,
            "main_page_count": self.main.page_count,
            "freelist_pages": len(trunks) + len(leaves),
            "tables": sum(1 for o in objects if o["type"] == "table"),
            "views": sum(1 for o in objects if o["type"] == "view"),
            "indexes": sum(1 for o in objects if o["type"] == "index"),
            "total_rows": sum(o.get("rows") or 0 for o in objects if o["type"] == "table"),
            "view_options": self.view_options(),
            "wal": self.wal_summary(),
            "journal": self.journal_summary(),
            "free_space": self.free_space_stats(),
        }
        return out

    def wal_summary(self) -> dict | None:
        if "wal" not in self.evidence:
            return None
        if self.wal is None:
            return {"present": True, "error": self.wal_error or "WAL is empty", "frames": 0}
        valid = self.wal.valid_frames
        return {
            "present": True, "error": "", "frames": len(self.wal.frames), "valid_frames": len(valid),
            "commits": len(self.wal.commits()), "page_size": self.wal.page_size,
            "checkpoint_seq": self.wal.checkpoint_seq, "salt1": self.wal.salt1, "salt2": self.wal.salt2,
            "old_frames": sum(1 for f in self.wal.frames if not f.salt_ok),
            "invalid_frames": sum(1 for f in self.wal.frames if f.salt_ok and not f.checksum_ok),
            "uncommitted_frames": max(0, sum(1 for f in self.wal.frames if f.valid) - len(valid)),
            "size": self.wal.size,
        }

    def journal_summary(self) -> dict | None:
        if "journal" not in self.evidence:
            return None
        if self.journal is None:
            return {"present": True, "error": self.journal_error or "journal is empty", "pages": 0}
        return {"present": True, "error": "", "pages": len(self.journal.pages),
                "zeroed_header": self.journal.zeroed_header, "initial_pages": self.journal.initial_pages,
                "page_numbers": sorted({p.page_number for p in self.journal.pages})}

    def free_space_stats(self) -> dict:
        """How much free space there is to recover from, and whether it looks wiped (secure_delete)."""
        free = zero = 0
        trunks, leaves = self.main.freelist()
        for pg in trunks + leaves:
            raw = self.main.page(pg)[8 if pg in trunks else 0:self.main.usable]
            free += len(raw)
            zero += raw.count(0)
        for pg in range(1, self.main.page_count + 1):
            raw = self.main.page(pg)
            hdr = parse_btree_header(raw, pg, self.main.usable)
            if hdr is None or hdr.kind != "table_leaf":
                continue
            for off, size in freeblocks(raw, hdr, self.main.usable):
                chunk = raw[off + 4:off + size]
                free += len(chunk)
                zero += chunk.count(0)
        ratio = zero / free if free else 0
        return {"bytes": free, "zero_ratio": round(ratio, 3), "wiped": free > 0 and ratio > 0.97}

    def objects(self, view_key: str = "current") -> list[dict]:
        if view_key in self._objects:
            return self._objects[view_key]
        v = self.view(view_key)
        cols, rows, _, _ = self._run(v, "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master "
                                        "ORDER BY type = 'table' DESC, name")
        out = []
        for typ, name, tbl, root, sql in rows:
            item = {"type": typ, "name": name, "tbl_name": tbl, "rootpage": root or 0, "sql": sql or ""}
            if typ in ("table", "view"):
                try:
                    item["columns"] = self.columns(view_key, name)
                except CaseError:
                    item["columns"] = []
                if typ == "table" and not name.startswith("sqlite_"):
                    item["foreign_keys"] = self.foreign_keys(view_key, name)
                try:
                    item["rows"] = self._run(v, f"SELECT count(*) FROM {quote_ident(name)}")[1][0][0]
                except CaseError:
                    item["rows"] = None
                if typ == "table" and isinstance(sql, str) and "VIRTUAL TABLE" in sql.upper():
                    item["virtual"] = True
            out.append(item)
        self._objects[view_key] = out
        return out

    def columns(self, view_key: str, table: str) -> list[dict]:
        _, rows, _, _ = self._run(self.view(view_key), f"PRAGMA table_info({quote_ident(table)})")
        return [{"cid": r[0], "name": r[1], "type": r[2] or "", "notnull": bool(r[3]), "default": r[4],
                 "pk": r[5]} for r in rows]

    def foreign_keys(self, view_key: str, table: str) -> list[dict]:
        _, rows, _, _ = self._run(self.view(view_key), f"PRAGMA foreign_key_list({quote_ident(table)})")
        return [{"table": r[2], "from": r[3], "to": r[4]} for r in rows]

    def signatures(self, view_key: str = "current") -> list[TableSig]:
        sigs = []
        for o in self.objects(view_key):
            if o["type"] == "table" and o["rootpage"] and o.get("columns") and not o.get("virtual"):
                sigs.append(TableSig.from_columns(o["name"], o["columns"], o["sql"], o["rootpage"]))
        return sigs

    # ------------------------------------------------------------ rows & formats
    def _has_rowid(self, view_key: str, table: str) -> bool:
        try:
            self._run(self.view(view_key), f"SELECT rowid FROM {quote_ident(table)} LIMIT 0")
            return True
        except CaseError:
            return False

    def detect_formats(self, scope: str, cols: list[str], rows: list) -> dict[str, str]:
        """Timestamp format per column: the examiner's choice if they made one, else auto-detected."""
        out = {}
        for i, name in enumerate(cols):
            chosen = self.column_formats.get((scope, name))
            if chosen:
                if chosen != "none":
                    out[name] = chosen
                continue
            sample = [r[i] for r in rows[:300] if r[i] is not None]
            fmt = decoders.detect_timestamp_format(sample, name)
            if fmt:
                out[name] = fmt
        return out

    def render_rows(self, cols: list[str], rows: list, formats: dict[str, str]) -> list[list[dict]]:
        idx_fmt = {i: formats[c] for i, c in enumerate(cols) if c in formats}
        out = []
        for r in rows:
            cells = []
            for i, val in enumerate(r):
                c = decoders.cell(val, self.blobs.put)
                if i in idx_fmt and c["t"] in ("int", "real"):
                    c["ts"] = decoders.format_dt(decoders.convert_timestamp(val, idx_fmt[i]))
                cells.append(c)
            out.append(cells)
        return out

    def rows(self, view_key: str, table: str, offset: int = 0, limit: int = 200, order: str = "",
             desc: bool = False, search: str = "") -> dict:
        v = self.view(view_key)
        cols_info = self.columns(view_key, table)
        names = [c["name"] for c in cols_info]
        if not names:
            raise CaseError(f"No such table: {table}")
        rowid = self._has_rowid(view_key, table)
        # An INTEGER PRIMARY KEY column already is the rowid, so don't show it twice.
        pks = [c for c in cols_info if c["pk"]]
        ipk = pks[0]["name"] if rowid and len(pks) == 1 and pks[0]["type"].strip().upper() == "INTEGER" else None
        select = ("rowid AS \"[rowid]\", " if rowid and not ipk else "") + "*"
        rowid_index = (names.index(ipk) if ipk else 0) if rowid else None
        where, params = "", []
        if search:
            where = " WHERE " + " OR ".join(f"CAST({quote_ident(n)} AS TEXT) LIKE ?" for n in names)
            params = [f"%{search}%"] * len(names)
        order_sql = ""
        if order and (order in names or order == "[rowid]"):
            order_sql = f" ORDER BY {'rowid' if order == '[rowid]' else quote_ident(order)} {'DESC' if desc else 'ASC'}"
        elif rowid:
            order_sql = " ORDER BY rowid"
        total = self._run(v, f"SELECT count(*) FROM {quote_ident(table)}{where}", params)[1][0][0]
        cols, data, _, _ = self._run(
            v, f"SELECT {select} FROM {quote_ident(table)}{where}{order_sql} LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)])
        # detect timestamp formats from the start of the table, so every page of rows reads the same way
        sample = self._run(v, f"SELECT {select} FROM {quote_ident(table)} LIMIT 300")[1]
        formats = self.detect_formats(table, cols, sample)
        return {"columns": cols, "rows": self.render_rows(cols, data, formats), "total": total,
                "offset": offset, "formats": formats, "has_rowid": rowid, "rowid_index": rowid_index}

    def set_format(self, scope: str, column: str, fmt: str) -> None:
        if fmt not in decoders.TIMESTAMP_FORMATS and fmt not in ("none", "auto"):
            raise CaseError(f"Unknown timestamp format: {fmt}")
        if fmt == "auto":
            self.column_formats.pop((scope, column), None)
        else:
            self.column_formats[(scope, column)] = fmt

    # ------------------------------------------------------------ SQL
    def query(self, view_key: str, sql: str, limit: int = 5000) -> dict:
        sql = (sql or "").strip()
        if not sql:
            raise CaseError("Enter a query")
        limit = max(1, min(int(limit), MAX_RESULT_ROWS))
        cols, rows, more, elapsed = self._run(self.view(view_key), sql, limit=limit)
        self.query_log.append({"sql": sql, "view": view_key, "rows": len(rows), "more": more, "at": _now(),
                               "seconds": round(elapsed, 3)})
        formats = self.detect_formats("query", cols, rows)
        return {"columns": cols, "rows": self.render_rows(cols, rows, formats), "count": len(rows),
                "truncated": more, "seconds": round(elapsed, 3), "formats": formats}

    def query_raw(self, view_key: str, sql: str, limit: int = MAX_RESULT_ROWS):
        return self._run(self.view(view_key), sql, limit=limit)[:3]

    def table_raw(self, view_key: str, table: str):
        rowid = self._has_rowid(view_key, table)
        select = ("rowid AS \"[rowid]\", " if rowid else "") + "*"
        return self._run(self.view(view_key), f"SELECT {select} FROM {quote_ident(table)}")[:2]

    def suggest_joins(self, view_key: str = "current") -> list[dict]:
        """Join candidates for the query builder: declared foreign keys, then naming conventions
        (x_id -> x.id, Core Data's ZFOO -> ZFOO.Z_PK, identical id-like column names)."""
        objects = [o for o in self.objects(view_key) if o["type"] in ("table", "view") and o.get("columns")]
        by_lower = {o["name"].lower(): o for o in objects}
        out, seen = [], set()

        def add(a, ac, b, bc, why):
            key = tuple(sorted([(a, ac), (b, bc)]))
            if key in seen or (a == b and ac == bc):
                return
            seen.add(key)
            out.append({"left": a, "left_col": ac, "right": b, "right_col": bc, "reason": why})

        for o in objects:
            for fk in o.get("foreign_keys", []):
                target = by_lower.get(fk["table"].lower())
                to = fk["to"] or next((c["name"] for c in (target or {}).get("columns", []) if c["pk"]), "rowid")
                add(o["name"], fk["from"], fk["table"], to, "foreign key")
        for o in objects:
            for c in o["columns"]:
                low = c["name"].lower()
                for suffix in ("_id", "id"):
                    if low.endswith(suffix) and len(low) > len(suffix):
                        stem = low[: -len(suffix)].rstrip("_")
                        for cand in (stem, stem + "s", stem + "es"):
                            t = by_lower.get(cand)
                            if t and t is not o:
                                pk = next((x["name"] for x in t["columns"] if x["pk"]), "rowid")
                                add(o["name"], c["name"], t["name"], pk, "naming (x_id → x)")
                if low.startswith("z") and low not in ("z_pk", "z_ent", "z_opt"):
                    t = by_lower.get(low)  # Core Data: column ZCHAT references table ZCHAT's Z_PK
                    if t and t is not o and any(x["name"].upper() == "Z_PK" for x in t["columns"]):
                        add(o["name"], c["name"], t["name"], "Z_PK", "Core Data relationship")
        return out

    # ------------------------------------------------------------ recovery
    def _owners(self, image: SqliteFile, schema) -> dict[int, str]:
        return {p["page"]: p.get("owner", "") for p in image.page_map(schema) if p.get("owner")}

    def recover(self, force: bool = False) -> list[dict]:
        with self._recover_lock:  # the page may ask twice at once (on open, and from the Recovered tab)
            if self._recovered is None or force:
                self._recovered = self._recover()
                with self._views_lock:  # the rebuilt database is out of date now
                    old = self._views.pop("recovered", None)
                    self._objects.pop("recovered", None)
                if old:
                    old.conn.close()
            return self._recovered

    META_COLUMNS = (("status", "TEXT"), ("source", "TEXT"), ("location", "TEXT"), ("orig_rowid", "INTEGER"),
                    ("confidence", "TEXT"), ("inferred", "TEXT"), ("note", "TEXT"), ("also_found_in", "TEXT"))

    def _build_recovered_db(self, recs: list[dict]) -> bytes:
        """Recovered records as a database of their own: one table per source table, the original columns
        plus where each record came from, so they can be browsed, searched and queried like live data."""
        path = self.tmp / f"recovered-build-{time.monotonic_ns()}.sqlite"
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode = OFF")
            decl = {}
            for o in self.objects("current"):
                if o["type"] == "table":
                    decl[o["name"]] = {c["name"]: c["type"] for c in o.get("columns", [])}
            by_table: dict[str, list[dict]] = {}
            for r in recs:
                by_table.setdefault(r["table"], []).append(r)
            for table, rows in by_table.items():
                cols = rows[0]["columns"]
                meta = [(f"dt_{m}", t) for m, t in self.META_COLUMNS]
                taken = {c.lower() for c in cols}
                meta = [(n if n.lower() not in taken else n + "_", t) for n, t in meta]
                col_sql = ", ".join([f"{quote_ident(n)} {t}" for n, t in meta] +
                                    [f"{quote_ident(c)} {decl.get(table, {}).get(c, '')}".strip() for c in cols])
                conn.execute(f"CREATE TABLE {quote_ident(table)} ({col_sql})")
                ph = ", ".join("?" * (len(meta) + len(cols)))
                conn.executemany(f"INSERT INTO {quote_ident(table)} VALUES ({ph})", [
                    [r["status"], r["source"], self._location(r), r["rowid"], r["confidence"],
                     ", ".join(r["inferred"]) or None, r["note"] or None, ", ".join(r.get("also") or []) or None,
                     *r["values"][:len(cols)], *[None] * max(0, len(cols) - len(r["values"]))]
                    for r in rows])
            conn.commit()
        finally:
            conn.close()
        data = path.read_bytes()
        path.unlink()
        return data

    @staticmethod
    def _location(r: dict) -> str:
        if r["source"] == "wal":
            return f"WAL frame {r['frame']}"
        if r["source"] == "journal":
            return f"journal record {r['frame']}"
        return f"page {r['page']} offset {r['offset']}" if r["page"] else ""

    def _recover(self) -> list[dict]:
        sigs = self.signatures("current")
        main_schema = self.main.schema()
        main_map = self.main.page_map(main_schema)
        records: list[Carved] = recover_database(self.main, sigs, main_map)
        # The database as the WAL leaves it can have free space of its own (deletes not yet checkpointed).
        if self.wal and self.wal.valid_frames:
            cur = self.view("current").image
            cur_map = cur.page_map(cur.schema())
            changed = {f.page_number for f in self.wal.valid_frames}
            extra = recover_database(cur, sigs, [p for p in cur_map if p["page"] in changed])
            for r in extra:
                r.note = (r.note + "; " if r.note else "") + "in a page the WAL rewrote"
            records += extra
        if self.wal and self.wal.valid_frames:
            records += self._superseded_main_rows(sigs, main_schema)
        owners = self._owners(self.main, main_schema)
        if self.wal:
            cur = self.view("current").image
            owners = {**owners, **self._owners(cur, cur.schema())}
            records += wal_records(self.main, self.wal, self.wal_data, sigs, owners)
        if self.journal:
            records += journal_records(self.main, self.journal, self.journal_data, sigs, owners)
        return self._classify(dedupe(records))

    def _superseded_main_rows(self, sigs: list[TableSig], main_schema: list[dict]) -> list[Carved]:
        """Rows the main file still holds that the WAL has since changed or deleted: what the database said
        before the transactions in the WAL. (Unchanged rows are classified "live" and hidden by default.)"""
        roots = {s["name"]: s["rootpage"] for s in main_schema if s["type"] == "table" and s["rootpage"]}
        cur = self.view("current").image
        cur_roots = {s["name"]: s["rootpage"] for s in cur.schema() if s["type"] == "table" and s["rootpage"]}
        changed = {f.page_number for f in self.wal.valid_frames}
        out = []
        for sig in sigs:
            root = roots.get(sig.name)
            if not root:
                continue
            # only tables whose pages the WAL touched can differ
            pages = {h.number for h, _, _ in self.main.walk(root, visit_cells=False)}
            if cur_roots.get(sig.name):
                pages |= {h.number for h, _, _ in cur.walk(cur_roots[sig.name], visit_cells=False)}
            if not pages & changed:
                continue
            for cell in self.main.table_rows(root):
                if cell.error:
                    continue
                vals = list(cell.values) + [None] * (sig.ncols - len(cell.values))
                if sig.ipk is not None and sig.ipk < len(vals):
                    vals[sig.ipk] = cell.rowid
                out.append(Carved(sig.name, vals[:sig.ncols], cell.types, cell.rowid, "main file", 0, cell.offset,
                                  "high", note="row in the main file, before the WAL's transactions"))
        return out

    def _classify(self, records: list[Carved]) -> list[dict]:
        """Compare each recovered record with the live table: is it deleted, an older version, or a copy of a
        row that's still there?"""
        v = self.view("current")
        by_table: dict[str, list[Carved]] = {}
        for r in records:
            by_table.setdefault(r.table, []).append(r)
        out = []
        for table, recs in by_table.items():
            cols = [c["name"] for c in self.columns("current", table)]
            has_rowid = self._has_rowid("current", table)
            live_by_rowid: dict[int, tuple] = {}
            live_values: set | None = None
            ids = [r.rowid for r in recs if r.rowid is not None]
            if has_rowid and ids:
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    q = f"SELECT rowid, * FROM {quote_ident(table)} WHERE rowid IN ({','.join('?' * len(chunk))})"
                    for row in self._run(v, q, chunk)[1]:
                        live_by_rowid[row[0]] = tuple(row[1:])
            if any(r.rowid is None for r in recs):
                live_values = {self._norm(row) for row in
                               self._run(v, f"SELECT * FROM {quote_ident(table)}", limit=500000)[1]}
            for r in recs:
                vals = list(r.values)[:len(cols)] + [None] * max(0, len(cols) - len(r.values))
                if r.rowid is not None and has_rowid:
                    live = live_by_rowid.get(r.rowid)
                    status = "deleted" if live is None else ("live" if self._norm(live) == self._norm(vals)
                                                             else "older version")
                elif live_values is not None:
                    status = "live" if self._norm(vals) in live_values else "deleted"
                else:
                    status = "deleted"
                out.append({"table": table, "columns": cols, "values": vals, "rowid": r.rowid,
                            "source": r.source, "page": r.page, "offset": r.offset, "frame": r.frame,
                            "confidence": r.confidence, "inferred": [cols[i] for i in r.inferred if i < len(cols)],
                            "note": r.note, "also": r.also, "status": status})
        order = {"deleted": 0, "older version": 1, "live": 2}
        out.sort(key=lambda x: (order[x["status"]], x["table"], x["source"], x["rowid"] if x["rowid"] is not None else -1))
        for i, rec in enumerate(out):
            rec["id"] = i
        return out

    @staticmethod
    def _norm(values) -> tuple:
        return tuple(round(v, 9) if isinstance(v, float) else (bytes(v) if isinstance(v, (bytes, bytearray, memoryview)) else v)
                     for v in values)

    def recovered_view(self, table: str = "", status: str = "", source: str = "", search: str = "",
                       offset: int = 0, limit: int = 500) -> dict:
        recs = self.recover()
        counts: dict = {"status": {}, "source": {}, "table": {}}
        for r in recs:
            for k in ("status", "source"):
                counts[k][r[k]] = counts[k].get(r[k], 0) + 1
            # per-table counts follow the status / source filters, so the list matches what the grid shows
            if (not status or r["status"] == status) and (not source or r["source"] == source):
                counts["table"][r["table"]] = counts["table"].get(r["table"], 0) + 1
        sel = [r for r in recs if (not table or r["table"] == table) and (not status or r["status"] == status)
               and (not source or r["source"] == source)]
        if search:
            s = search.lower()
            sel = [r for r in sel if any(isinstance(v, str) and s in v.lower() for v in r["values"])]
        page = sel[offset:offset + limit]
        formats_by_table: dict[str, dict] = {}
        rows = []
        for r in page:
            fm = formats_by_table.get(r["table"])
            if fm is None:
                sample = [x["values"] for x in recs if x["table"] == r["table"]][:300]
                fm = formats_by_table[r["table"]] = self.detect_formats(r["table"], r["columns"], sample)
            rows.append({k: r[k] for k in r if k != "values"} | {
                "cells": self.render_rows(r["columns"], [r["values"]], fm)[0], "formats": fm})
        return {"records": rows, "total": len(sel), "counts": counts, "offset": offset}

    # ------------------------------------------------------------ WAL / pages
    def wal_frames(self) -> dict:
        if not self.wal:
            return {"frames": [], "commits": []}
        cur = self.view("current").image
        owners = self._owners(self.main, self.main.schema())
        owners.update(self._owners(cur, cur.schema()))
        valid = {f.index for f in self.wal.valid_frames}
        commit_no, n = {}, 0
        for f in self.wal.valid_frames:
            if f.is_commit:
                n += 1
                commit_no[f.index] = n
        frames = []
        for f in self.wal.frames:
            raw = self.wal.frame_page(self.wal_data, f)
            hdr = parse_btree_header(raw, f.page_number, self.main.usable)
            frames.append({
                "index": f.index, "offset": f.offset, "page": f.page_number, "commit_size": f.commit_size,
                "commit": commit_no.get(f.index), "salt_ok": f.salt_ok, "checksum_ok": f.checksum_ok,
                "state": "valid" if f.index in valid else ("old salt" if not f.salt_ok else
                                                          ("uncommitted" if f.checksum_ok else "bad checksum")),
                "type": hdr.kind if hdr else ("page 1" if f.page_number == 1 else "other"),
                "cells": hdr.cell_count if hdr else None, "owner": owners.get(f.page_number, ""),
            })
        return {"frames": frames, "commits": self.wal.commits()}

    def page_map(self, view_key: str = "current") -> list[dict]:
        image = self.view(view_key).image
        pm = image.page_map(image.schema())
        in_wal = {f.page_number for f in self.wal.valid_frames} if self.wal else set()
        in_journal = {p.page_number for p in self.journal.pages} if self.journal else set()
        for p in pm:
            p["in_wal"] = p["page"] in in_wal
            p["in_journal"] = p["page"] in in_journal
        return pm

    def page_detail(self, view_key: str, number: int, source: str = "db", frame: int | None = None) -> dict:
        """Hex and structure of one page: from the chosen view, a WAL frame or a journal record."""
        image = self.view(view_key).image if source == "db" else self.main
        if source == "wal":
            if not self.wal or frame is None or not 0 <= frame < len(self.wal.frames):
                raise CaseError("No such WAL frame")
            f = self.wal.frames[frame]
            raw, number = self.wal.frame_page(self.wal_data, f), f.page_number
        elif source == "journal":
            if not self.journal or frame is None or not 0 <= frame < len(self.journal.pages):
                raise CaseError("No such journal record")
            p = self.journal.pages[frame]
            raw, number = self.journal.page_data(self.journal_data, p), p.page_number
        else:
            if not 1 <= number <= image.page_count:
                raise CaseError(f"Page {number} is out of range (1–{image.page_count})")
            raw = image.page(number)
        hdr = parse_btree_header(raw, number, image.usable)
        out = {"page": number, "size": len(raw), "raw": raw.hex(), "base": (number - 1) * image.page_size,
               "source": source, "frame": frame, "regions": []}
        if hdr:
            regions = [{"kind": "header", "start": hdr.header_offset, "end": hdr.header_offset + hdr.header_size},
                       {"kind": "pointers", "start": hdr.header_offset + hdr.header_size, "end": hdr.pointer_array_end}]
            if hdr.content_start > hdr.pointer_array_end:
                regions.append({"kind": "unallocated", "start": hdr.pointer_array_end, "end": hdr.content_start})
            cells = []
            for ptr in hdr.cell_pointers:
                if hdr.pointer_array_end <= ptr < image.usable:
                    c = image.parse_cell(raw, hdr, ptr)
                    regions.append({"kind": "cell", "start": ptr, "end": ptr + max(c.size, 1)})
                    cells.append({"offset": ptr, "size": c.size, "rowid": c.rowid, "left_child": c.left_child,
                                  "payload": c.payload_size, "overflow": c.overflow_page, "error": c.error,
                                  "values": [decoders.cell(v, self.blobs.put) for v in c.values[:12]]})
            for off, size in freeblocks(raw, hdr, image.usable):
                regions.append({"kind": "freeblock", "start": off, "end": off + size})
            out["regions"] = regions
            out.update({"type": hdr.kind, "cell_count": hdr.cell_count, "first_freeblock": hdr.first_freeblock,
                        "content_start": hdr.content_start, "fragmented": hdr.fragmented,
                        "right_child": hdr.right_child, "cells": cells})
        else:
            out["type"] = "not a b-tree page"
        out["regions"] = sorted(out["regions"], key=lambda r: r["start"])
        return out

    # ------------------------------------------------------------ blobs
    def blob(self, blob_id: str, chain: list[str] | None = None) -> dict:
        """Decode a BLOB, optionally after a chain of transforms (Base64 → zlib → plist, ...). The result of
        the chain is stored too, so it can be saved or decoded further."""
        data = self.blobs.get(blob_id)
        chain = [str(c) for c in (chain or [])]
        if chain:
            try:
                data = transforms.apply_chain(data, chain)
            except transforms.TransformError as exc:
                raise CaseError(str(exc)) from exc
        out = decoders.decode_blob(data)
        out["id"] = self.blobs.put(data)
        out["chain"] = chain
        return out

    def put_text(self, text: str) -> str:
        """Store a text value so the viewer can decode it (Base64 or hex in a TEXT column, say)."""
        return self.blobs.put(str(text).encode("utf-8"))

    # ------------------------------------------------------------ search everywhere
    def search_all(self, view_key: str, term: str, per_table: int = 50, include_recovered: bool = True) -> dict:
        """Find a value in every column of every table: text matches case-insensitively, and the same bytes
        are looked for inside BLOBs."""
        term = (term or "").strip()
        if len(term) < 2:
            raise CaseError("Search for at least 2 characters")
        v = self.view(view_key)
        like = f"%{term.replace(chr(92), chr(92) * 2).replace('%', chr(92) + '%').replace('_', chr(92) + '_')}%"
        results = []
        for o in self.objects(view_key):
            if o["type"] not in ("table", "view") or not o.get("columns") or o.get("virtual"):
                continue
            names = [c["name"] for c in o["columns"]]
            conds, params = [], []
            for n in names:
                q = quote_ident(n)
                conds.append(f"(CAST({q} AS TEXT) LIKE ? ESCAPE '\\' OR (typeof({q}) = 'blob' AND instr({q}, CAST(? AS BLOB)) > 0))")
                params += [like, term]
            where = " OR ".join(conds)
            rowid = o["type"] == "table" and self._has_rowid(view_key, o["name"])
            pks = [c for c in o["columns"] if c["pk"]]
            ipk = pks[0]["name"] if rowid and len(pks) == 1 and pks[0]["type"].strip().upper() == "INTEGER" else None
            select = ("rowid AS \"[rowid]\", " if rowid and not ipk else "") + "*"
            rowid_index = (names.index(ipk) if ipk else 0) if rowid else None
            try:
                total = self._run(v, f"SELECT count(*) FROM {quote_ident(o['name'])} WHERE {where}", params)[1][0][0]
                if not total:
                    continue
                cols, rows, _, _ = self._run(v, f"SELECT {select} FROM {quote_ident(o['name'])} WHERE {where} LIMIT ?",
                                             [*params, per_table])
            except CaseError:
                continue
            low = term.lower()
            hits = [[c for c, val in zip(cols, r) if (isinstance(val, str) and low in val.lower()) or
                     (isinstance(val, bytes) and term.encode() in val) or
                     (isinstance(val, (int, float)) and low in str(val).lower())] for r in rows]
            formats = self.detect_formats(o["name"], cols, rows)
            results.append({"table": o["name"], "columns": cols, "rows": self.render_rows(cols, rows, formats),
                            "formats": formats, "total": total, "hits": hits,
                            "rowid_index": rowid_index})
        recovered = []
        if include_recovered and self._recovered is not None:
            low = term.lower()
            for r in self._recovered:
                if r["status"] == "live":
                    continue
                if any((isinstance(x, str) and low in x.lower()) or (isinstance(x, bytes) and term.encode() in x)
                       for x in r["values"]):
                    recovered.append({"table": r["table"], "status": r["status"], "source": r["source"],
                                      "rowid": r["rowid"], "id": r["id"], "location": self._location(r),
                                      "columns": r["columns"], "cells": self.render_rows(r["columns"], [r["values"]], {})[0]})
                    if len(recovered) >= 500:
                        break
        return {"term": term, "tables": results, "recovered": recovered,
                "total": sum(t["total"] for t in results)}

    # ------------------------------------------------------------ WAL timeline
    def timeline(self, max_events: int = 20000) -> dict:
        """What each WAL transaction did, row by row: the main file is the state before the first commit;
        each commit's pages are applied in turn and the rows of every table it touched are compared."""
        if not self.wal or not self.wal.valid_frames:
            return {"events": [], "commits": [], "truncated": False}
        sigs = {s.name: s for s in self.signatures("current")}
        formats, all_formats = {}, {}
        for name in sigs:
            try:
                cols, rows = self.table_raw("current", name)
                fm = self.detect_formats(name, cols, rows[:300])
                formats[name] = next(((c, f) for c, f in fm.items() if c != "[rowid]"), None)
                all_formats[name] = fm
            except CaseError:
                formats[name] = None

        def snapshot(image: SqliteFile, names):
            roots = {s["name"]: s["rootpage"] for s in image.schema() if s["type"] == "table" and s["rootpage"]}
            state = {}
            for name in names:
                root, sig = roots.get(name), sigs[name]
                if not root:
                    state[name] = ({}, set())
                    continue
                pages = {h.number for h, _, _ in image.walk(root, visit_cells=False)}
                rows = {}
                for c in image.table_rows(root):
                    if c.error:
                        continue
                    vals = list(c.values)[:sig.ncols] + [None] * max(0, sig.ncols - len(c.values))
                    if sig.ipk is not None:
                        vals[sig.ipk] = c.rowid
                    rows[c.rowid if not sig.without_rowid else tuple(map(repr, vals))] = vals
                state[name] = (rows, pages)
            return state

        prev = snapshot(self.main, list(sigs))
        overlay: dict[int, bytes] = {}
        events, commits, pending, n = [], [], [], 0
        truncated = False
        for f in self.wal.valid_frames:
            pending.append(f)
            if not f.is_commit:
                continue
            n += 1
            for fr in pending:
                overlay[fr.page_number] = self.wal.frame_page(self.wal_data, fr)
            changed = {fr.page_number for fr in pending}
            image = SqliteFile(self.data, dict(overlay), f.commit_size)
            touched = [name for name, (_, pages) in prev.items() if pages & changed]
            # tables created in this commit have no pages in prev yet
            known_roots = {s["name"] for s in image.schema() if s["type"] == "table"}
            touched += [name for name in sigs if name in known_roots and not prev.get(name, ({}, set()))[1]
                        and name not in touched]
            now = snapshot(image, touched)
            counts = {"insert": 0, "update": 0, "delete": 0}
            for name in touched:
                before, after = prev.get(name, ({}, set()))[0], now[name][0]
                cols = sigs[name].columns
                for key in sorted(set(before) | set(after), key=repr):
                    b, a = before.get(key), after.get(key)
                    if b == a:
                        continue
                    op = "insert" if b is None else "delete" if a is None else "update"
                    counts[op] += 1
                    if len(events) >= max_events:
                        truncated = True
                        continue
                    row = a if a is not None else b
                    ts = ""
                    fmt = formats.get(name)
                    if fmt and fmt[0] in cols:
                        ts = decoders.format_dt(decoders.convert_timestamp(row[cols.index(fmt[0])], fmt[1]))
                    events.append({
                        "commit": n, "first_frame": pending[0].index, "last_frame": f.index, "table": name, "op": op,
                        "rowid": key if not isinstance(key, tuple) else None, "columns": cols,
                        "changed": [c for c, x, y in zip(cols, b, a) if x != y] if op == "update" else [],
                        "before": self.render_rows(cols, [b], all_formats.get(name, {}))[0] if b is not None else None,
                        "after": self.render_rows(cols, [a], all_formats.get(name, {}))[0] if a is not None else None,
                        "ts": ts, "ts_column": fmt[0] if fmt and ts else "",
                    })
                prev[name] = now[name]
            commits.append({"commit": n, "first_frame": pending[0].index, "last_frame": f.index,
                            "pages": sorted(changed), "tables": touched, **counts})
            pending = []
        return {"events": events, "commits": commits, "truncated": truncated}

    # ------------------------------------------------------------ BLOB export
    def blob_columns(self, view_key: str, table: str) -> list[dict]:
        out = []
        for c in self.columns(view_key, table):
            q = quote_ident(c["name"])
            n = self._run(self.view(view_key), f"SELECT count(*) FROM {quote_ident(table)} WHERE typeof({q}) = 'blob'")[1][0][0]
            if n:
                out.append({"name": c["name"], "count": n})
        return out

    def export_blobs(self, view_key: str, table: str, column: str, folder: str) -> dict:
        """Write every BLOB in a column to its own file, named by table, rowid and column with an extension
        from its detected type, plus a manifest.csv with sizes, types and hashes."""
        import csv

        dest = Path(folder)
        if not dest.is_dir():
            raise CaseError(f"Folder not found: {folder}")
        evidence_dirs = {Path(e.original).parent.resolve() for e in self.evidence.values()}
        if dest.resolve() in evidence_dirs:
            raise CaseError("Choose a folder other than the one holding the evidence")
        if column not in [c["name"] for c in self.columns(view_key, table)]:
            raise CaseError(f"No column {column} in {table}")
        rowid = self._has_rowid(view_key, table)
        sel = ("rowid, " if rowid else "NULL, ") + quote_ident(column)
        _, rows, _, _ = self._run(self.view(view_key), f"SELECT {sel} FROM {quote_ident(table)} "
                                                       f"WHERE typeof({quote_ident(column)}) = 'blob'")
        safe = lambda s: re.sub(r"[^A-Za-z0-9._-]+", "_", str(s))[:60]  # noqa: E731
        manifest = []
        for i, (rid, data) in enumerate(rows):
            info = decoders.detect_blob(data)
            name = f"{safe(table)}-{rid if rid is not None else i + 1}-{safe(column)}.{decoders.extension_for(info['mime'])}"
            path = dest / name
            k = 1
            while path.exists():
                path = dest / f"{path.stem}({k}){path.suffix}"
                k += 1
            path.write_bytes(data)
            h = transforms.hashes(data)
            manifest.append([path.name, rid, len(data), info["kind"], h["md5"], h["sha256"]])
        mpath = dest / f"{safe(table)}-{safe(column)}-manifest.csv"
        with open(mpath, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["file", "rowid", "bytes", "type", "md5", "sha256"])
            w.writerows(manifest)
        return {"folder": str(dest), "count": len(manifest), "manifest": str(mpath)}

    def blob_bytes(self, blob_id: str) -> bytes:
        return self.blobs.get(blob_id)

    # ------------------------------------------------------------ tags
    def add_tag(self, item: dict) -> dict:
        tag = {
            "id": next(self._tag_ids),
            "label": str(item.get("label") or "Bookmark")[:60],
            "note": str(item.get("note") or "")[:2000],
            "source": str(item.get("source") or ""),
            "table": str(item.get("table") or ""),
            "rowid": item.get("rowid"),
            "view": str(item.get("view") or ""),
            "columns": [str(c) for c in item.get("columns") or []][:500],
            "values": [self._display(c) for c in item.get("cells") or []][:500],
            "detail": str(item.get("detail") or "")[:500],
            "created": _now(),
        }
        self.tags.append(tag)
        return tag

    @staticmethod
    def _display(c) -> str:
        if not isinstance(c, dict):
            return str(c)
        t = c.get("t")
        if t == "null":
            return "NULL"
        if t == "blob":
            return f"[BLOB {c.get('kind', '')}, {c.get('len', 0):,} bytes]"
        text = c.get("s") if c.get("s") is not None else c.get("v")
        text = "" if text is None else str(text)
        return f"{text} ({c['ts']})" if c.get("ts") else text

    def update_tag(self, tag_id: int, label: str | None = None, note: str | None = None) -> dict:
        for t in self.tags:
            if t["id"] == tag_id:
                if label is not None:
                    t["label"] = str(label)[:60] or t["label"]
                if note is not None:
                    t["note"] = str(note)[:2000]
                return t
        raise CaseError("Bookmark not found")

    def delete_tag(self, tag_id: int) -> None:
        self.tags = [t for t in self.tags if t["id"] != tag_id]

    def save_query(self, name: str, sql: str, view_key: str) -> dict:
        name = (name or "").strip()[:80] or f"Query {len(self.saved_queries) + 1}"
        self.saved_queries = [q for q in self.saved_queries if q["name"] != name]
        q = {"name": name, "sql": sql, "view": view_key, "saved": _now()}
        self.saved_queries.append(q)
        return q

    def delete_query(self, name: str) -> None:
        self.saved_queries = [q for q in self.saved_queries if q["name"] != name]
