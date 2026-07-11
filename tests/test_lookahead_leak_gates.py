"""Data-leak / lookahead gates (2026-06-15).

A future-bar leak (e.g. `.shift(-1)`) makes a strategy's metrics impossibly good
on BOTH the IS and OOS slices, so the IS/OOS-gap overfit detector (gap ~0) and
the win-rate trap (needs PF < 1.2) both miss it, and it sails into PAPER.

GATE A (forven.policy._implausible_metrics_reason): reject Sharpe >= 5 / PF >= 8
(or a Sharpe pegged at the +/-10 backtest clamp) on either slice, at quick_screen
(primary, universal) and the gauntlet gate (defense-in-depth).

GATE B (forven.strategies.lookahead_probe.detect_lookahead): a truncation-
invariance probe at registration — a causal signal at bar t must be unchanged
when bars after t are withheld; if it flips, the strategy reads the future.
"""

import json
from datetime import datetime, timezone

import pandas as pd

from forven.db import get_db
from forven.policy import (
    _evaluate_quick_screen_gate,
    _implausible_metrics_reason,
    load_pipeline_config,
)
from forven.strategies.lookahead_probe import detect_lookahead


# ============================ GATE A: helper ===============================

def test_implausible_reason_flags_clamped_oos_sharpe():
    cfg = load_pipeline_config()
    metrics = {
        "in_sample": {"sharpe": 1.4, "profit_factor": 1.3},
        "out_of_sample": {"sharpe": 10.0, "profit_factor": 1.3},
    }
    reason = _implausible_metrics_reason(metrics, cfg)
    assert reason is not None
    low = reason.lower()
    assert "implausible" in low and "leak" in low


def test_implausible_reason_flags_absurd_is_profit_factor():
    cfg = load_pipeline_config()
    metrics = {
        "in_sample": {"sharpe": 2.0, "profit_factor": 15.0},
        "out_of_sample": {"sharpe": 1.8, "profit_factor": 1.4},
    }
    reason = _implausible_metrics_reason(metrics, cfg)
    assert reason is not None
    assert "pf" in reason.lower() or "profit" in reason.lower()


def test_implausible_reason_none_for_sane_metrics():
    cfg = load_pipeline_config()
    metrics = {
        "in_sample": {"sharpe": 1.5, "profit_factor": 1.4},
        "out_of_sample": {"sharpe": 1.1, "profit_factor": 1.35},
    }
    assert _implausible_metrics_reason(metrics, cfg) is None


def test_implausible_reason_none_for_empty_metrics():
    cfg = load_pipeline_config()
    assert _implausible_metrics_reason({}, cfg) is None
    assert _implausible_metrics_reason(None, cfg) is None


def test_implausible_reason_robust_to_none_values():
    cfg = load_pipeline_config()
    # None sharpe/pf must not crash — treated as absent.
    metrics = {
        "in_sample": {"sharpe": None, "profit_factor": None},
        "out_of_sample": {"sharpe": 1.0, "profit_factor": None},
    }
    assert _implausible_metrics_reason(metrics, cfg) is None


# ====================== GATE A: quick_screen gate ==========================

def _insert_strategy(sid, *, metrics, stage="quick_screen"):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, "
            "status, owner, stage, stage_changed_at, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'ETH', '1h', '{}', ?, 'active', 'brain', ?, ?, ?, ?)",
            (sid, sid, json.dumps(metrics), stage, now, now, now),
        )
        conn.commit()


# Healthy distinct IS/OOS slices that clear every other quick-screen guardrail
# (min_trades, IS Sharpe > 0.1, gap <= 1.5, robustness >= 10, PF floors, win
# trap). Used as the sane baseline; leak variants override sharpe/PF.
def _sane_metrics(**overrides):
    m = {
        "total_trades": 45,
        "total_return_pct": 12.0,
        "max_drawdown_pct": 0.12,
        "robustness_score": 60.0,
        "in_sample": {
            "sharpe": 1.4, "profit_factor": 1.5, "total_trades": 45,
            "total_return_pct": 18.0, "max_drawdown_pct": 0.12, "win_rate": 45.0,
        },
        "out_of_sample": {
            "sharpe": 1.0, "profit_factor": 1.3, "total_trades": 40,
            "total_return_pct": 8.0, "max_drawdown_pct": 0.12, "win_rate": 42.0,
        },
    }
    m.update(overrides)
    return m


