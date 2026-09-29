"""The landing page published on GitHub Pages.

The repository itself is just a handful of JSON files, so the root of the
``gh-pages`` branch is a small hand-written page that tells a human what to do
with it: which URL to add in F-Droid, which fingerprint to check it against, and
what the current version is.

It is plain HTML with inline CSS so the Pages site has no build step and no
external requests, and it is regenerated from the same state that produces the
index so it can never disagree with what F-Droid will install.
"""

from __future__ import annotations

import datetime
import html
import os

from .apk import Release
from .config import Config
from .scrape import SiteMetadata
from .state import State

_CSS = """
:root { color-scheme: light dark; --fg: #1c1c1e; --bg: #fbfbfd; --muted: #6b6b73;
        --line: #e2e2e7; --accent: #b5179e; --code: #f2f2f5; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #ececf0; --bg: #131316; --muted: #9a9aa4; --line: #2c2c33;
          --code: #1c1c21; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 2.5rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
       font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 52rem; margin: 0 auto; }
h1 { font-size: 1.9rem; margin: 0 0 .25rem; line-height: 1.2; }
h2 { font-size: 1.15rem; margin: 2.25rem 0 .75rem; }
a { color: var(--accent); }
p.tagline { color: var(--muted); margin: 0 0 2rem; }
.card { border: 1px solid var(--line); border-radius: 12px; padding: 1.1rem 1.25rem;
        background: color-mix(in srgb, var(--code) 45%, transparent); }
.endpoint { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: .95rem; word-break: break-all; }
.endpoint a { word-break: break-all; }
.copy-row { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .85rem; }
button.copy { font: inherit; font-size: .85rem; cursor: pointer; padding: .3rem .7rem;
              border-radius: 6px; border: 1px solid var(--line); background: var(--bg);
              color: var(--fg); }
button.copy:hover { border-color: var(--accent); color: var(--accent); }
dl { display: grid; grid-template-columns: max-content 1fr; gap: .35rem 1.25rem; margin: 0; }
dt { color: var(--muted); }
dd { margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
     font-size: .9rem; word-break: break-all; }
table { border-collapse: collapse; width: 100%; font-size: .92rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
td.mono, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                 font-size: .85rem; }
ul.files { list-style: none; padding: 0; margin: 0; display: grid; gap: .3rem; }
ul.files li { display: flex; justify-content: space-between; gap: 1rem;
              border-bottom: 1px solid var(--line); padding: .3rem 0; }
ul.files span.desc { color: var(--muted); }
.note { color: var(--muted); font-size: .9rem; }
footer { margin-top: 3rem; padding-top: 1.25rem; border-top: 1px solid var(--line);
         color: var(--muted); font-size: .85rem; }
.status { display: inline-block; padding: .05rem .5rem; border-radius: 999px;
          font-size: .78rem; border: 1px solid var(--line); }
"""

_SCRIPT = """
function copyText(button) {
  const target = document.getElementById(button.dataset.target);
  navigator.clipboard.writeText(target.textContent.trim()).then(() => {
    const original = button.textContent;
    button.textContent = 'Copied';
    setTimeout(() => { button.textContent = original; }, 1400);
  });
}
"""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _human(size: int | None) -> str:
    if not size:
        return "unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _when(millis: int | None) -> str:
    if not millis:
        return "unknown"
    stamp = datetime.datetime.fromtimestamp(millis / 1000, datetime.timezone.utc)
    return stamp.strftime("%Y-%m-%d %H:%M UTC")


def _copy_row(identifier: str, label: str) -> str:
    return (
        f'<div class="copy-row">'
        f'<button class="copy" data-target="{identifier}" '
        f'onclick="copyText(this)">Copy</button>'
        f"<span class=\"note\">{label}</span></div>"
    )


