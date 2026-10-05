"""Chromium / Electron web storage held in LevelDB: Local Storage, Session Storage and IndexedDB.

Each decoder turns raw LevelDB records into rows with readable columns. Every record is decoded, whatever
its state (live, older version, deleted), so deleted web-storage values show up just like live ones.

References: Chromium components/services/storage (dom_storage, indexed_db) and V8's ValueSerializer
(src/objects/value-serializer.cc). Layouts are as observed in current Chromium; anything unrecognised is
kept as raw bytes rather than guessed at.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import math
import struct

from .decoders import protobuf_decode
from .leveldb import LevelDb, Record, TYPE_VALUE
from .transforms import TransformError, snappy_raw

UTC = dt.timezone.utc


class DecodeError(Exception):
    pass


def kind_of(db: LevelDb) -> str:
    """localstorage | sessionstorage | indexeddb | leveldb (anything else)."""
    path = db.path.replace("\\", "/").lower()
    if db.comparator == "idb_cmp1" or path.endswith(".indexeddb.leveldb"):
        return "indexeddb"
    keys = {r.key for r in db.records[:5000]}
    if any(k.startswith((b"namespace-", b"map-")) for k in keys) or "session storage" in path:
        return "sessionstorage"
    if any(k.startswith((b"META:", b"_http", b"_file", b"_chrome")) for k in keys) or "local storage" in path:
        return "localstorage"
    return "leveldb"


# ---------------------------------------------------------------- Local / Session Storage
def _storage_string(raw: bytes) -> str:
    """DOMStorage values: a 0x01 prefix means Latin-1 bytes, 0x00 means UTF-16LE."""
    if not raw:
        return ""
    if raw[0] == 1:
        return raw[1:].decode("latin-1")
    if raw[0] == 0:
        return raw[1:].decode("utf-16-le", "replace")
    return raw.decode("utf-8", "replace")


def local_storage(db: LevelDb) -> dict:
    items, origins = [], {}
    for r in db.records:
        if r.kind != TYPE_VALUE:
            continue
        k = r.key
        if k.startswith(b"_") and b"\x00" in k:
            origin, _, key = k[1:].partition(b"\x00")
            items.append((r, {"origin": origin.decode("utf-8", "replace"), "key": _storage_string(key),
                              "value": _storage_string(r.value), "value_bytes": len(r.value)}))
        elif k.startswith((b"META:", b"METAACCESS:")):
            name, _, origin = k.partition(b":")
            fields = {f["field"]: f["value"] for f in (protobuf_decode(r.value) or []) if f["wire"] == 0}
            row = {"origin": origin.decode("utf-8", "replace")}
            if name == b"META":
                row.update({"last_modified": fields.get(1), "size_bytes": fields.get(2)})
            else:
                row.update({"last_accessed": fields.get(1)})
            items.append((r, {"$meta": True, **row}))
    rows = [(r, row) for r, row in items if "$meta" not in row]
    for r, row in items:
        if "$meta" in row and r.state == "live":
            o = origins.setdefault(row["origin"], {"origin": row["origin"]})
            o.update({k: v for k, v in row.items() if k not in ("$meta", "origin") and v is not None})
    return {
        "tables": {
            "local_storage": {"columns": ["origin", "key", "value", "value_bytes"], "rows": rows},
        },
        "extra": {"local_storage_origins": {"columns": ["origin", "last_modified", "last_accessed", "size_bytes"],
                                            "rows": [[o.get(c) for c in ("origin", "last_modified", "last_accessed",
                                                                         "size_bytes")] for o in origins.values()]}},
    }


def session_storage(db: LevelDb) -> dict:
    maps: dict[str, tuple[str, str, bool]] = {}  # map id -> (namespace, origin, live)
    for r in db.records:
        if r.kind != TYPE_VALUE or not r.key.startswith(b"namespace-"):
            continue
        rest = r.key[len(b"namespace-"):].decode("utf-8", "replace")
        ns, origin = rest[:36], rest[37:]
        mid = r.value.decode("ascii", "replace")
        if mid not in maps or r.state == "live":
            maps[mid] = (ns, origin, r.state == "live")
    rows = []
    for r in db.records:
        if r.kind != TYPE_VALUE or not r.key.startswith(b"map-"):
            continue
        mid, _, key = r.key[4:].partition(b"-")
        mid = mid.decode("ascii", "replace")
        ns, origin, _ = maps.get(mid, ("", "", False))
        try:
            value = r.value.decode("utf-16-le")
        except UnicodeDecodeError:
            value = r.value.decode("utf-8", "replace")
        rows.append((r, {"namespace": ns, "origin": origin, "map_id": mid, "key": key.decode("utf-8", "replace"),
                         "value": value}))
    return {"tables": {"session_storage": {"columns": ["origin", "key", "value", "namespace", "map_id"], "rows": rows}},
            "extra": {}}


# ---------------------------------------------------------------- IndexedDB key encoding
def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise DecodeError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _string_with_length(buf: bytes, pos: int) -> tuple[str, int]:
    n, pos = _varint(buf, pos)
    end = pos + 2 * n
    if end > len(buf):
        raise DecodeError("truncated string")
    return buf[pos:end].decode("utf-16-be", "replace"), end


def key_prefix(key: bytes) -> tuple[int, int, int, int]:
    """(database id, object store id, index id, position after the prefix)."""
    if not key:
        raise DecodeError("empty key")
    b0 = key[0]
    lens = (((b0 >> 5) & 7) + 1, ((b0 >> 2) & 7) + 1, (b0 & 3) + 1)
    pos, ids = 1, []
    for n in lens:
        if pos + n > len(key):
            raise DecodeError("truncated key prefix")
        ids.append(int.from_bytes(key[pos:pos + n], "little"))
        pos += n
    return ids[0], ids[1], ids[2], pos


def decode_idb_key(buf: bytes, pos: int = 0, depth: int = 0):
    """An IndexedDB key (the user-visible key of a record). Returns (python value, new position)."""
    if pos >= len(buf) or depth > 50:
        raise DecodeError("truncated IDB key")
    t = buf[pos]
    pos += 1
    if t == 0:
        return None, pos
    if t == 1:
        return _string_with_length(buf, pos)
    if t in (2, 3):
        val = struct.unpack("<d", buf[pos:pos + 8])[0]
        if t == 2:
            return {"$date": _js_date(val)}, pos + 8
        return (int(val) if val.is_integer() and abs(val) < 2 ** 53 else val), pos + 8
    if t == 4:
        n, pos = _varint(buf, pos)
        out = []
        for _ in range(n):
            v, pos = decode_idb_key(buf, pos, depth + 1)
            out.append(v)
        return out, pos
    if t == 5:
        return {"$minkey": True}, pos
    if t == 6:
        n, pos = _varint(buf, pos)
        return {"$binary": buf[pos:pos + n].hex()}, pos + n
    raise DecodeError(f"unknown IDB key type {t}")


def key_text(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _js_date(ms: float) -> str:
    if math.isnan(ms):
        return "Invalid Date"
    try:
        return (dt.datetime(1970, 1, 1, tzinfo=UTC) + dt.timedelta(milliseconds=ms)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except OverflowError:
        return str(ms)


# ---------------------------------------------------------------- V8 / Blink value deserialisation
class V8Reader:
    """Enough of V8's ValueSerializer format for what web pages store in IndexedDB."""

    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.p = data, pos
        self.objects: list = []
        self.version = 15

    def byte(self) -> int:
        if self.p >= len(self.d):
            raise DecodeError("truncated value")
        b = self.d[self.p]
        self.p += 1
        return b

    def take(self, n: int) -> bytes:
        if self.p + n > len(self.d):
            raise DecodeError("truncated value")
        chunk = self.d[self.p:self.p + n]
        self.p += n
        return chunk

    def varint(self) -> int:
        v, self.p = _varint(self.d, self.p)
        return v

    def zigzag(self) -> int:
        v = self.varint()
        return (v >> 1) ^ -(v & 1)

    def double(self) -> float:
        return struct.unpack("<d", self.take(8))[0]

    def tag(self) -> int:
        while True:  # padding bytes may appear anywhere
            t = self.byte()
            if t != 0x00:
                return t

    def peek_tag(self) -> int:
        p = self.p
        while p < len(self.d) and self.d[p] == 0:
            p += 1
        if p >= len(self.d):
            raise DecodeError("truncated value")
        return self.d[p]

    def string(self, t: int) -> str:
        n = self.varint()
        raw = self.take(n)
        if t == 0x22:  # '"' one-byte (Latin-1)
            return raw.decode("latin-1")
        if t == 0x63:  # 'c' two-byte
            return raw.decode("utf-16-le", "replace")
        return raw.decode("utf-8", "replace")  # 'S'

    def remember(self, obj):
        self.objects.append(obj)
        return obj

    def read(self, depth: int = 0):
        if depth > 200:
            raise DecodeError("value nested too deeply")
        t = self.tag()
        c = chr(t)
        if c == "_":
            return {"$undefined": True}
        if c == "0":
            return None
        if c == "T":
            return True
        if c == "F":
            return False
        if c == "I":
            return self.zigzag()
        if c == "U":
            return self.varint()
        if c == "N":
            v = self.double()
            return v if math.isfinite(v) else str(v)
        if c == "Z":
            return self.bigint()
        if c in ('"', "c", "S"):
            return self.string(t)
        if c == "^":
            ref = self.varint()
            return self.objects[ref] if ref < len(self.objects) else {"$ref": ref}
        if c == "o":
            obj = self.remember({})
            self.props(obj, ord("{"), depth)
            return obj
        if c == "A":  # dense array
            n = self.varint()
            arr = self.remember([])
            for _ in range(n):
                if self.peek_tag() == ord("-"):  # the hole
                    self.tag()
                    arr.append(None)
                else:
                    arr.append(self.read(depth + 1))
            extra = {}
            self.props(extra, ord("$"), depth, trailing=2)
            return arr if not extra else {"$array": arr, **extra}
        if c == "a":  # sparse array
            length = self.varint()
            obj = self.remember({})
            self.props(obj, ord("@"), depth, trailing=2)
            arr = [None] * min(length, 100000)
            rest = {}
            for k, v in obj.items():
                if k.isdigit() and int(k) < len(arr):
                    arr[int(k)] = v
                else:
                    rest[k] = v
            return arr if not rest else {"$array": arr, **rest}
        if c == "D":
            return self.remember({"$date": _js_date(self.double())})
        if c in ("y", "x"):
            return self.remember(c == "y")
        if c == "n":
            return self.remember(self.double())
        if c == "z":
            return self.remember(self.bigint())
        if c == "s":
            return self.remember(self.read(depth + 1))
        if c == "R":
            pattern = self.read(depth + 1)
            flags = self.varint()
            return self.remember({"$regexp": pattern, "flags": flags})
        if c == ";":
            m = self.remember({"$map": []})
            while self.peek_tag() != ord(":"):
                m["$map"].append([self.read(depth + 1), self.read(depth + 1)])
            self.tag()
            self.varint()
            return m
        if c == "'":
            s = self.remember({"$set": []})
            while self.peek_tag() != ord(","):
                s["$set"].append(self.read(depth + 1))
            self.tag()
            self.varint()
            return s
        if c == "B":
            n = self.varint()
            buf = self.take(n)
            out = self.remember({"$arraybuffer": base64.b64encode(buf).decode(), "bytes": n})
            if self.p < len(self.d) and self.peek_tag() == ord("V"):
                self.tag()
                return self.view(out)
            return out
        if c == "V":  # a view whose buffer was the previous object
            return self.view(self.objects[-1] if self.objects else None)
        if c == "r":
            return self.error(depth)
        if c == "\\":
            return self.host_object()
        if c == "?":  # verify object count
            self.varint()
            return self.read(depth)
        raise DecodeError(f"unsupported V8 tag {t:#04x} ({c!r}) at {self.p - 1}")

    def props(self, obj: dict, end_tag: int, depth: int, trailing: int = 1):
        while self.peek_tag() != end_tag:
            k = self.read(depth + 1)
            obj[str(k)] = self.read(depth + 1)
        self.tag()
        for _ in range(trailing):
            self.varint()

    def bigint(self) -> int:
        bitfield = self.varint()
        n = bitfield >> 1
        value = int.from_bytes(self.take(n), "little")
        return -value if bitfield & 1 else value

    def view(self, buffer):
        sub = chr(self.byte())
        offset, length = self.varint(), self.varint()
        if self.version >= 14:
            self.varint()  # flags
        names = {"b": "Int8Array", "B": "Uint8Array", "C": "Uint8ClampedArray", "w": "Int16Array",
                 "W": "Uint16Array", "d": "Int32Array", "D": "Uint32Array", "f": "Float32Array",
                 "F": "Float64Array", "q": "BigInt64Array", "Q": "BigUint64Array", "?": "DataView"}
        out = {"$view": names.get(sub, sub), "offset": offset, "length": length}
        if isinstance(buffer, dict) and "$arraybuffer" in buffer:
            raw = base64.b64decode(buffer["$arraybuffer"])[offset:offset + length]
            out["hex"] = raw[:256].hex(" ")
        return self.remember(out)

    def error(self, depth: int):
        out = {"$error": "Error"}
        while True:
            t = chr(self.byte())
            if t == ".":
                break
            if t in "EFRSTU":
                out["$error"] = {"E": "EvalError", "R": "RangeError", "F": "ReferenceError", "S": "SyntaxError",
                                 "T": "TypeError", "U": "URIError"}[t]
            elif t == "m":
                out["message"] = self.read(depth + 1)
            elif t == "s":
                out["stack"] = self.read(depth + 1)
            elif t == "c":
                out["cause"] = self.read(depth + 1)
            else:
                break
        return self.remember(out)

    def host_object(self):
        """Blink host objects: blobs and files are stored outside LevelDB, referenced by index."""
        t = chr(self.byte())
        if t == "i":  # BlobIndex
            return self.remember({"$blob_index": self.varint()})
        if t == "e":  # FileIndex
            return self.remember({"$file_index": self.varint()})
        if t == "b":  # Blob (uuid, type, size)
            uuid = self.string(ord("S"))
            typ = self.string(ord("S"))
            return self.remember({"$blob": uuid, "type": typ, "size": self.varint()})
        raise DecodeError(f"unsupported Blink host object {t!r}")


