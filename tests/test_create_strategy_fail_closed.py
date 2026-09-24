"""Strategy-creation fail-closed hardening (PHANTOM-1 / SYMBOL-1 / PARAMS-1 / NAMESPACE-1).

Four fail-open gaps let the pipeline mint garbage:
- phantom types certified without any runtime implementation (S05847
  soad_shock_reversal; class-less families vwap/regime_filtered);
- fabricated symbols silently rerouted to BTC/USDT while the API echoed the
  original (S05970 HIP3-WRPROXYPLACEHOLDER) — defeating substrate-gated
  hypotheses;
- custom types persisted with empty params (S06100) — unevaluable containers
  occupying quick_screen slots;
- a rejected sibling permanently consuming a carrier class name (S06020).
"""

from __future__ import annotations

import pytest

from forven.db import create_strategy_container, get_db, normalize_strategy_symbol_strict
from forven.strategies.certification import certify_execution_strategy
from forven.strategies.params import is_known_runtime_type


# ------------------------------------------------------------ (a) phantom types


def test_made_up_type_is_not_a_runtime_class():
    # (the original S05847 phantom type 'soad_shock_reversal' has since been
    # implemented in custom/, so use a name that provably has no class)
    assert not is_known_runtime_type("xqzzy_phantom_reversal", require_runtime_class=True)


def test_classless_family_fails_certification_mode_but_not_orphan_mode():
    # 'vwap' has param-vocabulary support but no registered runtime class:
    # certification must refuse it; the orphan scanner must keep its lenient
    # family semantics (existing rows aren't mass-orphaned).
    assert not is_known_runtime_type("vwap", require_runtime_class=True)
    assert is_known_runtime_type("vwap")


def test_real_class_passes_certification_mode():
    assert is_known_runtime_type("rsi_momentum", require_runtime_class=True)


def test_certification_mode_fails_closed_on_registry_error(monkeypatch):
    import forven.strategies.registry as registry

    def _boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(registry, "discover", _boom)
    assert not is_known_runtime_type("rsi_momentum", require_runtime_class=True)
    # Orphan mode keeps failing OPEN so a registry hiccup never mass-orphans.
    assert is_known_runtime_type("some_random_type")


def test_certify_rejects_phantom_type():
    cert = certify_execution_strategy("xqzzy_phantom_reversal", {"x": 1})
    assert cert.unregistered_runtime_type
    assert not cert.certified
    assert "no runtime class registered" in (cert.primary_blocking_reason() or "")


def test_certify_rejects_classless_family():
    cert = certify_execution_strategy("vwap", {"band_pct": 0.5})
    assert cert.unregistered_runtime_type
    assert not cert.certified


def test_certify_accepts_registered_class():
    cert = certify_execution_strategy("rsi_momentum", {"rsi_period": 14})
    assert not cert.unregistered_runtime_type


# ------------------------------------------------------------- (b) symbol guard


def test_strict_normalizer_rejects_fabricated_symbol():
    assert normalize_strategy_symbol_strict("HIP3-WRPROXYPLACEHOLDER") is None


def test_strict_normalizer_repairs_legitimate_forms():
    assert normalize_strategy_symbol_strict("BTC") == "BTC/USDT"
    assert normalize_strategy_symbol_strict("eth/usdt") == "ETH/USDT"
    assert normalize_strategy_symbol_strict("SOL-USDT") == "SOL/USDT"
    assert normalize_strategy_symbol_strict("ETH/USDT_15M") == "ETH/USDT"


def test_container_rejects_explicit_garbage_symbol(forven_db):
    with get_db() as conn:
        with pytest.raises(ValueError, match="unknown symbol"):
            create_strategy_container(
                conn=conn,
                name="x",
                type_="rsi_momentum",
                symbol="HIP3-WRPROXYPLACEHOLDER",
                timeframe="1h",
                params={"rsi_period": 14},
            )
    # Nothing was persisted.
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    assert n == 0


