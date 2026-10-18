"""Tests for which images get published.

`_build_assets` decides what ends up on disk from what the site advertises and
what `repo.json` pins. It had no coverage at all, which is how the favicon could
be wired to the wrong source without anything noticing: the page's `<link
rel=icon>` and the app's own webpack icon are different files, and picking the
wrong one looks fine on the page.

Every fetch is stubbed, so nothing here touches the network.
"""

import unittest
from unittest import mock

from bdon_fdroid import cli
from bdon_fdroid.config import Config
from bdon_fdroid.scrape import SiteMetadata

ICO_MAGIC = b"\x00\x00\x01\x00"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _site(**overrides) -> SiteMetadata:
    site = SiteMetadata(
        title="BanG Dream! Our Notes",
        og_image="https://s1.example.com/share.jpg",
        app_icon="https://s1.example.com/gw/img/icon.e419ff81.png",
        favicon="https://s1.example.com/gw/images/common/favicon.ico",
    )
    for name, value in overrides.items():
        setattr(site, name, value)
    return site


def _config() -> Config:
    config = Config()
    config.repo.address = "https://example.com/fdroid/repo"
    return config.validate()


def _responses(**by_url):
    """A stand-in for http.get_bytes_with_type, keyed by URL."""
    default = (b"", "application/octet-stream")

    def fetch(url):
        for fragment, value in by_url.items():
            if fragment in url:
                return value
        return default

    return mock.patch("bdon_fdroid.http.get_bytes_with_type", side_effect=fetch)


class FaviconSourceTests(unittest.TestCase):
    """The favicon comes from the page's <link rel=icon>, not the app icon.

    They are different files: the favicon is a 34 KB multi-resolution ICO, the
    app icon a webpack PNG of the game's own artwork. Publishing the wrong one
    would put a downscaled app icon in the tab.
    """

    def test_it_takes_the_pages_icon_link(self):
        site = _site(favicon="https://s1.example.com/gw/favicon.ico",
                     app_icon="https://s1.example.com/gw/img/icon.e419ff81.png")
        with _responses(
            **{"gw/favicon.ico": (ICO_MAGIC + b"rest", "image/vnd.microsoft.icon")}
        ):
            assets = cli._build_assets(_config(), site)
        self.assertIsNotNone(assets.favicon)
        self.assertEqual(assets.favicon.name, "favicon.ico")

    def test_it_is_never_the_apps_own_icon(self):
        site = _site(app_icon="https://s1.example.com/gw/img/icon.e419ff81.png")
        with _responses(**{
            "gw/images/common/favicon.ico": (ICO_MAGIC + b"x", "image/vnd.microsoft.icon"),
            "img/icon.e419ff81.png": (PNG_MAGIC + b"x", "image/png"),
        }):
            assets = cli._build_assets(_config(), site)
        self.assertEqual(assets.favicon.data, ICO_MAGIC + b"x")
        self.assertEqual(assets.app_icon.data, PNG_MAGIC + b"x")

    def test_a_pinned_favicon_url_wins(self):
        config = _config()
        config.assets.favicon_url = "https://s1.example.com/pinned.ico"
        with _responses(**{
            "pinned.ico": (ICO_MAGIC + b"p", "image/x-icon"),
            "gw/images/common/favicon.ico": (ICO_MAGIC + b"s", "image/x-icon"),
        }):
            assets = cli._build_assets(config, _site())
        self.assertEqual(assets.favicon.data, ICO_MAGIC + b"p")

    def test_the_extension_comes_from_the_content_type(self):
        # The URL has no filename, so a URL-derived name would be wrong.
        site = _site(favicon="https://s1.example.com/gw/common/favicon")
        with _responses(**{"gw/common/favicon": (ICO_MAGIC + b"x",
                                                 "image/vnd.microsoft.icon")}):
            assets = cli._build_assets(_config(), site)
        self.assertEqual(assets.favicon.name, "favicon.ico")

    def test_a_page_with_no_icon_link_still_builds(self):
        with _responses():
            assets = cli._build_assets(_config(), _site(favicon=""))
        self.assertIsNone(assets.favicon)
        # The repository itself is unaffected; only the tab icon is missing.

    def test_an_unfetchable_favicon_is_skipped_not_fatal(self):
        from bdon_fdroid.http import HttpError

        def fetch(url):
            if "favicon" in url:
                raise HttpError("404")
            return b"jpeg", "image/jpeg"

        with mock.patch("bdon_fdroid.http.get_bytes_with_type", side_effect=fetch):
            assets = cli._build_assets(_config(), _site())
        self.assertIsNone(assets.favicon)
        self.assertIsNotNone(assets.repo_icon, "the rest of the build must continue")


class IconSourceTests(unittest.TestCase):
    def test_the_repository_icon_comes_from_og_image(self):
        with _responses(**{"share.jpg": (b"jpeg", "image/jpeg")}):
            assets = cli._build_assets(_config(), _site())
        self.assertEqual(assets.repo_icon.name, "icon.jpg")

    def test_a_pinned_repository_icon_wins(self):
        config = _config()
        config.assets.icon_url = "https://s1.example.com/pinned.png"
        with _responses(**{"pinned.png": (PNG_MAGIC, "image/png"),
                           "share.jpg": (b"jpeg", "image/jpeg")}):
            assets = cli._build_assets(config, _site())
        self.assertEqual(assets.repo_icon.name, "icon.png")

    def test_the_app_icon_lands_where_v1_clients_look_for_it(self):
        with _responses(**{"img/icon.e419ff81.png": (PNG_MAGIC, "image/png")}):
            assets = cli._build_assets(_config(), _site())
        self.assertEqual(
            assets.app_icon.name, f"icons/{Config().package_name}.png"
        )

    def test_an_unknown_content_type_is_skipped(self):
        with _responses(**{"share.jpg": (b"x", "application/x-unknown")}):
            assets = cli._build_assets(_config(), _site())
        self.assertIsNone(assets.repo_icon)


class ScreenshotTests(unittest.TestCase):
    def test_screenshots_are_capped(self):
        config = _config()
        config.assets.max_screenshots = 2
        config.assets.screenshot_urls = [
            f"https://s1.example.com/shot{i}.png" for i in range(5)
        ]
        with _responses(**{"shot": (PNG_MAGIC, "image/png")}):
            assets = cli._build_assets(config, _site())
        self.assertEqual(len(assets.screenshots), 2)

    def test_each_screenshot_gets_its_own_path(self):
        config = _config()
        config.assets.screenshot_urls = [
            "https://s1.example.com/a.png",
            "https://s1.example.com/b.png",
        ]
        with _responses(**{".png": (PNG_MAGIC, "image/png")}):
            assets = cli._build_assets(config, _site())
        names = [a.name for a in assets.screenshots]
        self.assertEqual(len(set(names)), len(names), "paths collided")
        for name in names:
            self.assertIn("phoneScreenshots", name)
            self.assertIn(Config().package_name, name)


if __name__ == "__main__":
    unittest.main()