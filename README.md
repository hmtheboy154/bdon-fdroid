# bdon-fdroid

An unofficial, automatically-updated [F-Droid](https://f-droid.org) index for
**BanG Dream! Our Notes**, built from the publisher's own website and published on
GitHub Pages. The APKs are BILIBILI HK's own builds, downloaded from bilibili's
CDN; nothing here is built or re-signed.

The publisher does not offer an API for its downloads. The "Download for Android"
button on [bdon.biligames.com](https://bdon.biligames.com/) simply calls
`window.open()` with a URL that is hard-coded as a string literal inside one of
the site's hashed JavaScript bundles:

```js
// chunk-common.<content-hash>.js
Le="https://l12-pkg-download.biligames.com/sirius/apk/BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02.apk"
```

So tracking new releases means reading those bundles. This project does that,
and publishes the result as a real F-Droid repository so the app updates itself
from inside the F-Droid client like any other app.

Two things shape the design, both covered below:

- **The 450 MB APK is never stored here.** The index points at the publisher's
  own CDN, which is registered with F-Droid as a mirror - and, because only the
  official client understands mirrors, behind a small redirecting Worker.
  See [The APK is never stored here](#the-apk-is-never-stored-here).
- **The publisher publishes no API and no version metadata**, so `versionCode`
  can only be read out of the APK itself. Every new release is downloaded once,
  read, and discarded. See [How a run works](#how-a-run-works).

## How a run works

1. **Scrape** `https://bdon.biligames.com/`, collect every `<script src>`, and
   download the bundles. Nothing is hardcoded: the bundle filenames contain
   content hashes and change on every deploy.
2. **Find** the single `.apk` URL in them. More than one is an error, not a
   guess.
3. **Compare** it against `releases.json`, the committed cache of everything
   seen before, using the URL plus the CDN's `Content-Length`, `ETag` and
   `Last-Modified`. **This is the step that keeps the daily job cheap** - an
   unchanged check is a few HTTP requests, not a 450 MB download.
4. **Download** the APK only when something actually changed. While it streams
   past, the SHA-256 is computed; then the manifest and signing certificate are
   read, and the file is deleted. Nothing is kept.
5. **Publish** `index-v1.json`, `index-v2.json` and `entry.json` into signed
   JARs, plus a landing page, onto the `gh-pages` branch.

## The APK is never stored here

The game is about 450 MB, so this repository does not host it. The generated
index points at bilibili's own CDN, and the *hash* of the file is recorded so the
F-Droid client can still verify what it downloads.

Getting that to work took two non-obvious steps.

**A mirror, because F-Droid cannot follow an absolute URL.** F-Droid resolves an
APK's `apkName` by appending it to a **mirror address** - in `fdroidclient`:

```kotlin
// MirrorChooser.mirrorRequest -> Mirror.getUrl
URLBuilder(url).appendPathSegments(path.trimStart('/'))
```

An absolute URL in the index would be mangled into a path segment and break, so
the CDN directory is instead handed to the client as a mirror, and the APK is
named with the CDN's own filename:

```json
"mirrors": [{ "url": "https://l12-pkg-download.biligames.com/sirius/apk/",
              "countryCode": "CN" }],
"file": { "name": "/BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02.apk", "sha256": "..." }
```

`<mirror>/<name>` is then byte-for-byte the upstream URL, and F-Droid downloads
the game straight from the publisher. Paths the CDN does not have answer `403`,
so the client falls back to the Pages-hosted repository for the index, icon and
screenshots. If the publisher ever renames the CDN host, the next scheduled run
notices and republishes with the new mirror - no code change.

**A redirector, because only the official client honours mirrors.** The mirror
mechanism is necessary but not sufficient, and the gap is invisible until you try
a second client:

| Client | On a 404 | Result |
| --- | --- | --- |
| F-Droid | falls through to the next mirror | works |
| Neo Store | `NotFound -> Result(response)`, a *terminal* result; only exceptions rotate mirrors | fails |
| Obtainium | never reads `mirrors`; always uses `<repo address>/<apk name>` | fails |

All three agree the APK must be reachable **at the repository address**, and
GitHub Pages cannot redirect a request. So [`worker/`](./worker) holds a small
Cloudflare Worker in front of Pages:

```
client ──▶ worker/fdroid/repo/<apk>      ──302──▶  publisher's CDN
client ──▶ worker/fdroid/repo/entry.jar  ────────▶  GitHub Pages (proxied, byte for byte)
```

Still nothing stored by us: the 450 MB comes from bilibili and the index comes
from Pages. The Worker reads its redirect target from `repo.mirrors[0].url`
rather than hardcoding it, so if the publisher moves the CDN the scraper picks it
up and the Worker follows with no redeploy. It is deployed by its own workflow
whenever `worker/` changes; the daily update never touches it. See
[step 5 of the setup guide](./docs/setup-github-actions-and-pages.md#5-deploy-the-redirector-recommended).

## Quick start

```bash
# What does the site offer right now? Downloads nothing.
python3 -m bdon_fdroid check

# Generate a complete, signed repository in ./deploy
./scripts/init-repo-key.sh          # once; prints the secrets to configure
python3 -m bdon_fdroid build \
    --keystore keystore.p12 --key-alias bdon-fdroid \
    --repo-url https://<you>.github.io/bdon-fdroid/fdroid/repo

# Try it the way a client would
python3 -m bdon_fdroid serve --deploy deploy --port 8000
```

`releases.json` is committed and already contains the current release, so the
first `build` you run locally skips the 450 MB download and only regenerates
the repository. Delete it, or pass `--force-download`, to exercise the full
path.

Requirements: Python 3.10+ and a JDK (for `jarsigner` and `keytool`). There are
**no Python dependencies at all** - the scraper, the APK reader and the index
generator are all standard library, so the daily job needs no `pip install`.

Then add the repository in F-Droid under *Settings -> Repositories*, pasting the
URL with its `?fingerprint=` parameter. The fingerprint is displayed on the
published page; compare it with what the F-Droid app shows.

## Command line

| Command | What it does |
| --- | --- |
| `check` | Scrape and report. Never downloads the APK. |
| `fetch` | Scrape, and download + inspect the APK if it is new. Updates `releases.json`. |
| `build` | `fetch`, then write and sign the whole repository into `deploy/`. |
| `serve` | Serve `deploy/` locally, so a real F-Droid URL can be pointed at it. |
| `verify` | Download a published repository and check it with `fdroidserver`. |
| `dump` | Print the cached release state as JSON. |

Common options (accepted before or after the subcommand): `--repo-url`,
`--site-url`, `--state`, `--deploy`, `--keystore`, `--key-alias`, `--key-pass`,
`--force-download`, `--keep-releases`.

## Configuration

Everything tunable lives in [`repo.json`](./repo.json): the repository name and
address, the app's name, summary, description and licence, which images to use,
and how many past versions to keep. `projectUrl` is the one field here that is
deliberately empty by default - it is the link on "Generated by bdon-fdroid" in
the page footer, and a fork should point it at its own repository rather than
inherit this one. Environment variables (`REPO_URL`, `REPO_NAME`, `KEYSTORE_PATH`,
`KEYSTORE_PASS`, `REPO_KEYALIAS`) override it so CI can inject what it derived at
runtime.

## What gets published

```
deploy/
├── .nojekyll                  # stop GitHub Pages running Jekyll over the output
├── index.html                 # the landing page: repo URL, fingerprint, versions
├── repo-info.json             # the same facts, machine-readable
└── fdroid/repo/               # the repository itself, at the URL you add in F-Droid
    ├── entry.jar              # signed entry point; the client starts here
    ├── entry.json
    ├── index-v2.json          # current index, every published version
    ├── index-v2.jar
    ├── index-v1.json          # legacy index for older clients
    ├── index-v1.jar
    ├── icon.jpg
    ├── icons/com.bilibili.sirius.official.png   (the app icon, ~1 MB)
    └── com.bilibili.sirius.official/en-US/phoneScreenshots/*.webp
```

Both index formats are published because F-Droid 1.x clients read only v1, and
the v1 document is the only place they learn the mirror list - the two have to
agree, and there is a test for exactly that.

Every past release stays in the index, so users can still install or roll back to
an older version. `releases.json` is committed, which means the history doubles
as the project's changelog.

## Signing

F-Droid does not trust an unsigned index: it verifies the JAR signature on
`entry.jar` against the repository key the user installed it with, then checks
the SHA-256 of `index-v2.json` against the value inside the signed `entry.json`.

`bdon-fdroid` generates a repository key and signs with the JDK's `jarsigner`,
using the same algorithms `fdroidserver` does: SHA-256 / SHA256withRSA for
`entry.jar` and `index-v2.jar`, and SHA-1 / SHA1withRSA for `index-v1.jar`,
which needs the weaker algorithm for Android before 4.3.

**Keep the key.** Losing it means every user has to remove and re-add the
repository. Generate it with [`scripts/init-repo-key.sh`](./scripts/init-repo-key.sh),
which prints the three secrets to configure.

## Verifying it actually works

The test suite has no dependencies, except one group that is skipped unless
`fdroidserver` is installed:

```bash
python3 -m unittest discover -s tests

# The interesting half: build, sign, serve, and make the reference
# implementation download and verify the result.
python3 -m venv /tmp/fdcheck
/tmp/fdcheck/bin/pip install fdroidserver
/tmp/fdcheck/bin/python -m unittest discover -s tests -v

# Or check a repository that is already published.
python3 -m bdon_fdroid verify --repo-url https://<you>.github.io/bdon-fdroid/fdroid/repo \
                              --fingerprint <hex>
```

The conformance suite proves that a wrong fingerprint is rejected, and that a
tampered index is rejected.

## Documentation

- [Setting up GitHub Actions and GitHub Pages](./docs/setup-github-actions-and-pages.md) -
  step by step, from an empty repository to a published, self-updating repo.
- [Repository format notes](./docs/repository-format-notes.md) - the files that
  are generated, the conventions each format follows, and why the CDN mirror
  trick is necessary.
- [AGENTS.md](./AGENTS.md) - the constraints that are load-bearing but not
  obvious from the code. Read it before touching the index, the scraper or the
  workflows.

## Notes and limitations

### The package name is not the one on Google Play

The APK the website serves is **`com.bilibili.sirius.official`**. The Google Play
listing linked from the same page is a *different application*,
`com.bilibili.sirius`.

That is not a typo. F-Droid keys everything on the package name, and the
publisher's own client would refuse to update across two application ids, so the
repository indexes the build the site actually distributes.

Related: `versionCode` is not derivable from the URL either. The current release
is `1.0.1` at `versionCode 25`, which is why the APK has to be read at least once
per new release. The build refuses to publish if the scraped link ever resolves
to a different application than the one configured in `repo.json`.

### The index must not claim the app is restricted

F-Droid treats every entry in an app's `features` list as **mandatory**, and
`fdroidserver` only records a `uses-feature` that has an `android:name` *and* is
required. This project's manifest has no required features at all:

```xml
<uses-feature android:glEsVersion="0x30000"/>              <!-- no name -->
<uses-feature android:name="android.hardware.touchscreen" android:required="false"/>
```

So the published index carries **no** feature list. An earlier version synthesised
`glEsVersion196608` and listed every optional feature, and F-Droid then reported
the app as incompatible with phones that ran it perfectly well from Google Play.
`tests/test_features.py` pins the rule, and `releases.json` records which version
of it produced each cached entry, so a change to the rules cannot be masked by
stale metadata.

### Everything else

- The app is **proprietary**; `repo.json` says so explicitly via
  `"license": "Proprietary"`. This repository indexes the publisher's official
  builds and redistributes nothing.
- The signer fingerprint is reported only when the APK carries a v2/v3 signing
  block. Every app with `targetSdkVersion` 30 or higher must have one, so this
  is not a practical concern; a v1-only APK would simply omit the field rather
  than report a guess.
- Screenshots are optional. The site's images are content-hashed, so pin them in
  `repo.json` if you want a specific set. The icons are scraped: the repository
  icon from the page's `og:image` (~307 KB), and the app icon from the site's own
  image assets (~1 MB) - the page has no `apple-touch-icon` and its only
  `rel=icon` is a 16px favicon, so the real icon is found in the JS bundles.
- Only Android builds are indexed. The publisher also ships the game on Google
  Play and the App Store; both are linked from the published page.
- The app description is **HTML**, because both F-Droid clients parse it as such
  (`AnnotatedString.fromHtml` in the current one, `HtmlCompat.fromHtml` in the
  legacy one) and an HTML parser collapses plain newlines into spaces. The
  publisher's copy is therefore stored as `<p>`/`<br>`/`<b>`; pasting it as plain
  text renders as one wall of text. Obtainium ignores the field entirely. It is
  written down in `repo.json` only, since it is long enough that a second copy in
  `config.py` would only let the two drift.
- The category is `Party Game`, not `Games`. **There is no F-Droid category
  called "Games"** - in the current client that is the *name of a group* holding
  17 genres, so a category with that literal name falls through to the client's
  `else -> CategoryGroups.misc` branch and the app shows up under Misc.
  `Party Game` is the one genre that fits (the publisher's own copy calls the
  gameplay party-style, with up to five players) and is a real category in the
  f-droid.org index. The legacy clients that Neo Store and Droid-ify fork show
  categories as a flat list, where `Party Game` appears without an icon - the
  client falls back to the raw name, so it degrades rather than breaking.
- `Config.validate()` enforces F-Droid's own `char_limits` (summary 80,
  description 4000, author 256), so an over-long field fails the build instead of
  being silently truncated by a client. Tags count against the description limit.

## License

MIT for this tool. [BanG Dream!](https://bang-dream.com/) is a trademark of
[Bushiroad](https://bushiroad.co.jp/); the game was made by
[FROMTOKYO](https://www.fromtyo.jp/) and this Android build is published by
BILIBILI HK LIMITED. The game is redistributed by its publisher, not by this
project.
