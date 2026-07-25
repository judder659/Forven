from __future__ import annotations

import json

from forven import daemon
from forven.db import get_db
from forven.exchange.risk import reconcile_exchange_positions


def test_startup_recovery_preflight_blocks_on_reconciliation_issues(monkeypatch):
    state: dict[str, object] = {}

    monkeypatch.setattr(daemon, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(daemon, "_get_testnet", lambda: True)
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (True, None))
    monkeypatch.setattr(daemon, "sync_from_trades", lambda: 0)
    monkeypatch.setattr(
        daemon,
        "get_positions",
        lambda testnet=True: {
            "positions": [{"position": {"coin": "BTC", "szi": "0.00471"}}],
        },
    )
    monkeypatch.setattr(daemon, "get_open_orders", lambda testnet=True: [{"oid": 49994803084}])
    monkeypatch.setattr(
        daemon,
        "get_account_value",
        lambda testnet=True, require_connection=False: {
            "accountValue": 986.2,
            "totalMarginUsed": 16.19,
            "withdrawable": 986.2,
            "totalNtlPos": 323.17,
            "source": "exchange",
        },
    )
    monkeypatch.setattr(
        daemon,
        "reconcile_all_books",
        lambda testnet=True, **kwargs: {
            "sqlite_open": 0,
            "exchange_open": 1,
            "synced": False,
            "discrepancies": [
                {
                    "type": "missing_in_sqlite",
                    "details": "Exchange has long BTC size=0.00471 but no matching SQLite trade",
                }
            ],
        },
    )

    recovery = daemon.run_startup_recovery_preflight(state)

    assert recovery["recovery_active"] is True
    assert recovery["recovery_status"] == "blocked"
    assert recovery["recovery_position_count"] == 1
    assert recovery["recovery_discrepancy_count"] == 1
    assert recovery["recovery_requires_operator"] is True
    assert recovery["recovery_open_order_count"] == 1
    assert recovery["recovery_network"] == "testnet"
    assert "New entries remain blocked." in recovery["recovery_summary"]
    assert state["account_equity"] == 986.2
    # Subset match, not exact equality: the snapshot carries a per-book breakdown
    # ("books") alongside the aggregate, and pinning the whole dict makes every
    # future additive field a failure in a test that is about recovery blocking.
    account = state["exchange_account"]
    for key, expected in {
        "accountValue": 986.2,
        "totalMarginUsed": 16.19,
        "totalNtlPos": 323.17,
        "withdrawable": 986.2,
        "source": "exchange",
        "network": "testnet",
        "synced_at": state["account_equity_synced_at"],
    }.items():
        assert account[key] == expected, key


def test_startup_recovery_preflight_skips_without_credentials_in_paper(monkeypatch):
    state: dict[str, object] = {}

    monkeypatch.setattr(daemon, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(daemon, "_get_testnet", lambda: True)
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (False, "missing creds"))

    recovery = daemon.run_startup_recovery_preflight(state)

    assert recovery["recovery_active"] is False
    assert recovery["recovery_status"] == "skipped_no_credentials"
    assert recovery["recovery_requires_operator"] is False
    assert recovery["recovery_network"] == "testnet"


def test_periodic_reconcile_can_resolve_existing_recovery_block():
    state = {
        "recovery_active": True,
        "recovery_status": "blocked",
        "recovery_network": "testnet",
        "recovery_summary": "Startup recovery blocked by 1 discrepancy.",
    }

    daemon._update_recovery_state_from_reconcile(
        state,
        {"synced": True, "exchange_open": 0, "discrepancies": []},
        source="periodic",
    )

    assert state["recovery_active"] is False
    assert state["recovery_status"] == "resolved"
    assert state["recovery_requires_operator"] is False
    assert state["recovery_discrepancy_count"] == 0
    assert "resolved exchange recovery" in state["recovery_summary"]


def test_daemon_pid_probe_tolerates_windows_kill_errors(monkeypatch):
    def _boom(_pid: int, _sig: int):
        raise SystemError("WinError 87")

    monkeypatch.setattr(daemon.os, "kill", _boom)

    assert daemon._is_pid_running(12345) is False


