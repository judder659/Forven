"""Reddit connector.

Reddit blocks anonymous requests to its .json endpoints (403 on www, a login
redirect on old.reddit). Its public RSS feeds still answer, so without a key
search and thread reads use those. With a Reddit app key (client_id and
client_secret in Settings › Research, or REDDIT_CLIENT_ID and
REDDIT_CLIENT_SECRET) they use the OAuth JSON API, which adds scores and
comment counts.

The anonymous RSS limit is tight: about one request per 60-second window per
IP. Forven remembers when the window resets and waits for it once per call.
"""
from __future__ import annotations

import html
import os
import re
import threading
import time
from calendar import timegm
from typing import Any
from urllib.parse import quote, urlparse

import feedparser
import httpx

from forven.research_sources._http import RateLimitExceeded, SourceHttpClient

_MAX_COMMENTS = 20
_MAX_DEPTH = 3
_BASE = "https://www.reddit.com"
_OAUTH_BASE = "https://oauth.reddit.com"
_OAUTH_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

# Reddit 429s and 403s generic bot UAs on its public endpoints; a browser UA
# gets the RSS feeds.
_REDDIT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Longest pause waited out inside a call (agent tool calls time out at 120s);
# anything longer is reported as rate_limited.
_MAX_RSS_WAIT_S = 65.0

_rss_lock = threading.Lock()
_rss_blocked_until = 0.0
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


def _client(*, rate_per_min: int = 30) -> SourceHttpClient:
    # One attempt per call: the shared client retries 429s after half a
    # second, which only burns Reddit's budget. 429s are handled here.
    return SourceHttpClient(
        per_domain={"www.reddit.com": rate_per_min, "oauth.reddit.com": rate_per_min},
        default_rate_per_min=rate_per_min,
        max_retries=1,
    )


def _error_code_for_status(status: int) -> str:
    if status == 429:
        return "rate_limited"
    return f"http_{status // 100}xx"


def _http_error(resp: httpx.Response) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": _error_code_for_status(resp.status_code),
        "error": f"http {resp.status_code}",
    }


# ── OAuth ──────────────────────────────────────────────────────────────────────


