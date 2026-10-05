# AGENTS.md

Working notes for `bdon-fdroid`. This file records the things that are
**load-bearing and not obvious from the code** - the constraints that come from
F-Droid's format and from how the Android clients actually behave, each of which
cost real investigation. `docs/repository-format-notes.md` has the long-form
derivation; this is the short list of what will bite you.

Read this before changing anything that touches the index, the scraper, or the
workflows.

## What this is

Scrapes [bdon.biligames.com](https://bdon.biligames.com/) for the Android APK of
**BanG Dream! Our Notes** (EN / TW / HK / SEA build, not JP), and publishes a
static [F-Droid](https://f-droid.org) repository on GitHub Pages. A daily GitHub
Actions job does it. **The 450 MB APK is never stored** - the index points at
the publisher's own CDN, registered as an F-Droid mirror.

## Commands

```bash
python3 -m bdon_fdroid check      # scrape and report; downloads nothing
python3 -m bdon_fdroid build      # fetch if new, then write + sign ./deploy
python3 -m bdon_fdroid serve      # serve the output like a client would
python3 -m bdon_fdroid verify --repo-url <url> --fingerprint <hex>

python3 -m unittest discover -s tests        # 219 tests, stdlib only
(cd worker && node --test)                   # 18 tests
```

The conformance group is skipped unless `fdroidserver` is importable. Run it too
before claiming a change works:

```bash
python3 -m venv /tmp/fdcheck && /tmp/fdcheck/bin/pip install fdroidserver
/tmp/fdcheck/bin/python -m unittest discover -s tests
```

**Python 3.10+ standard library only.** No runtime dependencies, and CI must not
grow a `pip install` step. Signing uses the JDK's `jarsigner`, which is already
on the runner. If you need a parser, write it.

This applies to the **tests** as well as the tool, and the reason is not
theoretical: three places run the suite, and only one of them has anything
installed. `conformance.yml`'s `unit` job and `update.yml`'s daily job both run
plain `python3` with no third-party packages, so a single `import yaml` in a test
errored there while passing on a developer machine that had it - and took the
daily job with it, since that job runs the suite before committing, so nothing
was published either. `StdlibOnlyTests` in `tests/test_workflows.py` now enforces
it with `ast`, across `tests/` and `bdon_fdroid/`, with `fdroidserver` allowed
only because it is imported lazily behind a skip.

To reproduce a CI environment before committing:

```bash
python3 -m venv /tmp/bare && /tmp/bare/bin/python -m unittest discover -s tests
```

That venv has nothing in it, exactly like the unit and daily jobs.

## Hard rules

1. **Never store the APK.** `*.apk` is gitignored, and the design depends on it -
   that is the whole point of the project. Only a mirror base plus a filename is
   ever persisted.
2. **Never commit `keystore.p12`.** It is gitignored and restored in CI from the
   `KEYSTORE_BASE64` secret (`scripts/init-repo-key.sh`). Anyone holding it can
   impersonate this repository.
3. **Every commit needs `Assisted-by: Opencode:space-bunny-free`** as a trailer.
   Use the maintainer's own git identity - do not override with `-c user.name`
   or `-c user.email`.
4. **Metadata must be factually right about who made the game.** BanG Dream! is
   Bushiroad's property; FROMTOKYO made this game; BILIBILI HK LIMITED published
   and signed the APK. Do not credit anyone else. (Craft Egg made BanG Dream!
   Girls Band Party!, a different game - crediting them was a real error here.)
5. **`repo.json` is the canonical home for the repository name, the description,
   the author and the categories.** `bdon_fdroid/config.py` holds matching
   dataclass defaults so a fork that deletes the file still works - which means
   the two can drift silently, so `tests/test_repo_json.py` asserts they agree.
   Change both, or neither. The one deliberate exception is the long HTML
   description, which lives in `repo.json` only.
6. **`"license": "Proprietary"` is intentional** even though it is not an SPDX
   identifier and fdroidserver's `lint` would flag it. We use fdroidserver as a
   verifier, not a linter, and the honest value beats a fake open-source one.
7. **Do not hardcode this repository's URL into the code.** Every external
   address is either in `repo.json` or derived at runtime, because a fork
   publishes its own index and must not link back here. The landing page's
   footer link is `projectUrl`; the Worker's CDN target comes out of
   `repo.mirrors[0].url`; `PAGES_ORIGIN` is a Cloudflare variable with a
   committed default. There is a test asserting `landing.py` contains no
   `github.com` string at all.

## The load-bearing facts

### The APK cannot be addressed directly

F-Droid appends `apkName` to a mirror address (`Mirror.getUrl` calls
`appendPathSegments`). **An absolute URL in the index is therefore impossible** -
it would produce `https://cdn/.../BanGDream...apk/BanGDream...apk`. This is why
the CDN *directory* is registered as a mirror and the file is stored relative to
it. Any change that tries to put a full APK URL in `apkName` or `file.name` is
wrong, however reasonable it looks.

### A mirror only works in one client

| Client | Behaviour |
| --- | --- |
| Official F-Droid | follows mirrors; falls through on 404 |
| Neo Store, Droid-ify | do **not** fall back - a 404 is a terminal failure |
| Obtainium | never reads `mirrors` at all |

So `worker/` exists: a Cloudflare Worker that 302s `*.apk` to the CDN and
**proxies everything else from Pages byte-for-byte**. It must proxy rather than
redirect because the index files are signed - redirecting would break the JAR
signature, since the bytes a client verifies must be the bytes it stores.

The Worker reads its target from `repo.mirrors[0].url` out of the fetched index,
not from a hardcoded value, so a CDN move needs no redeploy. Keep it that way.

### Repository variables are `vars.X`, not environment variables

A repository *variable* reaches Actions only as `${{ vars.REPO_URL }}`. A bare
`${REPO_URL:-}` is **always empty**. This is not hypothetical: that bug shipped
the GitHub Pages address as the primary link, a URL only the official client can
install from, and the run went green. `update.yml` now fails loudly if
`REDIRECTOR_URL` is set without `REPO_URL`.

A repository variable must be read through `vars.NAME` and bridged into a step's
`env:` before the shell touches it. Reading a *bridged* name normally is correct
and is the documented idiom - what is never correct is a shell read of a name
nothing bridged, because the expansion is silently empty.

### A workflow file that is invalid schema still parses as YAML

`yaml.safe_load` accepts it, nothing local complains, and the file reads fine.
GitHub only rejects it when it validates the workflow, which shows up as a failed
run and an email - not as a red build of whatever you were working on. So
`redirector.yml` sat broken while the Worker it deploys kept working perfectly,
because the Worker had already been deployed by hand and only changes when
`worker/` does.

The cause was a comment dedented to the step level:

```yaml
      - name: Run the Worker tests
      # Never deploy code that does not pass its own tests.   <-- same indent
      - run: node --test worker/
```

The comment reads as though it introduces the next step, so the step above keeps
its `name:` and loses its `run:`. GitHub's error is "There's not enough info to
determine what you meant", reported at the comment, far from the mistake.

**A comment introducing a step belongs above the `- `, not beside it.** Note that a
comment at the item level is *not* wrong on its own - one introducing the next
step is normal. It is only ambiguous when it leaves the step above it without a
`run:` or `uses:`.

`tests/test_workflows.py` checks this and the other structural rules. It is
line-based rather than using a YAML parser, because the daily job may not gain a
dependency.

### Cloudflare 403s urllib's default User-Agent

`urllib.request.urlopen` sends `Python-urllib/x.y`, a signature Cloudflare's
managed rules reject with **403**. This bit the conformance job: the redirector
check fetched the index with bare urllib against an address served by Cloudflare,
got 403, and the run failed. Real F-Droid clients send their own User-Agent and
are unaffected, so **users were never affected** - it only ever looked like the
site was down.

The trap is the asymmetry: bare urllib works against GitHub Pages and fails
against the redirector, so the failure looks like an outage rather than a script
bug. Use `curl` in workflow shell steps, and keep setting `USER_AGENT` in
`http.py`. `tests/test_workflows.py` asserts no workflow mentions
`urllib.request`; `tests/test_http.py` asserts the header is actually sent.

### `repo.address` is expensive; the fingerprint is not

Changing `repo.address` means every user must **remove and re-add** the
repository. Changing anything else - the name, the description, the category -
does not, because clients key the repository on its address and verify it with
the signing key. Treat the address as a one-way door.

The fingerprint is written to `repo-info.json` at publish time; do not hardcode
one in the repository, because a fork's will differ.

### The app description is HTML

Both clients parse the field as HTML - `AnnotatedString.fromHtml` in the current
one, `HtmlCompat.fromHtml` in the legacy one - and an HTML parser **collapses
plain newlines into spaces**. The publisher's copy is therefore stored as
`<p>`/`<br>`/`<b>`. Pasting it as plain text renders as one wall of text. (690
apps in the real f-droid.org index have exactly this bug.)

`Config.validate()` enforces F-Droid's `char_limits` (summary 80, description
4000, author 256), tags included, so an over-long field fails the build instead
of being silently truncated by a client. Of the 4476 apps in the real
f-droid.org index, 686 have a newline and no block markup at all - they all
render as one wall of text.

### There is no F-Droid category called "Games"

In the current client "Games" is the name of a **group** holding 17 genres. A
category with that literal name falls through to `else -> CategoryGroups.misc`,
so the app would be filed under **Misc**. Use a real genre - `Party Game`, which
matches the publisher's own "party-style ... with up to five players".

The legacy clients that Neo Store and Droid-ify fork show a flat category list,
where an unknown name falls back to the raw string and simply has no icon. That
degrades safely; using `Games` does not.

### The package name is not the Play listing's

The APK the website serves is **`com.bilibili.sirius.official`**. Google Play
lists a *different* application, `com.bilibili.sirius`. They are two apps; a
client cannot upgrade across two application ids. Do not "correct" this.

### `uses-feature` must match `fdroidserver/update.py` exactly

Only a *named* feature that is *required* belongs in the index. A feature declared
only as `android:glEsVersion` has no name, so it is skipped - androguard never
reports it, so emitting it would diverge from every real F-Droid repository, and
clients would treat a GLES level as a hard requirement and refuse to install on
devices that run the game fine.

### Bump `MANIFEST_RULES_VERSION` when the manifest parser changes

`releases.json` is a committed cache. If you change how the manifest is
interpreted and do not bump `state.MANIFEST_RULES_VERSION`, `State.is_stale()`
stays false and **the stale cached metadata masks your fix**. This happened once
and the symptom was a fix that appeared not to work.

### v1 and v2 write app metadata from separate code paths

`indexgen` builds `apps`/`packages` for v1 and `packages[...]["metadata"]` for v2
independently. A field fixed in one and forgotten in the other ships silently.
There are tests asserting the two agree about `authorName` and `categories`; if
you add a metadata field, add it to both and to those tests.

Related shape difference, if you are writing assertions: v2 **localises**
`name`, `summary` and the `repo` block (`{"en-US": ...}`) but leaves
`authorName`, `categories` and `license` as plain scalars. v1 localises nothing.

## Scraping notes

- The download URL is a string literal inside one of the site's **hashed JS
  bundles**, found via the webpack public path. There is no API and no version
  metadata anywhere on the site.
- The **app icon** comes from the site's own image assets. The page has no
  `apple-touch-icon` and its only `rel=icon` is a 16px favicon. The repository
  icon is the page's `og:image`.
- **Screenshots are pinned in `repo.json`** because the site's images are
  content-hashed; any URL pasted there must be updated by hand when the site is
  redesigned.
- Determine an image's extension from its **`Content-Type`**, never from the URL.
- Image sizes come from the actual response, not a header that may be absent.

## Change detection

`releases.json` records `versionCode`, `sha256`, `etag`, `lastModified` and a
`versionHint`. The APK is fetched only when something actually changed, once, to
compute the hash and read the manifest - and then discarded. `--force-download`
bypasses this. Keeping a *metadata-only* change (a description edit) from
triggering a 450 MB download is the whole reason the cache exists; do not
"simplify" it away.

## Testing philosophy

Bugs here were invisible to builds and visible only on a real device, so the tests
are the only safety net. When you add a test, **confirm it fails when you revert
the thing it protects.** Every guard in this codebase was mutation-tested that
way - reintroducing the wrong author, de-HTML-ing the description, unbalancing a
tag, switching the category to `Games` each fail the suite.

`tests/test_conformance.py` is the one that matters most: it builds, signs, serves
over HTTP and makes the reference implementation download and verify the result,
including that a tampered index and a wrong fingerprint are both rejected. Its
`ShippedConfigConformanceTests` class does this for the real `repo.json`, because
the other class builds from the dataclass defaults and never saw the published
file.

`tests/test_repo_json.py` covers the shipped configuration, which for a long time
had **no** test coverage at all.

### The live-site check used to run only weekly

`conformance.yml`'s `published` job was gated on
`github.event_name == 'schedule' || inputs.repo_url != ''`. `inputs` is empty on
push and pull_request, so **the job that verifies the published address and the
redirector was skipped on every ordinary push** and ran once a week, Mondays
06:41 UTC. The defect described above could not be caught any sooner than a week
after being introduced - and was not, for weeks.

The gate now also runs when `vars.REPO_URL` or `vars.REDIRECTOR_URL` is set, so a
configured repository checks on every push while a fork with nothing configured
still skips cleanly rather than going red on a push it has no stake in. The
original concern - checking a repository that does not exist yet - is handled
inside the job, which exits with a notice when no fingerprint is published.

### The daily job committed on every single run

`lastChecked` in `releases.json` moves on every run by design, so the commit step
compared the **whole file** and always found a difference: four identical-looking
`chore: record release 1.0.2` commits in a row, with a genuine new release
buried among them. It now compares the `releases` array and commits only when
that list changes, so the history reads as a changelog instead of a heartbeat.
The comparison reports through its exit status inside an `if`, because `set -e`
would otherwise abort the step when it reports "changed".

`tests/test_workflows.py` checks the workflow files structurally. It exists
because an invalid workflow file is not a failing build - it is a file that
parses as YAML, reads correctly, and is rejected by GitHub somewhere else
entirely.

**Its limit, worth stating:** these checks catch structure, not meaning. The
conformance workflow passed every one of them while checking the wrong host and
using a User-Agent that got it 403'd. A check that passes on a file that is
semantically wrong tells you nothing about whether the file is right.

## Layout

```
bdon_fdroid/
  scrape.py      find the download URL in the site's JS bundles
  axml.py        binary AXML reader: manifest, uses-sdk, permissions, features
  apk.py         Release record
  state.py       releases.json cache, staleness, MANIFEST_RULES_VERSION
  indexgen.py    write entry/index v1+v2, mirrors, metadata
  signing.py     keystore + jarsigner, JAR signing
  landing.py     the published HTML page
  config.py      repo.json, char limits
  cli.py         check / fetch / build / serve / verify
worker/          Cloudflare redirector (index.js, wrangler.jsonc)
.github/workflows/
  update.yml        daily scrape, build, publish
  redirector.yml    deploy the Worker
  conformance.yml   tests + fdroidserver verification
```

## Before you call a change done

- `python3 -m unittest discover -s tests` passes.
- The same passes under an interpreter that has `fdroidserver`.
- `(cd worker && node --test)` passes if you touched the Worker.
- New tests were mutation-tested.
- `repo.json` metadata is still factually correct about the developer, the
  publisher and the trademark holder.
