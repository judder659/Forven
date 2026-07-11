"""Regression: a transient sub-account equity read must not fake a drawdown.

Surfaced live during the testnet rehearsal — one sub-account's get_account_value
momentarily returned 0 while funds were moving, the books-aggregate summed the
incomplete total ($659 -> $343), and the kill-switch tripped on a fake 48%
drawdown and flattened a live position. The aggregate must ride out a transient
zero/failed read via last-known-good, and skip (return None) when data is truly
incomplete rather than report a cratered value.
"""

import pytest

import forven.daemon as dmn


@pytest.fixture(autouse=True)
def _reset_cache_and_books(monkeypatch):
    dmn._BOOK_EQUITY_CACHE.clear()
    monkeypatch.setattr("forven.exchange.books.books_enabled", lambda: True)
    monkeypatch.setattr(
        "forven.exchange.books.active_book_addresses",
        lambda: [("long", "0xLONG"), ("short", "0xSHORT")],
    )
    # EQ-BASIS-1 made the master wallet opt-in; these tests pin the substitution /
    # reliability machinery with the master INCLUDED (its original semantics).
    # Books-only composition is covered by tests/test_equity_anchors.py.
    monkeypatch.setattr(dmn, "_equity_includes_master", lambda: True)
    yield
    dmn._BOOK_EQUITY_CACHE.clear()


def _stub_reads(monkeypatch, values: dict):
    """values keyed by '__master__' / '0xlong' / '0xshort' -> accountValue or Exception."""
    def _gav(testnet=True, account_address=None, **kw):
        key = (str(account_address).strip().lower() if account_address else "__master__")
        v = values.get(key)
        if isinstance(v, Exception):
            raise v
        return {"accountValue": v, "totalMarginUsed": 0.0, "totalNtlPos": 0.0}
    monkeypatch.setattr(dmn, "get_account_value", _gav)


def test_full_aggregate_sums_all_accounts(monkeypatch):
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 329.0, "0xshort": 30.0})
    out = dmn._book_aware_account_value(testnet=True)
    assert out is not None
    assert out["accountValue"] == pytest.approx(675.0)
    assert out["source"] == "books_aggregate"


def test_transient_zero_read_uses_last_known_not_crater(monkeypatch):
    # First tick: all good -> caches last-known.
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 329.0, "0xshort": 30.0})
    assert dmn._book_aware_account_value(testnet=True)["accountValue"] == pytest.approx(675.0)
    # Next tick: master read glitches to 0 -> must NOT crater to $359; uses last-known $316.
    _stub_reads(monkeypatch, {"__master__": 0.0, "0xlong": 329.0, "0xshort": 30.0})
    out = dmn._book_aware_account_value(testnet=True)
    assert out is not None
    assert out["accountValue"] == pytest.approx(675.0)  # NOT 359


def test_failed_read_uses_last_known(monkeypatch):
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 329.0, "0xshort": 30.0})
    dmn._book_aware_account_value(testnet=True)
    _stub_reads(monkeypatch, {"__master__": RuntimeError("read timeout"), "0xlong": 329.0, "0xshort": 30.0})
    out = dmn._book_aware_account_value(testnet=True)
    assert out is not None and out["accountValue"] == pytest.approx(675.0)


def test_real_loss_still_passes_through(monkeypatch):
    # A genuine positive-but-lower balance must flow through so real losses still
    # trip the kill-switch.
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 329.0, "0xshort": 30.0})
    dmn._book_aware_account_value(testnet=True)
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 200.0, "0xshort": 30.0})  # long dropped, real
    out = dmn._book_aware_account_value(testnet=True)
    assert out["accountValue"] == pytest.approx(546.0)  # reflects the real loss


