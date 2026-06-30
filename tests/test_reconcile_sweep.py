"""Tests for sweep_pending_close_reconcile (audit lead B-38).

The sweep previously closed every aged pending-close trade as 'exchange flat'
without ever consulting the exchange, because of four stacked bugs:
  (a) get_positions() returns a dict {'positions': [...]} but the sweep only
      iterated list-shaped payloads -> always saw zero positions;
  (b) entries are {'position': {...}} assetPositions wrappers, so even a list
      would have missed coin/szi;
  (c) the retry branch called close_position(asset) without the required size
      arg -> guaranteed TypeError, swallowed;
  (d) it read 'pending_close_reconcile_requested_at' while the writer stores
      'pending_close_reconcile_at' -> grace period collapsed to opened_at;
  (e) both exchange calls defaulted to testnet=True regardless of the
      configured network.
These tests pin the fixed behavior.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import forven.exchange.hyperliquid as hl_mod
import forven.scanner as scanner_mod


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _insert_trade(
    trade_id: str,
    *,
    asset: str = "BTC",
    direction: str = "long",
    size: float | None = 0.5,
    execution_type: str = "live",
    signal_data: dict | None = None,
    opened_at: str | None = None,
    entry_price: float = 100.0,
    signal_exit_price: float | None = 110.0,
) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """INSERT INTO trades (id, strategy, asset, direction, entry_price,
                   signal_entry_price, signal_exit_price, size, leverage, status,
                   execution_type, signal_data, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)""",
            (
                trade_id,
                "stub_strategy",
                asset,
                direction,
                entry_price,
                entry_price,
                signal_exit_price,
                size,
                1.0,
                execution_type,
                json.dumps(signal_data or {}),
                opened_at or _iso(120),
            ),
        )


def _get_trade(trade_id: str) -> dict | None:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None


def _aged_pending(minutes_ago: float = 60) -> dict:
    return {
        "pending_close_reconcile": True,
        "pending_close_reconcile_at": _iso(minutes_ago),
    }


def _positions_payload(*positions: dict) -> dict:
    """Real get_positions() shape: dict with assetPositions-style wrappers."""
    return {
        "positions": [{"type": "oneWay", "position": dict(p)} for p in positions],
        "marginSummary": {"accountValue": "1000.0"},
    }


# ── (a)+(b): dict-shaped get_positions with a real position is detected ─────


def test_open_exchange_position_is_not_closed_as_flat(monkeypatch, forven_db):
    """A trade whose asset is still open on the exchange must NOT be closed as
    'exchange flat'. With the retry close failing, it must stay OPEN."""
    _insert_trade("T-OPEN", asset="BTC", signal_data=_aged_pending())

    monkeypatch.setattr(
        hl_mod, "get_positions",
        lambda testnet=True: _positions_payload({"coin": "BTC", "szi": "0.5"}),
    )

    def _failing_close(*args, **kwargs):
        raise RuntimeError("order rejected")

    monkeypatch.setattr(hl_mod, "close_position", _failing_close)

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["resolved_count"] == 1
    assert summary["results"][0]["outcome"].startswith("retry_close_failed")
    trade = _get_trade("T-OPEN")
    assert trade["status"] == "OPEN"  # never fabricated a flat close


def test_flat_exchange_closes_locally(monkeypatch, forven_db):
    """Exchange verified flat (dict payload, no matching coin) -> close locally."""
    _insert_trade("T-FLAT", asset="BTC", signal_data=_aged_pending())

    monkeypatch.setattr(
        hl_mod, "get_positions",
        lambda testnet=True: _positions_payload({"coin": "ETH", "szi": "2.0"}),
    )
    monkeypatch.setattr(
        hl_mod, "close_position",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not retry close when flat")),
    )

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["results"][0]["outcome"] == "closed_locally_exchange_flat"
    trade = _get_trade("T-FLAT")
    assert trade["status"] == "CLOSED"
    sd = json.loads(trade["signal_data"])
    assert sd["close_reason"] == "reconcile_sweep_exchange_flat"
    # Honest close: exit resolved from the recorded signal_exit_price.
    assert trade["exit_price"] == 110.0
    assert sd["close_incomplete"] is False


def test_zero_size_wrapper_position_counts_as_flat(monkeypatch, forven_db):
    """An assetPositions wrapper with szi=0 for the asset means flat."""
    _insert_trade("T-ZERO", asset="BTC", signal_data=_aged_pending())

    monkeypatch.setattr(
        hl_mod, "get_positions",
        lambda testnet=True: _positions_payload({"coin": "BTC", "szi": "0"}),
    )

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["results"][0]["outcome"] == "closed_locally_exchange_flat"
    assert _get_trade("T-ZERO")["status"] == "CLOSED"


# ── (c): retry branch calls close_position with a size ──────────────────────


def test_retry_close_passes_size_side_and_testnet(monkeypatch, forven_db):
    _insert_trade("T-RETRY", asset="SOL", direction="long", size=3.25,
                  signal_data=_aged_pending())

    monkeypatch.setattr(scanner_mod, "_resolve_hyperliquid_testnet", lambda: False)

    seen_get = {}

    def _fake_get_positions(testnet=True):
        seen_get["testnet"] = testnet
        return _positions_payload({"coin": "SOL", "szi": "-3.25"})

    monkeypatch.setattr(hl_mod, "get_positions", _fake_get_positions)

    calls = []

    def _fake_close(asset, size, side="sell", testnet=True):
        calls.append({"asset": asset, "size": size, "side": side, "testnet": testnet})
        return {"status": "ok", "exit_price": 95.5, "close_price": 95.0}

    monkeypatch.setattr(hl_mod, "close_position", _fake_close)

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["results"][0]["outcome"] == "retry_close_succeeded"
    assert calls == [{"asset": "SOL", "size": 3.25, "side": "sell", "testnet": False}]
    # (e): both exchange calls honor the configured network.
    assert seen_get["testnet"] is False

    trade = _get_trade("T-RETRY")
    assert trade["status"] == "CLOSED"
    sd = json.loads(trade["signal_data"])
    assert sd["close_reason"] == "reconcile_sweep_retry_close"
    # Fill price from the close result is used for the local record.
    assert trade["exit_price"] == 95.5


def test_retry_close_short_uses_buy_side_and_exchange_size_fallback(monkeypatch, forven_db):
    """NULL local size falls back to the exchange position size; shorts close
    with a buy."""
    _insert_trade("T-SHORT", asset="ETH", direction="short", size=None,
                  signal_data=_aged_pending())

    monkeypatch.setattr(
        hl_mod, "get_positions",
        lambda testnet=True: _positions_payload({"coin": "ETH", "szi": "-1.5"}),
    )

    calls = []

    def _fake_close(asset, size, side="sell", testnet=True):
        calls.append({"asset": asset, "size": size, "side": side})
        return {"status": "ok"}

    monkeypatch.setattr(hl_mod, "close_position", _fake_close)

    scanner_mod.sweep_pending_close_reconcile()

    assert calls == [{"asset": "ETH", "size": 1.5, "side": "buy"}]


def test_retry_close_error_dict_keeps_trade_open(monkeypatch, forven_db):
    """close_position returning {'error': ...} (no raise) must not record a
    successful close."""
    _insert_trade("T-ERRDICT", asset="BTC", signal_data=_aged_pending())

    monkeypatch.setattr(
        hl_mod, "get_positions",
        lambda testnet=True: _positions_payload({"coin": "BTC", "szi": "0.5"}),
    )
    monkeypatch.setattr(
        hl_mod, "close_position",
        lambda *a, **k: {"error": "Could not get mid price for BTC"},
    )

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["results"][0]["outcome"].startswith("retry_close_failed")
    assert _get_trade("T-ERRDICT")["status"] == "OPEN"


# ── (d): grace period honored via pending_close_reconcile_at ────────────────


def test_age_grace_honored_via_pending_close_reconcile_at(monkeypatch, forven_db):
    """A trade opened hours ago but marked pending-close 5 minutes ago is
    within the 30-minute grace window -> NOT swept (the old code fell back to
    opened_at because it read the wrong key, sweeping on the first pass)."""
    _insert_trade(
        "T-FRESH",
        asset="BTC",
        opened_at=_iso(240),  # old open — must not drive the age check
        signal_data=_aged_pending(minutes_ago=5),
    )

    def _boom(*args, **kwargs):
        raise AssertionError("exchange must not be queried inside the grace window")

    monkeypatch.setattr(hl_mod, "get_positions", _boom)
    monkeypatch.setattr(hl_mod, "close_position", _boom)

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["resolved_count"] == 0
    assert _get_trade("T-FRESH")["status"] == "OPEN"


def test_aged_trade_is_swept_after_grace(monkeypatch, forven_db):
    _insert_trade("T-AGED", asset="BTC", opened_at=_iso(240),
                  signal_data=_aged_pending(minutes_ago=45))

    monkeypatch.setattr(hl_mod, "get_positions", lambda testnet=True: _positions_payload())

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["resolved_count"] == 1
    assert _get_trade("T-AGED")["status"] == "CLOSED"


def test_legacy_requested_at_key_still_honored(monkeypatch, forven_db):
    """Rows written with the legacy 'pending_close_reconcile_requested_at' key
    keep their grace window."""
    _insert_trade(
        "T-LEGACY",
        asset="BTC",
        opened_at=_iso(240),
        signal_data={
            "pending_close_reconcile": True,
            "pending_close_reconcile_requested_at": _iso(5),
        },
    )

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["resolved_count"] == 0
    assert _get_trade("T-LEGACY")["status"] == "OPEN"


# ── Local-only paper trades: honest local close, no exchange fabrication ────


def test_local_only_paper_trade_closed_as_local_paper_close(monkeypatch, forven_db):
    """Paper trades execute locally by design (paper_stage_local_execution_only)
    and never reach the exchange, so there is no exchange truth to reconcile.
    The honest behavior — documented here — is a LOCAL paper close at the exit
    price recorded when the close was requested (persisted signal_exit_price),
    never an 'exchange flat'/'exchange unreachable' verdict, and never a query
    against the (testnet) exchange account."""
    _insert_trade(
        "T-PAPER",
        asset="BTC",
        execution_type="paper",
        signal_exit_price=104.5,
        signal_data=_aged_pending(),
    )

    def _boom(*args, **kwargs):
        raise AssertionError("exchange must not be consulted for local-only paper trades")

    monkeypatch.setattr(hl_mod, "get_positions", _boom)
    monkeypatch.setattr(hl_mod, "close_position", _boom)

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["results"][0]["outcome"] == "closed_locally_paper_local"
    trade = _get_trade("T-PAPER")
    assert trade["status"] == "CLOSED"
    sd = json.loads(trade["signal_data"])
    assert sd["close_reason"] == "reconcile_sweep_paper_local_close"
    assert trade["exit_price"] == 104.5  # recorded request price, not fabricated
    assert sd["close_incomplete"] is False


def test_local_paper_trade_without_price_closes_at_mark_not_incomplete(monkeypatch, forven_db):
    """The E0006-E0016 bug: a local paper trade marked pending-close with NO usable exit price
    (an exchange-close path was attempted but returned no fill, so every pending price key and
    the signal_exit_price are None) was closed INCOMPLETE — status CLOSED with no exit_price/pnl,
    dropping it from the equity curve and the promotion gate. The fix prices it at the current
    mark (a local paper trade never reached an exchange, so it can always be priced) → a real,
    COMPLETE close. Exchange must never be consulted for a local-only paper trade."""
    _insert_trade(
        "T-PAPER-NOPRICE",
        asset="BTC",
        direction="long",
        execution_type="paper",
        entry_price=100.0,
        signal_exit_price=None,          # nothing recorded to fall back to
        signal_data=_aged_pending(),
    )
    monkeypatch.setattr(scanner_mod, "_load_live_price_cache", lambda: ({"BTC": 110.0}, 1.0))

    def _boom(*args, **kwargs):
        raise AssertionError("exchange must not be consulted for local-only paper trades")

    monkeypatch.setattr(hl_mod, "get_positions", _boom)
    monkeypatch.setattr(hl_mod, "close_position", _boom)

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["results"][0]["outcome"] == "closed_locally_paper_local"
    trade = _get_trade("T-PAPER-NOPRICE")
    assert trade["status"] == "CLOSED"
    sd = json.loads(trade["signal_data"])
    assert sd["close_incomplete"] is False               # the fix: COMPLETE, not incomplete
    assert trade["exit_price"] == 110.0                  # priced at the current (live-cache) mark
    assert trade["pnl_pct"] is not None                  # real PnL, not dropped from the gate


def test_local_paper_trade_without_price_or_mark_closes_at_entry(monkeypatch, forven_db):
    """No pending price, no signal_exit, and no live mark → close at the recorded entry
    (a neutral, COMPLETE 0-PnL close) rather than incomplete. A paper trade can always be priced."""
    _insert_trade(
        "T-PAPER-ENTRY",
        asset="BTC",
        execution_type="paper",
        entry_price=100.0,
        signal_exit_price=None,
        signal_data=_aged_pending(),
    )
    monkeypatch.setattr(scanner_mod, "_load_live_price_cache", lambda: ({}, None))

    def _boom(*args, **kwargs):
        raise AssertionError("exchange must not be consulted for local-only paper trades")

    monkeypatch.setattr(hl_mod, "get_positions", _boom)
    monkeypatch.setattr(hl_mod, "close_position", _boom)

    summary = scanner_mod.sweep_pending_close_reconcile()

    trade = _get_trade("T-PAPER-ENTRY")
    assert trade["status"] == "CLOSED"
    sd = json.loads(trade["signal_data"])
    assert sd["close_incomplete"] is False               # COMPLETE, never stranded
    assert trade["exit_price"] == 100.0                  # entry → neutral 0-PnL close


def test_paper_trade_with_exchange_order_id_is_reconciled_normally(monkeypatch, forven_db):
    """A paper trade that DID reach the exchange (carries an order id) is not
    local-only and goes through normal exchange reconciliation."""
    sd = _aged_pending()
    sd["entry_exchange_order_id"] = "12345"
    _insert_trade("T-PAPER-EX", asset="BTC", execution_type="paper", signal_data=sd)

    monkeypatch.setattr(hl_mod, "get_positions", lambda testnet=True: _positions_payload())

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["results"][0]["outcome"] == "closed_locally_exchange_flat"


# ── Exchange unreachable: fail OPEN, never ghost-close (RECON-1) ────────────


def test_exchange_unreachable_leaves_trade_open(monkeypatch, forven_db):
    """RECON-1: a transient exchange-read failure must NOT close the trade. An
    unreadable account is indistinguishable from a still-open position, so the
    sweep fails OPEN — the trade stays pending for the next sweep to retry.
    (Previously it closed-incomplete, ghost-closing a live position on a blip.)"""
    _insert_trade("T-UNREACH", asset="BTC", signal_data=_aged_pending())

    def _down(*args, **kwargs):
        raise ConnectionError("exchange down")

    monkeypatch.setattr(hl_mod, "get_positions", _down)

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["results"][0]["outcome"] == "skipped_exchange_unreachable"
    trade = _get_trade("T-UNREACH")
    assert trade["status"] == "OPEN"  # not ghost-closed on a transient failure


# ── Book-aware reconciliation: read/close the routed sub-account (RECON-1) ──


def _set_books_kv(short_addr="0xshortbook", long_addr="0xlongbook"):
    from forven.db import kv_set

    kv_set(
        "forven:settings",
        {
            "live_books_enabled": True,
            "hyperliquid_long_book_address": long_addr,
            "hyperliquid_short_book_address": short_addr,
        },
    )


def _set_book(trade_id: str, book: str) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute("UPDATE trades SET book = ? WHERE id = ?", (book, trade_id))


def test_subaccount_short_not_ghost_closed_when_master_flat(monkeypatch, forven_db):
    """RECON-1: a SHORT held only in the short-book sub-account must be seen as
    OPEN even though the MASTER wallet reads flat — otherwise the sweep would
    ghost-close a live position (the 28559eb8 bug, in the reconcile sibling).
    The retry-close must route to the sub-account, not master."""
    short_addr = "0xshortbook"
    _set_books_kv(short_addr=short_addr)
    _insert_trade("T-SUB-SHORT", asset="BTC", direction="short", signal_data=_aged_pending())
    _set_book("T-SUB-SHORT", "short")

    def _positions(testnet=True, account_address=None, **kw):
        # Master reads FLAT; the short sub-account holds the position.
        if account_address == short_addr:
            return _positions_payload({"coin": "BTC", "szi": "-0.5"})
        return _positions_payload()  # flat

    captured: dict = {}

    def _close(asset, size, side, testnet=True, vault_address=None, **kw):
        captured["vault_address"] = vault_address
        captured["side"] = side
        return {"exit_price": 100.0}

    monkeypatch.setattr(hl_mod, "get_positions", _positions)
    monkeypatch.setattr(hl_mod, "close_position", _close)

    summary = scanner_mod.sweep_pending_close_reconcile()

    # Seen in the sub-account -> retry close, ROUTED to the sub-account (not master).
    assert summary["results"][0]["outcome"] == "retry_close_succeeded"
    assert captured["vault_address"] == short_addr
    assert captured["side"] == "buy"  # closing a short


def test_non_pending_trades_are_ignored(monkeypatch, forven_db):
    _insert_trade("T-PLAIN", asset="BTC", signal_data={})

    def _boom(*args, **kwargs):
        raise AssertionError("exchange must not be queried for non-pending trades")

    monkeypatch.setattr(hl_mod, "get_positions", _boom)

    summary = scanner_mod.sweep_pending_close_reconcile()

    assert summary["resolved_count"] == 0
    assert _get_trade("T-PLAIN")["status"] == "OPEN"