def unwrap_blink(value: bytes) -> tuple[int, bytes]:
    """Strip Chromium's IndexedDB value envelope: (IDB version, V8-serialised bytes).
    Layout: varint version, then Blink's 0xFF <version>, an optional 0xFE trailer-offset field (8+4 bytes),
    then V8's own 0xFF <version>. Values Chromium compressed (0xFF 0x11 0x02) are Snappy-decompressed."""
    version, pos = _varint(value, 0)
    data = value[pos:]
    if data[:3] == b"\xff\x11\x02":
        try:
            data = snappy_raw(data[3:])
        except TransformError as exc:
            raise DecodeError(f"compressed value: {exc}") from exc
    if data[:3] == b"\xff\x11\x01":
        raise DecodeError("value is stored in an external blob file")
    p = 0
    if data[p:p + 1] == b"\xff":
        _blink, p = _varint(data, p + 1)
        if data[p:p + 1] == b"\xfe":
            p += 13
    return version, data[p:]


def decode_idb_value(value: bytes):
    version, payload = unwrap_blink(value)
    r = V8Reader(payload)
    if payload[:1] == b"\xff":
        r.p = 1
        r.version = r.varint()
    return version, r.read()


def to_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def indexeddb(db: LevelDb) -> dict:
    """Databases, object stores and every object-store record. Names come from the metadata records
    (whatever their state), so records of a deleted object store are still labelled."""
    db_names: dict[int, tuple[str, str]] = {}
    store_names: dict[tuple[int, int], str] = {}
    store_meta: dict[tuple[int, int], dict] = {}
    db_meta: dict[int, dict] = {}
    data_records: list[tuple[Record, int, int, int]] = []
    for r in db.records:
        if r.kind != TYPE_VALUE:
            continue
        try:
            dbid, osid, idx, p = key_prefix(r.key)
        except DecodeError:
            continue
        rest = r.key[p:]
        try:
            if dbid == 0 and osid == 0 and idx == 0 and rest[:1] == b"\xc9":  # DatabaseName
                origin, q = _string_with_length(rest, 1)
                name, _ = _string_with_length(rest, q)
                did, _ = _varint(r.value, 0)
                if did not in db_names or r.state == "live":
                    db_names[did] = (origin, name)
            elif dbid and osid == 0 and idx == 0 and rest:
                t = rest[0]
                if t == 50 and len(rest) > 2:  # ObjectStoreMetaData
                    sid, q = _varint(rest, 1)
                    meta = rest[q] if q < len(rest) else None
                    m = store_meta.setdefault((dbid, sid), {})
                    if meta == 0 and ((dbid, sid) not in store_names or r.state == "live"):
                        store_names[(dbid, sid)] = r.value.decode("utf-16-be", "replace")
                    elif meta == 1:
                        m["key_path"] = _key_path(r.value)
                    elif meta == 2:
                        m["auto_increment"] = bool(r.value and r.value[0])
                elif t == 4 and r.state == "live":  # user version
                    db_meta.setdefault(dbid, {})["version"] = _varint(r.value, 0)[0] if r.value else None
            elif dbid and osid and idx == 1:
                data_records.append((r, dbid, osid, p))
        except (DecodeError, IndexError, UnicodeDecodeError):
            continue

    rows = []
    for r, dbid, osid, p in data_records:
        origin, dbname = db_names.get(dbid, ("", f"database {dbid}"))
        store = store_names.get((dbid, osid), f"object store {osid}")
        try:
            key, _ = decode_idb_key(r.key, p)
            ktext = key_text(key)
        except DecodeError as exc:
            ktext = f"[undecodable key: {exc}]"
        err, value_json, version = "", None, None
        try:
            version, value = decode_idb_value(r.value)
            value_json = to_json(value)
        except (DecodeError, IndexError, UnicodeDecodeError, struct.error, OverflowError) as exc:
            err = str(exc)
        rows.append((r, {"origin": origin, "database": dbname, "object_store": store, "key": ktext,
                         "value": value_json, "value_raw": r.value, "decode_error": err or None}))
    databases = [[did, o, n, db_meta.get(did, {}).get("version")] for did, (o, n) in sorted(db_names.items())]
    stores = [[db_names.get(d, ("", f"database {d}"))[1], s, n, store_meta.get((d, s), {}).get("key_path"),
               store_meta.get((d, s), {}).get("auto_increment")] for (d, s), n in sorted(store_names.items())]
    return {
        "tables": {"indexeddb_records": {"columns": ["database", "object_store", "key", "value", "origin",
                                                     "decode_error", "value_raw"], "rows": rows}},
        "extra": {
            "indexeddb_databases": {"columns": ["id", "origin", "name", "version"], "rows": databases},
            "indexeddb_object_stores": {"columns": ["database", "id", "name", "key_path", "auto_increment"],
                                        "rows": stores},
        },
    }


def _key_path(raw: bytes):
    """Object store key path: a string (UTF-16BE), or an encoded array/null."""
    if raw[:2] == b"\x00\x00" and len(raw) > 2:  # typed encoding: 0 0 then type byte
        t = raw[2]
        if t == 0:
            return None
        if t == 1:
            return _string_with_length(raw, 3)[0]
        if t == 2:
            n, p = _varint(raw, 3)
            out = []
            for _ in range(n):
                s, p = _string_with_length(raw, p)
                out.append(s)
            return out
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return raw.hex()


def generic(db: LevelDb) -> dict:
    return {"tables": {}, "extra": {}}


DECODERS = {"localstorage": local_storage, "sessionstorage": session_storage, "indexeddb": indexeddb,
            "leveldb": generic}
