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
# The CDN filenames look like ``BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02.apk``.
_APK_FILENAME = re.compile(
    r"^(?P<app>.+?)_(?P<version>[0-9][0-9A-Za-z._-]*)_"
    r"(?P<stamp>\d{4}(?:_\d{2}){5})\.apk$"
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


def discover(site_url: str = SITE_URL, fetch=get_text) -> tuple[ApkLink, SiteMetadata]:
    """Fetch the site, scan its bundles, and return the APK link + metadata.

    ``fetch`` is injectable so tests can run against a saved copy of the site.
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
        raise ScrapeError(
            "no .apk URL found in any bundle of "
            f"{site_url} ({len(bundles)} bundle(s) scanned). "
            "The download button may have moved to a different file."
        )
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