def test_daemon_pid_probe_tolerates_windows_access_denied(monkeypatch):
    import sys

    class _Kernel32:
        def OpenProcess(self, *_args):
            return 0

        def CloseHandle(self, _handle):
            return 1

    class _Ctypes:
        windll = type("windll", (), {"kernel32": _Kernel32()})()

        @staticmethod
        def GetLastError():
            return 5

    monkeypatch.setattr(daemon.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "ctypes", _Ctypes)

    assert daemon._is_pid_running(12345) is True


def test_reconcile_can_adopt_missing_exchange_position_by_stop_order_id(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (
                id, strategy, strategy_name, strategy_id, asset, symbol, direction,
                entry_price, signal_entry_price, size, risk_pct, leverage, status,
                execution_type, source, signal_data, opened_at, closed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "E0095",
                "S00393",
                "BTC Strategy",
                "S00393",
                "BTC",
                "BTC",
                "long",
                69647.0,
                69647.0,
                0.00471,
                0.01,
                20.0,
                "CLOSED",
                "paper_challenger",
                "scanner",
                json.dumps({"exchange_stop_order_id": 49994803084, "stop_loss": 66440.0}),
                "2026-03-12T08:35:04+00:00",
                "2026-03-12T08:40:37+00:00",
            ),
        )

    monkeypatch.setattr(
        "forven.exchange.hyperliquid.get_positions",
        lambda testnet=True: {
            "positions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "0.00471",
                        "entryPx": "69647.0",
                        "leverage": {"type": "cross", "value": 20},
                    }
                }
            ]
        },
    )
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.get_open_orders",
        lambda testnet=True: [
            {
                "coin": "BTC",
                "oid": 49994803084,
                "reduceOnly": True,
                "timestamp": 1773304507192,
                "origSz": "0.00471",
                "sz": "0.00471",
            }
        ],
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 68613.0})

    recon = reconcile_exchange_positions(
        testnet=True,
        adopt_missing_in_sqlite=True,
        recovery_batch_id="batch-stop-match",
    )

    assert recon["synced"] is True
    assert recon["adopted_count"] == 1

    with get_db() as conn:
        recovered_trade = conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN' AND source = 'exchange_recovered'"
        ).fetchone()
        portfolio_position = conn.execute(
            "SELECT * FROM portfolio_positions WHERE trade_id = ?",
            (recovered_trade["id"],),
        ).fetchone()

    assert recovered_trade is not None
    recovered_signal = json.loads(recovered_trade["signal_data"] or "{}")
    assert recovered_signal["recovered_from_trade_id"] == "E0095"
    assert recovered_signal["exchange_stop_order_id"] == "49994803084"
    assert recovered_signal["recovery_batch_id"] == "batch-stop-match"
    assert recovered_signal["recovery_match_reason"].startswith("exchange_stop_order_id")
    assert recovered_signal["recovery_protection_status"] == "protected"
    # Provenance stamp (#33): recovered trades carry the same validated-source vs
    # traded-venue audit trail as live scanner trades.
    assert recovered_signal["execution_venue"] == "hyperliquid"
    assert recovered_signal["execution_mode"] == "recovered"
    assert "data_source" in recovered_signal
    assert portfolio_position is not None


def test_reconcile_can_adopt_missing_exchange_position_by_entry_order_id(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (
                id, strategy, strategy_name, strategy_id, asset, symbol, direction,
                entry_price, signal_entry_price, size, risk_pct, leverage, status,
                execution_type, source, signal_data, opened_at, closed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "E0096",
                "S00394",
                "BTC Strategy",
                "S00394",
                "BTC",
                "BTC",
                "long",
                69647.0,
                69647.0,
                0.00471,
                0.01,
                20.0,
                "CLOSED",
                "paper_challenger",
                "scanner",
                json.dumps({"entry_exchange_order_id": "entry-123", "stop_loss": 66440.0}),
                "2026-03-12T08:35:04+00:00",
                "2026-03-12T08:40:37+00:00",
            ),
        )

    monkeypatch.setattr(
        "forven.exchange.hyperliquid.get_positions",
        lambda testnet=True: {
            "positions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "0.00471",
                        "entryPx": "69647.0",
                        "entryOrderId": "entry-123",
                        "leverage": {"type": "cross", "value": 20},
                    }
                }
            ]
        },
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 68613.0})
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.place_protective_stop",
        lambda asset, position_direction, size, stop_loss_price, testnet=True: {
            "status": "ok",
            "stop_order_id": "entry-match-stop-1",
            "stop_loss": stop_loss_price,
        },
    )

    recon = reconcile_exchange_positions(
        testnet=True,
        adopt_missing_in_sqlite=True,
        recovery_batch_id="batch-entry-match",
    )

    assert recon["synced"] is True
    assert recon["adopted_count"] == 1

    with get_db() as conn:
        recovered_trade = conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN' AND source = 'exchange_recovered'"
        ).fetchone()

    assert recovered_trade is not None
    recovered_signal = json.loads(recovered_trade["signal_data"] or "{}")
    assert recovered_signal["recovered_from_trade_id"] == "E0096"
    assert recovered_signal["recovery_match_reason"].startswith("entry_exchange_order_id")
    assert recovered_signal["exchange_stop_order_id"] == "entry-match-stop-1"


