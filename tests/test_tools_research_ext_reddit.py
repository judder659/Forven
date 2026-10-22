from __future__ import annotations

from unittest.mock import patch

from forven.agents.tools_research import (
    _tool_discover_reddit_posts,
    _tool_inspect_reddit_thread,
)


from tests.research_ext_utils import parse_tool_result as json_loads

def _all_pass_contract():
    return {
        "lane": "benchmarking",
        "external_sources_allowed": True,
        "allowed_external_source_types": ["reddit"],
    }


def test_discover_blocked_when_not_benchmarking_lane(monkeypatch):
    monkeypatch.setattr(
        "forven.agents.tools_research._current_research_contract",
        lambda: {"lane": "exploration", "external_sources_allowed": True, "allowed_external_source_types": ["reddit"]},
    )
    res = json_loads(_tool_discover_reddit_posts({"query": "x"}))
    assert res["ok"] is False
    assert "benchmarking" in res["error"]


def test_discover_blocked_when_external_sources_not_allowed(monkeypatch):
    monkeypatch.setattr(
        "forven.agents.tools_research._current_research_contract",
        lambda: {"lane": "benchmarking", "external_sources_allowed": False, "allowed_external_source_types": ["reddit"]},
    )
    res = json_loads(_tool_discover_reddit_posts({"query": "x"}))
    assert res["ok"] is False
    assert "external benchmarking" in res["error"]


def test_discover_blocked_when_reddit_not_allowed_type(monkeypatch):
    monkeypatch.setattr(
        "forven.agents.tools_research._current_research_contract",
        lambda: {"lane": "benchmarking", "external_sources_allowed": True, "allowed_external_source_types": ["youtube"]},
    )
    res = json_loads(_tool_discover_reddit_posts({"query": "x"}))
    assert res["ok"] is False
    assert "reddit" in res["error"]


def test_discover_blocked_when_registry_disabled(monkeypatch):
    monkeypatch.setattr("forven.agents.tools_research._current_research_contract", _all_pass_contract)
    monkeypatch.setattr("forven.agents.tools_research._resolve_source_registry", lambda t: None)
    res = json_loads(_tool_discover_reddit_posts({"query": "x"}))
    assert res["ok"] is False
    assert "disabled" in res["error"]


def test_discover_blocked_when_no_subs(monkeypatch):
    monkeypatch.setattr("forven.agents.tools_research._current_research_contract", _all_pass_contract)
    monkeypatch.setattr(
        "forven.agents.tools_research._resolve_source_registry",
        lambda t: {"subs": [], "rate_limit_per_min": 30},
    )
    res = json_loads(_tool_discover_reddit_posts({"query": "x"}))
    assert res["ok"] is False


def test_discover_happy_path_calls_search(monkeypatch):
    monkeypatch.setattr("forven.agents.tools_research._current_research_contract", _all_pass_contract)
    monkeypatch.setattr(
        "forven.agents.tools_research._resolve_source_registry",
        lambda t: {"subs": ["algotrading"], "rate_limit_per_min": 30},
    )
    with patch("forven.research_sources.reddit.search_reddit_posts") as m:
        m.return_value = {"ok": True, "source": "reddit", "query": "x", "count": 2, "results": [{"title": "a"}, {"title": "b"}]}
        res = json_loads(_tool_discover_reddit_posts({"query": "x", "limit": 5}))
    assert res["ok"] is True
    assert res["count"] == 2
    # Confirm it was called with subs from registry (since params.subs not provided)
    call_kwargs = m.call_args.kwargs
    assert call_kwargs["subs"] == ["algotrading"]
    assert call_kwargs["limit"] == 5