def test_container_keeps_legacy_default_for_omitted_symbol(forven_db):
    with get_db() as conn:
        sid, _d, _b = create_strategy_container(
            conn=conn, name="x", type_="rsi_momentum", symbol="",
            timeframe="1h", params={"rsi_period": 14},
        )
        row = conn.execute("SELECT symbol FROM strategies WHERE id = ?", (sid,)).fetchone()
    assert row["symbol"] == "BTC/USDT"


# ----------------------------------------------------- (b)+(c) route behaviour


def _mint_hypothesis():
    from forven.hypotheses import create_hypothesis

    return create_hypothesis(
        title="fail-closed regression",
        market_thesis="t",
        mechanism="m",
        lane="benchmarking",
        source_type="operator_seed",
        target_assets=["BTC/USDT"],
        target_timeframes=["1h"],
    )["id"]


def test_route_422_on_unknown_symbol(forven_db):
    from fastapi import HTTPException

    from forven.routers.backtesting import create_backtesting_strategy

    hyp = _mint_hypothesis()
    with pytest.raises(HTTPException) as exc_info:
        create_backtesting_strategy(
            name="x", type="rsi_momentum", symbol="HIP3-WRPROXYPLACEHOLDER",
            timeframe="1h", body={"params": {"rsi_period": 14}, "hypothesis_id": hyp},
        )
    assert exc_info.value.status_code == 422
    assert "unknown_symbol" in str(exc_info.value.detail)


def test_route_422_on_phantom_type(forven_db):
    from fastapi import HTTPException

    from forven.routers.backtesting import create_backtesting_strategy

    hyp = _mint_hypothesis()
    with pytest.raises(HTTPException) as exc_info:
        create_backtesting_strategy(
            name="x", type="backtest", symbol="BTC/USDT", timeframe="4h",
            body={"type": "xqzzy_phantom_reversal", "params": {"z": 1}, "hypothesis_id": hyp},
        )
    assert exc_info.value.status_code == 422
    assert "no runtime class registered" in str(exc_info.value.detail)


def test_route_422_on_empty_params_for_custom_type(forven_db, monkeypatch):
    from fastapi import HTTPException

    from forven.routers.backtesting import create_backtesting_strategy

    # Simulate a registered custom class so certification passes but params are
    # empty (the S06100 shape: novel type, params={}).
    import forven.strategies.params as params_mod

    real = params_mod.is_known_runtime_type

    def _fake(strategy_type, **kw):
        if str(strategy_type) == "cross_asset_basis_momentum_mr":
            return True
        return real(strategy_type, **kw)

    import forven.strategies.certification as cert_mod

    monkeypatch.setattr(cert_mod, "is_known_runtime_type", _fake)
    hyp = _mint_hypothesis()
    with pytest.raises(HTTPException) as exc_info:
        create_backtesting_strategy(
            name="x", type="backtest", symbol="SOL/USDT", timeframe="1h",
            body={"type": "cross_asset_basis_momentum_mr", "params": {}, "hypothesis_id": hyp},
        )
    assert exc_info.value.status_code == 422
    assert "empty_params" in str(exc_info.value.detail)


def test_route_allows_empty_params_for_certified_family(forven_db):
    from forven.routers.backtesting import create_backtesting_strategy

    hyp = _mint_hypothesis()
    response = create_backtesting_strategy(
        name="bare ema", type="backtest", symbol="BTC/USDT", timeframe="1h",
        body={"type": "ema_cross", "params": {}, "hypothesis_id": hyp},
    )
    assert response["ok"] is True


