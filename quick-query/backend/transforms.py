"""Decoders for data embedded in other data: each transform takes bytes and returns bytes (or raises).

The BLOB viewer chains them, so a Base64 string holding a zlib stream holding a plist can be unwrapped one
step at a time. Everything is pure Python (Brotli only if the optional `brotli` module is installed).
"""
from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import math
import re
import struct
import urllib.parse
import zlib


class TransformError(Exception):
    pass


# ---------------------------------------------------------------- text encodings
def b64(data: bytes) -> bytes:
    text = re.sub(rb"\s+", b"", data)
    text = text.replace(b"-", b"+").replace(b"_", b"/")  # accept URL-safe Base64 too
    try:
        return base64.b64decode(text + b"=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TransformError(f"not Base64: {exc}") from exc


def hex_text(data: bytes) -> bytes:
    text = re.sub(rb"(0x|\\x|[\s:,-])", b"", data)
    try:
        return bytes.fromhex(text.decode("ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TransformError("not hex text") from exc


def url_decode(data: bytes) -> bytes:
    return urllib.parse.unquote_to_bytes(data.replace(b"+", b" "))


def utf16(data: bytes) -> bytes:
    for enc in ("utf-16-le", "utf-16-be") if not data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-16",):
        try:
            return data.decode(enc).encode("utf-8")
        except UnicodeDecodeError:
            continue
    raise TransformError("not UTF-16 text")


def xor_ff(data: bytes) -> bytes:
    return bytes(b ^ 0xFF for b in data)


# ---------------------------------------------------------------- compression
def zlib_t(data: bytes) -> bytes:
    try:
        return zlib.decompress(data)
    except zlib.error as exc:
        raise TransformError(f"zlib: {exc}") from exc


def deflate_raw(data: bytes) -> bytes:
    try:
        return zlib.decompress(data, -15)
    except zlib.error as exc:
        raise TransformError(f"deflate: {exc}") from exc


def gzip_t(data: bytes) -> bytes:
    try:
        return gzip.decompress(data)
    except (OSError, EOFError) as exc:
        raise TransformError(f"gzip: {exc}") from exc


def brotli_t(data: bytes) -> bytes:
    try:
        import brotli  # type: ignore
    except ImportError as exc:
        raise TransformError("Brotli needs the optional 'brotli' package (pip install brotli)") from exc
    try:
        return brotli.decompress(data)
    except Exception as exc:  # noqa: BLE001
        raise TransformError(f"brotli: {exc}") from exc


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise TransformError("truncated varint")
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not b & 0x80:
            return result, pos
        shift += 7


def snappy_raw(data: bytes) -> bytes:
    """Snappy block format (used by Chromium's LevelDB, among others)."""
    length, pos = _varint(data, 0)
    out = bytearray()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:  # literal
            n = tag >> 2
            if n >= 60:
                extra = n - 59
                n = int.from_bytes(data[pos:pos + extra], "little")
                pos += extra
            n += 1
            out += data[pos:pos + n]
            pos += n
            continue
        if kind == 1:
            n = ((tag >> 2) & 7) + 4
            off = ((tag >> 5) << 8) | data[pos]
            pos += 1
        elif kind == 2:
            n = (tag >> 2) + 1
            off = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
        else:
            n = (tag >> 2) + 1
            off = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        if off == 0 or off > len(out):
            raise TransformError("snappy: bad back-reference")
        for _ in range(n):
            out.append(out[-off])
    if len(out) != length:
        raise TransformError(f"snappy: expected {length} bytes, got {len(out)}")
    return bytes(out)


def snappy(data: bytes) -> bytes:
    """Snappy, raw or framed ('sNaPpY' stream)."""
    if data[:10] != b"\xff\x06\x00\x00sNaPpY":
        return snappy_raw(data)
    out, pos = bytearray(), 10
    while pos + 4 <= len(data):
        kind, size = data[pos], int.from_bytes(data[pos + 1:pos + 4], "little")
        body = data[pos + 4:pos + 4 + size]
        pos += 4 + size
        if kind == 0x00:
            out += snappy_raw(body[4:])
        elif kind == 0x01:
            out += body[4:]
    return bytes(out)


def lz4_block(data: bytes, expected: int | None = None) -> bytes:
    out = bytearray()
    pos = 0
    while pos < len(data):
        token = data[pos]
        pos += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = data[pos]
                pos += 1
                lit += b
                if b != 255:
                    break
        out += data[pos:pos + lit]
        pos += lit
        if pos >= len(data):
            break
        off = int.from_bytes(data[pos:pos + 2], "little")
        pos += 2
        if off == 0 or off > len(out):
            raise TransformError("lz4: bad offset")
        n = token & 15
        if n == 15:
            while True:
                b = data[pos]
                pos += 1
                n += b
                if b != 255:
                    break
        n += 4
        for _ in range(n):
            out.append(out[-off])
    if expected is not None and len(out) != expected:
        raise TransformError(f"lz4: expected {expected} bytes, got {len(out)}")
    return bytes(out)


def lz4(data: bytes) -> bytes:
    """Mozilla jsonlz4 / mozLz4 (Firefox session and bookmark backups), the LZ4 frame format, or a raw block."""
    try:
        if data[:8] == b"mozLz40\x00":
            return lz4_block(data[12:], struct.unpack("<I", data[8:12])[0])
        if data[:4] == b"\x04\x22\x4d\x18":
            flg = data[4]
            pos = 6 + (8 if flg & 0x08 else 0) + (4 if flg & 0x01 else 0) + 1
            out = bytearray()
            while pos + 4 <= len(data):
                size = struct.unpack("<I", data[pos:pos + 4])[0]
                pos += 4
                if size == 0:
                    break
                raw, size = size & 0x80000000, size & 0x7FFFFFFF
                block = data[pos:pos + size]
                pos += size + (4 if flg & 0x10 else 0)
                out += block if raw else lz4_block(block)
            return bytes(out)
        return lz4_block(data)
    except IndexError as exc:
        raise TransformError("lz4: truncated") from exc


# ---------------------------------------------------------------- serialisations (bytes -> JSON text)
def _to_json(obj) -> bytes:
    def default(o):
        if isinstance(o, (bytes, bytearray)):
            try:
                s = bytes(o).decode("utf-8")
                if all(ord(c) >= 32 or c in "\t\r\n" for c in s):
                    return s
            except UnicodeDecodeError:
                pass
            return {"$bytes": len(o), "hex": bytes(o)[:256].hex(" ")}
        return str(o)

    return json.dumps(obj, indent=2, default=default, ensure_ascii=False).encode("utf-8")


def msgpack_decode(data: bytes):
    pos = 0

    def take(n):
        nonlocal pos
        if pos + n > len(data):
            raise TransformError("msgpack: truncated")
        chunk = data[pos:pos + n]
        pos += n
        return chunk

    def u(n):
        return int.from_bytes(take(n), "big")

    def s(n):
        return int.from_bytes(take(n), "big", signed=True)

    def read(depth=0):
        if depth > 100:
            raise TransformError("msgpack: too deep")
        b = take(1)[0]
        if b <= 0x7F:
            return b
        if b >= 0xE0:
            return b - 0x100
        if 0x80 <= b <= 0x8F:
            return {str(read(depth + 1)): read(depth + 1) for _ in range(b & 0x0F)}
        if 0x90 <= b <= 0x9F:
            return [read(depth + 1) for _ in range(b & 0x0F)]
        if 0xA0 <= b <= 0xBF:
            return take(b & 0x1F).decode("utf-8", "replace")
        simple = {0xC0: None, 0xC2: False, 0xC3: True}
        if b in simple:
            return simple[b]
        if b in (0xC4, 0xC5, 0xC6):
            return take(u({0xC4: 1, 0xC5: 2, 0xC6: 4}[b]))
        if b in (0xC7, 0xC8, 0xC9):
            n = u({0xC7: 1, 0xC8: 2, 0xC9: 4}[b])
            return {"$ext": s(1), "data": take(n)}
        if b == 0xCA:
            return struct.unpack(">f", take(4))[0]
        if b == 0xCB:
            return struct.unpack(">d", take(8))[0]
        if 0xCC <= b <= 0xCF:
            return u(1 << (b - 0xCC))
        if 0xD0 <= b <= 0xD3:
            return s(1 << (b - 0xD0))
        if 0xD4 <= b <= 0xD8:
            ext = s(1)
            return {"$ext": ext, "data": take(1 << (b - 0xD4))}
        if b in (0xD9, 0xDA, 0xDB):
            return take(u({0xD9: 1, 0xDA: 2, 0xDB: 4}[b])).decode("utf-8", "replace")
        if b in (0xDC, 0xDD):
            return [read(depth + 1) for _ in range(u(2 if b == 0xDC else 4))]
        if b in (0xDE, 0xDF):
            return {str(read(depth + 1)): read(depth + 1) for _ in range(u(2 if b == 0xDE else 4))}
        raise TransformError(f"msgpack: bad byte 0x{b:02x}")

    value = read()
    if pos != len(data):
        raise TransformError(f"msgpack: {len(data) - pos} trailing bytes")
    return value


def bencode_decode(data: bytes):
    pos = 0

    def read(depth=0):
        nonlocal pos
        if depth > 100 or pos >= len(data):
            raise TransformError("bencode: truncated")
        c = data[pos:pos + 1]
        if c == b"i":
            end = data.index(b"e", pos)
            val = int(data[pos + 1:end])
            pos = end + 1
            return val
        if c in (b"l", b"d"):
            pos += 1
            items = []
            while data[pos:pos + 1] != b"e":
                items.append(read(depth + 1))
            pos += 1
            if c == b"l":
                return items
            return {(k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)): v
                    for k, v in zip(items[::2], items[1::2])}
        if c.isdigit():
            colon = data.index(b":", pos)
            n = int(data[pos:colon])
            pos = colon + 1 + n
            return data[colon + 1:pos]
        raise TransformError("bencode: bad token")

    try:
        value = read()
    except (ValueError, IndexError) as exc:
        raise TransformError(f"bencode: {exc}") from exc
    if pos != len(data):
        raise TransformError("bencode: trailing bytes")
    return value


def msgpack_t(data: bytes) -> bytes:
    return _to_json(msgpack_decode(data))


def bencode_t(data: bytes) -> bytes:
    return _to_json(bencode_decode(data))


def plist_t(data: bytes) -> bytes:
    import plistlib

    from .decoders import _jsonable, resolve_keyed_archive

    try:
        return _to_json(_jsonable(resolve_keyed_archive(plistlib.loads(data))))
    except Exception as exc:  # noqa: BLE001
        raise TransformError(f"plist: {exc}") from exc


def protobuf_t(data: bytes) -> bytes:
    from .decoders import protobuf_decode

    msg = protobuf_decode(data)
    if msg is None:
        raise TransformError("not a protobuf message")
    return _to_json(msg)


def strings_t(data: bytes) -> bytes:
    return "\n".join(f"{off:08x}  {s}" for off, s in strings(data)).encode("utf-8")


# ---------------------------------------------------------------- analysis
def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in counts if c)


def hashes(data: bytes) -> dict:
    return {name: hashlib.new(name, data).hexdigest() for name in ("md5", "sha1", "sha256")}


def strings(data: bytes, min_len: int = 4, limit: int = 5000) -> list[tuple[int, str]]:
    """ASCII and UTF-16LE strings with their offsets, like the Unix `strings` tool."""
    out = [(m.start(), m.group().decode("ascii")) for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data)]
    out += [(m.start(), m.group().decode("utf-16-le")) for m in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % min_len, data)]
    return sorted(out)[:limit]


