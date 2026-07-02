"""Signal-bar-close (pending) actions: the kernel's projection of the order the LAST
closed bar's signal decides for the FORMING bar's open, and the reconciler's handling
of it.

This is the LAG-1 fix: without pendings, the kernel cannot hold a position whose fill
bar isn't in the frame, so the scanner detected every entry only after the fill bar
CLOSED — one full timeframe bar (+ scan lag) later than the validated backtest's
next-bar-open fill. The pending machinery lets the scanner act the moment the signal
bar closes, while every subsequent scan reconciles the recorded trade against the
kernel's materialized position as a plain refresh/close.

Invariants proven here:
  * simulate() emits pending entries/exits from the last bar's signals ONLY (no change
    to closed_trades/open_positions — backtest parity untouched);
  * the projected fill-bar entry_time matches the kernel's own entry_time once the
    fill bar closes (the refresh-match invariant);
  * reconcile() acts on pendings only in live/paper mode (fresh_cutoff set), dedups a
    same-bar re-scan, never re-opens an operator-closed row, never double-opens a held
    slot, orders a same-bar exit→re-entry close-before-open, and never orphan-closes
    the pending-opened row while the fill bar is still forming.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from forven.strategies import execution_kernel as ek
from forven.strategies import sizing as _sizing
from forven.strategies.base import DirectionalSignals
from forven.strategies.paper_reconcile import reconcile


WARMUP = 5


def _frame(n: int = 40, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(rng.normal(0.0, 0.01, size=n).cumsum())
    spread = np.abs(rng.normal(0.0, 0.008, size=n)) + 0.002
    openp = np.empty(n)
    openp[0] = close[0]
    openp[1:] = close[:-1]
    high = np.maximum.reduce([close * (1 + spread), openp, close])
    low = np.minimum.reduce([close * (1 - spread), openp, close])
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": openp, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx
    )


def _controls(**over) -> dict:
    ec = _sizing.default_controls()
    ec.update(over)
    return ec


def _simulate(df, signals, ec=None, trade_mode="long_only"):
    return ek.simulate(
        df, signals, WARMUP, 1.0,
        regimes=None, round_trip_drag=0.0011, trade_mode=trade_mode,
        allowed_modes=("long",) if trade_mode == "long_only" else ("long", "short"),
        ec=ec or _controls(), initial_capital=10000.0,
    )


# ---------------------------------------------------------------- kernel emission


def test_pending_entry_emitted_for_last_bar_signal():
    df = _frame()
    sig = DirectionalSignals.empty(df.index)
    sig.long_entries.iloc[-1] = True  # signal on the LAST closed bar → fills NEXT bar
    res = _simulate(df, sig)
    assert res.open_positions == {}  # the kernel itself cannot hold it yet
    assert "long" in res.pending_entries
    pe = res.pending_entries["long"]
    assert pe["signal_time"] == str(df.index[-1])
    assert pe["atr_value"] and pe["atr_value"] > 0  # default controls are atr-sized
    assert res.pending_exits == {}
    assert res.ec is not None


def test_no_pending_without_last_bar_signal():
    df = _frame()
    sig = DirectionalSignals.empty(df.index)
    sig.long_entries.iloc[-3] = True  # older signal → a REAL kernel position, no pending
    res = _simulate(df, sig)
    assert "long" in res.open_positions
    assert res.pending_entries == {}
    assert res.pending_exits == {}


def test_pending_entry_suppressed_while_slot_held():
    df = _frame()
    sig = DirectionalSignals.empty(df.index)
    sig.long_entries.iloc[-3] = True   # opens a position (still held)
    sig.long_entries.iloc[-1] = True   # re-entry signal while held → NOT pending
    res = _simulate(df, sig)
    assert "long" in res.open_positions
    assert res.pending_entries == {}


def test_pending_exit_and_same_bar_reentry():
    df = _frame()
    sig = DirectionalSignals.empty(df.index)
    sig.long_entries.iloc[-4] = True
    sig.long_exits.iloc[-1] = True    # exit decision on the last closed bar
    sig.long_entries.iloc[-1] = True  # …and a re-entry on the same bar
    res = _simulate(df, sig)
    assert "long" in res.open_positions  # kernel still holds it (exit fills next bar)
    px = res.pending_exits["long"]
    assert px["exit_reason"] == "signal"
    assert px["entry_time"] == res.open_positions["long"]["entry_time"]
    # the pending exit frees the slot at the open tick → same-bar re-entry projects too
    assert "long" in res.pending_entries


def test_pending_time_stop():
    df = _frame()
    sig = DirectionalSignals.empty(df.index)
    sig.long_entries.iloc[10] = True  # fills at bar 11
    # Stop-free fraction sizing so nothing exits in-loop; the time stop is sized to fire
    # exactly on the VIRTUAL (forming) bar: len(df) - 11 bars held.
    ec = _controls(sizing_mode="fraction", stop_loss_pct=None, needs_atr=False,
                   time_stop_bars=len(df) - 11)
    res = _simulate(df, sig, ec=ec)
    assert "long" in res.open_positions
    assert res.pending_exits["long"]["exit_reason"] == "time_stop"


def test_projection_matches_kernel_fill_once_bar_closes():
    """The refresh-match invariant: the label the scanner projects for a pending entry
    (last label + one bar) must equal the kernel's own entry_time after the fill bar
    closes — otherwise every later scan would mis-key the recorded trade."""
    df = _frame(n=40)
    sig = DirectionalSignals.empty(df.index)
    sig.long_entries.iloc[30] = True  # bar 30 signal → fills at bar 31's open

    prefix = df.iloc[:31]  # bar 30 is the LAST closed bar
    res_before = _simulate(prefix, DirectionalSignals(
        long_entries=sig.long_entries.iloc[:31], long_exits=sig.long_exits.iloc[:31],
        short_entries=sig.short_entries.iloc[:31], short_exits=sig.short_exits.iloc[:31],
    ))
    assert "long" in res_before.pending_entries
    projected = str(prefix.index[-1] + pd.Timedelta(hours=1))

    after = df.iloc[:32]  # fill bar has closed
    res_after = _simulate(after, DirectionalSignals(
        long_entries=sig.long_entries.iloc[:32], long_exits=sig.long_exits.iloc[:32],
        short_entries=sig.short_entries.iloc[:32], short_exits=sig.short_exits.iloc[:32],
    ))
    assert res_after.open_positions["long"]["entry_time"] == projected


def test_pending_does_not_change_realized_output():
    """Pendings are advisory: closed_trades/open_positions must be identical whether or
    not the last bar carries a signal-only pending (backtest parity untouched)."""
    df = _frame()
    sig = DirectionalSignals.empty(df.index)
    sig.long_entries.iloc[12] = True
    sig.long_exits.iloc[20] = True
    base = _simulate(df, sig)

    sig2 = DirectionalSignals.empty(df.index)
    sig2.long_entries.iloc[12] = True
    sig2.long_exits.iloc[20] = True
    sig2.long_entries.iloc[-1] = True  # adds ONLY a pending
    with_pending = _simulate(df, sig2)

    assert base.closed_trades == with_pending.closed_trades
    assert base.open_positions == with_pending.open_positions


# ---------------------------------------------------------------- reconcile handling


def _pending_res(*, entries=None, exits=None, open_positions=None, closed=None):
    return ek.KernelResult(
        closed_trades=list(closed or []),
        open_positions=dict(open_positions or {}),
        pending_entries=dict(entries or {}),
        pending_exits=dict(exits or {}),
        ec=_controls(),
    )


NEXT = "2024-01-02 16:00:00+00:00"  # the projected (forming) fill-bar label
LAST = "2024-01-02 15:00:00+00:00"  # the last closed bar


def _pe(entry_time=NEXT):
    return {"direction": "long", "signal_time": LAST, "atr_value": 1.0,
            "regime": None, "entry_time": entry_time}


def test_reconcile_ignores_pendings_in_parity_mode():
    actions = reconcile(_pending_res(entries={"long": _pe()}), [], fresh_cutoff=None)
    assert actions == []


def test_reconcile_opens_pending_entry():
    actions = reconcile(_pending_res(entries={"long": _pe()}), [], fresh_cutoff=LAST)
    assert [a.kind for a in actions] == ["open"]
    a = actions[0]
    assert a.pending and a.late_entry and a.entry_time == NEXT


def test_reconcile_pending_entry_unstamped_is_dropped():
    """No projected label (scanner judged the frame stale) → no action."""
    pe = _pe(entry_time="")
    actions = reconcile(_pending_res(entries={"long": pe}), [], fresh_cutoff=LAST)
    assert actions == []


def test_reconcile_same_bar_rescan_is_idempotent_and_orphan_safe():
    """Second scan within the same forming bar: the pending entry was already opened.
    No duplicate open — and the recorded row (kernel is necessarily flat on it) must
    NOT be orphan-closed."""
    recorded = [{"direction": "long", "entry_time": NEXT, "status": "open", "_row": {}}]
    actions = reconcile(
        _pending_res(entries={"long": _pe()}), recorded,
        fresh_cutoff=LAST, window_start="2024-01-01 00:00:00+00:00",
    )
    assert actions == []


def test_reconcile_never_reopens_operator_closed_pending():
    recorded = [{"direction": "long", "entry_time": NEXT, "status": "closed", "_row": {}}]
    actions = reconcile(_pending_res(entries={"long": _pe()}), recorded, fresh_cutoff=LAST)
    assert actions == []


def test_reconcile_pending_entry_skipped_when_slot_held():
    """A different OPEN row (manual/adopted) holds the direction slot and is not
    closing this round → don't double-open."""
    recorded = [{"direction": "long", "entry_time": "2024-01-01 03:00:00+00:00",
                 "status": "open", "_row": {}}]
    actions = reconcile(
        _pending_res(entries={"long": _pe()}), recorded,
        fresh_cutoff=LAST, window_start="2024-01-02 00:00:00+00:00",
    )
    assert not [a for a in actions if a.kind == "open"]