def test_route_archives_as_untestable_when_data_availability_is_unknown(forven_db, monkeypatch):
    from forven.routers.backtesting import create_backtesting_strategy
    from forven.strategies import data_availability

    monkeypatch.setattr(
        data_availability,
        "evaluate_data_availability",
        lambda *_args, **_kwargs: data_availability.DataAvailabilityResult(
            ok=False,
            blocked=True,
            error="feed catalog unavailable",
        ),
    )

    response = create_backtesting_strategy(
        name="catalog guard",
        type="ema_cross",
        symbol="BTC/USDT",
        timeframe="1h",
        body={"type": "ema_cross", "params": {}, "hypothesis_id": _mint_hypothesis()},
    )

    assert response["status"] == "archived"
    with get_db() as conn:
        row = conn.execute(
            "SELECT notes, status_reason FROM strategies WHERE id = ?",
            (response["strategy_id"],),
        ).fetchone()
    assert str(row["status_reason"]).startswith("untestable:no_data:")
    assert "feed catalog unavailable" in str(row["status_reason"])
    assert "feed catalog unavailable" in str(row["notes"])


def test_route_response_symbol_matches_stored_row(forven_db):
    from forven.routers.backtesting import create_backtesting_strategy

    hyp = _mint_hypothesis()
    response = create_backtesting_strategy(
        name="x", type="rsi_momentum", symbol="eth",  # repairs to ETH/USDT
        timeframe="1h", body={"params": {"rsi_period": 14}, "hypothesis_id": hyp},
    )
    with get_db() as conn:
        row = conn.execute(
            "SELECT symbol FROM strategies WHERE id = ?", (response["strategy_id"],)
        ).fetchone()
    assert response["symbol"] == row["symbol"] == "ETH/USDT"


# -------------------------------------------------------- (d) namespace release


def _insert_holder(strategy_id: str, type_name: str, stage: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, runtime_type, symbol, timeframe, params, "
            "status, stage, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'ETH/USDT', '1h', '{}', ?, ?, datetime('now'), datetime('now'))",
            (strategy_id, strategy_id, type_name, type_name, stage, stage),
        )


def test_rejected_sibling_releases_carrier_name(forven_db):
    from forven.strategies.intake import _find_existing_strategy_container

    _insert_holder("S-OLD", "microprice_drift_lead", "rejected")
    assert _find_existing_strategy_container(
        type_name="microprice_drift_lead", ignore_terminal=True
    ) is None
    # Default (bulk-sweep) semantics still see it as known.
    hit = _find_existing_strategy_container(type_name="microprice_drift_lead")
    assert hit and hit["id"] == "S-OLD"


def test_active_sibling_still_blocks(forven_db):
    from forven.strategies.intake import _find_existing_strategy_container

    _insert_holder("S-ACTIVE", "microprice_drift_lead", "quick_screen")
    hit = _find_existing_strategy_container(
        type_name="microprice_drift_lead", ignore_terminal=True
    )
    assert hit and hit["id"] == "S-ACTIVE"


# ------------------------------------------------- client param/blob merging


def test_client_merges_rule_blobs_into_params():
    from forven.backtesting import BacktestingClient

    captured = {}

    class _FakeHTTP:
        def post(self, path, json=None):
            captured["path"] = path
            captured["payload"] = json

            class _R:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"ok": True}

            return _R()

    client = BacktestingClient.__new__(BacktestingClient)
    client._client = _FakeHTTP()
    client.create_strategy(
        name="x", type="composite", hypothesis_id="HYP-1",
        indicators=[{"kind": "rsi"}], entry_conditions=[{"op": "gt"}],
        params={"rsi_period": 14},
    )
    payload = captured["payload"]
    assert payload["params"]["rsi_period"] == 14
    assert payload["params"]["indicators"] == [{"kind": "rsi"}]
    assert payload["params"]["entry_conditions"] == [{"op": "gt"}]


def test_agent_family_match_no_longer_substring():
    from forven.agents.tools_backtesting import _is_certified_strategy_family

    # Novel composites that merely CONTAIN a family token are custom, not family.
    assert not _is_certified_strategy_family("taker_ema_cross_inflection_v2")
    assert not _is_certified_strategy_family("cross_asset_basis_momentum_mr")
    # Real families (exact and prefixed) still match.
    assert _is_certified_strategy_family("ema_cross")
    assert _is_certified_strategy_family("rsi_momentum")
