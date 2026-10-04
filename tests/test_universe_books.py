"""Universe books (forven.universe): panel sealing, causal weights, the daily
simulation's arithmetic, the forward book's no-backfill lifecycle, and gating."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from forven.db import kv_set
from forven.universe import engine
from forven.universe.panel import DailyPanel
from forven.universe.strategies import BOOKS, TrendBlendSpec, coin_positions, target_weights

LONG_FLAT = BOOKS["trend_blend_long_flat"]
LONG_SHORT = BOOKS["trend_blend_long_short"]


def _closes(days: int = 700, seed: int = 7, symbols: tuple[str, ...] = ("AAA-USDT", "BBB-USDT", "CCC-USDT")) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2022-01-01", periods=days, freq="D", tz="UTC")
    drifts = np.linspace(0.002, -0.002, len(symbols))  # one coin up, one down
    data = {
        symbol: 100.0 * np.exp(np.cumsum(drift + 0.03 * rng.standard_normal(days)))
        for symbol, drift in zip(symbols, drifts)
    }
    return pd.DataFrame(data, index=index)


def _panel(close: pd.DataFrame, funding: float = 0.0) -> DailyPanel:
    return DailyPanel(close=close, funding=pd.DataFrame(funding, index=close.index, columns=close.columns))


# ------------------------------------------------------------------ panel


def _patch_lake(monkeypatch, hours: int, per_hour_funding: float = 0.0001) -> pd.DatetimeIndex:
    import forven.basket_lab as basket_lab
    import forven.universe.panel as panel_mod

    index = pd.date_range("2026-03-01", periods=hours, freq="h", tz="UTC")

    def fake_hourly(symbol, start):
        return pd.Series(np.arange(len(index), dtype=float) + 1.0, index=index)

    def fake_funding(symbol, hourly_index):
        return pd.Series(per_hour_funding, index=hourly_index)

    monkeypatch.setattr(panel_mod, "_hourly_close", fake_hourly)
    monkeypatch.setattr(basket_lab, "_per_hour_funding_series", fake_funding)
    return index


def test_panel_keeps_complete_days_and_sums_funding(monkeypatch):
    from forven.universe.panel import load_daily_panel

    index = _patch_lake(monkeypatch, hours=24 * 5 + 6)  # 5 full days + 6h of a 6th
    panel = load_daily_panel(["AAA-USDT"], sealed=False, now=index[-1])
    assert panel.cutoff is None
    assert list(panel.close.index.strftime("%Y-%m-%d")) == ["2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]
    # Each day's close is its last hourly close (hour 23).
    assert panel.close["AAA-USDT"].iloc[0] == 24.0
    assert panel.funding["AAA-USDT"].iloc[0] == pytest.approx(24 * 0.0001)


def test_sealed_panel_ends_at_last_complete_day_before_a_midday_cutoff(monkeypatch):
    import forven.research_contract as research_contract
    from forven.universe.panel import load_daily_panel

    index = _patch_lake(monkeypatch, hours=24 * 8)
    monkeypatch.setattr(research_contract, "research_read_cutoff", lambda: pd.Timestamp("2026-03-05T12:00:00"))
    panel = load_daily_panel(["AAA-USDT"], sealed=True, now=index[-1])
    assert panel.cutoff == pd.Timestamp("2026-03-05T12:00:00", tz="UTC")
    assert panel.close.index.max() == pd.Timestamp("2026-03-04", tz="UTC")


# ------------------------------------------------------------------ weights


def test_weights_are_causal():
    close = _closes()
    base = target_weights(LONG_SHORT, close)
    cut = 450
    perturbed = close.copy()
    perturbed.iloc[cut + 1 :] *= np.random.default_rng(1).uniform(0.5, 1.5, size=perturbed.iloc[cut + 1 :].shape)
    again = target_weights(LONG_SHORT, perturbed)
    pd.testing.assert_frame_equal(base.iloc[: cut + 1], again.iloc[: cut + 1])
    assert not base.iloc[cut + 1 :].equals(again.iloc[cut + 1 :])


def test_long_flat_never_shorts_and_long_short_does():
    close = _closes()
    assert (target_weights(LONG_FLAT, close) >= 0.0).all().all()
    assert (target_weights(LONG_SHORT, close) < 0.0).any().any()


def test_book_is_flat_while_warming_and_scale_is_capped():
    close = _closes()
    weights = target_weights(LONG_SHORT, close)
    # Coin positions need 36 days of vol; the book's own vol estimate 40 more.
    assert (weights.iloc[:36] == 0.0).all().all()
    positions = coin_positions(LONG_SHORT, close)
    live = positions.notna()
    raw_gross = positions.where(live, 0.0).abs().sum(axis=1) / live.sum(axis=1).replace(0, np.nan)
    gross = weights.abs().sum(axis=1)
    assert (gross <= LONG_SHORT.max_book_leverage * raw_gross.fillna(0.0) + 1e-12).all()


def test_short_gap_holds_the_position_and_long_gap_drops_it():
    close = _closes()
    gappy = close.copy()
    gappy.iloc[500:502, 0] = np.nan  # 2 missing days: held
    gappy.iloc[600:610, 1] = np.nan  # 10 missing days: dropped after 3
    positions = coin_positions(LONG_SHORT, gappy)
    assert positions.iloc[500:502, 0].notna().all()
    assert positions.iloc[600:603, 1].notna().all()
    assert positions.iloc[603:610, 1].isna().all()


# ------------------------------------------------------------------ simulation


def test_simulate_arithmetic():
    spec = TrendBlendSpec("t", "t", "both")
    index = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    close = pd.DataFrame({"A": [100.0, 100.0, 110.0, 110.0], "B": [50.0, 50.0, 45.0, 45.0]}, index=index)
    funding = pd.DataFrame({"A": [0.0, 0.0, 0.001, 0.0], "B": [0.0, 0.0, 0.002, 0.0]}, index=index)
    weights = pd.DataFrame({"A": [0.0, 0.5, 0.5, 0.5], "B": [0.0, -0.5, -0.5, -0.5]}, index=index)
    run = engine.simulate(spec, DailyPanel(close=close, funding=funding), weights)
    cost = spec.cost_bps / 1e4
    # Day 1: entry at the close pays costs on turnover 1.0, nothing held yet.
    assert run.daily["net"].iloc[1] == pytest.approx(-1.0 * cost)
    # Day 2: long A +10%, short B -10%; long pays 0.1% funding, short receives 0.2%.
    expected = 0.5 * 0.10 + (-0.5) * (-0.10) - (0.5 * 0.001 + (-0.5) * 0.002)
    assert run.daily["net"].iloc[2] == pytest.approx(expected)
    assert run.daily["turnover"].iloc[2] == 0.0
    # Per-coin contributions add up to the book.
    assert run.contribution.sum(axis=1).to_numpy() == pytest.approx(run.daily["net"].to_numpy())


def test_double_costs_lower_returns_and_placebo_is_deterministic():
    from forven.universe.research import evaluate

    close = _closes(days=900)
    panel = _panel(close, funding=0.0001)
    first = evaluate(LONG_FLAT, panel, start=close.index[300])
    second = evaluate(LONG_FLAT, panel, start=close.index[300])
    assert first["placebo"] == second["placebo"]
    assert first["costs_2x"]["return_pct"] < first["summary"]["return_pct"]
    assert len(first["leave_one_out"]) == 3
    assert {row["symbol"] for row in first["coin_contributions"]} == set(close.columns)


def test_report_drops_non_finite_numbers():
    from forven.universe.research import _finite

    assert _finite({"a": float("nan"), "b": [1.0, float("inf")], "c": "x"}) == {"a": None, "b": [1.0, None], "c": "x"}


# ------------------------------------------------------------------ forward book


def _now(day: str, hour: int = 10) -> datetime:
    stamp = pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=hour)
    return stamp.to_pydatetime().astimezone(timezone.utc)


def test_book_starts_flat_and_never_backfills():
    from forven.universe.book import tick_book

    close = _closes()
    start_today = close.index[500]
    state, report = tick_book(LONG_SHORT, None, _panel(close.loc[: start_today - pd.Timedelta(days=1)]), _now(str(start_today.date())))
    assert report["started"] is True
    assert state["start_day"] == engine.day_label(start_today - pd.Timedelta(days=1))
    assert state["history"] == [] and state["weights"] == {}

    # Same day again: nothing new to book.
    unchanged, report = tick_book(LONG_SHORT, state, _panel(close.loc[: start_today - pd.Timedelta(days=1)]), _now(str(start_today.date()), 20))
    assert unchanged is None and report["reason"] == "up to date"

    # Three days later: exactly the three closes after the start are booked.
    later = start_today + pd.Timedelta(days=3)
    panel = _panel(close.loc[: later - pd.Timedelta(days=1)])
    state, report = tick_book(LONG_SHORT, state, panel, _now(str(later.date()), 2))
    assert report["days"] == 3
    assert [row["day"] for row in state["history"]] == [engine.day_label(start_today + pd.Timedelta(days=i)) for i in range(3)]
    # First booked day: nothing was held, so only the entry cost.
    assert state["history"][0]["net"] <= 0.0
    assert state["equity"] == pytest.approx(np.prod([1.0 + row["net"] for row in state["history"]]), rel=1e-6)

    # Days after the first match the research simulation exactly.
    run = engine.simulate(LONG_SHORT, panel, target_weights(LONG_SHORT, panel.close))
    for row in state["history"][1:]:
        assert row["net"] == pytest.approx(float(run.daily["net"].loc[pd.Timestamp(row["day"], tz="UTC")]), abs=1e-8)

    again, report = tick_book(LONG_SHORT, state, panel, _now(str(later.date()), 5))
    assert again is None


def test_book_waits_for_a_lagging_coin():
    from forven.universe.book import tick_book

    close = _closes()
    today = close.index[520]
    state, _ = tick_book(LONG_SHORT, None, _panel(close.loc[: close.index[510]]), _now(str(close.index[511].date())))
    lagging = close.loc[: today - pd.Timedelta(days=1)].copy()
    lagging.iloc[-3:, 1] = np.nan  # BBB is 3 closes behind
    new_state, report = tick_book(LONG_SHORT, state, _panel(lagging), _now(str(today.date())))
    assert report["days"] == len(close.loc[close.index[511] : today - pd.Timedelta(days=4)])
    assert new_state["last_day"] == engine.day_label(today - pd.Timedelta(days=4))
    stalled, report = tick_book(LONG_SHORT, new_state, _panel(lagging), _now(str(today.date()), 11))
    assert stalled is None and report["lagging"] == ["BBB-USDT"]


def test_book_refuses_a_changed_spec():
    from forven.universe.book import tick_book

    close = _closes()
    state, _ = tick_book(LONG_SHORT, None, _panel(close.loc[: close.index[500]]), _now(str(close.index[501].date())))
    state["spec_version"] = 99
    unchanged, report = tick_book(LONG_SHORT, state, _panel(close.loc[: close.index[505]]), _now(str(close.index[506].date())))
    assert unchanged is None and "spec version" in report["reason"]


def test_run_tick_is_gated_and_persists(forven_db, monkeypatch):
    import forven.universe.book as book_mod

    kv_set("forven:settings", {"universe_books_enabled": True})
    assert book_mod.run_universe_tick() == {"enabled": False, "books": []}  # portfolio layer off

    close = _closes(symbols=LONG_FLAT.symbols[:3])
    today = close.index[-1] + pd.Timedelta(days=1)
    monkeypatch.setattr(book_mod, "get_now", lambda: _now(str(today.date())))
    monkeypatch.setattr(book_mod, "load_daily_panel", lambda *a, **k: _panel(close))
    kv_set("forven:settings", {"portfolio_layer_enabled": True, "universe_books_enabled": True})
    report = book_mod.run_universe_tick()
    assert [b.get("started") for b in report["books"]] == [True, True]
    assert book_mod.get_book_state("trend_blend_long_flat")["start_day"] == engine.day_label(close.index[-1])

    summary = book_mod.book_summary("trend_blend_long_flat")
    assert summary["exists"] is True and summary["stats"]["days"] == 0
    assert book_mod.reset_book("trend_blend_long_flat")
    assert book_mod.get_book_state("trend_blend_long_flat") is None
    assert book_mod.book_summary("trend_blend_long_flat")["exists"] is False


# ------------------------------------------------------------------ gating


def test_routes_404_without_the_portfolio_layer(forven_db):
    from fastapi import HTTPException

    from forven.routers.universe import get_universe_book_research, get_universe_books

    kv_set("forven:settings", {})
    with pytest.raises(HTTPException) as off:
        get_universe_books()
    assert off.value.status_code == 404

    kv_set("forven:settings", {"portfolio_layer_enabled": True})
    payload = get_universe_books()
    assert payload["enabled"] is False
    assert [b["book"] for b in payload["books"]] == list(BOOKS)
    with pytest.raises(HTTPException) as unknown:
        get_universe_book_research("nope")
    assert unknown.value.status_code == 404


def test_job_is_seeded_with_the_portfolio_layer(forven_db):
    from forven import scheduler as sched

    kv_set("forven:settings", {})
    assert "forven-universe-books" not in sched._default_job_ids()
    kv_set("forven:settings", {"portfolio_layer_enabled": True})
    assert "forven-universe-books" in sched._default_job_ids()


def test_toggle_saves_through_the_risk_settings_section(forven_db):
    from forven.api_core import _apply_settings_section
    from forven.universe.book import universe_books_enabled

    kv_set("forven:settings", {"portfolio_layer_enabled": True})
    assert not universe_books_enabled()
    _apply_settings_section("risk", {"universe_books_enabled": True})
    assert universe_books_enabled()
    _apply_settings_section("risk", {"universe_books_enabled": False})
    assert not universe_books_enabled()
