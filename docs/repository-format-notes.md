# Repository format notes

What `bdon-fdroid` actually generates, and why each decision is the way it is.
Useful when the scraper breaks, when F-Droid changes a format, or when reviewing
a change to the index generation.

Nothing here is invented. The layouts come from three places:

- `fdroidserver`'s own output, `fdroidserver/tests/repo/{index-v1,index-v2,entry}.json`,
- the data classes in `f-droid/fdroidclient` (`libs/index/src/commonMain/kotlin/org/fdroid/index/`),
  which are what actually has to parse it,
- `fdroidserver/fdroidserver/index.py`, which builds it.

`tests/test_conformance.py` closes the loop: it builds a repository, signs it,
serves it over HTTP, and makes `fdroidserver` download and verify it.

## The files

| File | Signed wrapper | Purpose |
| --- | --- | --- |
| `entry.json` | `entry.jar` | Entry point. Names the real index and its hash. |
| `index-v2.json` | `index-v2.jar` | Current index: metadata plus every version. |
| `index-v1.json` | `index-v1.jar` | Legacy index, for F-Droid 1.x clients. |
| `icon.jpg` | - | Repository icon, named by `repo.icon`. |
| `icons/<package>.png` | - | App icon, at the path v1 clients expect. |
| `<package>/en-US/phoneScreenshots/*` | - | Optional screenshots. |

### How a client reads it

1. Fetch `entry.jar` **from the repository address** (index files always use the
   primary mirror first, regardless of the mirror list).
2. Verify its JAR signature against the key whose fingerprint the user installed
   the repository with.
3. Read the SHA-256 of `index-v2.json` out of the signed `entry.json`.
4. Fetch `index-v2.json` and check its hash against step 3.

That chain is why `entry.json` must name the exact bytes of `index-v2.json`, and
why the JAR must contain a single non-`META-INF/` entry named `index-v2.json` -
`fdroidserver`'s `get_index_from_jar()` takes the first entry that is not under
`META-INF/`.

## Addressing the APK: the CDN mirror trick

This is the part that makes the project possible without hosting 450 MB.

F-Droid does **not** accept an absolute URL as an APK location. In
`fdroidclient`:

```kotlin
// MirrorChooser.mirrorRequest()
mirror.getUrl(downloadRequest.indexFile.name)

// Mirror.getUrl()
URLBuilder(url).appendPathSegments(path.trimStart('/'))
```

The name is appended to a **mirror address**. Hand it
`https://cdn.example/sirius/apk/foo.apk` and you get
`https://<mirror>/https:/cdn.example/sirius/apk/foo.apk` - a broken path.

So the CDN directory is published as a mirror instead, and the APK is named with
the CDN's own filename:

```json
"repo": {
  "address": "https://example.github.io/bdon-fdroid/fdroid/repo",
  "mirrors": [{ "url": "https://l12-pkg-download.biligames.com/sirius/apk/",
                "countryCode": "CN" }]
}
```

```json
"versions": {
  "<sha256 of the apk>": {
    "file": { "name": "/BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02.apk",
              "sha256": "<sha256 of the apk>",
              "size": 446890829 }
  }
}
```

`<mirror><name>` is then the exact upstream URL. The client's mirror order is
random for downloads, and index, icon and screenshot requests against the CDN
return `403`, so it falls through to the repository address for everything else.
Verified against the live CDN: `GET .../apk/index-v2.json` -> `403`,
`GET .../apk/<the apk>` -> `200`, and `Range` requests -> `206`, so the client
can resume.

`countryCode: "CN"` is a hint, not a requirement: F-Droid prefers mirrors whose
country matches the device's, which is usually the right choice for a publisher
in that region.

`index-v1.json` must list the **same** mirror. It is the only place a v1 client
learns the mirror list, so if the two formats disagree, v1 clients try to fetch
a 450 MB APK from GitHub Pages and fail. `tests/test_indexgen.py` asserts they
agree.

### The mirror only works in one client

Publishing the CDN as a mirror is necessary but not sufficient, and the gap only
shows up when a second client is tried:

- **F-Droid** catches a 404 as a `ResponseException` and calls `handleException`,
  which moves on to the next mirror. This is the behaviour the design above
  depends on, and it is the only client that has it.
- **Neo Store** (`Downloader.kt`) has an explicit
  `response.status == NotFound -> Result(response)` branch, which is a
  *terminal* result. Mirror rotation only happens in the `catch (e: Exception)`
  path, so a 404 ends the download. It also picks a mirror at random up front
  (`mirrors.random()`), so the failure is intermittent rather than consistent.
- **Obtainium** (`fdroidrepo.dart`) never reads `mirrors`. It reads
  `index-v2.json`, takes `stripLastPathSegment(indexUrl)` as a base, and builds
  `'$baseUrl/$apkName'`.

