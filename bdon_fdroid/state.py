"""The committed cache of every release we have ever seen.

``releases.json`` is the project's memory.  It lets a routine run answer "has
anything changed?" with a single ``HEAD`` request instead of re-downloading the
APK, and it keeps the published index carrying every historical version so users
can still downgrade.

The file is written deterministically (sorted, fixed key order, trailing
newline) so an unchanged run produces an empty git diff.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import PACKAGE_NAME, __version__
from .apk import Release

STATE_VERSION = 1

#: Bump whenever the rules for interpreting an APK change, so a cache written by
#: an older version is not trusted.
#:
#: This is not hypothetical: an earlier version synthesised a
#: "glEsVersionNNNNNN" feature and ignored android:required="false", which made
#: F-Droid mark the app incompatible with devices that could run it. The cached
#: releases.json still held the bad list, so the fix appeared to do nothing
#: until the APK was re-read. With this version recorded, that cannot happen
#: again: stale entries stop matching and the APK is fetched once more.
MANIFEST_RULES_VERSION = 2


@dataclass
class State:
    """All known releases plus a little provenance for the landing page."""

    releases: list[Release]
    last_checked: int | None = None
    last_changed: int | None = None
    manifest_rules: int = MANIFEST_RULES_VERSION

    @property
    def is_stale(self) -> bool:
        """True when the cached entries were produced by different rules."""
        return self.manifest_rules != MANIFEST_RULES_VERSION

    def sorted_releases(self) -> list[Release]:
        return sorted(self.releases, key=Release.sort_key)

    def latest(self) -> Release | None:
        ordered = self.sorted_releases()
        return ordered[0] if ordered else None

    def find(self, url: str) -> Release | None:
        for release in self.releases:
            if release.url == url:
                return release
        return None

    def upsert(self, release: Release) -> None:
        """Insert ``release``, replacing any record with the same URL."""
        for index, existing in enumerate(self.releases):
            if existing.url == release.url:
                self.releases[index] = release
                return
        self.releases.append(release)

    def prune(self, keep: int) -> list[Release]:
        """Drop the oldest releases beyond ``keep``; return what was dropped."""
        if keep <= 0 or len(self.releases) <= keep:
            return []
        ordered = self.sorted_releases()
        dropped = ordered[keep:]
        self.releases = ordered[:keep]
        return dropped

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "stateVersion": STATE_VERSION,
            "generator": f"bdon-fdroid/{__version__}",
            "packageName": PACKAGE_NAME,
            "manifestRules": MANIFEST_RULES_VERSION,
            "lastChecked": self.last_checked,
            "lastChanged": self.last_changed,
            "releases": [release.to_dict() for release in self.sorted_releases()],
        }


def load(path: str) -> State:
    """Read ``releases.json``; an absent or unreadable file means "no history"."""
    if not os.path.exists(path):
        return State(releases=[])
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: {path} is not readable JSON ({exc}); fix or delete it")

    if data.get("stateVersion") != STATE_VERSION:
        raise SystemExit(
            f"error: {path} has stateVersion {data.get('stateVersion')!r}, "
            f"expected {STATE_VERSION}"
        )
    try:
        releases = [Release.from_dict(item) for item in data.get("releases", [])]
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"error: {path} is missing or has malformed release data: {exc}")
    return State(
        releases=releases,
        last_checked=data.get("lastChecked"),
        last_changed=data.get("lastChanged"),
        manifest_rules=int(data.get("manifestRules", 1)),
    )


def save(path: str, state: State) -> bool:
    """Write ``state`` to ``path``; return ``True`` if the file changed."""
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=False, ensure_ascii=False)
    payload += "\n"
    existing = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            existing = handle.read()
    if existing == payload:
        return False
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return True
