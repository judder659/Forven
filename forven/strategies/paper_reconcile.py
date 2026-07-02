"""Pure reconciliation between the execution kernel's view and recorded paper trades.

The live/paper scanner, each closed bar, runs the shared ``execution_kernel`` over a
strategy's history (via ``backtest.run_strategy_execution``) and gets a
:class:`~forven.strategies.execution_kernel.KernelResult` — the trades the backtest
WOULD have taken and the position it WOULD currently hold. This module turns that view
plus the strategy's already-recorded paper trades into a list of concrete actions
(open / close / backfill / refresh) for the scanner to apply.

Keeping this logic pure (no DB, no exchange) is what lets us prove, bar-by-bar, that
the resulting paper trades equal the backtest's trades — parity by construction — in a
fast unit test. The scanner layer only has to apply the actions via its existing
persistence/execution calls.

Matching is by ``(direction, entry_time)``: the kernel's ``entry_time`` is the bar's
open timestamp string (``str(df.index[idx])``), identical across runs over a growing
history prefix, so a recorded paper trade and its kernel counterpart line up exactly.
This is robust to missed scan cycles: any kernel-closed trade with no recorded
counterpart is backfilled, so a gap in scanner uptime never loses trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from forven.strategies.execution_kernel import KernelResult


ActionKind = Literal["open", "close", "backfill", "refresh", "orphan_close"]


def _ts_lt(a: str, b: str) -> bool:
    """a < b for ISO-ish timestamp strings, tolerant of format drift (space vs 'T',
    with/without tz). Falls back to plain string compare."""
    from datetime import datetime, timezone

    def _parse(s: str):
        s = str(s or "").strip().replace(" ", "T")
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    pa, pb = _parse(a), _parse(b)
    if pa is not None and pb is not None:
        return pa < pb
    return str(a) < str(b)


@dataclass
class ReconcileAction:
    kind: ActionKind
    direction: str
    entry_time: str
    # For open/refresh: the kernel's current open-position state (entry_price,
    # size_fraction, stop_price, target_price, trail_pct, entry_bar, regime, …).
    position: dict | None = None
    # For close/backfill: the kernel's finalized trade dict (exit_price, exit_time,
    # pnl_pct (net), exit_reason, …).
    trade: dict | None = None
    # For close/refresh: the recorded paper trade being acted on.
    recorded: dict | None = None
    # For open: True when this open is a FILL-NOW entry — the scanner fills at the current
    # mark + wall-clock now and re-anchors stop/target to that price (a real market order
    # placed the moment the signal is detected), instead of replaying the kernel's historical
    # next-bar-open fill. Set for FRESH kernel entries on the live/paper path; never on the
    # full-replay parity path (which faithfully back-stamps). The field name is retained for
    # continuity with the (now-repurposed) hop-in machinery that applies it.
    late_entry: bool = False
    # True when this action comes from the kernel's PENDING (signal-bar-close) projection:
    # the last CLOSED bar's signal decides an order for the forming bar's open — i.e. NOW.
    # A pending "open" carries the kernel's raw pending payload in ``position`` (the scanner
    # materializes fill/stop/size at its current mark); a pending "close" carries the pending
    # exit payload in ``trade`` (the scanner stamps the reference exit at the current mark).
    pending: bool = False


def _canonical_ts(entry_time: str) -> str:
    """Canonicalize a timestamp string for keying — tolerant of the same format drift
    (space vs 'T', tz present/absent) that ``_ts_lt`` handles, so a recorded OPEN and
    its kernel-finalized close still match when the candle index dtype/tz representation
    shifts between scans. Falls back to the raw string when parsing fails."""
    from datetime import datetime, timezone

    s = str(entry_time or "").strip().replace(" ", "T")
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return str(entry_time)


def _key(direction: str, entry_time: str) -> tuple[str, str]:
    return (str(direction or "long").strip().lower(), _canonical_ts(entry_time))


def reconcile(res: KernelResult, recorded: list[dict], *, recent_cutoff: str | None = None, window_start: str | None = None, fresh_cutoff: str | None = None) -> list[ReconcileAction]:
    """Diff the kernel's view against recorded paper trades → ordered actions.

    ``recorded`` is the strategy's paper trades (open and closed), each a dict with at
    least ``direction``, ``entry_time`` and ``status`` ("open"/"closed"). The kernel
    ``res`` never force-closes, so ``res.closed_trades`` are real exits and
    ``res.open_positions`` is what should be live now.

    ``recent_cutoff`` (an ISO timestamp string, same format the kernel uses for
    ``entry_time``) bounds what gets RECORDED to "from go-live forward": kernel trades
    that ENTERED before it are treated as pre-tracking — their backfill is suppressed
    and such open positions are NOT adopted (they belong on the chart as triggers, not
    as actual trades). This is what stops a fresh/reset paper book from replaying the
    strategy's ENTIRE would-be history as trades. Closes/refreshes of ALREADY-recorded
    trades always proceed regardless of the cutoff. ``None`` (default) = full replay,
    which is the backtest-parity semantics the tests assert.

    ``fresh_cutoff`` (an ISO timestamp; live/paper only) bounds which kernel OPEN positions
    are actually opened, and HOW. The scanner passes the timestamp of the Nth-from-last
    closed bar (``paper_kernel_fill_now_max_bars``):
      * a kernel entry ON/AFTER ``fresh_cutoff`` (FRESH) → emit a ``late_entry`` open: the
        scanner fills it at the CURRENT mark + wall-clock now and re-anchors stop/target,
        exactly as a real market order placed when the signal is detected (mirrors the live
        path). Keyed by the kernel's historical entry_time so the next scan REFRESHes it.
      * a kernel entry BEFORE ``fresh_cutoff`` but on/after ``recent_cutoff`` (recent but
        stale — a scan-gap catch-up or a long-held signal) → NO action: left as a chart
        trigger only. Never back-stamp an hours-old entry into an OPEN position; never chase
        a stale signal at the current price.
    ``None`` (default) disables the bound → faithful back-stamp opens at the kernel's
    historical entry, the trade-for-trade backtest-parity semantics the pure-kernel tests
    assert.

    Actions, in apply order:
      * ``close``    — a recorded OPEN trade the kernel has now finalized.
      * ``backfill`` — a kernel-finalized trade (entry ≥ cutoff) with no recorded
                       counterpart (opened & closed between scans) → record it closed.
      * ``open``     — a kernel OPEN position (entry ≥ cutoff) with no recorded
                       counterpart → open it.
      * ``refresh``  — a kernel OPEN position matching a recorded OPEN trade → update
                       its SL/TP/trailing for display.
    """
    def _recent(entry_time: str) -> bool:
        # entry_time >= recent_cutoff, but via the tolerant timestamp parse (space-vs-'T',
        # tz present/absent) so the cutoff can be ANY ISO-ish string — e.g. a persisted
        # paper go-live timestamp — not only the kernel's exact pandas-Timestamp format.
        # Falls back to a raw string compare when either side won't parse, so it's identical
        # to the old behaviour for same-format inputs.
        return recent_cutoff is None or not _ts_lt(entry_time, recent_cutoff)

    def _fresh(entry_time: str) -> bool:
        # FRESH = entry on/after fresh_cutoff (the Nth-from-last closed bar the scanner
        # passes). Only a JUST-filled entry is opened, at the current mark/time (fill-now);
        # older still-held entries are left as chart triggers. None disables the bound →
        # faithful back-stamp opens (full-history parity replay).
        return fresh_cutoff is None or not _ts_lt(entry_time, fresh_cutoff)

    recorded_by_key: dict[tuple[str, str], dict] = {}
    recorded_open_by_dir: dict[str, dict] = {}
    for r in recorded:
        recorded_by_key[_key(r.get("direction", "long"), r.get("entry_time"))] = r
        if str(r.get("status") or "open").strip().lower() != "closed":
            recorded_open_by_dir.setdefault(str(r.get("direction") or "long").strip().lower(), r)

    matched: set[int] = set()  # id() of recorded dicts consumed by a close/refresh/adopt

    closes: list[ReconcileAction] = []
    backfills: list[ReconcileAction] = []
    # Closes & backfills, in the kernel's chronological (exit) order.
    for kc in res.closed_trades:
        if kc.get("open_at_end"):
            continue  # defensive; simulate() never emits these
        direction = str(kc.get("direction", "long")).strip().lower()
        entry_time = str(kc.get("entry_time"))
        k = _key(direction, entry_time)
        r = recorded_by_key.get(k)
        if r is None:
            # Record a missed round-trip only if its entry was RECENT (since go-live) AND
            # FRESH enough that we'd have opened it (fill-now). A stale round-trip during an
            # outage is NOT synthesized — we never held it, so inventing it would corrupt the
            # book (same reason a stale OPEN is left as a chart trigger below). fresh_cutoff
            # None → _fresh is always True → unchanged full-history catch-up (parity).
            if _recent(entry_time) and _fresh(entry_time):
                backfills.append(ReconcileAction("backfill", direction, entry_time, trade=kc))
        elif str(r.get("status") or "open").strip().lower() != "closed":
            closes.append(ReconcileAction("close", direction, entry_time, trade=kc, recorded=r))
            matched.add(id(r))
        else:
            matched.add(id(r))  # already recorded closed → nothing to do, but it IS matched.

    # PENDING EXIT (signal-bar close): the kernel STILL holds this position, but the last
    # CLOSED bar's signal/time-stop already decided an exit at the forming bar's open —
    # i.e. NOW. Close the recorded trade this scan instead of waiting a full bar for the
    # fill bar to close (the one-bar exit lag). Live/paper mode only (fresh_cutoff set):
    # the full-replay parity path keeps the faithful historical-close semantics.
    pending_closes: list[ReconcileAction] = []
    closing_ids: set[int] = set(id(a.recorded) for a in closes if a.recorded is not None)
    _pending_exits = getattr(res, "pending_exits", None) or {}
    _pending_entries = getattr(res, "pending_entries", None) or {}
    if fresh_cutoff is not None:
        for direction, pe in _pending_exits.items():
            direction = str(direction or "long").strip().lower()
            entry_time = str(pe.get("entry_time") or "")
            r = recorded_by_key.get(_key(direction, entry_time))
            if r is None or str(r.get("status") or "open").strip().lower() == "closed" or id(r) in matched:
                continue  # never recorded / already closed (an earlier scan this bar) / consumed
            pending_closes.append(ReconcileAction(
                "close", direction, entry_time, trade=dict(pe), recorded=r, pending=True,
            ))
            matched.add(id(r))
            closing_ids.add(id(r))

    opens: list[ReconcileAction] = []
    refreshes: list[ReconcileAction] = []
    kernel_open_dirs: set[str] = set()
    for direction, pos in res.open_positions.items():
        direction = str(direction or "long").strip().lower()
        kernel_open_dirs.add(direction)
        entry_time = str(pos.get("entry_time"))
        r = recorded_by_key.get(_key(direction, entry_time))
        if r is not None and str(r.get("status") or "open").strip().lower() != "closed" and id(r) not in matched:
            refreshes.append(ReconcileAction("refresh", direction, entry_time, position=pos, recorded=r))
            matched.add(id(r))
            continue
        # No exact (direction, entry_time) match. Before opening a NEW trade, ADOPT a
        # same-direction recorded OPEN whose entry has DRIFTED — e.g. it was opened on a
        # different data source (the HL→Binance switch) or a non-kernel path, so its
        # kernel_entry_time no longer matches. There is ≤1 open per direction (unique
        # index), so this is unambiguous and avoids a duplicate-open + a stranded orphan.
        r_dir = recorded_open_by_dir.get(direction)
        if r_dir is not None and id(r_dir) not in matched:
            refreshes.append(ReconcileAction("refresh", direction, entry_time, position=pos, recorded=r_dir))
            matched.add(id(r_dir))
        elif r is not None:
            # A recorded trade ALREADY exists for this exact kernel position (an OPEN match
            # was handled above, so this ``r`` is CLOSED). The kernel still holds the position,
            # but the close was a deliberate exit the kernel can't see — a manual close/flip,
            # or a late hop-in finalized at its re-anchored stop. RE-OPENING it would silently
            # revert the user's action and double-count its PnL, so suppress the open. (When
            # the kernel later genuinely exits, the close loop matches this closed row and is a
            # no-op; a NEW signal enters on a fresh bar = a new entry_time = r is None = opens.)
            continue
        elif _recent(entry_time):  # don't adopt a position that opened before tracking began
            # The kernel holds a position with no recorded counterpart. How we open it:
            #  • fresh_cutoff None (full-replay parity): faithful back-stamp at the kernel's
            #    historical next-bar-open entry — trade-for-trade backtest parity.
            #  • FRESH (entry within the fill-now window): open FILL-NOW — the scanner fills
            #    at the current mark + wall-clock now and re-anchors stop/target, exactly as a
            #    real market order placed when the signal is detected (mirrors live). Keyed by
            #    the kernel's historical entry_time so the next scan REFRESHes (not re-opens).
            #  • recent but NOT fresh (gap catch-up / long-held stale signal): SKIP — leave it
            #    as a chart trigger only. Never back-stamp an hours-old entry into an OPEN
            #    position; never chase a stale signal at the current price.
            if fresh_cutoff is None:
                opens.append(ReconcileAction("open", direction, entry_time, position=pos))
            elif _fresh(entry_time):
                opens.append(ReconcileAction("open", direction, entry_time, position=pos, late_entry=True))
            # else: recent-but-stale → no action (chart trigger only)

    # PENDING ENTRY (signal-bar close): the last CLOSED bar's entry signal fills at the
    # forming bar's open — i.e. NOW. Open it fill-now immediately, keyed by the projected
    # fill-bar entry_time the scanner stamped, so once the fill bar closes and the kernel
    # actually holds the position, the next scan reconciles it as a plain REFRESH (and a
    # same-bar kernel round-trip reconciles as a plain CLOSE). Live/paper mode only.
    pending_opens: list[ReconcileAction] = []
    if fresh_cutoff is not None:
        for direction, pe in _pending_entries.items():
            direction = str(direction or "long").strip().lower()
            entry_time = str(pe.get("entry_time") or "")
            if not entry_time:
                continue  # scanner stamped no projected fill-bar label (stale frame) → skip
            if direction in kernel_open_dirs and direction not in _pending_exits:
                continue  # defensive: kernel still holds this direction and isn't exiting
            r = recorded_by_key.get(_key(direction, entry_time))
            if r is not None:
                # Already acted on this projected entry (an earlier scan within the same
                # forming bar), or the operator closed it mid-bar (never re-open — the same
                # suppression as the closed-row rule above). Mark it matched so the orphan
                # pass (the kernel is necessarily flat on it until the fill bar closes)
                # leaves the still-open row alone.
                matched.add(id(r))
                continue
            r_dir = recorded_open_by_dir.get(direction)
            if r_dir is not None and id(r_dir) not in closing_ids:
                continue  # slot held by another OPEN row that isn't closing this round
            if not _recent(entry_time):
                continue
            pending_opens.append(ReconcileAction(
                "open", direction, entry_time, position=dict(pe), late_entry=True, pending=True,
            ))

    # ORPHAN CLOSE: a recorded OPEN trade the kernel can no longer see (no exact match,
    # not adopted) for a direction the kernel now holds NO position on → the kernel has
    # exited it. Converge by closing it, so paper never holds a trade the strategy/kernel
    # already covered. Guarded to entries WITHIN the evaluated window (the kernel can't
    # speak to an entry older than its history). ``window_start`` None disables the guard
    # (the parity tests pass full history, so it never fires there anyway).
    orphan_closes: list[ReconcileAction] = []
    for direction, r in recorded_open_by_dir.items():
        if id(r) in matched or direction in kernel_open_dirs:
            continue
        entry_time = str(r.get("entry_time") or "")
        if window_start is not None and entry_time and _ts_lt(entry_time, str(window_start)):
            continue  # entry predates the kernel's evaluated window → leave it alone
        orphan_closes.append(ReconcileAction("orphan_close", direction, entry_time, recorded=r))

    # Apply closes/backfills before opens so a same-direction re-entry after an exit is
    # never mistaken for a still-open position; orphan-closes last (pure cleanup).
    return closes + pending_closes + backfills + opens + pending_opens + refreshes + orphan_closes
