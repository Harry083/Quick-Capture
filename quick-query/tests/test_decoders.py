import plistlib
import zlib

from backend import decoders


def test_timestamp_detection():
    d = decoders.detect_timestamp_format
    assert d([1700000000, 1700003600], "created") == "unix_s"
    assert d([1700000000123], "x") == "unix_ms"
    assert d([720000000.5, 720003600.25], "ZDATE") == "mac_s"
    assert d([720000000123456789], "date") == "mac_ns"
    assert d([13340000000000000], "last_visit_time") == "webkit"
    assert d([133400000000000000], "x") == "filetime"
    assert d([1, 2, 3, 4], "id") is None
    assert d(["text"], "date") is None


def test_timestamp_conversion():
    f = decoders.format_dt
    assert f(decoders.convert_timestamp(0, "unix_s")) == "1970-01-01 00:00:00 UTC"
    assert f(decoders.convert_timestamp(0, "mac_s")) == "2001-01-01 00:00:00 UTC"
    assert f(decoders.convert_timestamp(11644473600 * 1000000, "webkit")) == "1970-01-01 00:00:00 UTC"
    assert f(decoders.convert_timestamp(2440587.5, "julian")) == "1970-01-01 00:00:00 UTC"
    readings = {r["format"] for r in decoders.all_timestamp_readings(720000000)}
    assert "mac_s" in readings


def test_blob_types():
    kind = lambda b: decoders.detect_blob(b)["kind"]  # noqa: E731
    assert kind(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20) == "PNG image"
    assert kind(b"\xff\xd8\xff\xe0" + b"\x00" * 20) == "JPEG image"
    assert kind(plistlib.dumps({"a": 1}, fmt=plistlib.FMT_BINARY)) == "Binary plist"
    assert kind(b'{"a": [1, 2]}') == "JSON"
    assert kind(zlib.compress(b"hello" * 10)) == "zlib data"
    assert kind(b"SQLite format 3\x00" + b"\x00" * 84) == "SQLite database"
    assert kind(bytes.fromhex("0a0568656c6c6f1001")) == "Protobuf (probable)"


def test_keyed_archive_resolved():
    archive = {"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": plistlib.UID(1)},
               "$objects": ["$null", {"NS.keys": [plistlib.UID(2)], "NS.objects": [plistlib.UID(3)],
                                      "$class": plistlib.UID(4)}, "sender", "+447700900001",
                            {"$classname": "NSDictionary", "$classes": ["NSDictionary", "NSObject"]}]}
    out = decoders.decode_blob(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY))
    assert out["decoded"] == {"root": {"sender": "+447700900001"}}


def test_compressed_blob_is_unpacked():
    out = decoders.decode_blob(zlib.compress(b'{"lat": 51.5}'))
    assert out["inner"]["kind"] == "JSON" and out["inner"]["decoded"] == {"lat": 51.5}


def test_protobuf_decode():
    msg = decoders.protobuf_decode(bytes.fromhex("0a0568656c6c6f1096011a0f0a0d6e657374656420737472696e67"))
    assert msg[0] == {"field": 1, "wire": 2, "value": "hello"}
    assert msg[1] == {"field": 2, "wire": 0, "value": 150}
    assert msg[2]["value"] == [{"field": 1, "wire": 2, "value": "nested string"}]


def test_cell_big_int_keeps_precision():
    c = decoders.cell(2 ** 60)
    assert c["s"] == str(2 ** 60)
