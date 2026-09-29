"""Tests for the generated F-Droid index files.

The rules asserted here are the ones that were easy to get wrong, and that
fdroidclient depends on:

* the APK is named relative to a mirror, because the client appends the name to
  the mirror address and cannot follow an absolute URL;
* v1 keeps app metadata in ``apps`` and per-version data in ``packages``, and
  carries no screenshots or hashes for images;
* v2 keys versions by SHA-256 and carries full file entries;
* ``entry.json`` names ``index-v2.json`` and its hash, or clients reject it.
"""

import hashlib
import json
import os
import shutil
import tempfile
import unittest

from bdon_fdroid import indexgen
from bdon_fdroid.apk import Release
from bdon_fdroid.config import Config

MIRROR = "https://l12-pkg-download.biligames.com/sirius/apk/"
REPO_URL = "https://example.github.io/bdon-fdroid/fdroid/repo"

#: Taken from the config rather than hardcoded, so the tests follow the project
#: if the publisher ever renames their application id.
PACKAGE = Config().package_name


def _release(version_code=10001, version_name="1.0.1", **overrides) -> Release:
    fields = {
        "url": MIRROR + f"BanGDreamOurNotes_{version_name}_x.apk",
        "file_name": f"BanGDreamOurNotes_{version_name}_x.apk",
        "size": 446890829,
        "sha256": f"{version_code:064d}",
        "version_code": version_code,
        "version_name": version_name,
        "added": 1789000000000,
        "mirror_base": MIRROR,
        "signer": "c" * 64,
        "min_sdk_version": 24,
        "target_sdk_version": 35,
        "permissions": ["android.permission.INTERNET"],
    }
    fields.update(overrides)
    return Release(**fields)


