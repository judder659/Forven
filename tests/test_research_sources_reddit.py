from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from forven.research_sources import reddit

FIX = Path(__file__).parent / "fixtures"


def _mock(client, responses, seen=None):
    it = iter(responses)

    def handler(req):
        if seen is not None:
            seen.append(req)
        return next(it)

    client._transport = httpx.MockTransport(handler)


def test_search_parses_posts():
    body = (FIX / "reddit_search.json").read_text()
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(200, text=body)])
    res = reddit.search_reddit_posts("mean reversion", subs=["algotrading"], limit=5, client=client, access_token="tok")
    assert res["ok"] is True
    assert res["count"] >= 1
    first = res["results"][0]
    assert {"permalink", "title", "subreddit", "score", "num_comments", "created_utc"} <= first.keys()
    assert first["title"] == "Mean reversion on stretched funding rates"


def test_search_aggregates_across_subs():
    body = (FIX / "reddit_search.json").read_text()
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(200, text=body), httpx.Response(200, text=body)])
    res = reddit.search_reddit_posts("x", subs=["algotrading", "quant"], limit=10, client=client, access_token="tok")
    assert res["ok"] is True
    assert res["count"] <= 10


def test_search_respects_limit():
    body = (FIX / "reddit_search.json").read_text()
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(200, text=body)])
    res = reddit.search_reddit_posts("x", subs=["algotrading"], limit=1, client=client, access_token="tok")
    assert res["count"] == 1


def test_search_empty_query():
    client = reddit._client(rate_per_min=1000)
    res = reddit.search_reddit_posts("", subs=["algotrading"], limit=5, client=client)
    assert res["ok"] is False
    assert "empty query" in res["error"]
    assert res["error_code"] == "invalid_input"


def test_search_no_subs():
    client = reddit._client(rate_per_min=1000)
    res = reddit.search_reddit_posts("x", subs=[], limit=5, client=client)
    assert res["ok"] is False
    assert res["error_code"] == "invalid_input"


def test_search_rate_limit_429_returns_error_code():
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(429, text='{"error":429}')])
    res = reddit.search_reddit_posts("x", subs=["algotrading"], limit=5, client=client, access_token="tok")
    assert res["ok"] is False
    assert res["error_code"] == "rate_limited"


def test_search_http_500_returns_error_code():
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(500)])
    res = reddit.search_reddit_posts("x", subs=["algotrading"], limit=5, client=client, access_token="tok")
    assert res["ok"] is False
    assert res["error_code"] == "http_5xx"


def test_inspect_returns_content_with_title_and_comments():
    body = (FIX / "reddit_thread.json").read_text()
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(200, text=body)])
    res = reddit.inspect_reddit_thread_with_auth("/r/algotrading/comments/abc123/", client=client, access_token="tok")
    assert res["ok"] is True
    assert "Mean reversion on stretched funding rates" in res["content"]
    assert "mean-reversion strategy on BTC" in res["content"]
    assert any("ADX filtering" in c for c in res["top_comments"])
    assert res["url"].endswith(".json")


def test_inspect_walks_nested_replies():
    body = (FIX / "reddit_thread.json").read_text()
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(200, text=body)])
    res = reddit.inspect_reddit_thread_with_auth("/r/algotrading/comments/abc123/", client=client, access_token="tok")
    # Nested reply should be captured
    assert any("Cut drawdown" in c for c in res["top_comments"])


def test_inspect_deleted_thread_is_ok():
    body = (FIX / "reddit_deleted.json").read_text()
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(200, text=body)])
    res = reddit.inspect_reddit_thread_with_auth("/r/algotrading/comments/del999/", client=client, access_token="tok")
    assert res["ok"] is True
    assert res["selftext"] == "[deleted]"
    assert res["top_comments"] == []


def test_inspect_full_url_form():
    body = (FIX / "reddit_thread.json").read_text()
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(200, text=body)])
    res = reddit.inspect_reddit_thread_with_auth("https://www.reddit.com/r/algotrading/comments/abc123/", client=client, access_token="tok")
    assert res["ok"] is True


def test_inspect_empty_permalink():
    client = reddit._client(rate_per_min=1000)
    res = reddit.inspect_reddit_thread("  ", client=client)
    assert res["ok"] is False
    assert res["error_code"] == "invalid_input"


def test_inspect_returns_parse_error_on_non_list_payload():
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(200, text='{"message": "Forbidden", "error": 403}')])
    res = reddit.inspect_reddit_thread_with_auth("/r/algotrading/comments/x/", client=client, access_token="tok")
    assert res["ok"] is False
    assert res["error_code"] == "parse"


# ── no key: public RSS feeds ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_rss_pause(monkeypatch):
    monkeypatch.setattr(reddit, "_rss_blocked_until", 0.0)
    monkeypatch.setattr(reddit.time, "sleep", lambda s: None)


def test_search_without_key_reads_one_feed_for_all_subs():
    body = (FIX / "reddit_search.rss").read_text()
    client = reddit._client(rate_per_min=1000)
    seen = []
    _mock(client, [httpx.Response(200, text=body)], seen)
    res = reddit.search_reddit_posts("mean reversion", subs=["algotrading", "quant"], limit=5, client=client)
    assert len(seen) == 1
    assert seen[0].url.host == "www.reddit.com"
    assert seen[0].url.path == "/r/algotrading+quant/search.rss"
    assert seen[0].url.params["q"] == "mean reversion"
    assert "Mozilla" in seen[0].headers["user-agent"]
    assert res["ok"] is True and res["count"] == 2
    first = res["results"][0]
    assert first["title"] == "Mean reversion on stretched funding rates"
    assert first["permalink"] == "/r/algotrading/comments/abc123/mean_reversion_funding_rates/"
    assert first["subreddit"] == "algotrading"
    assert first["created_utc"] == 1775417678.0


