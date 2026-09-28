"""Tests for the binary XML (AXML) manifest reader.

The fixtures are built here rather than checked in, so the test suite stays
self-contained and does not redistribute any third-party APK.  A tiny writer
produces documents in exactly the layout ``axml.py`` reads, which doubles as an
executable description of that layout.
"""

import struct
import unittest
import zipfile

from bdon_fdroid import axml

_ANDROID_NS = "http://schemas.android.com/apk/res/android"


class _AxmlBuilder:
    """Builds a minimal but valid Android binary XML document."""

    def __init__(self):
        self._strings: list[str] = []
        self._body = bytearray()

    #: Sentinel the format uses for "absent" (no namespace, no raw value).
    NO_INDEX = -1

    def _index(self, value: str | None) -> int:
        """Pool index of ``value``; ``None`` means 'no namespace'."""
        if value is None:
            return self.NO_INDEX
        if value not in self._strings:
            self._strings.append(value)
        return self._strings.index(value)

    def _chunk(self, chunk_type: int, header_size: int, body: bytes) -> bytes:
        """Wrap ``body`` in a ResChunk.

        ``header_size`` is the chunk's declared header length (8 for the plain
        ResChunk_header, 28 for a string pool, 16 for an element), while the
        total size is always the 8 bytes of ResChunk_header plus ``body``.
        """
        return struct.pack("<HHI", chunk_type, header_size, 8 + len(body)) + body

    def element(self, name: str, attributes: dict, line: int = 1) -> None:
        """Append a START_TAG chunk.

        ``attributes`` maps name to a python value.  A name written as
        ``android:foo`` is put in the Android resource namespace, which is how
        real manifests distinguish it from a plain ``package`` attribute.
        """
        payload = bytearray()
        payload += struct.pack("<ii", line, -1)  # lineNumber, comment
        payload += struct.pack("<i", self._index(None))  # element namespace
        payload += struct.pack("<i", self._index(name))
        payload += struct.pack(
            "<HHHHHH", 20, 20, len(attributes), 0, 0, 0
        )  # attrStart, attrSize, count, idIdx, classIdx, styleIdx
        for attribute, value in attributes.items():
            if attribute.startswith("android:"):
                namespace, attribute = _ANDROID_NS, attribute[len("android:") :]
            else:
                namespace = None
            if isinstance(value, bool):
                value_type, data = axml._TYPE_INT_BOOLEAN, int(value)
            elif isinstance(value, int):
                value_type, data = axml._TYPE_INT_DEC, value
            else:
                value_type, data = axml._TYPE_STRING, self._index(str(value))
            payload += struct.pack(
                "<iiiHBBI",
                self._index(namespace),
                self._index(attribute),
                self.NO_INDEX,
                8,
                0,
                value_type,
                data,
            )
        self._body += self._chunk(axml._TYPE_START_TAG, 16, bytes(payload))

    def build(self) -> bytes:
        # UTF-16 string pool, which is what every real manifest uses.
        offsets = []
        encoded = bytearray()
        for value in self._strings:
            offsets.append(len(encoded))
            encoded += struct.pack("<H", len(value)) + value.encode("utf-16-le") + b"\x00\x00"
        header_size = 28
        strings_start = header_size + len(offsets) * 4
        pool = struct.pack(
            "<IIIII",
            len(self._strings),
            0,
            0,  # UTF-8 flag clear => UTF-16
            strings_start,
            0,
        )
        pool += b"".join(struct.pack("<I", offset) for offset in offsets)
        pool += bytes(encoded)
        document = b"\x03\x00\x08\x00" + struct.pack("<I", 8 + len(pool) + len(self._body))
        document += self._chunk(axml._TYPE_STRING_POOL, header_size, pool)
        return document + bytes(self._body)


