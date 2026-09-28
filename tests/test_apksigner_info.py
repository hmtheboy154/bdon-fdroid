"""Tests for reading the signing certificate out of an APK.

A synthetic APK carrying a real v2/v3 signing block is assembled here, so the
walk through the nested length-prefixed structures is exercised without
redistributing anyone's APK.  The block is built to the layout documented at
https://source.android.com/docs/security/features/apksigning/v2 and explained
in :mod:`bdon_fdroid.apksigner_info`.
"""

import hashlib
import io
import os
import shutil
import struct
import tempfile
import unittest
import zipfile

from bdon_fdroid import apksigner_info
from bdon_fdroid.apksigner_info import signing_certificate_sha256

V2_BLOCK_ID = 0x7109871A
V3_BLOCK_ID = 0xF05368C0

#: A stand-in for a DER-encoded certificate; only its length framing matters.
FAKE_CERT = b"\x30\x82\x01\x00" + b"\xab" * 256
DIGEST = b"\x11" * 32


def _length_prefixed(payload: bytes) -> bytes:
    """uint32 length prefix, as used for signers, digests, certificates."""
    return struct.pack("<I", len(payload)) + payload


def _pair(block_id: int, value: bytes) -> bytes:
    """One APK Signing Block pair: uint64 length, uint32 id, value.

    Note the pair length is a uint64 even though every other length prefix in
    the format is a uint32.
    """
    return struct.pack("<Q", 4 + len(value)) + struct.pack("<I", block_id) + value


def _signed_data(certificates: bytes) -> bytes:
    """digests, then certificates, then (empty) additional attributes."""
    digest_entry = _length_prefixed(struct.pack("<I", 0x0103) + _length_prefixed(DIGEST))
    digests = _length_prefixed(digest_entry)
    certs = _length_prefixed(certificates)
    return digests + certs + _length_prefixed(b"")


def _signer(certificates: bytes, is_v3: bool = False) -> bytes:
    """A single length-prefixed signer record.

    v3 inserts the min/max SDK levels between the signed data and the
    signatures; v2 goes straight from the signed data to the signatures.
    """
    inner = _length_prefixed(_signed_data(certificates))
    if is_v3:
        inner += struct.pack("<II", 23, 24)
    inner += _length_prefixed(b"")  # signatures
    inner += _length_prefixed(b"\x00" * 32)  # public key
    return _length_prefixed(inner)


def _v2_block(certificates: bytes) -> bytes:
    return _length_prefixed(_signer(certificates, is_v3=False))


def _v3_block(certificates: bytes) -> bytes:
    return _length_prefixed(_signer(certificates, is_v3=True))


