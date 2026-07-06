"""Trade-frequency-aware WFA window sizing (forven.wfa_window).

The window must be a function of the strategy's measured trade rate so every
OOS fold gets a judgeable trade sample (S05925: a ~3-trades/month 4h strategy
got 1-3 OOS trades per fold from the 1Y calendar window — coin-flip folds).
"""

import json

from forven.db import get_db
from forven.wfa_window import measured_trade_rate, recommended_wfa_window


def _insert_strategy(conn, sid, metrics):
    conn.execute(
        "INSERT INTO strategies (id, name, type, status, stage, owner, metrics) "
        "VALUES (?, ?, 'rsi_momentum', 'gauntlet', 'gauntlet', 'brain', ?)",
        (sid, sid, json.dumps(metrics)),
    )


def test_measured_rate_prefers_strategy_metrics(forven_db):
    with get_db() as conn:
        # ~2.78 trades/month == the S05925 shape (40 trades over 14.38 months).
        _insert_strategy(conn, "s-rate", {"total_trades": 40, "backtest_months": 14.38})
    rate, source = measured_trade_rate("s-rate")
    assert source == "strategy_metrics"
    assert 0.08 < rate < 0.10  # trades per day


def test_measured_rate_falls_back_to_latest_backtest(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "s-bt-rate", {})
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, "
            "metrics_json, config_json, created_at) "
            "VALUES ('bt-rate-1', 's-bt-rate', 'backtest', 'BTC', '4h', ?, '{}', datetime('now'))",
            (json.dumps({"total_trades": 30, "backtest_months": 3.0}),),
        )
    rate, source = measured_trade_rate("s-bt-rate")
    assert source == "latest_backtest"
    assert rate > 0


def test_measured_rate_ignores_other_timeframe_sweep_rows(forven_db):
    """S06128 (2026-07-06): the background timeframe sweep clobbered a 1h
    strategy's metrics blob and latest backtest rows with 15m runs (~3.5x the
    real cadence), shrinking its WFA window to 30-day folds. The rate must come
    from rows on the strategy's OWN timeframe, not the blob or newer sweep rows."""
    with get_db() as conn:
        # blob clobbered by the 15m sweep: 107 trades over ~12 months
        conn.execute(
            "INSERT INTO strategies (id, name, type, status, stage, owner, timeframe, metrics) "
            "VALUES ('s-sweep', 's-sweep', 'rsi_momentum', 'gauntlet', 'gauntlet', 'brain', '1h', ?)",
            (json.dumps({"total_trades": 107, "backtest_months": 11.99}),),
        )
        # older canonical 1h row: 30 trades over ~12 months (~0.082/day)
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, "
            "metrics_json, config_json, created_at) "
            "VALUES ('bt-1h', 's-sweep', 'backtest', 'BTC', '1h', ?, '{}', datetime('now', '-1 hour'))",
            (json.dumps({"total_trades": 30, "backtest_months": 11.99}),),
        )
        # newer 15m sweep rows with the inflated cadence
        for i in range(2):
            conn.execute(
                "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, "
                "metrics_json, config_json, created_at) "
                f"VALUES ('bt-15m-{i}', 's-sweep', 'backtest', 'BTC', '15m', ?, '{{}}', datetime('now'))",
                (json.dumps({"total_trades": 107, "backtest_months": 11.99}),),
            )
    rate, source = measured_trade_rate("s-sweep", "1h")
    assert source == "latest_backtest"
    assert 0.07 < rate < 0.10, rate  # the 1h cadence, not the 15m sweep's 0.29/day

    # timeframe resolved from the strategies row when not passed
    rate2, source2 = measured_trade_rate("s-sweep")
    assert (rate2, source2) == (rate, source)

    # and the window recommendation reflects the real (slow) cadence
    rec = recommended_wfa_window("s-sweep", "1h", n_splits=5, train_ratio=0.7)
    assert rec["window_days"] > 1000, rec


