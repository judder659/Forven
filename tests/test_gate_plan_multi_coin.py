"""Gate plan 6/6: multi-coin paper containers (forven.multi_coin)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

import forven.evolution as evolution
import forven.policy as policy
import forven.scanner as scanner
from forven import multi_coin
from forven.db import get_db, kv_get, kv_set

pytestmark = pytest.mark.gate_plan_defaults


def _row(asset, *, trades=10, ret=5.0, sharpe=1.0, error=None):
    if error:
        return {"asset": asset, "error": error}
    return {"asset": asset, "trades": trades, "return_pct": ret, "sharpe": sharpe}


def _breadth(verdict="general", *, stale=False, status="done", rows=None, home="BTC"):
    return {
        "status": status, "stale": stale, "home": home, "finished_at": "2026-10-19T10:00:00+00:00",
        "summary": {"verdict": verdict, "home": home},
        "rows": rows if rows is not None else [
            _row("BTC", sharpe=1.5),
            _row("ETH", sharpe=0.9),
            _row("SOL", sharpe=2.1),
            _row("XRP", ret=-3.0, sharpe=-0.5),  # lost money: not traded
            _row("DOGE", trades=0, ret=0.0),  # no trades: not traded
            _row("ZEC", error="funding feed missing"),  # untestable: not traded
            _row("ADA", sharpe=0.4),
        ],
    }


# --- choosing and freezing the coin list --------------------------------------------


def test_coins_are_home_first_then_winners_by_sharpe():
    assert multi_coin.choose_coins(_breadth(), 6) == ["BTC", "SOL", "ETH", "ADA"]
    assert multi_coin.choose_coins(_breadth(), 3) == ["BTC", "SOL", "ETH"]


@pytest.mark.parametrize(
    "state",
    [
        _breadth(verdict="mixed"),
        _breadth(verdict="home_only"),
        _breadth(verdict="too_few"),
        _breadth(stale=True),
        _breadth(status="running"),
        _breadth(rows=[_row("BTC"), _row("ETH", ret=-1.0)]),  # no other winner: one coin
        {"status": "none"},
    ],
)
def test_no_container_without_a_current_general_verdict(state):
    assert multi_coin.choose_coins(state, 6) is None


def test_frozen_list_must_still_fit_the_home_coin():
    record = multi_coin.frozen_record(["BTC", "SOL"], _breadth(), "2026-10-19T10:00:00+00:00")
    assert multi_coin.coins_from_record(record, "BTC") == ["BTC", "SOL"]
    assert multi_coin.coins_from_record(record, "ETH") is None
    assert multi_coin.coins_from_record({"coins": ["BTC"]}, "BTC") is None
    assert multi_coin.coins_from_record(None, "BTC") is None


def test_settings_defaults_bounds_and_strict_preset():
    assert multi_coin.settings({}) == {"multi_coin_enabled": True, "multi_coin_max_coins": 6}
    assert multi_coin.settings({"paper_trading": {"multi_coin_max_coins": 99}})["multi_coin_max_coins"] == 15
    assert multi_coin.settings({"paper_trading": {"multi_coin_enabled": "off"}})["multi_coin_enabled"] is False
    strict = {"paper_door": {"mode": "strict"}, "paper_trading": {"multi_coin_enabled": True}}
    assert multi_coin.settings(strict)["multi_coin_enabled"] is False


def test_pipeline_defaults_carry_the_switch(forven_db):
    cfg = policy.load_pipeline_config()
    assert cfg["paper_trading"]["multi_coin_enabled"] is True
    assert cfg["paper_trading"]["multi_coin_max_coins"] == 6


def _insert_paper_strategy(conn, sid, *, symbol="BTC/USDT", days=10):
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO strategies (id, name, type, symbol, stage, status, stage_changed_at, created_at) "
        "VALUES (?, ?, 'rsi_reversion', ?, 'paper', 'paper', ?, ?)",
        (sid, sid, symbol, (now - timedelta(days=days)).isoformat(), (now - timedelta(days=days + 5)).isoformat()),
    )


def test_tick_freezes_once_and_never_rewrites(forven_db, monkeypatch):
    with get_db() as conn:
        _insert_paper_strategy(conn, "S-MC1")
        _insert_paper_strategy(conn, "S-MC2", symbol="ETH/USDT")
    states = {"S-MC1": _breadth(), "S-MC2": _breadth(verdict="mixed", home="ETH")}
    monkeypatch.setattr(policy, "strategy_breadth", lambda sid: states.get(sid, {"status": "none"}))

    created = evolution.paper_multi_coin_tick(copy.deepcopy(policy.DEFAULT_PIPELINE_CONFIG))
    assert created == [{"strategy_id": "S-MC1", "coins": ["BTC", "SOL", "ETH", "ADA"]}]
    record = kv_get(multi_coin.kv_key("S-MC1"))
    assert record["coins"] == ["BTC", "SOL", "ETH", "ADA"] and record["breadth_verdict"] == "general"
    assert kv_get(multi_coin.kv_key("S-MC2")) is None

    # A later breadth run (other winners) does not change a frozen list.
    states["S-MC1"] = _breadth(rows=[_row("BTC"), _row("AVAX"), _row("NEAR")])
    assert evolution.paper_multi_coin_tick(copy.deepcopy(policy.DEFAULT_PIPELINE_CONFIG)) == []
    assert kv_get(multi_coin.kv_key("S-MC1"))["coins"] == ["BTC", "SOL", "ETH", "ADA"]


def test_tick_does_nothing_when_switched_off_or_strict(forven_db, monkeypatch):
    with get_db() as conn:
        _insert_paper_strategy(conn, "S-MC3")
    monkeypatch.setattr(policy, "strategy_breadth", lambda sid: _breadth())
    off = copy.deepcopy(policy.DEFAULT_PIPELINE_CONFIG)
    off["paper_trading"]["multi_coin_enabled"] = False
    assert evolution.paper_multi_coin_tick(off) == []
    strict = copy.deepcopy(policy.DEFAULT_PIPELINE_CONFIG)
    strict["paper_door"]["mode"] = "strict"
    assert evolution.paper_multi_coin_tick(strict) == []
    assert kv_get(multi_coin.kv_key("S-MC3")) is None


# --- one record, counted in days ------------------------------------------------------


def test_pool_by_day_weights_each_leg_by_its_share():
    rows = [
        ("2026-10-10T01:00:00+00:00", 0.03, 0.25),
        ("2026-10-10T05:00:00+00:00", 0.01, 0.25),
        ("2026-10-11T02:00:00+00:00", -0.02, 0.25),
        ("2026-10-09T02:00:00+00:00", 0.04, 1.0),  # before the freeze: the whole strategy
        ("2026-10-12T02:00:00+00:00", "nan", 0.25),
    ]
    assert multi_coin.pool_by_day(rows) == pytest.approx([0.04, 0.01, -0.005])


def test_leg_weight_reads_signal_data():
    assert multi_coin.leg_weight({"container_weight": 0.25}) == 0.25
    assert multi_coin.leg_weight({}) == 1.0
    assert multi_coin.leg_weight({"container_weight": 3}) == 1.0
    assert multi_coin.leg_weight(["not", "a", "dict"]) == 1.0


def _insert_closed(conn, sid, i, asset, pnl, closed_at, weight=None):
    sd = {"pnl_is_equity_fraction": 1}
    if weight is not None:
        sd.update(container_weight=weight, container_coins=["BTC", "ETH"])
    conn.execute(
        "INSERT INTO trades (id, strategy_id, strategy, asset, direction, status, pnl_pct, execution_type, "
        "opened_at, closed_at, signal_data) VALUES (?, ?, ?, ?, 'long', 'CLOSED', ?, 'paper', ?, ?, ?)",
        (f"t-{sid}-{i}", sid, sid, asset, pnl, closed_at, closed_at, json.dumps(sd)),
    )


def test_container_forward_record_is_pooled_per_day(forven_db):
    day = datetime.now(timezone.utc).replace(hour=1, minute=0, second=0, microsecond=0) - timedelta(days=3)
    with get_db() as conn:
        _insert_paper_strategy(conn, "S-MC4")
        _insert_paper_strategy(conn, "S-SINGLE")
        for i, (asset, pnl, offset) in enumerate((("BTC", 0.02, 0), ("ETH", 0.04, 2), ("BTC", -0.01, 26))):
            when = (day + timedelta(hours=offset)).isoformat()
            _insert_closed(conn, "S-MC4", i, asset, pnl, when, weight=0.5)
            _insert_closed(conn, "S-SINGLE", i, "BTC", pnl, when)
    kv_set(multi_coin.kv_key("S-MC4"), multi_coin.frozen_record(["BTC", "ETH"], _breadth(), day.isoformat()))

    assert policy.strategy_container_coins("S-MC4") == ["BTC", "ETH"]
    assert policy.strategy_container_coins("S-SINGLE") is None
    assert policy.live_forward_pnls("S-MC4", None) == pytest.approx([0.03, -0.005])
    assert policy.live_forward_pnls("S-SINGLE", None) == pytest.approx([0.02, 0.04, -0.01])


def test_live_review_hold_is_not_merit():
    code = policy._extract_reason_code(
        "Multi-coin live review pending: this strategy trades 3 coins (BTC, SOL, ETH) in paper"
    )
    assert code == "multi_coin_live_review"
    assert code in policy._EVIDENCE_ABSENCE_REASON_CODES


# --- scanner legs ----------------------------------------------------------------------


def test_a_leg_is_the_same_rule_on_another_coin():
    strat = {
        "asset": "BTC", "symbol": "BTC/USDT", "timeframe": "4h",
        "params": {"lookback": 20, "_asset": "BTC"},
        "execution_contract": {"asset": "BTC", "params": {"lookback": 20, "_asset": "BTC"}, "leverage": 2},
    }
    leg = scanner._container_leg(strat, "SOL", ["BTC", "SOL", "ETH", "ADA"])
    assert leg["asset"] == "SOL" and leg["symbol"] == "SOL/USDT"
    assert leg["params"] == {"lookback": 20, "_asset": "SOL"}
    assert leg["execution_contract"]["asset"] == "SOL"
    assert leg["execution_contract"]["params"]["_asset"] == "SOL"
    assert leg["execution_contract"]["leverage"] == 2
    assert leg["container_weight"] == pytest.approx(0.25)
    assert strat["params"]["_asset"] == "BTC"  # the strategy itself is untouched
    assert scanner._container_leg_asset(leg) == "SOL"
    assert scanner._container_leg_asset(strat) is None


def test_added_coins_record_from_the_freeze_not_from_paper_entry():
    strat = {"asset": "BTC", "symbol": "BTC/USDT", "params": {}, "stage_changed_at": "2026-09-01T00:00:00+00:00"}
    frozen = "2026-10-15T08:00:00+00:00"
    home = scanner._container_leg(strat, "BTC", ["BTC", "SOL"], frozen)
    added = scanner._container_leg(strat, "SOL", ["BTC", "SOL"], frozen)
    assert home["stage_changed_at"] == "2026-09-01T00:00:00+00:00"
    assert scanner._parse_iso_ts(added["stage_changed_at"]) == scanner._parse_iso_ts(frozen)
    # Frozen before the strategy (re-)entered paper: the later paper entry wins.
    later = {**strat, "stage_changed_at": "2026-10-18T00:00:00+00:00"}
    assert scanner._container_leg(later, "SOL", ["BTC", "SOL"], frozen)["stage_changed_at"] == later["stage_changed_at"]


def test_leg_sizes_on_its_share_of_paper_equity(monkeypatch):
    monkeypatch.setattr(scanner, "_get_paper_strategy_equity", lambda sid: 10_000.0)
    assert scanner._paper_sizing_equity("S1", {"asset": "BTC"}) == 10_000.0
    leg = {"asset": "ETH", "container_coins": ["BTC", "ETH"], "container_weight": 0.5}
    assert scanner._paper_sizing_equity("S1", leg) == 5_000.0


def test_margin_and_backfill_dedupe_are_per_coin(forven_db, monkeypatch):
    monkeypatch.setattr(scanner, "_get_open_trades", lambda sid: [
        {"asset": "BTC", "size": 1.0, "entry_price": 100.0, "leverage": 1, "execution_type": "paper"},
        {"asset": "ETH", "size": 2.0, "entry_price": 50.0, "leverage": 2, "execution_type": "paper"},
    ])
    assert scanner._kernel_occupied_margin("S1", "paper") == pytest.approx(150.0)
    assert scanner._kernel_occupied_margin("S1", "paper", asset="ETH") == pytest.approx(50.0)

    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy_id, strategy, asset, direction, status, execution_type, signal_data) "
            "VALUES ('t-k1', 'S-K', 'S-K', 'BTC', 'long', 'CLOSED', 'paper', ?)",
            (json.dumps({"kernel_entry_time": "2026-10-10 04:00:00+00:00"}),),
        )
    stamp = "2026-10-10 04:00:00+00:00"
    assert scanner._kernel_trade_exists("S-K", "long", stamp)
    assert scanner._kernel_trade_exists("S-K", "long", stamp, asset="BTC")
    assert not scanner._kernel_trade_exists("S-K", "long", stamp, asset="ETH")


def test_container_runs_every_leg_and_combines_actions(monkeypatch):
    calls: list[tuple[str, float]] = []

    def fake_kernel(sid, strat, *, account_equity=None, execution_type="paper", diagnostics=None):
        calls.append((strat["asset"], strat["container_weight"]))
        if strat["asset"] == "ETH":
            return scanner.KERNEL_SKIP_SCAN  # this leg's data is down: the others still run
        if diagnostics is not None:
            diagnostics[sid] = {"execution_decision": "kernel_managed"}
        return [f"OPEN {strat['asset']}"]

    monkeypatch.setattr(scanner, "manage_positions_via_kernel", fake_kernel)
    diag: dict = {}
    strat = {"asset": "BTC", "symbol": "BTC/USDT", "params": {}}
    out = scanner.manage_container_via_kernel("S1", strat, ["BTC", "ETH", "SOL"], diagnostics=diag)
    assert out == ["OPEN BTC", "OPEN SOL"]
    assert [c[0] for c in calls] == ["BTC", "ETH", "SOL"]
    assert all(w == pytest.approx(1 / 3) for _a, w in calls)
    assert diag["S1"]["execution_decision"] == "kernel_managed_container"
    assert diag["S1"]["coins"] == ["BTC", "ETH", "SOL"]


def test_container_defers_to_single_coin_handling_when_the_kernel_cannot_run(monkeypatch):
    monkeypatch.setattr(scanner, "manage_positions_via_kernel", lambda *a, **k: None)
    assert scanner.manage_container_via_kernel("S1", {"asset": "BTC", "params": {}}, ["BTC", "ETH"]) is None
    monkeypatch.setattr(scanner, "manage_positions_via_kernel", lambda *a, **k: scanner.KERNEL_SKIP_SCAN)
    assert scanner.manage_container_via_kernel("S1", {"asset": "BTC", "params": {}}, ["BTC", "ETH"]) is scanner.KERNEL_SKIP_SCAN


def test_scanner_reads_the_frozen_list_only_while_switched_on(forven_db):
    strat = {"asset": "BTC", "symbol": "BTC/USDT"}
    assert scanner._paper_container_coins("S-MC5", strat) is None
    kv_set(multi_coin.kv_key("S-MC5"), multi_coin.frozen_record(["BTC", "SOL"], _breadth(), "2026-10-19"))
    assert scanner._paper_container_coins("S-MC5", strat) == ["BTC", "SOL"]
    kv_set("forven:pipeline_thresholds", {"paper_trading": {"multi_coin_enabled": False}})
    assert scanner._paper_container_coins("S-MC5", strat) is None


# --- Propr ------------------------------------------------------------------------------


def test_propr_mirror_skips_container_legs(forven_db):
    from forven import propr_mirror

    with get_db() as conn:
        legs = (("t-single", "BTC", {}), ("t-leg", "ETH", {"container_weight": 0.5, "container_coins": ["BTC", "ETH"]}))
        for tid, asset, sd in legs:
            conn.execute(
                "INSERT INTO trades (id, strategy_id, strategy, asset, direction, status, execution_type, signal_data) "
                "VALUES (?, 'S-PR', 'S-PR', ?, 'long', 'OPEN', 'paper', ?)",
                (tid, asset, json.dumps(sd)),
            )
    assert [row["id"] for row in propr_mirror._roster_trades({"S-PR"})] == ["t-single"]
