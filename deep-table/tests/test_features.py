"""Search across tables, the rebuilt recovered database, the WAL timeline, BLOB export and decode chains."""
import base64
import csv
import struct
import zlib

import pytest

from backend import transforms
from backend.session import Case, CaseError
from conftest import connect, snapshot


def test_search_all(messages_db):
    case = Case(str(messages_db))
    try:
        r = case.search_all("current", "body 42 ")
        assert r["total"] == 1 and r["tables"][0]["table"] == "message"
        assert "body" in r["tables"][0]["hits"][0]
        r = case.search_all("current", "contact 1")  # case-insensitive, several tables' text
        assert any(t["table"] == "contact" for t in r["tables"])
        gone = next(x for x in case.recover() if x["table"] == "message" and x["status"] == "deleted")
        term = " ".join(gone["values"][2].split()[:3]) + " "  # "message body N ": only among recovered records
        r = case.search_all("current", term)
        assert not r["tables"] and any(x["status"] == "deleted" for x in r["recovered"])
        with pytest.raises(CaseError):
            case.search_all("current", "x")
    finally:
        case.close()


def test_search_inside_blobs(tmp_path):
    path = tmp_path / "b.db"
    conn = connect(path)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, data BLOB)")
    conn.execute("INSERT INTO t(data) VALUES (?)", (b"\x00\x01needle-in-binary\x00\xff",))
    conn.commit()
    conn.close()
    case = Case(str(path))
    try:
        r = case.search_all("current", "needle-in")
        assert r["total"] == 1 and r["tables"][0]["hits"][0] == ["data"]
    finally:
        case.close()


def test_recovered_records_as_database(messages_db):
    case = Case(str(messages_db))
    try:
        deleted = [r for r in case.recover() if r["table"] == "contact" and r["status"] == "deleted"]
        q = case.query("recovered", "SELECT dt_status, dt_source, name FROM contact WHERE dt_status = 'deleted' ORDER BY name")
        assert [row[2]["v"] for row in q["rows"]] == sorted(r["values"][1] for r in deleted)
        assert {o["name"] for o in case.objects("recovered")} >= {"contact", "message"}
        with pytest.raises(CaseError):
            case.query("recovered", "DELETE FROM contact")
        case.recover(force=True)  # re-running rebuilds it
        assert case.query("recovered", "SELECT count(*) FROM contact")["rows"][0][0]["v"] >= 2
    finally:
        case.close()


def test_wal_timeline(wal_db):
    case = Case(str(wal_db))
    try:
        t = case.timeline()
        assert [(c["commit"], c["insert"], c["update"], c["delete"]) for c in t["commits"]] == \
            [(1, 0, 1, 0), (2, 0, 0, 2), (3, 1, 0, 0)]
        ops = [(e["commit"], e["op"], e["rowid"]) for e in t["events"]]
        assert ops == [(1, "update", 5), (2, "delete", 10), (2, "delete", 11), (3, "insert", 41)]
        upd = t["events"][0]
        assert upd["changed"] == ["title"]
        assert upd["before"][1]["v"] == "Note 5" and upd["after"][1]["v"] == "Edited title"
        assert upd["ts"].endswith("UTC")  # the row's own timestamp gives the change a time
    finally:
        case.close()


def test_timeline_without_wal(messages_db):
    case = Case(str(messages_db))
    try:
        assert case.timeline()["events"] == []
    finally:
        case.close()


def test_export_blobs(tmp_path):
    work = tmp_path / "w.db"
    conn = connect(work)
    conn.execute("CREATE TABLE att(id INTEGER PRIMARY KEY, name TEXT, data BLOB)")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    conn.executemany("INSERT INTO att(name, data) VALUES (?, ?)", [("a", png), ("b", b"%PDF-1.4 ..."), ("c", None)])
    conn.commit()
    conn.close()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    db = snapshot(work, evidence / "e.db")
    out = tmp_path / "out"
    out.mkdir()
    case = Case(str(db))
    try:
        assert case.blob_columns("current", "att") == [{"name": "data", "count": 2}]
        r = case.export_blobs("current", "att", "data", str(out))
        assert r["count"] == 2
        assert (out / "att-1-data.png").read_bytes() == png
        assert (out / "att-2-data.pdf").exists()
        rows = list(csv.DictReader(open(r["manifest"], encoding="utf-8")))
        assert rows[0]["type"] == "PNG image" and len(rows[0]["sha256"]) == 64
        with pytest.raises(CaseError):  # never write next to the evidence
            case.export_blobs("current", "att", "data", str(evidence))
    finally:
        case.close()


def test_decode_chain(messages_db):
    case = Case(str(messages_db))
    try:
        payload = base64.b64encode(zlib.compress(b'{"k": "v"}')).decode()
        bid = case.put_text(payload)
        first = case.blob(bid)
        assert "base64" in first["suggest"]
        assert case.blob(bid, ["base64"])["kind"] == "zlib data"
        final = case.blob(bid, ["base64", "zlib"])
        assert final["kind"] == "JSON" and final["decoded"] == {"k": "v"} and final["chain"] == ["base64", "zlib"]
        with pytest.raises(CaseError):
            case.blob(bid, ["gzip"])
    finally:
        case.close()


def test_transforms():
    assert transforms.hex_text(b"68 65 6c 6c 6f") == b"hello"
    assert transforms.url_decode(b"a%20b+c") == b"a b c"
    assert transforms.b64(b"aGVsbG8") == b"hello"
    assert transforms.utf16("hé".encode("utf-16-le")) == "hé".encode()
    assert transforms.deflate_raw(zlib.compress(b"xyz" * 9)[2:-4]) == b"xyz" * 9
    # Snappy: literal + back-reference ("abcd" then copy 8 bytes at offset 4)
    assert transforms.snappy(bytes([12, 0x0C]) + b"abcd" + bytes([0x11, 0x04])) == b"abcdabcdabcd"
    # LZ4 block: literals "abc" then match offset 3 length 6, Mozilla header around it
    block = bytes([0x32]) + b"abc" + struct.pack("<H", 3)
    assert transforms.lz4_block(block) == b"abcabcabc"
    assert transforms.lz4(b"mozLz40\x00" + struct.pack("<I", 9) + block) == b"abcabcabc"
    assert transforms.msgpack_decode(bytes.fromhex("82a16101a16292c3a3666f6f")) == {"a": 1, "b": [True, "foo"]}
    assert transforms.bencode_decode(b"d3:agei30e4:name5:alice4:tagsl1:x1:yee") == \
        {"age": 30, "name": b"alice", "tags": [b"x", b"y"]}
    assert 7.9 < transforms.entropy(bytes(range(256)) * 4) <= 8.0
    assert (0, "hello") in transforms.strings(b"hello\x00\x01")
    with pytest.raises(transforms.TransformError):
        transforms.apply_chain(b"not compressed", ["zlib"])
