"""Small HTTP helpers built on :mod:`urllib.request`.

Only the standard library is available by design, so this module wraps the
handful of things the project needs:

* fetching a whole resource into memory (with a sane user agent),
* a ``HEAD`` request returning just the metadata,
* a *resumable* ranged download that also computes the SHA-256 on the fly.

The official APK is ~446 MB, so the download is streamed in fixed size chunks and
can be resumed after a dropped connection using ``Range`` requests.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

USER_AGENT = "bdon-fdroid/1.0 (+https://github.com/f-droid/bdon-fdroid)"
DEFAULT_TIMEOUT = 60
CHUNK_SIZE = 1024 * 1024


class HttpError(RuntimeError):
    """Raised for any non-recoverable HTTP failure."""


# ``urllib`` raises a mix of exception types depending on where it fails; a
# malformed URL surfaces as http.client.InvalidURL rather than URLError.
_TRANSPORT_ERRORS = (
    urllib.error.HTTPError,
    urllib.error.URLError,
    http.client.HTTPException,
    ValueError,
    OSError,
)


@dataclass(frozen=True)
class HeadInfo:
    """Result of a ``HEAD`` request."""

    url: str
    size: int | None
    etag: str | None
    last_modified: str | None
    accept_ranges: str | None

    @property
    def supports_ranges(self) -> bool:
        return (self.accept_ranges or "").lower() == "bytes"


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _open(url: str, method: str, timeout: int = DEFAULT_TIMEOUT):
    request = urllib.request.Request(url, method=method)
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept-Encoding", "identity")
    return urllib.request.urlopen(request, timeout=timeout, context=_ssl_context())


def resolve(base: str, url: str) -> str:
    """Resolve ``url`` against ``base``.

    The official site uses protocol-relative URLs (``//s1.biligames.com/...``),
    which :func:`urllib.parse.urljoin` does not handle correctly on its own, so
    they are upgraded to ``https://`` first.
    """
    if url.startswith("//"):
        url = "https:" + url
    return urllib.parse.urljoin(base, url)


def head(url: str, timeout: int = DEFAULT_TIMEOUT) -> HeadInfo:
    """Return metadata about ``url`` without downloading its body."""
    try:
        with _open(url, "HEAD", timeout) as response:
            return _head_from_response(url, response)
    except urllib.error.HTTPError as exc:
        # A few CDNs reject HEAD outright; fall back to a ranged GET.
        if exc.code not in (403, 405, 501):
            raise HttpError(f"HEAD {url} failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, http.client.HTTPException, ValueError) as exc:
        raise HttpError(f"HEAD {url} failed: {exc}") from exc

    try:
        with _open(url, "GET", timeout) as response:
            return _head_from_response(url, response)
    except _TRANSPORT_ERRORS as exc:
        raise HttpError(f"GET {url} failed: {exc}") from exc


def _head_from_response(url: str, response) -> HeadInfo:
    length = response.headers.get("Content-Length")
    return HeadInfo(
        url=response.geturl(),
        size=int(length) if length and length.isdigit() else None,
        etag=(response.headers.get("ETag") or "").strip('"') or None,
        last_modified=response.headers.get("Last-Modified"),
        accept_ranges=response.headers.get("Accept-Ranges"),
    )


def get_text(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Fetch ``url`` and decode it as UTF-8, replacing undecodable bytes."""
    try:
        with _open(url, "GET", timeout) as response:
            return response.read().decode("utf-8", "replace")
    except _TRANSPORT_ERRORS as exc:
        raise HttpError(f"GET {url} failed: {exc}") from exc


def get_bytes(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """Fetch ``url`` and return the whole body."""
    return get_bytes_with_type(url, timeout)[0]


def get_bytes_with_type(url: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[bytes, str]:
    """Fetch ``url``, returning ``(body, content_type)``.

    The type is needed to name an image file correctly: some CDNs serve images
    from URLs with no filename in them at all, so the extension cannot be
    derived from the path.
    """
    try:
        with _open(url, "GET", timeout) as response:
            content_type = response.headers.get("Content-Type") or ""
            return response.read(), content_type
    except _TRANSPORT_ERRORS as exc:
        raise HttpError(f"GET {url} failed: {exc}") from exc


def download(
    url: str,
    dest: str,
    expected_size: int | None = None,
    progress=None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[str, int]:
    """Download ``url`` to ``dest``, resuming a partial file if one exists.

    Returns ``(sha256_hexdigest, size_in_bytes)``. The digest is computed while
    the bytes stream past, so the file is never read a second time.
    """
    partial = dest + ".part"
    already = os.path.getsize(partial) if os.path.exists(partial) else 0

    if expected_size is not None and already > expected_size:
        # A stale or corrupt partial file; start over.
        os.unlink(partial)
        already = 0

    digest = hashlib.sha256()
    if already:
        # Re-hash what we already have so the digest stays correct.
        with open(partial, "rb") as handle:
            for block in iter(lambda: handle.read(CHUNK_SIZE), b""):
                digest.update(block)

    response = None
    try:
        if already:
            request = urllib.request.Request(url, method="GET")
            request.add_header("User-Agent", USER_AGENT)
            request.add_header("Accept-Encoding", "identity")
            request.add_header("Range", f"bytes={already}-")
            try:
                response = urllib.request.urlopen(
                    request, timeout=timeout, context=_ssl_context()
                )
            except _TRANSPORT_ERRORS as exc:
                print(f"  range request failed ({exc}); restarting download")
                response = None
            if response is not None and response.status != 206:
                # The server ignored the Range header and is sending the whole
                # object. Appending it to the partial file would produce a
                # corrupt result with a plausible-looking size, so start over.
                print(
                    f"  server ignored the range request "
                    f"(HTTP {response.status}); restarting download"
                )
                response.close()
                response = None

        if response is None:
            already = 0
            digest = hashlib.sha256()
            response = _open(url, "GET", timeout)

        with response:
            total = response.headers.get("Content-Length")
            total = int(total) + already if total and total.isdigit() else expected_size
            with open(partial, "ab" if already else "wb") as out:
                while True:
                    block = response.read(CHUNK_SIZE)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    if progress is not None:
                        progress(len(block), total)
    except _TRANSPORT_ERRORS as exc:
        raise HttpError(f"download of {url} failed: {exc}") from exc

    size = os.path.getsize(partial)
    if expected_size is not None and size != expected_size:
        raise HttpError(
            f"downloaded {size} bytes from {url}, expected {expected_size}"
        )
    os.replace(partial, dest)
    return digest.hexdigest(), size