def test_reconcile_requires_operator_when_recovery_match_is_ambiguous(forven_db, monkeypatch):
    with get_db() as conn:
        for trade_id in ("E0101", "E0102"):
            conn.execute(
                """
                INSERT INTO trades
                (
                    id, strategy, strategy_name, strategy_id, asset, symbol, direction,
                    entry_price, signal_entry_price, size, risk_pct, leverage, status,
                    execution_type, source, signal_data, opened_at, closed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    "S00401",
                    "BTC Strategy",
                    "S00401",
                    "BTC",
                    "BTC",
                    "long",
                    69000.0,
                    69000.0,
                    0.00471,
                    0.01,
                    20.0,
                    "CLOSED",
                    "paper_challenger",
                    "scanner",
                    json.dumps({}),
                    "2026-03-12T08:35:04+00:00",
                    "2026-03-12T08:40:37+00:00",
                ),
            )

    monkeypatch.setattr(
        "forven.exchange.hyperliquid.get_positions",
        lambda testnet=True: {
            "positions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "0.00471",
                        "entryPx": "69647.0",
                        "leverage": {"type": "cross", "value": 20},
                    }
                }
            ]
        },
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 68613.0})

    recon = reconcile_exchange_positions(
        testnet=True,
        adopt_missing_in_sqlite=True,
        recovery_batch_id="batch-ambiguous",
    )

    assert recon["synced"] is False
    assert recon["adopted_count"] == 0
    assert any(item["type"] == "ambiguous_recovery_match" for item in recon["discrepancies"])

    with get_db() as conn:
        recovered_count = conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE status = 'OPEN' AND source = 'exchange_recovered'"
        ).fetchone()["c"]

    assert recovered_count == 0


def test_startup_recovery_preflight_adopts_exchange_position_before_entries_resume(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (
                id, strategy, strategy_name, strategy_id, asset, symbol, direction,
                entry_price, signal_entry_price, size, risk_pct, leverage, status,
                execution_type, source, signal_data, opened_at, closed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "E0095",
                "S00393",
                "BTC Strategy",
                "S00393",
                "BTC",
                "BTC",
                "long",
                69647.0,
                69647.0,
                0.00471,
                0.01,
                20.0,
                "CLOSED",
                "paper_challenger",
                "scanner",
                json.dumps({"exchange_stop_order_id": 49994803084, "stop_loss": 66440.0}),
                "2026-03-12T08:35:04+00:00",
                "2026-03-12T08:40:37+00:00",
            ),
        )

    positions_payload = {
        "positions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.00471",
                    "entryPx": "69647.0",
                    "leverage": {"type": "cross", "value": 20},
                }
            }
        ]
    }
    open_orders_payload = [
        {
            "coin": "BTC",
            "oid": 49994803084,
            "reduceOnly": True,
            "timestamp": 1773304507192,
            "origSz": "0.00471",
            "sz": "0.00471",
        }
    ]

    monkeypatch.setattr(daemon, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(daemon, "_get_testnet", lambda: True)
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (True, None))
    monkeypatch.setattr(daemon, "get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr(daemon, "get_open_orders", lambda testnet=True: open_orders_payload)
    monkeypatch.setattr(
        daemon,
        "get_account_value",
        lambda testnet=True, require_connection=False: {"accountValue": 986.2},
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: open_orders_payload)
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 68613.0})

    recovery = daemon.run_startup_recovery_preflight({})

    assert recovery["recovery_active"] is False
    assert recovery["recovery_status"] == "ok"
    assert recovery["recovery_position_count"] == 1
    assert recovery["recovery_discrepancy_count"] == 0
    assert recovery["recovery_requires_operator"] is False
    assert "Adopted 1 exchange position" in recovery["recovery_summary"]

    with get_db() as conn:
        recovered_trade = conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN' AND source = 'exchange_recovered'"
        ).fetchone()
        portfolio_position = conn.execute(
            "SELECT * FROM portfolio_positions WHERE trade_id = ?",
            (recovered_trade["id"],),
        ).fetchone()

    assert recovered_trade is not None
    recovered_signal = json.loads(recovered_trade["signal_data"] or "{}")
    assert recovered_signal["recovery_protection_status"] == "protected"
    assert portfolio_position is not None


def test_startup_recovery_preflight_restores_missing_portfolio_positions(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (
                id, strategy, strategy_name, strategy_id, asset, symbol, direction,
                entry_price, signal_entry_price, size, risk_pct, leverage, status,
                execution_type, source, signal_data, opened_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "E0201",
                "S00501",
                "BTC Strategy",
                "S00501",
                "BTC",
                "BTC",
                "long",
                70000.0,
                70000.0,
                0.01,
                0.01,
                10.0,
                "OPEN",
                "paper_challenger",
                "scanner",
                json.dumps({}),
                "2026-03-12T08:35:04+00:00",
            ),
        )

    positions_payload = {
        "positions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.01",
                    "entryPx": "70000.0",
                    "leverage": {"type": "cross", "value": 10},
                }
            }
        ]
    }
    open_orders_payload = [
        {
            "coin": "BTC",
            "oid": 49994803084,
            "reduceOnly": True,
            "timestamp": 1773304507192,
            "origSz": "0.01",
            "sz": "0.01",
        }
    ]

    monkeypatch.setattr(daemon, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(daemon, "_get_testnet", lambda: True)
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (True, None))
    monkeypatch.setattr(daemon, "get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr(daemon, "get_open_orders", lambda testnet=True: open_orders_payload)
    monkeypatch.setattr(
        daemon,
        "get_account_value",
        lambda testnet=True, require_connection=False: {"accountValue": 1002.33},
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: open_orders_payload)
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 70000.0})

    recovery = daemon.run_startup_recovery_preflight({})

    assert recovery["recovery_active"] is False
    assert recovery["recovery_status"] == "ok"

    with get_db() as conn:
        restored_position = conn.execute(
            "SELECT * FROM portfolio_positions WHERE trade_id = 'E0201'"
        ).fetchone()

    assert restored_position is not None


def test_startup_recovery_preflight_preserves_pending_close_trade_until_exchange_flat(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (
                id, strategy, strategy_name, strategy_id, asset, symbol, direction,
                entry_price, signal_entry_price, fill_entry_price, size, risk_pct, leverage, status,
                execution_type, source, signal_data, opened_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "E0202",
                "S00502",
                "BTC Strategy",
                "S00502",
                "BTC",
                "BTC",
                "long",
                70000.0,
                70000.0,
                70000.0,
                0.01,
                0.01,
                10.0,
                "OPEN",
                "paper_challenger",
                "scanner",
                json.dumps(
                    {
                        "pending_close_reconcile": True,
                        "pending_close_reason": "take_profit",
                        "stop_loss": 68600.0,
                        "exchange_stop_order_id": "pending-stop-1",
                    }
                ),
                "2026-03-12T08:35:04+00:00",
            ),
        )

    positions_payload = {
        "positions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.01",
                    "entryPx": "70000.0",
                    "leverage": {"type": "cross", "value": 10},
                }
            }
        ]
    }
    open_orders_payload = [
        {
            "coin": "BTC",
            "oid": "pending-stop-1",
            "reduceOnly": True,
            "timestamp": 1773304507192,
            "origSz": "0.01",
            "sz": "0.01",
        }
    ]

    monkeypatch.setattr(daemon, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(daemon, "_get_testnet", lambda: True)
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (True, None))
    monkeypatch.setattr(daemon, "get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr(daemon, "get_open_orders", lambda testnet=True: open_orders_payload)
    monkeypatch.setattr(
        daemon,
        "get_account_value",
        lambda testnet=True, require_connection=False: {"accountValue": 1002.33},
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: open_orders_payload)
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 70000.0})

    recovery = daemon.run_startup_recovery_preflight({})

    assert recovery["recovery_active"] is False
    assert recovery["recovery_status"] == "ok"

    with get_db() as conn:
        trade = conn.execute(
            "SELECT status, signal_data FROM trades WHERE id = 'E0202'"
        ).fetchone()
        restored_position = conn.execute(
            "SELECT * FROM portfolio_positions WHERE trade_id = 'E0202'"
        ).fetchone()

    assert trade is not None
    signal_data = json.loads(trade["signal_data"] or "{}")
    assert trade["status"] == "OPEN"
    assert signal_data["pending_close_reconcile"] is True
    assert signal_data["recovery_protection_status"] == "protected"
    assert restored_position is not None


def test_startup_recovery_preflight_blocks_ambiguous_match_candidates(forven_db, monkeypatch):
    with get_db() as conn:
        for trade_id in ("E0301", "E0302"):
            conn.execute(
                """
                INSERT INTO trades
                (
                    id, strategy, strategy_name, strategy_id, asset, symbol, direction,
                    entry_price, signal_entry_price, size, risk_pct, leverage, status,
                    execution_type, source, signal_data, opened_at, closed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    "S00601",
                    "BTC Strategy",
                    "S00601",
                    "BTC",
                    "BTC",
                    "long",
                    70000.0,
                    70000.0,
                    0.00471,
                    0.01,
                    20.0,
                    "CLOSED",
                    "paper_challenger",
                    "scanner",
                    json.dumps({}),
                    "2026-03-12T08:35:04+00:00",
                    "2026-03-12T08:40:37+00:00",
                ),
            )

    positions_payload = {
        "positions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.00471",
                    "entryPx": "69647.0",
                    "leverage": {"type": "cross", "value": 20},
                }
            }
        ]
    }

    monkeypatch.setattr(daemon, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(daemon, "_get_testnet", lambda: True)
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (True, None))
    monkeypatch.setattr(daemon, "get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr(daemon, "get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr(
        daemon,
        "get_account_value",
        lambda testnet=True, require_connection=False: {"accountValue": 986.2},
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 68613.0})

    recovery = daemon.run_startup_recovery_preflight({})

    assert recovery["recovery_active"] is True
    assert recovery["recovery_status"] == "blocked"
    assert recovery["recovery_requires_operator"] is True
    assert recovery["recovery_discrepancy_count"] == 1


