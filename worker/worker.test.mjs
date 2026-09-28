/**
 * Tests for the redirecting front-end.
 *
 * These are the cases that actually broke a client, so they are worth pinning:
 * a redirect that resolves to the wrong directory, a double slash, or a proxy
 * that alters the signed index body would all fail silently in production.
 *
 * Run with:  node --test worker/
 */

import { strict as assert } from 'node:assert';
import { after, before, beforeEach, describe, it } from 'node:test';

import worker, {
  apkTarget,
  isApkRequest,
  mirrorFromIndex,
  resetMirrorCache,
} from './index.js';

const PAGES_ORIGIN = 'https://example.github.io/bdon-fdroid';
const CDN = 'https://cdn.example/sirius/apk/';
const APK = 'BanGDreamOurNotes_1.0.1_2026_09_17_22_42_02.apk';

const env = { PAGES_ORIGIN };

/** Stand in for the Worker runtime's global fetch. */
function stubFetch(routes) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    calls.push(url);
    for (const [pattern, handler] of routes) {
      if (url === pattern || url.startsWith(pattern)) {
        return handler(init);
      }
    }
    return new Response('not found', { status: 404 });
  };
  return {
    calls,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

/** The index GitHub Pages serves, which the Worker reads its mirror from. */
function indexRoute() {
  return [
    [
      `${PAGES_ORIGIN}/fdroid/repo/index-v2.json`,
      async () =>
        new Response(
          JSON.stringify({
            repo: { mirrors: [{ url: CDN, countryCode: 'CN' }] },
            packages: {},
          }),
          { headers: { 'Content-Type': 'application/json' } },
        ),
    ],
  ];
}

describe('mirrorFromIndex', () => {
  it('reads the v2 mirror object', () => {
    assert.equal(
      mirrorFromIndex({ repo: { mirrors: [{ url: 'https://cdn.example/apk/' }] } }),
      'https://cdn.example/apk/',
    );
  });

  it('accepts the v1 string form', () => {
    assert.equal(
      mirrorFromIndex({ repo: { mirrors: ['https://cdn.example/apk'] } }),
      'https://cdn.example/apk/',
    );
  });

  it('adds the trailing slash when it is missing', () => {
    // Without it, apkTarget would drop the last directory.
    assert.equal(
      mirrorFromIndex({ repo: { mirrors: [{ url: 'https://cdn.example/apk' }] } }),
      'https://cdn.example/apk/',
    );
  });

  it('rejects an index with no mirrors', () => {
    assert.throws(() => mirrorFromIndex({ repo: { mirrors: [] } }), /no repo\.mirrors/);
    assert.throws(() => mirrorFromIndex({}), /no repo\.mirrors/);
    assert.throws(() => mirrorFromIndex(null), /no repo\.mirrors/);
  });

  it('rejects a malformed first mirror', () => {
    assert.throws(
      () => mirrorFromIndex({ repo: { mirrors: [{ countryCode: 'CN' }] } }),
      /malformed/,
    );
  });
});

describe('apkTarget', () => {
  it('resolves to the exact upstream URL', () => {
    assert.equal(apkTarget(`/fdroid/repo/${APK}`, CDN), `${CDN}${APK}`);
  });

  it('does not double the slash', () => {
    // A client that concatenates naively produces "//BanGDream..." here, which
    // the CDN answers with 403. This is the failure the whole Worker exists to
    // prevent.
    const target = apkTarget(`/fdroid/repo/${APK}`, CDN);
    assert.ok(!target.includes('.apk//'), target);
    assert.ok(!target.includes('//' + APK), target);
  });

  it('ignores the repository prefix, only using the file name', () => {
    // The mirror base is a directory on the publisher's CDN, not a path on our
    // own site, so the /fdroid/repo prefix must be discarded.
    assert.equal(apkTarget(`/fdroid/repo/${APK}`, CDN), apkTarget(`/${APK}`, CDN));
  });

  it('percent-encodes a name that needs it', () => {
    assert.equal(
      apkTarget('/fdroid/repo/Game 1.0.apk', CDN),
      'https://cdn.example/sirius/apk/Game%201.0.apk',
    );
  });

  it('rejects a path with no file name', () => {
    assert.throws(() => apkTarget('/fdroid/repo/', CDN), /no file name/);
  });
});

describe('isApkRequest', () => {
  it('matches an apk path', () => {
    assert.equal(isApkRequest(`/fdroid/repo/${APK}`), true);
    assert.equal(isApkRequest('/fdroid/repo/Game.APK'), true, 'case-insensitive');
  });

  it('does not match repository metadata', () => {
    for (const path of [
      '/fdroid/repo/index-v2.json',
      '/fdroid/repo/entry.jar',
      '/fdroid/repo/icon.jpg',
      '/',
    ]) {
      assert.equal(isApkRequest(path), false, path);
    }
  });
});

describe('fetch', () => {
  let stub;

  before(() => {
    stub = stubFetch(indexRoute());
  });

  after(() => {
    stub.restore();
    resetMirrorCache();
  });

  beforeEach(() => resetMirrorCache());

  it('302s an APK request to the publisher CDN', async () => {
    const response = await worker.fetch(
      new Request(`https://worker.example/fdroid/repo/${APK}`),
      env,
    );
    assert.equal(response.status, 302);
    assert.equal(response.headers.get('Location'), `${CDN}${APK}`);
    assert.match(response.headers.get('Cache-Control') ?? '', /no-store/);
  });

  it('proxies the index to GitHub Pages without touching the body', async () => {
    // The index is signed, so altering a single byte would break every client.
    const body = '{"repo":{"name":"signed"}}';
    const local = stubFetch([
      [`${PAGES_ORIGIN}/fdroid/repo/index-v2.json`, async () =>
        new Response(body, { headers: { 'Content-Type': 'application/json' } })],
    ]);
    try {
      const response = await worker.fetch(
        new Request('https://worker.example/fdroid/repo/index-v2.json'),
        env,
      );
      assert.equal(response.status, 200);
      assert.equal(await response.text(), body);
      assert.equal(
        local.calls.at(-1),
        `${PAGES_ORIGIN}/fdroid/repo/index-v2.json`,
        'should be fetched from Pages',
      );
    } finally {
      local.restore();
    }
  });

  it('preserves the query string when proxying', async () => {
    const local = stubFetch([[PAGES_ORIGIN, async () => new Response('ok')]]);
    try {
      await worker.fetch(
        new Request('https://worker.example/fdroid/repo/entry.json?x=1'),
        env,
      );
      assert.equal(local.calls.at(-1), `${PAGES_ORIGIN}/fdroid/repo/entry.json?x=1`);
    } finally {
      local.restore();
    }
  });

  it('returns 500 when PAGES_ORIGIN is missing', async () => {
    const response = await worker.fetch(
      new Request(`https://worker.example/fdroid/repo/${APK}`),
      {},
    );
    assert.equal(response.status, 500);
  });
});

describe('redirect failure modes', () => {
  beforeEach(() => resetMirrorCache());

  it('returns 502, not 404, when the index cannot be read', async () => {
    // A 404 would tell the client the APK is gone; 502 says try again.
    const local = stubFetch([
      [`${PAGES_ORIGIN}/fdroid/repo/index-v2.json`, async () =>
        new Response('nope', { status: 500 })],
    ]);
    try {
      const response = await worker.fetch(
        new Request(`https://worker.example/fdroid/repo/${APK}`),
        env,
      );
      assert.equal(response.status, 502);
    } finally {
      local.restore();
    }
  });

  it('returns 502 when the index has no mirror', async () => {
    const local = stubFetch([
      [`${PAGES_ORIGIN}/fdroid/repo/index-v2.json`, async () =>
        new Response(JSON.stringify({ repo: { mirrors: [] } }))],
    ]);
    try {
      const response = await worker.fetch(
        new Request(`https://worker.example/fdroid/repo/${APK}`),
        env,
      );
      assert.equal(response.status, 502);
    } finally {
      local.restore();
    }
  });
});
