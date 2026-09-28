"""Tests for the scraper.

The fixtures are trimmed-down copies of the real page and bundle, keeping the
two properties that matter: the ``<script src>`` tags are content-hashed, and
the APK link is a string literal that only the download button uses.
"""

import unittest

from bdon_fdroid import scrape

# Shape of the live index.html: protocol-relative bundles with content hashes,
# plus document.write() cache-busting that must NOT be mistaken for a real file.
INDEX_HTML = """
<!doctype html><html lang=""><head>
<meta charset="utf-8"/>
<meta name="description" content='Let our sound resonate. BanG Dream! Our Notes'/>
<meta property="og:title" content="BanG Dream! Our Notes Official Website"/>
<meta property="og:image" content="https://s1.biligames.com/fe-static/game/gw/images/common/share.jpg"/>
<script>document.write(
  '<script src="//s1.biligames.com/fe-static/game/report/index.js?t=' +
    new Date().getTime() +
  '"><\\/script>'
);
</script>
<script defer="defer" src="//s1.biligames.com/fe-static/game/gw/js/chunk-vendors.076929bc.js"></script>
<script defer="defer" src="//s1.biligames.com/fe-static/game/gw/js/chunk-common.79593525.js"></script>
<script defer="defer" src="//s1.biligames.com/fe-static/game/gw/js/index.38a78496.js"></script>
</head><body><div id="app"></div></body></html>
"""

# The real bundle shape: minified, single-letter variable assigned the URL, and
# window.open() only reachable from the Android download button.
CHUNK_COMMON = (
    'var ye={"en-US":"https://apps.apple.com/us/app/x/id6757695187"},'
    'fe="https://play.google.com/store/apps/details?id=com.bilibili.sirius",'
    'Le="https://l12-pkg-download.biligames.com/sirius/apk/'
    'BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02.apk",'
    'E=e=>{if("ios"===e)window.open(l);else if("google"===e)window.open(fe);'
    'else if("apk"===e){if(t.value)return void window.open(Le)}}'
)
CHUNK_VENDORS = 'function(e,t){"use strict";e.exports=t}'

APK_URL = (
    "https://l12-pkg-download.biligames.com/sirius/apk/"
    "BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02.apk"
)


class ScriptUrlTests(unittest.TestCase):
    def test_finds_only_the_real_bundles(self):
        urls = scrape.script_urls(INDEX_HTML)
        names = [url.rsplit("/", 1)[-1] for url in urls]
        self.assertEqual(
            names,
            ["chunk-vendors.076929bc.js", "chunk-common.79593525.js", "index.38a78496.js"],
        )

    def test_protocol_relative_urls_become_https(self):
        for url in scrape.script_urls(INDEX_HTML):
            self.assertTrue(url.startswith("https://"), url)

    def test_javascript_concatenation_is_ignored(self):
        # The document.write() src contains a '+' and spaces, so it is not a URL.
        for url in scrape.script_urls(INDEX_HTML):
            self.assertNotIn("report", url)
            self.assertNotIn("new Date", url)


class FindApkUrlTests(unittest.TestCase):
    def test_extracts_the_single_apk_url(self):
        self.assertEqual(scrape.find_apk_urls(CHUNK_COMMON), [APK_URL])

    def test_ignores_ios_and_play_links(self):
        urls = scrape.find_apk_urls(CHUNK_COMMON)
        self.assertNotIn("apps.apple.com", " ".join(urls))
        self.assertNotIn("play.google.com", " ".join(urls))

    def test_handles_escaped_slashes_from_a_minifier(self):
        escaped = 'Le="https:\\/\\/cdn.example.com\\/sirius\\/apk\\/Game_2.0.apk"'
        self.assertEqual(
            scrape.find_apk_urls(escaped), ["https://cdn.example.com/sirius/apk/Game_2.0.apk"]
        )

    def test_no_apk_means_no_match(self):
        self.assertEqual(scrape.find_apk_urls(CHUNK_VENDORS), [])