def test_startup_recovery_preflight_restores_prior_stop_for_recovered_position(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (
                id, strategy, strategy_name, strategy_id, asset, symbol, direction,
                entry_price, signal_entry_price, size, risk_pct, leverage, status,
                execution_type, source, signal_data, opened_at, closed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "E0400",
                "S00700",
                "BTC Strategy",
                "S00700",
                "BTC",
                "BTC",
                "long",
                69647.0,
                69647.0,
                0.00471,
                0.01,
                20.0,
                "CLOSED",
                "paper_challenger",
                "scanner",
                json.dumps({"stop_loss": 66440.0}),
                "2026-03-12T08:35:04+00:00",
                "2026-03-12T08:40:37+00:00",
            ),
        )

    positions_payload = {
        "positions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.00471",
                    "entryPx": "69647.0",
                    "leverage": {"type": "cross", "value": 20},
                }
            }
        ]
    }
    placed: list[dict[str, object]] = []

    monkeypatch.setattr(daemon, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(daemon, "_get_testnet", lambda: True)
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (True, None))
    monkeypatch.setattr(daemon, "get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr(daemon, "get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr(
        daemon,
        "get_account_value",
        lambda testnet=True, require_connection=False: {"accountValue": 986.2},
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 68613.0})
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.place_protective_stop",
        lambda asset, position_direction, size, stop_loss_price, testnet=True: placed.append(
            {
                "asset": asset,
                "direction": position_direction,
                "size": size,
                "stop_loss_price": stop_loss_price,
                "testnet": testnet,
            }
        ) or {
            "status": "ok",
            "stop_order_id": "restored-stop-1",
            "stop_loss": stop_loss_price,
        },
    )

    recovery = daemon.run_startup_recovery_preflight({})

    assert recovery["recovery_active"] is False
    assert recovery["recovery_status"] == "ok"
    assert recovery["recovery_requires_operator"] is False
    assert recovery["recovery_discrepancy_count"] == 0
    assert placed and placed[0]["stop_loss_price"] == 66440.0

    with get_db() as conn:
        recovered_trade = conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN' AND source = 'exchange_recovered'"
        ).fetchone()

    assert recovered_trade is not None
    recovered_signal = json.loads(recovered_trade["signal_data"] or "{}")
    assert recovered_signal["recovery_protection_status"] == "protected"
    assert recovered_signal["exchange_stop_order_id"] == "restored-stop-1"
    assert recovered_signal["recovery_stop_source"] == "prior_signal_stop"


