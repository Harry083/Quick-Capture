"""Evidence safety and the session features the UI uses."""
import os

import pytest

from backend import report
from backend.api import Api
from backend.session import Case, CaseError
from conftest import sha256


def test_evidence_is_never_modified(wal_db):
    files = [wal_db, wal_db.with_name(wal_db.name + "-wal")]
    before = {p: (sha256(p), p.stat().st_mtime_ns) for p in files}
    case = Case(str(wal_db))
    try:
        case.summary()
        case.recover()
        for key in [v["key"] for v in case.view_options()]:
            case.objects(key)
        case.query("current", "SELECT * FROM note")
        report.generate_html(case)
        assert {e.role for e in case.evidence.values()} == {"database", "wal"}
        assert case.evidence["database"].hashes["sha256"] == before[wal_db][0]
    finally:
        case.close()
    after = {p: (sha256(p), p.stat().st_mtime_ns) for p in files}
    assert before == after
    assert not wal_db.with_name(wal_db.name + "-shm").exists()


@pytest.mark.parametrize("sql", [
    "DELETE FROM note", "UPDATE note SET title = 'x'", "INSERT INTO note(title) VALUES ('x')", "DROP TABLE note",
    "CREATE TABLE x(a)", "ATTACH DATABASE ':memory:' AS other", "PRAGMA journal_mode = DELETE",
])
def test_queries_cannot_write(wal_db, sql):
    case = Case(str(wal_db))
    try:
        with pytest.raises(CaseError):
            case.query("current", sql)
        assert case.rows("current", "note")["total"] == 39
    finally:
        case.close()


def test_temp_files_removed(messages_db):
    case = Case(str(messages_db))
    tmp = case.tmp
    case.rows("current", "message")
    assert tmp.exists()
    case.close()
    assert not tmp.exists()


def test_not_a_database(tmp_path):
    p = tmp_path / "x.db"
    p.write_bytes(b"not sqlite at all" * 10)
    with pytest.raises(CaseError):
        Case(str(p))


def test_rows_paging_search_and_timestamps(messages_db):
    case = Case(str(messages_db))
    try:
        r = case.rows("current", "message", offset=0, limit=10)
        assert r["total"] == 172 and len(r["rows"]) == 10
        assert r["formats"].get("sent") == "unix_s"
        sent = r["columns"].index("sent")
        assert r["rows"][0][sent]["ts"].endswith("UTC")
        assert r["rowid_index"] == r["columns"].index("id")  # INTEGER PRIMARY KEY: no extra [rowid] column
        assert case.rows("current", "message", search="body 42 ")["total"] == 1
        desc = case.rows("current", "message", order="id", desc=True, limit=1)
        assert desc["rows"][0][0]["v"] == 299
        case.set_format("message", "sent", "none")
        assert "sent" not in case.rows("current", "message")["formats"]
    finally:
        case.close()


def test_joins_tags_and_report(messages_db):
    case = Case(str(messages_db), {"case_number": "C-7", "examiner": "Examiner"})
    try:
        joins = case.suggest_joins()
        assert {"left": "message", "left_col": "contact_id", "right": "contact", "right_col": "id",
                "reason": "foreign key"} in joins
        r = case.rows("current", "message", limit=1)
        tag = case.add_tag({"label": "Key", "note": "first", "source": "table", "table": "message",
                            "rowid": 1, "columns": r["columns"], "cells": r["rows"][0]})
        case.save_query("Contacts", "SELECT name FROM contact ORDER BY id", "current")
        html = report.generate_html(case)
        assert "C-7" in html and "Key" in html and "Contacts" in html and "Contact 1" in html
        assert case.evidence["database"].hashes["sha256"] in html
        data = report.generate_json(case)
        assert data["tags"][0]["id"] == tag["id"] and data["queries"][0]["rows"]
        case.delete_tag(tag["id"])
        assert case.tags == []
    finally:
        case.close()


def test_page_detail(messages_db):
    case = Case(str(messages_db))
    try:
        pm = case.page_map()
        leaf = next(p for p in pm if p["type"] == "table_leaf" and p["owner"] == "message")
        d = case.page_detail("current", leaf["page"])
        assert d["type"] == "table_leaf" and d["cells"] and len(bytes.fromhex(d["raw"])) == d["size"]
        assert any(r["kind"] == "cell" for r in d["regions"])
    finally:
        case.close()


def test_api_round_trip(messages_db):
    api = Api()
    res = api.open_case(str(messages_db), {"case_number": "1"})
    assert res["ok"], res
    assert api.rows("current", "contact")["data"]["total"] == 27
    q = api.query("current", "SELECT count(*) AS n FROM message")
    assert q["ok"] and q["data"]["rows"][0][0]["v"] == 172
    bad = api.query("current", "DELETE FROM message")
    assert not bad["ok"] and "not authorized" in bad["error"]
    rec = api.recovered({"status": "deleted"})
    assert rec["ok"] and rec["data"]["total"] > 0
    assert api.close_case()["ok"]
    assert not api.rows("current", "contact")["ok"]
    assert not api.open_case(os.devnull + "-missing")["ok"]


def test_file_dialog_filters_are_valid():
    """pywebview raises ValueError for any filter it can't parse (the File… button broke on one).
    This is its validation pattern (webview/util.py, parse_file_type)."""
    import re

    from backend import api

    pattern = r'^([\w ]+)\((\*(?:\.(?:\w+|\*))*(?:;\*(?:\.(?:\w+|\*))*)*)\)$'
    filters = list(api.DB_FILE_TYPES) + [f"{k.upper()} file (*.{k})" for k in ("csv", "html", "json", "png", "bin")]
    for f in filters:
        assert re.search(pattern, f), f
