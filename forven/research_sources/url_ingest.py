"""Operator-initiated URL ingest for submitted ideas.

When an operator pastes a URL, we:
  1. Detect which source type it belongs to (youtube/reddit/github/blog/forum)
  2. Call the corresponding inspect_* helper (bypassing the agent-tool gating, which
     is designed for research-contract-driven calls — operator paste is out-of-band)
  3. Return extracted title + content, for the submit dialog's preview or for the
     strategy-creation task the submitted idea queues.

All source paths surface an explicit ``content_empty`` failure when the extractor
returns no readable text. A hollow source gives the agent nothing to build from —
the operator is told to paste a different source or describe the idea instead.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

from forven.research_contract import get_research_sources_block
from forven.research_sources import blog, forum, github, podcast, reddit
from forven.research_sources.forum import ADAPTERS as FORUM_ADAPTERS

try:
    from forven.research_sources.youtube import inspect_youtube_video
except ImportError:  # pragma: no cover
    inspect_youtube_video = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


def _build_preview(source_type: str, url: str, title: str, content: str) -> dict[str, Any]:
    """Build a preview envelope, surfacing ``content_empty`` when nothing extractable came back."""
    content = content or ""
    if not content.strip():
        return {
            "ok": False,
            "error_code": "content_empty",
            "error": (
                f"No readable content could be extracted from this {source_type}. "
                "Paste a different source or describe the idea instead."
            ),
            "source_type": source_type,
            "url": url,
            "title": title or "",
        }
    return {
        "ok": True,
        "source_type": source_type,
        "url": url,
        "title": title or "",
        "content": content,
        "content_bytes": len(content.encode("utf-8")),
    }


_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"}
_REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com"}
_GITHUB_HOSTS = {"github.com", "www.github.com"}
# Common podcast hosts (RSS feeds + episode pages). RSS on arbitrary domains is
# indistinguishable from a blog by host alone, so the blog fallback still applies
# to those; a "/podcast" path also routes here.
_PODCAST_HOST_SUFFIXES = (
    "podcasts.apple.com", "anchor.fm", "simplecast.com", "libsyn.com",
    "podbean.com", "buzzsprout.com", "megaphone.fm", "transistor.fm",
    "fireside.fm", "captivate.fm", "pca.st", "overcast.fm",
)


def detect_source_type(url: str) -> str | None:
    """Return 'youtube' | 'reddit' | 'github' | 'podcast' | 'forum' | 'blog' | None."""
    if not url or not isinstance(url, str):
        return None
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if not host or not parsed.scheme.startswith("http"):
        return None
    if host in _YOUTUBE_HOSTS:
        return "youtube"
    if host in _REDDIT_HOSTS:
        return "reddit"
    if host in _GITHUB_HOSTS:
        # Need a repo path — accept org/repo at minimum
        parts = [p for p in (parsed.path or "").split("/") if p]
        return "github" if len(parts) >= 2 else None
    host_no_www = host.removeprefix("www.")
    # Known podcast hosts / a "/podcast" path → podcast connector
    if any(host_no_www == h or host_no_www.endswith("." + h) for h in _PODCAST_HOST_SUFFIXES):
        return "podcast"
    if "/podcast" in (parsed.path or "").lower():
        return "podcast"
    # Known forums — match by suffix
    for site in FORUM_ADAPTERS:
        if site in host_no_www:
            return "forum"
    # Anything else → best-effort blog/article extraction
    return "blog"


def _github_full_name(url: str) -> str | None:
    parts = [p for p in (urlparse(url).path or "").split("/") if p]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def _reddit_permalink(url: str) -> str:
    path = urlparse(url).path or ""
    # Accept either the canonical permalink (which ends with a slug+slash) or a shorter form
    return path if path.startswith("/") else f"/{path}"


def _github_client_with_optional_pat():
    """Build a github client, auto-injecting PAT from settings if present (rate-limit boost)."""
    cfg = (get_research_sources_block() or {}).get("github") or {}
    pat = cfg.get("personal_access_token")
    pat_val = pat if isinstance(pat, str) and pat else None
    return github._client(pat=pat_val)


def _reddit_oauth_credentials() -> tuple[str | None, str | None]:
    cfg = (get_research_sources_block() or {}).get("reddit") or {}
    client_id = cfg.get("client_id") or os.getenv("REDDIT_CLIENT_ID")
    client_secret = cfg.get("client_secret") or os.getenv("REDDIT_CLIENT_SECRET")
    cid = client_id if isinstance(client_id, str) and client_id.strip() else None
    secret = client_secret if isinstance(client_secret, str) and client_secret.strip() else None
    return cid, secret


def _reddit_auth_required_error() -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": "auth_required",
        "error": (
            "Reddit blocked anonymous access to this thread. Configure reddit "
            "client_id/client_secret in research source settings or set "
            "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET, then retry."
        ),
        "source_type": "reddit",
    }


def _inspect_reddit_with_optional_oauth(permalink: str) -> dict[str, Any]:
    result = reddit.inspect_reddit_thread(permalink)
    if result.get("ok") or result.get("error_code") != "http_4xx" or result.get("error") != "http 403":
        return result

    client_id, client_secret = _reddit_oauth_credentials()
    if not client_id or not client_secret:
        return _reddit_auth_required_error()

    token_result = reddit.fetch_oauth_token(client_id, client_secret)
    if not token_result.get("ok"):
        return {
            **token_result,
            "source_type": "reddit",
            "error": f"Reddit OAuth failed: {token_result.get('error') or 'auth failed'}",
        }
    return reddit.inspect_reddit_thread_with_auth(
        permalink,
        access_token=str(token_result.get("access_token") or ""),
    )


def fetch_preview(url: str) -> dict[str, Any]:
    """Fetch a URL using the detected source's inspect_* helper. Does NOT persist anything.

    Returns:
        {"ok": True, "source_type", "url", "title", "content", "content_bytes"}
        or {"ok": False, "error", "error_code", "source_type"?}
    """
    source_type = detect_source_type(url)
    if source_type is None:
        return {"ok": False, "error_code": "invalid_input", "error": "unsupported or malformed URL"}

    try:
        if source_type == "youtube":
            if not callable(inspect_youtube_video):
                return {"ok": False, "error_code": "unavailable", "error": "youtube helper unavailable", "source_type": source_type}
            raw = inspect_youtube_video(url)
            return _normalize_youtube(raw, url)

        if source_type == "reddit":
            result = _inspect_reddit_with_optional_oauth(_reddit_permalink(url))
            if not result.get("ok"):
                log.warning("url_ingest reddit fetch failed url=%s code=%s err=%s", url, result.get("error_code"), result.get("error"))
                return {**result, "source_type": source_type}
            return _build_preview("reddit", url, result.get("title", ""), result.get("content", ""))

        if source_type == "github":
            full_name = _github_full_name(url)
            if not full_name:
                return {"ok": False, "error_code": "invalid_input", "error": "github URL must include org/repo", "source_type": source_type}
            result = github.inspect_github_repo(full_name, client=_github_client_with_optional_pat())
            if not result.get("ok"):
                log.warning("url_ingest github fetch failed url=%s code=%s err=%s", url, result.get("error_code"), result.get("error"))
                return {**result, "source_type": source_type}
            return _build_preview("github", url, full_name, result.get("content", ""))

        if source_type == "forum":
            result = forum.inspect_forum_thread(url)
            if not result.get("ok"):
                log.warning("url_ingest forum fetch failed url=%s code=%s err=%s", url, result.get("error_code"), result.get("error"))
                return {**result, "source_type": source_type}
            return _build_preview("forum", url, result.get("title", "") or "", result.get("content", ""))

        if source_type == "podcast":
            result = podcast.inspect_podcast_episode(url)
            if not result.get("ok"):
                log.warning("url_ingest podcast fetch failed url=%s code=%s err=%s", url, result.get("error_code"), result.get("error"))
                return {**result, "source_type": source_type}
            title = result.get("title", "") or result.get("show", "") or ""
            return _build_preview("podcast", url, title, result.get("content", "") or "")

        # blog fallback
        result = blog.inspect_blog_article(url)
        if not result.get("ok"):
            log.warning("url_ingest blog fetch failed url=%s code=%s err=%s", url, result.get("error_code"), result.get("error"))
            return {**result, "source_type": source_type}
        return _build_preview("blog", url, result.get("title", "") or "", result.get("content", "") or "")
    except Exception as exc:  # pragma: no cover — defence in depth
        log.exception("url_ingest fetch_preview unexpected failure url=%s source_type=%s", url, source_type)
        return {"ok": False, "error_code": "fetch_failed", "error": str(exc) or "fetch failed", "source_type": source_type}


_YT_STATUS_MESSAGES = {
    "captions_unavailable": "captions unavailable",
    "caption_track_missing_base_url": "caption track unavailable",
    "transcript_url_not_allowed": "transcript URL blocked",
    "transcript_fetch_blocked": "transcript fetch blocked",
    "transcript_fetch_error": "transcript fetch failed",
    "transcript_empty": "transcript was empty",
    "invalid_youtube_url": "not a valid YouTube URL",
}


def _normalize_youtube(raw: Any, url: str) -> dict[str, Any]:
    """Adapt the existing inspect_youtube_video output to the preview envelope.

    inspect_youtube_video returns {"status": "ok" | "unavailable" | "blocked" | "error",
    "title", "transcript": [...], ...}. Anything other than status=="ok" with a
    non-empty transcript must surface as an extraction failure so the UI can tell the
    operator to paste a different source — agents should not fabricate a strategy from
    a bare title.
    """
    if not isinstance(raw, dict):
        return {"ok": False, "error_code": "parse", "error": "invalid youtube response", "source_type": "youtube"}
    # Legacy boolean-ok shape (kept for defensive compatibility).
    if raw.get("ok") is False:
        return {"ok": False, "error_code": "fetch_failed", "error": str(raw.get("error") or "youtube fetch failed"), "source_type": "youtube"}
    status = raw.get("status")
    title = raw.get("title") or (raw.get("video") or {}).get("title") or ""
    transcript = raw.get("transcript")
    if status is not None and status != "ok":
        reason = str(raw.get("reason") or "")
        human = _YT_STATUS_MESSAGES.get(reason) or reason.replace("_", " ") or status
        return {
            "ok": False,
            "error_code": "transcript_unavailable",
            "error": f"YouTube transcript unavailable ({human}). Paste a different source or describe the idea instead.",
            "source_type": "youtube",
            "url": url,
            "title": title,
            "status": status,
            "reason": reason,
        }
    if isinstance(transcript, list):
        parts = []
        for item in transcript:
            if isinstance(item, dict):
                text = item.get("text") or ""
            else:
                text = str(item or "")
            if text.strip():
                parts.append(text.strip())
        content = "\n".join(parts)
    else:
        content = str(transcript or "")
    if not content.strip():
        return {
            "ok": False,
            "error_code": "transcript_unavailable",
            "error": "YouTube transcript was empty. Paste a different source or describe the idea instead.",
            "source_type": "youtube",
            "url": url,
            "title": title,
        }
    # Prepend the title so agents reading the content blob have context
    content_full = f"{title}\n\n{content}" if title and content else (title or content)
    return {
        "ok": True,
        "source_type": "youtube",
        "url": url,
        "title": title,
        "content": content_full,
        "content_bytes": len(content_full.encode("utf-8")),
    }
