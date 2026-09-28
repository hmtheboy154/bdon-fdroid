"""Tests for JAR signing.

Signing needs a JDK, so these tests skip themselves when ``jarsigner`` is
absent.  A throwaway keystore is generated per test run: nothing here touches a
real repository key.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from bdon_fdroid import indexgen, signing

KEYSTORE_PASSWORD = "unit-test-password"


def _jarsigner_available() -> bool:
    return shutil.which("jarsigner") is not None and shutil.which("keytool") is not None


@unittest.skipUnless(_jarsigner_available(), "a JDK (keytool/jarsigner) is required")
class SigningTests(unittest.TestCase):
    keystore: str
    alias = "bdon-fdroid-test"

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="bdon-fdroid-sign-")
        cls.keystore = os.path.join(cls.dir, "keystore.p12")
        result = subprocess.run(
            [
                "keytool", "-genkeypair", "-noprompt",
                "-alias", cls.alias,
                "-keyalg", "RSA", "-keysize", "2048",
                "-validity", "2",
                "-dname", "CN=bdon-fdroid tests, O=Tests, C=US",
                "-keystore", cls.keystore,
                "-storetype", "PKCS12",
                "-storepass", KEYSTORE_PASSWORD,
                "-keypass", KEYSTORE_PASSWORD,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            shutil.rmtree(cls.dir, ignore_errors=True)
            raise unittest.SkipTest(f"could not create a test keystore: {result.stderr.strip()}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="bdon-fdroid-jars-")
        self.addCleanup(shutil.rmtree, self.out, True)
        self.keystore_obj = signing.KeyStore(self.keystore, self.alias, KEYSTORE_PASSWORD)

    def test_fingerprint_is_64_hex_characters(self):
        fingerprint = self.keystore_obj.fingerprint()
        self.assertEqual(len(fingerprint), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in fingerprint), fingerprint)

    def test_build_jar_holds_exactly_one_payload(self):
        path = signing.build_jar("index-v2.json", b'{"a":1}', os.path.join(self.out, "x.jar"))
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            self.assertEqual(names, ["index-v2.json"])
            self.assertEqual(archive.read("index-v2.json"), b'{"a":1}')

    def test_jar_bytes_are_reproducible(self):
        # A stable timestamp keeps an unchanged run from producing a diff.
        first = signing.build_jar("a.json", b"x", os.path.join(self.out, "1.jar"))
        second = signing.build_jar("a.json", b"x", os.path.join(self.out, "2.jar"))
        with open(first, "rb") as a, open(second, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_every_index_file_becomes_a_signed_jar(self):
        payloads = {
            "entry.json": b'{"index":{}}',
            "index-v1.json": b'{"repo":{}}',
            "index-v2.json": b'{"packages":{}}',
        }
        written = signing.sign_index_files(self.out, payloads, self.keystore_obj)
        self.assertEqual(
            sorted(os.path.basename(p) for p in written),
            ["entry.jar", "index-v1.jar", "index-v2.jar"],
        )
        for path in written:
            signing.verify_jar(path, os.path.basename(path))

    def test_v2_and_entry_use_sha256_while_v1_uses_sha1(self):
        # Matches fdroidserver: old Android cannot verify anything stronger than
        # SHA-1, but index-v2 should use the stronger algorithm.
        self.assertEqual(signing.ALGORITHMS["index-v2.jar"], ("SHA-256", "SHA256withRSA"))
        self.assertEqual(signing.ALGORITHMS["entry.jar"], ("SHA-256", "SHA256withRSA"))
        self.assertEqual(signing.ALGORITHMS["index-v1.jar"], ("SHA1", "SHA1withRSA"))

    def test_signed_jar_contains_the_signature_block(self):
        path = signing.build_jar("index-v2.json", b"{}", os.path.join(self.out, "i.jar"))
        signing.sign_jar(path, self.keystore_obj, "index-v2.jar")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        self.assertIn("index-v2.json", names)
        self.assertTrue(any(n.endswith(".SF") for n in names), names)
        self.assertTrue(any(n.endswith((".RSA", ".DSA", ".EC")) for n in names), names)

    def test_jarsigner_agrees_the_signature_is_intact(self):
        path = signing.build_jar("index-v2.json", b"{}", os.path.join(self.out, "i.jar"))
        signing.sign_jar(path, self.keystore_obj, "index-v2.jar")
        result = subprocess.run(
            ["jarsigner", "-verify", path], capture_output=True, text=True
        )
        self.assertIn("jar verified", result.stdout + result.stderr)

    def test_a_tampered_payload_is_rejected(self):
        # This is the property that makes the whole scheme worth anything.
        path = signing.build_jar("index-v2.json", b'{"good":1}', os.path.join(self.out, "i.jar"))
        signing.sign_jar(path, self.keystore_obj, "index-v2.jar")
        # Rewrite the payload inside the signed jar.
        with zipfile.ZipFile(path) as archive:
            entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
        with zipfile.ZipFile(path, "w") as archive:
            for info, data in entries:
                if info.filename == "index-v2.json":
                    data = b'{"good":0}'
                archive.writestr(info, data)
        result = subprocess.run(
            ["jarsigner", "-verify", path], capture_output=True, text=True
        )
        self.assertNotIn("jar verified", result.stdout + result.stderr)

    def test_verify_rejects_a_jar_with_two_payloads(self):
        path = os.path.join(self.out, "bad.jar")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("one.json", b"1")
            archive.writestr("two.json", b"2")
        with self.assertRaises(signing.SigningError):
            signing.verify_jar(path, "index-v2.jar")

    def test_verify_rejects_an_unsigned_jar(self):
        path = signing.build_jar("index-v2.json", b"{}", os.path.join(self.out, "u.jar"))
        with self.assertRaises(signing.SigningError):
            signing.verify_jar(path, "index-v2.jar")

    def test_missing_keystore_is_reported_clearly(self):
        with self.assertRaises(signing.SigningError) as caught:
            signing.KeyStore(
                os.path.join(self.out, "absent.p12"), self.alias, KEYSTORE_PASSWORD
            )
        self.assertIn("init-repo-key.sh", str(caught.exception))

    def test_empty_password_is_reported_clearly(self):
        with self.assertRaises(signing.SigningError) as caught:
            signing.KeyStore(self.keystore, self.alias, "")
        self.assertIn("KEYSTORE_PASS", str(caught.exception))

    def test_wrong_password_is_reported_clearly(self):
        with self.assertRaises(signing.SigningError):
            signing.KeyStore(self.keystore, self.alias, "not-the-password").fingerprint()


@unittest.skipUnless(_jarsigner_available(), "a JDK (keytool/jarsigner) is required")
class EndToEndRepositoryTests(unittest.TestCase):
    """A built repository must be internally consistent, not just well formed."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="bdon-fdroid-e2e-")
        cls.keystore_path = os.path.join(cls.dir, "keystore.p12")
        result = subprocess.run(
            [
                "keytool", "-genkeypair", "-noprompt",
                "-alias", "e2e", "-keyalg", "RSA", "-keysize", "2048", "-validity", "2",
                "-dname", "CN=e2e, O=Tests, C=US",
                "-keystore", cls.keystore_path, "-storetype", "PKCS12",
                "-storepass", KEYSTORE_PASSWORD, "-keypass", KEYSTORE_PASSWORD,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            shutil.rmtree(cls.dir, ignore_errors=True)
            raise unittest.SkipTest("could not create a test keystore")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_repository_files_agree_with_each_other(self):
        from bdon_fdroid.apk import Release
        from bdon_fdroid.config import Config

        root = os.path.join(self.dir, "repo")
        os.makedirs(root, exist_ok=True)
        config = Config()
        config.repo.address = "https://example.github.io/bdon-fdroid/fdroid/repo"
        config.validate()
        release = Release(
            url="https://cdn.example/sirius/apk/Game_1.0.1.apk",
            file_name="Game_1.0.1.apk",
            size=1234,
            sha256="e" * 64,
            version_code=10001,
            version_name="1.0.1",
            added=1789000000000,
            mirror_base="https://cdn.example/sirius/apk/",
            signer="f" * 64,
            min_sdk_version=24,
            target_sdk_version=35,
        )
        summary = indexgen.write_repository(root, config, [release])
        signing.sign_index_files(
            root, summary["files"],
            signing.KeyStore(self.keystore_path, "e2e", KEYSTORE_PASSWORD),
        )

        import hashlib
        import json

        with open(os.path.join(root, indexgen.ENTRY), encoding="utf-8") as handle:
            entry = json.load(handle)
        with open(os.path.join(root, indexgen.INDEX_V2), "rb") as handle:
            index_bytes = handle.read()
        # The client checks exactly this before trusting the index.
        self.assertEqual(entry["index"]["sha256"], hashlib.sha256(index_bytes).hexdigest())

        # And the signed jar must actually contain the same bytes.
        with zipfile.ZipFile(os.path.join(root, "index-v2.jar")) as archive:
            self.assertEqual(archive.read(indexgen.INDEX_V2), index_bytes)

        for name in indexgen.INDEX_FILES + tuple(
            n.replace(".json", ".jar") for n in indexgen.INDEX_FILES
        ):
            self.assertTrue(os.path.exists(os.path.join(root, name)), name)


if __name__ == "__main__":
    unittest.main()
