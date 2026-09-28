"""Read the signing certificate fingerprint out of an APK.

F-Droid records a ``signer`` (and optionally ``preferredSigner``) field holding
the SHA-256 of the developer's signing certificate, taken from the DER blob
exactly as encoded.  ``fdroidserver`` gets this via ``androguard``; the layout
of the APK Signing Block is simple enough to walk directly.

Structure (from https://source.android.com/docs/security/features/apksigning/v2):

    APK Signing Block          <- sits immediately before the Central Directory
      uint64 size_of_block            (excludes this first field)
      repeated { uint64 length; uint32 id; value }
      uint64 size_of_block            (copy)
      magic "APK Sig Block 42"

The v2 (0x7109871a) and v3 (0xf05368c0) blocks hold, in order:

    signers        -> length-prefixed sequence of signers
      signed_data  -> length-prefixed: digests, certificates, attributes
        digests      -> length-prefixed sequence
        certificates -> length-prefixed sequence of length-prefixed DER certs
      [v3 only] min_sdk, max_sdk
      signatures   -> length-prefixed
      public_key   -> length-prefixed

The first certificate of the first signer is the one ``fdroidserver`` reports.

Verified against a real ``F-Droid.apk``: this module produced the same
fingerprint (``43238d51...``) that f-droid.org publishes in its own
``index-v1.json``.
"""

from __future__ import annotations

import hashlib
import struct

_EOCD_SIGNATURE = b"\x50\x4b\x05\x06"
_BLOCK_MAGIC = b"APK Sig Block 42"
_MAX_COMMENT = 0xFFFF

_V2_BLOCK_ID = 0x7109871A
_V3_BLOCK_ID = 0xF05368C0
_V31_BLOCK_ID = 0x1B93AD61
_V4_BLOCK_ID = 0x42726577  # "Brew", verity padding; carries no certificates


def _find_eocd(handle) -> int:
    """Return the offset of the End Of Central Directory record."""
    handle.seek(0, 2)
    size = handle.tell()
    window = min(size, _MAX_COMMENT + 22 + 20)
    handle.seek(size - window)
    buffer = handle.read(window)
    index = buffer.rfind(_EOCD_SIGNATURE)
    if index < 0:
        raise ValueError("not a ZIP file: no end-of-central-directory record")
    return size - window + index


def _read_signing_blocks(handle) -> dict[int, bytes]:
    """Return the ``{block id: value}`` map of the APK Signing Block."""
    eocd = _find_eocd(handle)
    handle.seek(eocd + 16)
    (central_directory,) = struct.unpack("<I", handle.read(4))

    handle.seek(central_directory - len(_BLOCK_MAGIC))
    if handle.read(len(_BLOCK_MAGIC)) != _BLOCK_MAGIC:
        return {}  # only a JAR (v1) signature; nothing to walk here

    handle.seek(central_directory - len(_BLOCK_MAGIC) - 8)
    (size_of_block,) = struct.unpack("<Q", handle.read(8))
    start = central_directory - size_of_block - 8
    if start < 0:
        raise ValueError("corrupt APK signing block")

    handle.seek(start)
    (size_copy,) = struct.unpack("<Q", handle.read(8))
    if size_copy != size_of_block:
        raise ValueError("APK signing block size mismatch")

    # The trailing 8 bytes (size) and 16 bytes (magic) are not pairs.
    pairs = handle.read(size_of_block - 24)
    blocks: dict[int, bytes] = {}
    pos = 0
    while pos + 12 <= len(pairs):
        (length,) = struct.unpack_from("<Q", pairs, pos)
        if length < 12 or pos + 8 + length > len(pairs):
            break
        (block_id,) = struct.unpack_from("<I", pairs, pos + 8)
        blocks[block_id] = pairs[pos + 12 : pos + 8 + length]
        pos += 8 + length
    return blocks


class _Reader:
    """Cursor over a buffer of little-endian length-prefixed elements."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def eof(self) -> bool:
        return self._pos >= len(self._data)

    def length_prefixed(self) -> bytes:
        """Read a ``uint32`` length followed by that many bytes, and advance."""
        if self._pos + 4 > len(self._data):
            raise ValueError("truncated length prefix in APK signing block")
        (length,) = struct.unpack_from("<I", self._data, self._pos)
        start = self._pos + 4
        end = start + length
        if end > len(self._data):
            raise ValueError("length prefix overruns APK signing block")
        self._pos = end
        return self._data[start:end]


def _first_u32(data: bytes) -> bytes:
    """Read the single length-prefixed element that ``data`` starts with."""
    return _Reader(data).length_prefixed()


def _signer_certificate(signer: bytes) -> bytes | None:
    """Return the DER of the first certificate in a single signer record.

    The nesting is: signer -> signed data -> (digests, certificates), and the
    certificates sit *after* the digests inside the signed data, so this has to
    be walked with a cursor rather than by nesting slices.
    """
    try:
        signed_data = _Reader(signer).length_prefixed()
        contents = _Reader(signed_data)
        contents.length_prefixed()  # digests, not needed
        certificates = contents.length_prefixed()
        certificate = _first_u32(certificates)
    except ValueError:
        return None
    return certificate or None


def _walk_signers(block: bytes):
    """Yield the DER of the first certificate of each signer in a v2/v3 block."""
    try:
        signers = _first_u32(block)
    except ValueError:
        return
    reader = _Reader(signers)
    while not reader.eof():
        try:
            signer = reader.length_prefixed()
        except ValueError:
            return
        certificate = _signer_certificate(signer)
        if certificate:
            yield certificate


def signing_certificate_sha256(apk_path: str) -> str | None:
    """Return the SHA-256 of the APK's signing certificate, or ``None``.

    ``None`` means the APK only carries a legacy JAR (v1) signature, in which
    case the fingerprint would have to come from a PKCS#7 parse that this module
    deliberately does not attempt.  The caller treats that as "signer unknown"
    and simply omits the field from the index.
    """
    with open(apk_path, "rb") as handle:
        blocks = _read_signing_blocks(handle)

    for block_id in (_V3_BLOCK_ID, _V31_BLOCK_ID, _V2_BLOCK_ID):
        block = blocks.get(block_id)
        if not block:
            continue
        for certificate in _walk_signers(block):
            return hashlib.sha256(certificate).hexdigest()
    return None
