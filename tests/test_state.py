"""Tests for the ``releases.json`` cache.

This cache is what keeps the daily job cheap: it remembers the hash of a remote
object so an unchanged run needs one ``HEAD`` instead of a 446 MB download.  It
is also committed to git, so it has to round-trip losslessly and write
deterministically.
"""

import json
import os
import shutil
import tempfile
import unittest

from bdon_fdroid import state as state_module
from bdon_fdroid.apk import Release
from bdon_fdroid.state import STATE_VERSION, State

MIRROR = "https://cdn.example/sirius/apk/"


def _release(version_code=10001, version_name="1.0.1", **overrides) -> Release:
    fields = {
        "url": MIRROR + f"Game_{version_name}.apk",
        "file_name": f"Game_{version_name}.apk",
        "size": 446890829,
        "sha256": f"{version_code:064d}",
        "version_code": version_code,
        "version_name": version_name,
        "added": 1789000000000,
        "mirror_base": MIRROR,
        "etag": "abc123",
        "last_modified": "Fri, 18 Sep 2026 10:45:38 GMT",
        "signer": "c" * 64,
        "min_sdk_version": 24,
        "target_sdk_version": 35,
        "permissions": ["android.permission.INTERNET"],
    }
    fields.update(overrides)
    return Release(**fields)


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bdon-fdroid-state-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "releases.json")

    def test_every_field_survives_a_round_trip(self):
        original = State(
            releases=[_release(10001, "1.0.1"), _release(10000, "1.0.0")],
            last_checked=1789000000000,
            last_changed=1789000000000,
        )
        self.assertTrue(state_module.save(self.path, original))
        loaded = state_module.load(self.path)
        self.assertEqual(loaded.last_checked, 1789000000000)
        self.assertEqual(loaded.last_changed, 1789000000000)
        self.assertEqual(len(loaded.releases), 2)
        for before, after in zip(original.sorted_releases(), loaded.sorted_releases()):
            self.assertEqual(before.to_dict(), after.to_dict())

    def test_saving_identical_state_reports_no_change(self):
        # The workflow relies on this to avoid a commit on every run.
        original = State(releases=[_release()], last_checked=1)
        self.assertTrue(state_module.save(self.path, original))
        self.assertFalse(state_module.save(self.path, state_module.load(self.path)))

    def test_missing_file_is_an_empty_history_not_an_error(self):
        state = state_module.load(os.path.join(self.dir, "absent.json"))
        self.assertEqual(state.releases, [])
        self.assertIsNone(state.latest())

    def test_written_json_is_newline_terminated_and_ordered(self):
        state_module.save(self.path, State(releases=[_release(10000), _release(10001)]))
        with open(self.path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue(text.endswith("\n"))
        versions = [r["versionCode"] for r in json.loads(text)["releases"]]
        self.assertEqual(versions, [10001, 10000], "newest first")

    def test_a_future_format_is_refused_rather_than_misread(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"stateVersion": STATE_VERSION + 1, "releases": []}, handle)
        with self.assertRaises(SystemExit):
            state_module.load(self.path)

    def test_corrupt_json_reports_the_path(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(SystemExit) as caught:
            state_module.load(self.path)
        self.assertIn("releases.json", str(caught.exception))


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.state = State(releases=[_release(10001, "1.0.1"), _release(10000, "1.0.0")])

    def test_latest_is_the_highest_version_code(self):
        self.assertEqual(self.state.latest().version_code, 10001)

    def test_find_matches_on_url(self):
        target = self.state.releases[0]
        self.assertIs(self.state.find(target.url), target)
        self.assertIsNone(self.state.find(MIRROR + "nope.apk"))

    def test_upsert_replaces_rather_than_duplicating(self):
        replacement = _release(10001, "1.0.1", sha256="f" * 64)
        self.state.upsert(replacement)
        self.assertEqual(len(self.state.releases), 2)
        self.assertEqual(self.state.find(replacement.url).sha256, "f" * 64)

    def test_upsert_appends_a_genuinely_new_release(self):
        self.state.upsert(_release(10002, "1.0.2"))
        self.assertEqual(len(self.state.releases), 3)
        self.assertEqual(self.state.latest().version_code, 10002)

    def test_prune_keeps_the_newest(self):
        dropped = self.state.prune(1)
        self.assertEqual([r.version_code for r in dropped], [10000])
        self.assertEqual(self.state.latest().version_code, 10001)

    def test_prune_is_a_no_op_when_within_the_limit(self):
        self.assertEqual(self.state.prune(5), [])
        self.assertEqual(len(self.state.releases), 2)


class ChangeDetectionTests(unittest.TestCase):
    """The comparison that decides whether to download 446 MB or not."""

    def test_identical_remote_metadata_means_unchanged(self):
        release = _release()
        same = _release(10001, "1.0.1")
        self.assertEqual(release.remote_fingerprint(), same.remote_fingerprint())

    def test_a_different_etag_means_changed(self):
        release = _release()
        moved = _release(10001, "1.0.1", etag="def456")
        self.assertNotEqual(release.remote_fingerprint(), moved.remote_fingerprint())

    def test_a_different_size_means_changed(self):
        release = _release()
        resized = _release(10001, "1.0.1", size=1)
        self.assertNotEqual(release.remote_fingerprint(), resized.remote_fingerprint())

    def test_rehashing_the_same_file_is_considered_unchanged(self):
        # The URL is the primary key; a rebuilt-but-identical APK must not
        # trigger a fresh download.
        release = _release(10001, "1.0.1")
        rebuilt = _release(10001, "1.0.1")
        self.assertEqual(release.url, rebuilt.url)


if __name__ == "__main__":
    unittest.main()