def oauth_credentials(cfg: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """client_id/client_secret from the reddit settings block, else the env."""
    cfg = cfg or {}
    client_id = cfg.get("client_id") or os.getenv("REDDIT_CLIENT_ID")
    client_secret = cfg.get("client_secret") or os.getenv("REDDIT_CLIENT_SECRET")
    cid = client_id.strip() if isinstance(client_id, str) and client_id.strip() else None
    secret = client_secret.strip() if isinstance(client_secret, str) and client_secret.strip() else None
    return cid, secret


def fetch_oauth_token(client_id: str, client_secret: str) -> dict[str, Any]:
    """Fetch an app-only Reddit OAuth token using configured script-app credentials."""
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    if not cid or not secret:
        return {
            "ok": False,
            "error_code": "auth_required",
            "error": "Reddit client_id/client_secret are required for OAuth.",
        }
    try:
        resp = httpx.post(
            _OAUTH_TOKEN_URL,
            auth=(cid, secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": _REDDIT_UA},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        return {"ok": False, "error_code": "auth_failed", "error": str(exc)}
    if resp.status_code >= 400:
        return {
            "ok": False,
            "error_code": _error_code_for_status(resp.status_code),
            "error": f"oauth http {resp.status_code}",
        }
    try:
        payload = resp.json()
    except ValueError:
        return {"ok": False, "error_code": "parse", "error": "invalid oauth json"}
    token = payload.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return {"ok": False, "error_code": "auth_failed", "error": "missing reddit access token"}
    try:
        expires_in = float(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600.0
    return {"ok": True, "access_token": token, "expires_in": expires_in}


def cached_access_token(client_id: str | None, client_secret: str | None) -> str | None:
    """An app-only token for these credentials, reused until shortly before expiry."""
    if not client_id or not client_secret:
        return None
    key = (client_id, client_secret)
    cached = _token_cache.get(key)
    if cached and cached[1] > time.time():
        return cached[0]
    result = fetch_oauth_token(client_id, client_secret)
    if not result.get("ok"):
        return None
    token = str(result["access_token"])
    _token_cache[key] = (token, time.time() + max(float(result.get("expires_in") or 3600) - 60.0, 60.0))
    return token


# ── RSS (no key) ───────────────────────────────────────────────────────────────


def _retry_after_seconds(resp: httpx.Response) -> float:
    for header in ("x-ratelimit-reset", "retry-after"):
        raw = resp.headers.get(header)
        if raw:
            try:
                return max(float(raw), 0.0)
            except ValueError:
                continue
    return 60.0


def _note_rss_budget(resp: httpx.Response) -> None:
    """Remember when Reddit's window resets once a response says it's used up."""
    global _rss_blocked_until
    if resp.status_code == 429:
        pause = _retry_after_seconds(resp)
    else:
        try:
            remaining = float(resp.headers.get("x-ratelimit-remaining", "1"))
        except ValueError:
            return
        if remaining >= 1:
            return
        pause = _retry_after_seconds(resp)
    with _rss_lock:
        _rss_blocked_until = max(_rss_blocked_until, time.monotonic() + pause)


def _rss_get(client: SourceHttpClient, url: str, params: dict[str, Any] | None = None) -> httpx.Response | dict[str, Any]:
    """GET a Reddit feed, waiting at most once for the rate window. Returns the response or an error dict."""
    slept = False
    while True:
        with _rss_lock:
            wait = _rss_blocked_until - time.monotonic()
        if wait > 0:
            if slept or wait > _MAX_RSS_WAIT_S:
                return {"ok": False, "error_code": "rate_limited", "error": f"Reddit asked Forven to wait {int(wait) + 1}s"}
            time.sleep(wait)
            slept = True
        try:
            resp = client.get(url, headers={"User-Agent": _REDDIT_UA, "Accept": "application/atom+xml, application/xml;q=0.9"}, params=params)
        except RateLimitExceeded as exc:
            return {"ok": False, "error_code": "rate_limited", "error": str(exc)}
        _note_rss_budget(resp)
        if resp.status_code != 429:
            return resp


_BLOCK_TAGS = re.compile(r"<\s*(br|/p|/div|/li|/blockquote|/pre|/h\d)\b[^>]*>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_SUBMITTED_BY = re.compile(r"\s*submitted by\s+/u/\S+.*$", re.IGNORECASE | re.DOTALL)


def _html_to_text(raw: str) -> str:
    text = _BLOCK_TAGS.sub("\n", raw or "")
    text = html.unescape(_TAGS.sub("", text))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _entry_html(entry: Any) -> str:
    content = entry.get("content") or []
    if content and isinstance(content[0], dict):
        return str(content[0].get("value") or "")
    return str(entry.get("summary") or "")


def _entry_epoch(entry: Any) -> float | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return float(timegm(parsed)) if parsed else None


def _reddit_path(ref: str) -> str | None:
    """Path of a reddit permalink (/r/.../comments/...), or None for anything else."""
    ref = ref.strip()
    if ref.startswith("http://") or ref.startswith("https://"):
        parsed = urlparse(ref)
        host = (parsed.hostname or "").lower()
        if host != "reddit.com" and not host.endswith(".reddit.com"):
            return None
        ref = parsed.path
    if not ref.startswith("/"):
        ref = f"/{ref}"
    ref = ref.rstrip("/")
    if "/comments/" not in ref:
        return None
    return ref


def _search_rss(query: str, subs: list[str], limit: int, client: SourceHttpClient) -> dict[str, Any]:
    # One request for all subs (r/a+b+c): the anonymous limit allows few calls.
    url = f"{_BASE}/r/{'+'.join(quote(s) for s in subs)}/search.rss"
    got = _rss_get(client, url, params={"q": query, "restrict_sr": "on", "sort": "relevance", "t": "year", "limit": limit})
    if isinstance(got, dict):
        return got
    if got.status_code >= 400:
        return _http_error(got)
    feed = feedparser.parse(got.text)
    results: list[dict[str, Any]] = []
    for entry in feed.entries[:limit]:
        link = str(entry.get("link") or "")
        tags = entry.get("tags") or []
        results.append({
            "permalink": urlparse(link).path or link,
            "title": entry.get("title"),
            "subreddit": (tags[0].get("term") if tags else None),
            "score": None,
            "num_comments": None,
            "created_utc": _entry_epoch(entry),
        })
    return {"ok": True, "source": "reddit", "query": query, "count": len(results), "results": results}


def _inspect_rss(path: str, client: SourceHttpClient) -> dict[str, Any]:
    url = f"{_BASE}{path}.rss"
    got = _rss_get(client, url)
    if isinstance(got, dict):
        return got
    if got.status_code >= 400:
        return _http_error(got)
    feed = feedparser.parse(got.text)
    if not feed.entries:
        return {"ok": False, "error_code": "parse", "error": "empty reddit feed"}
    post, comments = feed.entries[0], feed.entries[1:]
    title = str(post.get("title") or feed.feed.get("title") or "")
    selftext = _SUBMITTED_BY.sub("", _html_to_text(_entry_html(post)))
    flat: list[str] = []
    for entry in comments[:_MAX_COMMENTS]:
        body = _html_to_text(_entry_html(entry))
        if body:
            flat.append(body)
    content = f"{title}\n\n{selftext}\n\n---\n" + "\n\n".join(flat)
    return {
        "ok": True,
        "source": "reddit",
        "title": title,
        "selftext": selftext,
        "top_comments": flat,
        "url": url,
        "content": content,
    }


# ── Search and thread reads ────────────────────────────────────────────────────


def search_reddit_posts(
    query: str,
    *,
    subs: list[str],
    limit: int = 10,
    client: SourceHttpClient | None = None,
    access_token: str | None = None,
) -> dict[str, Any]:
    if not query.strip():
        return {"ok": False, "error_code": "invalid_input", "error": "empty query"}
    if not subs:
        return {"ok": False, "error_code": "invalid_input", "error": "no subs configured"}
    client = client or _client()
    if not access_token:
        return _search_rss(query, list(subs), limit, client)
    url = f"{_OAUTH_BASE}/r/{'+'.join(quote(s) for s in subs)}/search"
    try:
        resp = client.get(
            url,
            headers={"User-Agent": _REDDIT_UA, "Authorization": f"Bearer {access_token}"},
            params={"q": query, "restrict_sr": 1, "sort": "relevance", "t": "year", "limit": limit, "raw_json": 1},
        )
    except RateLimitExceeded as exc:
        return {"ok": False, "error_code": "rate_limited", "error": str(exc)}
    if resp.status_code >= 400:
        return _http_error(resp)
    try:
        payload = resp.json()
    except ValueError:
        return {"ok": False, "error_code": "parse", "error": "invalid json"}
    results: list[dict[str, Any]] = []
    for child in ((payload.get("data") or {}).get("children") or []):
        d = child.get("data") or {}
        results.append({
            "permalink": d.get("permalink"),
            "title": d.get("title"),
            "subreddit": d.get("subreddit"),
            "score": d.get("score"),
            "num_comments": d.get("num_comments"),
            "created_utc": d.get("created_utc"),
        })
        if len(results) >= limit:
            break
    return {
        "ok": True,
        "source": "reddit",
        "query": query,
        "count": len(results),
        "results": results[:limit],
    }


def _walk_comments(node: dict[str, Any], flat: list[str], depth: int) -> None:
    if len(flat) >= _MAX_COMMENTS or depth > _MAX_DEPTH:
        return
    data = node.get("data") or {}
    body = data.get("body")
    score = data.get("score", 0)
    if body:
        flat.append(f"[{score}] {body}")
    replies = data.get("replies")
    if isinstance(replies, dict):
        for child in ((replies.get("data") or {}).get("children") or []):
            if len(flat) >= _MAX_COMMENTS:
                break
            _walk_comments(child, flat, depth + 1)


def inspect_reddit_thread(permalink: str, *, client: SourceHttpClient | None = None) -> dict[str, Any]:
    return inspect_reddit_thread_with_auth(permalink, client=client)


def inspect_reddit_thread_with_auth(
    permalink: str,
    *,
    client: SourceHttpClient | None = None,
    access_token: str | None = None,
) -> dict[str, Any]:
    if not permalink.strip():
        return {"ok": False, "error_code": "invalid_input", "error": "empty permalink"}
    path = _reddit_path(permalink)
    if path is None:
        return {"ok": False, "error_code": "invalid_input", "error": "not a reddit thread link (needs /comments/)"}
    client = client or _client()
    if not access_token:
        return _inspect_rss(path, client)

    url = f"{_OAUTH_BASE}{path}.json"
    try:
        resp = client.get(url, headers={"User-Agent": _REDDIT_UA, "Authorization": f"Bearer {access_token}"}, params={"raw_json": 1})
    except RateLimitExceeded as exc:
        return {"ok": False, "error_code": "rate_limited", "error": str(exc)}
    if resp.status_code >= 400:
        return _http_error(resp)
    try:
        payload = resp.json()
    except ValueError:
        return {"ok": False, "error_code": "parse", "error": "invalid json"}
    if not isinstance(payload, list) or len(payload) < 2:
        return {"ok": False, "error_code": "parse", "error": "unexpected reddit response shape"}

    submission_listing = payload[0]
    comments_listing = payload[1]
    sub_children = ((submission_listing.get("data") or {}).get("children") or [])
    submission = (sub_children[0].get("data") if sub_children else {}) or {}

    title = submission.get("title", "") or ""
    selftext = submission.get("selftext", "") or ""

    flat: list[str] = []
    for child in ((comments_listing.get("data") or {}).get("children") or []):
        if len(flat) >= _MAX_COMMENTS:
            break
        _walk_comments(child, flat, depth=0)

    content = f"{title}\n\n{selftext}\n\n---\n" + "\n\n".join(flat)
    return {
        "ok": True,
        "source": "reddit",
        "title": title,
        "selftext": selftext,
        "top_comments": flat,
        "url": url,
        "content": content,
    }
