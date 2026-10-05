"""Builders for test databases. Python's bundled SQLite is often compiled with SECURE_DELETE, so every builder
turns it off explicitly, the way most app databases (iOS, Android, browsers) run."""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def connect(path: Path, wal: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA secure_delete = OFF")
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 0")
    return conn


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(conn_path: Path, dest: Path, suffixes=("", "-wal", "-journal")) -> Path:
    """Copy a database (and companions) while its connection is still open, as an examiner receives it."""
    for s in suffixes:
        src = conn_path.with_name(conn_path.name + s)
        if src.exists():
            shutil.copyfile(src, dest.with_name(dest.name + s))
    return dest


@pytest.fixture
def messages_db(tmp_path) -> Path:
    """Rollback-journal database: 300 messages, some deleted (freeblocks), a run deleted (freelist pages)."""
    path = tmp_path / "messages.db"
    conn = connect(path)
    conn.execute("CREATE TABLE contact(id INTEGER PRIMARY KEY, name TEXT, phone TEXT)")
    conn.execute("CREATE TABLE message(id INTEGER PRIMARY KEY, contact_id INTEGER REFERENCES contact(id), "
                 "body TEXT, sent INTEGER, flags INTEGER)")
    conn.executemany("INSERT INTO contact(name, phone) VALUES (?, ?)",
                     [(f"Contact {i}", f"+44 7700 9{i:05d}") for i in range(1, 30)])
    for i in range(1, 301):
        conn.execute("INSERT INTO message(contact_id, body, sent, flags) VALUES (?, ?, ?, ?)",
                     (1 + i % 29, f"message body {i} " + "lorem " * (i % 7), 1700000000 + i * 60, i % 3))
    conn.commit()
    conn.execute("DELETE FROM message WHERE id % 25 = 0")  # scattered: freeblocks
    conn.execute("DELETE FROM message WHERE id BETWEEN 101 AND 220")  # a run: whole pages freed
    conn.execute("DELETE FROM contact WHERE id IN (3, 4)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def wal_db(tmp_path) -> Path:
    """WAL database whose -wal holds three commits after a checkpoint: an edit, a delete and an insert."""
    work = tmp_path / "work.db"
    conn = connect(work, wal=True)
    conn.execute("CREATE TABLE note(id INTEGER PRIMARY KEY, title TEXT, body TEXT, modified REAL)")
    for i in range(1, 41):
        conn.execute("INSERT INTO note(title, body, modified) VALUES (?, ?, ?)",
                     (f"Note {i}", f"body of note {i} " * 3, 720000000.5 + i * 3600))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("UPDATE note SET title = 'Edited title' WHERE id = 5")
    conn.commit()
    conn.execute("DELETE FROM note WHERE id IN (10, 11)")
    conn.commit()
    conn.execute("INSERT INTO note(title, body, modified) VALUES ('Late note', 'added after the delete', 720999999.0)")
    conn.commit()
    dest = snapshot(work, tmp_path / "notes.db")
    conn.close()
    return dest
