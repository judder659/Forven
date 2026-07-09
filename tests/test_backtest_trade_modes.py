from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

from forven.db import get_db
from forven.strategies import backtest as backtest_mod
from forven.strategies.base import BaseStrategy, DirectionalSignals, Signal


def _price_frame() -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01T00:00:00Z", periods=260, freq="h", tz="UTC")
    closes = [100.0] * 210
    closes.extend([101.0 + idx for idx in range(11)])  # long-friendly move
    closes.extend([110.0 - idx for idx in range(39)])  # short-friendly move
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": closes,
            "high": [value + 0.5 for value in closes],
            "low": [value - 0.5 for value in closes],
            "close": closes,
            "volume": [1_000.0] * len(closes),
        }
    )
    frame = frame.set_index("timestamp", drop=False)
    return frame


class _MirrorShortStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "Mirror Short"

    @property
    def asset(self) -> str:
        return "BTC"

    @property
    def strategy_type(self) -> str:
        return "mirror_short_dummy"

    @property
    def default_params(self) -> dict:
        return {}

    @property
    def mirror_short_safe(self) -> bool:
        return True

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        return Signal(price=float(df["close"].iloc[-1]), direction="long")

    def generate_signals(self, df: pd.DataFrame):
        entries = pd.Series(False, index=df.index, dtype=bool)
        exits = pd.Series(False, index=df.index, dtype=bool)
        if len(df) > 230:
            entries.iloc[221] = True
            exits.iloc[230] = True
        return entries, exits


class _BothSidesStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "Both Sides"

    @property
    def asset(self) -> str:
        return "BTC"

    @property
    def strategy_type(self) -> str:
        return "both_dummy"

    @property
    def default_params(self) -> dict:
        return {}

    @property
    def supported_trade_modes(self) -> set[str]:
        return {"long_only", "both"}

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        return Signal(price=float(df["close"].iloc[-1]), direction="long")

    def generate_signals(self, df: pd.DataFrame) -> DirectionalSignals:
        signals = DirectionalSignals.empty(df.index)
        if len(df) > 230:
            signals.long_entries.iloc[210] = True
            signals.long_exits.iloc[220] = True
            signals.short_entries.iloc[221] = True
            signals.short_exits.iloc[230] = True
        return signals


def _patch_backtest_environment(monkeypatch):
    monkeypatch.setattr(backtest_mod, "_should_use_process_isolation", lambda: False)
    monkeypatch.setattr(backtest_mod, "_sync_strategy_metrics_and_promote_if_eligible", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backtest_mod, "_run_remote_backtest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        backtest_mod,
        "_validate_backtest_execution_parity",
        lambda strategy_type, params, **_kwargs: (dict(params or {}), None, None),
    )
    monkeypatch.setattr(
        backtest_mod,
        "canonicalize_params",
        lambda _strategy_type, params: SimpleNamespace(params=dict(params or {})),
    )


def test_backtest_strategy_supports_mirrored_short_only(forven_db, monkeypatch):
    _patch_backtest_environment(monkeypatch)
    monkeypatch.setattr(
        backtest_mod,
        "_resolve_strategy_class",
        lambda strategy_type: _MirrorShortStrategy if strategy_type == "mirror_short_dummy" else None,
    )

    result = backtest_mod.backtest_strategy(
        strategy_id="S-SHORT",
        asset="BTC/USDT",
        strategy_type="mirror_short_dummy",
        params={},
        bars=260,
        candles_df=_price_frame(),
        trade_mode="short_only",
        persist_legacy_run=False,
    )

    assert not result.get("error")
    assert result["trade_mode"] == "short_only"
    assert result["position_model"] == "single_side"
    assert result["metrics"]["trade_mode"] == "short_only"
    assert result["metrics"]["by_side"]["short"]["total_trades"] >= 1
    assert {trade["direction"] for trade in result["trades"]} == {"short"}
    assert result["trades"][0]["pnl_pct"] > 0