TRANSFORMS = {
    "base64": ("Base64", b64),
    "hex": ("Hex text", hex_text),
    "url": ("URL-encoded", url_decode),
    "utf16": ("UTF-16 text", utf16),
    "zlib": ("zlib", zlib_t),
    "deflate": ("Raw deflate", deflate_raw),
    "gzip": ("GZip", gzip_t),
    "snappy": ("Snappy", snappy),
    "lz4": ("LZ4 / Mozilla jsonlz4", lz4),
    "brotli": ("Brotli", brotli_t),
    "plist": ("Plist", plist_t),
    "protobuf": ("Protobuf", protobuf_t),
    "msgpack": ("MessagePack", msgpack_t),
    "bencode": ("Bencode", bencode_t),
    "xor_ff": ("XOR 0xFF", xor_ff),
    "strings": ("Strings", strings_t),
}


def apply_chain(data: bytes, chain: list[str]) -> bytes:
    for i, name in enumerate(chain):
        if name not in TRANSFORMS:
            raise TransformError(f"unknown transform: {name}")
        try:
            data = TRANSFORMS[name][1](data)
        except TransformError as exc:
            raise TransformError(f"step {i + 1} ({TRANSFORMS[name][0]}): {exc}") from exc
        except Exception as exc:  # noqa: BLE001  (malformed input must never crash the viewer)
            raise TransformError(f"step {i + 1} ({TRANSFORMS[name][0]}): {type(exc).__name__}: {exc}") from exc
    return data


