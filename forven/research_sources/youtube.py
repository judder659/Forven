from __future__ import annotations

import html
import json
import xml.etree.ElementTree as ET
from typing import Any, Iterator
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

_BASE_URL = "https://www.youtube.com"
_DEFAULT_TIMEOUT = 15.0
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Forven/1.0; +https://github.com/openai)",
    "Accept-Language": "en-US,en;q=0.9",
}


def _is_allowed_youtube_request_host(host: str) -> bool:
    """Hosts YouTube legitimately serves / redirects to (consent, CDN)."""
    host = (host or "").strip().lower()
    suffixes = ("youtube.com", "youtu.be", "google.com", "googlevideo.com", "ytimg.com", "gstatic.com")
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _guard_request_host(request: "httpx.Request") -> None:
    """SECURITY (audit 2026-06-22, L5): with follow_redirects=True a YouTube
    response could (in theory) redirect the client at an arbitrary/internal host.
    This httpx request event-hook fires before EVERY request — including every
    redirect hop — and refuses any host outside the YouTube/Google allowlist, so
    a redirect can never steer the fetch at an RFC1918/loopback/metadata address.
    """
    from forven.security.url_safety import UnsafeUrlError

    host = (request.url.host or "").lower()
    if not _is_allowed_youtube_request_host(host):
        raise UnsafeUrlError(f"YouTube fetch redirected to a non-allowed host: {host!r}")


_REQUEST_GUARD_HOOKS = {"request": [_guard_request_host]}


