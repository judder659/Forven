"""Data-substrate provenance (2026-07-07 funding-interval incident).

A keepalive backfill transiently flipped funding print cadence 8h->1h on some
symbols; every interval-derived per-hour rate inflated ~8x and a +54%/yr
"validated" basket result recomputed as -14%/yr hours later. Verdicts must be
stamped with the DATA semantics they were scored on and refuse comparison
when those semantics change — exactly like engine-version provenance.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

import forven.data_provenance as dp
from forven.engine_provenance import BACKTEST_ENGINE_VERSION


@pytest.fixture()
def fake_lake(tmp_path, monkeypatch):
    """Point the enrichment DIR constants at a tmp lake with one funding file."""
    from forven import data_manager

    funding_dir = tmp_path / "funding"
    (funding_dir / "BTC-USDT").mkdir(parents=True)
    for attr in ("OI_DIR", "DERIVATIVES_DIR", "BASIS_DIR", "VOL_DIR"):
        monkeypatch.setattr(data_manager, attr, tmp_path / attr.lower())
    monkeypatch.setattr(data_manager, "FUNDING_DIR", funding_dir)
    dp.clear_fingerprint_cache()
    yield funding_dir
    dp.clear_fingerprint_cache()


def _write_funding(funding_dir, *, cadence_hours: float, n: int, rate: float = 1e-4, start="2026-01-01"):
    ts = pd.date_range(start, periods=n, freq=f"{int(cadence_hours * 60)}min", tz="UTC")
    pd.DataFrame({"timestamp": ts, "funding_rate": np.full(n, rate)}).to_parquet(
        funding_dir / "BTC-USDT" / "history.parquet"
    )


def test_fingerprint_stable_under_appends(fake_lake):
    _write_funding(fake_lake, cadence_hours=8, n=600)
    fp1, detail = dp.data_fingerprint("BTC/USDT", "1h")
    assert detail["funding_rate"]["cadence_h"] == 8.0

    dp.clear_fingerprint_cache()
    _write_funding(fake_lake, cadence_hours=8, n=900)  # normal growth
    fp2, _ = dp.data_fingerprint("BTC/USDT", "1h")
    assert fp1 == fp2


def test_fingerprint_flips_on_cadence_change(fake_lake):
    _write_funding(fake_lake, cadence_hours=8, n=600)
    fp1, _ = dp.data_fingerprint("BTC/USDT", "1h")

    dp.clear_fingerprint_cache()
    _write_funding(fake_lake, cadence_hours=1, n=1600)  # the incident shape
    fp2, detail = dp.data_fingerprint("BTC/USDT", "1h")
    assert fp1 != fp2
    assert detail["funding_rate"]["cadence_h"] == 1.0


def test_fingerprint_flips_on_scale_change(fake_lake):
    _write_funding(fake_lake, cadence_hours=8, n=600, rate=1e-4)
    fp1, _ = dp.data_fingerprint("BTC/USDT", "1h")

    dp.clear_fingerprint_cache()
    _write_funding(fake_lake, cadence_hours=8, n=600, rate=1.25e-5)  # /8 rescale
    fp2, _ = dp.data_fingerprint("BTC/USDT", "1h")
    assert fp1 != fp2


def test_stamp_and_staleness_contract(fake_lake):
    _write_funding(fake_lake, cadence_hours=8, n=600)
    config = dp.stamp_data_fingerprint({"engine_version": 2}, "BTC/USDT", "1h")
    assert dp.DATA_FINGERPRINT_KEY in config
    # matching semantics -> fresh
    assert dp.is_stale_data_artifact(config, "BTC/USDT", "1h") is False
    # unstamped -> grandfathered, never stale
    assert dp.is_stale_data_artifact({"engine_version": 2}, "BTC/USDT", "1h") is False

    # semantics change -> explicit mismatch -> stale
    dp.clear_fingerprint_cache()
    _write_funding(fake_lake, cadence_hours=1, n=1600)
    assert dp.is_stale_data_artifact(config, "BTC/USDT", "1h") is True


def test_verdict_reader_refuses_stale_data_rows(forven_db, monkeypatch):
    from forven.db import get_db
    from forven.policy import _extract_gauntlet_verdict_payloads

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, status, stage, owner, timeframe) "
            "VALUES ('s-dp', 's-dp', 'rsi_momentum', 'gauntlet', 'gauntlet', 'brain', '1h')",
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, "
            "metrics_json, config_json, created_at) VALUES (?, 's-dp', 'cost_stress', 'BTC', '1h', ?, ?, datetime('now'))",
            (
                "cs-stale",
                json.dumps({"status": "succeeded", "verdict": "PASS", "degradation_pct": 5.0}),
                # Stamp the CURRENT engine version, not a literal: this test isolates
                # DATA provenance, so the row must not also be rejected as
                # engine-stale. A hardcoded version silently turns this into a
                # vacuous test on the next engine re-baseline (v2->v5 did exactly
                # that — the negative assertion kept passing for the wrong reason
                # while the positive one failed).
                json.dumps(
                    {
                        "engine_version": BACKTEST_ENGINE_VERSION,
                        dp.DATA_FINGERPRINT_KEY: "oldsemantics",
                    }
                ),
            ),
        )

    monkeypatch.setattr(dp, "data_fingerprint", lambda s, t: ("newsemantics", {}))
    payloads, _ = _extract_gauntlet_verdict_payloads("s-dp", None, {})
    assert "cost_stress" not in payloads  # stale-data row refused

    monkeypatch.setattr(dp, "data_fingerprint", lambda s, t: ("oldsemantics", {}))
    payloads2, _ = _extract_gauntlet_verdict_payloads("s-dp", None, {})
    assert "cost_stress" in payloads2  # matching semantics accepted


def test_stale_data_reason_code_is_counter_exempt():
    from forven.policy import _EVIDENCE_ABSENCE_REASON_CODES, _extract_reason_code

    code = _extract_reason_code("Validation artifacts were scored on a different data fingerprint")
    assert code == "stale_data_artifacts"
    assert "stale_data_artifacts" in _EVIDENCE_ABSENCE_REASON_CODES


def test_per_print_funding_conversion_survives_mixed_cadence(fake_lake):
    from forven.basket_lab import _per_hour_funding_series

    # 400 prints at 8h then 200 prints at 1h, same PER-PRINT rate value.
    ts8 = pd.date_range("2026-01-01", periods=400, freq="8h", tz="UTC")
    ts1 = pd.date_range(ts8[-1] + pd.Timedelta(hours=8), periods=200, freq="1h", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": ts8.append(ts1),
            "funding_rate": np.full(600, 8e-5),
        }
    )
    frame.to_parquet(fake_lake / "BTC-USDT" / "history.parquet")

    index = pd.date_range("2026-01-01", periods=int(400 * 8 + 220), freq="1h", tz="UTC")
    s = _per_hour_funding_series("BTC/USDT", index)
    assert s is not None
    # 8h era: rate/8; 1h era: rate/1 — a single whole-file divisor gets one wrong
    early = s.loc["2026-01-10"]
    late = s.loc[ts1[50] : ts1[100]]
    assert np.allclose(early.dropna(), 1e-5)
    assert np.allclose(late.dropna(), 8e-5)
