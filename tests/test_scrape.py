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


# --- the remote-config source ------------------------------------------------
#
# Trimmed from the live bundle of 2026-10-05, when the publisher moved the APK
# link out of the JavaScript and behind their feature-flag service. Note the two
# clients: the bundles construct one per namespace, so only the *nearest*
# preceding one is the one to ask.

CONFIG_CHUNK_COMMON = (
    'var h=new mo.f$({appKey:"555.187",nscode:20,strict:mo.er.STRICT,'
    'apiURL:"kv1.biligames.com"});h.getGroup("switch").then(e=>{u.value=e});'
    'const b=(0,Z.KR)(!1),'
    'k=new mo.f$({appKey:"555.187",nscode:24,apiURL:"kv1.biligames.com"});'
    'k.getGroup("switch").then(e=>{b.value="true"===e.isopen||!0===e.isopen});'
    'const y=(0,Z.KR)("");'
    'k.getGroup("apklink").then(e=>{y.value=e.link,console.log("apkLink",y.value)})'
)

CONFIG_RESPONSE = (
    '{"code":0,"message":"","data":{"versionId":"1791513743282",'
    '"appVersionId":"39117","disableUseLocalCache":false,"data":{'
    '"apklink.link":"https://l14-pkg-download.biligames.com/sirius/apk/'
    'BanGDreamOurNotes_1.0.3_2026_10_02_22_46_55.apk",'
    '"switch.isopen":"true"}}}'
)

CONFIG_APK_URL = (
    "https://l14-pkg-download.biligames.com/sirius/apk/"
    "BanGDreamOurNotes_1.0.3_2026_10_02_22_46_55.apk"
)


class ConfigEndpointDiscoveryTests(unittest.TestCase):
    """Read the endpoint out of the bundle rather than hardcoding it."""

    def test_finds_the_app_key_namespace_and_host(self):
        endpoint = scrape.find_config_endpoint([CONFIG_CHUNK_COMMON])
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.app_key, "555.187")
        self.assertEqual(endpoint.nscode, "24")
        self.assertEqual(endpoint.api_url, "kv1.biligames.com")

    def test_asks_the_namespace_the_apklink_call_belongs_to(self):
        # The bundle builds a client for nscode 20 first. Asking that one would
        # return an unrelated group, so proximity to the call is what matters.
        endpoint = scrape.find_config_endpoint([CONFIG_CHUNK_COMMON])
        self.assertNotEqual(endpoint.nscode, "20")

    def test_takes_the_nearest_client_when_one_receiver_is_reused(self):
        # Minified code reuses identifiers, so matching on the receiver alone is
        # not enough - the *last* assignment before the call is the one in scope.
        bundle = (
            'k=new mo.f$({appKey:"555.187",nscode:24,apiURL:"kv1.biligames.com"});'
            "k.getGroup('other').then(()=>{});"
            'k=new mo.f$({appKey:"555.187",nscode:31,apiURL:"kv1.biligames.com"});'
            'k.getGroup("apklink").then(e=>{y=e.link})'
        )
        self.assertEqual(scrape.find_config_endpoint([bundle]).nscode, "31")

    def test_will_not_borrow_a_client_belonging_to_something_else(self):
        # A client constructed for a different receiver, nearest the call, must
        # not be used: it would answer for another namespace.
        bundle = (
            'k=new mo.f$({appKey:"555.187",nscode:24,apiURL:"kv1.biligames.com"});'
            "k.getGroup('other').then(()=>{});"
            'zz=new mo.f$({appKey:"999.9",nscode:88,apiURL:"kv9.example.com"});'
            'k.getGroup("apklink").then(e=>{y=e.link})'
        )
        endpoint = scrape.find_config_endpoint([bundle])
        self.assertEqual(endpoint.nscode, "24")
        self.assertEqual(endpoint.api_url, "kv1.biligames.com")

    def test_builds_a_request_without_a_scheme_in_the_host(self):
        url = scrape.find_config_endpoint([CONFIG_CHUNK_COMMON]).url()
        self.assertTrue(url.startswith("https://kv1.biligames.com/"), url)
        self.assertIn("appKey=555.187", url)
        self.assertIn("nscode=24", url)
        self.assertIn("/x/kv-frontend/namespace/data", url)

    def test_an_explicit_scheme_is_left_alone(self):
        endpoint = scrape.ConfigEndpoint(
            api_url="https://kv1.example.com", app_key="k", nscode="1"
        )
        self.assertTrue(endpoint.url().startswith("https://kv1.example.com/"))

    def test_no_such_call_means_no_endpoint(self):
        self.assertIsNone(scrape.find_config_endpoint([CHUNK_COMMON, CHUNK_VENDORS]))

    def test_no_bundles_means_no_endpoint(self):
        self.assertIsNone(scrape.find_config_endpoint([]))

    def test_an_unrelated_constructor_is_not_borrowed(self):
        # A client that never has an apklink call beside it must not be used.
        bundle = (
            'k=new mo.f$({appKey:"999.1",nscode:77,apiURL:"kv9.example.com"});'
            'other.getGroup("somethingelse").then(()=>{})'
        )
        self.assertIsNone(scrape.find_config_endpoint([bundle]))