All three therefore need the APK to be reachable at the repository address, and
GitHub Pages cannot redirect. [`worker/`](../worker/index.js) bridges that: a
`*.apk` request is answered with a 302 to `repo.mirrors[0].url` and everything
else is proxied from Pages untouched. Two details matter:

- The index is **signed**, so the proxy must not touch a byte of it. Streaming
  the upstream response through unmodified is what keeps the signature valid.
- The redirect target is read from the index rather than hardcoded, so a change
  of CDN host needs no Worker redeploy. That read is not an extra failure mode: a
  client must fetch the index before it can request an APK, so the index is
  already known to be reachable at that point.

## Index format differences

The two formats are not the same data reshaped, and this is where it is easy to
get something subtly wrong.

| | `index-v1.json` | `index-v2.json` |
| --- | --- | --- |
| App metadata | `apps[]` | `packages[<id>].metadata` |
| Versions | `packages[<id>][]`, a list | `versions`, a map keyed by SHA-256 |
| Localised text | `localized: {"en-US": {...}}` or a plain string | `{"en-US": "text"}` maps |
| App icon | `icon: "<name>"`, resolved as `/icons/<name>` | `icon: {"en-US": {name, sha256, size}}` |
| Screenshots | `localized[locale].phoneScreenshots`, **names only** | `screenshots.phone[locale]`, **full file entries** |
| Image hashes | not included | included, and verified on download |
| APK fields | `apkName`, `hash`, `hashType`, `size` | `file.{name, sha256, size}` |
| Mirrors | `repo.mirrors`, a list of strings | `repo.mirrors`, a list of objects |
| Changelogs | yes, in `whatsNew` | not carried over by `fdroidserver` |

Consequences this project has to respect:

- **v1 has no screenshots.** `fdroidserver` does not put them in
  `index-v1.json` at all; the fixture confirms it. They are referenced from
  `localized[locale].phoneScreenshots` as bare filenames, and the client
  resolves those to `/<package>/<locale>/phoneScreenshots/<name>`.
- **`features` is a list of *mandatory* hardware requirements.** F-Droid
  refuses to offer the app to a device missing any of them, and
  `fdroidserver/update.py` only records a `uses-feature` that has an
  `android:name` **and** is required (`android:required` absent or `"true"`).
  A feature declared only as `android:glEsVersion` has no name and is never
  reported - which is also why `androguard.get_features()`, the function
  fdroidserver actually calls, omits it.

  This project's manifest declares no required features:

  ```xml
  <uses-feature android:glEsVersion="0x30000"/>                          <!-- no name -->
  <uses-feature android:name="android.hardware.touchscreen"
               android:required="false"/>
  ```

  so the index carries no feature list at all. An earlier version of this
  project synthesised `glEsVersion196608` and listed every optional feature,
  and users reported the app as incompatible with devices that ran it fine from
  Google Play. `tests/test_features.py` pins the rule.

  Because a change to these rules would be masked by the committed cache,
  `releases.json` records `manifestRules`, the version of the rules that
  produced each entry. A mismatch discards the cache and re-reads the APK once.
- **v1 requires `apps[]` to be populated.** `AppV1` carries `name` and
  `summary`; the `packages` entries do not. An empty `apps` array yields a
  repository F-Droid cannot display a name for.
- **`hashType` must be `"sha256"`.** `PackageV1.toPackageVersionV2()` asserts it.
- **`repo.icon` is a plain filename** at the repository root, while the app icon
  is resolved as `/icons/<name>`, so the file has to live in `icons/`.
- **`versionCode` is mandatory** in both formats and is what the client compares
  to decide whether an update exists. It cannot be derived from the URL, which
  is why the APK has to be read at least once per new release.

## Signing

`fdroidserver/signindex.py` documents why the two formats use different
algorithms:

> The old indexes must be signed by SHA1withRSA otherwise they will no longer be
> compatible with old Androids.

`bdon-fdroid` matches that:

| File | Digest | Signature |
| --- | --- | --- |
| `entry.jar` | SHA-256 | SHA256withRSA |
| `index-v2.jar` | SHA-256 | SHA256withRSA |
| `index-v1.jar` | SHA1 | SHA1withRSA |

Three consequences worth knowing:

- A modern JDK's `jarsigner -verify` **refuses** the SHA-1 signature, reporting
  the jar as unsigned. That is expected and is why the self-check skips it;
  F-Droid's own verifier accepts it.
- `jarsigner -verify -strict` fails for *any* repository key, because these keys
  are self-signed and untimestamped. `fdroidserver` treats exit code 4 (chain
  invalid) as success for exactly this reason. The self-check therefore looks
  for `jar verified` rather than the exit status.