def test_empty_master_counts_as_zero_not_unreliable(monkeypatch):
    # Master drained to $0 (all capital in the sub-accounts) is a VALID config:
    # a 0 read with no history is a legitimately EMPTY account, counted as $0 —
    # NOT treated as unreliable (which would stop the daemon computing equity).
    _stub_reads(monkeypatch, {"__master__": 0.0, "0xlong": 329.0, "0xshort": 30.0})
    out = dmn._book_aware_account_value(testnet=True)
    assert out is not None
    assert out["accountValue"] == pytest.approx(359.0)


def test_raised_read_with_no_history_returns_none(monkeypatch):
    # A read that ERRORS (not just 0) with no last-known is genuinely unknown,
    # so the aggregate is unreliable -> None -> risk cycle skips the tick.
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": RuntimeError("read error"), "0xshort": 30.0})
    assert dmn._book_aware_account_value(testnet=True) is None


def test_substitution_is_logged_for_diagnosis(monkeypatch, caplog):
    # KS-CACHE-LOG: the cache substitution is the exact mechanism that poisoned the
    # aggregate in the 2026-06-29 false kill-switch. It must log LOUD (WARNING) so a
    # recurrence is diagnosable in api.log BEFORE the operator restarts (which wipes
    # this in-memory cache and the evidence with it).
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 329.0, "0xshort": 30.0})
    dmn._book_aware_account_value(testnet=True)  # seed last-known-good cache
    _stub_reads(monkeypatch, {"__master__": RuntimeError("read timeout"), "0xlong": 329.0, "0xshort": 30.0})
    with caplog.at_level("WARNING"):
        out = dmn._book_aware_account_value(testnet=True)
    assert out is not None and out["accountValue"] == pytest.approx(675.0)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "SUBSTITUTED" in msgs and "__master__" in msgs  # names the failing wallet + cached value
    assert "DEGRADED" in msgs  # full per-wallet composition evidence line


# ---------------------------------------------- DRAIN-1: clean-zero wallet drains


@pytest.fixture(autouse=True)
def _reset_clean_zero_streaks():
    dmn._BOOK_CLEAN_ZERO_STREAK.clear()
    yield
    dmn._BOOK_CLEAN_ZERO_STREAK.clear()


def test_transient_failed_read_still_substitutes_no_drain(monkeypatch):
    # A RAISED read (timeout) must NEVER count toward the clean-zero drain streak —
    # even repeatedly. It stays on the last-known-good substitution path forever so
    # a persistent transient glitch can't fake an accepted drain.
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 329.0, "0xshort": 30.0})
    dmn._book_aware_account_value(testnet=True)  # seed cache
    for _ in range(6):
        _stub_reads(monkeypatch, {"__master__": RuntimeError("read timeout"), "0xlong": 329.0, "0xshort": 30.0})
        out = dmn._book_aware_account_value(testnet=True)
    assert out is not None
    assert out["accountValue"] == pytest.approx(675.0)  # still substituted, never drained
    assert out.get("drain_accepted") is False
    assert dmn._BOOK_EQUITY_CACHE.get("__master__") == pytest.approx(316.0)  # cache intact


def test_clean_zero_streak_accepts_drain_and_purges_cache(monkeypatch):
    # A read that SUCCEEDS and reports 0 is a CLEAN zero. After N consecutive clean
    # zeros the drain is accepted: the cache entry is purged and the aggregate
    # reflects the drain (no longer inflated by the substituted balance).
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 329.0, "0xshort": 30.0})
    assert dmn._book_aware_account_value(testnet=True)["accountValue"] == pytest.approx(675.0)

    # Ticks 1..2: master reads clean 0 -> still substituted (streak building).
    for expected_streak in (1, 2):
        _stub_reads(monkeypatch, {"__master__": 0.0, "0xlong": 329.0, "0xshort": 30.0})
        out = dmn._book_aware_account_value(testnet=True)
        assert out["accountValue"] == pytest.approx(675.0)  # cached $316 still in
        assert out.get("drain_accepted") is False
        assert dmn._BOOK_CLEAN_ZERO_STREAK.get("__master__") == expected_streak

    # Tick 3: streak hits the confirm threshold -> ACCEPT the drain.
    _stub_reads(monkeypatch, {"__master__": 0.0, "0xlong": 329.0, "0xshort": 30.0})
    out = dmn._book_aware_account_value(testnet=True)
    assert out["accountValue"] == pytest.approx(359.0)  # 329 + 30, master drained
    assert out.get("drain_accepted") is True
    assert "__master__" not in dmn._BOOK_EQUITY_CACHE  # cache purged
    assert "__master__" not in dmn._BOOK_CLEAN_ZERO_STREAK  # streak reset