class FetchConfigTests(unittest.TestCase):
    def test_reads_the_link_and_the_switch(self):
        endpoint = scrape.ConfigEndpoint("kv1.biligames.com", "555.187", "24")
        link, enabled = scrape.fetch_config_apk_url(endpoint, lambda url: CONFIG_RESPONSE)
        self.assertEqual(link, CONFIG_APK_URL)
        self.assertTrue(enabled)

    def test_reports_the_switch_when_the_publisher_has_turned_it_off(self):
        response = CONFIG_RESPONSE.replace('"switch.isopen":"true"', '"switch.isopen":"false"')
        endpoint = scrape.ConfigEndpoint("kv1.biligames.com", "555.187", "24")
        _link, enabled = scrape.fetch_config_apk_url(endpoint, lambda url: response)
        self.assertFalse(enabled)

    def test_rejects_a_response_that_is_not_config(self):
        endpoint = scrape.ConfigEndpoint("kv1.biligames.com", "555.187", "24")
        with self.assertRaises(scrape.ScrapeError) as caught:
            scrape.fetch_config_apk_url(endpoint, lambda url: '{"code":-304}')
        self.assertIn("-304", str(caught.exception))

    def test_rejects_a_response_with_no_link(self):
        endpoint = scrape.ConfigEndpoint("kv1.biligames.com", "555.187", "24")
        payload = '{"code":0,"data":{"data":{"switch.isopen":"true"}}}'
        with self.assertRaises(scrape.ScrapeError) as caught:
            scrape.fetch_config_apk_url(endpoint, lambda url: payload)
        self.assertIn("apklink.link", str(caught.exception))

    def test_rejects_a_body_that_is_not_json(self):
        endpoint = scrape.ConfigEndpoint("kv1.biligames.com", "555.187", "24")
        with self.assertRaises(scrape.ScrapeError):
            scrape.fetch_config_apk_url(endpoint, lambda url: "<html>502</html>")


class DiscoverConfigFallbackTests(unittest.TestCase):
    """``discover`` must fall through to the config service, not give up."""

    def setUp(self):
        self.pages = {
            scrape.SITE_URL: INDEX_HTML,
            "https://s1.biligames.com/fe-static/game/gw/js/chunk-vendors.076929bc.js": CHUNK_VENDORS,
            "https://s1.biligames.com/fe-static/game/gw/js/chunk-common.79593525.js": CONFIG_CHUNK_COMMON,
            "https://s1.biligames.com/fe-static/game/gw/js/index.38a78496.js": "r(6179)",
        }

    def fetch(self, url):
        if url.startswith("https://kv1.biligames.com/"):
            return CONFIG_RESPONSE
        return self.pages[url]

    def test_finds_the_new_release_through_the_config_service(self):
        link, _meta = scrape.discover(fetch=self.fetch)
        self.assertEqual(link.url, CONFIG_APK_URL)
        self.assertEqual(link.version_hint, "1.0.3")
        self.assertEqual(
            link.mirror_base, "https://l14-pkg-download.biligames.com/sirius/apk/"
        )

    def test_a_literal_still_wins_over_the_config_service(self):
        # The old arrangement must keep working, so a publisher that hardcodes
        # the link again needs no second request.
        self.pages[
            "https://s1.biligames.com/fe-static/game/gw/js/chunk-common.79593525.js"
        ] = CONFIG_CHUNK_COMMON + f'Le="{APK_URL}"'

        def fetch(url):
            if url.startswith("https://kv1.biligames.com/"):
                raise AssertionError("asked the config service despite a literal")
            return self.pages[url]

        link, _meta = scrape.discover(fetch=fetch)
        self.assertEqual(link.url, APK_URL)

    def test_fails_loudly_when_neither_source_has_a_link(self):
        pages = dict(self.pages)
        pages["https://s1.biligames.com/fe-static/game/gw/js/chunk-common.79593525.js"] = (
            CHUNK_VENDORS
        )
        with self.assertRaises(scrape.ScrapeError) as caught:
            scrape.discover(fetch=pages.__getitem__)
        message = str(caught.exception)
        self.assertIn("no .apk URL", message)
        self.assertIn("remote-config", message)

    def test_a_pinned_endpoint_is_used_when_discovery_would_fail(self):
        pages = dict(self.pages)
        pages["https://s1.biligames.com/fe-static/game/gw/js/chunk-common.79593525.js"] = (
            CHUNK_VENDORS
        )
        pinned = scrape.ConfigEndpoint("kv1.biligames.com", "555.187", "24")
        link, _meta = scrape.discover(
            fetch=self.fetch, config_endpoint=pinned
        )
        self.assertEqual(link.url, CONFIG_APK_URL)

    def test_the_turned_off_switch_is_reported_and_the_link_still_indexed(self):
        # A remote kill switch: the URL keeps resolving, so nothing would fail,
        # and the index would quietly stop updating one day. The warning is the
        # only signal, so it has to actually be printed.
        import contextlib
        import io

        payload = CONFIG_RESPONSE.replace(
            '"switch.isopen":"true"', '"switch.isopen":"false"'
        )

        def fetch(url):
            if url.startswith("https://kv1.biligames.com/"):
                return payload
            return self.pages[url]

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            link, _meta = scrape.discover(fetch=fetch)
        self.assertEqual(link.url, CONFIG_APK_URL)
        self.assertIn("switch.isopen", buffer.getvalue())

    def test_no_warning_when_the_switch_is_on(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            scrape.discover(fetch=self.fetch)
        self.assertNotIn("switch.isopen", buffer.getvalue())

    def test_a_config_url_off_the_publishers_hosts_is_rejected(self):
        payload = CONFIG_RESPONSE.replace(
            "https://l14-pkg-download.biligames.com",
            "https://cdn.evil.example",
        )
        pages = dict(self.pages)

        def fetch(url):
            if url.startswith("https://kv1.biligames.com/"):
                return payload
            return pages[url]

        with self.assertRaises(scrape.ScrapeError):
            scrape.discover(fetch=fetch)


if __name__ == "__main__":
    unittest.main()
