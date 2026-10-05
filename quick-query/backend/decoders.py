"""Value decoders: timestamps, BLOB type detection, plists, protobuf and hex dumps.

Everything here works on plain Python values, so it applies equally to live rows, query results and carved
records.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import gzip
import json
import plistlib
import struct
import zlib

UTC = dt.timezone.utc
UNIX_EPOCH = dt.datetime(1970, 1, 1, tzinfo=UTC)

# ---------------------------------------------------------------- timestamps
# name -> (label, epoch offset from 1970 in seconds, units per second, SQL expression template).
# The SQL template turns a column into ISO text inside a query; {c} is the column expression.
TIMESTAMP_FORMATS: dict[str, tuple[str, float, float, str]] = {
    "unix_s": ("Unix seconds (1970)", 0, 1, "datetime({c}, 'unixepoch')"),
    "unix_ms": ("Unix milliseconds", 0, 1e3, "strftime('%Y-%m-%d %H:%M:%f', {c} / 1000.0, 'unixepoch')"),
    "unix_us": ("Unix microseconds", 0, 1e6, "strftime('%Y-%m-%d %H:%M:%f', {c} / 1000000.0, 'unixepoch')"),
    "unix_ns": ("Unix nanoseconds", 0, 1e9, "strftime('%Y-%m-%d %H:%M:%f', {c} / 1000000000.0, 'unixepoch')"),
    "mac_s": ("Mac absolute / Cocoa (2001)", 978307200, 1,
              "strftime('%Y-%m-%d %H:%M:%f', {c} + 978307200, 'unixepoch')"),
    "mac_ns": ("Mac absolute nanoseconds (2001)", 978307200, 1e9,
               "strftime('%Y-%m-%d %H:%M:%f', {c} / 1000000000.0 + 978307200, 'unixepoch')"),
    "webkit": ("WebKit / Chrome (µs since 1601)", -11644473600, 1e6,
               "strftime('%Y-%m-%d %H:%M:%f', {c} / 1000000.0 - 11644473600, 'unixepoch')"),
    "filetime": ("Windows FILETIME (100 ns since 1601)", -11644473600, 1e7,
                 "strftime('%Y-%m-%d %H:%M:%f', {c} / 10000000.0 - 11644473600, 'unixepoch')"),
    "hfs": ("HFS+ (seconds since 1904)", -2082844800, 1, "datetime({c} - 2082844800, 'unixepoch')"),
    "dotnet": (".NET ticks (100 ns since 0001)", -62135596800, 1e7,
               "strftime('%Y-%m-%d %H:%M:%f', {c} / 10000000.0 - 62135596800, 'unixepoch')"),
    "julian": ("Julian day (SQLite julianday)", -210866760000, 1 / 86400, "datetime({c})"),
    "gps": ("GPS seconds (1980-01-06)", 315964800, 1, "datetime({c} + 315964800, 'unixepoch')"),
}
# Order matters for auto-detection: the first format that puts the values in range wins, so the more
# specific / commoner encodings come first.
DETECT_ORDER = ("unix_s", "unix_ms", "unix_us", "unix_ns", "mac_s", "mac_ns", "webkit", "filetime", "julian",
                "dotnet", "hfs")
PLAUSIBLE = (dt.datetime(1995, 1, 1, tzinfo=UTC), dt.datetime(2040, 1, 1, tzinfo=UTC))


def convert_timestamp(value, fmt: str) -> dt.datetime | None:
    """Convert a number in the given format to an aware UTC datetime, or None if it can't be."""
    if fmt not in TIMESTAMP_FORMATS or isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    _, epoch, per_second, _ = TIMESTAMP_FORMATS[fmt]
    try:
        seconds = value / per_second + epoch
        return UNIX_EPOCH + dt.timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        return None


def format_dt(when: dt.datetime | None) -> str:
    if when is None:
        return ""
    text = when.strftime("%Y-%m-%d %H:%M:%S")
    if when.microsecond:
        text += f".{when.microsecond // 1000:03d}"
    return text + " UTC"


