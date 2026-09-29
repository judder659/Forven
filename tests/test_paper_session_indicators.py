from __future__ import annotations

import pandas as pd

from forven.api_domains import paper as paper_domain
from forven.db import get_db


def test_session_indicators_follow_requested_timeframe_and_classify_panels(monkeypatch):
    requested_intervals: list[str] = []

    session = {
        "id": "compat:strategy:S00338",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "started_at": "2026-03-11T20:00:00+00:00",
        "indicators": {
            "entry_signal": {"name": "entry_signal", "value": 1.0, "timestamp": "2026-03-11T23:19:00+00:00"},
            "exit_signal": {"name": "exit_signal", "value": 0.0, "timestamp": "2026-03-11T23:19:00+00:00"},
            "atr_14": {"name": "atr_14", "value": 12.5, "timestamp": "2026-03-11T23:19:00+00:00"},
            "ema_fast": {"name": "ema_fast", "value": 101.0, "timestamp": "2026-03-11T23:19:00+00:00"},
            "EMA_9": {"name": "EMA_9", "value": 102.0, "timestamp": "2026-03-11T23:19:00+00:00"},
        },
    }

    idx = pd.date_range("2026-03-11T22:00:00Z", periods=180, freq="min", tz="UTC")
    close = pd.Series(range(180), dtype=float).to_numpy() + 100.0
    frame = pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 0.6,
            "low": close - 0.8,
            "close": close,
            "volume": pd.Series(range(180), dtype=float).to_numpy() + 1000.0,
        },
        index=idx,
    )

    monkeypatch.setattr(paper_domain, "_find_compat_paper_session", lambda session_id, include_deployed=True: session)

    def _fake_fetch(asset: str, *, bars: int = 300, interval: str = "1h", end_time=None,
                    clean: bool = False, include_unclosed: bool = False, **_):
        requested_intervals.append(interval)
        return frame.tail(bars)

    monkeypatch.setattr(paper_domain, "fetch_market_candles",_fake_fetch)

    result = paper_domain.get_paper_session_indicators(
        "compat:strategy:S00338",
        limit=60,
        timeframe="1m",
    )

    assert requested_intervals == ["1m"]
    assert result["config"]["ema_fast"]["panel"] == "main"
    assert result["config"]["EMA_9"]["panel"] == "main"
    assert result["config"]["atr_14"]["panel"] == "sub"
    assert result["config"]["entry_signal"]["panel"] == "none"
    assert result["config"]["exit_signal"]["panel"] == "none"
    assert result["config"]["ema_fast"]["color"] == "#22c55e"
    assert result["config"]["atr_14"]["color"] == "#fb7185"
    assert result["config"]["entry_signal"]["color"] == "#22c55e"
    assert len(result["indicators"]["atr_14"]) == 60
    assert len(result["indicators"]["EMA_9"]) == 60
    assert result["indicators"]["EMA_9"][-1]["timestamp"].endswith("00:59:00+00:00")
    assert result["indicators"]["entry_signal"] == [
        {"timestamp": "2026-03-11T23:19:00+00:00", "value": 1.0}
    ]


def test_session_indicators_use_container_default_periods(monkeypatch):
    session = {
        "id": "compat:strategy:S-DEFAULT-PARAMS",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "started_at": "2026-03-11T20:00:00+00:00",
        "params": {
            "ema_fast": 3,
            "ema_slow": 5,
            "rsi_period": 3,
        },
        "indicators": {},
    }

    idx = pd.date_range("2026-03-11T22:00:00Z", periods=20, freq="min", tz="UTC")
    close = pd.Series(range(20), dtype=float).to_numpy() + 100.0
    frame = pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 0.6,
            "low": close - 0.8,
            "close": close,
            "volume": pd.Series(range(20), dtype=float).to_numpy() + 1000.0,
        },
        index=idx,
    )

    monkeypatch.setattr(paper_domain, "_find_compat_paper_session", lambda session_id, include_deployed=True: session)
    monkeypatch.setattr(paper_domain, "fetch_market_candles",lambda *_args, **_kwargs: frame)

    result = paper_domain.get_paper_session_indicators(
        "compat:strategy:S-DEFAULT-PARAMS",
        indicators="ema_fast,ema_slow,rsi",
        limit=20,
        timeframe="1m",
    )

    assert result["indicators"]["ema_fast"][-1]["value"] == paper_domain._ema_series(list(close), 3)[-1]
    assert result["indicators"]["ema_slow"][-1]["value"] == paper_domain._ema_series(list(close), 5)[-1]
    assert result["indicators"]["rsi"][-1]["value"] == paper_domain._rsi_series(list(close), 3)[-1]