def test_startup_recovery_preflight_places_emergency_stop_for_recovered_position(forven_db, monkeypatch):
    positions_payload = {
        "positions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.00471",
                    "entryPx": "69647.0",
                    "leverage": {"type": "cross", "value": 20},
                }
            }
        ]
    }
    placed: list[dict[str, object]] = []

    monkeypatch.setattr(daemon, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(daemon, "_get_testnet", lambda: True)
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (True, None))
    monkeypatch.setattr(daemon, "get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr(daemon, "get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr(
        daemon,
        "get_account_value",
        lambda testnet=True, require_connection=False: {"accountValue": 986.2},
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 68613.0})
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.place_protective_stop",
        lambda asset, position_direction, size, stop_loss_price, testnet=True: placed.append(
            {
                "asset": asset,
                "direction": position_direction,
                "size": size,
                "stop_loss_price": stop_loss_price,
                "testnet": testnet,
            }
        ) or {
            "status": "ok",
            "stop_order_id": "emergency-stop-1",
            "stop_loss": stop_loss_price,
        },
    )

    recovery = daemon.run_startup_recovery_preflight({})

    assert recovery["recovery_active"] is False
    assert recovery["recovery_status"] == "ok"
    assert recovery["recovery_requires_operator"] is False
    assert recovery["recovery_discrepancy_count"] == 0
    assert placed and round(float(placed[0]["stop_loss_price"]), 3) == round(68613.0 * (1.0 - 0.02 / 20.0), 3)

    with get_db() as conn:
        recovered_trade = conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN' AND source = 'exchange_recovered'"
        ).fetchone()

    assert recovered_trade is not None
    recovered_signal = json.loads(recovered_trade["signal_data"] or "{}")
    assert recovered_signal["recovery_protection_status"] == "protected"
    assert recovered_signal["exchange_stop_order_id"] == "emergency-stop-1"
    assert recovered_signal["recovery_stop_source"] == "emergency_risk_clamp"