class DescribeTests(unittest.TestCase):
    def test_mirror_base_is_the_directory_holding_the_apk(self):
        # This is the value that makes the CDN-mirror trick work: F-Droid
        # appends apkName to the mirror address, so the two must join up exactly.
        link = scrape.describe(APK_URL)
        self.assertEqual(link.mirror_base + link.file_name, APK_URL)
        self.assertEqual(
            link.mirror_base, "https://l12-pkg-download.biligames.com/sirius/apk/"
        )

    def test_version_hint_comes_from_the_filename(self):
        link = scrape.describe(APK_URL)
        self.assertEqual(link.version_hint, "1.0.1")
        self.assertEqual(link.build_stamp, "2026 09 17 22 42 02")

    def test_unfamiliar_filename_yields_no_hint_rather_than_an_error(self):
        link = scrape.describe("https://cdn.example.com/sirius/apk/somethingelse.apk")
        self.assertIsNone(link.version_hint)
        self.assertIsNone(link.build_stamp)
        self.assertEqual(link.mirror_base + link.file_name,
                         "https://cdn.example.com/sirius/apk/somethingelse.apk")

    def test_rejects_a_url_that_is_not_an_apk(self):
        with self.assertRaises(scrape.ScrapeError):
            scrape.describe("https://cdn.example.com/sirius/apk/notes.txt")


class DiscoverTests(unittest.TestCase):
    """``discover`` takes an injected fetcher, so no network is involved."""

    def setUp(self):
        self.pages = {
            scrape.SITE_URL: INDEX_HTML,
            "https://s1.biligames.com/fe-static/game/gw/js/chunk-vendors.076929bc.js": CHUNK_VENDORS,
            "https://s1.biligames.com/fe-static/game/gw/js/chunk-common.79593525.js": CHUNK_COMMON,
            "https://s1.biligames.com/fe-static/game/gw/js/index.38a78496.js": "r(6179)",
        }

    def fetch(self, url):
        return self.pages[url]

    def test_discovers_the_apk_and_site_metadata(self):
        link, meta = scrape.discover(fetch=self.fetch)
        self.assertEqual(link.url, APK_URL)
        self.assertEqual(meta.title, "BanG Dream! Our Notes Official Website")
        self.assertIn("Let our sound resonate", meta.description)
        self.assertTrue(meta.og_image.endswith("share.jpg"))
        self.assertEqual(meta.play_store,
                         "https://play.google.com/store/apps/details?id=com.bilibili.sirius")
        self.assertIn("apps.apple.com", meta.app_store)
        self.assertEqual(len(meta.bundles), 3)

    def test_fails_loudly_when_no_apk_is_present(self):
        pages = dict(self.pages)
        pages["https://s1.biligames.com/fe-static/game/gw/js/chunk-common.79593525.js"] = CHUNK_VENDORS
        with self.assertRaises(scrape.ScrapeError) as caught:
            scrape.discover(fetch=pages.__getitem__)
        self.assertIn("no .apk URL", str(caught.exception))

    def test_refuses_to_guess_between_two_apks(self):
        pages = dict(self.pages)
        pages["https://s1.biligames.com/fe-static/game/gw/js/chunk-common.79593525.js"] = (
            CHUNK_COMMON
            + 'other="https://l14-pkg-download.biligames.com/sirius/apk/Other_1.0.apk"'
        )
        with self.assertRaises(scrape.ScrapeError) as caught:
            scrape.discover(fetch=pages.__getitem__)
        self.assertIn("several", str(caught.exception))

    def test_rejects_an_apk_hosted_somewhere_unexpected(self):
        pages = dict(self.pages)
        pages["https://s1.biligames.com/fe-static/game/gw/js/chunk-common.79593525.js"] = (
            'Le="https://cdn.evil.example/sirius/apk/Game_1.0.apk"'
        )
        with self.assertRaises(scrape.ScrapeError):
            scrape.discover(fetch=pages.__getitem__)

    def test_fails_when_the_page_has_no_bundles(self):
        with self.assertRaises(scrape.ScrapeError):
            scrape.discover(fetch=lambda url: "<html><body>down for maintenance</body></html>")


if __name__ == "__main__":
    unittest.main()