def detect_timestamp_format(values: list, column_name: str = "") -> str | None:
    """Guess which timestamp encoding a column uses from a sample of its values.

    Each candidate format is scored by the share of non-zero numeric values that land between 1995 and 2040;
    it needs at least 80%. A column name that says "date"/"time" lowers the bar to 60%."""
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and v not in (0, -1)]
    if len(nums) < 1:
        return None
    name = column_name.lower()
    hinted = any(k in name for k in ("date", "time", "_ts", "stamp", "created", "modified", "updated", "last",
                                     "expir", "visit", "accessed", "start", "end"))
    if not hinted and all(isinstance(v, int) and abs(v) < 100000 for v in nums):
        return None  # small integers: ids, counts, flags
    threshold = 0.6 if hinted else 0.8
    lo, hi = PLAUSIBLE
    target = dt.datetime(2020, 1, 1, tzinfo=UTC)
    best = None
    for fmt in DETECT_ORDER:
        hits = []
        for v in nums:
            when = convert_timestamp(v, fmt)
            if when is not None and lo <= when <= hi:
                hits.append(when)
        if len(hits) / len(nums) < threshold:
            continue
        if fmt == "julian" and not hinted:
            continue  # 2.4M-ish floats are rare enough that it's only a guess with a hint
        # Several encodings can land in range (WebKit µs read as Mac ns lands in 2001). Prefer the one whose
        # median is nearest the present: real evidence clusters there, misreadings pile up at an epoch.
        hits.sort()
        distance = abs((hits[len(hits) // 2] - target).total_seconds())
        if best is None or distance < best[0]:
            best = (distance, fmt)
    return best[1] if best else None


def all_timestamp_readings(value) -> list[dict]:
    """Every plausible reading of one number: the 'what could this be?' popover in the UI."""
    out = []
    lo, hi = dt.datetime(1970, 1, 2, tzinfo=UTC), dt.datetime(2100, 1, 1, tzinfo=UTC)
    for fmt, (label, *_rest) in TIMESTAMP_FORMATS.items():
        when = convert_timestamp(value, fmt)
        if when is not None and lo <= when <= hi:
            out.append({"format": fmt, "label": label, "value": format_dt(when)})
    return out


# ---------------------------------------------------------------- blob detection
SIGNATURES: list[tuple[bytes, int, str, str]] = [
    # (magic, offset, kind, mime)
    (b"\xff\xd8\xff", 0, "JPEG image", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", 0, "PNG image", "image/png"),
    (b"GIF87a", 0, "GIF image", "image/gif"),
    (b"GIF89a", 0, "GIF image", "image/gif"),
    (b"BM", 0, "BMP image", "image/bmp"),
    (b"II*\x00", 0, "TIFF image", "image/tiff"),
    (b"MM\x00*", 0, "TIFF image", "image/tiff"),
    (b"\x00\x00\x01\x00", 0, "ICO image", "image/x-icon"),
    (b"bplist", 0, "Binary plist", "application/x-bplist"),
    (b"SQLite format 3\x00", 0, "SQLite database", "application/vnd.sqlite3"),
    (b"%PDF", 0, "PDF document", "application/pdf"),
    (b"PK\x03\x04", 0, "ZIP archive", "application/zip"),
    (b"\x1f\x8b", 0, "gzip data", "application/gzip"),
    (b"\x28\xb5\x2f\xfd", 0, "Zstandard data", "application/zstd"),
    (b"BZh", 0, "bzip2 data", "application/x-bzip2"),
    (b"\xfd7zXZ\x00", 0, "XZ data", "application/x-xz"),
    (b"OggS", 0, "Ogg media", "audio/ogg"),
    (b"ID3", 0, "MP3 audio", "audio/mpeg"),
    (b"#!AMR", 0, "AMR audio", "audio/amr"),
    (b"\x30\x82", 0, "DER (ASN.1) data", "application/x-x509-ca-cert"),
]


def detect_blob(data: bytes) -> dict:
    """Identify a blob from its bytes. Returns {"kind", "mime", "image": bool}."""
    if not data:
        return {"kind": "empty", "mime": "", "image": False}
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return {"kind": "WebP image", "mime": "image/webp", "image": True}
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return {"kind": "WAV audio", "mime": "audio/wav", "image": False}
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"heim", b"heis"):
            return {"kind": "HEIC image", "mime": "image/heic", "image": False}
        if brand in (b"avif",):
            return {"kind": "AVIF image", "mime": "image/avif", "image": True}
        if brand.startswith(b"M4A"):
            return {"kind": "M4A audio", "mime": "audio/mp4", "image": False}
        return {"kind": f"MP4/QuickTime ({brand.decode('latin-1').strip()})", "mime": "video/mp4", "image": False}
    for magic, off, kind, mime in SIGNATURES:
        if data[off:off + len(magic)] == magic:
            if kind == "BMP image" and len(data) < 26:
                continue
            if kind == "ICO image" and (len(data) < 6 or data[4:6] == b"\x00\x00"):
                continue
            return {"kind": kind, "mime": mime, "image": mime.startswith("image/") and kind != "TIFF image"}
    head = data[:64].lstrip()
    if head.startswith(b"<?xml") and b"<plist" in data[:400]:
        return {"kind": "XML plist", "mime": "application/xml", "image": False}
    if head[:1] in (b"{", b"[") and _is_json(data):
        return {"kind": "JSON", "mime": "application/json", "image": False}
    if len(data) >= 2 and data[0] == 0x78 and data[1] in (0x01, 0x5E, 0x9C, 0xDA) and (data[0] << 8 | data[1]) % 31 == 0:
        return {"kind": "zlib data", "mime": "application/zlib", "image": False}
    if _is_text(data):
        return {"kind": "Text", "mime": "text/plain", "image": False}
    if protobuf_decode(data, max_depth=1) is not None:
        return {"kind": "Protobuf (probable)", "mime": "application/x-protobuf", "image": False}
    return {"kind": "Binary data", "mime": "application/octet-stream", "image": False}


def _is_json(data: bytes) -> bool:
    try:
        json.loads(data.decode("utf-8"))
        return True
    except (UnicodeDecodeError, ValueError):
        return False


def _is_text(data: bytes) -> bool:
    try:
        text = data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return False
    bad = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\r\n")
    return bad <= len(text) // 50


# ---------------------------------------------------------------- structured decoders
def _jsonable(obj, depth: int = 0):
    """Make plist / protobuf output JSON-serialisable for the UI."""
    if depth > 40:
        return "…"
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v, depth + 1) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        b = bytes(obj)
        return {"$bytes": len(b), "hex": b[:64].hex(" ") + (" …" if len(b) > 64 else "")}
    if isinstance(obj, plistlib.UID):
        return {"$uid": obj.data}
    if isinstance(obj, dt.datetime):
        return format_dt(obj if obj.tzinfo else obj.replace(tzinfo=UTC))
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return str(obj)
    return obj


def resolve_keyed_archive(plist) -> object:
    """Flatten an NSKeyedArchiver plist by following its $objects UID references from $top."""
    if not (isinstance(plist, dict) and plist.get("$archiver") == "NSKeyedArchiver" and "$objects" in plist):
        return plist
    objects = plist["$objects"]

    def resolve(obj, depth=0, seen=frozenset()):
        if depth > 30:
            return "…"
        if isinstance(obj, plistlib.UID):
            idx = obj.data
            if idx in seen or idx >= len(objects):
                return {"$ref": idx}
            target = objects[idx]
            return None if target == "$null" else resolve(target, depth + 1, seen | {idx})
        if isinstance(obj, dict):
            cls = obj.get("$class")
            out = {}
            if "NS.keys" in obj and "NS.objects" in obj:  # NSDictionary
                keys = [resolve(k, depth + 1, seen) for k in obj["NS.keys"]]
                vals = [resolve(v, depth + 1, seen) for v in obj["NS.objects"]]
                return {str(k): v for k, v in zip(keys, vals)}
            if "NS.objects" in obj:  # NSArray / NSSet
                return [resolve(v, depth + 1, seen) for v in obj["NS.objects"]]
            if "NS.string" in obj:
                return obj["NS.string"]
            if "NS.bytes" in obj:
                return obj["NS.bytes"]
            if "NS.time" in obj:  # NSDate: seconds since 2001
                return format_dt(convert_timestamp(obj["NS.time"], "mac_s"))
            for k, v in obj.items():
                if k == "$class":
                    continue
                out[k] = resolve(v, depth + 1, seen)
            if isinstance(cls, plistlib.UID) and cls.data < len(objects):
                meta = objects[cls.data]
                if isinstance(meta, dict) and "$classname" in meta:
                    out = {"$class": meta["$classname"], **out}
            return out
        if isinstance(obj, list):
            return [resolve(v, depth + 1, seen) for v in obj]
        return obj

    top = plist.get("$top", {})
    return {k: resolve(v) for k, v in top.items()} if isinstance(top, dict) else resolve(top)


def protobuf_decode(data: bytes, max_depth: int = 6, _depth: int = 0):
    """Schema-less protobuf decode. Returns a list of {field, wire, value} or None if the bytes aren't a
    well-formed message. Length-delimited fields are shown as a nested message, text or bytes, whichever
    parses."""
    if not data or _depth > max_depth:
        return None
    pos, out = 0, []

    def varint(p):
        result = shift = 0
        while True:
            if p >= len(data) or shift > 63:
                raise ValueError
            b = data[p]
            result |= (b & 0x7F) << shift
            p += 1
            if not b & 0x80:
                return result, p
            shift += 7

    try:
        while pos < len(data):
            key, pos = varint(pos)
            field, wire = key >> 3, key & 7
            if field == 0 or field > 536870911:
                return None
            if wire == 0:
                val, pos = varint(pos)
            elif wire == 1:
                if pos + 8 > len(data):
                    return None
                raw = data[pos:pos + 8]
                val = {"fixed64": struct.unpack("<Q", raw)[0], "double": struct.unpack("<d", raw)[0]}
                pos += 8
            elif wire == 2:
                length, pos = varint(pos)
                if pos + length > len(data):
                    return None
                chunk = data[pos:pos + length]
                pos += length
                nested = protobuf_decode(chunk, max_depth, _depth + 1) if chunk else None
                printable = _is_text(chunk) and not any(b < 32 and b != 9 for b in chunk)
                if chunk and printable:
                    val = chunk.decode("utf-8")
                elif nested is not None:
                    val = nested
                elif chunk and _is_text(chunk):
                    val = chunk.decode("utf-8")
                else:
                    val = {"$bytes": len(chunk), "hex": chunk[:64].hex(" ")}
            elif wire == 5:
                if pos + 4 > len(data):
                    return None
                raw = data[pos:pos + 4]
                val = {"fixed32": struct.unpack("<I", raw)[0], "float": struct.unpack("<f", raw)[0]}
                pos += 4
            else:
                return None
            out.append({"field": field, "wire": wire, "value": val})
    except ValueError:
        return None
    return out or None


def decode_blob(data: bytes, max_image: int = 20 * 1024 * 1024) -> dict:
    """Everything the BLOB viewer shows: the type, a preview (image data URI / decoded structure / text) and
    a hex dump of the start."""
    info = detect_blob(data)
    out = {**info, "size": len(data), "hex": hexdump(data[:4096]), "truncated_hex": len(data) > 4096}
    kind = info["kind"]
    try:
        if info["image"] and len(data) <= max_image:
            out["data_uri"] = f"data:{info['mime']};base64,{base64.b64encode(data).decode()}"
        elif kind in ("Binary plist", "XML plist"):
            pl = plistlib.loads(data)
            out["decoded"] = _jsonable(resolve_keyed_archive(pl))
            out["decoded_label"] = "NSKeyedArchiver (resolved)" if isinstance(pl, dict) and \
                pl.get("$archiver") == "NSKeyedArchiver" else "Property list"
        elif kind == "JSON":
            out["decoded"] = json.loads(data.decode("utf-8"))
            out["decoded_label"] = "JSON"
        elif kind in ("gzip data", "zlib data"):
            inner = gzip.decompress(data) if kind == "gzip data" else zlib.decompress(data)
            inner_info = decode_blob(inner, max_image)
            out["decoded_label"] = f"Decompressed: {inner_info['kind']} ({len(inner):,} bytes)"
            out["inner"] = inner_info
        elif kind == "Text":
            out["text"] = data[:200000].decode("utf-8", "replace")
        elif kind == "Protobuf (probable)":
            out["decoded"] = protobuf_decode(data)
            out["decoded_label"] = "Protobuf (no schema: field numbers only)"
    except (plistlib.InvalidFileException, ValueError, OSError, EOFError, zlib.error, binascii.Error) as exc:
        out["decode_error"] = f"{type(exc).__name__}: {exc}"
    return out


def hexdump(data: bytes, base: int = 0, width: int = 16) -> str:
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + i:08x}  {hexpart:<{width * 3 - 1}}  {asc}")
    return "\n".join(lines)


# ---------------------------------------------------------------- cells for the UI
def cell(value, blob_store=None) -> dict:
    """A JSON-safe description of one value for the data grid. Blobs are kept server-side in blob_store
    (a callable that stores bytes and returns an id) and sent as a short preview."""
    if value is None:
        return {"t": "null"}
    if isinstance(value, bool):
        return {"t": "int", "v": int(value)}
    if isinstance(value, int):
        # JavaScript numbers lose precision past 2^53; send big ones as text too
        return {"t": "int", "v": value, **({"s": str(value)} if abs(value) > 2 ** 53 else {})}
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return {"t": "real", "v": None, "s": str(value)}
        return {"t": "real", "v": value}
    if isinstance(value, str):
        return {"t": "text", "v": value if len(value) <= 2000 else value[:2000], "len": len(value)}
    data = bytes(value)
    info = detect_blob(data)
    out = {"t": "blob", "len": len(data), "kind": info["kind"], "preview": data[:24].hex(" ")}
    if blob_store is not None:
        out["id"] = blob_store(data)
    return out
