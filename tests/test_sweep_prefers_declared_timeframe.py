"""The timeframe sweep must never crown a NEGATIVE off-declared context over the
author's declared timeframe. Live case S06895 (2026-07-11, declared 4h): the
declared-4h sweep row (9 trades, Sharpe +3.0) missed the flat 10-trade degeneracy
floor by ONE trade, its pre-existing positive 4h rows were as_of-excluded, and the
crown fell to a 31-trade 1h context at Sharpe −2.40 — the strategy was then
merit-archived at a timeframe it never declared. A genuinely BETTER positive
off-declared timeframe must still win (the best-of-N enhancement stands)."""

from __future__ import annotations

import json

from forven.db import get_db
from forven.gauntlet.tasks import _best_sweep_result


def _insert_strategy(sid: str, timeframe: str = "4h"):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO strategies "
            "(id, name, type, symbol, timeframe, params, metrics, status, owner, stage, "
            " stage_changed_at, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'BTC', ?, '{}', '{}', 'quick_screen', 'brain', "
            "'quick_screen', datetime('now'), datetime('now'), datetime('now'))",
            (sid, sid, timeframe),
        )


def _insert_bt(
    sid: str,
    rid: str,
    tf: str,
    *,
    trades: int,
    sharpe: float,
    total_return: float = 0.0,
    is_trades: int | None = None,
    created_at: str = "2026-07-11T16:20:00+00:00",
):
    metrics = {
        "total_trades": trades,
        "sharpe_ratio": sharpe,
        "total_return_pct": total_return,
        "in_sample": {"total_trades": is_trades if is_trades is not None else max(trades - 2, 1)},
    }
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results "
            "(result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at) "
            "VALUES (?, ?, 'backtest', 'BTC', ?, ?, '{}', ?)",
            (rid, sid, tf, json.dumps(metrics), created_at),
        )


def test_declared_tf_wins_over_negative_offtf_when_declared_row_degenerate(forven_db):
    """S06895 reconstruction: declared-4h row degenerate by one trade; every
    surviving off-declared context is negative -> the declared 4h comes back
    UNMEASURED (no result id, no metrics) so the gate retries on the declared
    context instead of judging a hijacked negative one — and the degenerate
    lucky slice never reaches strategies.metrics."""
    sid = "S-SWPD1"
    _insert_strategy(sid, "4h")
    _insert_bt(sid, "bt-4h", "4h", trades=9, sharpe=2.996, total_return=6.0, is_trades=33)
    _insert_bt(sid, "bt-1h", "1h", trades=31, sharpe=-2.40, total_return=-9.2, is_trades=94)
    _insert_bt(sid, "bt-15m", "15m", trades=156, sharpe=-8.485, total_return=-33.9, is_trades=268)

    tf, rid, metrics = _best_sweep_result(sid, "4h")
    assert tf == "4h", f"negative off-declared context must not be crowned (got {tf})"
    assert rid is None, "a degeneracy-skipped declared slice must not be crowned either"
    assert metrics == {}


def test_declared_row_absent_negative_survivors_returns_declared_unmeasured(forven_db):
    """When the declared timeframe has NO row at all and every survivor is
    negative, the declared timeframe comes back unmeasured — a negative
    off-declared context is never crowned just because the declared run is
    missing (the last unclosed corner of the S06895 class)."""
    sid = "S-SWPD6"
    _insert_strategy(sid, "4h")
    _insert_bt(sid, "bt-1h", "1h", trades=31, sharpe=-2.40, total_return=-9.2, is_trades=94)
    _insert_bt(sid, "bt-15m", "15m", trades=156, sharpe=-8.485, total_return=-33.9, is_trades=268)

    tf, rid, metrics = _best_sweep_result(sid, "4h")
    assert tf == "4h"
    assert rid is None
    assert metrics == {}