def test_reconcile_pending_exit_closes_recorded_open():
    pos = {"entry_time": "2024-01-02 10:00:00+00:00", "entry_price": 100.0,
           "size_fraction": 0.5, "entry_bar": 10}
    px = {"direction": "long", "entry_time": pos["entry_time"], "entry_price": 100.0,
          "size_fraction": 0.5, "exit_reason": "signal", "signal_time": LAST}
    recorded = [{"direction": "long", "entry_time": pos["entry_time"], "status": "open", "_row": {}}]
    actions = reconcile(
        _pending_res(exits={"long": px}, open_positions={"long": pos}), recorded,
        fresh_cutoff=LAST,
    )
    kinds = [(a.kind, a.pending) for a in actions]
    assert ("close", True) in kinds
    # the still-held kernel position must NOT also refresh/re-open the closing row
    assert not [a for a in actions if a.kind in ("open", "refresh")]


def test_reconcile_pending_exit_idempotent_after_close():
    pos = {"entry_time": "2024-01-02 10:00:00+00:00", "entry_price": 100.0,
           "size_fraction": 0.5, "entry_bar": 10}
    px = {"direction": "long", "entry_time": pos["entry_time"], "exit_reason": "signal"}
    recorded = [{"direction": "long", "entry_time": pos["entry_time"], "status": "closed", "_row": {}}]
    actions = reconcile(
        _pending_res(exits={"long": px}, open_positions={"long": pos}), recorded,
        fresh_cutoff=LAST,
    )
    assert not [a for a in actions if a.kind == "close"]


def test_reconcile_same_bar_exit_then_reentry_orders_close_before_open():
    held = "2024-01-02 10:00:00+00:00"
    pos = {"entry_time": held, "entry_price": 100.0, "size_fraction": 0.5, "entry_bar": 10}
    px = {"direction": "long", "entry_time": held, "entry_price": 100.0,
          "size_fraction": 0.5, "exit_reason": "signal", "signal_time": LAST}
    recorded = [{"direction": "long", "entry_time": held, "status": "open", "_row": {}}]
    actions = reconcile(
        _pending_res(exits={"long": px}, entries={"long": _pe()},
                     open_positions={"long": pos}),
        recorded, fresh_cutoff=LAST,
    )
    kinds = [a.kind for a in actions]
    assert kinds.index("close") < kinds.index("open")
    open_a = next(a for a in actions if a.kind == "open")
    assert open_a.pending and open_a.entry_time == NEXT