def search_youtube_videos(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search YouTube and return normalized candidate video metadata."""
    search_url = f"{_BASE_URL}/results?search_query={quote_plus(query)}"
    with httpx.Client(timeout=_DEFAULT_TIMEOUT, headers=_DEFAULT_HEADERS, follow_redirects=True, event_hooks=_REQUEST_GUARD_HOOKS) as client:
        response = client.request("GET", search_url)
        response.raise_for_status()
        initial_data = _extract_yt_initial_data(response.text)
        if not initial_data:
            return {"query": query, "results": []}
        candidates = _extract_search_candidates(initial_data, max_results=max_results)
        return {"query": query, "results": candidates}


def inspect_youtube_video(url: str) -> dict[str, Any]:
    """Fetch a YouTube watch page and return transcript details when available.

    Two-layer strategy:
      1. Scrape the watch page's embedded caption track (fast; works for most videos).
      2. On any non-ok status, fall back to yt-dlp's subtitle extraction which handles
         auto-generated captions and endpoint changes our scraper misses.

    On success returns {"status": "ok", "transcript": [...], "title", ...}; the
    transcript_source field indicates which layer produced the transcript.
    """
    primary = _inspect_via_scraper(url)
    if primary.get("status") == "ok" and primary.get("transcript"):
        primary.setdefault("transcript_source", "scraper")
        return primary

    # Fallback: yt-dlp subtitle extraction
    video_id = _safe_video_id(url)
    if not video_id:
        return primary

    fallback_transcript = _fetch_transcript_via_ytdlp(video_id)
    if not fallback_transcript:
        return primary

    # Merge scraper-derived metadata if we have it; otherwise minimal.
    title = primary.get("title") or ""
    channel = primary.get("channel_name") or ""
    description = primary.get("description_excerpt") or ""
    if not title:
        meta = _fetch_ytdlp_metadata(video_id)
        title = meta.get("title") or title
        channel = meta.get("channel_name") or channel
        description = meta.get("description_excerpt") or description

    return {
        "status": "ok",
        "url": primary.get("url") or f"{_BASE_URL}/watch?v={video_id}",
        "video_id": video_id,
        "title": title,
        "channel_name": channel,
        "description_excerpt": description,
        "transcript": fallback_transcript,
        "transcript_source": "yt_dlp",
    }


def _inspect_via_scraper(url: str) -> dict[str, Any]:
    """Original scraper-only path. Kept standalone so it can be tested in isolation."""
    normalized_url = _normalize_watch_url(url)
    if normalized_url is None:
        return {
            "status": "error",
            "reason": "invalid_youtube_url",
            "url": url,
        }
    video_id = _extract_video_id(normalized_url)
    with httpx.Client(timeout=_DEFAULT_TIMEOUT, headers=_DEFAULT_HEADERS, follow_redirects=True, event_hooks=_REQUEST_GUARD_HOOKS) as client:
        html_text = _fetch_watch_page(client, normalized_url)
        player_response = _extract_yt_initial_player_response(html_text)
        video_details = player_response.get("videoDetails") or {}
        video_metadata = {
            "url": normalized_url,
            "video_id": video_id,
            "title": video_details.get("title") or "",
            "channel_name": video_details.get("author") or "",
            "description_excerpt": video_details.get("shortDescription") or "",
        }
        caption_track = _select_preferred_caption_track(_extract_caption_tracks(player_response))
        if caption_track is None:
            return {
                "status": "unavailable",
                "reason": "captions_unavailable",
                **video_metadata,
            }

        transcript_url = caption_track.get("baseUrl") or ""
        if not transcript_url:
            return {
                "status": "unavailable",
                "reason": "caption_track_missing_base_url",
                **video_metadata,
            }

        normalized_transcript_url = _normalize_transcript_url(transcript_url)
        if normalized_transcript_url is None:
            return {
                "status": "blocked",
                "reason": "transcript_url_not_allowed",
                **video_metadata,
                "transcript_url": transcript_url,
            }

        try:
            transcript_xml = _fetch_transcript_xml(client, normalized_transcript_url)
        except httpx.HTTPStatusError as exc:
            return {
                "status": "blocked",
                "reason": "transcript_fetch_blocked",
                "http_status": exc.response.status_code,
                **video_metadata,
            }
        except httpx.HTTPError as exc:
            return {
                "status": "error",
                "reason": "transcript_fetch_error",
                "error": str(exc),
                **video_metadata,
            }

        transcript = _parse_transcript_xml(transcript_xml)
        if not transcript:
            return {
                "status": "unavailable",
                "reason": "transcript_empty",
                **video_metadata,
            }
        return {
            "status": "ok",
            **video_metadata,
            "transcript": transcript,
            "transcript_url": normalized_transcript_url,
        }


def _fetch_watch_page(client: httpx.Client, url: str) -> str:
    response = client.request("GET", url)
    response.raise_for_status()
    return response.text


def _fetch_transcript_xml(client: httpx.Client, url: str) -> str:
    response = client.request("GET", url)
    response.raise_for_status()
    return response.text


def _extract_yt_initial_data(html_text: str) -> dict[str, Any] | None:
    payload = _extract_json_blob(html_text, "ytInitialData")
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def _extract_yt_initial_player_response(html_text: str) -> dict[str, Any]:
    payload = _extract_json_blob(html_text, "ytInitialPlayerResponse")
    if not payload:
        return {}
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_json_blob(text: str, marker: str) -> str | None:
    marker_index = text.find(marker)
    if marker_index == -1:
        return None

    start_index = None
    for idx in range(marker_index, len(text)):
        if text[idx] in "{[":
            start_index = idx
            break
    if start_index is None:
        return None

    opening = text[start_index]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escape = False

    for idx in range(start_index, len(text)):
        char = text[idx]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start_index : idx + 1]

    return None


def _extract_search_candidates(initial_data: dict[str, Any], max_results: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for renderer in _iter_video_renderers(initial_data):
        video_id = str(renderer.get("videoId") or "").strip()
        title = _extract_runs_text(renderer.get("title"))
        if not video_id or not title:
            continue

        channel_name = _extract_runs_text(renderer.get("ownerText"))
        channel_url = _normalize_channel_url(renderer)
        thumb_url = _first_thumbnail_url(renderer.get("thumbnail"))
        candidates.append(
            {
                "video_id": video_id,
                "title": title,
                "url": f"{_BASE_URL}/watch?v={video_id}",
                "channel_name": channel_name,
                "channel_url": channel_url,
                "description": _extract_runs_text(renderer.get("descriptionSnippet")),
                "duration": _text_from_node(renderer.get("lengthText")),
                "views": _text_from_node(renderer.get("viewCountText")),
                "published": _text_from_node(renderer.get("publishedTimeText")),
                "thumbnail_url": thumb_url,
            }
        )
        if len(candidates) >= max_results:
            break
    return candidates


def _iter_video_renderers(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        renderer = node.get("videoRenderer")
        if isinstance(renderer, dict):
            yield renderer
        for value in node.values():
            yield from _iter_video_renderers(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_video_renderers(item)


def _extract_runs_text(node: Any) -> str:
    if isinstance(node, dict):
        runs = node.get("runs")
        if isinstance(runs, list):
            parts = [str(run.get("text", "")) for run in runs if isinstance(run, dict)]
            return "".join(parts).strip()
        if "simpleText" in node:
            return str(node.get("simpleText") or "").strip()
    return ""


def _text_from_node(node: Any) -> str:
    if isinstance(node, dict):
        simple = node.get("simpleText")
        if simple:
            return str(simple).strip()
        return _extract_runs_text(node)
    if node is None:
        return ""
    return str(node).strip()


def _normalize_channel_url(renderer: dict[str, Any]) -> str:
    owner_text = renderer.get("ownerText") or {}
    runs = owner_text.get("runs") if isinstance(owner_text, dict) else None
    if isinstance(runs, list):
        for run in runs:
            if not isinstance(run, dict):
                continue
            endpoint = run.get("navigationEndpoint") or {}
            browse_endpoint = endpoint.get("browseEndpoint") if isinstance(endpoint, dict) else None
            if isinstance(browse_endpoint, dict):
                base_url = str(browse_endpoint.get("canonicalBaseUrl") or browse_endpoint.get("url") or "").strip()
                if base_url:
                    if base_url.startswith("http"):
                        return base_url
                    return f"{_BASE_URL}{base_url if base_url.startswith('/') else '/' + base_url}"
    return ""


def _first_thumbnail_url(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    thumbnails = node.get("thumbnails")
    if isinstance(thumbnails, list):
        for thumb in thumbnails:
            if isinstance(thumb, dict) and thumb.get("url"):
                return str(thumb["url"])
    return ""


def _extract_caption_tracks(player_response: dict[str, Any]) -> list[dict[str, Any]]:
    captions = player_response.get("captions") or {}
    tracklist = captions.get("playerCaptionsTracklistRenderer") if isinstance(captions, dict) else None
    caption_tracks = tracklist.get("captionTracks") if isinstance(tracklist, dict) else None
    return [track for track in caption_tracks if isinstance(track, dict)] if isinstance(caption_tracks, list) else []


def _select_preferred_caption_track(caption_tracks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not caption_tracks:
        return None

    def _score(track: dict[str, Any]) -> tuple[int, int, int]:
        language = str(track.get("languageCode") or "").strip().lower()
        vss_id = str(track.get("vssId") or "").strip().lower()
        is_auto_generated = str(track.get("kind") or "").strip().lower() == "asr"
        language_is_english = language.startswith("en") or vss_id.endswith(".en") or vss_id.endswith("a.en")
        has_base_url = bool(str(track.get("baseUrl") or "").strip())
        return (
            0 if language_is_english else 1,
            0 if not is_auto_generated else 1,
            0 if has_base_url else 1,
        )

    return min(caption_tracks, key=_score)


def _parse_transcript_xml(transcript_xml: str) -> list[dict[str, Any]]:
    if not transcript_xml.strip():
        return []
    try:
        root = ET.fromstring(transcript_xml)
    except ET.ParseError:
        return []

    transcript: list[dict[str, Any]] = []
    for node in root.findall(".//text"):
        start = _coerce_float(node.attrib.get("start"))
        dur = _coerce_float(node.attrib.get("dur"))
        text = html.unescape("".join(node.itertext()).strip())
        if not text:
            continue
        transcript.append({"start": start, "dur": dur, "text": text})
    return transcript


def _coerce_float(value: str | None) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _normalize_watch_url(url: str) -> str | None:
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()

    if _is_valid_youtube_watch_host(netloc):
        if netloc == "youtu.be" or netloc.endswith(".youtu.be"):
            video_id = parsed.path.lstrip("/").split("/")[0]
            return f"{_BASE_URL}/watch?v={video_id}"

        params = parse_qs(parsed.query)
        video_id = (params.get("v") or [""])[0]
        if video_id:
            return f"{_BASE_URL}/watch?v={video_id}"

    return None


def _normalize_transcript_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or ""

    if not _is_valid_youtube_host(host):
        return None
    if not path.startswith("/api/timedtext"):
        return None

    return url


def _is_valid_youtube_watch_host(host: str) -> bool:
    return host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be" or host.endswith(".youtu.be")


def _is_valid_youtube_host(host: str) -> bool:
    return host == "youtube.com" or host.endswith(".youtube.com")


def _extract_video_id(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    video_id = (params.get("v") or [""])[0]
    if video_id:
        return video_id
    if parsed.netloc.lower() == "youtu.be" or parsed.netloc.lower().endswith(".youtu.be"):
        video_id = parsed.path.lstrip("/").split("/")[0]
        return video_id
    return ""


def _safe_video_id(url: str) -> str:
    """Extract a video id from any YouTube URL shape, without raising."""
    try:
        normalized = _normalize_watch_url(url) or url
        return _extract_video_id(normalized) or ""
    except Exception:
        return ""


_SUBTITLE_LANG_PREFERENCE = ("en", "en-US", "en-GB", "en-CA", "en-AU")


def _fetch_transcript_via_ytdlp(video_id: str) -> list[dict[str, Any]] | None:
    """Download YouTube subtitles via yt-dlp and parse into transcript list.

    Returns list-of-dicts with {"start", "dur", "text"} matching the scraper's shape,
    or None if yt-dlp is unavailable / subtitle extraction fails entirely.

    Uses a temp directory so we never leak files. Prefers manual subs over auto-captions;
    prefers json3 format because it's trivially parseable.
    """
    try:
        import tempfile
        from pathlib import Path

        from yt_dlp import YoutubeDL
    except ImportError:  # pragma: no cover - optional dependency
        return None

    watch_url = f"{_BASE_URL}/watch?v={video_id}"

    with tempfile.TemporaryDirectory(prefix="forven-ytsub-") as tmpdir:
        tmp_path = Path(tmpdir)
        opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": list(_SUBTITLE_LANG_PREFERENCE),
            "subtitlesformat": "json3",
            "outtmpl": str(tmp_path / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "noprogress": True,
        }
        try:
            with YoutubeDL(opts) as ydl:
                ydl.download([watch_url])
        except Exception:
            return None

        for sub_file in sorted(tmp_path.glob("*.json3")):
            try:
                payload = json.loads(sub_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            transcript = _parse_json3_transcript(payload)
            if transcript:
                return transcript
    return None


def _parse_json3_transcript(payload: Any) -> list[dict[str, Any]]:
    """Parse YouTube's json3 subtitle format into our transcript shape."""
    if not isinstance(payload, dict):
        return []
    events = payload.get("events")
    if not isinstance(events, list):
        return []
    transcript: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        segs = event.get("segs")
        if not isinstance(segs, list):
            continue
        text = "".join(str(seg.get("utf8") or "") for seg in segs if isinstance(seg, dict)).strip()
        if not text:
            continue
        start_ms = event.get("tStartMs") or 0
        dur_ms = event.get("dDurationMs") or 0
        try:
            start = float(start_ms) / 1000.0
        except (TypeError, ValueError):
            start = 0.0
        try:
            dur = float(dur_ms) / 1000.0
        except (TypeError, ValueError):
            dur = 0.0
        transcript.append({"start": start, "dur": dur, "text": text})
    return transcript


def _fetch_ytdlp_metadata(video_id: str) -> dict[str, str]:
    """Pull basic video metadata (title, channel, description) via yt-dlp.

    Only called when the scraper failed before it could extract metadata; otherwise
    we reuse the scraper-derived fields.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError:  # pragma: no cover
        return {}

    opts = {"skip_download": True, "quiet": True, "no_warnings": True, "noprogress": True}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{_BASE_URL}/watch?v={video_id}", download=False)
    except Exception:
        return {}
    if not isinstance(info, dict):
        return {}
    description = str(info.get("description") or "")
    return {
        "title": str(info.get("title") or ""),
        "channel_name": str(info.get("channel") or info.get("uploader") or ""),
        "description_excerpt": description[:500],
    }