def test_quick_screen_rejects_leak_metrics(forven_db):
    # Loosen the win-rate-trap-irrelevant structural floors are not in play here;
    # quick_screen reads metrics directly off the row.
    metrics = _sane_metrics(
        out_of_sample={
            "sharpe": 10.0, "profit_factor": 15.0, "total_trades": 40,
            "total_return_pct": 5000.0, "max_drawdown_pct": 0.02, "win_rate": 79.0,
        },
    )
    _insert_strategy("qs-leak", metrics=metrics)
    passed, reason = _evaluate_quick_screen_gate("qs-leak", load_pipeline_config())
    assert passed is False
    low = reason.lower()
    assert "implausible" in low or "leak" in low, reason


def test_quick_screen_passes_sane_strategy(forven_db):
    _insert_strategy("qs-sane", metrics=_sane_metrics())
    passed, reason = _evaluate_quick_screen_gate("qs-sane", load_pipeline_config())
    assert passed is True, reason
    assert "implausible" not in reason.lower()


# ============ WIN-RATE-UNIT-1: win-rate guards fire in EITHER unit ============
# compute_metrics emits win_rate as a 0-1 RATIO, but the S00552 guards compared
# against percent values (> 60), so the ratio form (0.79) could never trip them —
# a silent lookahead escape hatch. The guards now normalize to percent points, so
# they fire whether win_rate is stored as a ratio (0.80) or percent (80.0).


def _config_with_low_pf_floor():
    # Drop the PF floor below 1.0 so the win-rate-TRAP guard (which needs
    # min PF < 1.0) is reachable — otherwise GUARDRAIL 3 (PF floor, default 1.0)
    # rejects first and the win-rate guard is never exercised.
    config = load_pipeline_config()
    config = dict(config)
    qs = dict(config.get("quick_screen", {}))
    qs["min_profit_factor"] = 0.5
    config["quick_screen"] = qs
    return config


def test_quick_screen_win_rate_trap_fires_ratio_form(forven_db):
    # OOS win_rate 0.80 (RATIO) + min PF 0.9 (< 1.0): the win-rate-trap must fire.
    # Pre-fix this passed because 0.80 is not > 60.0.
    metrics = _sane_metrics(
        in_sample={
            "sharpe": 1.2, "profit_factor": 0.95, "total_trades": 45,
            "total_return_pct": 4.0, "max_drawdown_pct": 0.12, "win_rate": 0.80,
        },
        out_of_sample={
            "sharpe": 0.9, "profit_factor": 0.9, "total_trades": 40,
            "total_return_pct": 3.0, "max_drawdown_pct": 0.12, "win_rate": 0.80,
        },
    )
    _insert_strategy("qs-wrtrap-ratio", metrics=metrics)
    passed, reason = _evaluate_quick_screen_gate("qs-wrtrap-ratio", _config_with_low_pf_floor())
    assert passed is False, reason
    assert "win rate trap" in reason.lower(), reason
    # Log message prints sensible percent, not the raw 0.80 ratio.
    assert "80.0%" in reason, reason


def test_quick_screen_win_rate_trap_fires_percent_form(forven_db):
    # win_rate 80.0 (PERCENT) + PF 0.9: unit-agnostic — still fires.
    metrics = _sane_metrics(
        in_sample={
            "sharpe": 1.2, "profit_factor": 0.95, "total_trades": 45,
            "total_return_pct": 4.0, "max_drawdown_pct": 0.12, "win_rate": 80.0,
        },
        out_of_sample={
            "sharpe": 0.9, "profit_factor": 0.9, "total_trades": 40,
            "total_return_pct": 3.0, "max_drawdown_pct": 0.12, "win_rate": 80.0,
        },
    )
    _insert_strategy("qs-wrtrap-pct", metrics=metrics)
    passed, reason = _evaluate_quick_screen_gate("qs-wrtrap-pct", _config_with_low_pf_floor())
    assert passed is False, reason
    assert "win rate trap" in reason.lower(), reason
    assert "80.0%" in reason, reason


