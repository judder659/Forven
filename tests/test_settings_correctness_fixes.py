"""Settings-correctness regressions from the 2026-06-09 full audit.

Four trust leads: every knob on the Settings page must display the value the
backend actually enforces, and a save must never silently corrupt or reset
sibling settings.

- B-11: ``max_drawdown_pct`` exists in BOTH the main blob (risk kill-switch,
  enforced) and the flat pipeline payload (legacy promotion threshold). The
  pipeline overlay in ``get_settings`` must not shadow the enforced blob value.
- B-13: risk enforcement reads ``max_risk_per_trade_pct`` / ``max_daily_loss_pct``
  (always seeded), so the risk section must write those keys — and keep the
  legacy twins (``max_position_size_pct`` / ``max_daily_loss``) in sync so no
  write path is a placebo.
- B-23: clearing a numeric pipeline field must NOT persist null into the
  promotion-gate config (a stored null crashes gate evaluation later).
- B-12: a partial nested data-engine save must deep-merge over STORED values,
  never reset un-edited siblings (e.g. ``source_reconciliation.enabled``) back
  to defaults.
"""

import pytest
from fastapi import HTTPException

from forven.api_core import (
    PipelineSettingsUpdateBody,
    _load_settings_payload,
    get_pipeline_settings,
    get_settings,
    put_pipeline_settings,
    put_settings_section,
)


# ---------------------------------------------------------------------------
# B-11: max_drawdown_pct key collision (blob kill-switch vs pipeline threshold)
# ---------------------------------------------------------------------------


class TestMaxDrawdownKeyCollision:
    def test_settings_shows_blob_kill_switch_not_pipeline_threshold(self, forven_db):
        # Defaults already collide: blob 30 (enforced) vs pipeline 40 (legacy).
        assert get_pipeline_settings()["max_drawdown_pct"] == 40
        assert get_settings()["max_drawdown_pct"] == 30

    def test_risk_edit_sticks_across_reload(self, forven_db):
        put_settings_section("risk", {"max_drawdown_pct": 12})
        # The operator must see the edited (enforced) value, not the pipeline 40.
        assert get_settings()["max_drawdown_pct"] == 12
        # And the value enforcement reads (the raw blob) matches the display.
        assert _load_settings_payload()["max_drawdown_pct"] == 12

    def test_pipeline_twin_still_reachable_via_pipeline_endpoint(self, forven_db):
        put_settings_section("risk", {"max_drawdown_pct": 12})
        # The legacy pipeline promotion threshold keeps its own value and route.
        assert get_pipeline_settings()["max_drawdown_pct"] == 40

    def test_enforcement_reads_the_displayed_value(self, forven_db):
        put_settings_section("risk", {"max_drawdown_pct": 12})
        from forven.exchange.risk import _get_risk_limits

        assert _get_risk_limits()["max_drawdown"] == pytest.approx(0.12)

    def test_non_colliding_pipeline_keys_still_overlay(self, forven_db):
        # The shadow exclusion is surgical: other flat pipeline keys (which have
        # no blob twin) must keep flowing into get_settings for the UI.
        assert get_settings()["autopilot_enabled"] is True


# ---------------------------------------------------------------------------
# B-13: risk twins — displayed fields must be the keys enforcement reads
# ---------------------------------------------------------------------------


