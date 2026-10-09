"""Discover the official APK URL from the BanG Dream! Our Notes website.

The site is a Vue single-page app, so the download link is not in the HTML.
Instead, the "Download for Android" button calls ``window.open(<url>)`` with a
string literal that lives in one of the hashed webpack bundles referenced by
``index.html``.  That literal is the only thing this module has to find.

The bundles are named ``chunk-common.<content-hash>.js`` and friends and change
name on every deploy, so nothing here hardcodes a filename: we re-parse the
``<script src>`` tags on every run.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import dataclass, field

from . import SITE_URL
from .http import HttpError, get_text, resolve

# ``src="//host/path"`` / ``src='/path'`` / ``src=/path`` in the document.
_SCRIPT_SRC = re.compile(
    r"""<script\b[^>]*?\bsrc\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s>]+))""",
    re.IGNORECASE,
)
# Any absolute URL ending in .apk. Minifiers may escape the slashes, so the
# pattern accepts "https:\/\/..." as well and the escapes are undone afterwards.
_APK_URL = re.compile(r"""https?:(?:\\?/){2}[^\s"'`<>)]+?\.apk""")
_META = re.compile(
    r"""<meta\b[^>]*?\b(?:property|name)\s*=\s*(?:"([^"]+)"|'([^']+)')[^>]*?"""
    r"""\bcontent\s*=\s*(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE,
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# The single-letter variable webpack uses for the public path is not stable
# across minifier versions, so match the assignment shape instead: ``<id>.p="..."``
# where the value looks like a URL.
_PUBLIC_PATH = re.compile(
    r"""\.\s*p\s*=\s*(?P<q>["'])(?P<path>//[^"']+|https?://[^"']+)(?P=q)"""
)
# Asset names webpack emits as "img/<file>". The hash in the middle is the
# content fingerprint and changes on every deploy, so it is never matched. The
# slashes may be escaped, as some minifiers write "img\\/icon.png".
_ICON_ASSET = re.compile(
    r"""(?<![\w./\\-])img\\?/[\w.-]*icon[\w.-]*\.(?:png|jpe?g|webp|svg)""",
    re.IGNORECASE,
)
_PLAY_STORE = re.compile(
    r"""https?:(?:\\?/){2}play\.google\.com/store/apps/details\?id=[A-Za-z0-9_.]+"""
)
_APP_STORE = re.compile(
    r"""https?:(?:\\?/){2}apps\.apple\.com(?:\\?/)[A-Za-z0-9/%._-]*?id\d+"""
)
# The publisher moved the download link out of the bundle and into their remote
# config service. The site still *asks* for it at runtime, from code shaped like:
#
#   k = new f$({appKey:"555.187", nscode:24, apiURL:"kv1.biligames.com"})
#   k.getGroup("apklink").then(e => { link = e.link })
#
# So find the call, then the client that was constructed for it. The receiver is
# matched too, because the bundles construct several clients for other
# namespaces and picking the wrong one would ask the wrong question.
_CONFIG_CALL = re.compile(
    r"""(?P<recv>[A-Za-z_$][\w$]*)\s*\.\s*getGroup\(\s*["']apklink["']\s*\)"""
)
_CONFIG_ARGS = re.compile(
    r"""appKey\s*:\s*["'](?P<app_key>[^"']+)["']"""
    r""".{0,80}?nscode\s*:\s*(?P<nscode>\d+)"""
    r""".{0,80}?apiURL\s*:\s*["'](?P<api_url>[^"']+)["']""",
    re.S,
)
#: Path the SDK reads a namespace from. Undocumented and internal, so it is
#: matched as a shape rather than assumed to be stable.
_CONFIG_PATH = "/x/kv-frontend/namespace/data"

# The CDN filenames look like ``BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02.apk``.
_APK_FILENAME = re.compile(
    r"^(?P<app>.+?)_(?P<version>[0-9][0-9A-Za-z._-]*)_"
    r"(?P<stamp>\d{4}(?:_\d{2}){5})\.apk$"
)

# The nearest-preceding client matters: the bundles construct several, one per
# namespace, and the wrong one answers a different question.
_CONFIG_CONSTRUCT = re.compile(
    r"""(?P<recv>[A-Za-z_$][\w$]*)\s*=\s*new\s+[\w$.]+\s*\(\s*\{(?P<args>[^{}]*)\}"""
)


# Only JS from the game's own static host is worth scanning.
_ALLOWED_HOST_SUFFIXES = ("biligames.com",)


class ScrapeError(RuntimeError):
    """Raised when the APK link cannot be identified with confidence."""


@dataclass(frozen=True)
class ApkLink:
    """A discovered APK download URL."""

    url: str
    file_name: str
    version_hint: str | None
    build_stamp: str | None
    mirror_base: str

    @property
    def etag_key(self) -> str:
        """Stable identity for a release: the URL itself."""
        return self.url


@dataclass
class SiteMetadata:
    """Whatever we can learn about the site without executing its JavaScript."""

    title: str = ""
    description: str = ""
    og_image: str = ""
    app_icon: str = ""
    play_store: str = ""
    app_store: str = ""
    bundles: list[str] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "ogImage": self.og_image,
            "appIcon": self.app_icon,
            "playStore": self.play_store,
            "appStore": self.app_store,
        }


def _clean(meta_url: str) -> str:
    return html.unescape(meta_url).strip()


def _allowed(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_HOST_SUFFIXES)


def parse_metadata(document: str) -> SiteMetadata:
    """Pull the interesting bits out of the site's ``<head>``."""
    meta = SiteMetadata()
    for match in _META.finditer(document):
        key = (match.group(1) or match.group(2) or "").strip().lower()
        value = _clean(match.group(3) or match.group(4) or "")
        if not key or not value:
            continue
        if key in ("description", "og:description"):
            meta.description = meta.description or value
        elif key == "og:title" and not meta.title:
            meta.title = value
        elif key == "og:image":
            meta.og_image = value
        elif key not in meta.extra:
            meta.extra[key] = value

    title = _TITLE.search(document)
    if title and not meta.title:
        meta.title = _clean(title.group(1))
    return meta


def script_urls(document: str, base_url: str = SITE_URL) -> list[str]:
    """Return the absolute URLs of every static ``<script src>`` in the document.

    The page also builds cache-busting URLs with ``document.write('<script
    src="..." + Date.now() + "">')``.  Those never resolve to a real file, so
    anything that is not a plain absolute http(s) URL is discarded.
    """
    urls: list[str] = []
    for match in _SCRIPT_SRC.finditer(document):
        raw = _clean(match.group(1) or match.group(2) or match.group(3) or "")
        if not raw or any(char.isspace() for char in raw) or "+" in raw:
            continue
        absolute = resolve(base_url, raw)
        if absolute.split("?")[0].endswith(".js") and _is_fetchable(absolute):
            if absolute not in urls:
                urls.append(absolute)
    return urls


def _is_fetchable(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def find_apk_urls(bundle: str) -> list[str]:
    """Return every distinct absolute ``.apk`` URL inside a JS bundle."""
    found: list[str] = []
    for raw in _APK_URL.findall(bundle):
        # Minifiers escape forward slashes; undo that before matching.
        candidate = raw.replace("\\/", "/")
        if candidate not in found:
            found.append(candidate)
    return found


def _collect_store_links(bundle: str, metadata: SiteMetadata) -> None:
    """Record the Google Play / App Store links, which live in the bundle too."""
    if not metadata.play_store:
        found = _PLAY_STORE.search(bundle)
        if found:
            metadata.play_store = found.group(0).replace("\\/", "/")
    if not metadata.app_store:
        found = _APP_STORE.search(bundle)
        if found:
            metadata.app_store = found.group(0).replace("\\/", "/")


def describe(url: str) -> ApkLink:
    """Turn an APK URL into an :class:`ApkLink`."""
    parts = urllib.parse.urlsplit(url)
    file_name = parts.path.rsplit("/", 1)[-1]
    if not file_name.endswith(".apk"):
        raise ScrapeError(f"download URL does not end in .apk: {url}")

    match = _APK_FILENAME.match(file_name)
    version_hint = match.group("version") if match else None
    build_stamp = match.group("stamp").replace("_", " ") if match else None

    # The mirror base is the directory holding the APK.  F-Droid resolves an
    # APK's ``apkName`` relative to the mirror address, so this must be exactly
    # the directory containing the file.
    mirror_base = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path.rsplit("/", 1)[0] + "/", "", "")
    )
    return ApkLink(
        url=url,
        file_name=file_name,
        version_hint=version_hint,
        build_stamp=build_stamp,
        mirror_base=mirror_base,
    )