def test_inspect_without_key_reads_the_thread_feed():
    body = (FIX / "reddit_thread.rss").read_text()
    client = reddit._client(rate_per_min=1000)
    seen = []
    _mock(client, [httpx.Response(200, text=body)], seen)
    res = reddit.inspect_reddit_thread("https://www.reddit.com/r/algotrading/comments/abc123/mean_reversion_funding_rates/", client=client)
    assert str(seen[0].url) == "https://www.reddit.com/r/algotrading/comments/abc123/mean_reversion_funding_rates.rss"
    assert res["ok"] is True
    assert res["title"] == "Mean reversion on stretched funding rates"
    assert "fades funding above 0.05%" in res["selftext"]
    assert "Sharpe 1.4" in res["selftext"]
    assert "submitted by" not in res["selftext"]
    assert res["top_comments"] == ["Did you try ADX filtering? Regimes matter.", "Cut drawdown by a third with ADX < 20."]
    assert "ADX filtering" in res["content"]


def test_inspect_rejects_non_reddit_and_non_thread_links():
    client = reddit._client(rate_per_min=1000)
    assert reddit.inspect_reddit_thread("https://evil.example/r/x/comments/1/", client=client)["error_code"] == "invalid_input"
    assert reddit.inspect_reddit_thread("https://www.reddit.com/r/algotrading/", client=client)["error_code"] == "invalid_input"


def test_short_429_is_waited_out_once(monkeypatch):
    waits = []
    monkeypatch.setattr(reddit.time, "sleep", waits.append)
    body = (FIX / "reddit_search.rss").read_text()
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(429, headers={"x-ratelimit-reset": "8"}), httpx.Response(200, text=body)])
    res = reddit.search_reddit_posts("x", subs=["algotrading"], client=client)
    assert res["ok"] is True
    assert len(waits) == 1 and 7 < waits[0] <= 8


def test_second_429_in_one_call_is_reported(monkeypatch):
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(429, headers={"x-ratelimit-reset": "8"}), httpx.Response(429, headers={"x-ratelimit-reset": "8"})])
    res = reddit.search_reddit_posts("x", subs=["algotrading"], client=client)
    assert res["ok"] is False and res["error_code"] == "rate_limited"


def test_used_up_window_paces_the_next_call(monkeypatch):
    waits = []
    monkeypatch.setattr(reddit.time, "sleep", waits.append)
    body = (FIX / "reddit_search.rss").read_text()
    client = reddit._client(rate_per_min=1000)
    spent = {"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "58"}
    _mock(client, [httpx.Response(200, text=body, headers=spent), httpx.Response(200, text=body)])
    assert reddit.search_reddit_posts("x", subs=["algotrading"], client=client)["ok"] is True
    assert waits == []
    assert reddit.search_reddit_posts("y", subs=["algotrading"], client=client)["ok"] is True
    assert len(waits) == 1 and 57 < waits[0] <= 58


def test_long_429_is_reported_and_remembered():
    client = reddit._client(rate_per_min=1000)
    seen = []
    _mock(client, [httpx.Response(429, headers={"x-ratelimit-reset": "300"})], seen)
    res = reddit.search_reddit_posts("x", subs=["algotrading"], client=client)
    assert res["ok"] is False and res["error_code"] == "rate_limited"
    again = reddit.inspect_reddit_thread("/r/algotrading/comments/abc123/", client=client)
    assert again["error_code"] == "rate_limited"
    assert len(seen) == 1  # the second call didn't hit Reddit


def test_feed_403_is_an_http_error():
    client = reddit._client(rate_per_min=1000)
    _mock(client, [httpx.Response(403, text="blocked")])
    res = reddit.inspect_reddit_thread("/r/algotrading/comments/abc123/", client=client)
    assert res == {"ok": False, "error_code": "http_4xx", "error": "http 403"}


# ── with a key: OAuth ─────────────────────────────────────────────────────────


def test_oauth_search_uses_bearer_token_and_one_request():
    body = (FIX / "reddit_search.json").read_text()
    client = reddit._client(rate_per_min=1000)
    seen = []
    _mock(client, [httpx.Response(200, text=body)], seen)
    reddit.search_reddit_posts("x", subs=["algotrading", "quant"], client=client, access_token="tok")
    assert len(seen) == 1
    assert seen[0].url.host == "oauth.reddit.com"
    assert seen[0].url.path == "/r/algotrading+quant/search"
    assert seen[0].headers["authorization"] == "Bearer tok"


def test_access_token_is_cached(monkeypatch):
    calls = []

    def fake_fetch(cid, secret):
        calls.append(cid)
        return {"ok": True, "access_token": f"tok-{len(calls)}", "expires_in": 3600}

    monkeypatch.setattr(reddit, "_token_cache", {})
    monkeypatch.setattr(reddit, "fetch_oauth_token", fake_fetch)
    assert reddit.cached_access_token("cid", "sec") == "tok-1"
    assert reddit.cached_access_token("cid", "sec") == "tok-1"
    assert reddit.cached_access_token(None, "sec") is None
    assert calls == ["cid"]


def test_oauth_credentials_fall_back_to_env(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "env-id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "env-secret")
    assert reddit.oauth_credentials({"client_id": None, "client_secret": None}) == ("env-id", "env-secret")
    assert reddit.oauth_credentials({"client_id": " cfg-id ", "client_secret": "s"}) == ("cfg-id", "s")
