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
