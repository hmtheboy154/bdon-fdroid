"""Tests for discovering the app icon from the site's own assets.

The site has no ``<link rel=apple-touch-icon>``, and its only ``<link rel=icon>``
is a 16px ``favicon.ico`` - far too small to be the app icon. The real icon is a
webpack asset, referenced by name from a bundle and resolved against the public
path:

    ga = t.p + "img/icon.e419ff81.png"    where  t.p = "//s1.../gw/"

So both halves are read from the bundles, with no hardcoded paths, which is what
keeps this working through the content-hash renames the site does on every
deploy. Ambiguity raises rather than guessing, because a wrong icon is published
to every client that syncs the repository.
"""

import unittest

from bdon_fdroid import scrape

#: Trimmed shapes of the real bootstrap and app bundles.
BOOTSTRAP = 'r.p="//s1.biligames.com/fe-static/game-global-bangdreamon/gw/";r.u=e=>e'
APP_BUNDLE = (
    'ca=t.p+"img/logo.743c5945.svg",'
    'ga=t.p+"img/icon.e419ff81.png",'
    'ha=t.p+"img/logo_fromtokyo.a6c4d54d.png"'
)
VENDORS = 'function(e,t){"use strict";e.exports=t}'


class PublicPathTests(unittest.TestCase):
    def test_finds_the_public_path(self):
        self.assertEqual(
            scrape._webpack_public_path([BOOTSTRAP, APP_BUNDLE]),
            "//s1.biligames.com/fe-static/game-global-bangdreamon/gw/",
        )

    def test_finds_an_https_public_path(self):
        self.assertEqual(
            scrape._webpack_public_path(['r.p="https://cdn.example.com/game/";']),
            "https://cdn.example.com/game/",
        )

    def test_ignores_unrelated_assignments(self):
        # Minified code is full of .p= that is not a public path.
        noisy = 'a.p=1;b.prop="x";c.p={};d.p="not a url";'
        self.assertEqual(scrape._webpack_public_path([noisy]), "")

    def test_no_public_path_is_not_an_error(self):
        self.assertEqual(scrape._webpack_public_path([VENDORS]), "")

    def test_two_public_paths_is_an_error(self):
        # A split bundle could put the icon under either host, so decline.
        with self.assertRaises(scrape.ScrapeError) as caught:
            scrape._webpack_public_path(['r.p="//a.example.com/gw/";', 'r.p="//b.example/gw/";'])
        self.assertIn("appIconUrl", str(caught.exception))


class AppIconTests(unittest.TestCase):
    def test_resolves_the_icon_to_an_absolute_url(self):
        self.assertEqual(
            scrape.find_app_icon([BOOTSTRAP, APP_BUNDLE]),
            "https://s1.biligames.com/fe-static/game-global-bangdreamon/gw/img/icon.e419ff81.png",
        )

    def test_scheme_relative_public_path_becomes_https(self):
        self.assertTrue(scrape.find_app_icon([BOOTSTRAP, APP_BUNDLE]).startswith("https://"))

    def test_ignores_other_assets(self):
        # The bundle mentions logos and other art; only "icon" counts.
        icon = scrape.find_app_icon([BOOTSTRAP, APP_BUNDLE])
        self.assertIn("/img/icon.", icon)
        self.assertNotIn("logo", icon)

    def test_finds_the_icon_in_any_bundle(self):
        self.assertIn("/img/icon.", scrape.find_app_icon([APP_BUNDLE, BOOTSTRAP]))

    def test_escaped_slashes_are_handled(self):
        # Minifiers sometimes escape the slashes in an asset name.
        bundle = 'r.p="//cdn.example/gw/";x="img\\/icon.abc123.png"'
        self.assertEqual(
            scrape.find_app_icon([bundle]), "https://cdn.example/gw/img/icon.abc123.png"
        )

    def test_accepts_common_image_extensions(self):
        for extension in ("png", "jpg", "jpeg", "webp", "svg"):
            with self.subTest(extension=extension):
                bundle = f'r.p="//cdn.example/gw/";x="img/icon.abc123.{extension}"'
                self.assertTrue(
                    scrape.find_app_icon([bundle]).endswith(f"icon.abc123.{extension}")
                )

    def test_no_icon_yields_empty_string(self):
        self.assertEqual(scrape.find_app_icon([BOOTSTRAP, VENDORS]), "")

    def test_no_public_path_yields_empty_string(self):
        # The page still works; the caller falls back to the configured icon.
        self.assertEqual(scrape.find_app_icon(['x="img/icon.abc123.png"']), "")

    def test_two_candidate_icons_is_an_error(self):
        # Better to stop than to publish a wrong icon to every client.
        bundle = 'r.p="//cdn.example/gw/";a="img/icon.aaa.png";b="img/icon.bbb.png"'
        with self.assertRaises(scrape.ScrapeError) as caught:
            scrape.find_app_icon([bundle])
        self.assertIn("appIconUrl", str(caught.exception))
        self.assertIn("img/icon.aaa.png", str(caught.exception))


class DiscoverIntegrationTests(unittest.TestCase):
    """The icon is discovered as part of the normal scrape."""

    PAGES = {
        scrape.SITE_URL: '<html><head><script defer src="//s1.biligames.com/gw/js/index.1.js"></script>'
        '<script defer src="//s1.biligames.com/gw/js/chunk-common.2.js"></script>'
        "</head></html>",
        "https://s1.biligames.com/gw/js/index.1.js": BOOTSTRAP,
        "https://s1.biligames.com/gw/js/chunk-common.2.js": (
            'Le="https://l12-pkg-download.biligames.com/sirius/apk/Game_1.0.apk",'
            + APP_BUNDLE
        ),
    }

    def test_discover_reports_the_icon(self):
        link, meta = scrape.discover(fetch=self.PAGES.__getitem__)
        self.assertEqual(
            meta.app_icon, "https://s1.biligames.com/fe-static/game-global-bangdreamon/gw/img/icon.e419ff81.png"
        )
        self.assertEqual(
            link.url, "https://l12-pkg-download.biligames.com/sirius/apk/Game_1.0.apk"
        )

    def test_icon_is_in_serialised_metadata(self):
        _link, meta = scrape.discover(fetch=self.PAGES.__getitem__)
        self.assertIn("appIcon", meta.as_dict())
        self.assertTrue(meta.as_dict()["appIcon"])


if __name__ == "__main__":
    unittest.main()
