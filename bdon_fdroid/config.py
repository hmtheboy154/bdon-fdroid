"""Project configuration.

Everything that a user might reasonably want to change lives in ``repo.json``
at the project root, in the same shape F-Droid itself uses for repository
settings.  Environment variables and CLI flags can override individual values so
that GitHub Actions can inject the Pages URL it derived at runtime.

No YAML parser is used anywhere in this project: ``repo.json`` is plain JSON,
which keeps the daily job free of ``pip install`` steps.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import PACKAGE_NAME, SITE_URL

DEFAULT_CONFIG = "repo.json"


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or inconsistent."""


@dataclass
class RepoSettings:
    name: str = "BanG Dream! Our Notes"
    description: str = (
        "Official Android builds of BanG Dream! Our Notes, indexed straight from "
        "the publisher's website. The APKs are downloaded from the official "
        "bilibili CDN, not from this site."
    )
    #: Full URL users add to F-Droid.  Must end in ``/fdroid/repo``.
    address: str = ""
    icon: str = "icon.jpg"
    mirror_country_code: str = "CN"
    max_age: int = 0


@dataclass
class AppSettings:
    name: str = "BanG Dream! Our Notes"
    summary: str = "GEKISO Gameplay x Adventure rhythm game"
    description: str = ""
    license: str = "Proprietary"
    categories: list[str] = field(default_factory=lambda: ["Multimedia"])
    website: str = SITE_URL
    author_name: str = "Craft Egg Inc. / bilibili"
    issue_tracker: str = ""
    source_code: str = ""
    translation: str = ""


@dataclass
class AssetSettings:
    """Where the branding images come from.

    Every field is optional. Leave them empty to have the official site scraped
    at build time; set one to pin it, which is the escape hatch when the site
    changes shape and the scraper cannot tell what an image is for.
    """

    #: Repository icon, shown in a client's repository list. Defaults to the
    #: page's ``og:image``, which is small and square.
    icon_url: str = ""
    #: The game's own icon. Scraped from the site's image assets, because the
    #: page has no ``<link rel=apple-touch-icon>`` and its only ``rel=icon`` is a
    #: 16px favicon.
    app_icon_url: str = ""
    screenshot_urls: list[str] = field(default_factory=list)
    max_screenshots: int = 4
    feature_graphic_url: str = ""


@dataclass
class Config:
    package_name: str = PACKAGE_NAME
    site_url: str = SITE_URL
    repo: RepoSettings = field(default_factory=RepoSettings)
    app: AppSettings = field(default_factory=AppSettings)
    assets: AssetSettings = field(default_factory=AssetSettings)
    keep_releases: int = 20

    def validate(self) -> "Config":
        """Check what every command needs.

        The repository address is deliberately *not* required here: `check` and
        `fetch` only talk to the publisher and work perfectly well before a
        Pages URL exists. Publishing needs it - see :meth:`require_repo_address`.
        """
        if not self.app.summary:
            raise ConfigError("app.summary must not be empty")
        return self

    def require_repo_address(self) -> "Config":
        """Check that a repository address is set, for commands that publish."""
        if not self.repo.address:
            raise ConfigError(
                "repo.address is empty. Set it in repo.json, or pass --repo-url / "
                "REPO_URL (it must be the full URL users add to F-Droid, ending "
                "in /fdroid/repo)."
            )
        if not self.repo.address.startswith(("http://", "https://")):
            raise ConfigError(
                f"repo.address must be an http(s) URL, got {self.repo.address!r}"
            )
        if not self.repo.address.rstrip("/").endswith("/fdroid/repo"):
            print(
                "warning: repo.address does not end in /fdroid/repo; some F-Droid "
                "features assume that layout"
            )
        return self


def _merge(target: dict, overrides: dict) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _from_dict(data: dict) -> Config:
    config = Config()
    repo = data.get("repo", {})
    config.repo = RepoSettings(
        name=repo.get("name", config.repo.name),
        description=repo.get("description", config.repo.description),
        address=repo.get("address", ""),
        icon=repo.get("icon", config.repo.icon),
        mirror_country_code=repo.get("mirrorCountryCode", config.repo.mirror_country_code),
        max_age=int(repo.get("maxAge", config.repo.max_age)),
    )
    app = data.get("app", {})
    config.app = AppSettings(
        name=app.get("name", config.app.name),
        summary=app.get("summary", config.app.summary),
        description=app.get("description", config.app.description),
        license=app.get("license", config.app.license),
        categories=list(app.get("categories", config.app.categories)),
        website=app.get("website", config.app.website),
        author_name=app.get("authorName", config.app.author_name),
        issue_tracker=app.get("issueTracker", ""),
        source_code=app.get("sourceCode", ""),
        translation=app.get("translation", ""),
    )
    assets = data.get("assets", {})
    config.assets = AssetSettings(
        icon_url=assets.get("iconUrl", ""),
        app_icon_url=assets.get("appIconUrl", ""),
        screenshot_urls=list(assets.get("screenshotUrls", [])),
        max_screenshots=int(assets.get("maxScreenshots", config.assets.max_screenshots)),
        feature_graphic_url=assets.get("featureGraphicUrl", ""),
    )
    config.package_name = data.get("packageName", config.package_name)
    config.site_url = data.get("siteUrl", config.site_url)
    config.keep_releases = int(data.get("keepReleases", config.keep_releases))
    return config


def _default_config_path(start: str | None = None) -> str:
    """Find ``repo.json`` by walking up from ``start`` (or the CWD)."""
    base = os.path.abspath(start or os.getcwd())
    current = base
    while True:
        candidate = os.path.join(current, DEFAULT_CONFIG)
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.join(base, DEFAULT_CONFIG)
        current = parent


def load(path: str | None = None, overrides: dict | None = None) -> Config:
    """Load configuration and apply, in increasing order of precedence:

    1. the built-in defaults,
    2. ``repo.json`` (found by walking up from ``start``),
    3. environment variables, so CI can inject what it derived at runtime,
    4. ``overrides`` (CLI flags).
    """
    path = path or _default_config_path()
    data: dict = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"could not read {path}: {exc}") from exc
    config = _from_dict(data)

    env_repo = {
        "REPO_URL": ("address", "repo"),
        "REPO_NAME": ("name", "repo"),
        "REPO_DESCRIPTION": ("description", "repo"),
        "REPO_ICON": ("icon", "repo"),
    }
    for variable, (attribute, _) in env_repo.items():
        value = os.environ.get(variable)
        if value:
            setattr(config.repo, attribute, value)
    if os.environ.get("BDON_SITE_URL"):
        config.site_url = os.environ["BDON_SITE_URL"]
    if os.environ.get("REPO_KEEP_RELEASES"):
        try:
            config.keep_releases = int(os.environ["REPO_KEEP_RELEASES"])
        except ValueError as exc:
            raise ConfigError(f"REPO_KEEP_RELEASES must be an integer: {exc}") from exc

    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key in ("package_name", "site_url", "keep_releases"):
            setattr(config, key, value)
        elif key == "repo_url":
            config.repo.address = value
        elif key == "repo_name":
            config.repo.name = value
        else:
            raise ConfigError(f"unknown configuration override: {key}")

    return config.validate()