class ConfigEndpoint:
    """The publisher's remote-config endpoint, as read out of their JavaScript."""

    def __init__(self, api_url: str, app_key: str, nscode: str):
        self.api_url = api_url
        self.app_key = app_key
        self.nscode = nscode

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ConfigEndpoint(api_url={self.api_url!r}, app_key={self.app_key!r}, "
            f"nscode={self.nscode!r})"
        )

    def url(self) -> str:
        """The request that returns this namespace's data."""
        base = self.api_url
        if "://" not in base:
            base = "https://" + base  # the bundles write it without a scheme
        query = urllib.parse.urlencode(
            {"appKey": self.app_key, "nscode": self.nscode, "unlimit": "true"}
        )
        return f"{base.rstrip('/')}{_CONFIG_PATH}?{query}"


def find_config_endpoint(bundle_bodies: list[str]) -> ConfigEndpoint | None:
    """Find the remote-config endpoint the site reads the APK link from.

    Returns ``None`` when the bundles contain no such call, which is the normal
    case for a site that hardcodes the link instead. This is not an API in any
    documented sense: it is the publisher's internal feature-flag backend, read
    out of their own bundle rather than taken on trust. There is no version
    list, no history and no enumeration - only a URL to whatever is current.
    """
    for body in bundle_bodies:
        for call in _CONFIG_CALL.finditer(body):
            receiver = call.group("recv")
            constructor = re.compile(
                re.escape(receiver)
                + r"""\s*=\s*new\s+[\w$.]+\s*\(\s*\{(?P<args>[^{}]*)\}"""
            )
            # The client built for this call is the nearest one before it; the
            # bundles construct several, one per namespace, and the others
            # answer unrelated questions.
            chosen = None
            for match in constructor.finditer(body):
                if match.end() <= call.start():
                    chosen = match
            if chosen is None:
                continue
            args = _CONFIG_ARGS.search(chosen.group("args"))
            if args:
                return ConfigEndpoint(
                    api_url=args.group("api_url"),
                    app_key=args.group("app_key"),
                    nscode=args.group("nscode"),
                )
    return None


