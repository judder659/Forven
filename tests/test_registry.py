from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

import forven.strategies.registry as registry_mod
from forven.db import get_db
from forven.strategies.base import BaseStrategy, Signal
from forven.strategies.catalog import get_prebuilt_catalog
from forven.strategies.custom_catalog import custom_strategy_status


class DummyStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "Dummy Strategy"

    @property
    def asset(self) -> str:
        return str(self.params.get("_asset") or "BTC")

    @property
    def strategy_type(self) -> str:
        return "dummy"

    @property
    def default_params(self) -> dict:
        return {"fast": 12, "slow": 26, "signal": 9}

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        price = float(df["close"].iloc[-1]) if not df.empty else 0.0
        return Signal(price=price)


class BadCtorStrategy(BaseStrategy):
    def __init__(self, sentinel: str):
        self.strategy_id = sentinel
        self.params = {}

    @property
    def name(self) -> str:
        return "Bad Ctor Strategy"

    @property
    def asset(self) -> str:
        return "BTC"

    @property
    def strategy_type(self) -> str:
        return "bad_ctor"

    @property
    def default_params(self) -> dict:
        return {}

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        price = float(df["close"].iloc[-1]) if not df.empty else 0.0
        return Signal(price=price)


class AbstractStrategy(BaseStrategy):
    @property
    def strategy_type(self) -> str:
        return "abstract"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        price = float(df["close"].iloc[-1]) if not df.empty else 0.0
        return Signal(price=price)


def _insert_strategy_row(
    strategy_id: str,
    *,
    strategy_type: str,
    params: str,
    owner: str = "risk-manager",
    runtime_type: str | None = None,
    status: str = "paper",
    stage: str = "paper",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, runtime_type, symbol, timeframe, params, status, owner, stage, stage_changed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                strategy_id,
                strategy_type,
                runtime_type,
                "BTC",
                "1h",
                params,
                status,
                owner,
                stage,
                now,
                now,
                now,
            ),
        )


def test_base_strategy_param_accessor_supports_generated_code_conventions():
    strategy = DummyStrategy("S-P", {"fast": 8, "threshold": 1.5})

    assert strategy.p["fast"] == 8
    assert strategy.p.fast == 8
    assert strategy.p("threshold", 0) == 1.5
    assert strategy.p("missing", 42) == 42
    assert strategy.p.get("slow") == 26


def test_base_strategy_accepts_callable_default_params_from_generated_code():
    class CallableDefaultsStrategy(BaseStrategy):
        name = "Callable Defaults"
        asset = "BTC"
        strategy_type = "callable_defaults"

        @staticmethod
        def default_params() -> dict:
            return {"fast": 12, "slow": 26}

        def generate_signal(self, df: pd.DataFrame) -> Signal:
            return Signal(price=float(df["close"].iloc[-1]) if not df.empty else 0.0)

    strategy = CallableDefaultsStrategy("S-CALLABLE", {"fast": 8})

    assert strategy.params == {"fast": 8, "slow": 26}
    assert strategy.p.fast == 8


def test_signal_from_condition_collapses_series_to_latest_bar():
    index = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    df = pd.DataFrame({"close": [100.0, 101.0, 102.0]}, index=index)

    signal = Signal.from_condition(
        pd.Series([False, False, True], index=index),
        pd.Series([False, True, False], index=index),
        df,
        confidence=pd.Series([0.1, 0.2, 0.9], index=index),
    )

    assert signal.entry_signal is True
    assert signal.exit_signal is False
    assert signal.price == 102.0
    assert signal.confidence == 0.9


def test_discover_skips_bad_builtin_module_and_continues(monkeypatch):
    registry_mod.reset()

    def _fake_iter_modules(_path):
        return [
            (None, "bad_builtin", False),
            (None, "good_builtin", False),
        ]

    def _fake_load_builtin(modname: str) -> None:
        if modname == "bad_builtin":
            raise ImportError("broken duplicate")
        registry_mod.register_type("good_builtin", DummyStrategy)

    monkeypatch.setattr(registry_mod.pkgutil, "iter_modules", _fake_iter_modules)
    monkeypatch.setattr(registry_mod, "_load_builtin_strategy_module", _fake_load_builtin)

    registry_mod.discover(include_custom=False)

    assert "good_builtin" in registry_mod._TYPE_MAP


