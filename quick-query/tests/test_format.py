"""The raw parser must read exactly what SQLite itself reads."""
import sqlite3

import pytest

from backend.sqlite_format import (
    FormatError,
    SqliteFile,
    local_payload_size,
    parse_header,
    parse_wal,
    read_varint,
    wal_overlay,
)
from conftest import connect


def encode_varint(v: int) -> bytes:
    if v < 0:
        v += 1 << 64
    if v > 0x00FFFFFFFFFFFFFF:
        out = [v & 0xFF]
        v >>= 8
        for _ in range(8):
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        return bytes(reversed(out))
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    return bytes(reversed(out))


@pytest.mark.parametrize("value", [0, 1, 127, 128, 16383, 16384, 2 ** 32, 2 ** 56 - 1, 2 ** 56, 2 ** 63 - 1, -1, -2 ** 63])
def test_varint_round_trip(value):
    enc = encode_varint(value)
    assert read_varint(enc, 0) == (value, len(enc))


def test_not_sqlite_is_rejected():
    with pytest.raises(FormatError):
        parse_header(b"hello world" * 20)


def test_rows_match_sqlite(tmp_path):
    """Every serial type, plus payloads big enough to spill onto overflow pages."""
    path = tmp_path / "types.db"
    conn = connect(path)
    conn.execute("PRAGMA page_size = 1024")
    conn.execute("CREATE TABLE t(a, b, c)")
    values = [
        (None, 0, 1), (127, -128, 32767), (-32768, 8388607, -8388608), (2147483647, -2147483648, 140737488355327),
        (-140737488355328, 9223372036854775807, -9223372036854775808), (3.14159, -0.0, 1e300),
        ("", "héllo wörld ✓", b""), (b"\x00\x01\x02", "x" * 5000, b"\xff" * 20000), ("y" * 100000, None, 42),
    ]
    conn.executemany("INSERT INTO t VALUES (?, ?, ?)", values)
    conn.commit()
    expected = conn.execute("SELECT rowid, a, b, c FROM t ORDER BY rowid").fetchall()
    root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 't'").fetchone()[0]
    conn.close()

    db = SqliteFile.open(path)
    assert db.page_size == 1024
    got = [(c.rowid, *c.values) for c in db.table_rows(root)]
    assert not [c for c in db.table_rows(root) if c.error]
    assert got == [tuple(r) for r in expected]


def test_schema_and_page_map(messages_db):
    db = SqliteFile.open(messages_db)
    names = {s["name"] for s in db.schema()}
    assert {"contact", "message"} <= names
    pm = db.page_map()
    assert len(pm) == db.page_count
    kinds = {p["type"] for p in pm}
    assert "table_leaf" in kinds and kinds & {"freelist_trunk", "freelist_leaf"}
    trunks, leaves = db.freelist()
    assert len(trunks) + len(leaves) == db.header.freelist_count


def test_local_payload_rule():
    # values from the file-format spec for a 4096-byte usable size
    assert local_payload_size(100, 4096, True) == 100
    assert local_payload_size(4061, 4096, True) == 4061
    assert local_payload_size(4062, 4096, True) < 4062


def test_wal_parse_and_overlay(wal_db):
    wal_bytes = wal_db.with_name(wal_db.name + "-wal").read_bytes()
    wal = parse_wal(wal_bytes)
    commits = wal.commits()
    assert len(commits) == 3
    assert all(f.checksum_ok and f.salt_ok for f in wal.valid_frames)
    # applying the WAL ourselves gives the same rows SQLite returns with the WAL
    overlay, size = wal_overlay(wal, wal_bytes)
    db = SqliteFile(wal_db.read_bytes(), overlay, size)
    root = next(s["rootpage"] for s in db.schema() if s["name"] == "note")
    rows = {c.rowid: c.values[1] for c in db.table_rows(root)}
    assert rows[5] == "Edited title" and 10 not in rows and 11 not in rows and rows[41] == "Late note"
    conn = sqlite3.connect(f"file:{wal_db}?mode=ro", uri=True)
    assert conn.execute("SELECT count(*) FROM note").fetchone()[0] == len(rows)
    conn.close()
    # the main file alone is the state before the WAL
    main = SqliteFile(wal_db.read_bytes())
    rows_main = {c.rowid: c.values[1] for c in main.table_rows(root)}
    assert rows_main[5] == "Note 5" and 10 in rows_main
