"""Tests for the HTTP layer, especially the resumable download.

The APK is ~450 MB, so an interrupted download has to be able to continue
rather than start over. These tests run a real HTTP server on localhost and
exercise that path, including the awkward cases: a server that ignores ``Range``,
a partial file left over from a previous run, and a body that arrives truncated.
"""

import hashlib
import http.server
import os
import shutil
import socket
import tempfile
import threading
import unittest

# Imported under an alias: the stdlib also has a top-level `http` module, and
# `from bdon_fdroid import http` would otherwise shadow it.
from bdon_fdroid import http as bdon_http

PAYLOAD = os.urandom(300_000)  # deliberately not a round number of chunks
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


class _RangeIgnoringHandler(http.server.BaseHTTPRequestHandler):
    """Serves /file, optionally pretending not to support Range."""

    payload = PAYLOAD
    honour_range = True

    def log_message(self, fmt, *args):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(self.payload)))
        self.send_header("ETag", '"deadbeef"')
        self.send_header("Last-Modified", "Fri, 18 Sep 2026 10:45:38 GMT")
        if self.honour_range:
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        if self.path != "/file":
            self.send_error(404)
            return
        start = 0
        if self.honour_range and self.headers.get("Range"):
            start = int(self.headers["Range"].split("=", 1)[1].split("-", 1)[0])
        body = self.payload[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        if start:
            self.send_header(
                "Content-Range", f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}"
            )
        self.end_headers()
        self.wfile.write(body)


class _Server:
    def __init__(self, handler_cls):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.port), handler_cls
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/file"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class _ServerFixture(unittest.TestCase):
    honour_range = True

    @classmethod
    def setUpClass(cls):
        _RangeIgnoringHandler.honour_range = cls.honour_range
        cls.server = _Server(_RangeIgnoringHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        _RangeIgnoringHandler.honour_range = True

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bdon-fdroid-http-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.dest = os.path.join(self.dir, "release.apk")


class DownloadTests(_ServerFixture):
    def test_downloads_and_hashes(self):
        sha256, size = bdon_http.download(self.server.url, self.dest, len(PAYLOAD))
        self.assertEqual(sha256, PAYLOAD_SHA256)
        self.assertEqual(size, len(PAYLOAD))
        with open(self.dest, "rb") as handle:
            self.assertEqual(handle.read(), PAYLOAD)

    def test_resumes_from_a_partial_file(self):
        # The behaviour that stops a 450 MB transfer starting over.
        partial = self.dest + ".part"
        with open(partial, "wb") as handle:
            handle.write(PAYLOAD[:100_000])
        sha256, size = bdon_http.download(self.server.url, self.dest, len(PAYLOAD))
        self.assertEqual(sha256, PAYLOAD_SHA256)
        self.assertEqual(size, len(PAYLOAD))

    def test_resumes_when_the_server_ignores_range(self):
        _RangeIgnoringHandler.honour_range = False
        self.addCleanup(setattr, _RangeIgnoringHandler, "honour_range", True)
        partial = self.dest + ".part"
        with open(partial, "wb") as handle:
            handle.write(PAYLOAD[:100_000])
        sha256, size = bdon_http.download(self.server.url, self.dest, len(PAYLOAD))
        self.assertEqual(sha256, PAYLOAD_SHA256, "must restart, but still hash correctly")
        self.assertEqual(size, len(PAYLOAD))

    def test_a_oversized_partial_file_is_discarded(self):
        partial = self.dest + ".part"
        with open(partial, "wb") as handle:
            handle.write(PAYLOAD + b"junk")
        sha256, size = bdon_http.download(self.server.url, self.dest, len(PAYLOAD))
        self.assertEqual(sha256, PAYLOAD_SHA256)
        self.assertEqual(size, len(PAYLOAD))

    def test_a_truncated_response_is_reported(self):
        # Content-Length lies; the size check is what catches it.
        _RangeIgnoringHandler.payload = PAYLOAD[:1000]
        self.addCleanup(setattr, _RangeIgnoringHandler, "payload", PAYLOAD)
        with self.assertRaises(bdon_http.HttpError) as caught:
            bdon_http.download(self.server.url, self.dest, len(PAYLOAD))
        self.assertIn("expected", str(caught.exception))

    def test_the_partial_file_is_renamed_only_on_success(self):
        bdon_http.download(self.server.url, self.dest, len(PAYLOAD))
        self.assertTrue(os.path.exists(self.dest))
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_progress_callback_receives_everything(self):
        seen = []
        bdon_http.download(
            self.server.url, self.dest, len(PAYLOAD), lambda delta, total: seen.append((delta, total))
        )
        self.assertEqual(sum(delta for delta, _ in seen), len(PAYLOAD))


class HeadTests(_ServerFixture):
    def test_reports_size_etag_and_range_support(self):
        info = bdon_http.head(self.server.url)
        self.assertEqual(info.size, len(PAYLOAD))
        self.assertEqual(info.etag, "deadbeef", "quotes are stripped")
        self.assertEqual(info.last_modified, "Fri, 18 Sep 2026 10:45:38 GMT")
        self.assertTrue(info.supports_ranges)

    def test_reports_the_absence_of_range_support(self):
        _RangeIgnoringHandler.honour_range = False
        self.addCleanup(setattr, _RangeIgnoringHandler, "honour_range", True)
        self.assertFalse(bdon_http.head(self.server.url).supports_ranges)

    def test_a_404_becomes_a_readable_error(self):
        with self.assertRaises(bdon_http.HttpError):
            bdon_http.head("http://127.0.0.1:1/nope")

    def test_a_malformed_url_becomes_a_readable_error_not_a_traceback(self):
        with self.assertRaises(bdon_http.HttpError):
            bdon_http.get_text("not a url at all")


class TextAndResolveTests(unittest.TestCase):
    def test_resolve_upgrades_protocol_relative_urls(self):
        self.assertEqual(
            bdon_http.resolve("https://bdon.biligames.com/", "//s1.biligames.com/a.js"),
            "https://s1.biligames.com/a.js",
        )

    def test_resolve_handles_root_relative_and_absolute_urls(self):
        self.assertEqual(
            bdon_http.resolve("https://example.com/x/y", "/a.js"), "https://example.com/a.js"
        )
        self.assertEqual(
            bdon_http.resolve("https://example.com/x/", "https://other.example/b.js"),
            "https://other.example/b.js",
        )

    def test_decodes_undecodable_bytes_rather_than_raising(self):
        # A bundle served with the wrong charset must not abort a run.
        import http.server
        import threading as _threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "3")
                self.end_headers()
                self.wfile.write(b"\xff\xfe\xfd")

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        thread = _threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = bdon_http.get_text(f"http://127.0.0.1:{port}/x")
            self.assertIsInstance(body, str)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
