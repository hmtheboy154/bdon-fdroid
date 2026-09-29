"""Conformance test: can a real F-Droid client implementation read our output?

The rest of the suite checks that the index files are shaped the way we intend.
This one checks the thing that actually matters - that ``fdroidserver``, the
reference implementation, will download a signed repository over HTTP, verify
its signature, match the key fingerprint, and agree with the index hash.

``fdroidserver`` is a heavyweight dependency that the daily job does not need,
so this test skips unless it is importable. CI installs it explicitly; see
``.github/workflows/conformance.yml`` and ``bdon-fdroid verify``.

To run it locally::

    python3 -m venv /tmp/fdcheck
    /tmp/fdcheck/bin/pip install fdroidserver
    /tmp/fdcheck/bin/python -m unittest discover -s tests -v
"""

import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import pathlib
import threading
import unittest

from bdon_fdroid import config as config_module
from bdon_fdroid import indexgen, signing
from bdon_fdroid.apk import Release
from bdon_fdroid.config import Config

KEYSTORE_PASSWORD = "conformance-test-password"
MIRROR = "https://l12-pkg-download.biligames.com/sirius/apk/"
PACKAGE = Config().package_name
REPO_JSON = pathlib.Path(__file__).resolve().parent.parent / "repo.json"


def _fdroidserver_available() -> bool:
    try:
        import fdroidserver  # noqa: F401
    except ImportError:
        return False
    return True


def _release(version_code: int = 10001, version_name: str = "1.0.1") -> Release:
    """A stand-in for a real download. No APK is fetched by these tests."""
    return Release(
        url=MIRROR + "BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02.apk",
        file_name="BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02.apk",
        size=446890829,
        sha256="d" * 64,
        version_code=version_code,
        version_name=version_name,
        added=1789000000000,
        mirror_base=MIRROR,
        signer="a" * 64,
        min_sdk_version=26,
        target_sdk_version=36,
    )