def test_startup_recovery_preflight_repairs_missing_stop_for_existing_open_trade(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (
                id, strategy, strategy_name, strategy_id, asset, symbol, direction,
                entry_price, signal_entry_price, size, risk_pct, leverage, status,
                execution_type, source, signal_data, opened_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "E0410",
                "S00710",
                "BTC Strategy",
                "S00710",
                "BTC",
                "BTC",
                "long",
                70000.0,
                70000.0,
                0.01,
                0.01,
                10.0,
                "OPEN",
                "paper_challenger",
                "scanner",
                json.dumps({"stop_loss": 68600.0}),
                "2026-03-12T08:35:04+00:00",
            ),
        )

    positions_payload = {
        "positions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.01",
                    "entryPx": "70000.0",
                    "leverage": {"type": "cross", "value": 10},
                }
            }
        ]
    }
    placed: list[dict[str, object]] = []

    monkeypatch.setattr(daemon, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(daemon, "_get_testnet", lambda: True)
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (True, None))
    monkeypatch.setattr(daemon, "get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr(daemon, "get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr(
        daemon,
        "get_account_value",
        lambda testnet=True, require_connection=False: {"accountValue": 1002.33},
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 70000.0})
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.place_protective_stop",
        lambda asset, position_direction, size, stop_loss_price, testnet=True: placed.append(
            {
                "asset": asset,
                "direction": position_direction,
                "size": size,
                "stop_loss_price": stop_loss_price,
                "testnet": testnet,
            }
        ) or {
            "status": "ok",
            "stop_order_id": "open-trade-stop-1",
            "stop_loss": stop_loss_price,
        },
    )

    recovery = daemon.run_startup_recovery_preflight({})

    assert recovery["recovery_active"] is False
    assert recovery["recovery_status"] == "ok"
    assert recovery["recovery_requires_operator"] is False
    assert recovery["recovery_discrepancy_count"] == 0
    assert placed and placed[0]["stop_loss_price"] == 68600.0

    with get_db() as conn:
        repaired_trade = conn.execute(
            "SELECT * FROM trades WHERE id = 'E0410'"
        ).fetchone()

    assert repaired_trade is not None
    repaired_signal = json.loads(repaired_trade["signal_data"] or "{}")
    assert repaired_signal["exchange_stop_order_id"] == "open-trade-stop-1"
    assert repaired_signal["recovery_protection_status"] == "protected"
    assert repaired_signal["recovery_stop_source"] == "prior_signal_stop"