def test_backtest_strategy_supports_both_side_directional_signals(forven_db, monkeypatch):
    _patch_backtest_environment(monkeypatch)
    monkeypatch.setattr(
        backtest_mod,
        "_resolve_strategy_class",
        lambda strategy_type: _BothSidesStrategy if strategy_type == "both_dummy" else None,
    )

    result = backtest_mod.backtest_strategy(
        strategy_id="S-BOTH",
        asset="BTC/USDT",
        strategy_type="both_dummy",
        params={},
        bars=260,
        candles_df=_price_frame(),
        trade_mode="both",
        persist_legacy_run=False,
    )

    assert not result.get("error")
    assert result["trade_mode"] == "both"
    assert result["position_model"] == "hedged"
    assert result["metrics"]["trade_mode"] == "both"
    assert result["metrics"]["by_side"]["long"]["total_trades"] >= 1
    assert result["metrics"]["by_side"]["short"]["total_trades"] >= 1
    assert {trade["direction"] for trade in result["trades"]} == {"long", "short"}


def test_backtest_strategy_persists_full_config_for_legacy_history_rows(forven_db, monkeypatch):
    _patch_backtest_environment(monkeypatch)
    monkeypatch.setattr(
        backtest_mod,
        "_resolve_strategy_class",
        lambda strategy_type: _BothSidesStrategy if strategy_type == "both_dummy" else None,
    )

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-HISTORY",
                "History Strategy",
                "both_dummy",
                "BTC/USDT",
                "1h",
                "{}",
                "{}",
                "quick_screen",
                "test",
                "quick_screen",
            ),
        )

    result = backtest_mod.backtest_strategy(
        strategy_id="S-HISTORY",
        asset="BTC/USDT",
        strategy_type="both_dummy",
        params={"custom_threshold": 7, "timeframe": "1h"},
        bars=260,
        candles_df=_price_frame(),
        trade_mode="both",
        persist_legacy_run=True,
    )

    run_id = str(result.get("run_id") or "").strip()
    assert run_id.startswith("B")

    with get_db() as conn:
        row = conn.execute(
            "SELECT start_date, end_date, config_json FROM backtest_results WHERE result_id = ?",
            (run_id,),
        ).fetchone()

    assert row is not None
    assert str(row["start_date"] or "").strip()
    assert str(row["end_date"] or "").strip()

    config = json.loads(row["config_json"] or "{}")
    assert config.get("params", {}).get("custom_threshold") == 7
    assert config.get("trade_mode") == "both"
    assert config.get("position_model") == "hedged"


def test_resolve_backtest_trade_mode_falls_back_to_short_only_for_mirror_safe_strategy():
    resolved_mode, error = backtest_mod.resolve_backtest_trade_mode(
        None,
        allow_shorting=True,
        strategy_type="mirror_short_dummy",
        params={},
        strategy_obj=_MirrorShortStrategy("S-MIRROR", {}),
    )

    assert error is None
    assert resolved_mode == "short_only"


def test_resolve_backtest_trade_mode_honors_imported_strategy_declared_mode():
    """An imported (sandbox-only) strategy's real class — and its declared
    ``supported_trade_modes`` — is never imported into the trusted parent, so the
    parent must derive support from the validated stored params. A dual-side imported
    strategy declaring ``trade_mode='both'`` must not be rejected as unsupported,
    whether or not a proxy ``strategy_obj`` is passed (regression: imported strategy
    backtest failed with "does not support trade_mode='both'")."""
    from forven.strategies.sandbox_proxy import SandboxOnlyStrategy

    runtime_type = "imported__btc_persistbrkregime_s195444"
    params = {"_asset": "BTC", "trade_mode": "both"}
    proxy = SandboxOnlyStrategy("S05074", params, runtime_type=runtime_type)

    assert proxy.supported_trade_modes == {"long_only", "both"}

    # Proxy-passed path (backtest / scanner build the proxy as the probe).
    resolved_mode, error = backtest_mod.resolve_backtest_trade_mode(
        "both", strategy_type=runtime_type, params=params, strategy_obj=proxy,
    )
    assert error is None
    assert resolved_mode == "both"

    # Type+params-only path (no proxy) must resolve identically.
    resolved_mode, error = backtest_mod.resolve_backtest_trade_mode(
        "both", strategy_type=runtime_type, params=params, strategy_obj=None,
    )
    assert error is None
    assert resolved_mode == "both"


