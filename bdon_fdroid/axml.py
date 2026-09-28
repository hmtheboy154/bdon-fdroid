"""A minimal reader for Android's binary XML format (AXML).

``AndroidManifest.xml`` inside an APK is not plain XML; it is a chunked binary
format.  Pulling in ``androguard`` (what ``fdroidserver`` uses) just to read a
handful of attributes would add a heavy dependency, so this module implements
the small subset we need:

* the string pool and the attribute values we care about,
* ``manifest`` / ``uses-sdk`` / ``uses-permission`` / ``uses-feature`` elements.

Only the ``ResChunk`` types that appear in a manifest are handled; anything else
is skipped, so unknown chunks are tolerated rather than fatal.

Verified against the APKs in ``fdroidserver/tests/repo`` - the extracted values
match the fixtures in that project's ``index-v1.json``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# ResChunk_header types
_TYPE_STRING_POOL = 0x0001
_TYPE_START_TAG = 0x0102

# Res_value data types we decode; the rest are surfaced as their raw integer.
_TYPE_REFERENCE = 0x01
_TYPE_ATTRIBUTE = 0x02
_TYPE_STRING = 0x03
_TYPE_FLOAT = 0x04
_TYPE_DIMENSION = 0x05
_TYPE_TYPE_FRACTION = 0x06
_TYPE_INT_DEC = 0x10
_TYPE_INT_HEX = 0x11
_TYPE_INT_BOOLEAN = 0x12

_ANDROID_NS = "http://schemas.android.com/apk/res/android"


class AxmlError(ValueError):
    """Raised when the input is not a well-formed binary XML document."""


def _uleb128(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a UTF-8 length prefix (used by UTF-8 string pools)."""
    value = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


class _StringPool:
    """The string pool chunk, shared by every element in the document."""

    def __init__(self, data: bytes, offset: int) -> None:
        (
            count,
            style_count,
            flags,
            strings_start,
            _styles_start,
        ) = struct.unpack_from("<IIIII", data, offset + 8)
        if count > 0x100000:  # sanity guard against corrupt input
            raise AxmlError("implausible string pool")
        self._is_utf8 = bool(flags & (1 << 8))
        self.strings: list[str] = []
        if count == 0:
            return

        offsets = struct.unpack_from("<%dI" % count, data, offset + 28)
        base = offset + strings_start
        for relative in offsets:
            self.strings.append(self._read_string(data, base + relative))

    def _read_string(self, data: bytes, pos: int) -> str:
        if self._is_utf8:
            # Two length prefixes (UTF-16 length, then UTF-8 byte length).
            _utf16_len, pos = _uleb128(data, pos)
            _utf8_len, pos = _uleb128(data, pos)
            end = pos
            while data[end] & 0x80:
                end += 1
            return data[pos : end + 1].decode("utf-8", "replace")
        length = struct.unpack_from("<H", data, pos)[0]
        return data[pos + 2 : pos + 2 + length * 2].decode("utf-16-le", "replace")

    def get(self, index: int) -> str:
        if index < 0 or index >= len(self.strings):
            return ""
        return self.strings[index]


@dataclass
class Element:
    """A single ``<tag>`` with its attributes flattened to plain values."""

    name: str
    attributes: dict[str, object] = field(default_factory=dict)

    def android(self, attribute: str, default=None):
        """Look up ``android:<attribute>``."""
        return self.attributes.get(f"{{{_ANDROID_NS}}}{attribute}", default)

    def get(self, attribute: str, default=None):
        """Look up an attribute with no namespace."""
        return self.attributes.get(attribute, default)


@dataclass
class Manifest:
    """The subset of manifest data the F-Droid index needs."""

    package: str
    version_code: int | None
    version_name: str | None
    min_sdk_version: int | None
    target_sdk_version: int | None
    max_sdk_version: int | None
    permissions: list[str]
    features: list[str]
    compile_sdk_version: int | None
    native_code: list[str]

    def as_dict(self) -> dict:
        return {
            "package": self.package,
            "versionCode": self.version_code,
            "versionName": self.version_name,
            "minSdkVersion": self.min_sdk_version,
            "targetSdkVersion": self.target_sdk_version,
            "maxSdkVersion": self.max_sdk_version,
            "permissions": self.permissions,
            "features": self.features,
            "compileSdkVersion": self.compile_sdk_version,
            "nativeCode": self.native_code,
        }


def _is_disabled(value) -> bool:
    """True when an ``android:required`` style attribute is switched off.

    AXML encodes these either as the string "false" or as a typed boolean, so
    both have to be recognised.
    """
    if value is False:
        return True
    return isinstance(value, str) and value.strip().lower() == "false"


def _attribute_key(pool: _StringPool, ns_index: int, name_index: int) -> str:
    """Build a collision-free key for an attribute.

    Namespaced attributes become ``{namespace}name`` so that ``android:icon``
    and a plain ``icon`` can coexist, mirroring how the Android tooling refers
    to them.
    """
    namespace = pool.get(ns_index)
    name = pool.get(name_index)
    if namespace and namespace != name:
        return f"{{{namespace}}}{name}"
    return name or namespace