def fetch_config_apk_url(endpoint: ConfigEndpoint, fetch=get_text) -> tuple[str, bool]:
    """Read ``apklink.link`` from the endpoint.

    Returns the URL and whether the publisher's own switch says the direct APK is
    currently offered at all. That switch is a remote kill switch: if it goes to
    ``false`` the link may still resolve while the site has stopped advertising
    it, which is worth saying out loud rather than silently indexing.
    """
    try:
        payload = json.loads(fetch(endpoint.url()))
    except (HttpError, ValueError) as exc:
        raise ScrapeError(f"could not read {endpoint.url()}: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("code") not in (0, "0"):
        raise ScrapeError(
            f"{endpoint.url()} did not return config data: "
            f"{payload.get('code') if isinstance(payload, dict) else payload!r} "
            f"{payload.get('message') if isinstance(payload, dict) else ''}".strip()
        )
    data = payload.get("data", {})
    values = data.get("data", {}) if isinstance(data, dict) else {}
    link = values.get("apklink.link")
    if not isinstance(link, str) or not link:
        raise ScrapeError(
            f"{endpoint.url()} returned no apklink.link; keys were "
            + ", ".join(sorted(values)) or "(none)"
        )
    switch = values.get("switch.isopen")
    return link, switch in ("true", True, "True")


def _discover_via_config(
    site_url: str,
    fetch,
    pinned: "ConfigEndpoint | None",
    bundles: list[str],
    bundle_bodies: list[str],
    metadata: SiteMetadata,
) -> tuple[ApkLink, SiteMetadata]:
    """Fall back to the publisher's remote config for the APK link.

    Only reached when no bundle carries a literal, so the failure message that
    used to end the run now describes both sources.
    """
    endpoint = pinned or find_config_endpoint(bundle_bodies)
    if endpoint is None:
        raise ScrapeError(
            f"no .apk URL found in any bundle of {site_url} "
            f"({len(bundles)} scanned), and the bundles declare no remote-config "
            "endpoint to ask. The download button has moved somewhere this scraper "
            "does not know about; set configEndpoint in repo.json if you know where."
        )

    print(f"  no .apk URL in any bundle; asking {endpoint.api_url} instead")
    apk_url, enabled = fetch_config_apk_url(endpoint, fetch)
    if not enabled:
        print(
            "  warning: the publisher's own switch.isopen is not 'true'; the direct "
            "APK may stop being offered at any time, and this index will stop updating."
        )
    if not _allowed(apk_url):
        raise ScrapeError(f"APK URL is not on a biligames.com host: {apk_url}")

    metadata.app_icon = find_app_icon(bundle_bodies)
    if metadata.app_icon:
        print(f"  app icon: {metadata.app_icon.rsplit('/', 1)[-1]}")
    else:
        print("  app icon: not found; falling back to the configured icon")
    return describe(apk_url), metadata


def discover(
    site_url: str = SITE_URL,
    fetch=get_text,
    config_endpoint: "ConfigEndpoint | None" = None,
) -> tuple[ApkLink, SiteMetadata]:
    """Fetch the site, scan its bundles, and return the APK link + metadata.

    ``fetch`` is injectable so tests can run against a saved copy of the site.

    Two sources, in order. The APK link used to be a string literal in one of
    the site's bundles; it now arrives from the publisher's remote-config
    service instead, and the visible Android button points at Google Play. The
    literal is still tried first, because if the publisher ever hardcodes it
    again that is the more direct answer, and because a bundle that *does* carry
    the link needs no second request.
    """
    try:
        document = fetch(site_url)
    except HttpError as exc:
        raise ScrapeError(f"could not fetch {site_url}: {exc}") from exc

    metadata = parse_metadata(document)
    bundles = script_urls(document, site_url)
    if not bundles:
        raise ScrapeError(f"no <script src> bundles found on {site_url}")
    metadata.bundles = bundles

    bundle_bodies: list[str] = []
    candidates: dict[str, str] = {}  # apk url -> bundle that mentioned it
    for bundle_url in bundles:
        if not _allowed(bundle_url):
            continue
        try:
            body = fetch(bundle_url)
        except HttpError as exc:
            print(f"  warning: skipping {bundle_url}: {exc}")
            continue
        bundle_bodies.append(body)
        for apk_url in find_apk_urls(body):
            candidates.setdefault(apk_url, bundle_url)
        _collect_store_links(body, metadata)

    if not candidates:
        return _discover_via_config(site_url, fetch, config_endpoint, bundles, bundle_bodies,
                                    metadata)

    if len(candidates) > 1:
        listing = "\n  ".join(sorted(candidates))
        raise ScrapeError(
            "expected exactly one .apk URL but found several; refusing to guess:\n  " + listing
        )

    apk_url, bundle_url = next(iter(candidates.items()))
    if not _allowed(apk_url):
        raise ScrapeError(f"APK URL is not on a biligames.com host: {apk_url}")

    link = describe(apk_url)
    print(f"  found in {bundle_url.rsplit('/', 1)[-1]}")

    metadata.app_icon = find_app_icon(bundle_bodies)
    if metadata.app_icon:
        print(f"  app icon: {metadata.app_icon.rsplit('/', 1)[-1]}")
    else:
        print("  app icon: not found; falling back to the configured icon")

    return link, metadata


def find_app_icon(bundle_bodies: list[str]) -> str:
    """Find the app's own icon among the site's image assets, or return "".

    The site has no ``<link rel=apple-touch-icon>`` and its only ``<link
    rel=icon>`` is a 16px ``favicon.ico``, so the real icon is not in the HTML.
    It is a webpack asset, referenced by name from a bundle and resolved against
    the public path, e.g.::

        ga = t.p + "img/icon.e419ff81.png"   where  t.p = "//s1.../gw/"

    Both the public path and the asset name are read from the bundles, so this
    follows the site through content-hash renames without hardcoding anything.
    Ambiguity is treated as failure rather than a guess: picking the wrong image
    would publish a wrong app icon to every client that syncs the repository.
    """
    public_path = _webpack_public_path(bundle_bodies)
    if not public_path:
        return ""
    candidates = set()
    for body in bundle_bodies:
        # Undo any minifier escaping before resolving against the public path.
        candidates.update(name.replace("\\/", "/") for name in _ICON_ASSET.findall(body))
    if not candidates:
        return ""
    if len(candidates) > 1:
        raise ScrapeError(
            "found several candidate app icons in the site's bundles; refusing to "
            "guess. Set assets.appIconUrl in repo.json to pin one.\n  "
            + "\n  ".join(sorted(candidates))
        )
    name = candidates.pop()
    absolute = "https:" + public_path if public_path.startswith("//") else public_path
    return urllib.parse.urljoin(absolute, name)


def _webpack_public_path(bundle_bodies: list[str]) -> str:
    """Return the public path webpack resolves asset names against."""
    paths: list[str] = []
    for body in bundle_bodies:
        paths.extend(match.group("path") for match in _PUBLIC_PATH.finditer(body))
    unique = set(paths)
    if not unique:
        return ""
    if len(unique) > 1:
        # Several roots would be a bundle split across hosts; the icon could
        # live under any of them, so decline rather than pick one.
        raise ScrapeError(
            "the site's bundles declare several public paths; set "
            "assets.appIconUrl in repo.json to pin the icon.\n  "
            + "\n  ".join(sorted(unique))
        )
    return unique.pop()