def test_session_markers_include_strategy_signal_intent_from_default_params(monkeypatch):
    session = {
        "id": "compat:strategy:S-SIGNAL-MARKERS",
        "strategy_id": "S-SIGNAL-MARKERS",
        "strategy_type": "ema_cross",
        "runtime_type": "ema_cross",
        "symbol": "BTC/USDT",
        "timeframe": "1m",
        "params": {"ema_fast": 3, "ema_slow": 5},
        "trades": [],
        "positions": [],
    }
    bars = [
        {
            "timestamp": ts.isoformat(),
            "open": 100.0 + idx,
            "high": 101.0 + idx,
            "low": 99.0 + idx,
            "close": 100.0 + idx,
            "volume": 1000.0 + idx,
        }
        for idx, ts in enumerate(pd.date_range("2026-03-11T22:00:00Z", periods=6, freq="min", tz="UTC"))
    ]

    monkeypatch.setattr(paper_domain, "_find_compat_paper_session", lambda session_id, include_deployed=True: session)
    monkeypatch.setattr(paper_domain, "_load_session_bars", lambda *_args, **_kwargs: bars)

    import forven.scanner as scanner_mod

    def fake_get_signal(strat_id: str, strat: dict, frame: pd.DataFrame, strategy_instance=None) -> dict:
        assert strat_id == "S-SIGNAL-MARKERS"
        assert strat["params"]["ema_fast"] == 3
        price = float(frame["close"].iloc[-1])
        return {
            "price": price,
            "entry_signal": len(frame) == 3,
            "exit_signal": len(frame) == 5,
            "direction": "long",
        }

    monkeypatch.setattr(scanner_mod, "get_signal", fake_get_signal)

    result = paper_domain.get_paper_session_markers(
        "compat:strategy:S-SIGNAL-MARKERS",
        include_generated=True,
    )

    signal_entries = [marker for marker in result["entries"] if marker.get("marker_kind") == "signal"]
    signal_exits = [marker for marker in result["exits"] if marker.get("marker_kind") == "signal"]
    assert signal_entries == [
        {
            "timestamp": bars[2]["timestamp"],
            "price": 102.0,
            "trade_id": "signal:entry:S-SIGNAL-MARKERS:2",
            "is_open": False,
            "direction": "long",
            "marker_kind": "signal",
            "reason": "entry_signal",
        }
    ]
    assert signal_exits == [
        {
            "timestamp": bars[4]["timestamp"],
            "price": 104.0,
            "trade_id": "signal:exit:S-SIGNAL-MARKERS:4",
            "is_open": False,
            "direction": "long",
            "marker_kind": "signal",
            "reason": "exit_signal",
        }
    ]


def test_session_markers_skip_generated_signal_reconstruction_by_default(monkeypatch):
    session = {
        "id": "compat:strategy:S-SIGNAL-MARKERS",
        "strategy_id": "S-SIGNAL-MARKERS",
        "strategy_type": "ema_cross",
        "runtime_type": "ema_cross",
        "symbol": "BTC/USDT",
        "timeframe": "1m",
        "params": {"ema_fast": 3, "ema_slow": 5},
        "trades": [],
        "positions": [],
    }

    monkeypatch.setattr(paper_domain, "_find_compat_paper_session", lambda session_id, include_deployed=True: session)
    monkeypatch.setattr(
        paper_domain,
        "_load_session_bars",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not recompute markers")),
    )

    result = paper_domain.get_paper_session_markers("compat:strategy:S-SIGNAL-MARKERS")

    assert result == {"entries": [], "exits": [], "blocked": []}


