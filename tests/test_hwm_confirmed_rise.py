"""HWM-CONFIRM-1 / EQ-SNAPSHOT-1: a one-tick upward equity read must not latch
the real-capital high-water mark.

2026-10-12: the kill switch measured 5.0% against an HWM of $1,044.59 that the
exchange never showed (real peak ~$1,033, ~3.9% drawdown). The HWM had stepped
up ~$25 at two position closes, where the perp read still held the isolated
margin and the spot read already showed it released.
"""

from datetime import timedelta

import pytest

import forven.exchange.hyperliquid as hl
from forven.exchange import risk


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: float("inf"))
    current = {"t": risk.get_now()}
    monkeypatch.setattr(risk, "get_now", lambda: current["t"])

    def advance(seconds: float) -> None:
        current["t"] = current["t"] + timedelta(seconds=seconds)

    return advance


def test_single_tick_spike_does_not_raise_hwm(forven_db, clock):
    risk.update_equity(1000.0)
    spike = risk.update_equity(1025.0)
    assert spike["high_water_mark"] == 1000.0
    assert spike["drawdown_pct"] == 0.0
    clock(2)
    after = risk.update_equity(999.5)
    assert after["high_water_mark"] == 1000.0
    clock(60)
    later = risk.update_equity(1001.0)
    assert later["high_water_mark"] == 1000.0  # spike candidate was discarded


def test_sustained_rise_commits_the_level_every_read_supports(forven_db, clock):
    risk.update_equity(1000.0)
    risk.update_equity(1030.0)
    clock(5)
    assert risk.update_equity(1012.0)["high_water_mark"] == 1000.0  # inside spacing
    clock(risk._HALT_CONFIRM_MIN_SPACING_SECONDS)
    confirmed = risk.update_equity(1015.0)
    assert confirmed["high_water_mark"] == 1012.0  # lowest read while pending
    clock(risk._HALT_CONFIRM_MIN_SPACING_SECONDS)
    assert risk.update_equity(1016.0)["high_water_mark"] == 1015.0


def test_degraded_reads_neither_start_nor_confirm_a_rise(forven_db, clock):
    risk.update_equity(1000.0)
    risk.update_equity(1040.0, degraded=True)
    clock(60)
    assert risk.update_equity(1040.0, degraded=True)["high_water_mark"] == 1000.0
    clock(60)
    assert risk.update_equity(1010.0)["high_water_mark"] == 1000.0  # first real candidate


def test_paper_source_still_raises_immediately(forven_db, clock):
    risk.update_equity(10000.0, source="paper")
    assert risk.update_equity(10200.0, source="paper")["high_water_mark"] == 10200.0


class _Info:
    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def user_state(self, address, dex=""):
        self.calls += 1
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


def _state(account_value: str, positions: dict[str, str]) -> dict:
    return {
        "marginSummary": {
            "accountValue": account_value,
            "totalMarginUsed": account_value,
            "totalNtlPos": "0.0",
            "totalRawUsd": "0.0",
        },
        "assetPositions": [{"position": {"coin": c, "szi": s}} for c, s in positions.items()],
    }


def _patch_reads(monkeypatch, info, spot_frees):
    frees = list(spot_frees)
    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl, "_get_account_info_client", lambda testnet: (info, "0xacct"))
    monkeypatch.setattr(hl, "_with_breaker", lambda name, br, fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(
        hl, "_extract_spot_usdc_balance",
        lambda i, a: (0.0, frees.pop(0) if len(frees) > 1 else frees[0]),
    )


def test_read_straddling_a_close_is_retaken(monkeypatch):
    # perp still shows the ETH short's $25.70 margin; spot already released it.
    info = _Info([_state("25.70", {"ETH": "-0.0191"}), _state("0.0", {}), _state("0.0", {})])
    _patch_reads(monkeypatch, info, [1019.0, 1019.0])
    acc = hl.get_account_value(testnet=True, account_address="0xSHORT")
    assert acc["accountValue"] == pytest.approx(1019.0)  # not 1044.70


def test_stable_open_position_costs_one_confirming_read(monkeypatch):
    info = _Info([_state("163.35", {"SOL": "-2.8"})])
    _patch_reads(monkeypatch, info, [340.43])
    acc = hl.get_account_value(testnet=True, account_address="0xSHORT")
    assert acc["accountValue"] == pytest.approx(503.78)
    assert info.calls == 2


def test_read_that_keeps_moving_fails_closed(monkeypatch):
    info = _Info([
        _state("25.0", {"ETH": "-0.02"}),
        _state("30.0", {"ETH": "-0.03"}),
        _state("35.0", {"ETH": "-0.04"}),
    ])
    _patch_reads(monkeypatch, info, [100.0])
    with pytest.raises(RuntimeError, match="straddled a fill"):
        hl.get_account_value(testnet=True, account_address="0xSHORT")
