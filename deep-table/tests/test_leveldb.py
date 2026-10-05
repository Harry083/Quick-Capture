"""LevelDB and Chromium web storage, tested against stores written by a real Chromium (see fixtures/)."""
import json
import struct
import zipfile
from pathlib import Path

import pytest

from backend import chromium, leveldb
from backend.session import Case
from conftest import sha256

FIXTURE = Path(__file__).parent / "fixtures" / "chromium-stores.zip"
IDB = "IndexedDB/http_127.0.0.1_8765.indexeddb.leveldb"


@pytest.fixture(scope="module")
def stores(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("chromium")
    with zipfile.ZipFile(FIXTURE) as z:
        z.extractall(root)
    return root


def rows(case, table, view="current"):
    d = case.rows(view, table, limit=2000)
    return [{c: (cell.get("s") if cell.get("s") is not None else cell.get("v")) for c, cell in zip(d["columns"], r)}
            for r in d["rows"]]


# ---------------------------------------------------------------- raw reader
def test_reads_logs_tables_and_manifest(stores):
    db = leveldb.open_dir(stores / IDB)
    assert db.comparator == "idb_cmp1" and db.manifest.last_sequence > 0
    kinds = {f["kind"]: f for f in db.files}
    assert kinds["table"]["records"] > 0 and kinds["table"]["in_version"]  # the .ldb table was read
    assert kinds["log"]["records"] > 0
    assert all(r.crc_ok for r in db.records)
    assert not db.errors
    states = {r.state for r in db.records}
    assert states == {"live", "older version", "deleted", "tombstone"}


def write_log(records: list[bytes]) -> bytes:
    """A LevelDB log with records split across 32 KiB blocks, as LevelDB writes them."""
    out = bytearray()
    for payload in records:
        first = True
        while True:
            left = leveldb.BLOCK - len(out) % leveldb.BLOCK
            if left < 7:
                out += b"\x00" * left
                left = leveldb.BLOCK
            chunk = payload[:left - 7]
            payload = payload[len(chunk):]
            last = not payload
            kind = 1 if first and last else 2 if first else 4 if last else 3
            crc = leveldb.crc32c(bytes([kind]) + chunk)
            masked = (((crc >> 15) | (crc << 17)) + 0xA282EAD8) & 0xFFFFFFFF
            out += struct.pack("<IHB", masked, len(chunk), kind) + chunk
            first = False
            if last:
                break
    return bytes(out)


def batch(seq: int, ops) -> bytes:
    body = b""
    for op in ops:
        if op[0] == "put":
            body += b"\x01" + bytes([len(op[1])]) + op[1] + _v(len(op[2])) + op[2]
        else:
            body += b"\x00" + bytes([len(op[1])]) + op[1]
    return struct.pack("<QI", seq, len(ops)) + body


def _v(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def test_log_records_across_blocks_and_states(tmp_path):
    big = b"v" * 70000  # spans three blocks: FIRST, MIDDLE, LAST
    log = write_log([batch(1, [("put", b"a", b"one"), ("put", b"b", big)]),
                     batch(3, [("put", b"a", b"two")]),
                     batch(4, [("del", b"b")])])
    (tmp_path / "000003.log").write_bytes(log + b"\x07\x00\x00")  # plus a torn tail
    db = leveldb.open_dir(tmp_path)
    got = {(r.key, r.seq): (r.state, r.value) for r in db.records}
    assert got[(b"a", 3)] == ("live", b"two")
    assert got[(b"a", 1)] == ("older version", b"one")
    assert got[(b"b", 2)] == ("deleted", big)
    assert got[(b"b", 4)][0] == "tombstone"
    assert all(r.crc_ok for r in db.records)


def test_bad_checksum_is_flagged(tmp_path):
    log = bytearray(write_log([batch(1, [("put", b"k", b"value")])]))
    log[-1] ^= 0xFF
    (tmp_path / "000003.log").write_bytes(bytes(log))
    db = leveldb.open_dir(tmp_path)
    assert len(db.records) == 1 and not db.records[0].crc_ok


def test_not_leveldb(tmp_path):
    (tmp_path / "notes.txt").write_text("hello")
    with pytest.raises(leveldb.LevelDbError):
        leveldb.open_dir(tmp_path)


# ---------------------------------------------------------------- Chromium decoding
def test_local_storage(stores):
    case = Case(str(stores / "Local Storage" / "leveldb"))
    try:
        assert case.summary()["leveldb"]["store"] == "localstorage"
        live = {r["key"]: r["value"] for r in rows(case, "local_storage")}
        assert live == {"username": "alice@example.com", "theme": "light", "unicode": "café ☕ 東京"}
        rec = {(r["values"][1], r["status"]): r["values"][2] for r in case.recover()}
        assert rec[("draft", "deleted")] == "Meet at the station at 10, don't tell Bob"
        assert rec[("theme", "older version")] == "dark"
        origin = rows(case, "local_storage_origins")[0]
        assert origin["origin"] == "http://127.0.0.1:8765" and origin["size_bytes"] > 0
    finally:
        case.close()


def test_session_storage(stores):
    case = Case(str(stores / "Session Storage"))
    try:
        rec = {r["values"][1]: (r["status"], r["values"][2]) for r in case.recover()}
        # the browsing session ended, so Chromium cleared it: the values only survive as deleted records
        assert rec["cart"] == ("deleted", '{"items":[1,2,3],"total":42.5}')
        assert rec["tab"] == ("deleted", "inbox")
    finally:
        case.close()


def test_indexeddb(stores):
    case = Case(str(stores / IDB / "CURRENT"))  # choosing any file inside the folder opens the folder
    try:
        s = case.summary()
        assert s["kind"] == "leveldb" and s["leveldb"]["store"] == "indexeddb"
        assert rows(case, "indexeddb_databases")[0]["name"] == "chat"
        stores_ = {r["name"]: r for r in rows(case, "indexeddb_object_stores")}
        assert stores_["messages"]["key_path"] == "id"
        recs = rows(case, "indexeddb_records")
        assert not [r for r in recs if r["decode_error"]]
        msgs = {int(r["key"]): json.loads(r["value"]) for r in recs if r["object_store"] == "messages"}
        assert sorted(msgs) == [i for i in range(1, 41) if i not in (7, 8)]
        m2 = msgs[2]
        assert m2["text"] == "IDB message number 2" and m2["sender"] == "bob"
        assert m2["sent"] == {"$date": "2024-01-01T09:02:00.000Z"}
        assert m2["meta"]["big"] == 2 ** 70 and m2["meta"]["f"] == 1.0 and m2["tags"] == ["a", "b"]
        assert msgs[5]["text"] == "IDB message number 5 (edited)"
        settings = {r["key"]: json.loads(r["value"]) for r in recs
                    if r["object_store"] == "settings" and not r["key"].startswith("bulk-")}  # padding: over the grid cap
        assert settings["map"] == {"$map": [["k", 1]]} and settings["set"] == {"$set": ["x", "y"]}
        assert settings["bytes"]["bytes"] == 4 and settings["audio"]["list"] == [1, None, "x"]
        assert settings["theme"] == "light"

        rec = case.recover()
        deleted = {int(r["values"][2]): json.loads(r["values"][3]) for r in rec
                   if r["status"] == "deleted" and r["values"][1] == "messages"}
        assert sorted(deleted) == [7, 8] and deleted[7]["text"] == "IDB message number 7"
        older = [json.loads(r["values"][3]) for r in rec if r["status"] == "older version"]
        assert {"IDB message number 5"} <= {o.get("text") for o in older if isinstance(o, dict)}
        assert "dark" in older

        # SQL over the JSON values, the rebuilt database of deleted records, search and the timeline
        q = case.query("current", "SELECT json_extract(value, '$.text') FROM indexeddb_records "
                                  "WHERE object_store = 'messages' AND key = '3'")
        assert q["rows"][0][0]["v"] == "IDB message number 3"
        d = case.query("recovered", "SELECT key FROM indexeddb_records WHERE dt_status = 'deleted' ORDER BY key")
        assert [r[0]["v"] for r in d["rows"]] == ["7", "8"]
        found = case.search_all("current", "number 7")
        assert any(r["status"] == "deleted" for r in found["recovered"])
        ops = [(e["op"], e["after"] or e["before"]) for e in case.timeline()["events"]]
        assert sum(1 for op, _ in ops if op == "update") == 2  # message 5 and the theme setting
        assert sum(1 for op, _ in ops if op == "delete") == 2  # messages 7 and 8
    finally:
        case.close()


def test_leveldb_evidence_untouched(stores):
    folder = stores / "Local Storage" / "leveldb"
    before = {p.name: (sha256(p), p.stat().st_mtime_ns) for p in folder.iterdir()}
    case = Case(str(folder))
    try:
        case.summary()
        case.recover()
        case.timeline()
        names = {e["name"] for e in case.summary()["evidence"]}
        assert names == set(before)
        assert case.evidence["database"].hashes["sha256"]  # the folder digest
    finally:
        case.close()
    assert {p.name: (sha256(p), p.stat().st_mtime_ns) for p in folder.iterdir()} == before


def test_v8_values():
    def v8(body: bytes):
        return chromium.V8Reader(b"\xff\x0f" + body, 2).read()

    assert v8(b'o"\x01aI\x04{\x01') == {"a": 2}
    assert v8(b"A\x02I\x02_$\x00\x02") == [1, {"$undefined": True}]
    assert v8(b"c\x04h\x00i\x00") == "hi"
    assert v8(b"N" + struct.pack("<d", 1.5)) == 1.5
    assert v8(b"Z\x02\x05") == 5 and v8(b"Z\x03\x05") == -5
    # Blink's envelope (IDB version, 0xFF version, trailer offset) around V8's own header
    raw = b"\x03\xff\x15\xfe" + b"\x00" * 12 + b"\xff\x0fT"
    assert chromium.decode_idb_value(raw) == (3, True)