def test_startup_recovery_preflight_blocks_unprotected_recovered_position(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (
                id, strategy, strategy_name, strategy_id, asset, symbol, direction,
                entry_price, signal_entry_price, size, risk_pct, leverage, status,
                execution_type, source, signal_data, opened_at, closed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "E0401",
                "S00701",
                "BTC Strategy",
                "S00701",
                "BTC",
                "BTC",
                "long",
                69647.0,
                69647.0,
                0.00471,
                0.01,
                20.0,
                "CLOSED",
                "paper_challenger",
                "scanner",
                json.dumps({"stop_loss": 66440.0}),
                "2026-03-12T08:35:04+00:00",
                "2026-03-12T08:40:37+00:00",
            ),
        )

    positions_payload = {
        "positions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.00471",
                    "entryPx": "69647.0",
                    "leverage": {"type": "cross", "value": 20},
                }
            }
        ]
    }

    monkeypatch.setattr(daemon, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(daemon, "_get_testnet", lambda: True)
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (True, None))
    monkeypatch.setattr(daemon, "get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr(daemon, "get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr(
        daemon,
        "get_account_value",
        lambda testnet=True, require_connection=False: {"accountValue": 986.2},
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_positions", lambda testnet=True: positions_payload)
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 68613.0})
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.place_protective_stop",
        lambda asset, position_direction, size, stop_loss_price, testnet=True: {"error": "exchange unavailable"},
    )

    recovery = daemon.run_startup_recovery_preflight({})

    assert recovery["recovery_active"] is True
    assert recovery["recovery_status"] == "blocked"
    assert recovery["recovery_requires_operator"] is True
    assert recovery["recovery_discrepancy_count"] == 1
    assert "Adopted 1 exchange position" in recovery["recovery_summary"]

    with get_db() as conn:
        recovered_trade = conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN' AND source = 'exchange_recovered'"
        ).fetchone()

    assert recovered_trade is not None
    recovered_signal = json.loads(recovered_trade["signal_data"] or "{}")
    assert recovered_signal["recovery_protection_status"] == "missing"
    assert recovered_signal["recovery_stop_restore_error"] == "exchange unavailable"


def test_startup_recovery_preflight_resolves_inverse_orphan_when_exchange_is_flat(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (
                id, strategy, strategy_name, strategy_id, asset, symbol, direction,
                entry_price, signal_entry_price, fill_entry_price, size, risk_pct, leverage, status,
                execution_type, source, signal_data, opened_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "E0501",
                "S00801",
                "BTC Strategy",
                "S00801",
                "BTC",
                "BTC",
                "long",
                70000.0,
                70000.0,
                70000.0,
                0.01,
                0.01,
                10.0,
                "OPEN",
                "paper_challenger",
                "scanner",
                json.dumps({}),
                "2026-03-12T08:35:04+00:00",
            ),
        )

    monkeypatch.setattr(daemon, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(daemon, "_get_testnet", lambda: True)
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (True, None))
    monkeypatch.setattr(daemon, "get_positions", lambda testnet=True: {"positions": []})
    monkeypatch.setattr(daemon, "get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr(
        daemon,
        "get_account_value",
        lambda testnet=True, require_connection=False: {"accountValue": 1002.33},
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_positions", lambda testnet=True: {"positions": []})
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 70000.0})

    recovery = daemon.run_startup_recovery_preflight({})

    assert recovery["recovery_active"] is False
    assert recovery["recovery_status"] == "ok"
    assert recovery["recovery_discrepancy_count"] == 0

    with get_db() as conn:
        trade = conn.execute("SELECT status, exit_price FROM trades WHERE id = 'E0501'").fetchone()

    # Lead-1: a local-only paper trade is absent from the exchange BY DESIGN.
    # It must stay OPEN (the scanner owns its exit) — force-closing it at the
    # testnet mid fabricated the outcome — and its presence must not flag
    # recovery (which would block new entries for as long as any paper trade
    # is open).
    assert trade is not None
    assert trade["status"] == "OPEN"
    assert trade["exit_price"] is None
