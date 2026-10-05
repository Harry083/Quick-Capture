"""Deleted records come back from freeblocks, unallocated space, freelist pages, the WAL and the journal."""
import re
import sqlite3

from backend.session import Case
from conftest import connect, snapshot


def deleted(case, table):
    return [r for r in case.recover() if r["table"] == table and r["status"] == "deleted"]


def test_freeblocks_and_freelist(messages_db):
    case = Case(str(messages_db))
    try:
        recs = deleted(case, "message")
        bodies = {int(r["values"][2].split()[2]) for r in recs if isinstance(r["values"][2], str)}
        expected = {i for i in range(1, 301) if i % 25 == 0 or 101 <= i <= 220}
        # SQLite overwrites much of what it frees when it rebalances pages; every deleted row whose bytes
        # are still somewhere in the file must come back, and nothing else may be reported as deleted.
        raw = messages_db.read_bytes()
        present = {i for i in expected if re.search(rb"message body %d " % i, raw)}
        assert len(present) > 20
        assert bodies & present == present
        assert not bodies - expected
        sources = {r["source"] for r in recs}
        assert sources & {"freeblock", "unallocated"}
        names = {r["values"][1] for r in deleted(case, "contact")}
        assert names == {"Contact 3", "Contact 4"}
    finally:
        case.close()


def test_deleted_rows_match_original_values(messages_db):
    case = Case(str(messages_db))
    try:
        for r in deleted(case, "message"):
            if r["rowid"] is None or r["inferred"]:
                continue
            i = r["rowid"]
            assert r["values"] == [i, 1 + i % 29, f"message body {i} " + "lorem " * (i % 7), 1700000000 + i * 60, i % 3]
    finally:
        case.close()


def test_secure_delete_leaves_nothing(tmp_path):
    path = tmp_path / "wiped.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, secret TEXT, n INTEGER)")
    conn.executemany("INSERT INTO t(secret, n) VALUES (?, ?)", [(f"secret value {i}", i) for i in range(200)])
    conn.commit()
    conn.execute("DELETE FROM t WHERE n % 2 = 0")
    conn.commit()
    conn.close()
    case = Case(str(path))
    try:
        assert deleted(case, "t") == []
        assert case.free_space_stats()["wiped"]
    finally:
        case.close()


def test_wal_history(wal_db):
    case = Case(str(wal_db))
    try:
        recs = case.recover()
        gone = {r["rowid"] for r in recs if r["table"] == "note" and r["status"] == "deleted"}
        assert {10, 11} <= gone
        older = [r for r in recs if r["table"] == "note" and r["status"] == "older version"]
        assert any(r["rowid"] == 5 and r["values"][1] == "Note 5" for r in older)
        # each WAL commit is its own view
        keys = [v["key"] for v in case.view_options()]
        assert keys[:2] == ["current", "main"] and "commit:3" in keys
        assert case.rows("main", "note", search="Edited")["total"] == 0
        assert case.rows("current", "note", search="Edited")["total"] == 1
        assert case.rows("commit:1", "note")["total"] == 40
        assert case.rows("commit:2", "note")["total"] == 38
        assert case.rows("commit:3", "note")["total"] == 39
    finally:
        case.close()


def test_old_wal_frames_after_restart(tmp_path):
    """After a checkpoint the WAL restarts from the top with a new salt. Frames past the new end still hold
    the previous cycle's pages, and their rows are recovered."""
    work = tmp_path / "w.db"
    conn = connect(work, wal=True)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    for i in range(30):  # many small commits: a long WAL
        conn.execute("INSERT INTO t(v) VALUES (?)", (f"first cycle row {i} " + "z" * 300,))
        conn.commit()
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    conn.execute("DELETE FROM t WHERE id > 25")  # restarts the log at frame 0 with a new salt
    conn.commit()
    dest = snapshot(work, tmp_path / "evidence.db")
    conn.close()
    case = Case(str(dest))
    try:
        s = case.wal_summary()
        assert s["old_frames"] > 0
        gone = [r for r in case.recover() if r["status"] == "deleted"]
        assert {r["values"][1].split()[3] for r in gone} >= {"25", "26", "27", "28", "29"}
        # the same rows also sit in the older WAL frames, and that provenance is kept
        assert any(r["source"] == "wal" or any(a.startswith("wal frame") for a in r["also"]) for r in gone)
    finally:
        case.close()


def test_journal_history(tmp_path):
    """journal_mode=PERSIST keeps the journal after commit: its pages are the table before the transaction."""
    work = tmp_path / "j.db"
    conn = connect(work)
    conn.execute("PRAGMA journal_mode = PERSIST")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t(v) VALUES (?)", [(f"original {i}",) for i in range(50)])
    conn.commit()
    conn.execute("UPDATE t SET v = 'changed' WHERE id = 7")
    conn.execute("DELETE FROM t WHERE id = 8")
    conn.commit()
    dest = snapshot(work, tmp_path / "evidence.db")
    conn.close()
    case = Case(str(dest))
    try:
        assert case.journal is not None and case.journal.pages
        recs = case.recover()
        assert any(r["rowid"] == 7 and r["status"] == "older version" and r["values"][1] == "original 6" for r in recs)
        assert any(r["rowid"] == 8 and r["status"] == "deleted" for r in recs)
        assert case.rows("journal", "t", search="original 6")["total"] == 1
    finally:
        case.close()
