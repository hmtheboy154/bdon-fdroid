"""Tests for the generated landing page.

The page is what a person actually looks at when deciding whether to trust a
repository, and it is the part of the output with no schema to validate it
against. Every bug reported after the first deployment was here or in a
dependency of it, so the links are now asserted rather than eyeballed.
"""

import re
import unittest

from bdon_fdroid import landing
from bdon_fdroid.apk import Release
from bdon_fdroid.config import Config
from bdon_fdroid.scrape import SiteMetadata
from bdon_fdroid.state import State

REPO_URL = "https://example.github.io/bdon-fdroid/fdroid/repo"
MIRROR = "https://l12-pkg-download.biligames.com/sirius/apk/"
FINGERPRINT = "604f52d7463693edd837d6f9a89011d0159cb1e44a27418391527ed9f96f6157"


def _config() -> Config:
    config = Config()
    config.repo.address = REPO_URL
    return config.validate()


def _state(*releases: Release) -> State:
    return State(
        releases=list(releases) or [_release(10001, "1.0.1")],
        last_checked=1789000000000,
        last_changed=1789000000000,
    )


def _release(version_code: int, version_name: str, **overrides) -> Release:
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
        "min_sdk_version": 26,
        "target_sdk_version": 36,
    }
    fields.update(overrides)
    return Release(**fields)


def _render(state: State | None = None, fingerprint: str | None = FINGERPRINT) -> str:
    return landing.render(
        _config(),
        state or _state(),
        fingerprint,
        SiteMetadata(
            title="BanG Dream! Our Notes",
            play_store="https://play.google.com/store/apps/details?id=com.bilibili.sirius",
            app_store="https://apps.apple.com/us/app/bang-dream-our-notes/id6757695187",
            og_image="https://s1.biligames.com/share.jpg",
        ),
    )


def _hrefs(document: str) -> list[str]:
    return re.findall(r'href="([^"]+)"', document)


def _text_of(document: str) -> str:
    body = document[document.index("<body>") :]
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.DOTALL)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


class RepositoryFileLinkTests(unittest.TestCase):
    """Every link in the file list must resolve *under the repository address*.

    The bug this guards against: the base was derived by trimming the last path
    segment, which turned
    ``/fdroid/repo/index-v2.json`` into ``/fdroid/index-v2.json`` and 404'd every
    single link on the page.
    """

    def test_index_links_keep_the_repo_path_segment(self):
        hrefs = _hrefs(_render())
        for name in ("index-v1.json", "index-v2.json", "entry.json",
                     "index-v1.jar", "index-v2.jar", "entry.jar"):
            self.assertIn(
                f"{REPO_URL}/{name}", hrefs, f"{name} link is missing or wrong"
            )

    def test_no_link_drops_the_final_segment(self):
        for href in _hrefs(_render()):
            self.assertNotIn(
                "/bdon-fdroid/fdroid/index-v2.json", href, f"bad link: {href}"
            )

    def test_the_fingerprint_is_part_of_the_add_url_only(self):
        # The repository address a client uses must not carry the fingerprint as
        # part of its path; it is a query parameter, and clients that append a
        # file name need a clean base.
        document = _render()
        self.assertIn(f"{REPO_URL}?fingerprint={FINGERPRINT}", document)

    def test_links_work_with_a_redirector_address(self):
        # When a redirector fronts the repository the address is a different
        # host, but the same rule applies.
        config = _config()
        config.repo.address = "https://bdon-fdroid.example.workers.dev/fdroid/repo"
        document = landing.render(config, _state(), FINGERPRINT)
        self.assertIn(
            "https://bdon-fdroid.example.workers.dev/fdroid/repo/index-v2.json",
            _hrefs(document),
        )

    def test_links_work_when_the_address_has_a_trailing_slash(self):
        config = _config()
        config.repo.address = REPO_URL + "/"
        document = landing.render(config, _state(), FINGERPRINT)
        self.assertIn(f"{REPO_URL}/entry.jar", _hrefs(document))
        # No doubled slash.
        for href in _hrefs(document):
            self.assertNotIn(".jar//", href, href)
            self.assertNotIn(".json//", href, href)


class EssentialContentTests(unittest.TestCase):
    def test_shows_the_current_version_and_hash(self):
        text = _text_of(_render())
        self.assertIn("1.0.1", text)
        self.assertIn("10001", text)
        self.assertIn(f"{10001:064d}", text)

    def test_shows_the_signing_fingerprint(self):
        self.assertIn(FINGERPRINT, _render())

    def test_shows_the_publisher_cdn_not_a_local_copy(self):
        # The page must be honest that the bytes come from bilibili.
        text = _text_of(_render())
        self.assertIn(MIRROR, text)
        self.assertIn("not", text.lower())  # "is not stored here"

    def test_states_the_package_name(self):
        self.assertIn("com.bilibili.sirius.official", _text_of(_render()))