def test_resolve_backtest_trade_mode_keeps_strict_guard_for_first_party_strategy():
    """The sandbox-only relaxation must NOT leak to first-party strategies the parent
    CAN introspect: forcing trade_mode='both' on a long-only type still errors."""
    resolved_mode, error = backtest_mod.resolve_backtest_trade_mode(
        "both",
        strategy_type="macd",
        params={"trade_mode": "both"},
        strategy_obj=None,
    )
    assert resolved_mode == "both"
    assert error is not None
    assert "does not support trade_mode='both'" in error


# --------------------------------------------------------------- TRADE-MODE-2
# An explicit trade_mode in the strategy's OWN declared default_params is an
# author declaration — honored even when the class omitted the
# supported_trade_modes attribute (the template never mentioned it, so
# CRUX-1-style dual-side classes were structurally unrunnable).


class _DeclaredBothStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "Declared Both"

    @property
    def asset(self) -> str:
        return "BTC"

    @property
    def strategy_type(self) -> str:
        return "declared_both_dummy"

    @property
    def default_params(self) -> dict:
        return {"trade_mode": "both", "lookback": 20}

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        return Signal(price=float(df["close"].iloc[-1]), direction="long")


class _UndeclaredLongOnlyStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "Undeclared Long Only"

    @property
    def asset(self) -> str:
        return "BTC"

    @property
    def strategy_type(self) -> str:
        return "undeclared_long_only_dummy"

    @property
    def default_params(self) -> dict:
        return {"lookback": 20}

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        return Signal(price=float(df["close"].iloc[-1]), direction="long")


def test_declared_default_trade_mode_both_is_supported():
    obj = _DeclaredBothStrategy("s-declared", {})
    mode, err = backtest_mod.resolve_backtest_trade_mode(
        "both", strategy_type="declared_both_dummy", params={"trade_mode": "both"}, strategy_obj=obj,
    )
    assert err is None, err
    assert mode == "both"
    # A dual-side author's sides are individually runnable (lane-split path).
    mode, err = backtest_mod.resolve_backtest_trade_mode(
        "short_only", strategy_type="declared_both_dummy", params={}, strategy_obj=obj,
    )
    assert err is None, err
    assert mode == "short_only"


def test_declared_default_resolves_via_registry_without_obj(monkeypatch):
    from forven.strategies import registry

    monkeypatch.setitem(registry._TYPE_MAP, "declared_both_dummy", _DeclaredBothStrategy)
    monkeypatch.setattr(registry, "discover", lambda *a, **k: None)
    mode, err = backtest_mod.resolve_backtest_trade_mode(
        "both", strategy_type="declared_both_dummy", params={"trade_mode": "both"},
    )
    assert err is None, err
    assert mode == "both"


def test_request_override_on_undeclared_class_still_rejected():
    # The strict guard is unchanged: a caller merely REQUESTING 'both' on a
    # class that declares neither supported_trade_modes nor a default
    # trade_mode is still refused.
    obj = _UndeclaredLongOnlyStrategy("s-undeclared", {})
    mode, err = backtest_mod.resolve_backtest_trade_mode(
        "both", strategy_type="undeclared_long_only_dummy", params={"trade_mode": "both"}, strategy_obj=obj,
    )
    assert err is not None
    assert "does not support trade_mode='both'" in err


def test_param_derived_both_on_long_only_clamps_instead_of_erroring():
    # TRADE-MODE-3: when 'both' is NOT an explicit override but comes from the
    # strategy's own params (requested_trade_mode=None) on a long-only archetype
    # that can't run it — the crucible/validation path for a macd/williams_r with
    # trade_mode='both' stamped in — clamp to the runnable long side and evaluate
    # on merit, rather than erroring (which archived the strategy). Contrast with
    # test_request_override_on_undeclared_class_still_rejected: an EXPLICIT 'both'
    # override on the same class still errors.
    obj = _UndeclaredLongOnlyStrategy("s-undeclared", {})
    mode, err = backtest_mod.resolve_backtest_trade_mode(
        None, strategy_type="undeclared_long_only_dummy", params={"trade_mode": "both"}, strategy_obj=obj,
    )
    assert err is None, err
    assert mode == "long_only"