def test_measured_rate_uses_combined_counts_not_oos_window_top_level(forven_db):
    """S06127 (2026-07-06): compact backtest blobs mirror the OOS evaluation
    window at the TOP level (14 trades / 3.6mo of a 12mo run), which inflated
    the cadence ~1.6x and collapsed the WFA window from ~47k to ~31k bars. The
    rate must come from the combined IS+OOS figures."""
    blob = {
        # top level mirrors the OOS window: 14 trades over 3.6 months
        "total_trades": 14,
        "backtest_months": 3.5962,
        "in_sample": {"total_trades": 17, "backtest_months": 8.3929},
        "out_of_sample": {"total_trades": 14, "backtest_months": 3.5962},
        "combined": {"total_trades": 31, "backtest_months": 11.9891},
    }
    with get_db() as conn:
        _insert_strategy(conn, "s-oos-top", {})
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, "
            "metrics_json, config_json, created_at) "
            "VALUES ('bt-oos-top', 's-oos-top', 'backtest', 'BTC', '1h', ?, '{}', datetime('now'))",
            (json.dumps(blob),),
        )
    rate, source = measured_trade_rate("s-oos-top", "1h")
    assert source == "latest_backtest"
    # 31 trades / 11.99 months ~= 0.085/day — NOT the OOS window's 0.128/day
    assert 0.075 < rate < 0.095, rate

    # without a combined section, IS+OOS sums are used
    blob2 = {k: v for k, v in blob.items() if k != "combined"}
    with get_db() as conn:
        conn.execute(
            "UPDATE backtest_results SET metrics_json = ? WHERE result_id = 'bt-oos-top'",
            (json.dumps(blob2),),
        )
    rate2, _ = measured_trade_rate("s-oos-top", "1h")
    assert 0.075 < rate2 < 0.095, rate2


def test_low_frequency_4h_strategy_gets_multi_year_window(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "s-slow", {"total_trades": 40, "backtest_months": 14.38})
    rec = recommended_wfa_window("s-slow", "4h", n_splits=5, train_ratio=0.7)
    # ~0.0914 trades/day, target 10/fold -> ~109 OOS days/fold -> ~1824-day window.
    assert rec["window_days"] > 1500, rec
    assert rec["trade_rate_source"] == "strategy_metrics"
    assert rec["target_oos_trades_per_fold"] >= 10
    # Bars stay within the runner ceiling.
    assert rec["window_bars"] <= 50_000


def test_high_frequency_strategy_keeps_short_window(forven_db):
    with get_db() as conn:
        # ~10 trades/day: the min-OOS-days floor dominates, not the trade rate.
        _insert_strategy(conn, "s-fast", {"total_trades": 900, "backtest_months": 3.0})
    rec = recommended_wfa_window("s-fast", "1h", n_splits=5, train_ratio=0.7)
    assert rec["window_days"] <= 600, rec


def test_unmeasured_strategy_uses_timeframe_fallback(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "s-fresh", {})
    rec_4h = recommended_wfa_window("s-fresh", "4h", n_splits=5, train_ratio=0.7)
    rec_1h = recommended_wfa_window("s-fresh", "1h", n_splits=5, train_ratio=0.7)
    assert rec_4h["trade_rate_source"] == "none"
    # Coarser timeframe -> longer fallback window.
    assert rec_4h["window_days"] > rec_1h["window_days"]


def test_sub_hourly_window_capped_by_max_bars(forven_db):
    with get_db() as conn:
        # Very low rate on 5m bars: uncapped this would explode past 50k bars.
        _insert_strategy(conn, "s-5m-slow", {"total_trades": 5, "backtest_months": 12.0})
    rec = recommended_wfa_window("s-5m-slow", "5m", n_splits=5, train_ratio=0.7)
    assert rec["capped_by_max_bars"] is True
    assert rec["window_bars"] == 50_000


def test_more_splits_need_a_longer_window(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "s-splits", {"total_trades": 40, "backtest_months": 14.38})
    rec5 = recommended_wfa_window("s-splits", "4h", n_splits=5, train_ratio=0.7)
    rec10 = recommended_wfa_window("s-splits", "4h", n_splits=10, train_ratio=0.7)
    assert rec10["window_days"] > rec5["window_days"]