- Zip entry timestamps are pinned, so signing identical input twice produces
  identical bytes and an unchanged run commits no diff.

Passwords are passed as `-storepass:env` / `-keypass:env`, so they never appear
in the process list or in a log.

## Reading the APK

Two things have to be read out of the file, and neither is worth a dependency.

**`AndroidManifest.xml` is binary XML.** `axml.py` implements the chunked format
directly: the string pool (UTF-8 and UTF-16), the attribute stream, and the
resolvers for integer, boolean, string and resource-reference values. It yields
`package`, `versionCode`, `versionName`, min/target/max SDK, requested
permissions and hardware features.

Two traps worth recording:

- A chunk's body begins at that chunk's **own** `headerSize`, which is 16 for
  element chunks, not at a fixed 8 bytes. Slicing at 8 silently misaligns every
  attribute.
- The attribute struct is `ns, name, rawValue, size(2), res0(1), dataType(1),
  data(4)`, so unpacking it as five 32-bit values puts the data type in the wrong
  place.

**The signing certificate lives in the APK Signing Block.** `apksigner_info.py`
walks the block that sits immediately before the ZIP central directory:

```
uint64 size_of_block
repeated { uint64 length; uint32 id; value }   <- length is a uint64 here
uint64 size_of_block
magic "APK Sig Block 42"
```

then, for v2 (`0x7109871a`) and v3 (`0xf05368c0`):

```
signers        -> length-prefixed sequence
  signer       -> length-prefixed sequence
    signed_data-> digests, then certificates, then attributes
    (v3 only) min_sdk, max_sdk
    signatures
    public_key
```

The nesting has to be walked with a cursor, not by nesting slices: the
certificates sit *after* the digests inside the signed data, and a length prefix
at the wrong level yields a plausible-looking fragment rather than an error. The
first certificate of the first signer is what `fdroidserver` reports as
`signer`.

Correctness was checked against a real `F-Droid.apk`, where the extracted
fingerprint matched the `signer` field in f-droid.org's own published index
byte for byte.

A v1-only APK has no signing block. The parser returns `None` and the index
simply omits `signer`, rather than guessing. Every app with
`targetSdkVersion` 30 or higher is required to carry a v2 signature, so this
does not apply to the APKs this project cares about.

## The scraper

The site's HTML references its bundles with content-hashed names:

```html
<script defer="defer" src="//s1.biligames.com/fe-static/.../chunk-common.79593525.js"></script>
```

so nothing can be hardcoded; the page is re-read every time. Two details that
bite:

- The page also contains `document.write('<script src="..." + new Date()...)`,
  which a naive `<script src>` regex picks up as a URL containing `+` and
  spaces. Those are discarded.
- Minifiers may escape slashes, so the pattern accepts `https:\/\/...` and the
  escapes are undone afterwards.

### Finding the app icon

The site has no `<link rel=apple-touch-icon>`, and its only `<link rel=icon>` is
a 16px `favicon.ico` - far too small, and the wrong shape. The real icon is a
webpack asset referenced by name from a bundle and resolved against the public
path:

```js
ga = t.p + "img/icon.e419ff81.png"    where  t.p = "//s1.../gw/"
```

So both halves are read from the bundles - the public path from the assignment
`r.p="..."` in the bootstrap, and the asset name by matching `img/*icon*.` - and
resolved against each other. Nothing is hardcoded, so the content-hash renames the
site does on every deploy are followed automatically. On the live site exactly one
asset matches.

Ambiguity is treated as failure rather than a guess: picking the wrong image
would publish a wrong app icon to every client that syncs the repository.
`assets.appIconUrl` in `repo.json` is the escape hatch.

Image file extensions are taken from the response's `Content-Type`, never from
the URL. The Google Play screenshot URLs end in `?w=2560-h1440-rw` with no
filename at all and serve `image/webp`, so a URL-derived name would have written
WebP bytes into a `.jpg`. An unrecognised type is skipped with a warning rather
than guessed.

The scraper refuses to continue if it finds zero or more than one `.apk` URL, or
if the URL is not on a `biligames.com` host. A silent wrong answer here would
mean a repository that installs the wrong application, so ambiguity is an error
rather than a guess.

## Change detection

`releases.json` is committed, and is the reason the daily job is cheap. A release
is identified by its URL; the cached SHA-256 is trusted only while
`(Content-Length, ETag, Last-Modified)` all still match what the CDN reports. If
any of them differ for a known URL, the APK is downloaded again, because
publishing a stale hash would let the client install a file that was never
verified.

The file is written deterministically - fixed key order, sorted releases,
trailing newline - so an unchanged run produces an empty git diff.