def test_discover_params_subs_override_registry(monkeypatch):
    monkeypatch.setattr("forven.agents.tools_research._current_research_contract", _all_pass_contract)
    monkeypatch.setattr(
        "forven.agents.tools_research._resolve_source_registry",
        lambda t: {"subs": ["algotrading"], "rate_limit_per_min": 30},
    )
    with patch("forven.research_sources.reddit.search_reddit_posts") as m:
        m.return_value = {"ok": True, "source": "reddit", "query": "x", "count": 0, "results": []}
        json_loads(_tool_discover_reddit_posts({"query": "x", "subs": ["quant"]}))
    assert m.call_args.kwargs["subs"] == ["quant"]


def test_inspect_happy_path(monkeypatch):
    monkeypatch.setattr("forven.agents.tools_research._current_research_contract", _all_pass_contract)
    monkeypatch.setattr(
        "forven.agents.tools_research._resolve_source_registry",
        lambda t: {"subs": ["algotrading"], "rate_limit_per_min": 30},
    )
    with patch("forven.research_sources.reddit.inspect_reddit_thread_with_auth") as m:
        m.return_value = {"ok": True, "source": "reddit", "content": "hello", "title": "T", "selftext": "", "top_comments": [], "url": "u"}
        res = json_loads(_tool_inspect_reddit_thread({"permalink": "/r/algotrading/comments/x/"}))
    assert res["ok"] is True
    assert res["content"] == "hello"


def test_inspect_blocked_when_not_benchmarking(monkeypatch):
    monkeypatch.setattr(
        "forven.agents.tools_research._current_research_contract",
        lambda: {"lane": "exploration", "external_sources_allowed": True, "allowed_external_source_types": ["reddit"]},
    )
    res = json_loads(_tool_inspect_reddit_thread({"permalink": "/r/x/"}))
    assert res["ok"] is False


def test_inspect_propagates_registry_error(monkeypatch):
    from forven.research_sources._registry import RegistryError
    monkeypatch.setattr("forven.agents.tools_research._current_research_contract", _all_pass_contract)
    def _raise(_t):
        raise RegistryError("subs must be list[str]")
    monkeypatch.setattr("forven.agents.tools_research._resolve_source_registry", _raise)
    res = json_loads(_tool_inspect_reddit_thread({"permalink": "/r/x/"}))
    assert res["ok"] is False
    assert "registry error" in res["error"]


def test_discover_handles_non_integer_limit(monkeypatch):
    monkeypatch.setattr("forven.agents.tools_research._current_research_contract", _all_pass_contract)
    monkeypatch.setattr(
        "forven.agents.tools_research._resolve_source_registry",
        lambda t: {"subs": ["algotrading"], "rate_limit_per_min": 30},
    )
    with patch("forven.research_sources.reddit.search_reddit_posts") as m:
        m.return_value = {"ok": True, "source": "reddit", "query": "x", "count": 0, "results": []}
        res = json_loads(_tool_discover_reddit_posts({"query": "x", "limit": "abc"}))
    assert res["ok"] is True
    assert m.call_args.kwargs["limit"] == 10  # fell back to default


def test_discover_handles_zero_limit(monkeypatch):
    monkeypatch.setattr("forven.agents.tools_research._current_research_contract", _all_pass_contract)
    monkeypatch.setattr(
        "forven.agents.tools_research._resolve_source_registry",
        lambda t: {"subs": ["algotrading"], "rate_limit_per_min": 30},
    )
    with patch("forven.research_sources.reddit.search_reddit_posts") as m:
        m.return_value = {"ok": True, "source": "reddit", "query": "x", "count": 0, "results": []}
        json_loads(_tool_discover_reddit_posts({"query": "x", "limit": 0}))
    assert m.call_args.kwargs["limit"] == 10


def test_coerce_positive_int_helper():
    from forven.agents.tools_research import _coerce_positive_int
    assert _coerce_positive_int(None, 5) == 5
    assert _coerce_positive_int("abc", 5) == 5
    assert _coerce_positive_int(-3, 5) == 5
    assert _coerce_positive_int(0, 5) == 5
    assert _coerce_positive_int(7, 5) == 7
    assert _coerce_positive_int("7", 5) == 7