def test_get_active_skips_bad_row_and_loads_non_execution_owner(forven_db):
    registry_mod.reset()
    registry_mod.register_type("macd", DummyStrategy)

    _insert_strategy_row(
        "S-GOOD",
        strategy_type="macd",
        params=json.dumps({"fast_period": 5, "slow_period": 13, "signal_period": 3}),
        owner="risk-manager",
    )
    _insert_strategy_row(
        "S-BAD",
        strategy_type="macd",
        params="{bad json",
        owner="execution-trader",
    )

    active = registry_mod.get_active()

    assert "S-GOOD" in active
    assert "S-BAD" not in active
    assert active["S-GOOD"].params["fast"] == 5
    assert active["S-GOOD"].params["slow"] == 13
    assert active["S-GOOD"].params["signal"] == 3
    assert getattr(active["S-GOOD"], "runtime_type") == "macd"

    with get_db() as conn:
        row = conn.execute(
            "SELECT runtime_type FROM strategies WHERE id = ?",
            ("S-GOOD",),
        ).fetchone()
    assert row["runtime_type"] == "macd"


def test_get_active_backfills_runtime_type_from_unique_prefix_match(forven_db):
    registry_mod.reset()
    registry_mod.register_type("bb_fade_s00194", DummyStrategy)

    _insert_strategy_row(
        "S-CUSTOM",
        strategy_type="bb_fade",
        params=json.dumps({}),
        owner="risk-manager",
        runtime_type=None,
    )

    active = registry_mod.get_active()

    assert "S-CUSTOM" in active
    assert getattr(active["S-CUSTOM"], "runtime_type") == "bb_fade_s00194"

    with get_db() as conn:
        row = conn.execute(
            "SELECT runtime_type FROM strategies WHERE id = ?",
            ("S-CUSTOM",),
        ).fetchone()
    assert row["runtime_type"] == "bb_fade_s00194"


def test_resolve_runtime_type_quarantines_ambiguous_prefix_match():
    registry_mod.reset()
    registry_mod.register_type("bb_fade_s00194", DummyStrategy)
    registry_mod.register_type("bb_fade_s00200", DummyStrategy)

    resolved, meta = registry_mod.resolve_runtime_type("bb_fade")

    assert resolved is None
    assert "ambiguous" in str(meta.get("blocked_reason") or "").lower()


def test_register_type_keeps_existing_mapping_when_override_is_abstract():
    registry_mod.reset()
    registry_mod.register_type("stochastic", DummyStrategy)

    registry_mod.register_type("stochastic", AbstractStrategy)

    assert registry_mod._TYPE_MAP["stochastic"] is DummyStrategy


def test_register_type_rejects_incompatible_constructor():
    registry_mod.reset()

    registry_mod.register_type("bad_ctor", BadCtorStrategy)

    assert "bad_ctor" not in registry_mod._TYPE_MAP


def test_custom_catalog_marks_versioned_modules_archived():
    assert custom_strategy_status("rsi_momentum") == "active"
    assert custom_strategy_status("rsi_momentum_s00293") == "archived"
    assert custom_strategy_status("S00224_bollinger") == "archived"
    assert custom_strategy_status("_probe_signal_fields") == "ignored"


def test_discover_ignores_private_custom_modules(monkeypatch):
    registry_mod.reset()
    loaded: list[str] = []

    monkeypatch.setattr(registry_mod, "_builtin_discovered", True)
    monkeypatch.setattr(
        registry_mod.pkgutil,
        "iter_modules",
        lambda _path: [(None, "_private_probe", False), (None, "public_strategy", False)],
    )
    monkeypatch.setattr(registry_mod, "_load_custom_strategy_module", loaded.append)
    monkeypatch.setattr(registry_mod, "_ensure_active_db_strategy_modules", lambda: None)

    registry_mod.discover()

    assert loaded == ["public_strategy"]