def render(
    config: Config,
    state: State,
    fingerprint: str | None = None,
    site: SiteMetadata | None = None,
) -> str:
    """Render the landing page as a complete HTML document."""
    latest = state.latest()
    repo_url = config.repo.address
    fingerprint = (fingerprint or "").strip().lower()
    with_fingerprint = f"{repo_url}?fingerprint={fingerprint}" if fingerprint else repo_url
    fdroid_link = f"https://fdroid.link/#{repo_url}?fingerprint={fingerprint}" if fingerprint else ""

    parts: list[str] = []
    parts.append(
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(config.repo.name)} &middot; F-Droid repository</title>\n"
        f'<meta name="description" content="{_esc(config.app.summary)}">\n'
        f"<style>{_CSS}</style>\n"
        f"</head>\n<body>\n<main>\n"
    )
    parts.append(f"<h1>{_esc(config.app.name)}</h1>")
    # The heading is the game's real name, because that is what a visitor came
    # for. The claim that this is *unofficial* belongs in the tagline: the APKs
    # are BILIBILI's own builds, but nothing here is built or re-signed by us.
    parts.append(
        f'<p class="tagline">An unofficial, automatically-updated F-Droid index '
        f'for {_esc(config.app.name)}, built from the publisher&apos;s own website.</p>'
    )

    # --- the endpoint ----------------------------------------------------
    parts.append("<h2>Add this repository to F-Droid</h2>")
    parts.append('<div class="card">')
    parts.append(
        f'<div class="endpoint" id="repo-url"><a href="{_esc(with_fingerprint)}">'
        f"{_esc(with_fingerprint)}</a></div>"
    )
    parts.append(_copy_row("repo-url", "F-Droid client &rarr; Settings &rarr; Repositories"))
    if fdroid_link:
        parts.append(
            f'<p class="note" style="margin-top:.9rem">Or open this on your phone: '
            f'<a href="{_esc(fdroid_link)}">{_esc(fdroid_link)}</a></p>'
        )
    parts.append("</div>")

    # --- signing key -----------------------------------------------------
    parts.append("<h2>Repository signing key</h2>")
    if fingerprint:
        parts.append('<div class="card">')
        parts.append("<dl>")
        parts.append(
            '<dt>SHA-256 fingerprint</dt>'
            f'<dd id="fingerprint">{_esc(fingerprint.upper())}</dd>'
        )
        parts.append("</dl>")
        parts.append(_copy_row("fingerprint", "compare with what F-Droid shows"))
        parts.append("</div>")
    else:
        parts.append(
            '<p class="note">No signing key fingerprint is available for this build.</p>'
        )

    # --- current version -------------------------------------------------
    parts.append("<h2>Latest version</h2>")
    if latest is not None:
        parts.append('<div class="card"><dl>')
        rows = [
            ("Version", f"{latest.version_name} (versionCode {latest.version_code})"),
            ("Package", config.package_name),
            ("Filename", latest.file_name),
            ("Size", _human(latest.size)),
            ("SHA-256", latest.sha256),
            ("First seen", _when(latest.added)),
            ("Last checked", _when(state.last_checked)),
        ]
        if latest.min_sdk_version:
            rows.append(("Android", f"API {latest.min_sdk_version}+"))
        if latest.signer:
            rows.append(("Signed by", latest.signer))
        for label, value in rows:
            parts.append(f"<dt>{_esc(label)}</dt><dd>{_esc(value)}</dd>")
        parts.append("</dl>")
        parts.append(
            f'<p class="note" style="margin-top:1rem">'
            f'Downloaded by F-Droid straight from the publisher: '
            f'<a href="{_esc(latest.url)}">{_esc(latest.mirror_base)}</a></p>'
        )
        parts.append("</div>")
    else:
        parts.append('<p class="note">No version has been published yet.</p>')

    # --- history ---------------------------------------------------------
    releases = state.sorted_releases()
    if len(releases) > 1:
        parts.append("<h2>Release history</h2>")
        parts.append("<table><thead><tr><th>Version</th><th>versionCode</th>")
        parts.append("<th>Size</th><th>Published</th><th>APK</th></tr></thead><tbody>")
        for release in releases:
            parts.append(
                "<tr>"
                f"<td>{_esc(release.version_name)}</td>"
                f"<td class='mono'>{_esc(release.version_code)}</td>"
                f"<td class='mono'>{_esc(_human(release.size))}</td>"
                f"<td class='mono'>{_esc(_when(release.added))}</td>"
                f"<td class='mono'><a href='{_esc(release.url)}'>apk</a></td>"
                "</tr>"
            )
        parts.append("</tbody></table>")

    # --- how it works ----------------------------------------------------
    parts.append("<h2>How this works</h2>")
    parts.append(
        "<p>The publisher does not offer an API for its downloads, so the link to "
        "the Android package is hard-coded inside a hashed JavaScript bundle on "
        f'<a href="{_esc(config.app.website)}">the official website</a>. A scheduled '
        "job reads the page, follows the bundles, extracts the download URL, and "
        "compares it with the version already published. Only when something has "
        "changed is the APK fetched &mdash; once, to compute its SHA-256 and read "
        "its manifest &mdash; and then discarded.</p>"
    )
    parts.append(
        '<p class="note">The APK itself is <strong>not</strong> stored here. The '
        "index points at the publisher&rsquo;s own CDN, which is registered with "
        "F-Droid as a mirror, so installs and updates come from bilibili directly. "
        "This site only ever holds the index files and a few small images.</p>"
    )

    # --- raw files -------------------------------------------------------
    parts.append("<h2>Repository files</h2>")
    parts.append('<ul class="files">')
    # These must be relative to the repository address itself. Deriving a base by
    # trimming the last path segment would drop the "repo" component and 404.
    base = repo_url.rstrip("/") + "/"
    entries = [
        ("entry.jar", "signed entry point; the client starts here"),
        ("index-v2.json", "the current index, every published version"),
        ("index-v2.jar", "the same index, signed with SHA-256"),
        ("index-v1.json", "legacy index for older F-Droid clients"),
        ("index-v1.jar", "the same, signed with SHA-1 for Android before 4.3"),
        ("entry.json", "the entry point, unsigned"),
    ]
    for name, description in entries:
        parts.append(
            f'<li><a href="{_esc(base + name)}"><span class="mono">{_esc(name)}</span></a>'
            f'<span class="desc">{_esc(description)}</span></li>'
        )
    parts.append("</ul>")

    if site is not None and (site.play_store or site.app_store):
        parts.append("<h2>Official channels</h2><ul>")
        if site.play_store:
            parts.append(
                f'<li><a href="{_esc(site.play_store)}">Google Play</a></li>'
            )
        if site.app_store:
            parts.append(f'<li><a href="{_esc(site.app_store)}">App Store</a></li>')
        if site.og_image:
            parts.append(
                f'<li><a href="{_esc(site.og_image)}">Official site artwork</a></li>'
            )
        parts.append("</ul>")

    # --- other clients ---------------------------------------------------
    # Obtainium cannot search a third-party F-Droid repository, so the package
    # name has to be typed in by hand. Its URL handling keeps an `appId` query
    # parameter and uses it to pre-fill the field, so a link that carries it
    # saves the reader looking the name up.
    obtainium = f"{repo_url}?appId={config.package_name}"
    parts.append("<h2>Other clients</h2>")
    parts.append('<ul class="files">')
    for description, href, label in (
        (
            "Obtainium",
            obtainium,
            f"?appId={config.package_name}",
        ),
    ):
        parts.append(
            f'<li><span class="desc">{_esc(description)}</span>'
            f'<a href="{_esc(href)}"><span class="mono">{_esc(label)}</span></a></li>'
        )
    parts.append("</ul>")
    parts.append(
        '<p class="note" style="margin-top:.75rem">Obtainium cannot list the apps in '
        "a third-party F-Droid repository, so it needs the package name; the link above "
        "carries it.</p>"
    )

    # --- footer ----------------------------------------------------------
    # The disclaimer names the people who actually own the game, not just the
    # tool: BanG Dream! is Bushiroad's, FROMTOKYO made this one, and BILIBILI HK
    # is the party that signed the APKs being served from its CDN.
    #
    # The link to the tool is configurable rather than hardcoded, so a fork
    # publishing its own index does not send visitors back to the repository it
    # was forked from. Left unset, the name renders as plain text as before.
    if config.project_url:
        tool = f'<a href="{_esc(config.project_url)}"><code>bdon-fdroid</code></a>'
    else:
        tool = "<code>bdon-fdroid</code>"
    parts.append(
        f"<footer>Generated by {tool} &middot; "
        f"package <code>{_esc(config.package_name)}</code> &middot; "
        f"{_esc(config.app.license)} software, redistributed by its publisher. "
        "Not affiliated with or endorsed by Bushiroad, FROMTOKYO, BILIBILI or "
        "the F-Droid project."
        "</footer>"
    )
    parts.append(f"</main>\n<script>{_SCRIPT}</script>\n</body>\n</html>\n")
    return "".join(parts)


def write(
    path: str,
    config: Config,
    state: State,
    fingerprint: str | None = None,
    site: SiteMetadata | None = None,
) -> str:
    document = render(config, state, fingerprint, site)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(document)
    return document