def test_declared_tf_read_from_immutable_params_over_hijacked_column(forven_db):
    """After a prior crowning persisted timeframe='1h' onto the strategy row,
    the author's declaration must still come from params._timeframe — otherwise
    the bias defends the previously-crowned context instead of the author's."""
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION

    sid = "S-SWPD7"
    strategy_params = {"_timeframe": "4h", "kc_period": 10}
    _insert_strategy(sid, "1h")  # column already hijacked to 1h
    _insert_bt(sid, "bt-4h", "4h", trades=30, sharpe=1.5, total_return=6.0)
    _insert_bt(sid, "bt-1h", "1h", trades=100, sharpe=0.8, total_return=3.0)
    with get_db() as conn:
        conn.execute(
            "UPDATE backtest_results SET config_json = ? WHERE strategy_id = ?",
            (
                json.dumps(
                    {"engine_version": BACKTEST_ENGINE_VERSION, "params": strategy_params}
                ),
                sid,
            ),
        )
        conn.commit()

    tf, rid, _metrics = _best_sweep_result(
        sid, "1h", params=strategy_params, since=None, as_of=None
    )
    assert tf == "4h", "params._timeframe must define the declared context"
    assert rid == "bt-4h"


def test_positive_offtf_still_beats_positive_declared(forven_db):
    sid = "S-SWPD2"
    _insert_strategy(sid, "4h")
    _insert_bt(sid, "bt-4h", "4h", trades=30, sharpe=1.0, total_return=4.0)
    _insert_bt(sid, "bt-1h", "1h", trades=100, sharpe=2.0, total_return=12.0)

    tf, rid, _metrics = _best_sweep_result(sid, "4h")
    assert tf == "1h", "a genuinely better positive off-declared TF must still win"
    assert rid == "bt-1h"


def test_declared_positive_beats_weaker_offtf(forven_db):
    sid = "S-SWPD3"
    _insert_strategy(sid, "4h")
    _insert_bt(sid, "bt-4h", "4h", trades=30, sharpe=1.5, total_return=6.0)
    _insert_bt(sid, "bt-1h", "1h", trades=100, sharpe=0.8, total_return=3.0)

    tf, rid, _metrics = _best_sweep_result(sid, "4h")
    assert tf == "4h"
    assert rid == "bt-4h"


def test_negative_offtf_never_displaces_positive_declared(forven_db):
    """Even when the off-declared score is numerically higher (less negative /
    more trades), a non-positive-Sharpe context cannot displace the declared TF."""
    sid = "S-SWPD4"
    _insert_strategy(sid, "4h")
    _insert_bt(sid, "bt-4h", "4h", trades=12, sharpe=0.05, total_return=0.2)
    _insert_bt(sid, "bt-1h", "1h", trades=100, sharpe=-0.01, total_return=1.5)

    tf, _rid, _metrics = _best_sweep_result(sid, "4h")
    assert tf == "4h"


def test_asof_pinned_excludes_unpinned_rows(forven_db):
    """The as_of currency pin is NOT relaxed: an as_of=None pre-existing row stays
    excluded when the sweep runs pinned, even if it is the declared timeframe."""
    sid = "S-SWPD5"
    _insert_strategy(sid, "4h")
    # Pre-existing positive declared-TF row WITHOUT the pin (config as_of absent
    # and params mismatch is avoided by passing params={} + config {}).
    _insert_bt(sid, "bt-4h-unpinned", "4h", trades=40, sharpe=2.5, total_return=9.0)
    with get_db() as conn:
        conn.execute(
            "UPDATE backtest_results SET config_json = ? WHERE result_id = 'bt-4h-unpinned'",
            (json.dumps({"base_params": {}, "as_of": None}),),
        )
        conn.commit()

    tf, rid, metrics = _best_sweep_result(
        sid, "4h", params={}, since=None, as_of="2026-07-11T16:04:08+00:00"
    )
    # The unpinned row must not be selected; with no eligible rows at all the
    # fallback timeframe comes back with empty metrics.
    assert rid is None
    assert metrics == {}
    assert tf == "4h"