def suggest(data: bytes) -> list[str]:
    """Transforms worth offering for these bytes: the ones that succeed and produce something different."""
    head = data[:65536]
    out = []
    stripped = head.strip()
    if stripped and re.fullmatch(rb"[A-Za-z0-9+/_\-\s]+=*", stripped) and len(stripped) >= 8:
        out.append("base64")
    if stripped and re.fullmatch(rb"(0x)?[0-9a-fA-F\s:]+", stripped) and len(re.sub(rb"[\s:]", b"", stripped)) % 2 == 0 \
            and len(stripped) >= 8:
        out.append("hex")
    if b"%" in head and re.search(rb"%[0-9a-fA-F]{2}", head):
        out.append("url")
    if len(head) >= 4 and head[1:2] == b"\x00" and head[3:4] == b"\x00":
        out.append("utf16")
    for name, test in (("zlib", lambda d: d[:1] == b"\x78"), ("gzip", lambda d: d[:2] == b"\x1f\x8b"),
                       ("lz4", lambda d: d[:8] == b"mozLz40\x00" or d[:4] == b"\x04\x22\x4d\x18"),
                       ("snappy", lambda d: d[:10] == b"\xff\x06\x00\x00sNaPpY"),
                       ("plist", lambda d: d[:6] == b"bplist" or b"<plist" in d[:400])):
        if test(head):
            out.append(name)
    for name in ("deflate", "snappy", "msgpack", "bencode", "protobuf"):
        if name in out or len(data) > 4 * 1024 * 1024:
            continue
        try:
            result = TRANSFORMS[name][1](data)
        except Exception:  # noqa: BLE001
            continue
        if result and result != data and (name != "protobuf" or len(data) >= 4):
            if name in ("msgpack", "bencode") and not data[:1] in (b"\x80", b"\x90", b"\xdc", b"\xde", b"d", b"l") \
                    and not 0x80 <= data[0] <= 0x9F:
                continue  # a lone scalar "decodes" from almost anything
            out.append(name)
    return out