def test_discover_quarantines_custom_system_exit(monkeypatch):
    registry_mod.reset()
    loaded: list[str] = []

    def _load(modname: str) -> None:
        loaded.append(modname)
        if modname == "bad_strategy":
            raise SystemExit(0)

    monkeypatch.setattr(registry_mod, "_builtin_discovered", True)
    monkeypatch.setattr(
        registry_mod.pkgutil,
        "iter_modules",
        lambda _path: [(None, "bad_strategy", False), (None, "good_strategy", False)],
    )
    monkeypatch.setattr(registry_mod, "_load_custom_strategy_module", _load)
    monkeypatch.setattr(registry_mod, "_ensure_active_db_strategy_modules", lambda: None)

    registry_mod.discover()

    assert loaded == ["bad_strategy", "good_strategy"]
    assert "bad_strategy" in registry_mod._FAILED_CUSTOM_MODULES


def test_prebuilt_catalog_skips_strategy_constructor_system_exit(monkeypatch):
    class ExitStrategy:
        def __init__(self, *_args, **_kwargs) -> None:
            raise SystemExit(0)

    ExitStrategy.__module__ = "forven.strategies.builtin.test_exit"
    monkeypatch.setattr(registry_mod, "discover", lambda **_kwargs: None)
    monkeypatch.setattr(registry_mod, "_TYPE_MAP", {"exit_strategy": ExitStrategy})

    assert get_prebuilt_catalog() == []


def test_prebuilt_catalog_excludes_custom_registry_types(monkeypatch):
    class CustomStrategy:
        pass

    CustomStrategy.__module__ = "forven.strategies.custom.generated"
    monkeypatch.setattr(registry_mod, "discover", lambda **_kwargs: None)
    monkeypatch.setattr(registry_mod, "_TYPE_MAP", {"custom_strategy": CustomStrategy})

    assert get_prebuilt_catalog() == []


def test_resolve_runtime_type_loads_archived_custom_module_by_exact_name():
    registry_mod.reset()
    registry_mod.discover()

    resolved, meta = registry_mod.resolve_runtime_type("bb_fade_s00194")

    assert resolved == "bb_fade_s00194"
    assert meta["blocked_reason"] is None


def test_prebuilt_catalog_includes_eth_15m_ema_template_without_live_instance():
    registry_mod.reset()

    catalog = get_prebuilt_catalog()
    entries = {entry["type"]: entry for entry in catalog}

    assert "ema_cross_eth_15m" in entries
    assert entries["ema_cross_eth_15m"]["parameters"]["ema_fast"]["default"] == 24
    assert entries["ema_cross_eth_15m"]["parameters"]["ema_slow"]["default"] == 32
    assert entries["ema_cross_eth_15m"]["parameters"]["ema_regime"]["default"] == 192
    assert entries["ema_cross_eth_15m"]["parameters"]["adx_min"]["default"] == 25
    assert all(
        getattr(strategy, "strategy_type", "") != "ema_cross_eth_15m"
        for strategy in registry_mod.get_all().values()
    )


def test_prebuilt_catalog_includes_eth_15m_vwap_pullback_template_without_live_instance():
    registry_mod.reset()

    catalog = get_prebuilt_catalog()
    entries = {entry["type"]: entry for entry in catalog}

    assert "vwap_pullback_eth_15m" in entries
    assert entries["vwap_pullback_eth_15m"]["parameters"]["vwap_period"]["default"] == 32
    assert entries["vwap_pullback_eth_15m"]["parameters"]["distance_pct"]["default"] == 0.015
    assert entries["vwap_pullback_eth_15m"]["parameters"]["ema_regime"]["default"] == 96
    assert entries["vwap_pullback_eth_15m"]["parameters"]["rsi_entry"]["default"] == 35
    assert entries["vwap_pullback_eth_15m"]["parameters"]["rsi_exit"]["default"] == 60
    assert all(
        getattr(strategy, "strategy_type", "") != "vwap_pullback_eth_15m"
        for strategy in registry_mod.get_all().values()
    )


# ── get_active() hydration cache ─────────────────────────────────────────────
# Hydration used to re-run per call (per strategy per scan) — the GIL-held
# sweeps behind the "event loop stalled" DEGRADED storms.