def _decode_value(pool: _StringPool, raw_index: int, value_type: int, data: int):
    if value_type == _TYPE_STRING:
        return pool.get(data)
    if value_type == _TYPE_INT_BOOLEAN:
        return bool(data)
    if value_type in (_TYPE_INT_DEC, _TYPE_INT_HEX):
        return data
    if value_type in (_TYPE_REFERENCE, _TYPE_ATTRIBUTE):
        # Resource references cannot be resolved without resources.arsc, but the
        # name is still useful for diagnostics (e.g. android:icon).
        return "@%s0x%08x" % (pool.get(raw_index) or "?", data)
    if value_type in (_TYPE_FLOAT, _TYPE_DIMENSION, _TYPE_TYPE_FRACTION):
        return data
    return data


def iter_elements(data: bytes):
    """Yield every start tag in a binary XML document as an :class:`Element`."""
    if len(data) < 8 or data[:4] != b"\x03\x00\x08\x00":
        raise AxmlError("not an Android binary XML document")

    pool: _StringPool | None = None
    offset = 8
    end = len(data)

    while offset + 8 <= end:
        chunk_type, header_size, chunk_size = struct.unpack_from("<HHI", data, offset)
        if chunk_size < 8 or offset + chunk_size > end:
            break
        if header_size < 8:
            header_size = 8
        # The body starts at the chunk's own header, which is larger than the
        # 8-byte ResChunk_header for element chunks (it also holds the node's
        # line number and comment).
        body = data[offset + header_size : offset + chunk_size]

        if chunk_type == _TYPE_STRING_POOL:
            if pool is None:
                pool = _StringPool(data, offset)
        elif chunk_type == _TYPE_START_TAG and pool is not None:
            name_index = struct.unpack_from("<i", body, 4)[0]
            (
                _attr_start,
                _attr_size,
                attr_count,
            ) = struct.unpack_from("<HHH", body, 8)
            element = Element(name=pool.get(name_index))
            pos = 20
            for _ in range(attr_count):
                if pos + 20 > len(body):
                    break
                (
                    ns_index,
                    attr_name_index,
                    raw_index,
                    _size,
                    _res0,
                    value_type,
                    value,
                ) = struct.unpack_from("<iiiHBBI", body, pos)
                pos += 20
                key = _attribute_key(pool, ns_index, attr_name_index)
                element.attributes[key] = _decode_value(
                    pool, raw_index, value_type, value
                )
            yield element

        offset += chunk_size


def parse_manifest(data: bytes) -> Manifest:
    """Extract the fields the F-Droid index cares about from a manifest blob."""
    package = ""
    version_code = None
    version_name = None
    min_sdk = None
    target_sdk = None
    max_sdk = None
    compile_sdk = None
    permissions: list[str] = []
    features: list[str] = []
    native_code: list[str] = []

    for element in iter_elements(data):
        if element.name == "manifest":
            package = element.get("package", "") or ""
            raw_code = element.android("versionCode")
            if isinstance(raw_code, int):
                version_code = raw_code
            raw_name = element.android("versionName")
            if isinstance(raw_name, str):
                version_name = raw_name
            raw_compile = element.android("compileSdkVersion")
            if isinstance(raw_compile, int):
                compile_sdk = raw_compile
        elif element.name == "uses-sdk":
            if isinstance(element.android("minSdkVersion"), int):
                min_sdk = element.android("minSdkVersion")
            if isinstance(element.android("targetSdkVersion"), int):
                target_sdk = element.android("targetSdkVersion")
            if isinstance(element.android("maxSdkVersion"), int):
                max_sdk = element.android("maxSdkVersion")
        elif element.name == "uses-permission":
            name = element.android("name")
            if isinstance(name, str) and name not in permissions:
                permissions.append(name)
        elif element.name == "uses-feature":
            # Match fdroidserver exactly (fdroidserver/update.py): only a
            # *named* feature that is *required* belongs in the index.
            #
            # A feature declared only as android:glEsVersion has no name, so it
            # is skipped - androguard.get_features() never reports it, which is
            # why emitting "glEsVersion196608" would diverge from every real
            # F-Droid repository. Listing it also makes clients treat a GLES
            # level as a hard requirement and refuse to install on devices
            # that support the game perfectly well.
            name = element.android("name")
            if not isinstance(name, str) or not name:
                continue
            if _is_disabled(element.android("required")):
                continue
            if name.startswith("android.feature."):
                # Legacy spelling, still emitted by old build tools.
                name = name[len("android.feature.") :]
            if name not in features:
                features.append(name)
        elif element.name == "application":
            raw_native = element.android("extractNativeLibs")
            del raw_native  # not part of the index
        elif element.name == "uses-native-library":
            name = element.get("name")
            if isinstance(name, str) and name.startswith("lib") and name not in native_code:
                native_code.append(name)

    return Manifest(
        package=package,
        version_code=version_code,
        version_name=version_name,
        min_sdk_version=min_sdk,
        target_sdk_version=target_sdk,
        max_sdk_version=max_sdk,
        permissions=permissions,
        features=features,
        compile_sdk_version=compile_sdk,
        native_code=native_code,
    )