def test_quick_screen_legacy_high_wr_requires_pf_ratio_form(forven_db):
    # OOS win_rate 0.75 (RATIO) + OOS PF 1.1 (<= 1.2): the legacy high-WR standard
    # must fire (requires OOS PF > 1.2). Both PFs stay >= the 1.0 floor so the
    # win-rate-TRAP branch does not swallow this — only the legacy branch bites.
    metrics = _sane_metrics(
        in_sample={
            "sharpe": 1.2, "profit_factor": 1.15, "total_trades": 45,
            "total_return_pct": 6.0, "max_drawdown_pct": 0.12, "win_rate": 0.75,
        },
        out_of_sample={
            "sharpe": 0.9, "profit_factor": 1.1, "total_trades": 40,
            "total_return_pct": 4.0, "max_drawdown_pct": 0.12, "win_rate": 0.75,
        },
    )
    _insert_strategy("qs-legacy-wr", metrics=metrics)
    passed, reason = _evaluate_quick_screen_gate("qs-legacy-wr", load_pipeline_config())
    assert passed is False, reason
    assert "overfit reject" in reason.lower() and "high win rate" in reason.lower(), reason
    assert "75.0%" in reason, reason


def test_quick_screen_healthy_win_rate_ratio_does_not_fire(forven_db):
    # win_rate 0.55 (RATIO, = 55%): below the 60% trigger — guards must not fire.
    metrics = _sane_metrics(
        in_sample={
            "sharpe": 1.4, "profit_factor": 1.5, "total_trades": 45,
            "total_return_pct": 18.0, "max_drawdown_pct": 0.12, "win_rate": 0.55,
        },
        out_of_sample={
            "sharpe": 1.0, "profit_factor": 1.3, "total_trades": 40,
            "total_return_pct": 8.0, "max_drawdown_pct": 0.12, "win_rate": 0.55,
        },
    )
    _insert_strategy("qs-healthy-wr", metrics=metrics)
    passed, reason = _evaluate_quick_screen_gate("qs-healthy-wr", load_pipeline_config())
    assert passed is True, reason
    assert "win rate" not in reason.lower(), reason


# ============================ GATE B: probe ================================

class _LeakStrategy:
    """generate_signals reads the FUTURE bar via .shift(-1) — a lookahead leak."""

    strategy_id = "leak-strat"

    def generate_signals(self, df: pd.DataFrame):
        close = df["close"]
        # Enter when the NEXT bar's close is higher than now (impossible to know
        # at bar t). .shift(-1) pulls bar t+1 back to t => future leak.
        future = close.shift(-1)
        entries = (future > close).fillna(False)
        exits = (future < close).fillna(False)
        return entries, exits


class _CausalStrategy:
    """Clean causal breakout: only uses past/current bars (.shift(+1))."""

    strategy_id = "causal-strat"

    def generate_signals(self, df: pd.DataFrame):
        close = df["close"]
        prior_high = close.rolling(20).max().shift(1)
        entries = (close > prior_high).fillna(False)
        exits = (close < close.rolling(20).min().shift(1)).fillna(False)
        return entries, exits


def test_detect_lookahead_flags_future_shift():
    reason = detect_lookahead(_LeakStrategy())
    assert reason is not None
    low = reason.lower()
    assert "lookahead" in low
    assert "future" in low


def test_detect_lookahead_passes_causal_strategy():
    assert detect_lookahead(_CausalStrategy()) is None


def test_detect_lookahead_none_without_generate_signals():
    class _NoVectorized:
        strategy_id = "no-vec"

    assert detect_lookahead(_NoVectorized()) is None


def test_detect_lookahead_blocks_strategy_originated_probe_errors():
    class _Boom:
        strategy_id = "boom"

        def generate_signals(self, df):
            raise RuntimeError("kaboom")

    reason = detect_lookahead(_Boom())
    assert reason is not None
    assert "causal execution could not be verified" in reason


# ================= GATE B: engine-revival re-entry (LOOKAHEAD-REENTRY-2) =================
# PR#63 added a lookahead re-probe to the brain's research-recovery re-entry
# (brain.try_research_recovery via brain._reentry_lookahead_reason). The OTHER
# re-entry path — the engine-version-staleness revival in
# gauntlet.engine.requeue_stale_engine_artifacts — gated only on
# detect_execution_crash (a raise), so a clean-running future-bar leak could be
# revived back into quick_screen WITHOUT the causality check. It now runs the same
# re-probe at that choke point. These tests mirror the brain-recovery tests: a leak
# blocks the revival (archived, status_reason lookahead_blocked:tier2:lookahead); an
# inconclusive probe (helper returns None) never blocks a legitimate revival.