class TestRiskTwinKeys:
    def test_max_risk_per_trade_pct_write_path_exists_and_round_trips(self, forven_db):
        put_settings_section("risk", {"max_risk_per_trade_pct": 7})
        payload = _load_settings_payload()
        assert payload["max_risk_per_trade_pct"] == 7
        # Legacy twin synced so older readers (brain/bot summaries) agree.
        assert payload["max_position_size_pct"] == 7

    def test_max_risk_per_trade_pct_reaches_enforcement(self, forven_db):
        put_settings_section("risk", {"max_risk_per_trade_pct": 7})
        from forven.exchange.risk import _get_risk_limits

        assert _get_risk_limits()["max_risk_per_trade"] == pytest.approx(0.07)

    def test_legacy_max_position_size_pct_write_is_not_a_placebo(self, forven_db):
        # Enforcement prefers max_risk_per_trade_pct (always seeded), so a write
        # to the legacy key must sync the preferred key to take effect.
        put_settings_section("risk", {"max_position_size_pct": 4})
        payload = _load_settings_payload()
        assert payload["max_position_size_pct"] == 4
        assert payload["max_risk_per_trade_pct"] == 4
        from forven.exchange.risk import _get_risk_limits

        assert _get_risk_limits()["max_risk_per_trade"] == pytest.approx(0.04)

    def test_max_daily_loss_pct_write_path_exists_and_derives_usd_twin(self, forven_db):
        put_settings_section("risk", {"max_daily_loss_pct": 3})
        payload = _load_settings_payload()
        assert payload["max_daily_loss_pct"] == 3
        # USD twin derived from initial_capital (default 10000) on save.
        assert payload["max_daily_loss"] == pytest.approx(300.0)
        from forven.exchange.risk import _get_risk_limits

        assert _get_risk_limits()["daily_loss_limit"] == pytest.approx(0.03)

    def test_legacy_max_daily_loss_usd_write_is_not_a_placebo(self, forven_db):
        # Enforcement uses max_daily_loss_pct whenever present (it always is),
        # so the legacy USD write must recompute the pct twin to take effect.
        put_settings_section("risk", {"max_daily_loss": 500})
        payload = _load_settings_payload()
        assert payload["max_daily_loss"] == 500
        assert payload["max_daily_loss_pct"] == pytest.approx(5.0)
        from forven.exchange.risk import _get_risk_limits

        assert _get_risk_limits()["daily_loss_limit"] == pytest.approx(0.05)

    def test_explicit_both_twins_in_one_payload_are_respected(self, forven_db):
        put_settings_section(
            "risk",
            {"max_risk_per_trade_pct": 6, "max_position_size_pct": 9},
        )
        payload = _load_settings_payload()
        assert payload["max_risk_per_trade_pct"] == 6
        assert payload["max_position_size_pct"] == 9

    def test_seed_defaults_unchanged(self, forven_db):
        payload = _load_settings_payload()
        assert payload["max_risk_per_trade_pct"] == 10
        assert payload["max_position_size_pct"] == 10
        assert payload["max_daily_loss_pct"] == 2
        assert payload["max_daily_loss"] == 200


# ---------------------------------------------------------------------------
# B-23: clearing a numeric pipeline field must not persist null
# ---------------------------------------------------------------------------


class TestPipelineNullRejection:
    def test_nested_threshold_null_is_rejected_with_clear_message(self, forven_db):
        with pytest.raises(HTTPException) as exc:
            put_pipeline_settings(
                PipelineSettingsUpdateBody(updates={"gauntlet": {"min_trades": None}})
            )
        assert exc.value.status_code == 400
        assert "gauntlet.min_trades" in str(exc.value.detail)

    def test_rejected_null_does_not_poison_gate_config(self, forven_db):
        from forven.policy import load_pipeline_config

        before = load_pipeline_config()["gauntlet"].get("min_trades")
        with pytest.raises(HTTPException):
            put_pipeline_settings(
                PipelineSettingsUpdateBody(updates={"gauntlet": {"min_trades": None}})
            )
        after = load_pipeline_config()["gauntlet"].get("min_trades")
        assert after == before
        assert after is not None
        # Gate evaluation reads this with int(...) — must not raise.
        int(after)

    def test_flat_pipeline_null_is_rejected(self, forven_db):
        with pytest.raises(HTTPException) as exc:
            put_pipeline_settings(
                PipelineSettingsUpdateBody(updates={"min_sharpe_ratio": None})
            )
        assert exc.value.status_code == 400
        assert get_pipeline_settings()["min_sharpe_ratio"] == 0.5

    def test_normalize_crash_path_null_is_rejected_not_500(self, forven_db):
        # quick_screen.min_sharpe used to crash _normalize_pipeline_config
        # (float(None)) inside save — now refused cleanly at the boundary.
        with pytest.raises(HTTPException) as exc:
            put_pipeline_settings(
                PipelineSettingsUpdateBody(updates={"quick_screen": {"min_sharpe": None}})
            )
        assert exc.value.status_code == 400

    def test_valid_updates_still_save(self, forven_db):
        from forven.policy import load_pipeline_config

        result = put_pipeline_settings(
            PipelineSettingsUpdateBody(
                updates={"gauntlet": {"min_trades": 42}, "autopilot_worker_concurrency": 2}
            )
        )
        assert result["autopilot_worker_concurrency"] == 2
        assert load_pipeline_config()["gauntlet"]["min_trades"] == 42


