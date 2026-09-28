# Setting up GitHub Actions and GitHub Pages

From an empty repository to a published F-Droid repository that updates itself
every day. Expect about fifteen minutes, most of it waiting for a download.

- [1. Create the repository](#1-create-the-repository)
- [2. Generate the signing key](#2-generate-the-signing-key)
- [3. Add the three secrets](#3-add-the-three-secrets)
- [4. Run the first build](#4-run-the-first-build)
- [5. Enable GitHub Pages](#5-enable-github-pages)
- [6. Point F-Droid at it](#6-point-f-droid-at-it)
- [7. Confirm the daily job](#7-confirm-the-daily-job)
- [Going further](#going-further)
- [Troubleshooting](#troubleshooting)

---

## 1. Create the repository

Push this project to a new GitHub repository:

```bash
git init -b main
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<you>/bdon-fdroid.git
git push -u origin main
```

Nothing else is needed: the workflows infer the Pages address from the
repository owner and name, so `https://<you>.github.io/bdon-fdroid/fdroid/repo`
is derived automatically.

Two settings are worth changing on a fork:

- **Settings -> Actions -> General -> Workflow permissions**: set *Read and
  write permissions*. The workflow pushes the `gh-pages` branch and commits the
  release history, so it needs write access.
- **Settings -> Pages -> Build and deployment -> Source**: choose *Deploy from a
  branch* and select `gh-pages` / `root`. The first workflow run creates the
  branch for you, so do this in step 5.

## 2. Generate the signing key

F-Droid repositories are signed so that a client can tell the index it
downloaded really came from the repository you added. That needs a key pair.

```bash
./scripts/init-repo-key.sh
```

It will ask for a password, then print:

- the keystore path and alias,
- the **SHA-256 fingerprint** of the signing certificate,
- the three secret values to paste next,
- the keystore, base64-encoded, ready to copy.

> **Store `keystore.p12` somewhere safe now.** It is gitignored and never
> written anywhere else. If you lose it, every existing installation of the
> repository becomes untrusted and users must remove and re-add it. A new key
> cannot be substituted into a live repository.

The script refuses to overwrite an existing keystore, on purpose.

## 3. Add the three secrets

Go to **Settings -> Secrets and variables -> Actions -> New repository secret**
and add each of these:

| Secret name | Value |
| --- | --- |
| `KEYSTORE_BASE64` | The long base64 block printed by `init-repo-key.sh`. |
| `KEYSTORE_PASS` | The keystore password you chose. |
| `REPO_KEYALIAS` | The alias, e.g. `bdon-fdroid`. |

The key is only ever restored inside the runner, written to a temporary file
with `umask 077`, and never committed or uploaded as an artifact.

> `KEYSTORE_BASE64` must be one line with no wrapping. To produce it yourself
> from a keystore you already have: `base64 -w0 keystore.p12`.

## 4. Run the first build

Open the **Actions** tab, select **Update repository**, and use *Run workflow*.

The first run is the slow one. Nothing is cached yet, so it will discover the
current release and download the APK - roughly 450 MB - to compute its SHA-256
and read its manifest. On a GitHub-hosted runner that takes a few minutes. The
step prints progress as it goes.

When it finishes you should see a summary like:

| | |
| --- | --- |
| Add this URL | `https://<you>.github.io/bdon-fdroid/fdroid/repo` |
| Key fingerprint | `604f…` (yours will differ) |
| Latest version | `1.0.1 (versionCode 10001)` |

Three things happened along the way:

- `releases.json` was committed back to `main` with the release it found. That
  file is the cache that makes every later run cheap.
- The `gh-pages` branch was created and populated with `deploy/`.
- `repo-info.json` was written, which is where the fingerprint comes from.

## 5. Enable GitHub Pages

**Settings -> Pages -> Build and deployment**, set:

- Source: **Deploy from a branch**
- Branch: **gh-pages**, folder **/ (root)**

Save. Pages builds within a minute or two, and your repository is live at:

```
https://<you>.github.io/bdon-fdroid/
```

The root page is a landing page showing the repository URL, the signing key
fingerprint, the current version and the full release history. The F-Droid
repository itself lives under `/fdroid/repo`, which is the address to paste
into the app.

> The repository is published with a `.nojekyll` file. Without it, Pages would
> run Jekyll over the output, which is free to drop files it does not
> recognise.

## 6. Point F-Droid at it

In the F-Droid app: **Settings -> Repositories -> +**, then use the URL from the
landing page, which includes the fingerprint:

```
https://<you>.github.io/bdon-fdroid/fdroid/repo?fingerprint=604f52d7...
```

The `?fingerprint=` part is what makes the addition trustworthy: F-Droid checks
the repository's signing key against it and refuses a mismatch. Compare the
fingerprint in F-Droid's repository list with the one on the landing page - they
must match.

The landing page also offers an `fdroid.link` URL, which opens the install
prompt directly on a phone.

On a browser, you can sanity-check the repository by hand:

```bash
curl -sI https://<you>.github.io/bdon-fdroid/fdroid/repo/entry.jar | head -1
```

## 7. Confirm the daily job

The workflow runs daily at 03:17 UTC. Nothing about the site is published on a
schedule, so a check that finds no change is a success, not a failure - the job
still refreshes `releases.json` and the landing page so the "last checked" time
stays honest.

Watch it under **Actions -> Update repository**. A no-change run looks like:

```
  unchanged: cached hash still matches the remote object
  (nothing new upstream; the index was refreshed anyway)
```

You can also trigger one by hand at any time, and there is a checkbox to force
a full re-download:

```bash
gh workflow run update.yml -f force_download=true
```

---

## Going further

### Using a custom domain

Create a `CNAME` file in the published tree so Pages picks it up:

```bash
echo "froid.example.com" > deploy/CNAME
```

Then set the repository address to match, and tell the workflow about it with a
repository **variable** (Settings -> Secrets and variables -> Actions ->
*Variables*, not secrets):

| Variable | Value |
| --- | --- |
| `REPO_URL` | `https://froid.example.com/fdroid/repo` |

`REPO_URL` takes precedence over the derived address, so no workflow edit is
needed. The same variable works if you rename the repository.

### Renaming the repository

The address is derived from the repository name, so renaming breaks it. Either
set `REPO_URL` as above, or leave the address stable by keeping the name.

### Changing what is published

Edit [`repo.json`](../repo.json) - the app name and summary, the licence, the
description, how many past versions to keep, or which screenshots to use - and
run the workflow manually.

### Adding screenshots

The site's images are content-hashed, so pin the ones you want in
`repo.json`:

```json
"assets": {
  "screenshotUrls": [
    "https://s1.biligames.com/fe-static/game-global-bangdreamon/gw/img/img_cinematic-story_1.57d105fc.jpg"
  ],
  "maxScreenshots": 4
}
```

They are fetched at build time and stored under the paths F-Droid expects. If a
URL 404s the build logs a warning and carries on without it.

### Running it entirely locally

```bash
python3 -m bdon_fdroid build \
    --keystore keystore.p12 --key-alias bdon-fdroid \
    --repo-url http://127.0.0.1:8000/fdroid/repo
python3 -m bdon_fdroid serve --port 8000
```

Then add `http://127.0.0.1:8000/fdroid/repo?fingerprint=...` in F-Droid on the
same device. Note that F-Droid will still fetch the APK from the real CDN,
because that is the mirror.

### Mirroring the repository elsewhere

F-Droid clients accept a list of mirrors, so a second host would give
redundancy. Add its base URL to `config/mirrors.yml`-style configuration in
`repo.json` (or extend `build_mirrors()` in
[`indexgen.py`](../bdon_fdroid/indexgen.py)) and keep it byte-identical to this
one. Remember that both `index-v1.json` and `index-v2.json` must list the same
mirrors; there is a test that checks they agree.

---

## Troubleshooting

**`KEYSTORE_BASE64 is not set`** - the secret is missing or empty. Re-run
`init-repo-key.sh` and add all three secrets.

**`jarsigner failed`** - usually a wrong `KEYSTORE_PASS` or `REPO_KEYALIAS`.
Check them against what the script printed.

**`could not fetch ... No such file or directory`** in the download step - the
runner ran out of disk. A full download needs roughly 1 GB free; if this
persists, the hosted runners are unusually constrained.

**`error: expected exactly one .apk URL but found several`** - the publisher
changed the download button, for example by adding a second regional build. The
scraper deliberately refuses to guess. Open a pull request adjusting
`discover()` in [`scrape.py`](../bdon_fdroid/scrape.py) to select the right one.

**`no .apk URL found in any bundle`** - the button moved to a file the scraper
does not read, e.g. a bundle loaded lazily rather than from `<script src>`. The
error names how many bundles were scanned.

**Pages shows a 404** - confirm the workflow's publish step succeeded and that
Pages is set to deploy from the `gh-pages` branch. `deploy/` is only published
by the workflow, so nothing appears until it has run once.

**F-Droid says the repository is not signed** - the fingerprint in the URL does
not match. Copy it from the landing page, or read it from
`https://<you>.github.io/bdon-fdroid/repo-info.json`:

```bash
curl -s https://<you>.github.io/bdon-fdroid/repo-info.json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["signerFingerprint"])'
```

**The daily job is not running** - GitHub disables scheduled workflows in
repositories with no activity for 60 days. Opening a pull request or pushing
anything reactivates it. The job also has a 90-minute timeout, generous enough
for a full download.

**Something is wrong with the published repository** - the conformance workflow
checks the live site weekly, and can be run on demand with a specific address:

```bash
gh workflow run conformance.yml -f repo_url=https://<you>.github.io/bdon-fdroid/fdroid/repo
```

It installs `fdroidserver` and asserts that the index verifies, that a wrong
fingerprint is rejected, and that a tampered index is rejected.