from forven.db import create_strategy_container, kv_get  # noqa: E402
from forven.engine_provenance import BACKTEST_ENGINE_VERSION  # noqa: E402

_STALE_VERSION = BACKTEST_ENGINE_VERSION + 1000  # explicitly stamped, never current


def _revival_strategy(stage="archived", name="Revival Leak Test"):
    with get_db() as conn:
        strategy_id, _display_id, _base_id = create_strategy_container(
            conn=conn,
            name=name,
            type_="rsi_momentum",
            symbol="ETH/USDT",
            timeframe="1h",
            params={"rsi_period": 14},
            stage="quick_screen",
        )
        if stage != "quick_screen":
            conn.execute(
                "UPDATE strategies SET stage = ?, stage_changed_at = ? WHERE id = ?",
                (stage, datetime.now(timezone.utc).isoformat(), strategy_id),
            )
    return strategy_id


def _insert_stale_verdict(strategy_id, *, result_id, verdict="FAIL"):
    from forven.api_core import _persist_backtest_result_row

    _persist_backtest_result_row(
        result_id=result_id,
        strategy_id=strategy_id,
        result_type="walk_forward",
        symbol="ETH/USDT",
        timeframe="1h",
        start_date=None,
        end_date=None,
        metrics={"status": "succeeded", "verdict": verdict},
        config={"status": "succeeded", "engine_version": _STALE_VERSION},
    )


def _make_failed_gate_archive(name):
    """An archived strategy with a failed_gate workflow + a stale-engine verdict —
    exactly the shape the revival sweep would otherwise un-archive."""
    from forven.gauntlet.settings import build_settings_snapshot
    from forven.gauntlet.store import create_or_get_workflow

    strategy_id = _revival_strategy(stage="archived", name=name)
    workflow = create_or_get_workflow(
        strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot()
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_workflows SET status = 'failed_gate' WHERE id = ?", (workflow["id"],)
        )
    _insert_stale_verdict(strategy_id, result_id=f"r-{name}", verdict="FAIL")
    return strategy_id, workflow["id"]


def _stage_of(strategy_id):
    with get_db() as conn:
        row = conn.execute("SELECT stage, status_reason FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    return row["stage"], row["status_reason"]


def test_engine_revival_blocks_lookahead_leak(forven_db, monkeypatch):
    """A stale-engine revival candidate whose re-probe reports a leak is NOT
    revived: it stays archived with the lookahead status_reason and a
    skipped_lookahead marker (mirrors the crash-probe skip)."""
    import forven.brain as brain
    from forven.gauntlet.engine import requeue_stale_engine_artifacts

    strategy_id, _wf = _make_failed_gate_archive("leak-revival")
    # Force the re-probe (imported lazily from brain inside the sweep) to report a
    # leak — mock the source so the test is deterministic and offline.
    monkeypatch.setattr(
        brain, "_reentry_lookahead_reason",
        lambda sid, stype, params: "Lookahead leak: causal signal flips when future bars withheld",
    )

    summary = requeue_stale_engine_artifacts(limit=10)

    assert summary["revived"] == 0
    stage, status_reason = _stage_of(strategy_id)
    assert stage == "archived"
    assert status_reason == "lookahead_blocked:tier2:lookahead"
    marker = kv_get(f"forven:engine_rebaseline:v{BACKTEST_ENGINE_VERSION}:{strategy_id}")
    assert marker and marker["action"] == "skipped_lookahead"


def test_engine_revival_proceeds_when_probe_is_clean(forven_db, monkeypatch):
    """A clean re-probe (helper returns None — no leak, or an inconclusive infra
    fault) never blocks a legitimate revival: the strategy is revived normally."""
    import forven.brain as brain
    from forven.gauntlet.engine import requeue_stale_engine_artifacts

    strategy_id, _wf = _make_failed_gate_archive("clean-revival")
    # None = no leak / inconclusive — the sweep must revive as before.
    monkeypatch.setattr(brain, "_reentry_lookahead_reason", lambda sid, stype, params: None)

    summary = requeue_stale_engine_artifacts(limit=10)

    assert summary["revived"] == 1
    stage, _status_reason = _stage_of(strategy_id)
    assert stage == "quick_screen"
    marker = kv_get(f"forven:engine_rebaseline:v{BACKTEST_ENGINE_VERSION}:{strategy_id}")
    assert marker and marker["action"] == "revived"