@unittest.skipUnless(_fdroidserver_available(), "fdroidserver is not installed")
@unittest.skipUnless(shutil.which("jarsigner"), "a JDK is required")
class RepositoryConformanceTests(unittest.TestCase):
    """Build, sign, serve and verify a repository the way a client would."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="bdon-fdroid-conf-")
        cls.repo_root = os.path.join(cls.dir, "fdroid", "repo")
        os.makedirs(cls.repo_root, exist_ok=True)

        cls.keystore_path = os.path.join(cls.dir, "keystore.p12")
        result = subprocess.run(
            [
                "keytool", "-genkeypair", "-noprompt",
                "-alias", "conformance", "-keyalg", "RSA", "-keysize", "2048",
                "-validity", "2", "-dname", "CN=conformance, O=Tests, C=US",
                "-keystore", cls.keystore_path, "-storetype", "PKCS12",
                "-storepass", KEYSTORE_PASSWORD, "-keypass", KEYSTORE_PASSWORD,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            shutil.rmtree(cls.dir, ignore_errors=True)
            raise unittest.SkipTest(f"could not create a keystore: {result.stderr.strip()}")

        cls.keystore = signing.KeyStore(cls.keystore_path, "conformance", KEYSTORE_PASSWORD)
        cls.fingerprint = cls.keystore.fingerprint()

        config = Config()
        # The address must be the URL the client will actually use, so it has to
        # be the one the local test server is reachable on.
        cls.server = _Server(cls.dir)
        config.repo.address = f"http://127.0.0.1:{cls.server.port}/fdroid/repo"
        config.validate()

        cls.package = Config().package_name
        cls.release = Release(
            url=MIRROR + "BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02.apk",
            file_name="BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02.apk",
            size=446890829,
            sha256="d" * 64,
            version_code=10001,
            version_name="1.0.1",
            added=1789000000000,
            mirror_base=MIRROR,
            signer="a" * 64,
            min_sdk_version=24,
            target_sdk_version=35,
            permissions=["android.permission.INTERNET"],
        )
        assets = indexgen.Assets(
            repo_icon=indexgen.Asset("icon.jpg", data=b"fake-jpeg"),
            app_icon=indexgen.Asset("icons/com.bilibili.sirius.jpg", data=b"fake-jpeg"),
        )
        summary = indexgen.write_repository(
            cls.repo_root, config, [cls.release], assets
        )
        signing.sign_index_files(cls.repo_root, summary["files"], cls.keystore)
        cls.config = config

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "server", None):
            cls.server.stop()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _url(self) -> str:
        return f"{self.config.repo.address}?fingerprint={self.fingerprint.upper()}"

    def _configure_fdroidserver(self):
        # fdroidserver reads jarsigner's location from its own config file; the
        # conformance check only needs signature verification, so point it at
        # the JDK already on PATH instead of running 'fdroid init'.
        from fdroidserver import common

        common.config = {
            "jarsigner": shutil.which("jarsigner"),
            "keytool": shutil.which("keytool"),
            "apksigner": None,
        }
        return common

    def test_index_v2_downloads_and_verifies(self):
        from fdroidserver import index

        self._configure_fdroidserver()
        document, _ = index.download_repo_index_v2(
            self._url(), verify_fingerprint=self.fingerprint
        )
        packages = document["packages"]
        self.assertIn(PACKAGE, packages)
        versions = packages[PACKAGE]["versions"]
        self.assertEqual(list(versions), [self.release.sha256])
        version = versions[self.release.sha256]
        self.assertEqual(version["manifest"]["versionCode"], 10001)
        self.assertEqual(version["file"]["name"], "/" + self.release.file_name)

    def test_index_v1_downloads_and_verifies(self):
        from fdroidserver import index

        self._configure_fdroidserver()
        result = index.download_repo_index_v1(
            self._url(), verify_fingerprint=self.fingerprint
        )
        document = result[0] if isinstance(result, tuple) else result
        package = document["packages"][PACKAGE][0]
        self.assertEqual(package["hash"], self.release.sha256)
        self.assertEqual(package["hashType"], "sha256")
        self.assertEqual(package["versionCode"], 10001)

    def test_both_formats_agree_about_the_mirror(self):
        # If these diverged, a v1 client would try to fetch the APK from the
        # Pages site and fail.
        from fdroidserver import index

        self._configure_fdroidserver()
        v2, _ = index.download_repo_index_v2(
            self._url(), verify_fingerprint=self.fingerprint
        )
        result = index.download_repo_index_v1(
            self._url(), verify_fingerprint=self.fingerprint
        )
        v1 = result[0] if isinstance(result, tuple) else result
        self.assertEqual(v1["repo"]["mirrors"], [MIRROR])
        self.assertEqual(
            v2["repo"]["mirrors"][0]["url"], v1["repo"]["mirrors"][0]
        )

    def test_a_wrong_fingerprint_is_rejected(self):
        from fdroidserver import index
        from fdroidserver.exception import VerificationException

        self._configure_fdroidserver()
        url = f"{self.config.repo.address}?fingerprint={'0' * 64}"
        with self.assertRaises(VerificationException):
            index.download_repo_index_v2(url, verify_fingerprint="0" * 64)

    def test_a_tampered_index_is_rejected(self):
        from fdroidserver import index
        from fdroidserver.exception import VerificationException

        self._configure_fdroidserver()
        path = os.path.join(self.repo_root, indexgen.INDEX_V2)
        with open(path, encoding="utf-8") as handle:
            original = handle.read()
        try:
            document = json.loads(original)
            document["packages"][PACKAGE]["metadata"]["license"] = "Evil"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            with self.assertRaises(VerificationException):
                index.download_repo_index_v2(
                    self._url(), verify_fingerprint=self.fingerprint
                )
        finally:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(original)

    def test_icon_the_index_points_at_is_served(self):
        from urllib.request import urlopen

        from fdroidserver import index

        self._configure_fdroidserver()
        document, _ = index.download_repo_index_v2(
            self._url(), verify_fingerprint=self.fingerprint
        )
        icon = document["packages"][PACKAGE]["metadata"]["icon"]["en-US"]
        url = self.config.repo.address + icon["name"]
        with urlopen(url) as response:  # noqa: S310 - a local test server
            body = response.read()
        self.assertEqual(len(body), icon["size"])


@unittest.skipUnless(_fdroidserver_available(), "fdroidserver is not installed")
@unittest.skipUnless(shutil.which("jarsigner"), "a JDK is required")
class ShippedConfigConformanceTests(unittest.TestCase):
    """Verify the repository that ``repo.json`` actually describes.

    The other class builds from the dataclass defaults, so it never sees the
    file that is really published. The description is 2730 characters of HTML
    and the category is a string no schema enforces, which is exactly the kind
    of thing that parses fine in a unit test and reaches a user broken. Only
    the address is overridden, because it has to be the local test server.
    """

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="bdon-fdroid-shipped-")
        cls.repo_root = os.path.join(cls.dir, "fdroid", "repo")
        os.makedirs(cls.repo_root, exist_ok=True)
        cls.keystore_path = os.path.join(cls.dir, "keystore.p12")
        result = subprocess.run(
            [
                "keytool", "-genkeypair", "-noprompt",
                "-alias", "shipped", "-keyalg", "RSA", "-keysize", "2048",
                "-validity", "2", "-dname", "CN=shipped, O=Tests, C=US",
                "-keystore", cls.keystore_path, "-storetype", "PKCS12",
                "-storepass", KEYSTORE_PASSWORD, "-keypass", KEYSTORE_PASSWORD,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            shutil.rmtree(cls.dir, ignore_errors=True)
            raise unittest.SkipTest(f"could not create a keystore: {result.stderr.strip()}")

        cls.keystore = signing.KeyStore(cls.keystore_path, "shipped", KEYSTORE_PASSWORD)
        cls.fingerprint = cls.keystore.fingerprint()

        cls.server = _Server(cls.dir)
        config = config_module.load(str(REPO_JSON))
        config.repo.address = f"http://127.0.0.1:{cls.server.port}/fdroid/repo"
        config.validate()
        cls.config = config

        assets = indexgen.Assets(
            repo_icon=indexgen.Asset("icon.jpg", data=b"fake-jpeg"),
            app_icon=indexgen.Asset("icons/com.bilibili.sirius.official.jpg", data=b"fake-jpeg"),
        )
        summary = indexgen.write_repository(cls.repo_root, config, [_release()], assets)
        signing.sign_index_files(cls.repo_root, summary["files"], cls.keystore)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "server", None):
            cls.server.stop()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _configure_fdroidserver(self):
        from fdroidserver import common

        common.config = {
            "jarsigner": shutil.which("jarsigner"),
            "keytool": shutil.which("keytool"),
            "apksigner": None,
        }
        return common

    def _url(self) -> str:
        return f"{self.config.repo.address}?fingerprint={self.fingerprint.upper()}"

    def test_shipped_repository_downloads_and_verifies(self):
        from fdroidserver import index

        self._configure_fdroidserver()
        document, _ = index.download_repo_index_v2(
            self._url(), verify_fingerprint=self.fingerprint
        )
        self.assertIn(PACKAGE, document["packages"])
        result = index.download_repo_index_v1(
            self._url(), verify_fingerprint=self.fingerprint
        )
        v1 = result[0] if isinstance(result, tuple) else result
        self.assertIn(PACKAGE, v1["packages"])

    def test_shipped_description_survives_the_round_trip(self):
        from fdroidserver import index

        self._configure_fdroidserver()
        document, _ = index.download_repo_index_v2(
            self._url(), verify_fingerprint=self.fingerprint
        )
        served = document["packages"][PACKAGE]["metadata"]["description"]["en-US"]
        self.assertEqual(served, self.config.app.description)
        # The clients parse this, so it has to still be HTML after the round
        # trip and not have been escaped into visible angle brackets.
        self.assertIn("<p>", served)
        self.assertNotIn("&lt;p&gt;", served)

    def test_shipped_metadata_reaches_both_index_formats(self):
        from fdroidserver import index

        self._configure_fdroidserver()
        v2, _ = index.download_repo_index_v2(
            self._url(), verify_fingerprint=self.fingerprint
        )
        result = index.download_repo_index_v1(
            self._url(), verify_fingerprint=self.fingerprint
        )
        v1 = result[0] if isinstance(result, tuple) else result
        # v2 puts app metadata under packages[...]["metadata"]; v1 keeps it in a
        # separate "apps" list, which is why a field fixed in one format and
        # forgotten in the other goes unnoticed.
        meta = v2["packages"][PACKAGE]["metadata"]
        app = next(a for a in v1["apps"] if a["packageName"] == PACKAGE)
        for value in (meta["authorName"], app["authorName"]):
            self.assertEqual(value, "FROMTOKYO / published by BILIBILI HK LIMITED")
        for value in (meta["categories"], app["categories"]):
            self.assertEqual(value, ["Party Game"])
        self.assertIn("Unofficial", v2["repo"]["name"]["en-US"])
        self.assertIn("Unofficial", v1["repo"]["name"])


class _Server:
    """A throwaway static file server, so the repository is fetched over HTTP."""

    def __init__(self, root: str):
        self.root = root
        self.port = self._free_port()
        handler = functools.partial(_QuietHandler, directory=root)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if os.environ.get("BDON_FDROID_TEST_VERBOSE"):
            sys.stderr.write("  " + (fmt % args) + "\n")


if __name__ == "__main__":
    unittest.main()
