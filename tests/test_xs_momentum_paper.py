"""Tests for the paper-only XS-VolMom book and staged Binance Vision bulk backfill."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from forven import scheduler as sched_mod
from forven import xs_momentum_paper as xs_mod


def _write_synthetic_lake(
    base_dir: Path,
    *,
    as_of: datetime,
    n_days: int = 35,
    extra_last_day_spike: bool = False,
) -> tuple[Path, Path]:
    """Create 12 synthetic coins (10 liquid + 2 illiquid) under ``base_dir/ohlcv``
    plus Hyperliquid hourly funding under ``base_dir/funding_hl``."""
    ohlcv_dir = base_dir / "ohlcv"
    hl_dir = base_dir / "funding_hl"
    ohlcv_dir.mkdir(parents=True, exist_ok=True)
    hl_dir.mkdir(parents=True, exist_ok=True)

    day_start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    start = day_start - timedelta(days=n_days)
    # 4h timestamps up to `as_of`
    ts_idx = pd.date_range(start=start, end=as_of, freq="4h", tz="UTC")

    # 10 liquid coins with clear monotonic return dispersion + 2 illiquid coins
    specs = [
        ("COIN01-USDT", +0.015, 50_000_000.0),
        ("COIN02-USDT", +0.012, 50_000_000.0),
        ("COIN03-USDT", +0.009, 50_000_000.0),
        ("COIN04-USDT", +0.006, 50_000_000.0),
        ("COIN05-USDT", +0.003, 50_000_000.0),
        ("COIN06-USDT", -0.003, 50_000_000.0),
        ("COIN07-USDT", -0.006, 50_000_000.0),
        ("COIN08-USDT", -0.009, 50_000_000.0),
        ("COIN09-USDT", -0.012, 50_000_000.0),
        ("COIN10-USDT", -0.015, 50_000_000.0),
        # Illiquid traps (must be filtered out by >= $25M/day median quote volume)
        ("TINYUP-USDT", +0.040, 2_000_000.0),
        ("TINYDN-USDT", -0.040, 2_000_000.0),
    ]

    for sym, daily_drift, daily_qvol in specs:
        sym_dir = ohlcv_dir / sym
        sym_dir.mkdir(parents=True, exist_ok=True)
        # Small deterministic noise so rolling std > 0
        rng = np.random.default_rng(abs(hash(sym)) % (2**32))
        bar_drift = daily_drift / 6.0
        rets = bar_drift + rng.normal(0.0, 0.004, size=len(ts_idx))
        prices = 100.0 * np.exp(np.cumsum(rets))
        if extra_last_day_spike and sym == "COIN10-USDT":
            # Huge spike AFTER day_start (intraday today) to test lookahead & 20% stop cap
            after_mask = ts_idx >= day_start
            prices[after_mask] = prices[after_mask] * 1.50
        vol = (daily_qvol / 6.0) / prices
        df = pd.DataFrame(
            {
                "timestamp": ts_idx,
                "open": prices,
                "high": prices * 1.005,
                "low": prices * 0.995,
                "close": prices,
                "volume": vol,
            }
        )
        df.to_parquet(sym_dir / "4h.parquet", index=False)

        # Write HL hourly funding (+0.0001 per hour = +0.24%/day; longs pay, shorts receive)
        coin = sym.split("-", 1)[0]
        c_dir = hl_dir / coin
        c_dir.mkdir(parents=True, exist_ok=True)
        f_idx = pd.date_range(start=day_start - timedelta(days=3), end=as_of, freq="1h", tz="UTC")
        fdf = pd.DataFrame({"timestamp": f_idx, "funding_rate": np.full(len(f_idx), 0.0001)})
        fdf.to_parquet(c_dir / "2026.parquet", index=False)

    return ohlcv_dir, base_dir


def test_load_closed_daily_panel_excludes_intraday_bars_from_signal(tmp_path: Path) -> None:
    as_of = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    ohlcv_dir, _ = _write_synthetic_lake(tmp_path, as_of=as_of, extra_last_day_spike=True)

    close_panel, qvol_panel, marks = xs_mod.load_closed_daily_panel(as_of=as_of, ohlcv_dir=ohlcv_dir)
    # Signal panel must end strictly BEFORE 2026-06-15 00:00 UTC (last closed day is 2026-06-14)
    assert close_panel.index[-1] == pd.Timestamp("2026-06-14", tz="UTC")
    assert len(qvol_panel) == len(close_panel)
    # Latest mark reflects today's +50% spike on COIN10-USDT while closed signal panel does NOT
    assert marks["COIN10-USDT"] > close_panel["COIN10-USDT"].iloc[-1] * 1.35


def test_compute_scores_filters_illiquid_coins_and_keeps_dollar_neutral_buffer(tmp_path: Path) -> None:
    as_of = datetime(2026, 6, 15, 0, 5, tzinfo=timezone.utc)
    ohlcv_dir, _ = _write_synthetic_lake(tmp_path, as_of=as_of)
    close_panel, qvol_panel, _ = xs_mod.load_closed_daily_panel(as_of=as_of, ohlcv_dir=ohlcv_dir)

    weights, scores, eligible = xs_mod.compute_scores_and_targets(close_panel, qvol_panel, set(), set())
    assert "TINYUP-USDT" not in eligible
    assert "TINYDN-USDT" not in eligible
    assert len(eligible) == 10
    assert len(weights) == 10
    assert sum(weights.values()) == pytest.approx(0.0, abs=1e-6)
    assert sum(abs(v) for v in weights.values()) == pytest.approx(1.0, abs=1e-5)
    # Strongest coins long, weakest coins short
    assert weights["COIN01-USDT"] > 0
    assert weights["COIN10-USDT"] < 0


def test_rebalance_is_idempotent_accrues_funding_and_caps_squeeze(forven_db, tmp_path: Path) -> None:
    day1 = datetime(2026, 6, 15, 0, 5, tzinfo=timezone.utc)
    ohlcv_dir, data_root = _write_synthetic_lake(tmp_path, as_of=day1)

    r1 = xs_mod.rebalance_xs_momentum_paper(as_of=day1, ohlcv_dir=ohlcv_dir, base_data_root=data_root)
    assert r1["ok"] is True and r1["skipped"] is False
    assert r1["entry"]["turnover"] == pytest.approx(1.0, abs=1e-4)
    assert r1["entry"]["cost_pnl_pct"] == pytest.approx(-0.065, abs=1e-4)
    assert r1["state"]["maker_equity"] > r1["state"]["equity"]

    # Second call on the same UTC date is a no-op
    r1_dup = xs_mod.rebalance_xs_momentum_paper(as_of=day1 + timedelta(hours=4), ohlcv_dir=ohlcv_dir, base_data_root=data_root)
    assert r1_dup["ok"] is True and r1_dup["skipped"] is True

    # Day 2 with a +50% adverse squeeze on short leg COIN10-USDT -> capped at -20% * 0.10 = -2.0%
    day2 = day1 + timedelta(days=1)
    ohlcv_dir2, data_root2 = _write_synthetic_lake(tmp_path / "d2", as_of=day2, extra_last_day_spike=True)
    r2 = xs_mod.rebalance_xs_momentum_paper(as_of=day2, ohlcv_dir=ohlcv_dir2, base_data_root=data_root2)
    assert r2["ok"] is True and r2["skipped"] is False
    assert "hyperliquid" in r2["entry"]["funding_sources"]
    # Equal-weight 5 longs + 5 shorts with identical +0.0001/h funding nets near 0 funding
    assert abs(r2["entry"]["funding_pnl_pct"]) < 0.05
    marked_c10 = next(m for m in r2["entry"]["marked_legs"] if m["symbol"] == "COIN10-USDT")
    assert marked_c10["stop_capped"] is True
    assert marked_c10["price_pnl_pct"] == pytest.approx(-2.0, abs=1e-3)

    status = xs_mod.get_xs_momentum_paper_status()
    assert status["initialized"] is True
    assert status["mode"] == "paper_only"
    assert status["stats"]["days_observed"] == 2


def test_scheduler_seeds_xs_momentum_job_and_stages_bulk_backfill(forven_db, monkeypatch) -> None:
    assert "forven-xs-momentum-paper" in sched_mod._DEFAULT_JOB_IDS
    sched_mod.seed_forven_jobs()
    jobs = {j["id"]: j for j in sched_mod.get_jobs()}
    assert "forven-xs-momentum-paper" in jobs
    assert jobs["forven-xs-momentum-paper"]["schedule_expr"] == "5 0 * * *"

    # Verify data_manager_backfill runs fast monthly streams (funding, basis) BEFORE slow (ohlcv, oi)
    calls: list[tuple[str, ...]] = []
    from forven import data_manager as dm_mod

    monkeypatch.setattr(
        dm_mod.data_manager,
        "backfill",
        lambda *, streams=("ohlcv", "funding", "oi"), **kw: calls.append(tuple(streams)) or {"BTC-USDT": {"funding": 100}},
    )
    status, err = asyncio.run(sched_mod.run_job(jobs["forven-data-bv-backfill"]))
    assert status == "ok" and err is None
    assert calls == [("funding", "basis"), ("ohlcv", "oi")]

    # Verify xs_momentum_paper_rebalance dispatches cleanly through scheduler.run_job
    monkeypatch.setattr(xs_mod, "rebalance_xs_momentum_paper", lambda: {"ok": True, "skipped": False})
    xs_status, xs_err = asyncio.run(sched_mod.run_job(jobs["forven-xs-momentum-paper"]))
    assert xs_status == "ok" and xs_err is None