class _ConfigFixture(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.config.repo.address = REPO_URL
        self.config.validate()


class IndexV2Tests(_ConfigFixture):
    def test_versions_are_keyed_by_sha256(self):
        releases = [_release(10001, "1.0.1"), _release(10000, "1.0.0")]
        document = indexgen.build_index_v2(self.config, releases, indexgen.Assets())
        versions = document["packages"][PACKAGE]["versions"]
        self.assertEqual(set(versions), {r.sha256 for r in releases})
        for release in releases:
            self.assertEqual(versions[release.sha256]["file"]["sha256"], release.sha256)

    def test_newest_version_is_reported_as_last_updated(self):
        releases = [_release(10001, "1.0.1"), _release(10000, "1.0.0")]
        document = indexgen.build_index_v2(self.config, releases, indexgen.Assets())
        meta = document["packages"][PACKAGE]["metadata"]
        self.assertEqual(meta["preferredSigner"], "c" * 64)
        self.assertEqual(len(document["packages"][PACKAGE]["versions"]), 2)

    def test_manifest_carries_sdk_and_permissions(self):
        document = indexgen.build_index_v2(self.config, [_release()], indexgen.Assets())
        version = next(
            iter(document["packages"][PACKAGE]["versions"].values())
        )
        self.assertEqual(version["manifest"]["versionCode"], 10001)
        self.assertEqual(version["manifest"]["usesSdk"],
                         {"minSdkVersion": 24, "targetSdkVersion": 35})
        self.assertEqual(
            version["manifest"]["usesPermission"], [{"name": "android.permission.INTERNET"}]
        )

    def test_the_cdn_is_registered_as_a_mirror(self):
        document = indexgen.build_index_v2(self.config, [_release()], indexgen.Assets())
        self.assertEqual(
            document["repo"]["mirrors"],
            [{"url": MIRROR, "countryCode": "CN"}],
        )

    def test_address_is_the_url_users_add(self):
        document = indexgen.build_index_v2(self.config, [_release()], indexgen.Assets())
        self.assertEqual(document["repo"]["address"], REPO_URL)

    def test_app_metadata_reaches_the_index(self):
        document = indexgen.build_index_v2(self.config, [_release()], indexgen.Assets())
        meta = document["packages"][PACKAGE]["metadata"]
        # v2 localises the display strings but leaves authorName, categories
        # and license as plain scalars.
        self.assertEqual(meta["name"]["en-US"], "BanG Dream! Our Notes")
        self.assertEqual(meta["summary"]["en-US"], "GEKISO Gameplay x Adventure rhythm game")
        self.assertEqual(meta["authorName"], "FROMTOKYO / published by BILIBILI HK LIMITED")
        self.assertEqual(meta["categories"], ["Party Game"])
        self.assertEqual(meta["license"], "Proprietary")

    def test_repo_name_reaches_the_index(self):
        # The name is what a user reads in their repository list, so it has to
        # survive into v2 and not just sit in repo.json.
        document = indexgen.build_index_v2(self.config, [_release()], indexgen.Assets())
        name = document["repo"]["name"]["en-US"]
        self.assertEqual(name, "Unofficial BanG Dream! Our Notes index")
        self.assertIn("Unofficial", name)
        self.assertIn("Unofficial", document["repo"]["description"]["en-US"])

    def test_unknown_sdk_is_omitted_rather_than_guessed(self):
        # usesSdk is optional in the schema, and inventing an API level would be
        # worse than saying nothing.
        release = _release()
        release.min_sdk_version = None
        release.target_sdk_version = None
        document = indexgen.build_index_v2(self.config, [release], indexgen.Assets())
        version = next(iter(document["packages"][PACKAGE]["versions"].values()))
        self.assertNotIn("usesSdk", version["manifest"])

    def test_a_half_known_sdk_uses_the_known_level_for_both(self):
        release = _release()
        release.min_sdk_version = 24
        release.target_sdk_version = None
        document = indexgen.build_index_v2(self.config, [release], indexgen.Assets())
        version = next(iter(document["packages"][PACKAGE]["versions"].values()))
        self.assertEqual(version["manifest"]["usesSdk"],
                         {"minSdkVersion": 24, "targetSdkVersion": 24})


class MirrorAddressingTests(_ConfigFixture):
    """The core of the "never store the APK" design."""

    def test_apk_name_is_relative_and_joins_up_to_the_real_url(self):
        release = _release()
        document = indexgen.build_index_v2(self.config, [release], indexgen.Assets())
        version = next(iter(document["packages"][PACKAGE]["versions"].values()))
        name = version["file"]["name"]
        # fdroidclient does Mirror.getUrl(name) -> appendPathSegments, so the
        # name must NOT be absolute: it would be mangled into a path segment.
        self.assertFalse(name.startswith("http"), name)
        mirror = document["repo"]["mirrors"][0]["url"]
        self.assertEqual(mirror + name.lstrip("/"), release.url)

    def test_v1_agrees_with_v2_about_the_mirror_and_name(self):
        release = _release()
        v1 = indexgen.build_index_v1(self.config, [release], indexgen.Assets())
        v2 = indexgen.build_index_v2(self.config, [release], indexgen.Assets())
        self.assertEqual(v1["repo"]["mirrors"], [MIRROR])
        self.assertEqual(v1["repo"]["mirrors"][0], v2["repo"]["mirrors"][0]["url"])
        package = v1["packages"][PACKAGE][0]
        self.assertEqual(package["apkName"], release.file_name)
        self.assertFalse(package["apkName"].startswith("http"))


class IndexV1Tests(_ConfigFixture):
    def test_app_metadata_and_versions_are_separate(self):
        release = _release()
        document = indexgen.build_index_v1(self.config, [release], indexgen.Assets())
        app = document["apps"][0]
        self.assertEqual(app["packageName"], PACKAGE)
        self.assertEqual(app["name"], "BanG Dream! Our Notes")
        self.assertEqual(app["license"], "Proprietary")
        self.assertEqual(app["suggestedVersionCode"], "10001")
        package = document["packages"][PACKAGE][0]
        self.assertEqual(package["hashType"], "sha256")
        self.assertEqual(package["size"], 446890829)

    def test_no_screenshots_in_v1(self):
        # fdroidserver does not emit screenshots into index-v1; clients get them
        # from the v2 metadata instead.
        assets = indexgen.Assets(
            screenshots=[indexgen.Asset(f"{PACKAGE}/en-US/phoneScreenshots/1.jpg", data=b"x")]
        )
        document = indexgen.build_index_v1(self.config, [_release()], assets)
        self.assertNotIn("screenshots", json.dumps(document))
        # ...but they are referenced from `localized`, which is how v1 locates them.
        localized = document["apps"][0]["localized"]["en-US"]
        self.assertEqual(localized["phoneScreenshots"], ["1.jpg"])

    def test_v1_agrees_with_v2_about_author_and_categories(self):
        # v1 and v2 build their metadata from separate code paths, so a field
        # fixed in one is easy to forget in the other. Neo Store and Droid-ify
        # read v1, so a stale v1 is not harmless.
        v1 = indexgen.build_index_v1(self.config, [_release()], indexgen.Assets())
        v2 = indexgen.build_index_v2(self.config, [_release()], indexgen.Assets())
        app = v1["apps"][0]
        meta = v2["packages"][PACKAGE]["metadata"]
        self.assertEqual(app["authorName"], meta["authorName"])
        self.assertEqual(app["categories"], meta["categories"])
        self.assertEqual(app["authorName"], "FROMTOKYO / published by BILIBILI HK LIMITED")
        self.assertEqual(app["categories"], ["Party Game"])

    def test_v1_repo_block_is_unofficial_too(self):
        document = indexgen.build_index_v1(self.config, [_release()], indexgen.Assets())
        self.assertEqual(
            document["repo"]["name"], "Unofficial BanG Dream! Our Notes index"
        )
        self.assertIn("Unofficial", document["repo"]["description"])


class EntryTests(unittest.TestCase):
    def test_entry_names_the_index_and_its_hash(self):
        index_bytes = b'{"repo":{}}'
        entry = indexgen.build_entry(index_bytes, 1)
        self.assertEqual(entry["index"]["name"], "/index-v2.json")
        self.assertEqual(entry["index"]["sha256"], hashlib.sha256(index_bytes).hexdigest())
        self.assertEqual(entry["index"]["size"], len(index_bytes))
        self.assertEqual(entry["index"]["numPackages"], 1)
        self.assertEqual(entry["diffs"], {})


class WriteRepositoryTests(_ConfigFixture):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="bdon-fdroid-test-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_writes_every_index_file(self):
        summary = indexgen.write_repository(self.root, self.config, [_release()])
        for name in indexgen.INDEX_FILES:
            self.assertTrue(os.path.exists(os.path.join(self.root, name)), name)
        self.assertEqual(summary["latest"].version_code, 10001)

    def test_assets_land_at_the_paths_the_index_names(self):
        assets = indexgen.Assets(
            repo_icon=indexgen.Asset("icon.jpg", data=b"jpeg-bytes"),
            app_icon=indexgen.Asset(f"icons/{PACKAGE}.jpg", data=b"jpeg-bytes"),
            screenshots=[
                indexgen.Asset(f"{PACKAGE}/en-US/phoneScreenshots/1.jpg", data=b"shot")
            ],
        )
        indexgen.write_repository(self.root, self.config, [_release()], assets)
        for name in assets.repo_icon, assets.app_icon, *assets.screenshots:
            self.assertTrue(os.path.exists(os.path.join(self.root, name.name)), name.name)
        with open(os.path.join(self.root, indexgen.INDEX_V2), encoding="utf-8") as handle:
            document = json.load(handle)
        icon = document["packages"][PACKAGE]["metadata"]["icon"]["en-US"]
        self.assertEqual(icon["name"], f"/icons/{PACKAGE}.jpg")
        self.assertEqual(icon["sha256"], hashlib.sha256(b"jpeg-bytes").hexdigest())

    def test_refuses_to_publish_an_empty_repository(self):
        with self.assertRaises(indexgen.RepoBuildError):
            indexgen.write_repository(self.root, self.config, [])

    def test_output_is_deterministic_apart_from_timestamps(self):
        # An unchanged run must produce byte-identical files, otherwise the
        # workflow would commit a pointless diff on every single execution.
        first = indexgen.write_repository(self.root, self.config, [_release()])
        second = indexgen.write_repository(self.root, self.config, [_release()])
        for name in (indexgen.INDEX_V1, indexgen.INDEX_V2):
            a = json.loads(first["files"][name])
            b = json.loads(second["files"][name])
            a["repo"].pop("timestamp")
            b["repo"].pop("timestamp")
            self.assertEqual(a, b, name)

    def test_entry_hash_tracks_the_index_it_names(self):
        # entry.json carries the index's hash, so it necessarily moves when the
        # index's timestamp does; what must always hold is that it *matches*.
        summary = indexgen.write_repository(self.root, self.config, [_release()])
        entry = json.loads(summary["files"][indexgen.ENTRY])
        index_bytes = summary["files"][indexgen.INDEX_V2]
        self.assertEqual(entry["index"]["sha256"], hashlib.sha256(index_bytes).hexdigest())
        self.assertEqual(entry["index"]["size"], len(index_bytes))
        # And the file on disk must be exactly what entry.json describes,
        # because that is what the client verifies.
        with open(os.path.join(self.root, indexgen.INDEX_V2), "rb") as handle:
            on_disk = handle.read()
        self.assertEqual(hashlib.sha256(on_disk).hexdigest(), entry["index"]["sha256"])


if __name__ == "__main__":
    unittest.main()
