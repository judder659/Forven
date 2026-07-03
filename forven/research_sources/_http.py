"""Shared HTTP client for research-source connectors.

Adds per-domain rate limiting, retry on 5xx/429, a consistent User-Agent,
and — critically — SSRF protection: every URL (initial + each redirect hop)
is resolved and blocked if it points at loopback, RFC1918, link-local, or
cloud-metadata addresses. Without this, an agent ingesting a hostile blog
post or Reddit thread could be instructed to fetch http://127.0.0.1:8003
or http://169.254.169.254 and turn the user's machine into a confused
deputy. See security audit 2026-04-23.

Phase 5 / P5-T02: SSRF helpers moved to ``forven.security.url_safety`` so
the same policy can also gate MCP HTTP transports. Re-exported here for
backwards compatibility with existing research-source modules.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import urlparse

import httpx

from forven.security.url_safety import (  # noqa: F401 (re-exported for callers/tests)
    UnsafeUrlError,
    _FORBIDDEN_HOSTNAMES,
    _is_blocked_ip,
    _validate_public_url,
    _validate_public_url_dns,
    _validate_public_url_static,
)

USER_AGENT = "forven-research-sources/1.0 (+https://github.com/judder659/Forven)"

# Max bytes we'll buffer from any single fetch. 5 MB is comfortably more than
# a Reddit thread, GitHub README, or blog article, and small enough that an
# agent cannot be coaxed into OOM'ing a tester's machine by fetching a
# multi-GB file.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class RateLimitExceeded(RuntimeError):
    pass


class SourceHttpClient:
    def __init__(
        self,
        *,
        default_rate_per_min: int = 30,
        per_domain: dict[str, int] | None = None,
        timeout_s: float = 15.0,
        max_retries: int = 3,
    ) -> None:
        self._default = max(int(default_rate_per_min), 1)
        self._per_domain = {k.lower(): max(int(v), 1) for k, v in (per_domain or {}).items()}
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._transport: httpx.BaseTransport | None = None
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            transport_kwargs = {"transport": self._transport} if self._transport else {}
            # follow_redirects is OFF because we need to re-validate each hop
            # against the SSRF blocklist. A public-looking URL can 302 to
            # http://127.0.0.1:8003 and httpx's auto-follow would happily
            # oblige.
            self._client = httpx.Client(timeout=self._timeout, follow_redirects=False, **transport_kwargs)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _budget(self, host: str) -> int:
        return self._per_domain.get(host.lower(), self._default)

    def _tick(self, host: str) -> None:
        now = time.monotonic()
        window = 60.0
        q = self._calls[host]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= self._budget(host):
            raise RateLimitExceeded(f"rate limit hit for {host}")
        q.append(now)

    def get(self, url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None) -> httpx.Response:
        # SSRF guard: validate BEFORE the rate-limit tick so a single bad URL
        # doesn't consume the host's budget. DNS is skipped when a mock
        # transport is wired in (tests) since no real network call happens.
        _validate_public_url(url, skip_dns=self._transport is not None)
        host = urlparse(url).hostname or ""
        self._tick(host)
        merged = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"}
        if headers:
            merged.update(headers)
        client = self._get_client()
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = self._fetch_with_redirects(client, url, merged, params)
            except httpx.ReadTimeout:
                raise
            except UnsafeUrlError:
                # Redirect target failed validation — do NOT retry, since the
                # target isn't going to change on retry and we don't want to
                # mask the error.
                raise
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(0.5 * (2 ** attempt))
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self._max_retries - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
            return resp
        # Note: Retry-After header is not honored yet; fixed exponential backoff is adequate for v1.
        assert last_exc is not None
        raise last_exc

    def _fetch_with_redirects(
        self,
        client: httpx.Client,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        *,
        max_hops: int = 5,
    ) -> httpx.Response:
        """GET `url`, manually following up to `max_hops` redirects.

        Re-validates every hop against the SSRF blocklist and caps the final
        response body at MAX_RESPONSE_BYTES to prevent OOM. Params only
        attach to the initial request — redirects carry their own query
        string.
        """
        current_url = url
        for hop in range(max_hops + 1):
            with client.stream(
                "GET",
                current_url,
                headers=headers,
                params=params if hop == 0 else None,
            ) as resp:
                if resp.is_redirect:
                    next_url = resp.headers.get("location")
                    if not next_url:
                        # Malformed redirect, return as-is.
                        return resp
                    # Resolve relative redirects against the current URL.
                    next_url = str(httpx.URL(current_url).join(next_url))
                    _validate_public_url(
                        next_url, skip_dns=self._transport is not None
                    )
                    current_url = next_url
                    continue
                # Pre-flight Content-Length check avoids reading a huge body.
                clen = resp.headers.get("content-length")
                if clen is not None:
                    try:
                        if int(clen) > MAX_RESPONSE_BYTES:
                            raise UnsafeUrlError(
                                f"response too large: {clen} bytes > "
                                f"{MAX_RESPONSE_BYTES} byte limit"
                            )
                    except ValueError:
                        pass
                # Stream-read with a hard cap so a server lying about
                # Content-Length (or not sending it) still can't OOM us.
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_RESPONSE_BYTES:
                        raise UnsafeUrlError(
                            f"response exceeded {MAX_RESPONSE_BYTES} byte limit"
                        )
                # Rebuild a Response the caller can .text / .json() on. `buf`
                # was read via iter_bytes(), which ALREADY applied any
                # Content-Encoding (gzip/deflate/br) transfer decoding — so it
                # holds the decoded body. We must therefore drop the
                # Content-Encoding (and now-stale Content-Length) headers before
                # handing them back: httpx's Response constructor decodes again
                # based on Content-Encoding, and re-decoding an already-decoded
                # body raises `zlib.error: Error -3 ... incorrect header check`.
                # This bit every discover_*/inspect_* fetch against a server
                # that compresses responses (the GitHub API always; most blogs
                # and forums by default).
                decoded_headers = httpx.Headers(
                    [
                        (key, value)
                        for key, value in resp.headers.multi_items()
                        if key.lower() not in ("content-encoding", "content-length")
                    ]
                )
                return httpx.Response(
                    status_code=resp.status_code,
                    headers=decoded_headers,
                    content=bytes(buf),
                    request=resp.request,
                )
        raise UnsafeUrlError(
            f"exceeded {max_hops} redirects starting from {url}"
        )
