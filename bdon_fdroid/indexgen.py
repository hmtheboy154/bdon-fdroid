"""Generate the F-Droid repository index files.

``fdroid update`` builds these, but it insists on having the APK on local disk
and ``fdroid deploy`` would then copy a 446 MB file into the repository.  We only
want the *index*, pointing at a CDN we do not control, so the files are emitted
here instead.

Three documents are produced, and each is also wrapped in a signed JAR by
:mod:`bdon_fdroid.signing`:

``index-v1.json``
    Still needed by F-Droid 1.x clients.  Carries the app metadata in ``apps``
    and one record per version in ``packages``.
``index-v2.json``
    The current format: localised metadata plus a version map keyed by SHA-256.
``entry.json``
    The signed entry point naming ``index-v2.json`` and its hash.

The one non-obvious decision is how the APK is addressed.  F-Droid resolves an
APK's ``apkName`` by *appending it to a mirror address* (``Mirror.getUrl`` ->
``URLBuilder.appendPathSegments``), so an absolute URL in the index is silently
mangled and cannot be used.  We therefore hand the official CDN directory to the
client as a mirror and use the CDN's own filename as the ``apkName``, making
``<mirror>/<apkName>`` the exact upstream URL.  The game is then downloaded
straight from the publisher without us ever storing it.  Paths the CDN does not
have answer ``403``, so the client falls back to the Pages-hosted repository for
the index, icon and screenshots.

File layout inside the repository follows what ``fdroidclient`` expects when it
reads the v1 format, where icon names are resolved as ``/icons/<name>`` and
screenshots as ``/<package>/<locale>/phoneScreenshots/<name>``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field

from .apk import Release
from .config import Config

INDEX_V1 = "index-v1.json"
INDEX_V2 = "index-v2.json"
ENTRY = "entry.json"
INDEX_FILES = (INDEX_V1, INDEX_V2, ENTRY)
LOCALE = "en-US"

#: Bumped when the generated repository layout changes.  F-Droid refuses to
#: downgrade a repository to an older format, so this only ever goes up.
FORMAT_VERSION = 30000


class RepoBuildError(RuntimeError):
    """Raised when the repository cannot be generated."""


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Asset:
    """An image copied into the repository.

    ``name`` is the path relative to the repository root, chosen to match what
    ``fdroidclient`` expects for its kind (icon -> ``icons/...``, screenshot ->
    ``<package>/<locale>/phoneScreenshots/...``).
    """

    name: str
    source: str | None = None
    data: bytes | None = None

    def payload(self) -> bytes:
        if self.data is not None:
            return self.data
        if not self.source:
            raise RepoBuildError(f"asset {self.name} has neither data nor a source")
        from .http import get_bytes

        return get_bytes(self.source)

    def write(self, root: str) -> dict:
        """Write the file under ``root`` and return its index-v2 file entry."""
        payload = self.payload()
        path = os.path.join(root, self.name)
        os.makedirs(os.path.dirname(path) or root, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(payload)
        return {
            "name": "/" + self.name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }

    @property
    def basename(self) -> str:
        return self.name.rsplit("/", 1)[-1]


@dataclass
class Assets:
    """The images published alongside the index."""

    repo_icon: Asset | None = None
    app_icon: Asset | None = None
    feature_graphic: Asset | None = None
    screenshots: list[Asset] = field(default_factory=list)

    def write_all(self, root: str) -> "Assets":
        for asset in [self.repo_icon, self.app_icon, self.feature_graphic, *self.screenshots]:
            if asset is not None:
                asset.write(root)
        return self

    def v1_icon(self) -> str | None:
        return self.app_icon.basename if self.app_icon else None

    def v2_icon(self, entry: dict) -> dict | None:
        return {LOCALE: entry} if self.app_icon else None

    def v1_localized(self) -> dict:
        """The ``localized`` block: how v1 clients locate screenshots."""
        localized: dict = {}
        if self.feature_graphic:
            localized["featureGraphic"] = self.feature_graphic.basename
        if self.screenshots:
            localized["phoneScreenshots"] = [a.basename for a in self.screenshots]
        return {LOCALE: localized} if localized else {}

    def v2_screenshots(self, entries: list[dict]) -> dict | None:
        return {"phone": {LOCALE: entries}} if self.screenshots else None

    def v2_feature_graphic(self, entry: dict) -> dict | None:
        return {LOCALE: entry} if self.feature_graphic else None


def _dump(document: dict) -> bytes:
    """Serialise deterministically so an unchanged run yields no git diff."""
    text = json.dumps(document, indent=2, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def _localized(text: str) -> dict:
    return {LOCALE: text}


def build_mirrors(config: Config, latest: Release) -> list[dict]:
    """The official CDN directory, expressed as an F-Droid mirror."""
    mirror: dict = {"url": latest.mirror_base}
    if config.repo.mirror_country_code:
        # Lets clients in the publisher's region prefer this mirror.
        mirror["countryCode"] = config.repo.mirror_country_code
    return [mirror]


def build_index_v1(
    config: Config,
    releases: list[Release],
    assets: Assets,
    icon_entry: dict | None = None,
    screenshot_entries: list[dict] | None = None,
) -> dict:
    """The legacy index.  ``apps`` carries metadata, ``packages`` one entry per
    version."""
    del icon_entry, screenshot_entries  # v1 has no hashes/screenshots; names only
    latest = releases[0]
    app = config.app

    app_entry: dict = {
        "packageName": config.package_name,
        "name": app.name,
        "license": app.license,
        "categories": sorted(app.categories),
        "webSite": app.website,
        "authorName": app.author_name,
        "added": latest.added,
        "lastUpdated": latest.added,
        "suggestedVersionName": latest.version_name,
        "suggestedVersionCode": str(latest.version_code),
    }
    if app.summary:
        app_entry["summary"] = app.summary
    if app.description:
        app_entry["description"] = app.description
    for key, value in (
        ("issueTracker", app.issue_tracker),
        ("sourceCode", app.source_code),
        ("translation", app.translation),
    ):
        if value:
            app_entry[key] = value
    if assets.v1_icon():
        app_entry["icon"] = assets.v1_icon()
    if assets.v1_localized():
        app_entry["localized"] = assets.v1_localized()

    versions = []
    for release in releases:
        entry: dict = {
            "apkName": release.file_name,
            "hash": release.sha256,
            "hashType": "sha256",
            "packageName": config.package_name,
            "size": release.size,
            "versionCode": release.version_code,
            "versionName": release.version_name,
            "added": release.added,
        }
        if release.signer:
            entry["signer"] = release.signer
        if release.min_sdk_version is not None:
            entry["minSdkVersion"] = release.min_sdk_version
        if release.target_sdk_version is not None:
            entry["targetSdkVersion"] = release.target_sdk_version
        if release.permissions:
            entry["uses-permission"] = [{"name": name} for name in release.permissions]
        if release.features:
            entry["features"] = release.features
        versions.append(entry)

    repo: dict = {
        "name": config.repo.name,
        "address": config.repo.address,
        "description": config.repo.description,
        "icon": config.repo.icon,
        "timestamp": now_ms(),
        "version": FORMAT_VERSION,
        # v1 clients read the mirror list from here and nowhere else.
        "mirrors": [mirror["url"] for mirror in build_mirrors(config, latest)],
    }
    if config.repo.max_age:
        repo["maxage"] = config.repo.max_age

    return {
        "repo": repo,
        "requests": {"install": [], "uninstall": []},
        "apps": [app_entry],
        "packages": {config.package_name: versions},
    }


def build_index_v2(
    config: Config,
    releases: list[Release],
    assets: Assets | None = None,
    icon_entry: dict | None = None,
    feature_entry: dict | None = None,
    screenshot_entries: list[dict] | None = None,
) -> dict:
    """The current index: localised metadata plus a version map keyed by hash."""
    del assets
    latest = releases[0]
    app = config.app

    metadata: dict = {
        "name": _localized(app.name),
        "added": latest.added,
        "lastUpdated": latest.added,
    }
    if app.summary:
        metadata["summary"] = _localized(app.summary)
    if app.description:
        metadata["description"] = _localized(app.description)
    metadata["license"] = app.license
    metadata["categories"] = sorted(app.categories)
    metadata["webSite"] = app.website
    metadata["authorName"] = app.author_name
    for key, value in (
        ("issueTracker", app.issue_tracker),
        ("sourceCode", app.source_code),
        ("translation", app.translation),
    ):
        if value:
            metadata[key] = value
    if icon_entry:
        metadata["icon"] = icon_entry
    if feature_entry:
        metadata["featureGraphic"] = feature_entry
    if screenshot_entries:
        metadata["screenshots"] = {"phone": {LOCALE: screenshot_entries}}
    if latest.signer:
        # Lets the client confirm the APK really came from the publisher.
        metadata["preferredSigner"] = latest.signer

    versions: dict[str, dict] = {}
    for release in releases:
        manifest: dict = {
            "versionName": release.version_name,
            "versionCode": release.version_code,
        }
        if release.min_sdk_version is not None or release.target_sdk_version is not None:
            manifest["usesSdk"] = {
                "minSdkVersion": release.min_sdk_version or 1,
                "targetSdkVersion": release.target_sdk_version or release.min_sdk_version or 1,
            }
        if release.signer:
            manifest["signer"] = {"sha256": [release.signer]}
        if release.permissions:
            manifest["usesPermission"] = [{"name": name} for name in release.permissions]
        if release.features:
            manifest["features"] = [{"name": name} for name in release.features]

        versions[release.sha256] = {
            "added": release.added,
            "file": {
                # Relative to the mirror; see the module docstring.
                "name": "/" + release.file_name,
                "sha256": release.sha256,
                "size": release.size,
            },
            "manifest": manifest,
        }

    repo = {
        "name": _localized(config.repo.name),
        "address": config.repo.address,
        "description": _localized(config.repo.description),
        "mirrors": build_mirrors(config, latest),
        "timestamp": now_ms(),
    }
    if config.repo.max_age:
        repo["maxAge"] = config.repo.max_age

    return {
        "repo": repo,
        "packages": {config.package_name: {"metadata": metadata, "versions": versions}},
    }


def build_entry(index_v2: bytes, num_packages: int) -> dict:
    """The signed entry point that names the real index."""
    return {
        "timestamp": now_ms(),
        "version": FORMAT_VERSION,
        "index": {
            "name": "/" + INDEX_V2,
            "sha256": hashlib.sha256(index_v2).hexdigest(),
            "size": len(index_v2),
            "numPackages": num_packages,
        },
        "diffs": {},
    }


def _ensure_releases(releases: list[Release]) -> list[Release]:
    if not releases:
        raise RepoBuildError(
            "no releases to publish; run 'bdon-fdroid fetch' or check the scraper first"
        )
    return sorted(releases, key=Release.sort_key)


def write_repository(
    root: str,
    config: Config,
    releases: list[Release],
    assets: Assets | None = None,
) -> dict:
    """Write every index file and asset under ``root``.

    Returns a summary dict for the CLI and the landing page.
    """
    releases = _ensure_releases(releases)
    assets = assets or Assets()
    os.makedirs(root, exist_ok=True)

    # Materialise the assets first so their hashes can go into the index.
    icon_entry = None
    screenshot_entries: list[dict] = []
    feature_entry = None
    for asset in [assets.repo_icon, assets.app_icon, assets.feature_graphic]:
        if asset is not None:
            entry = asset.write(root)
            if asset is assets.app_icon:
                icon_entry = {LOCALE: entry}
            elif asset is assets.feature_graphic:
                feature_entry = {LOCALE: entry}
    for asset in assets.screenshots:
        screenshot_entries.append(asset.write(root))

    index_v1 = build_index_v1(config, releases, assets)
    index_v2 = build_index_v2(
        config, releases, assets, icon_entry, feature_entry, screenshot_entries
    )
    entry = build_entry(_dump(index_v2), 1)

    files = {INDEX_V1: _dump(index_v1), INDEX_V2: _dump(index_v2), ENTRY: _dump(entry)}
    for name, payload in files.items():
        with open(os.path.join(root, name), "wb") as handle:
            handle.write(payload)

    published = [
        asset.name
        for asset in [assets.repo_icon, assets.app_icon, assets.feature_graphic, *assets.screenshots]
        if asset is not None
    ]
    return {
        "root": root,
        "files": files,
        "assets": published,
        "releases": releases,
        "latest": releases[0],
        "numPackages": 1,
        "signerFingerprint": None,
    }