def test_session_markers_use_persisted_signal_results_before_recomputing(forven_db, monkeypatch):
    session = {
        "id": "compat:strategy:S-PERSISTED-SIGNALS",
        "strategy_id": "S-PERSISTED-SIGNALS",
        "strategy_type": "ema_cross",
        "runtime_type": "ema_cross",
        "symbol": "BTC/USDT",
        "timeframe": "1m",
        "params": {"ema_fast": 3, "ema_slow": 5},
        "trades": [],
        "positions": [],
    }
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO scanner_signal_results
            (ts, strategy_id, symbol, signal_type, matched, executed, price, match_reason, block_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-03-11T22:03:00+00:00",
                "S-PERSISTED-SIGNALS",
                "BTC",
                "entry",
                1,
                0,
                102.0,
                "ema_cross",
                None,
            ),
        )
        conn.execute(
            """
            INSERT INTO scanner_signal_results
            (ts, strategy_id, symbol, signal_type, matched, executed, price, match_reason, block_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-03-11T22:04:00+00:00",
                "S-PERSISTED-SIGNALS",
                "BTC",
                "evaluate",
                0,
                0,
                103.0,
                None,
                "regime blocked",
            ),
        )

    monkeypatch.setattr(paper_domain, "_find_compat_paper_session", lambda session_id, include_deployed=True: session)
    monkeypatch.setattr(
        paper_domain,
        "_load_session_bars",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not recompute markers")),
    )

    result = paper_domain.get_paper_session_markers("compat:strategy:S-PERSISTED-SIGNALS")

    assert result["entries"] == [
        {
            "timestamp": "2026-03-11T22:03:00+00:00",
            "price": 102.0,
            "trade_id": "signal:persisted:entry:S-PERSISTED-SIGNALS:1",
            "is_open": False,
            "direction": "long",
            "marker_kind": "signal",
            "reason": "ema_cross",
            "executed": False,
        }
    ]
    assert result["exits"] == []
    assert result["blocked"] == [
        {
            "timestamp": "2026-03-11T22:04:00+00:00",
            "price": 103.0,
            "trade_id": "signal:persisted:blocked:S-PERSISTED-SIGNALS:2",
            "is_open": False,
            "direction": "long",
            "marker_kind": "blocked",
            "reason": "regime blocked",
            "executed": False,
        }
    ]


def test_session_markers_coalesce_repeated_persisted_signals(forven_db, monkeypatch):
    session = {
        "id": "compat:strategy:S-PERSISTED-REPEATS",
        "strategy_id": "S-PERSISTED-REPEATS",
        "strategy_type": "ema_cross",
        "runtime_type": "ema_cross",
        "symbol": "BTC/USDT",
        "timeframe": "1m",
        "params": {"ema_fast": 3, "ema_slow": 5},
        "trades": [],
        "positions": [],
    }
    rows = [
        ("2026-03-11T22:01:00+00:00", "exit", 101.0),
        ("2026-03-11T22:02:00+00:00", "exit", 102.0),
        ("2026-03-11T22:03:00+00:00", "exit", 103.0),
        ("2026-03-11T22:04:00+00:00", "entry", 104.0),
        ("2026-03-11T22:05:00+00:00", "entry", 105.0),
        ("2026-03-11T22:06:00+00:00", "exit", 106.0),
    ]
    with get_db() as conn:
        conn.executemany(
            """
            INSERT INTO scanner_signal_results
            (ts, strategy_id, symbol, signal_type, matched, executed, price, match_reason, block_reason)
            VALUES (?, 'S-PERSISTED-REPEATS', 'BTC', ?, 1, 0, ?, 'scanner', NULL)
            """,
            rows,
        )

    monkeypatch.setattr(paper_domain, "_find_compat_paper_session", lambda session_id, include_deployed=True: session)
    monkeypatch.setattr(
        paper_domain,
        "_load_session_bars",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not recompute markers")),
    )

    result = paper_domain.get_paper_session_markers("compat:strategy:S-PERSISTED-REPEATS")

    assert [marker["timestamp"] for marker in result["exits"]] == [
        "2026-03-11T22:01:00+00:00",
        "2026-03-11T22:06:00+00:00",
    ]
    assert [marker["timestamp"] for marker in result["entries"]] == [
        "2026-03-11T22:04:00+00:00",
    ]


def test_session_markers_skip_plain_evaluations_and_read_the_newest_rows(forven_db, monkeypatch):
    session = {
        "id": "compat:strategy:S-MARKER-RECENT",
        "strategy_id": "S-MARKER-RECENT",
        "strategy_type": "ema_cross",
        "runtime_type": "ema_cross",
        "symbol": "BTC/USDT",
        "timeframe": "1m",
        "params": {},
        "trades": [],
        "positions": [],
    }
    rows = [
        # Oldest history, beyond the marker limit once newer rows exist.
        ("2026-03-11T20:00:00+00:00", "entry", 1, 0, 90.0, "ema_cross", None),
        ("2026-03-11T22:00:00+00:00", "evaluate", 0, 0, 101.0, None, "no_signal"),
        ("2026-03-11T22:01:00+00:00", "evaluate", 0, 0, 101.5, None, "evaluation_only"),
        ("2026-03-11T22:02:00+00:00", "evaluate", 0, 0, 102.0, None, "regime blocked"),
    ]
    with get_db() as conn:
        for ts, signal_type, matched, executed, price, match_reason, block_reason in rows:
            conn.execute(
                """
                INSERT INTO scanner_signal_results
                (ts, strategy_id, symbol, signal_type, matched, executed, price, match_reason, block_reason)
                VALUES (?, 'S-MARKER-RECENT', 'BTC', ?, ?, ?, ?, ?, ?)
                """,
                (ts, signal_type, matched, executed, price, match_reason, block_reason),
            )

    monkeypatch.setattr(paper_domain, "_find_compat_paper_session", lambda session_id, include_deployed=True: session)
    monkeypatch.setattr(
        paper_domain,
        "_load_session_bars",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not recompute markers")),
    )

    result = paper_domain.get_paper_session_markers("compat:strategy:S-MARKER-RECENT", limit=3)

    assert result["entries"] == []  # the oldest row fell outside the newest three
    assert [marker["reason"] for marker in result["blocked"]] == ["regime blocked"]