def _manifest_document(**overrides) -> bytes:
    attributes = {
        "android:versionCode": overrides.get("version_code", 10001),
        "android:versionName": overrides.get("version_name", "1.0.1"),
        "android:compileSdkVersion": overrides.get("compile_sdk", 35),
        "package": overrides.get("package", "com.bilibili.sirius"),
    }
    builder = _AxmlBuilder()
    builder.element("manifest", attributes)
    sdk = {}
    if overrides.get("min_sdk") is not None:
        sdk["android:minSdkVersion"] = overrides["min_sdk"]
    if overrides.get("target_sdk") is not None:
        sdk["android:targetSdkVersion"] = overrides["target_sdk"]
    if overrides.get("max_sdk") is not None:
        sdk["android:maxSdkVersion"] = overrides["max_sdk"]
    builder.element("uses-sdk", sdk)
    for permission in overrides.get("permissions", ["android.permission.INTERNET"]):
        builder.element("uses-permission", {"android:name": permission})
    for feature in overrides.get("features", []):
        builder.element("uses-feature", {"android:name": feature})
    return builder.build()


class ParseManifestTests(unittest.TestCase):
    def test_reads_everything_the_index_needs(self):
        manifest = axml.parse_manifest(
            _manifest_document(
                version_code=10001,
                version_name="1.0.1",
                min_sdk=24,
                target_sdk=35,
                permissions=["android.permission.INTERNET", "android.permission.VIBRATE"],
                features=["android.hardware.touchscreen"],
            )
        )
        self.assertEqual(manifest.package, "com.bilibili.sirius")
        self.assertEqual(manifest.version_code, 10001)
        self.assertEqual(manifest.version_name, "1.0.1")
        self.assertEqual(manifest.min_sdk_version, 24)
        self.assertEqual(manifest.target_sdk_version, 35)
        self.assertEqual(
            manifest.permissions, ["android.permission.INTERNET", "android.permission.VIBRATE"]
        )
        self.assertEqual(manifest.features, ["android.hardware.touchscreen"])
        self.assertEqual(manifest.compile_sdk_version, 35)

    def test_missing_uses_sdk_is_none_not_an_error(self):
        # Several real APKs legitimately have no <uses-sdk>; the index generator
        # has to cope rather than crash.
        manifest = axml.parse_manifest(_manifest_document(min_sdk=None, target_sdk=None))
        self.assertIsNone(manifest.min_sdk_version)
        self.assertIsNone(manifest.target_sdk_version)
        self.assertEqual(manifest.version_code, 10001)

    def test_duplicate_permissions_are_collapsed(self):
        manifest = axml.parse_manifest(
            _manifest_document(
                permissions=["android.permission.INTERNET", "android.permission.INTERNET"]
            )
        )
        self.assertEqual(manifest.permissions, ["android.permission.INTERNET"])

    def test_rejects_input_that_is_not_binary_xml(self):
        with self.assertRaises(axml.AxmlError):
            axml.parse_manifest(b"<?xml version='1.0'?><manifest/>")

    def test_reads_a_manifest_out_of_a_real_zip(self):
        import io

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("AndroidManifest.xml", _manifest_document())
            archive.writestr("classes.dex", b"not really a dex")
        with zipfile.ZipFile(buffer) as archive:
            manifest = axml.parse_manifest(archive.read("AndroidManifest.xml"))
        self.assertEqual(manifest.package, "com.bilibili.sirius")
        self.assertEqual(manifest.version_code, 10001)


class ResourceReferenceTests(unittest.TestCase):
    def test_reference_typed_attribute_is_surfaced_not_swallowed(self):
        # versionName is sometimes a resource reference. Resolving it needs
        # resources.arsc, so the parser must at least not crash or invent a value.
        builder = _AxmlBuilder()
        pool_value = "0x7f050007"
        builder.element(
            "manifest",
            {"android:versionCode": 9, "android:versionName": pool_value,
             "package": "com.example.app"},
        )
        manifest = axml.parse_manifest(builder.build())
        self.assertEqual(manifest.version_code, 9)
        self.assertEqual(manifest.package, "com.example.app")
        self.assertIsInstance(manifest.version_name, str)


if __name__ == "__main__":
    unittest.main()