def _build_apk(blocks: dict) -> bytes:
    """A zip with an APK Signing Block spliced in before the central directory.

    Inserting the block shifts the central directory, so the offset recorded in
    the End Of Central Directory record has to be rewritten too - otherwise the
    reader would look for the block 24 bytes too far along.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"<binary>")
        archive.writestr("classes.dex", b"dex")
    payload = buffer.getvalue()

    pairs = b""
    for block_id, value in blocks.items():
        pairs += _pair(block_id, value)
    # size_of_block counts everything except the first size field.
    size_of_block = len(pairs) + 8 + len(apksigner_info._BLOCK_MAGIC)
    signing_block = (
        struct.pack("<Q", size_of_block)
        + pairs
        + struct.pack("<Q", size_of_block)
        + apksigner_info._BLOCK_MAGIC
    )

    eocd = payload.rindex(apksigner_info._EOCD_SIGNATURE)
    (central_directory,) = struct.unpack_from("<I", payload, eocd + 16)

    patched = bytearray(payload[:central_directory] + signing_block + payload[central_directory:])
    eocd = patched.rindex(apksigner_info._EOCD_SIGNATURE)
    struct.pack_into("<I", patched, eocd + 16, central_directory + len(signing_block))
    return bytes(patched)


def _write_apk(path: str, blocks: dict) -> None:
    with open(path, "wb") as handle:
        handle.write(_build_apk(blocks))


class _TempApkMixin(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bdon-fdroid-sig-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)


class SigningBlockTests(_TempApkMixin):
    def test_reads_the_v2_certificate(self):
        path = self.path("v2.apk")
        _write_apk(path, {V2_BLOCK_ID: _v2_block(_length_prefixed(FAKE_CERT))})
        self.assertEqual(
            signing_certificate_sha256(path), hashlib.sha256(FAKE_CERT).hexdigest()
        )

    def test_reads_the_v3_certificate(self):
        path = self.path("v3.apk")
        _write_apk(path, {V3_BLOCK_ID: _v3_block(_length_prefixed(FAKE_CERT))})
        self.assertEqual(
            signing_certificate_sha256(path), hashlib.sha256(FAKE_CERT).hexdigest()
        )

    def test_v3_wins_over_v2_when_both_are_present(self):
        # fdroidserver prefers v3, then v2, then the JAR signature.
        other = b"\x30\x82\x00\x40" + b"\xcd" * 64
        path = self.path("both.apk")
        _write_apk(
            path,
            {
                V2_BLOCK_ID: _v2_block(_length_prefixed(other)),
                V3_BLOCK_ID: _v3_block(_length_prefixed(FAKE_CERT)),
            },
        )
        self.assertEqual(
            signing_certificate_sha256(path), hashlib.sha256(FAKE_CERT).hexdigest()
        )

    def test_first_signer_wins(self):
        first, second = FAKE_CERT, b"\x30\x82\x00\x40" + b"\xcd" * 64
        block = _v2_block(_length_prefixed(first) + _length_prefixed(second))
        path = self.path("multi.apk")
        _write_apk(path, {V2_BLOCK_ID: block})
        self.assertEqual(signing_certificate_sha256(path), hashlib.sha256(first).hexdigest())

    def test_verity_padding_is_ignored(self):
        # 0x42726577 is "Brew", the verity padding block; it has no certificate.
        path = self.path("padded.apk")
        _write_apk(
            path,
            {
                0x42726577: b"\x00" * 128,
                V2_BLOCK_ID: _v2_block(_length_prefixed(FAKE_CERT)),
            },
        )
        self.assertEqual(
            signing_certificate_sha256(path), hashlib.sha256(FAKE_CERT).hexdigest()
        )


class GracefulDegradationTests(_TempApkMixin):
    def test_jar_only_apk_reports_no_signer(self):
        # A v1-signed APK has no signing block. Reporting None is correct: the
        # caller then omits the field rather than publishing a wrong hash.
        path = self.path("legacy.apk")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"<binary>")
            archive.writestr("META-INF/CERT.RSA", b"pkcs7-ish")
        with open(path, "wb") as handle:
            handle.write(buffer.getvalue())
        self.assertIsNone(signing_certificate_sha256(path))

    def test_a_file_that_is_not_a_zip_raises(self):
        path = self.path("notazip.apk")
        with open(path, "wb") as handle:
            handle.write(b"this is not an apk at all")
        with self.assertRaises(ValueError):
            signing_certificate_sha256(path)

    def test_a_truncated_block_does_not_produce_a_bogus_fingerprint(self):
        # Better to report "unknown" than to hash a misparsed fragment.
        path = self.path("truncated.apk")
        _write_apk(path, {V2_BLOCK_ID: b"\x05\x00\x00\x00"})
        self.assertIsNone(signing_certificate_sha256(path))


class ReaderTests(unittest.TestCase):
    def test_reader_rejects_an_overrunning_length_prefix(self):
        reader = apksigner_info._Reader(struct.pack("<I", 100) + b"short")
        with self.assertRaises(ValueError):
            reader.length_prefixed()

    def test_reader_advances_past_each_element(self):
        reader = apksigner_info._Reader(
            _length_prefixed(b"first") + _length_prefixed(b"second")
        )
        self.assertEqual(reader.length_prefixed(), b"first")
        self.assertEqual(reader.length_prefixed(), b"second")
        self.assertTrue(reader.eof())


if __name__ == "__main__":
    unittest.main()
