"""Command line interface.

Subcommands, roughly in the order a newcomer would use them::

    bdon-fdroid check     # scrape only: what does the site say right now?
    bdon-fdroid fetch     # scrape and, if new, download + inspect the APK
    bdon-fdroid build     # fetch, then write and sign the whole repository
    bdon-fdroid serve     # serve the build output for local testing
    bdon-fdroid verify    # check a built repository against fdroidserver
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import SITE_URL, __version__
from .apk import ApkError, fetch_release, probe
from .config import Config, ConfigError, load as load_config
from .http import HttpError
from .indexgen import LOCALE, Asset, Assets, RepoBuildError, write_repository
from .landing import write as write_landing
from .scrape import ConfigEndpoint, ScrapeError, discover
from .signing import KeyStore, SigningError, sign_index_files
from .state import State, load as load_state, save as save_state

DEFAULT_STATE = "releases.json"
DEFAULT_DEPLOY = "deploy"
#: Where the repository itself lives inside the published site.  F-Droid's own
#: docs and several client features expect the address to end in /fdroid/repo.
REPO_SUBDIR = os.path.join("fdroid", "repo")

#: Image type to file extension. Anything not listed here is skipped rather than
#: written under a guessed extension, since a client loading a mislabelled file
#: may simply show a broken image.
EXTENSION_BY_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/gif": ".gif",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
}


def _out(*parts: str) -> None:
    print(*parts, flush=True)


def _load(args) -> Config:
    return load_config(
        args.config,
        overrides={
            "repo_url": args.repo_url,
            "repo_name": args.repo_name,
            "site_url": args.site_url,
            "keep_releases": args.keep_releases,
        },
    )


def _keystore(args) -> KeyStore | None:
    """Build a :class:`KeyStore` from flags/environment, or ``None``."""
    path = args.keystore or os.environ.get("KEYSTORE_PATH")
    alias = args.key_alias or os.environ.get("REPO_KEYALIAS")
    password = args.key_pass or os.environ.get("KEYSTORE_PASS")
    if not path and not alias and not password:
        return None
    missing = [
        name
        for name, value in (
            ("KEYSTORE_PATH", path),
            ("REPO_KEYALIAS", alias),
            ("KEYSTORE_PASS", password),
        )
        if not value
    ]
    if missing:
        raise SigningError(
            "signing needs " + ", ".join(missing) + " (see docs/setup-github-actions-and-pages.md)"
        )
    return KeyStore(path, alias, password)


def _pinned_endpoint(config) -> "ConfigEndpoint | None":
    """Turn a hand-set ``configEndpoint`` block into an endpoint, if it is set."""
    pinned = config.config_endpoint
    if not (pinned.app_key and pinned.nscode and pinned.api_url):
        return None
    return ConfigEndpoint(
        api_url=pinned.api_url, app_key=pinned.app_key, nscode=pinned.nscode
    )

def _scrape(args) -> tuple[Config, object, object]:
    config = _load(args)
    _out(f"scraping {config.site_url} ...")
    link, site = discover(
        config.site_url,
        config_endpoint=_pinned_endpoint(config) or None,
    )
    return config, link, site


def _refresh_state(args, config, link, state: State) -> tuple[State, bool]:
    """Decide whether the APK needs downloading, and update the state.

    Returns ``(state, changed)``.  ``changed`` is True when a new release was
    discovered, which is the signal to rebuild and redeploy.
    """
    info = probe(link)
    known = state.find(link.url)

    if state.is_stale:
        # The rules for reading an APK changed, so anything cached was parsed
        # under the old ones and cannot be reused. Re-read the APK once.
        print(
            "  the release cache was written by an older manifest parser; "
            "re-reading the APK so its metadata is correct"
        )
        known = None

    if known is not None and known.remote_fingerprint() == (
        info.size,
        info.etag,
        info.last_modified,
    ):
        _out("  unchanged: cached hash still matches the remote object")
        state.last_checked = int(time.time() * 1000)
        return state, False

    if known is not None and not args.force_download:
        # The URL is the same but the remote object changed under it.  Trusting
        # the cached hash would let the client install a different file than the
        # one we verified, so re-download rather than publish a stale hash.
        _out("  remote object changed for a known URL; re-verifying by downloading")

    release = fetch_release(
        link,
        info,
        added=known.added if known else None,
        expected_package=config.package_name,
    )
    state.upsert(release)
    state.last_checked = int(time.time() * 1000)
    state.last_changed = state.last_checked
    return state, True


def _asset(url: str, stem: str) -> Asset | None:
    """Download an image into memory, choosing the extension from its type.

    The extension must come from the response's ``Content-Type``, not from the
    URL: the Google Play screenshot URLs end in ``?w=2560-h1440-rw`` with no
    filename at all, and they serve ``image/webp``, so a URL-derived name would
    have written WebP bytes into a ``.jpg``.

    Best effort throughout: a repository with no icon is still valid, so a
    failure here is reported and skipped rather than failing the build.
    """
    if not url:
        return None
    from .http import get_bytes_with_type

    try:
        data, content_type = get_bytes_with_type(url)
    except HttpError as exc:
        _out(f"  warning: could not fetch asset {url}: {exc}")
        return None
    if not data:
        _out(f"  warning: asset {url} was empty, skipping")
        return None
    extension = EXTENSION_BY_TYPE.get(
        (content_type or "").split(";")[0].strip().lower(), ""
    )
    if not extension:
        _out(f"  warning: unrecognised image type {content_type!r} for {url}")
        return None
    return Asset(name=stem + extension, data=data)


def _build_assets(config, site) -> Assets:
    """Pick the repository images out of what the site advertises.

    Two different icons on purpose. The *repository* icon is the small
    ``og:image``, which every client fetches once when it syncs; the *app* icon
    is the game's own artwork, which only a client that opens the app's page
    needs. The site's own icon is over 1 MB, which is why the two are not
    simply the same file.
    """
    assets = Assets()
    package = config.package_name

    if config.assets.icon_url:
        assets.repo_icon = _asset(config.assets.icon_url, "icon")
    elif site.og_image:
        assets.repo_icon = _asset(site.og_image, "icon")

    # fdroidclient resolves a v1 app icon as /icons/<name>, so the file has to
    # live in icons/ for older clients to find it.
    icon_source = config.assets.app_icon_url or site.app_icon
    if icon_source:
        assets.app_icon = _asset(icon_source, f"icons/{package}")

    for index, url in enumerate(
        config.assets.screenshot_urls[: config.assets.max_screenshots], start=1
    ):
        asset = _asset(url, f"{package}/{LOCALE}/phoneScreenshots/{index}")
        if asset is not None:
            assets.screenshots.append(asset)
    return assets


def cmd_check(args) -> int:
    """Scrape the site and report, without downloading anything."""
    config, link, site = _scrape(args)
    info = probe(link)
    _out("")
    _out("  APK URL     :", link.url)
    _out("  file name   :", link.file_name)
    _out("  size        :", info.size)
    _out("  mirror base :", link.mirror_base)
    if link.version_hint:
        _out("  version hint:", link.version_hint, "(from the filename)")
    if link.build_stamp:
        _out("  build stamp :", link.build_stamp)

    state = load_state(args.state)
    known = state.find(link.url)
    if known:
        _out("")
        _out("  cached      : versionCode", known.version_code, known.version_name)
        _out("  sha256      :", known.sha256)
        if known.remote_fingerprint() == (info.size, info.etag, info.last_modified):
            _out("  -> no action needed")
        else:
            _out("  -> the remote object changed; a rebuild would re-download it")
    else:
        _out("  -> new release; a build would download and inspect the APK")
    del site, config
    return 0


def cmd_fetch(args) -> int:
    """Scrape and, when something is new, download and inspect the APK."""
    config, link, site = _scrape(args)
    del site
    state = load_state(args.state)
    state, changed = _refresh_state(args, config, link, state)
    dropped = state.prune(config.keep_releases)
    for release in dropped:
        _out(f"  pruned old release {release.version_name} ({release.file_name})")
    if save_state(args.state, state):
        _out(f"  wrote {args.state}")
    if changed:
        latest = state.latest()
        _out(f"  new release: {latest.version_name} (versionCode {latest.version_code})")
    return 0


def _write_build_info(
    path: str, config, state, summary, fingerprint, site, changed_this_run: bool
) -> None:
    """Record what was published, for machines rather than humans.

    ``changed`` says whether *this* run discovered a new release, which is not
    the same question as "when did the history last change".
    """
    latest = state.latest()
    info = {
        "generator": f"bdon-fdroid/{__version__}",
        "packageName": config.package_name,
        "repoAddress": config.repo.address,
        "repoUrlWithFingerprint": (
            f"{config.repo.address}?fingerprint={fingerprint}" if fingerprint else None
        ),
        "signerFingerprint": fingerprint,
        "signed": bool(fingerprint),
        "numPackages": summary["numPackages"],
        "builtAt": int(time.time() * 1000),
        "changed": bool(changed_this_run),
        "checkedAt": state.last_checked,
        "changedAt": state.last_changed,
        "latest": {
            "versionName": latest.version_name,
            "versionCode": latest.version_code,
            "size": latest.size,
            "sha256": latest.sha256,
            "url": latest.url,
            "mirrorBase": latest.mirror_base,
        } if latest else None,
        "releases": [
            {
                "versionName": release.version_name,
                "versionCode": release.version_code,
                "sha256": release.sha256,
                "size": release.size,
                "url": release.url,
            }
            for release in summary["releases"]
        ],
        "site": {
            "website": config.app.website,
            "playStore": getattr(site, "play_store", ""),
            "appStore": getattr(site, "app_store", ""),
        },
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def cmd_build(args) -> int:
    """Fetch, then write and sign the complete repository."""
    config, link, site = _scrape(args)
    config.require_repo_address()
    state = load_state(args.state)
    state, changed = _refresh_state(args, config, link, state)
    dropped = state.prune(config.keep_releases)
    for release in dropped:
        _out(f"  pruned old release {release.version_name}")
    save_state(args.state, state)

    releases = state.sorted_releases()
    if not releases:
        raise RepoBuildError("nothing to publish: no releases known")

    deploy = args.deploy
    os.makedirs(deploy, exist_ok=True)
    repo_root = os.path.join(deploy, REPO_SUBDIR)
    os.makedirs(repo_root, exist_ok=True)

    keystore = _keystore(args)
    fingerprint = keystore.fingerprint() if keystore else None
    if keystore is None:
        _out("  no keystore configured: the repository will NOT be signed")
    else:
        _out(f"  signing key fingerprint: {fingerprint}")

    assets = _build_assets(config, site)
    summary = write_repository(repo_root, config, releases, assets)

    if keystore is not None:
        sign_index_files(repo_root, summary["files"], keystore)
        summary["signerFingerprint"] = fingerprint

    write_landing(
        os.path.join(deploy, "index.html"), config, state, fingerprint, site
    )
    # GitHub Pages would otherwise run Jekyll, which drops files it dislikes.
    with open(os.path.join(deploy, ".nojekyll"), "w", encoding="utf-8") as handle:
        handle.write("")

    # A small machine-readable description of what was published, so other
    # tooling (including this project's own workflow) does not have to scrape
    # the HTML or shell out to keytool.
    _write_build_info(
        os.path.join(deploy, "repo-info.json"),
        config=config,
        state=state,
        summary=summary,
        fingerprint=fingerprint,
        site=site,
        changed_this_run=changed,
    )

    _out("")
    _out(f"  repository  : {config.repo.address}")
    _out(f"  versions    : {len(releases)}")
    for release in releases:
        _out(f"    - {release.version_name} (versionCode {release.version_code}) {release.sha256[:16]}...")
    _out(f"  assets      : {len(summary['assets'])}")
    _out(f"  output      : {deploy}")
    if not changed:
        _out("  (nothing new upstream; the index was refreshed anyway)")
    return 0


def cmd_serve(args) -> int:
    """Serve the build output so a real F-Droid URL can be pointed at it."""
    import http.server
    import socketserver

    root = os.path.abspath(args.deploy)
    if not os.path.isdir(root):
        _out(f"error: {root} does not exist; run 'bdon-fdroid build' first")
        return 1

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *handler_args, **kwargs):
            super().__init__(*handler_args, directory=root, **kwargs)

        def log_message(self, fmt, *log_args):  # quieter output
            _out("  " + (fmt % log_args))

    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        _out(f"serving {root} on http://127.0.0.1:{args.port}/")
        _out("press Ctrl-C to stop")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            _out("\nstopped")
    return 0


def cmd_verify(args) -> int:
    """Validate a built repository with fdroidserver, if it is installed."""
    repo_url = args.repo_url
    if not repo_url:
        _out("error: --repo-url is required (the address users add to F-Droid)")
        return 2
    try:
        import shutil

        from fdroidserver import common, index  # type: ignore
    except ImportError:
        _out(
            "fdroidserver is not installed, so the repository cannot be verified.\n"
            "Install it in a throwaway virtualenv, e.g.:\n"
            "  python3 -m venv /tmp/fdcheck && /tmp/fdcheck/bin/pip install fdroidserver"
        )
        return 2

    fingerprint = args.fingerprint
    if not fingerprint:
        keystore = _keystore(args)
        if keystore is None:
            _out("error: pass --fingerprint or configure a keystore to read it from")
            return 2
        fingerprint = keystore.fingerprint()
    fingerprint = fingerprint.strip().lower()

    # fdroidserver reads jarsigner's location from its own config; we only need
    # its signature verification, so point it at the JDK on PATH.
    common.config = {
        "jarsigner": shutil.which("jarsigner"),
        "keytool": shutil.which("keytool"),
        "apksigner": None,
    }
    url = f"{repo_url.rstrip('/')}?fingerprint={fingerprint}"
    _out(f"verifying {repo_url} against fingerprint {fingerprint[:16]}...")

    failures = 0
    try:
        v2, _ = index.download_repo_index_v2(url, verify_fingerprint=fingerprint)
        _out("  index-v2.json: signature and hash verified")
        packages = v2.get("packages", {})
        _out(f"  packages: {', '.join(sorted(packages)) or 'none'}")
        for package_name, package in packages.items():
            for version in package.get("versions", {}).values():
                _out(
                    f"    {package_name} versionCode="
                    f"{version['manifest']['versionCode']} "
                    f"file={version['file']['name']}"
                )
    except Exception as exc:  # noqa: BLE001 - report whatever went wrong
        _out(f"  index-v2.json FAILED: {exc}")
        failures += 1

    try:
        result = index.download_repo_index_v1(url, verify_fingerprint=fingerprint)
        v1 = result[0] if isinstance(result, tuple) else result
        _out("  index-v1.json: signature verified")
        _out(f"  mirrors: {', '.join(v1['repo'].get('mirrors', [])) or 'none'}")
    except Exception as exc:  # noqa: BLE001
        _out(f"  index-v1.json FAILED: {exc}")
        failures += 1

    if failures:
        _out(f"\n{failures} check(s) failed")
        return 1
    _out("\nall checks passed")
    return 0


def cmd_dump(args) -> int:
    """Print the local state as JSON; handy when debugging."""
    state = load_state(args.state)
    _out(json.dumps(state.to_dict(), indent=2, ensure_ascii=False))
    return 0


def _add_common(parser: argparse.ArgumentParser, use_suppress: bool) -> None:
    """Options accepted both before and after the subcommand.

    The top level parser and the subcommands get *separate* action objects,
    because ``parents=`` shares them by reference and ``set_defaults`` mutates
    them in place.  The subcommands' copies default to ``argparse.SUPPRESS`` so
    that when argparse copies a subparser's namespace onto the main one, an
    option the user did not type after the subcommand does not overwrite a value
    they gave before it.
    """

    def default(value):
        return argparse.SUPPRESS if use_suppress else value

    parser.add_argument("--config", default=default(None),
                        help="path to repo.json (default: found by walking up)")
    parser.add_argument("--state", default=default(DEFAULT_STATE),
                        help=f"release cache (default: {DEFAULT_STATE})")
    parser.add_argument("--repo-url", default=default(None),
                        help="repository address users add to F-Droid")
    parser.add_argument("--repo-name", default=default(None),
                        help="display name of the repository")
    parser.add_argument("--site-url", default=default(None),
                        help=f"official site to scrape (default: {SITE_URL})")
    parser.add_argument("--keep-releases", type=int, default=default(None),
                        help="how many versions to keep in the index")
    parser.add_argument("--deploy", default=default(DEFAULT_DEPLOY),
                        help=f"output directory (default: {DEFAULT_DEPLOY})")
    parser.add_argument("--force-download", action="store_true",
                        default=default(False),
                        help="re-download the APK even if the cached hash looks current")
    parser.add_argument("--keystore", default=default(None),
                        help="PKCS#12 keystore (or set KEYSTORE_PATH)")
    parser.add_argument("--key-alias", default=default(None),
                        help="keystore alias (or set REPO_KEYALIAS)")
    parser.add_argument("--key-pass", default=default(None),
                        help="keystore password (or set KEYSTORE_PASS)")


def build_parser() -> argparse.ArgumentParser:
    # Separate action objects per level; see _add_common.
    top = argparse.ArgumentParser(add_help=False)
    _add_common(top, use_suppress=False)
    shared = argparse.ArgumentParser(add_help=False)
    _add_common(shared, use_suppress=True)

    parser = argparse.ArgumentParser(
        prog="bdon-fdroid",
        parents=[top],
        description=(
            "Scrape the official BanG Dream! Our Notes website and publish a "
            "static F-Droid repository. The APK is never stored: the index points "
            "at the publisher's CDN, which is registered as a F-Droid mirror."
        ),
        epilog=(
            "Examples:\n"
            "  bdon-fdroid check\n"
            "      Report what the official site currently offers, downloading nothing.\n"
            "  bdon-fdroid build --keystore keystore.p12 --key-alias bdon-fdroid\n"
            "      Fetch if needed, then write and sign deploy/ ready for GitHub Pages.\n"
            "  bdon-fdroid verify --repo-url <published address> --fingerprint <hex>\n"
            "      Check a published repository against fdroidserver.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"bdon-fdroid {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", parents=[shared],
                           help="scrape the site and report, downloading nothing")
    check.set_defaults(func=cmd_check)

    fetch = sub.add_parser("fetch", parents=[shared],
                           help="scrape and inspect the APK if it is new")
    fetch.set_defaults(func=cmd_fetch)

    build = sub.add_parser("build", parents=[shared],
                           help="fetch, then write and sign the whole repository")
    build.set_defaults(func=cmd_build)

    serve = sub.add_parser("serve", parents=[shared],
                           help="serve the output directory locally")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    verify = sub.add_parser("verify", parents=[shared],
                            help="validate a repository using fdroidserver")
    verify.add_argument("--fingerprint", help="expected repository key fingerprint")
    verify.set_defaults(func=cmd_verify)

    dump = sub.add_parser("dump", parents=[shared],
                          help="print the cached release state as JSON")
    dump.set_defaults(func=cmd_dump)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse arguments and dispatch, turning expected failures into
    a non-zero exit code with a readable message rather than a traceback."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        ApkError,
        ConfigError,
        HttpError,
        RepoBuildError,
        ScrapeError,
        SigningError,
    ) as exc:
        _out(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        _out("\ninterrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
