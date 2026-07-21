"""LIVE-CLAMP-1: the per-trade risk backstop in scanner._execute_direct.

Kernel mirror-parity sizing deliberately skips the risk-budget caps
(enforce_risk_caps=False), so before this clamp NOTHING bounded a single live
order's loss-at-stop as a fraction of real equity — only the GO-LIVE notional
ceiling and the aggregate portfolio budget. These tests pin the final backstop:
every real-capital open must satisfy loss-at-stop <= max_risk_per_trade x real
equity, and the check fails closed when the cap or equity can't be resolved.
"""

import pytest


def _call_open(monkeypatch, *, size, price=100.0, stop=98.0, cap=0.02,
               equity=1000.0, testnet=False):
    import forven.scanner as scanner

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(scanner, "_resolve_hyperliquid_testnet", lambda: testnet)
    monkeypatch.setattr(scanner, "_resolve_trade_vault_address", lambda tid, strict=True: None)
    monkeypatch.setattr(scanner, "get_risk_status", lambda: {"limits": {"max_risk_per_trade": cap}})
    monkeypatch.setattr(scanner, "_get_real_account_equity", lambda: equity)
    monkeypatch.setattr("forven.exchange.risk.is_trading_allowed", lambda: (True, "ok"))

    # Sentinel: reaching the leverage-set call means every pre-exchange guard
    # (including the clamp) passed.
    def _reached_exchange(*args, **kwargs):
        raise RuntimeError("REACHED_EXCHANGE")

    monkeypatch.setattr("forven.exchange.hyperliquid.set_leverage", _reached_exchange)

    return scanner._execute_direct(
        "open", "T-CLAMP-1", "S-CLAMP-1", "BTC", "long", size, price,
        stop_loss=stop, take_profit=None, leverage=1.0,
    )


class TestLiveRiskClampBackstop:
    def test_oversized_open_is_refused(self, forven_db, monkeypatch):
        # price 100, stop 98 -> $2/unit at stop; 15 units = $30 loss-at-stop
        # vs cap $20 (2% of $1000): must refuse before touching the exchange.
        with pytest.raises(RuntimeError) as err:
            _call_open(monkeypatch, size=15.0)
        assert "loss-at-stop" in str(err.value)
        assert "REACHED_EXCHANGE" not in str(err.value)

    def test_within_cap_open_passes_the_clamp(self, forven_db, monkeypatch):
        # 5 units = $10 loss-at-stop vs cap $20: the clamp passes and the call
        # proceeds to the exchange (sentinel).
        with pytest.raises(RuntimeError) as err:
            _call_open(monkeypatch, size=5.0)
        assert "REACHED_EXCHANGE" in str(err.value)

    def test_unresolvable_equity_fails_closed(self, forven_db, monkeypatch):
        with pytest.raises(RuntimeError) as err:
            _call_open(monkeypatch, size=1.0, equity=None)
        assert "fail closed" in str(err.value)

    def test_missing_cap_defaults_conservatively(self, forven_db, monkeypatch):
        # Limits without max_risk_per_trade fall back to the 2% default cap
        # (the pre-existing guard skipped the check entirely): 15 units = $30
        # loss-at-stop vs $20 default budget -> refused; 5 units passes.
        with pytest.raises(RuntimeError) as err:
            _call_open(monkeypatch, size=15.0, cap=None)
        assert "loss-at-stop" in str(err.value)
        with pytest.raises(RuntimeError) as err:
            _call_open(monkeypatch, size=5.0, cap=None)
        assert "REACHED_EXCHANGE" in str(err.value)

    def test_testnet_orders_are_exempt(self, forven_db, monkeypatch):
        # No real capital at risk on testnet — oversized opens still reach the
        # exchange (the testnet harness sizes its own probes).
        with pytest.raises(RuntimeError) as err:
            _call_open(monkeypatch, size=15.0, testnet=True)
        assert "REACHED_EXCHANGE" in str(err.value)