def test_get_active_caches_hydration_within_ttl(forven_db, monkeypatch):
    registry_mod.reset()
    registry_mod.register_type("macd", DummyStrategy)
    _insert_strategy_row(
        "S-CACHED",
        strategy_type="macd",
        params=json.dumps({"fast_period": 5}),
    )

    hydrations = []
    real_load = registry_mod._load_db_strategies

    def _counting_load(target):
        hydrations.append(1)
        real_load(target)

    monkeypatch.setattr(registry_mod, "_load_db_strategies", _counting_load)

    first = registry_mod.get_active()
    second = registry_mod.get_active()

    assert "S-CACHED" in first
    assert "S-CACHED" in second
    assert len(hydrations) == 1


def test_get_active_rehydrates_when_rows_change_after_ttl(forven_db):
    registry_mod.reset()
    registry_mod.register_type("macd", DummyStrategy)
    _insert_strategy_row("S-FIRST", strategy_type="macd", params=json.dumps({}))

    assert "S-FIRST" in registry_mod.get_active()

    _insert_strategy_row("S-SECOND", strategy_type="macd", params=json.dumps({}))
    # Expire the TTL without sleeping: rewind the cache stamp.
    hydrated, sig, stamp = registry_mod._ACTIVE_CACHE
    registry_mod._ACTIVE_CACHE = (
        hydrated,
        sig,
        stamp - registry_mod._ACTIVE_CACHE_TTL_SECONDS - 1,
    )

    active = registry_mod.get_active()

    assert "S-FIRST" in active
    assert "S-SECOND" in active


def test_get_active_skips_rehydrate_when_signature_unchanged(forven_db, monkeypatch):
    registry_mod.reset()
    registry_mod.register_type("macd", DummyStrategy)
    _insert_strategy_row("S-STABLE", strategy_type="macd", params=json.dumps({}))

    assert "S-STABLE" in registry_mod.get_active()

    hydrated, sig, stamp = registry_mod._ACTIVE_CACHE
    registry_mod._ACTIVE_CACHE = (
        hydrated,
        sig,
        stamp - registry_mod._ACTIVE_CACHE_TTL_SECONDS - 1,
    )

    def _fail_load(target):
        raise AssertionError("unchanged rows must not re-hydrate")

    monkeypatch.setattr(registry_mod, "_load_db_strategies", _fail_load)

    assert "S-STABLE" in registry_mod.get_active()


def test_get_active_serves_stale_cache_when_signature_probe_fails(forven_db, monkeypatch):
    registry_mod.reset()
    registry_mod.register_type("macd", DummyStrategy)
    _insert_strategy_row("S-STALE-OK", strategy_type="macd", params=json.dumps({}))

    assert "S-STALE-OK" in registry_mod.get_active()

    hydrated, sig, stamp = registry_mod._ACTIVE_CACHE
    registry_mod._ACTIVE_CACHE = (
        hydrated,
        sig,
        stamp - registry_mod._ACTIVE_CACHE_TTL_SECONDS - 1,
    )
    monkeypatch.setattr(registry_mod, "_active_rows_signature", lambda: None)

    def _fail_load(target):
        raise AssertionError("probe failure must serve the stale cache, not hydrate")

    monkeypatch.setattr(registry_mod, "_load_db_strategies", _fail_load)

    assert "S-STALE-OK" in registry_mod.get_active()


def test_load_archived_custom_runtime_type_negative_caches_broken_module(monkeypatch):
    registry_mod.reset()
    registry_mod._ARCHIVED_CUSTOM_MODULES["broken_type_s99999"] = "broken_type_s99999"

    attempts = []

    def _always_fails(modname):
        attempts.append(modname)
        raise ImportError("rejected by the AST security guard")

    monkeypatch.setattr(registry_mod, "_load_custom_strategy_module", _always_fails)

    assert registry_mod._load_archived_custom_runtime_type("broken_type_s99999") is False
    assert registry_mod._load_archived_custom_runtime_type("broken_type_s99999") is False
    assert len(attempts) == 1
    assert "broken_type_s99999" in registry_mod._FAILED_CUSTOM_MODULES