class HistoryTests(unittest.TestCase):
    def test_single_release_needs_no_history_table(self):
        self.assertNotIn("Release history", _text_of(_render()))

    def test_multiple_releases_are_listed_newest_first(self):
        state = _state(_release(10001, "1.0.1"), _release(10000, "1.0.0"))
        text = _text_of(_render(state))
        self.assertIn("Release history", text)
        self.assertLess(text.index("1.0.1"), text.index("1.0.0"))

    def test_each_release_links_to_its_own_apk(self):
        state = _state(_release(10001, "1.0.1"), _release(10000, "1.0.0"))
        document = _render(state)
        for release in state.releases:
            self.assertIn(release.url, document)


class PackageNameTests(unittest.TestCase):
    """The package name has to be visible, and Obtainium needs it directly.

    Obtainium cannot list the apps in a third-party F-Droid repository, so a
    reader has to type the package name in by hand. Worse, it is *not* the one
    on the Google Play listing, so guessing from there does not work.
    """

    def test_package_name_is_shown_under_latest_version(self):
        text = _text_of(_render())
        self.assertIn("Package", text)
        self.assertIn("com.bilibili.sirius.official", text)

    def test_package_name_is_its_own_row_not_part_of_the_filename(self):
        document = _render()
        # The old row showed the APK filename with its extension stripped, which
        # is not the package name and did not help anyone.
        self.assertNotIn("BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02", text_before_latest(document))

    def test_an_appid_link_is_offered_for_obtainium(self):
        hrefs = _hrefs(_render())
        expected = f"{REPO_URL}?appId=com.bilibili.sirius.official"
        self.assertIn(expected, hrefs)
        for href in hrefs:
            # Obtainium keeps the appId parameter and pre-fills the field, so
            # the fingerprint must not be mixed into the same URL.
            if "appId=" in href:
                self.assertNotIn("fingerprint", href)

    def test_all_clients_are_listed_with_the_fingerprint_url(self):
        document = _render()
        text = _text_of(document)
        for client in ("Obtainium"):
            self.assertIn(client, text)
        with_fingerprint = f"{REPO_URL}?fingerprint={FINGERPRINT}"
        self.assertGreaterEqual(_hrefs(document).count(with_fingerprint), 1)


def text_before_latest(document: str) -> str:
    """The part of the page above the "Latest version" heading."""
    marker = "<h2>Latest version</h2>"
    return document[: document.index(marker)] if marker in document else document


class UnofficialCopyTests(unittest.TestCase):
    """The page has to say what it is, before anyone trusts it.

    Nothing here is built or re-signed by us - the APK served from the mirror is
    BILIBILI HK's own build, downloaded from its CDN - so "unofficial" is the
    accurate description, and the disclaimer has to name the parties who own the
    game rather than only the tool that made the page.
    """

    def test_tagline_calls_the_page_an_unofficial_index(self):
        text = _text_of(_render())
        self.assertIn("unofficial", text.lower())
        self.assertIn("index", text.lower())

    def test_title_uses_the_configured_repo_name(self):
        document = _render()
        self.assertIn("<title>Unofficial BanG Dream! Our Notes index", document)

    def test_heading_is_the_games_real_name_not_the_repos(self):
        # The h1 answers "what is this page about"; the tagline carries the
        # unofficial claim. Putting the repo name in the h1 would bury that.
        text = _text_of(_render())
        self.assertTrue(text.lstrip().startswith("BanG Dream! Our Notes"))
        self.assertNotIn("Unofficial BanG Dream! Our Notes index", text)

    def test_disclaimer_names_the_right_parties(self):
        text = _text_of(_render())
        for party in ("Bushiroad", "FROMTOKYO", "BILIBILI"):
            self.assertIn(party, text)
        self.assertIn("F-Droid project", text)

    def test_nobody_who_did_not_make_the_game_is_named(self):
        text = _text_of(_render())
        self.assertNotIn("Craft Egg", text)


class MissingDataTests(unittest.TestCase):
    def test_renders_without_any_release(self):
        # A first run that has not found an APK yet must still produce a page.
        document = _render(State(releases=[]))
        self.assertIn("No version has been published yet", _text_of(document))

    def test_renders_without_a_fingerprint(self):
        text = _text_of(_render(fingerprint=None))
        self.assertIn("No signing key fingerprint", text)

    def test_escapes_html_in_configured_values(self):
        config = _config()
        config.app.name = 'BanG <script>alert(1)</script> "Dream"'
        document = landing.render(config, _state(), FINGERPRINT)
        self.assertNotIn("<script>alert(1)</script>", document)
        self.assertIn("&lt;script&gt;", document)


class StructureTests(unittest.TestCase):
    def test_is_a_complete_html_document(self):
        document = _render()
        self.assertTrue(document.startswith("<!doctype html>"))
        self.assertIn("<html lang=\"en\">", document)
        self.assertIn("</html>", document)

    def test_has_no_external_resources(self):
        # A GitHub Pages page should not depend on a third party to render.
        document = _render()
        self.assertNotIn("http://", document.replace("http://www.w3.org", ""))
        for href in _hrefs(document):
            self.assertFalse(
                href.endswith((".css", ".js", ".woff", ".woff2")),
                f"external asset referenced: {href}",
            )

    def test_declares_a_viewport(self):
        # Rendered on a phone, which is the only place this is read.
        self.assertIn('name="viewport"', _render())


if __name__ == "__main__":
    unittest.main()