def test_positive_read_resets_clean_zero_streak(monkeypatch):
    # A drain streak that is INTERRUPTED by a real positive read must reset — funds
    # came back, so it was never a drain.
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 329.0, "0xshort": 30.0})
    dmn._book_aware_account_value(testnet=True)
    for _ in range(2):
        _stub_reads(monkeypatch, {"__master__": 0.0, "0xlong": 329.0, "0xshort": 30.0})
        dmn._book_aware_account_value(testnet=True)
    assert dmn._BOOK_CLEAN_ZERO_STREAK.get("__master__") == 2
    # Funds return -> streak clears, cache re-seeded.
    _stub_reads(monkeypatch, {"__master__": 300.0, "0xlong": 329.0, "0xshort": 30.0})
    out = dmn._book_aware_account_value(testnet=True)
    assert out["accountValue"] == pytest.approx(659.0)
    assert "__master__" not in dmn._BOOK_CLEAN_ZERO_STREAK
    # A later single clean-zero must NOT immediately drain (streak restarts at 1).
    _stub_reads(monkeypatch, {"__master__": 0.0, "0xlong": 329.0, "0xshort": 30.0})
    out = dmn._book_aware_account_value(testnet=True)
    assert out["accountValue"] == pytest.approx(659.0)  # substituted $300 again
    assert out.get("drain_accepted") is False


def test_genuine_partial_loss_still_flows_no_drain(monkeypatch):
    # A clean read that is LOWER but non-zero is a real trading loss, not a drain:
    # it flows straight through as the aggregate with no cache purge / drain flag.
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 329.0, "0xshort": 30.0})
    dmn._book_aware_account_value(testnet=True)
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 150.0, "0xshort": 30.0})  # long lost half
    out = dmn._book_aware_account_value(testnet=True)
    assert out["accountValue"] == pytest.approx(496.0)  # reflects the loss
    assert out.get("drain_accepted") is False
    assert not dmn._BOOK_CLEAN_ZERO_STREAK  # a non-zero read never advances the streak


def test_accepted_drain_is_logged(monkeypatch, caplog):
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 329.0, "0xshort": 30.0})
    dmn._book_aware_account_value(testnet=True)
    for _ in range(2):
        _stub_reads(monkeypatch, {"__master__": 0.0, "0xlong": 329.0, "0xshort": 30.0})
        dmn._book_aware_account_value(testnet=True)
    _stub_reads(monkeypatch, {"__master__": 0.0, "0xlong": 329.0, "0xshort": 30.0})
    with caplog.at_level("WARNING"):
        dmn._book_aware_account_value(testnet=True)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "DRAIN" in msgs and "__master__" in msgs


def test_clear_book_equity_cache_purges_everything(monkeypatch):
    _stub_reads(monkeypatch, {"__master__": 316.0, "0xlong": 329.0, "0xshort": 30.0})
    dmn._book_aware_account_value(testnet=True)
    assert dmn._BOOK_EQUITY_CACHE  # seeded
    # Seed a partial clean-zero streak too.
    _stub_reads(monkeypatch, {"__master__": 0.0, "0xlong": 329.0, "0xshort": 30.0})
    dmn._book_aware_account_value(testnet=True)
    assert dmn._BOOK_CLEAN_ZERO_STREAK
    n = dmn.clear_book_equity_cache()
    assert n >= 1
    assert not dmn._BOOK_EQUITY_CACHE
    assert not dmn._BOOK_CLEAN_ZERO_STREAK
