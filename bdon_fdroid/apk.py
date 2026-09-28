"""Inspect an official APK without keeping it.

The download link points at bilibili/biligames' own CDN, so this project never
stores the game.  But the F-Droid index needs a few facts that can only come
from the APK itself:

* the SHA-256, which is mandatory in both index formats and which the client
  verifies on download,
* ``versionCode``, which F-Droid uses to decide whether an update exists and
  which is *not* derivable from the URL,
* the min/target SDK, requested permissions and the signing certificate.

The APK is only ever streamed to a temporary file, which is deleted immediately
afterwards.  The per-release facts are cached in ``releases.json`` so that a
routine run - where nothing has changed - never downloads 446 MB at all.
"""

from __future__ import annotations

import os
import tempfile
import time
import zipfile
from dataclasses import dataclass, field

from . import PACKAGE_NAME, axml
from .apksigner_info import signing_certificate_sha256
from .http import HeadInfo, HttpError, download, head
from .scrape import ApkLink, ScrapeError

# Fields of the cached record that identify the *remote object* rather than its
# contents.  If none of these change we can trust the cached SHA-256.
_REMOTE_FIELDS = ("size", "etag", "last_modified")


class ApkError(RuntimeError):
    """Raised when the downloaded APK is not what we expect."""


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Release:
    """Everything the index needs to publish one version of the game."""

    url: str
    file_name: str
    size: int
    sha256: str
    version_code: int
    version_name: str
    added: int
    mirror_base: str
    signer: str | None = None
    min_sdk_version: int | None = None
    target_sdk_version: int | None = None
    permissions: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    version_hint: str | None = None
    build_stamp: str | None = None

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "fileName": self.file_name,
            "size": self.size,
            "sha256": self.sha256,
            "versionCode": self.version_code,
            "versionName": self.version_name,
            "added": self.added,
            "mirrorBase": self.mirror_base,
            "signer": self.signer,
            "minSdkVersion": self.min_sdk_version,
            "targetSdkVersion": self.target_sdk_version,
            "permissions": self.permissions,
            "features": self.features,
            "etag": self.etag,
            "lastModified": self.last_modified,
            "versionHint": self.version_hint,
            "buildStamp": self.build_stamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Release":
        return cls(
            url=data["url"],
            file_name=data["fileName"],
            size=int(data["size"]),
            sha256=data["sha256"],
            version_code=int(data["versionCode"]),
            version_name=data.get("versionName", ""),
            added=int(data["added"]),
            mirror_base=data["mirrorBase"],
            signer=data.get("signer"),
            min_sdk_version=data.get("minSdkVersion"),
            target_sdk_version=data.get("targetSdkVersion"),
            permissions=list(data.get("permissions", [])),
            features=list(data.get("features", [])),
            etag=data.get("etag"),
            last_modified=data.get("lastModified"),
            version_hint=data.get("versionHint"),
            build_stamp=data.get("buildStamp"),
        )

    # -- comparison ------------------------------------------------------
    def remote_fingerprint(self) -> tuple:
        """Identify the object on the CDN, ignoring its contents."""
        return tuple(getattr(self, name) for name in _REMOTE_FIELDS)

    def sort_key(self) -> tuple:
        """Newest first, with the URL as a stable tiebreaker."""
        return (-self.version_code, -self.added, self.url)


def _looks_like_apk(path: str) -> None:
    with open(path, "rb") as handle:
        if handle.read(4) != b"PK\x03\x04":
            raise ApkError("downloaded file is not a ZIP archive, so not an APK")


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _progress_printer(label: str):
    """Return a callback that prints download progress at most every few seconds."""
    state = {"bytes": 0, "last": 0.0}

    def report(delta: int, total: int | None) -> None:
        state["bytes"] += delta
        now = time.time()
        if total:
            if now - state["last"] < 5 and state["bytes"] < total:
                return
            state["last"] = now
            percent = state["bytes"] * 100 // total
            print(
                f"  {label}: {percent}% "
                f"({_human(state['bytes'])} / {_human(total)})",
                flush=True,
            )
        elif now - state["last"] >= 15:
            state["last"] = now
            print(f"  {label}: {_human(state['bytes'])}", flush=True)

    return report


def fetch_release(
    link: ApkLink,
    info: HeadInfo,
    added: int | None = None,
    expected_package: str = PACKAGE_NAME,
) -> Release:
    """Download ``link`` and extract everything needed to publish it.

    The APK is streamed into a temporary directory and the whole directory is
    removed before this returns, on every path. The 450 MB file exists only
    long enough to be hashed and have its manifest read.

    ``expected_package`` is a guard: if the scraped link ever resolves to a
    different application, the build fails rather than publishing a repository
    that installs the wrong thing.
    """
    print(f"  downloading {_human(info.size or 0)} from {link.file_name} ...")
    progress = _progress_printer("download")

    with tempfile.TemporaryDirectory(prefix="bdon-fdroid-") as directory:
        path = os.path.join(directory, "release.apk")
        sha256, size = download(link.url, path, info.size, progress)
        _looks_like_apk(path)
        with zipfile.ZipFile(path) as archive:
            manifest = axml.parse_manifest(archive.read("AndroidManifest.xml"))
        signer = signing_certificate_sha256(path)

    if manifest.package != expected_package:
        raise ApkError(
            f"expected package {expected_package} but the APK at {link.file_name} "
            f"declares {manifest.package!r}; refusing to publish. If the "
            f"publisher changed which build the site serves, update "
            f"packageName in repo.json deliberately."
        )
    if not isinstance(manifest.version_code, int):
        # Some builds use a resource reference, which we cannot resolve without
        # resources.arsc.  Fail loudly rather than publishing a version the
        # F-Droid client cannot compare against.
        raise ApkError(
            "could not read versionCode from AndroidManifest.xml "
            f"(versionName={manifest.version_name!r}); "
            "F-Droid requires an integer versionCode"
        )

    print(
        f"  versionCode={manifest.version_code} versionName={manifest.version_name!r} "
        f"sha256={sha256[:16]}... signer={(signer or 'unknown')[:16]}"
    )
    return Release(
        url=link.url,
        file_name=link.file_name,
        size=size,
        sha256=sha256,
        version_code=manifest.version_code,
        version_name=manifest.version_name or link.version_hint or "",
        added=added if added is not None else _now_ms(),
        mirror_base=link.mirror_base,
        signer=signer,
        min_sdk_version=manifest.min_sdk_version,
        target_sdk_version=manifest.target_sdk_version,
        permissions=manifest.permissions,
        features=manifest.features,
        etag=info.etag,
        last_modified=info.last_modified,
        version_hint=link.version_hint,
        build_stamp=link.build_stamp,
    )


def probe(link: ApkLink) -> HeadInfo:
    """``HEAD`` the discovered APK URL."""
    try:
        info = head(link.url)
    except HttpError as exc:
        raise ScrapeError(f"could not query the APK URL: {exc}") from exc
    print(
        f"  remote object: {_human(info.size or 0)}"
        + (f" etag={info.etag}" if info.etag else "")
        + (" (supports resume)" if info.supports_ranges else "")
    )
    return info
