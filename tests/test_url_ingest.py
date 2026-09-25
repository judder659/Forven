"""URL ingest for operator-submitted ideas."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from forven.api import app
from forven.research_sources.url_ingest import detect_source_type, fetch_preview


# ---- detection ----


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=abc", "youtube"),
        ("https://youtu.be/abc", "youtube"),
        ("https://m.youtube.com/watch?v=abc", "youtube"),
        ("https://www.reddit.com/r/algotrading/comments/x/y/", "reddit"),
        ("https://old.reddit.com/r/quant/comments/x/", "reddit"),
        ("https://github.com/quantopian/zipline", "github"),
        ("https://github.com/single-segment-only", None),  # no repo → not a detectable source
        ("https://www.elitetrader.com/et/threads/mean-reversion.1/", "forum"),
        ("https://www.quantconnect.com/forum/discussion/1/x", "forum"),
        ("https://quantnet.com/threads/x.1/", "forum"),
        ("https://www.quantstart.com/articles/some-post/", "blog"),
        ("https://random-blog.example.com/post", "blog"),
        ("not a url", None),
        ("", None),
        ("ftp://example.com/x", None),
    ],
)
def test_detect_source_type(url, expected):
    assert detect_source_type(url) == expected


# ---- preview (mocked inspect helpers) ----


def test_fetch_preview_reddit():
    fake = {"ok": True, "title": "T", "content": "hello world", "selftext": "", "top_comments": [], "url": "u", "source": "reddit"}
    with patch("forven.research_sources.url_ingest.reddit.inspect_reddit_thread", return_value=fake):
        res = fetch_preview("https://www.reddit.com/r/algotrading/comments/abc/title/")
    assert res["ok"] is True
    assert res["source_type"] == "reddit"
    assert res["title"] == "T"
    assert res["content"] == "hello world"
    assert res["content_bytes"] == len(b"hello world")


def test_fetch_preview_github_uses_full_name_from_path():
    fake = {"ok": True, "full_name": "quantopian/zipline", "content": "body", "readme": "x", "recent_issues": [], "metadata": {}, "source": "github"}
    with patch("forven.research_sources.url_ingest.github.inspect_github_repo", return_value=fake) as m:
        res = fetch_preview("https://github.com/quantopian/zipline/tree/main")
    assert res["ok"] is True
    assert res["source_type"] == "github"
    assert res["title"] == "quantopian/zipline"
    assert m.call_args.args[0] == "quantopian/zipline"


def test_fetch_preview_blog_fallback():
    fake = {"ok": True, "content": "article text", "title": "Post Title", "url": "u", "source": "blog"}
    with patch("forven.research_sources.url_ingest.blog.inspect_blog_article", return_value=fake):
        res = fetch_preview("https://random.example.com/post")
    assert res["ok"] is True
    assert res["source_type"] == "blog"
    assert res["title"] == "Post Title"
    assert res["content"] == "article text"


def test_fetch_preview_forum_known_site():
    fake = {"ok": True, "title": "Thread", "content": "posts", "site": "elitetrader.com", "posts": [], "url": "u", "source": "forum"}
    with patch("forven.research_sources.url_ingest.forum.inspect_forum_thread", return_value=fake):
        res = fetch_preview("https://www.elitetrader.com/et/threads/x.1/")
    assert res["ok"] is True
    assert res["source_type"] == "forum"
    assert res["title"] == "Thread"


def test_fetch_preview_invalid_url():
    res = fetch_preview("not-a-url")
    assert res["ok"] is False
    assert res["error_code"] == "invalid_input"


def test_fetch_preview_github_without_repo_path():
    res = fetch_preview("https://github.com/")
    assert res["ok"] is False


def test_fetch_preview_propagates_underlying_error():
    fail = {"ok": False, "error_code": "http_4xx", "error": "http 404"}
    with patch("forven.research_sources.url_ingest.reddit.inspect_reddit_thread", return_value=fail):
        res = fetch_preview("https://www.reddit.com/r/x/comments/1/")
    assert res["ok"] is False
    assert res["error_code"] == "http_4xx"


def test_fetch_preview_reddit_403_reports_auth_required(monkeypatch):
    fail = {"ok": False, "error_code": "http_4xx", "error": "http 403"}
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(
        "forven.research_sources.url_ingest.get_research_sources_block",
        lambda: {"reddit": {"client_id": None, "client_secret": None}},
    )
    with patch("forven.research_sources.url_ingest.reddit.inspect_reddit_thread", return_value=fail):
        res = fetch_preview("https://www.reddit.com/r/x/comments/1/")
    assert res["ok"] is False
    assert res["error_code"] == "auth_required"
    assert "client_id/client_secret" in res["error"]


def test_fetch_preview_reddit_403_retries_with_oauth(monkeypatch):
    fail = {"ok": False, "error_code": "http_4xx", "error": "http 403"}
    success = {
        "ok": True,
        "title": "OAuth thread",
        "content": "oauth content",
        "selftext": "",
        "top_comments": [],
        "url": "u",
        "source": "reddit",
    }
    monkeypatch.setattr(
        "forven.research_sources.url_ingest.get_research_sources_block",
        lambda: {"reddit": {"client_id": "cid", "client_secret": "secret"}},
    )
    with (
        patch("forven.research_sources.url_ingest.reddit.inspect_reddit_thread", return_value=fail),
        patch("forven.research_sources.url_ingest.reddit.fetch_oauth_token", return_value={"ok": True, "access_token": "tok"}),
        patch("forven.research_sources.url_ingest.reddit.inspect_reddit_thread_with_auth", return_value=success) as authed,
    ):
        res = fetch_preview("https://www.reddit.com/r/x/comments/1/")
    assert res["ok"] is True
    assert res["title"] == "OAuth thread"
    assert authed.call_args.kwargs["access_token"] == "tok"


def test_fetch_preview_youtube_with_transcript():
    fake = {
        "status": "ok",
        "url": "https://youtube.com/watch?v=abc",
        "video_id": "abc",
        "title": "ICT Smart Money Concepts",
        "channel_name": "Example",
        "description_excerpt": "",
        "transcript": [{"text": "order blocks and liquidity runs"}],
    }
    with patch("forven.research_sources.url_ingest.inspect_youtube_video", return_value=fake):
        res = fetch_preview("https://youtube.com/watch?v=abc")
    assert res["ok"] is True
    assert res["source_type"] == "youtube"
    assert res["title"] == "ICT Smart Money Concepts"
    assert "order blocks" in res["content"]


def test_fetch_preview_youtube_unavailable_captions_surfaces_failure():
    """Videos without captions must not silently succeed — agents would fabricate
    a strategy from the bare title."""
    fake = {
        "status": "unavailable",
        "reason": "captions_unavailable",
        "url": "https://youtube.com/watch?v=abc",
        "video_id": "abc",
        "title": "ICT Smart Money Concepts",
        "channel_name": "Example",
        "description_excerpt": "",
    }
    with patch("forven.research_sources.url_ingest.inspect_youtube_video", return_value=fake):
        res = fetch_preview("https://youtube.com/watch?v=abc")
    assert res["ok"] is False
    assert res["error_code"] == "transcript_unavailable"
    assert res["source_type"] == "youtube"


def test_fetch_preview_youtube_blocked_transcript_surfaces_failure():
    fake = {
        "status": "blocked",
        "reason": "transcript_fetch_blocked",
        "url": "https://youtube.com/watch?v=abc",
        "video_id": "abc",
        "title": "Some Video",
        "channel_name": "",
        "description_excerpt": "",
    }
    with patch("forven.research_sources.url_ingest.inspect_youtube_video", return_value=fake):
        res = fetch_preview("https://youtube.com/watch?v=abc")
    assert res["ok"] is False
    assert res["error_code"] == "transcript_unavailable"


def test_fetch_preview_youtube_empty_transcript_surfaces_failure():
    """Status 'ok' but empty transcript list must also fail — prevents fabricating
    a strategy when only the title made it through."""
    fake = {
        "status": "ok",
        "url": "https://youtube.com/watch?v=abc",
        "video_id": "abc",
        "title": "Title Only",
        "channel_name": "",
        "description_excerpt": "",
        "transcript": [],
    }
    with patch("forven.research_sources.url_ingest.inspect_youtube_video", return_value=fake):
        res = fetch_preview("https://youtube.com/watch?v=abc")
    assert res["ok"] is False
    assert res["error_code"] == "transcript_unavailable"


# ---- API endpoints ----


def _queued_tasks() -> list[dict]:
    from forven.db import get_db

    with get_db() as conn:
        return [dict(row) for row in conn.execute("SELECT type, input_data FROM agent_tasks")]


def test_preview_url_endpoint(forven_db):
    fake = {"ok": True, "title": "Post", "content": "a" * 5000, "selftext": "", "top_comments": [], "url": "u", "source": "reddit"}
    with patch("forven.research_sources.url_ingest.reddit.inspect_reddit_thread", return_value=fake):
        client = TestClient(app)
        r = client.post("/api/ideas/preview_url", json={"url": "https://www.reddit.com/r/x/comments/1/"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["source_type"] == "reddit"
    # Content is truncated to ~4000 chars for preview
    assert len(body["content_preview"]) <= 4000
    assert body["preview_truncated"] is True
    assert body["content_bytes"] == 5000
    assert _queued_tasks() == []


def test_preview_url_endpoint_surfaces_fetch_error(forven_db):
    fake = {"ok": False, "error_code": "http_4xx", "error": "http 404"}
    with patch("forven.research_sources.url_ingest.blog.inspect_blog_article", return_value=fake):
        client = TestClient(app)
        r = client.post("/api/ideas/preview_url", json={"url": "https://random.example.com/post"})
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is False
    assert body["error_code"] == "http_4xx"


def test_submit_idea_endpoint_queues_the_url_source(forven_db):
    fake = {"ok": True, "title": "Strategy from blog", "content": "full body text", "url": "u", "source": "blog"}
    with patch("forven.research_sources.url_ingest.blog.inspect_blog_article", return_value=fake):
        client = TestClient(app)
        r = client.post("/api/ideas", json={"url": "https://random.example.com/post"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    tasks = _queued_tasks()
    assert [task["type"] for task in tasks] == ["generate_strategies"]
    payload = json.loads(tasks[0]["input_data"])
    assert payload["source_type"] == "blog"
    assert payload["source_title"] == "Strategy from blog"
    assert payload["operator_idea"]["title"] == "Strategy from blog"


def test_submit_idea_endpoint_keeps_the_operators_fields(forven_db):
    fake = {"ok": True, "title": "Extracted Title", "content": "body", "url": "u", "source": "blog"}
    with patch("forven.research_sources.url_ingest.blog.inspect_blog_article", return_value=fake):
        client = TestClient(app)
        r = client.post(
            "/api/ideas",
            json={
                "url": "https://random.example.com/post",
                "title": "Operator Chose This Title",
                "market_thesis": "Specific thesis.",
                "mechanism": "Specific mechanism.",
            },
        )
    assert r.json()["ok"] is True
    idea = json.loads(_queued_tasks()[0]["input_data"])["operator_idea"]
    assert idea["title"] == "Operator Chose This Title"
    assert idea["market_thesis"] == "Specific thesis."
    assert idea["mechanism"] == "Specific mechanism."


def test_submit_idea_endpoint_fetch_failure_queues_nothing(forven_db):
    fake = {"ok": False, "error_code": "http_4xx", "error": "http 404"}
    with patch("forven.research_sources.url_ingest.blog.inspect_blog_article", return_value=fake):
        client = TestClient(app)
        r = client.post("/api/ideas", json={"url": "https://random.example.com/post"})
    body = r.json()
    assert body["ok"] is False
    assert body["error_code"] == "http_4xx"
    assert _queued_tasks() == []


def test_submit_idea_endpoint_needs_text_or_a_url(forven_db):
    client = TestClient(app)
    r = client.post("/api/ideas", json={"url": ""})
    assert r.status_code == 200
    assert r.json()["error_code"] == "empty_idea"
    assert _queued_tasks() == []


def test_submit_idea_endpoint_empty_content_surfaces_failure(forven_db):
    """Unextractable article: blog returns ok with empty content. A hollow source
    gives the agent nothing to build from, so the operator gets ``content_empty``
    and is told to paste a different source."""
    fake = {"ok": True, "title": "No Content", "content": "", "url": "u", "source": "blog"}
    with patch("forven.research_sources.url_ingest.blog.inspect_blog_article", return_value=fake):
        client = TestClient(app)
        r = client.post("/api/ideas", json={"url": "https://random.example.com/empty"})
    body = r.json()
    assert body["ok"] is False
    assert body["error_code"] == "content_empty"
    assert body["source_type"] == "blog"
    assert _queued_tasks() == []
