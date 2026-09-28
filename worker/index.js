/**
 * Redirecting front-end for the bdon-fdroid repository.
 *
 * The problem this solves: F-Droid clients do not agree on how to find an APK.
 *
 *   - The official F-Droid client understands "mirrors" and falls through to
 *     the next one when a request 404s, so pointing the mirror at the
 *     publisher's CDN works.
 *   - Neo Store treats a 404 as a final answer and only rotates on an
 *     exception, so it fails outright.
 *   - Obtainium does not read `mirrors` at all; it always builds the download
 *     URL as `<repo address>/<apkName>`.
 *
 * All three agree on one thing: the APK must be reachable at the repository
 * address. GitHub Pages is static and cannot redirect, so this Worker sits in
 * front of it and does two things:
 *
 *   - `<repo>/<something>.apk`  ->  302 to the publisher's CDN
 *   - anything else              ->  proxied to GitHub Pages, byte for byte
 *
 * Nothing is stored here or by this project: the 450 MB APK is still served by
 * bilibili, and the index is still served by GitHub Pages. Only the redirect
 * is ours.
 *
 * The redirect target is read from `repo.mirrors[0].url` in index-v2.json rather
 * than hardcoded, so if the publisher moves the APK to a different CDN host the
 * scraper picks it up and this Worker follows along with no redeploy. That read
 * is not an extra failure mode: a client has to fetch the index before it can
 * ask for an APK, so the index is already known to be reachable at that point.
 *
 * Deploy once with `npx wrangler deploy`; see
 * docs/setup-github-actions-and-pages.md.
 */

const MIRROR_TTL_MS = 10 * 60 * 1000;

/** Per-isolate cache for the resolved mirror, to avoid refetching the index. */
let mirrorCache = { url: null, expires: 0 };

/**
 * Drop the cached mirror. Exported so tests do not leak a resolved mirror from
 * one case into the next.
 */
export function resetMirrorCache() {
  mirrorCache = { url: null, expires: 0 };
}

/**
 * Read the mirror base URL out of an index-v2 document.
 *
 * @param {unknown} index parsed index-v2.json
 * @returns {string} the mirror base, guaranteed to end in "/"
 */
export function mirrorFromIndex(index) {
  const mirrors = index?.repo?.mirrors;
  if (!Array.isArray(mirrors) || mirrors.length === 0) {
    throw new Error('index has no repo.mirrors');
  }
  const url = typeof mirrors[0] === 'string' ? mirrors[0] : mirrors[0]?.url;
  if (typeof url !== 'string' || url === '') {
    throw new Error('index has a malformed first mirror');
  }
  return url.endsWith('/') ? url : `${url}/`;
}

/**
 * The CDN URL for an APK request path.
 *
 * Only the final path segment is used: the mirror base is a directory, and the
 * APK name is whatever the index asked for. The incoming path is the repository
 * address plus the name, so the repository prefix has to come off first.
 *
 * @param {string} pathname request path, e.g. "/fdroid/repo/Game_1.0.apk"
 * @param {string} mirrorBase base URL from {@link mirrorFromIndex}
 * @returns {string} absolute URL on the publisher's CDN
 */
export function apkTarget(pathname, mirrorBase) {
  const name = pathname.slice(pathname.lastIndexOf('/') + 1);
  if (name === '') {
    throw new Error('no file name in request path');
  }
  return new URL(encodeURIComponent(name), mirrorBase).toString();
}

/** True when this request is for an APK rather than for repository metadata. */
export function isApkRequest(pathname) {
  return /\.apk$/i.test(pathname);
}

export default {
  /**
   * @param {Request} request
   * @param {Record<string, string>} env bindings, notably PAGES_ORIGIN
   * @param {unknown} ctx execution context
   */
  async fetch(request, env, ctx) {
    const origin = env?.PAGES_ORIGIN;
    if (!origin) {
      return new Response('PAGES_ORIGIN is not configured\n', { status: 500 });
    }
    const incoming = new URL(request.url);

    if (isApkRequest(incoming.pathname)) {
      return this.redirectToApk(incoming, origin);
    }
    return this.proxy(request, incoming, origin);
  },

  /**
   * 302 the client to the publisher's own copy of the APK.
   *
   * 302 rather than 307 because this only ever handles GET, and because some
   * F-Droid clients handle a plain redirect more predictably than a temporary
   * one. The client verifies the SHA-256 after downloading, so the redirect
   * costs nothing in integrity.
   */
  async redirectToApk(incoming, origin) {
    let mirrorBase;
    try {
      mirrorBase = await this.resolveMirror(origin);
      const location = apkTarget(incoming.pathname, mirrorBase);
      return new Response(null, {
        status: 302,
        headers: {
          Location: location,
          // The mirror is a CDN; do not let anything cache the redirect
          // itself, or a new release would be shadowed by a stale 302.
          'Cache-Control': 'no-store',
        },
      });
    } catch (cause) {
      // Deliberately 502 rather than 404: the APK is not missing, the redirect
      // could not be built, and a client treats these differently.
      return new Response(
        `could not resolve the APK mirror: ${cause?.message ?? cause}\n`,
        { status: 502, headers: { 'Content-Type': 'text/plain' } },
      );
    }
  },

  /** Fetch index-v2.json from GitHub Pages, caching the mirror briefly. */
  async resolveMirror(origin) {
    const now = Date.now();
    if (mirrorCache.url && now < mirrorCache.expires) {
      return mirrorCache.url;
    }
    const indexUrl = new URL(`${origin.replace(/\/$/, '')}/fdroid/repo/index-v2.json`);
    const response = await fetch(indexUrl.toString(), {
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) {
      throw new Error(`index-v2.json returned HTTP ${response.status}`);
    }
    const mirrorBase = mirrorFromIndex(await response.json());
    mirrorCache = { url: mirrorBase, expires: now + MIRROR_TTL_MS };
    return mirrorBase;
  },

  /**
   * Pass everything else through to GitHub Pages unchanged.
   *
   * The index files are signed, so the body has to arrive byte for byte as
   * published; streaming the response through without touching it keeps the
   * signature valid. Range requests are forwarded for completeness, though the
   * index files are small enough not to need them.
   */
  async proxy(request, incoming, origin) {
    const base = origin.replace(/\/$/, '');
    const target = `${base}${incoming.pathname}${incoming.search}`;
    const upstream = await fetch(new Request(target, request));
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: upstream.headers,
    });
  },
};