# ---------------------------------------------------------------------------
# B-12: data-engine nested partial save must not reset siblings
# ---------------------------------------------------------------------------


class TestDataEngineNestedPartialSave:
    def test_editing_one_nested_leaf_preserves_siblings(self, forven_db):
        put_settings_section(
            "data-engine",
            {"data_engine_settings": {"source_reconciliation": {"enabled": True}}},
        )
        assert (
            _load_settings_payload()["data_engine_settings"]["source_reconciliation"]["enabled"]
            is True
        )

        # Routine tuning of a different leaf in the same nested dict...
        put_settings_section(
            "data-engine",
            {"data_engine_settings": {"source_reconciliation": {"max_divergence_pct": 5.0}}},
        )
        sr = _load_settings_payload()["data_engine_settings"]["source_reconciliation"]
        # ...must not silently disable the promotion gate.
        assert sr["enabled"] is True
        assert sr["max_divergence_pct"] == 5.0
        # Untouched siblings keep their defaults.
        assert sr["block_when_missing"] is False
        assert sr["staleness_hours"] == 24

    def test_editing_one_source_priority_preserves_other_streams(self, forven_db):
        put_settings_section(
            "data-engine",
            {"data_engine_settings": {"source_priority": {"funding": ["bybit", "binance"]}}},
        )
        put_settings_section(
            "data-engine",
            {"data_engine_settings": {"source_priority": {"candles": ["okx"]}}},
        )
        priority = _load_settings_payload()["data_engine_settings"]["source_priority"]
        assert priority["candles"] == ["okx"]
        assert priority["funding"] == ["bybit", "binance"]
        assert priority["oi"] == ["binance"]

    def test_top_level_keys_still_merge(self, forven_db):
        put_settings_section(
            "data-engine",
            {"data_engine_settings": {"auto_catchup_enabled": False}},
        )
        des = _load_settings_payload()["data_engine_settings"]
        assert des["auto_catchup_enabled"] is False
        # Nested defaults still filled for genuinely-missing keys (source
        # reconciliation now defaults ON — the Binance↔HL divergence safety net).
        assert des["source_reconciliation"]["enabled"] is True


# ---------------------------------------------------------------------------
# MODE-SPLIT-1: a requested trading_mode is persisted only after enforcement
# (config.set_execution_mode) accepts it. Previously a refused 'live' was
# stored in the settings blob anyway, so Settings displayed "live" while the
# runtime stayed paper.
# ---------------------------------------------------------------------------


class TestTradingModePersistAfterAccept:
    def test_rejected_live_is_a_400_and_never_persisted(self, forven_db):
        from forven.config import get_execution_mode

        with pytest.raises(HTTPException) as exc_info:
            put_settings_section("trading-mode", {"trading_mode": "live"})
        assert exc_info.value.status_code == 400
        assert "live" in str(exc_info.value.detail)
        # The stored mirror and the enforced mode both still say paper.
        assert _load_settings_payload()["trading_mode"] == "paper"
        assert get_execution_mode() == "paper"
        assert get_settings()["trading_mode"] == "paper"

    def test_paper_save_round_trips(self, forven_db):
        result = put_settings_section("trading-mode", {"trading_mode": "paper"})
        assert result["trading_mode"] == "paper"
        assert _load_settings_payload()["trading_mode"] == "paper"

    def test_beta_build_coerces_live_to_paper_without_erroring(self, forven_db, monkeypatch):
        monkeypatch.setenv("FORVEN_ENV", "beta")
        result = put_settings_section("trading-mode", {"trading_mode": "live"})
        assert result["trading_mode"] == "paper"
        assert _load_settings_payload()["trading_mode"] == "paper"
