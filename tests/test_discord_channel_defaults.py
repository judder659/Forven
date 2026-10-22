"""Discord aliases resolve per install: no shipped channel ids, config first,
then the bot's own server channels by name, then a catch-all."""
from __future__ import annotations

import pytest

from forven import discord_channels as dc

SERVER = [
    {"id": "11", "name": "general"},
    {"id": "22", "name": "alerts"},
    {"id": "33", "name": "quant-researcher"},
    {"id": "44", "name": "chat"},
]


@pytest.fixture
def setup(monkeypatch):
    state = {"config": {}, "live": None}
    monkeypatch.setattr(dc, "load_config", lambda: state["config"])
    monkeypatch.setattr(dc, "available_channels", lambda: state["live"])
    return state


def test_no_channel_ids_ship_with_forven(setup):
    assert dc.load_channel_map() == {}
    assert dc.resolve_channel_id("alerts") is None
    assert "alerts" in dc.channel_aliases()


def test_aliases_match_the_bots_own_server(setup):
    setup["live"] = SERVER
    channels = dc.load_channel_map()
    assert channels["alerts"] == "22"
    assert channels["heartbeat"] == "22"  # heartbeat falls back to #alerts
    assert channels["research"] == "33"  # matched by its old room name
    assert channels["chat"] == "44"
    assert channels["paper-trades"] == "11"  # no match -> #general


def test_catch_all_is_first_channel_without_general(setup):
    setup["live"] = [{"id": "77", "name": "bot-posts"}]
    assert dc.resolve_channel_id("alerts") == "77"
    # A reply channel is never the catch-all.
    assert "chat" not in dc.load_channel_map()


def test_config_overrides_win(setup):
    setup["live"] = SERVER
    setup["config"] = {"discord_channels": {"alerts": "99", "custom": "55"}}
    channels = dc.load_channel_map()
    assert channels["alerts"] == "99"
    assert channels["custom"] == "55"
    assert "custom" in dc.channel_aliases()


def test_without_fallback_only_real_matches(setup):
    setup["live"] = SERVER
    matched = dc.load_channel_map(fallback=False)
    assert matched["alerts"] == "22"
    assert "paper-trades" not in matched


def test_raw_ids_pass_through(setup):
    assert dc.resolve_channel_id("123456") == "123456"
    assert dc.resolve_channel_id("") is None
