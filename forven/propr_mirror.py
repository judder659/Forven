"""Propr strategy mirror (PROPR-2).

The operator picks a roster of strategies on the /propr page; this service
copies their trades onto the Propr challenge account. It deliberately does NOT
touch any execution path: live trading dispatches to Hyperliquid exactly as it
always has, paper trading stays local — the mirror is a read-only OBSERVER of
the trades table that places its own independently-sized orders on Propr.

Runs as the `forven-propr-mirror` interval job (60s, seeded only while the
hidden PROPR-1 flag is on). Every placement rides the adapter's permission
guards, so mirroring works unattended on a verifiable paper/trial account and
its ENTRIES fail closed the moment Propr flips the account to real money (then
FORVEN_ALLOW_PROPR_LIVE is required). Its EXITS do not: closes, protective legs
and the post-close cancel take the reduce lane (PROPR-PERM-2), so the mirror can
always unwind what it opened even after that flip.

Semantics:
* OPEN mirror — a roster strategy's paper/live trade opens => place a Propr
  market entry with the trade's stop/TP as a bracket. Sizing is independent of
  the source: a fixed MIRROR_RISK_PCT of the member's slice of the CHALLENGE
  account at the trade's stop distance, notional-capped inside the venue's
  leverage room.
  Entries are skipped (recorded, not retried) when they pre-date the strategy
  joining the roster, are older than the freshness window (a stale entry is a
  different trade than the strategy took), or price already crossed the stop.
  RETRY-1 is the one exception to "failed is terminal": a terminally failed
  open re-arms (attempts reset, cooldown-bounded) while its source trade is
  still OPEN and the last scan still shows the strategy emitting the
  same-direction entry signal — the strategy is still asking for that exact
  trade, so lateness alone does not disqualify it, and the signal gate
  REPLACES the freshness window for these.
* CLOSE mirror — the source trade leaves OPEN => reduce-only close of the
  mirrored quantity + best-effort cancel of the resting bracket legs.
* Idempotency — entry intentIds derive deterministically from the source
  trade id, so a re-tick after a lost state write cannot double-open; closes
  are reduceOnly, so a duplicate close is harmless by construction.
* Propr NETS one position per asset (verified 2026-07-31 against the live
  order history — an earlier revision claimed same-side-only merging, which is
  wrong): same-side entries merge into the netted position, and an
  opposite-side entry REDUCES or FLIPS it — and the venue cancels every
  resting protective stop when the position flips, leaving the book
  unprotected. PROPR-NET-1: an open that would net against an opposite-side
  leg (tracked here or live on the venue) is DEFERRED, not placed; it mirrors
  normally if the opposite leg closes inside the freshness window and expires
  to a stale skip past it. Same-side merging is unaffected: each mirrored
  close still reduces the netted position by that trade's own quantity.

Halts (two independent layers, both OPEN-only — closes are never blocked):
* The GLOBAL trading halt (`risk.is_trading_allowed`: kill-switch / daily-loss
  halt / operator STOP). The operator's single "stop everything" control must
  mean everything, venues included — same stance as
  `basket_live.reconcile_basket_live`.
* PROPR-3's account-level halt on the challenge account's own kill rules.

`risk.close_all_positions` deliberately does NOT flatten Propr: it sweeps the
Hyperliquid wallets that hold this system's real capital, and adding a second
independent flatten path for a mirror would be a real order placed by the
kill-switch on a venue it never sized. The mirror instead follows its SOURCES —
when the kill-switch flattens a live trade, the source row leaves OPEN and the
close pass reduces the Propr leg on the next tick (<= 60s). A roster of PAPER
strategies is NOT swept by close_all_positions (live rows only), so those
mirrored legs ride on their own venue stops under the PROPR-3 account halt.
If that ever needs to change, it belongs in close_all_positions, not here.

State: kv `forven:propr-mirror:state` {trade_id: {...}} — display + retry
bookkeeping only. It is NOT a durable position ledger and correctness must not
depend on it: mirrored opens are idempotent by intentId and closes are
reduce-only, but a LOST state entry means nothing here will close that leg. The
per-tick reconcile against `propr.raw_positions()` exists for exactly that gap
— it reports venue positions with no open state entry (kv
`forven:propr-mirror:unmanaged`) so an orphan is visible instead of silent, and
it retires tracked legs whose venue position is GONE (PROPR-LEDGER-2, status
``venue_missing``) so state cannot keep reporting exposure the venue no longer
holds.
Roster: kv `forven:settings` key `propr_mirror_strategies` {sid: added_iso},
toggle `propr_mirror_enabled`. Managed ONLY via /api/propr/mirror (the generic
settings PUT preserves unknown keys; nothing here is in the settings manifest).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from forven.config import propr_enabled
from forven.db import get_db, kv_get, kv_set

log = logging.getLogger("forven.propr_mirror")

MIRROR_ENABLED_KEY = "propr_mirror_enabled"
MIRROR_STRATEGIES_KEY = "propr_mirror_strategies"
STATE_KEY = "forven:propr-mirror:state"
HALT_STATE_KEY = "forven:propr-mirror:halt"
UNMANAGED_STATE_KEY = "forven:propr-mirror:unmanaged"

# Account-level halt (PROPR-3): stop OPENING mirrored positions well before the
# venue's challenge rules fail the attempt. 0.8 = halt at 80% of the allowance
# (e.g. a $150/day cap halts new opens at $120 of daily loss). Closes always
# run — reducing risk is never halted.
DAILY_LOSS_HALT_FRACTION = 0.8
DRAWDOWN_HALT_FRACTION = 0.8
# Used only when the challenge payload doesn't carry readable rules — the
# free-trial terms (3%/day, 6% drawdown), which are also Propr's strictest
# published tier, so the fallback can only ever be MORE conservative.
_FALLBACK_DAILY_LOSS_PCT = 3.0
_FALLBACK_DRAWDOWN_PCT = 6.0

# The mirror's OWN per-trade risk on the member's slice — deliberately NOT
# inherited from the source trade's risk_pct: the challenge account has its own
# goal (pass the phase) and its own kill rules, so aggression is set HERE. At
# 2% of an ~$833 slice, six concurrent stop-outs total ~$100 — inside the $120
# daily halt line — and the PROPR-3 risk budget defers anything that would
# stack past it. MIRROR_RISK_PCT is the DEFAULT; the operator tunes the live
# value from the Settings page (MIRROR_RISK_SETTING_KEY, stored as whole
# percent like every other *_pct settings knob).
MIRROR_RISK_PCT = 0.02
MIRROR_RISK_SETTING_KEY = "propr_mirror_risk_pct"
_MIRROR_RISK_MAX_PCT = 10.0


def mirror_risk_fraction(settings: dict | None = None) -> float:
    """The per-trade risk FRACTION the mirror sizes with.

    Reads the operator's whole-percent setting (2 => 2%); anything unset,
    non-numeric, or non-positive falls back to MIRROR_RISK_PCT, and the value
    is capped at 10% — past that a single stop-out can breach the venue's
    daily rules on its own, which no slice can save.
    """
    s = settings if isinstance(settings, dict) else _settings()
    try:
        pct = float(s.get(MIRROR_RISK_SETTING_KEY))
    except (TypeError, ValueError):
        return MIRROR_RISK_PCT
    if not (pct > 0):  # also catches NaN, which fails every comparison
        return MIRROR_RISK_PCT
    return min(pct, _MIRROR_RISK_MAX_PCT) / 100.0
# Notional headroom inside the venue's leverage caps (5x BTC/ETH, 2x rest).
_NOTIONAL_LEVERAGE_HEADROOM = {"BTC": 4.5, "ETH": 4.5}
_DEFAULT_NOTIONAL_HEADROOM = 1.9

# An entry the mirror missed by more than this is a DIFFERENT trade than the
# strategy took — skip it rather than chase.
OPEN_FRESHNESS_MINUTES = 30
MAX_OPENS_PER_TICK = 3
MAX_OPEN_ATTEMPTS = 3
MAX_CLOSE_ATTEMPTS = 10
_STATE_RETENTION_DAYS = 7

# A terminal close failure is not proof that venue exposure disappeared. Keep
# treating that leg as a live claim until bracket attribution or corroborated
# venue absence retires it.
_UNRESOLVED_CLAIM_STATUSES = frozenset({"open", "close_failed"})

# The venue's "position_not_found_or_not_open" reject. On a reduce-only close
# it means there is nothing left to reduce — the leg is already flat (its stop
# filled first, or per-asset netting consumed it) — so retrying can never
# succeed and the FIRST such reject is terminal.
_PROPR_CODE_NO_POSITION = 13065

# PROPR-LEDGER-2: consecutive venue reads a tracked-open leg must be absent
# from before it is retired as ``venue_missing``. Hysteresis, not a delay knob:
# one partial positions response must not retire a real leg.
_VENUE_MISSING_TICKS = 3

# RETRY-1: how fresh the scanner's signal snapshot must be to prove the entry
# signal is still active (fail closed on a stale snapshot), and how long a
# re-armed entry that fails terminally AGAIN must wait before the next round —
# so a venue that keeps rejecting sees one bounded round per cooldown, not one
# order per tick for as long as a 4h signal stays lit.
RETRY_SIGNAL_MAX_SCAN_AGE_MINUTES = 15
RETRY_REARM_COOLDOWN_MINUTES = 10

# PROPR-CLOSE-1: how much of a reduce-only close may go unfilled and still count
# as complete. The adapter quantizes the close size DOWN to the venue's step, so
# a full close can legitimately report a hair less than the mirrored quantity;
# anything beyond that rounding is a REAL residual position, not noise.
_CLOSE_FILL_TOLERANCE_FRAC = 1e-6


def _settings() -> dict:
    try:
        raw = kv_get("forven:settings", {})
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def mirror_enabled(settings: dict | None = None) -> bool:
    s = settings if isinstance(settings, dict) else _settings()
    return bool(s.get(MIRROR_ENABLED_KEY, False))


def mirror_roster(settings: dict | None = None) -> dict[str, str]:
    """{strategy_id: added_at_iso}. Malformed entries are dropped, not fatal."""
    s = settings if isinstance(settings, dict) else _settings()
    raw = s.get(MIRROR_STRATEGIES_KEY)
    if not isinstance(raw, dict):
        return {}
    roster: dict[str, str] = {}
    for sid, added in raw.items():
        cleaned = str(sid or "").strip()
        if cleaned:
            roster[cleaned] = str(added or "")
    return roster


def set_mirror_config(enabled: bool | None = None, strategy_ids: list[str] | None = None) -> dict:
    """Persist the toggle and/or roster. New roster entries are stamped with
    the current time so pre-existing open trades are never mirrored; entries
    already on the roster keep their original timestamp."""
    settings = _settings()
    if enabled is not None:
        settings[MIRROR_ENABLED_KEY] = bool(enabled)
    if strategy_ids is not None:
        existing = mirror_roster(settings)
        now_iso = datetime.now(timezone.utc).isoformat()
        settings[MIRROR_STRATEGIES_KEY] = {
            str(sid).strip(): existing.get(str(sid).strip(), now_iso)
            for sid in strategy_ids
            if str(sid or "").strip()
        }
    kv_set("forven:settings", settings)
    return {"enabled": mirror_enabled(settings), "strategies": mirror_roster(settings)}


def get_state() -> dict:
    raw = kv_get(STATE_KEY, {}) or {}
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict) -> None:
    kv_set(STATE_KEY, state)


def get_halt_state() -> dict:
    raw = kv_get(HALT_STATE_KEY, {}) or {}
    return raw if isinstance(raw, dict) else {}


def get_unmanaged_state() -> dict:
    """Last reconcile's venue positions that no open state entry accounts for."""
    raw = kv_get(UNMANAGED_STATE_KEY, {}) or {}
    return raw if isinstance(raw, dict) else {}


def _num(value) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _challenge_rules(attempt: dict, equity: float, high_water_mark: float | None) -> dict:
    """The CURRENT phase's risk rules, from the venue's own challenge payload.

    Propr expresses limits as percentages of the phase starting balance
    (maxDailyLossPercent / maxDrawdownPercent) with drawdownType static
    (measured from the starting balance) or trailing (from the high-water
    mark). Unreadable fields fall back to the strictest published tier, so a
    parse failure can only make the halt MORE conservative.
    """
    challenge = attempt.get("challenge") if isinstance(attempt.get("challenge"), dict) else {}
    attempt_phases = [p for p in (attempt.get("phases") or []) if isinstance(p, dict)]
    current_id = str(attempt.get("currentPhaseId") or "")
    att_phase = next(
        (p for p in attempt_phases if str(p.get("attemptPhaseId") or "") == current_id), None
    )
    if att_phase is None and attempt_phases:
        att_phase = next(
            (p for p in attempt_phases if str(p.get("status") or "").lower() == "active"),
            attempt_phases[0],
        )
    starting_balance = _num((att_phase or {}).get("startingBalance")) or _num(
        challenge.get("initialBalance")
    )

    ch_phase = None
    if att_phase is not None:
        phase_id = str(att_phase.get("phaseId") or "")
        ch_phase = next(
            (p for p in (challenge.get("phases") or [])
             if isinstance(p, dict) and str(p.get("phaseId") or "") == phase_id),
            None,
        )
    if ch_phase is None:
        phases = [p for p in (challenge.get("phases") or []) if isinstance(p, dict)]
        ch_phase = phases[0] if phases else {}

    daily_pct = _num(ch_phase.get("maxDailyLossPercent"))
    dd_pct = _num(ch_phase.get("maxDrawdownPercent"))
    target_pct = _num(ch_phase.get("profitTargetPercent"))
    dd_type = str(ch_phase.get("drawdownType") or "").strip().lower()
    source = "challenge"
    if daily_pct is None or dd_pct is None or starting_balance is None:
        source = "defaults"
        daily_pct = daily_pct or _FALLBACK_DAILY_LOSS_PCT
        dd_pct = dd_pct or _FALLBACK_DRAWDOWN_PCT
        starting_balance = starting_balance or equity

    # Drawdown reference/floor pair by type; unknown type takes whichever pair
    # yields the HIGHER floor (the conservative one).
    static_ref = starting_balance
    static_floor = starting_balance * (1 - dd_pct / 100.0)
    trailing_ref = high_water_mark or equity
    trailing_floor = trailing_ref * (1 - dd_pct / 100.0)
    if dd_type == "trailing":
        dd_ref, dd_floor = trailing_ref, trailing_floor
    elif dd_type == "static":
        dd_ref, dd_floor = static_ref, static_floor
    else:
        dd_ref, dd_floor = (
            (trailing_ref, trailing_floor)
            if trailing_floor >= static_floor
            else (static_ref, static_floor)
        )

    return {
        "source": source,
        "starting_balance": starting_balance,
        "daily_loss_limit_usd": starting_balance * daily_pct / 100.0,
        "drawdown_type": dd_type or "unknown",
        "drawdown_ref": dd_ref,
        "drawdown_floor": dd_floor,
        # Informational (not a halt input): the phase's profit goal, so the
        # page can show progress toward passing next to the two kill rules.
        "profit_target_usd": (
            starting_balance * target_pct / 100.0 if target_pct else None
        ),
    }


def _evaluate_halt(attempt: dict, equity: float, now: datetime) -> dict:
    """Account-level halt check, persisted for the page.

    Day-start equity anchors the daily-loss rule. PROPR-ANCHOR-1: this used to
    claim OUR first observation of the UTC day was "a strictly tighter proxy
    than the venue's day anchor" — it is the OPPOSITE. The venue measures the
    day's loss from the balance at the day's start; anchoring on a later
    observation, AFTER part of that loss has already landed, makes our measured
    daily loss SMALLER than the venue's and fires the halt LATE. So:

    * ``same_day`` — today's anchor is already recorded: use it (unchanged).
    * ``carried`` — no anchor for today, but the previous tick's observed equity
      is on record (written every tick): anchor on the HIGHER of that and
      current equity, so an overnight/pre-first-tick loss still counts.
    * ``first_observation`` — nothing on record at all (cold start / wiped
      state). We genuinely cannot know the day's opening balance; anchor on
      current equity and stamp ``anchor_source`` so the panel can say the daily
      rule is only PARTIALLY enforced today. (Anchoring on the PHASE starting
      balance was considered and rejected: once equity sits below the phase
      start it would halt every remaining day of the challenge outright, which
      the drawdown rule already covers more precisely.)

    ``anchor_source`` is carried through the day so a day that began blind stays
    flagged as such.
    """
    prev = get_halt_state()
    day = now.strftime("%Y-%m-%d")
    if str(prev.get("day") or "") == day and _num(prev.get("day_start_equity")):
        day_start = float(prev["day_start_equity"])
        anchor_source = str(prev.get("anchor_source") or "same_day")
    else:
        carried = _num(prev.get("equity"))
        if carried:
            day_start = max(float(equity), carried)
            anchor_source = "carried"
        else:
            day_start = equity
            anchor_source = "first_observation"

    account = attempt.get("account") if isinstance(attempt.get("account"), dict) else {}
    high_water_mark = _num(account.get("highWaterMark"))
    rules = _challenge_rules(attempt, equity, high_water_mark)

    reasons: list[str] = []
    daily_loss = max(0.0, day_start - equity)
    daily_budget = rules["daily_loss_limit_usd"] * DAILY_LOSS_HALT_FRACTION
    if daily_loss >= daily_budget:
        reasons.append(
            f"daily loss ${daily_loss:.2f} reached {DAILY_LOSS_HALT_FRACTION:.0%} of the "
            f"${rules['daily_loss_limit_usd']:.2f} venue cap"
        )
    dd_used = max(0.0, rules["drawdown_ref"] - equity)
    dd_allowance = max(0.0, rules["drawdown_ref"] - rules["drawdown_floor"])
    if dd_allowance > 0 and dd_used >= DRAWDOWN_HALT_FRACTION * dd_allowance:
        reasons.append(
            f"drawdown ${dd_used:.2f} reached {DRAWDOWN_HALT_FRACTION:.0%} of the "
            f"${dd_allowance:.2f} allowance ({rules['drawdown_type']})"
        )

    halt = {
        "day": day,
        "day_start_equity": day_start,
        "anchor_source": anchor_source,
        # The panel should say so when today's daily rule is only partially
        # enforced (we never saw the day's opening balance).
        "daily_rule_fully_enforced": anchor_source != "first_observation",
        "equity": equity,
        "daily_loss": daily_loss,
        "daily_loss_limit_usd": rules["daily_loss_limit_usd"],
        "daily_halt_at_usd": daily_budget,
        "drawdown_used": dd_used,
        "drawdown_allowance_usd": dd_allowance,
        "drawdown_type": rules["drawdown_type"],
        "starting_balance": rules["starting_balance"],
        "profit_target_usd": rules["profit_target_usd"],
        "profit_progress_usd": equity - rules["starting_balance"],
        "rules_source": rules["source"],
        "halted": bool(reasons),
        "reasons": reasons,
        "checked_at": now.isoformat(),
    }
    kv_set(HALT_STATE_KEY, halt)

    if reasons and not prev.get("halted"):
        log.warning("Propr mirror HALTED (opens blocked): %s", "; ".join(reasons))
        try:
            from forven.notifications import emit_notification
            emit_notification(
                "propr_mirror_halt",
                severity="warning",
                source="propr_mirror",
                title="Propr mirror halted — challenge limit proximity",
                summary="; ".join(reasons),
                body=(
                    "New mirrored opens are blocked to protect the challenge account. "
                    "Closes still execute. The daily-loss halt clears at the next UTC "
                    "day; the drawdown halt clears if equity recovers."
                ),
                dedupe_key=f"propr_mirror_halt:{day}",
            )
        except Exception as exc:
            log.debug("Could not emit propr mirror halt notification: %s", exc)
    return halt


def roster_candidates() -> list[dict]:
    """Strategies the picker offers: paper / live_graduated / gauntlet stages."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, COALESCE(display_name, name) AS name, stage, timeframe "
            "FROM strategies WHERE stage IN ('paper', 'live_graduated', 'gauntlet') "
            "ORDER BY stage, id"
        ).fetchall()
    return [dict(r) for r in rows]


def _parse_when(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _signal_data(row: dict) -> dict:
    raw = row.get("signal_data")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _stop_and_tp(row: dict) -> tuple[float | None, float | None]:
    data = _signal_data(row)

    def _num(*keys):
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    return _num("stop_loss_price", "stop_loss"), _num("take_profit_price", "take_profit")


def _roster_trades(roster_ids: set[str]) -> list[dict]:
    """OPEN paper/live trades belonging to roster strategies."""
    if not roster_ids:
        return []
    placeholders = ",".join("?" for _ in roster_ids)
    params = list(roster_ids)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, strategy, strategy_id, asset, direction, entry_price, risk_pct, "
            f"leverage, status, execution_type, opened_at, signal_data FROM trades "
            f"WHERE status = 'OPEN' AND execution_type IN ('paper', 'live') "
            f"AND (strategy_id IN ({placeholders}) OR strategy IN ({placeholders}))",
            params + params,
        ).fetchall()
    return [dict(r) for r in rows]


def _trade_status(trade_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return str(dict(row)["status"]) if row else None


def _trade_roster_key(row: dict, roster_ids: set[str]) -> str | None:
    for key in ("strategy_id", "strategy"):
        value = str(row.get(key) or "").strip()
        if value and value in roster_ids:
            return value
    return None


def _entry_signal_snapshot(now: datetime) -> dict | None:
    """The last scan's per-strategy signal snapshot, or None when it is missing
    or too stale to prove anything (fail closed — a RETRY-1 re-arm needs a
    CURRENT signal, not a memory of one)."""
    try:
        scanner_state = kv_get("scanner_state", {}) or {}
    except Exception:
        return None
    if not isinstance(scanner_state, dict):
        return None
    signals = scanner_state.get("signals")
    if not isinstance(signals, dict):
        return None
    last_scan = _parse_when(scanner_state.get("last_scan"))
    if last_scan is None or (now - last_scan) > timedelta(
        minutes=RETRY_SIGNAL_MAX_SCAN_AGE_MINUTES
    ):
        return None
    return signals


def _entry_signal_active(row: dict, signals: dict | None) -> bool:
    """True when the snapshot shows this trade's strategy STILL emitting the
    same-direction entry signal. Strategies that publish directional_signals
    gate on the per-direction entry flag (the scalar entry_signal can be
    false while the other direction fires)."""
    if not isinstance(signals, dict):
        return False
    sid = str(row.get("strategy_id") or row.get("strategy") or "").strip()
    sig = signals.get(sid)
    if not isinstance(sig, dict):
        return False
    direction = str(row.get("direction") or "").strip().lower()
    directional = sig.get("directional_signals")
    if isinstance(directional, dict) and f"{direction}_entry" in directional:
        return bool(directional.get(f"{direction}_entry"))
    return bool(sig.get("entry_signal")) and (
        str(sig.get("direction") or "").strip().lower() == direction
    )


def mirror_equity_slice(equity: float) -> tuple[float, dict]:
    """SLICE-1 (Propr): one roster member's equal share of the CHALLENGE account.

    The mirror had the same defect the Hyperliquid live path did — every roster
    member sized against the FULL challenge balance, with no member aware that
    five others were sizing against the same dollars. On the live $5,000 phase
    with a 6-strategy roster that is not a theoretical over-allocation, it is a
    challenge-ending one:

        per-trade risk  = 5000 * 2%      = $100
        six stop-outs   = $600           = 4x the $150 daily-loss limit,
                                           and 2x the $300 drawdown allowance

    i.e. one bad session inside the rules of the game ends the attempt. Dividing
    first makes six simultaneous stop-outs $100 total — 67% of the daily limit —
    and keeps worst-case exposure at MIRROR_RISK_PCT of the challenge no matter
    how large the roster grows.

    Returns (slice_usd, meta). Falls back to the FULL equity only when the roster
    cannot be read, which is the conservative direction here in the sense that it
    preserves today's behaviour rather than silently halving positions on a
    transient KV error — the daily-budget check downstream still bounds it.
    """
    meta: dict = {"roster_size": None, "challenge_equity_usd": round(float(equity or 0.0), 2)}
    try:
        n = max(len(mirror_roster() or {}), 1)
    except Exception:  # noqa: BLE001 — never block a mirror open on the roster read
        return float(equity), {**meta, "reason": "roster unreadable — sized off full equity"}
    return float(equity) / n, {**meta, "roster_size": n,
                               "slice_usd": round(float(equity) / n, 2)}


def _size_mirror_order(
    asset: str, mid: float, stop_price: float,
    leverage: float | None, equity: float,
) -> tuple[float, str | None]:
    """Independent Propr sizing: the operator's mirror risk (see
    mirror_risk_fraction) of this member's SLICE of challenge equity at the
    stop distance, notional-capped inside the venue's leverage room.
    Returns (size, skip_reason).

    ``equity`` is the member's slice (see mirror_equity_slice), not the account —
    the notional headroom below is therefore also per-slice, so N members cannot
    collectively exceed the challenge balance."""
    risk = mirror_risk_fraction()
    stop_dist = abs(mid - stop_price)
    if stop_dist <= 0:
        return 0.0, "zero stop distance"
    size = (equity * risk) / stop_dist
    headroom = _NOTIONAL_LEVERAGE_HEADROOM.get(asset, _DEFAULT_NOTIONAL_HEADROOM)
    if leverage:
        headroom = min(headroom, max(1.0, float(leverage)))
    max_notional = equity * headroom
    if size * mid > max_notional:
        size = max_notional / mid
    return size, None


def _notify_mirror_failure(
    trade_id: str, entry: dict, kind: str, *, reason: str | None = None
) -> None:
    """Terminal mirror failures must reach the operator, not just the page."""
    detail = str(reason or entry.get("reason") or "")
    try:
        from forven.notifications import emit_notification
        emit_notification(
            "propr_mirror_failure",
            severity="warning",
            source="propr_mirror",
            title=f"Propr mirror {kind} failed ({entry.get('asset')})",
            summary=(
                f"{entry.get('strategy')} {entry.get('asset')} {entry.get('direction')} "
                f"(trade {trade_id}): {detail}"
            ),
            body=detail,
            dedupe_key=f"propr_mirror_failure:{trade_id}:{kind}",
        )
    except Exception as exc:
        log.debug("Could not emit propr mirror failure notification: %s", exc)


def _position_key(propr, asset, direction) -> tuple[str, str]:
    """(normalized asset, long|short) — the identity Propr nets positions by."""
    side = str(direction or "").strip().lower()
    return (
        propr.normalize_asset(str(asset or "")),
        "long" if side == "long" else "short",
    )


def _venue_position_keys(propr, positions) -> set[tuple[str, str]]:
    """(asset, side) for every venue position row, sided by the ADAPTER's
    quantity-aware ``position_side`` — a payload with a missing or
    unrecognized side field must never misclassify a real long as short
    (Codex P1 on #113: that would retire the tracked leg and cancel its
    live brackets)."""
    keys: set[tuple[str, str]] = set()
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        asset = propr.normalize_asset(str(pos.get("asset") or pos.get("coin") or ""))
        if asset:
            keys.add((asset, propr.position_side(pos)))
    return keys


def _netting_conflict(propr, state: dict, trade_id: str, asset: str, direction: str) -> str | None:
    """PROPR-NET-1: the reason this open must not be placed, else None.

    Propr keeps ONE netted position per asset, so an opposite-side open is a
    reduce/flip of an existing leg, not a new hedged position — and the venue
    cancels every resting protective stop when the position flips (observed
    2026-07-31: one entry flipped the ETH book long->short and Propr cancelled
    all three resting ETH stops, leaving the exposure unprotected). Check both
    this mirror's open legs and the venue book; an unreadable venue fails
    CLOSED — an order that cannot be verified safe is not placed."""
    opposite = ("short" if direction == "long" else "long")
    for other_id, other in state.items():
        if other_id == trade_id or not isinstance(other, dict):
            continue
        other_asset = propr.normalize_asset(str(other.get("asset") or ""))
        if other_asset == asset and (
            other.get("bracket_cancel_pending") or other.get("bracket_resize_pending")
        ):
            return (
                f"bracket cleanup pending: mirrored trade {other_id} still has an "
                f"unverified reduce-only order on {asset}"
            )
        if other.get("status") not in _UNRESOLVED_CLAIM_STATUSES:
            continue
        if _position_key(propr, other.get("asset"), other.get("direction")) == (asset, opposite):
            return (
                f"netting conflict: mirrored trade {other_id} holds the opposite side of "
                f"{asset} and Propr nets one position per asset"
            )
    try:
        positions = propr.raw_positions() or []
    except Exception as exc:
        return f"venue positions unreadable ({exc}) — failing closed on the netting check"
    if (asset, opposite) in _venue_position_keys(propr, positions):
        return (
            f"netting conflict: the venue already holds an opposite-side {asset} position "
            "and Propr nets one position per asset"
        )
    return None


def _record_open_error(trade_id: str, entry: dict, reason: str) -> None:
    """Bounded-retry bookkeeping for a failed open attempt; notifies once the
    failure turns terminal."""
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    entry["reason"] = reason
    if entry["attempts"] < MAX_OPEN_ATTEMPTS:
        entry["status"] = "error"
    else:
        entry["status"] = "failed"
        log.error("Propr mirror open for trade %s FAILED terminally: %s", trade_id, reason)
        _notify_mirror_failure(trade_id, entry, "open")


def _mirror_open(
    propr, row: dict, state: dict, equity: float, now: datetime,
    risk_budget: dict | None = None,
) -> None:
    trade_id = str(row["id"])
    entry = state.setdefault(trade_id, {"status": "pending", "attempts": 0})
    entry.update({
        "strategy": str(row.get("strategy_id") or row.get("strategy") or ""),
        "asset": str(row.get("asset") or ""),
        "direction": str(row.get("direction") or ""),
        "source_execution_type": str(row.get("execution_type") or ""),
    })

    asset = propr.normalize_asset(str(row.get("asset") or ""))
    direction = str(row.get("direction") or "").strip().lower()
    stop_price, tp_price = _stop_and_tp(row)
    if stop_price is None:
        entry.update({"status": "skipped", "reason": "source trade has no stop — never mirror unprotected"})
        return

    mid = float(propr.get_all_mids().get(asset, 0) or 0)
    if mid <= 0:
        _record_open_error(trade_id, entry, f"no mid price for {asset}")
        return

    # Price already through the stop => the trade is over before we arrived.
    if (direction == "long" and mid <= stop_price) or (direction == "short" and mid >= stop_price):
        entry.update({"status": "skipped", "reason": "price already beyond the stop at mirror time"})
        return

    # PROPR-NET-1: never place an open that would net against an opposite-side
    # leg. Deferred (status pending), not skipped — it mirrors normally if the
    # opposite leg closes inside the freshness window, and past the window the
    # tick loop expires it to a stale skip like any other aged entry.
    conflict = _netting_conflict(propr, state, trade_id, asset, direction)
    if conflict:
        entry.update({"status": "pending", "reason": f"deferred: {conflict}"})
        return

    # SLICE-1: size against this member's share of the challenge, not the whole
    # balance. Six members each sizing off full equity put 2x the account at risk
    # and could exhaust the drawdown allowance in one session.
    slice_usd, slice_meta = mirror_equity_slice(equity)
    entry["capital_slice"] = slice_meta
    size, skip_reason = _size_mirror_order(
        asset, mid, stop_price, row.get("leverage"), slice_usd
    )
    if skip_reason or size <= 0:
        entry.update({"status": "skipped", "reason": skip_reason or "size resolved to zero"})
        return

    # PROPR-3: loss-at-stop of everything already open counts against the
    # remaining daily budget, so N concurrent stop-outs can't stack past the
    # venue's daily cap. Deferred (status stays pending) — it retries next
    # tick and mirrors normally if room frees up inside the freshness window.
    risk_usd = size * abs(mid - stop_price)
    if risk_budget is not None:
        if risk_usd > risk_budget.get("remaining", 0.0):
            entry.update({
                "status": "pending",
                "reason": (
                    f"deferred: risk-at-stop ${risk_usd:.2f} exceeds the remaining "
                    f"daily budget ${risk_budget.get('remaining', 0.0):.2f}"
                ),
            })
            return

    lev = propr.set_leverage(asset, float(row.get("leverage") or 1.0))
    if isinstance(lev, dict) and lev.get("error"):
        _record_open_error(trade_id, entry, f"set_leverage: {lev['error']}")
        return

    result = propr.market_order(
        asset,
        "buy" if direction == "long" else "sell",
        size,
        stop_loss_price=stop_price,
        take_profit_price=tp_price,
        idempotency_key=f"propr-mirror:{trade_id}",
    )
    if isinstance(result, dict) and not result.get("error"):
        entry.update({
            "status": "open",
            "reason": None,
            "quantity": result.get("filled_size") or size,
            "entry_price": result.get("entry_price"),
            "entry_order_id": result.get("entry_order_id"),
            "stop_order_id": result.get("stop_order_id"),
            "take_profit_order_id": result.get("take_profit_order_id"),
            # RACE-1: the close path cancels brackets before reducing and must
            # be able to RE-ARM the stop if the close then defers — keep the
            # level it would re-arm at.
            "stop_price": float(stop_price),
            "risk_usd": risk_usd,
            "opened_at": now.isoformat(),
        })
        if risk_budget is not None:
            risk_budget["remaining"] = max(0.0, risk_budget.get("remaining", 0.0) - risk_usd)
        log.info("Propr mirror OPEN %s %s %s (size %.6g) for trade %s",
                 asset, direction, entry["strategy"], size, trade_id)
        # PROPR-LEG-1: the entry filled but the venue rejected a bracket leg. A
        # rejected STOP left a leveraged challenge position running naked while
        # the PROPR-3 daily budget kept counting its risk_usd as bounded — the
        # one thing this mirror refuses to do at open time (see the "never mirror
        # unprotected" skip above) happening after the fact. Re-arm standalone
        # (mirrors scanner.py's HL-1 handling); if it still can't be armed, the
        # position is closed rather than left running.
        _failed_legs = result.get("protective_leg_failed") or []
        if "stop" in _failed_legs:
            _rearm_or_close_unprotected(propr, trade_id, entry, asset, direction, stop_price, now)
    else:
        _record_open_error(trade_id, entry, str((result or {}).get("error") or "order rejected"))


def _rearm_or_close_unprotected(
    propr, trade_id: str, entry: dict, asset: str, direction: str,
    stop_price: float, now: datetime,
) -> None:
    """Arm a rejected stop leg standalone; flatten the mirror if it won't arm."""
    quantity = _num(entry.get("quantity")) or 0.0
    rearmed = None
    try:
        rearmed = propr.place_protective_stop(asset, direction, quantity, float(stop_price))
    except Exception as exc:
        log.error("Propr mirror: stop re-arm for trade %s raised: %s", trade_id, exc)
        rearmed = {"error": str(exc)}
    if isinstance(rearmed, dict) and not rearmed.get("error") and rearmed.get("stop_order_id"):
        entry["stop_order_id"] = str(rearmed["stop_order_id"])
        entry["protective_stop_rearmed"] = True
        log.warning("Propr mirror: re-armed the rejected stop leg for trade %s", trade_id)
        return

    err = (rearmed or {}).get("error") if isinstance(rearmed, dict) else rearmed
    entry["stop_unarmed"] = True
    entry["reason"] = f"stop leg rejected and could not be re-armed: {err}"
    log.critical(
        "Propr mirror: %s %s for trade %s is UNPROTECTED (stop rejected, re-arm failed: %s) "
        "— closing the mirrored position", asset, direction, trade_id, err,
    )
    try:
        from forven.notifications import emit_notification
        emit_notification(
            "propr_mirror_unprotected",
            severity="critical",
            source="propr_mirror",
            title=f"Propr mirror position UNPROTECTED ({asset})",
            summary=(
                f"{entry.get('strategy')} {asset} {direction} (trade {trade_id}): the stop "
                "leg was rejected and could not be re-armed — closing the mirrored position."
            ),
            body=str(err or ""),
            dedupe_key=f"propr_mirror_unprotected:{trade_id}",
        )
    except Exception as exc:
        log.debug("Could not emit propr mirror unprotected notification: %s", exc)
    # Flatten: an unprotected leveraged position on a challenge account is a
    # worse outcome than an unmirrored trade. _mirror_close sets the terminal
    # status (closed / retryable error) itself.
    try:
        _mirror_close(propr, trade_id, entry, now)
    except Exception as exc:
        log.error("Propr mirror: emergency close of unprotected %s failed: %s", trade_id, exc)
        entry["status"] = "error"
        entry["reason"] = f"unprotected and emergency close raised: {exc}"


def _cancel_bracket_legs(
    propr, asset: str, entry: dict, *, trade_id: str | None = None
) -> dict[str, str]:
    """Cancel a retired leg's resting stop/TP orders and retain failures.

    The bracket legs are reduce-only so they can never re-open a position,
    but on a netted venue an orphan can still reduce a same-side sibling. A
    failed cancellation therefore remains durable state and is retried on
    later mirror ticks instead of being forgotten with the retired leg."""
    failures: dict[str, str] = {}
    for leg_key in ("stop_order_id", "take_profit_order_id"):
        leg_id = entry.get(leg_key)
        if leg_id:
            try:
                cancelled = propr.cancel_order(asset, leg_id)
                if not isinstance(cancelled, dict):
                    failures[leg_key] = "venue returned no cancellation result"
                elif cancelled.get("error"):
                    failures[leg_key] = str(cancelled["error"])
            except Exception as exc:
                failures[leg_key] = str(exc)
    if failures:
        entry["bracket_cancel_pending"] = failures
        detail = "; ".join(
            f"{leg_key}: {reason}" for leg_key, reason in sorted(failures.items())
        )
        entry["bracket_cancel_error"] = detail
        log.error(
            "Propr mirror: retired leg %s still has unverified bracket order(s): %s",
            trade_id or "<unknown>", detail,
        )
        if trade_id:
            _notify_mirror_failure(
                trade_id, entry, "bracket cancellation", reason=detail
            )
    else:
        entry.pop("bracket_cancel_pending", None)
        entry.pop("bracket_cancel_error", None)
    return failures


def _retry_pending_bracket_cancellations(propr, state: dict, summary: dict) -> None:
    """Retry orphan-order cleanup retained on otherwise terminal legs."""
    for trade_id, entry in state.items():
        if not isinstance(entry, dict) or not entry.get("bracket_cancel_pending"):
            continue
        asset = propr.normalize_asset(str(entry.get("asset") or ""))
        if _cancel_bracket_legs(propr, asset, entry, trade_id=str(trade_id)):
            summary["bracket_cancel_pending"] = summary.get("bracket_cancel_pending", 0) + 1
        else:
            summary["bracket_cancel_recovered"] = summary.get("bracket_cancel_recovered", 0) + 1


def _defer_close(trade_id: str, entry: dict, reason: str) -> None:
    """Bounded deferral bookkeeping for a close that must not be placed yet."""
    attempts = int(entry.get("close_attempts") or 0) + 1
    entry["close_attempts"] = attempts
    entry["reason"] = reason
    if attempts >= MAX_CLOSE_ATTEMPTS:
        entry["status"] = "close_failed"
        log.error("Propr mirror: close for trade %s FAILED after %d attempts: %s",
                  trade_id, attempts, entry["reason"])
        _notify_mirror_failure(trade_id, entry, "close")


def _rearm_deferred_leg(propr, trade_id: str, entry: dict, asset: str, direction: str) -> None:
    """RACE-1 companion: a deferred or partially-closed leg whose brackets were
    already cancelled is real exposure with no stop until the next tick. Re-arm
    the stop at the entry's stored level; a leg that cannot be re-armed gets a
    critical notification rather than silent nakedness."""
    stop_price = _num(entry.get("stop_price"))
    quantity = _num(entry.get("quantity")) or 0.0
    if stop_price and quantity > 0:
        rearmed = None
        try:
            rearmed = propr.place_protective_stop(asset, direction, quantity, float(stop_price))
        except Exception as exc:
            rearmed = {"error": str(exc)}
        if isinstance(rearmed, dict) and not rearmed.get("error") and rearmed.get("stop_order_id"):
            entry["stop_order_id"] = str(rearmed["stop_order_id"])
            entry.pop("stop_unarmed", None)
            log.warning(
                "Propr mirror: re-armed the stop for trade %s while its close waits", trade_id
            )
            return
    entry["stop_unarmed"] = True
    log.critical(
        "Propr mirror: %s %s (trade %s) is waiting to close WITHOUT brackets — "
        "stop re-arm unavailable (stored stop %s)", asset, direction, trade_id,
        entry.get("stop_price"),
    )
    try:
        from forven.notifications import emit_notification
        emit_notification(
            "propr_mirror_unprotected",
            severity="critical",
            source="propr_mirror",
            title=f"Propr mirror leg unprotected while closing ({asset})",
            summary=(
                f"{entry.get('strategy')} {asset} {direction} (trade {trade_id}): brackets "
                "were cancelled ahead of a close that then had to wait, and the stop could "
                "not be re-armed."
            ),
            body=str(entry.get("reason") or ""),
            dedupe_key=f"propr_mirror_unprotected:{trade_id}",
        )
    except Exception as exc:
        log.debug("Could not emit propr mirror unprotected notification: %s", exc)


def _mirror_close(propr, trade_id: str, entry: dict, now: datetime, state: dict | None = None) -> None:
    asset = propr.normalize_asset(str(entry.get("asset") or ""))
    direction = str(entry.get("direction") or "").strip().lower()
    quantity = float(entry.get("quantity") or 0)
    if quantity <= 0:
        entry.update({"status": "closed", "reason": "nothing to close (zero mirrored quantity)"})
        # A zero-claim leg can still carry live bracket orders (e.g. its claim
        # was consumed piecemeal by partial fills) — a terminal record must not
        # strand a resting reduce-only order that can fire into another leg's
        # share of the netted position.
        _cancel_bracket_legs(propr, asset, entry, trade_id=trade_id)
        return

    # PROPR-LEG-2 (Codex P1 on #113): when another tracked-open leg shares the
    # SAME side of this netted position, a reduce-only close sized off this
    # leg's ledger quantity can consume the OTHER leg's share (this leg's stop
    # may have filled venue-side between attribution passes). Close only what
    # the venue holds beyond the other legs' claims; if nothing is left, the
    # venue already consumed this leg. The ambiguity only exists with another
    # same-side leg — a sole leg keeps the plain path, where a reduce-only
    # close is harmless by construction. ``state`` is optional only for older
    # callers/tests; the tick always passes it.
    key = _position_key(propr, entry.get("asset"), entry.get("direction"))
    # close_failed siblings count as claims (Codex P1 on #116, round 2b): an
    # unconfirmed close means the sibling's venue share may still be live, and
    # dropping it from the clamp would hand this close the sole-leg path — the
    # blind full-quantity close this function exists to prevent. The claim
    # clears when venue state proves it flat (venue_missing / a flat side) or
    # a bracket fill retires it.
    others = [
        (tid, e) for tid, e in (state or {}).items()
        if tid != trade_id and isinstance(e, dict)
        and e.get("status") in _UNRESOLVED_CLAIM_STATUSES
        and _position_key(propr, e.get("asset"), e.get("direction")) == key
    ]
    clamped_from = None
    if others:
        # RACE-1 (Codex P1 on #116, round 2): between any snapshot and the
        # close submission this leg's OWN brackets can fill — most likely on
        # exactly the price move that closed the source trade — and a bracket
        # fill after sizing double-reduces this leg's claim, with the excess
        # coming out of the sibling's share. So cancel own brackets FIRST and
        # verify their final state: once confirmed cancelled they can never
        # fill mid-close. Sibling-bracket races stay safe by arithmetic — a
        # sibling fill reduces the venue quantity and that sibling's claim
        # equally, leaving the clamp margin intact.
        cancel_errors: dict[str, str] = {}
        for leg_key in ("stop_order_id", "take_profit_order_id"):
            oid = entry.get(leg_key)
            if not oid:
                continue
            try:
                cancelled = propr.cancel_order(asset, oid)
                if not isinstance(cancelled, dict):
                    cancel_errors[leg_key] = "venue returned no cancellation result"
                elif cancelled.get("error"):
                    cancel_errors[leg_key] = str(cancelled["error"])
                elif cancelled.get("already_filled_or_cancelled"):
                    cancel_errors[leg_key] = (
                        "venue says already filled or cancelled; order state not yet confirmed"
                    )
            except Exception as exc:
                cancel_errors[leg_key] = str(exc)
                log.debug("Propr mirror: bracket cancel %s before close failed: %s", oid, exc)
        orders = _read_order_rows(propr)
        if orders is None:
            _defer_close(trade_id, entry, (
                "bracket cancellation/order state is unverified because the order book is "
                "unreadable — cannot prove a bracket will not race the close; deferring"
            ))
            if cancel_errors:
                entry["bracket_cancel_unverified"] = sorted(cancel_errors)
                _notify_mirror_failure(trade_id, entry, "bracket cancellation")
            # A successfully-cancelled stop must be replaced while the close
            # waits. An errored/ambiguous stop may still be live, so placing a
            # duplicate could consume a sibling claim later.
            if "stop_order_id" not in cancel_errors:
                _rearm_deferred_leg(propr, trade_id, entry, asset, direction)
            return
        for leg_key, label in (("stop_order_id", "stop"), ("take_profit_order_id", "take-profit")):
            oid = entry.get(leg_key)
            order = orders.get(str(oid)) if oid else None
            if order is None:
                # Absent from the listing after a cancel that raised nothing:
                # it was resting and is now gone — no fill to account.
                continue
            status = _order_row_status(order)
            if status == "filled":
                entry.update({
                    "status": "closed",
                    "reason": (
                        f"{label} order {oid} filled in the close race window — the venue "
                        "consumed this leg's share before the mirror close was placed"
                    ),
                    "exit_price": _order_row_fill_price(order),
                    "closed_quantity": _order_row_filled_size(order) or entry.get("quantity"),
                    "closed_at": now.isoformat(),
                    "bracket_filled": label,
                })
                log.warning(
                    "Propr mirror: %s for trade %s filled before its close could be "
                    "placed — retiring the leg instead of closing", label, trade_id,
                )
                return
            if status in ("cancelled", "canceled", "rejected", "expired"):
                # Even if the cancellation call itself failed, the fresh order
                # row proves this bracket can no longer race the close.
                cancel_errors.pop(leg_key, None)
                continue
            if status == "partially_filled":
                filled_so_far = float(_order_row_filled_size(order) or 0.0)
                accounted_key = f"bracket_partial_accounted_{leg_key}"
                accounted = float(_num(entry.get(accounted_key)) or 0.0)
                delta = filled_so_far - accounted
                if delta > 1e-12:
                    entry[accounted_key] = filled_so_far
                    quantity = max(0.0, quantity - delta)
                    entry["quantity"] = round(quantity, 10)
        if quantity <= 1e-12:
            entry.update({
                "status": "closed",
                "reason": "brackets consumed the whole claim before the close was placed",
                "closed_at": now.isoformat(),
            })
            return
        if cancel_errors:
            failed_legs = ", ".join(sorted(cancel_errors))
            details = "; ".join(
                f"{leg_key}: {reason}" for leg_key, reason in sorted(cancel_errors.items())
            )
            _defer_close(trade_id, entry, (
                f"bracket cancellation unverified ({failed_legs}) — deferring to avoid "
                f"a bracket/close race that could consume a sibling claim: {details}"
            ))
            entry["bracket_cancel_unverified"] = sorted(cancel_errors)
            log.error(
                "Propr mirror: close for trade %s deferred because bracket cancellation "
                "could not be verified: %s", trade_id, details,
            )
            _notify_mirror_failure(trade_id, entry, "bracket cancellation")
            # Re-arm only when the old stop is known not to be live. If its
            # cancellation is the uncertainty, another reduce-only stop could
            # later fire as well and consume a same-side sibling's share.
            if "stop_order_id" not in cancel_errors:
                _rearm_deferred_leg(propr, trade_id, entry, asset, direction)
            return
        entry.pop("bracket_cancel_unverified", None)
        try:
            positions = propr.raw_positions() or []
        except Exception:
            # Unreadable venue + shared side: deferring is the only close that
            # cannot be wrong. Brackets are already cancelled, so re-arm the
            # stop while we wait. Bounded, so a persistent outage still
            # surfaces as close_failed instead of deferring forever.
            _defer_close(trade_id, entry, (
                f"venue positions unreadable — deferring: another mirrored leg shares "
                f"this {key[0]} {key[1]} position and a blind close could consume its share"
            ))
            _rearm_deferred_leg(propr, trade_id, entry, asset, direction)
            return
        venue_qty = 0.0
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            pos_asset = propr.normalize_asset(str(pos.get("asset") or pos.get("coin") or ""))
            if pos_asset == key[0] and propr.position_side(pos) == key[1]:
                venue_qty += abs(
                    _num(pos.get("quantity")) or _num(pos.get("size")) or _num(pos.get("szi")) or 0.0
                )
        other_claims = sum(float(_num(e.get("quantity")) or 0.0) for _, e in others)
        if venue_qty <= 1e-12:
            # One flat read is NOT proof: _retire_venue_missing_legs demands
            # _VENUE_MISSING_TICKS consecutive misses precisely because a
            # single partial positions response can omit a real leg. Defer —
            # the venue_missing hysteresis retires the leg (and releases its
            # claim) once the flat side is corroborated, and the re-armed stop
            # keeps any REAL residual protected meanwhile (a reduce-only stop
            # on a truly flat side is inert and is cancelled at retire time).
            _defer_close(trade_id, entry, (
                f"venue side {key[0]} {key[1]} reads flat — deferring for "
                "venue_missing corroboration before treating the leg as consumed"
            ))
            _rearm_deferred_leg(propr, trade_id, entry, asset, direction)
            return
        closable = min(quantity, venue_qty - other_claims)
        if closable <= 0:
            # The venue holds LESS than the same-side claims but is not flat:
            # some leg was (partly) consumed, and which one is unknowable from
            # position data alone. Guessing here retires brackets on a possibly
            # live leg — defer instead and let the attribution pass (which
            # reads per-order status) resolve identity. Bounded, so a
            # persistent mismatch still alarms as close_failed. Brackets are
            # already cancelled, so re-arm the stop while we wait.
            _defer_close(trade_id, entry, (
                f"deferred: venue {key[0]} {key[1]} holds {venue_qty:.10g} against "
                f"{other_claims + quantity:.10g} of same-side mirror claims — the deficit "
                "cannot be attributed to a specific leg yet"
            ))
            _rearm_deferred_leg(propr, trade_id, entry, asset, direction)
            return
        if closable < quantity:
            clamped_from = quantity
            log.warning(
                "Propr mirror: clamping close for trade %s from %.10g to %.10g — venue %s %s "
                "holds %.10g and other mirrored legs claim %.10g; the residual stays open "
                "and bracketed",
                trade_id, quantity, closable, key[0], key[1], venue_qty, other_claims,
            )

    ask_qty = quantity if clamped_from is None else closable
    result = propr.close_position(asset, ask_qty, "sell" if direction == "long" else "buy")

    # A no-position reject is terminal on the FIRST attempt: the venue has
    # nothing this reduce-only close could reduce — the leg is already flat
    # (its stop filled first, or per-asset netting consumed it) — and no
    # number of retries changes that. Recorded as closed-at-venue, not
    # alarmed as a failure: before this branch existed each such close burned
    # MAX_CLOSE_ATTEMPTS rejects and a close_failed notification for a
    # position that no longer existed.
    if (
        isinstance(result, dict)
        and result.get("error")
        and result.get("error_code") == _PROPR_CODE_NO_POSITION
    ):
        entry.update({
            "status": "closed",
            "reason": "venue reported no open position to reduce (13065) — already flat",
            "venue_position_missing": True,
            "closed_at": now.isoformat(),
        })
        _cancel_bracket_legs(propr, asset, entry, trade_id=trade_id)
        log.warning(
            "Propr mirror: close for trade %s found no venue position (13065) — "
            "recording the leg as already closed", trade_id,
        )
        return
    # PROPR-CLOSE-1: gate the "closed" transition AND the bracket-leg cancels on
    # a fill that CONFIRMS THE WHOLE MIRRORED QUANTITY. Marking a close "closed"
    # retires the position from this ledger AND cancels its stop/TP — leaving a
    # REAL open position on the challenge account with no protection and nothing
    # left watching it. forven/exchange/propr.py::close_position errors on a
    # rejected/cancelled order or a NON-POSITIVE filled quantity, so its clean
    # return only rules out a zero fill: a reduce-only market close that fills 0.4
    # of 1.0 comes back clean and would retire a still-open 0.6 leg. Re-check the
    # quantity here — one layer must not own the whole invariant.
    #
    # A SHORT fill keeps the entry OPEN at its residual size with the bracket legs
    # INTACT (the residual stays protected) and retries next tick; closes are
    # reduce-only, so a duplicate close is harmless by construction, whereas a
    # false "closed" is not.
    _errored = not isinstance(result, dict) or bool(result.get("error"))
    _fill_reported = isinstance(result, dict) and "filled_size" in result
    filled = _num(result.get("filled_size")) if _fill_reported else None
    # The venue-quantized size the adapter actually asked for; anything it could
    # not accept is dust below one size step, not a leg we can retry.
    _requested = _num((result or {}).get("requested_size")) if isinstance(result, dict) else None
    _fillable = _requested if (_requested is not None and _requested <= ask_qty) else ask_qty
    _tolerance = max(_fillable * _CLOSE_FILL_TOLERANCE_FRAC, 1e-9)
    # Only a CLEAN payload may shrink the mirrored quantity: on an errored one the
    # reported fill is not trustworthy, and a reduce-only close asking for more
    # than is left is harmless (the venue caps it) while asking for too little
    # strands the remainder. A CLAMPED close measures the fill against the
    # LEDGER quantity, not the order: even a fully-filled clamped order leaves
    # a residual claim, which must stay open and bracketed (the partial branch)
    # rather than be retired on a guess about who consumed the deficit — and a
    # clamped close with no fill confirmation retries instead of assuming.
    _close_target = _fillable if clamped_from is None else quantity
    _short_fill = not _errored and filled is not None and (filled + _tolerance) < _close_target
    if (
        not _errored and not _short_fill
        and (filled is not None or not _fill_reported)
        and clamped_from is None
    ):
        entry.update({
            "status": "closed",
            "reason": None,
            "exit_price": result.get("exit_price"),
            "closed_quantity": filled,
            "closed_at": now.isoformat(),
        })
        _cancel_bracket_legs(propr, asset, entry, trade_id=trade_id)
        log.info("Propr mirror CLOSE %s %s for trade %s", asset, direction, trade_id)
    else:
        attempts = int(entry.get("close_attempts") or 0) + 1
        entry["close_attempts"] = attempts
        if _short_fill:
            residual = round(max(quantity - float(filled or 0.0), 0.0), 10)
            # Shrink to what is REALLY still on the venue so the next reduce-only
            # close asks for the residual, and record the partial for the panel.
            entry["quantity"] = residual
            entry["status"] = "open"
            entry["partial_close_filled"] = float(entry.get("partial_close_filled") or 0.0) + float(filled or 0.0)
            entry["partial_close_at"] = now.isoformat()
            if result.get("exit_price") is not None:
                entry["exit_price"] = result.get("exit_price")
            entry["reason"] = (
                f"partial close ({float(filled or 0.0):.10g}/{_fillable:.10g}) — residual "
                f"{residual:.10g} still open (retrying reduce-only)"
            )
            log.warning(
                "Propr mirror PARTIAL close %s %s for trade %s: filled %s of %s, residual %s "
                "kept open", asset, direction, trade_id, filled, _fillable, residual,
            )
        elif isinstance(result, dict) and not result.get("error"):
            entry["reason"] = (
                "close order accepted but no fill confirmed — position assumed still "
                "open (retrying reduce-only)"
            )
        else:
            entry["reason"] = str((result or {}).get("error") or "close rejected")
        if attempts >= MAX_CLOSE_ATTEMPTS:
            entry["status"] = "close_failed"
            log.error("Propr mirror: close for trade %s FAILED after %d attempts: %s",
                      trade_id, attempts, entry["reason"])
            _notify_mirror_failure(trade_id, entry, "close")
        # RACE-1: on the multi-leg path the brackets were cancelled ahead of
        # the close — any outcome that leaves quantity on the venue (partial
        # residual, unconfirmed fill, rejected close) is unprotected until the
        # retry lands. Re-arm the stop for whatever claim remains.
        if others and float(_num(entry.get("quantity")) or 0.0) > 0:
            _rearm_deferred_leg(propr, trade_id, entry, asset, direction)


# Order-row readers, local to the mirror because forven/exchange/ is
# off-limits without explicit instruction (AGENTS.md "Do NOT"). The key lists
# mirror forven.exchange.propr's private _order_* readers — keep them in sync.
def _order_row_id(order: dict) -> str | None:
    value = order.get("orderId") or order.get("order_id") or order.get("id")
    return str(value) if value is not None else None


def _order_row_status(order: dict) -> str:
    return str(order.get("status") or "").strip().lower()


def _order_row_fill_price(order: dict) -> float | None:
    for key in ("averageFillPrice", "average_fill_price", "avgFillPrice", "fillPrice"):
        value = _num(order.get(key))
        if value and value > 0:
            return float(value)
    return None


def _order_row_filled_size(order: dict) -> float | None:
    for key in ("cumulativeQuantity", "cumulative_quantity", "filledQuantity", "executedQuantity"):
        value = _num(order.get(key))
        if value is not None:
            return float(value)
    return None


def _order_row_trigger_price(order: dict) -> float | None:
    for key in ("triggerPrice", "trigger_price", "stopPrice", "stop_price", "price"):
        value = _num(order.get(key))
        if value and value > 0:
            return float(value)
    return None


def _read_order_rows(propr) -> dict[str, dict] | None:
    """order id -> row for every order the venue lists, or None when the
    listing is unreadable (callers must treat that as unverifiable, never as
    empty)."""
    try:
        rows: dict[str, dict] = {}
        for order in propr.list_orders() or []:
            oid = _order_row_id(order)
            if oid:
                rows[oid] = order
        return rows
    except Exception as exc:
        log.debug("Propr mirror: order read failed: %s", exc)
        return None


def _resize_sibling_bracket(propr, trade_id: str, entry: dict, filled_leg_key: str,
                            orders: dict, remaining: float) -> None:
    """PROPR-LEG-3: after a partial bracket fill shrinks a leg's claim, the
    SURVIVING bracket still rests at the original size — on a netted book its
    trigger would reduce the residual plus a sibling leg's share. Cancel it and
    re-place at the residual: a stop that cannot be re-placed raises the
    critical unprotected notification; a take-profit that cannot is left off
    (the stop is the protection, the TP is opportunistic)."""
    asset = propr.normalize_asset(str(entry.get("asset") or ""))
    direction = str(entry.get("direction") or "").strip().lower()
    sibling_key = (
        "take_profit_order_id" if filled_leg_key == "stop_order_id" else "stop_order_id"
    )
    sibling_oid = entry.get(sibling_key)
    if not sibling_oid:
        # A previous stop resize may already have confirmed cancellation and
        # failed only while placing the replacement. Retry that placement
        # without another cancel/order id.
        if sibling_key == "stop_order_id" and entry.get("bracket_resize_pending"):
            _rearm_deferred_leg(propr, trade_id, entry, asset, direction)
            if entry.get("stop_unarmed"):
                entry["bracket_resize_error"] = (
                    "protective stop replacement is still unavailable after resize"
                )
            else:
                entry.pop("bracket_resize_pending", None)
                entry.pop("bracket_resize_error", None)
                entry["reason"] = "protective stop resize recovered"
        else:
            entry.pop("bracket_resize_pending", None)
        return
    sibling_row = orders.get(str(sibling_oid))
    trigger = _order_row_trigger_price(sibling_row) if sibling_row else None
    cancelled = None
    try:
        cancelled = propr.cancel_order(asset, sibling_oid)
    except Exception as exc:
        cancelled = {"error": str(exc)}
    if not isinstance(cancelled, dict):
        cancelled = {"error": "venue returned no cancellation result"}
    if cancelled.get("already_filled_or_cancelled"):
        # A 400 deliberately conflates already-filled with already-cancelled.
        # Re-read once: only an explicit terminal cancellation permits a
        # replacement; filled/partial/absent remains for the attribution pass.
        refreshed = _read_order_rows(propr)
        refreshed_row = refreshed.get(str(sibling_oid)) if refreshed is not None else None
        refreshed_status = _order_row_status(refreshed_row) if refreshed_row else ""
        if refreshed_status in ("cancelled", "canceled", "rejected", "expired"):
            sibling_row = refreshed_row
            trigger = _order_row_trigger_price(sibling_row) or trigger
            cancelled = {"cancelled": True, "order_id": str(sibling_oid)}
        else:
            cancelled = {
                "error": (
                    "cancel response was already-filled-or-cancelled but the fresh order "
                    f"state was {refreshed_status or 'absent'}"
                )
            }
    if cancelled.get("error"):
        message = f"could not verify cancellation of sibling bracket {sibling_oid}: {cancelled['error']}"
        entry["bracket_resize_error"] = message
        entry["bracket_resize_pending"] = {
            "filled_leg_key": filled_leg_key,
            "remaining": float(remaining),
        }
        entry["reason"] = message
        log.critical("Propr mirror: %s (trade %s)", message, trade_id)
        _notify_mirror_failure(trade_id, entry, "bracket resize")
        return
    entry.pop("bracket_resize_error", None)
    entry[sibling_key] = None
    if sibling_key == "stop_order_id":
        if trigger and not _num(entry.get("stop_price")):
            entry["stop_price"] = float(trigger)
        _rearm_deferred_leg(propr, trade_id, entry, asset, direction)
        if entry.get("stop_unarmed"):
            entry["bracket_resize_pending"] = {
                "filled_leg_key": filled_leg_key,
                "remaining": float(remaining),
            }
            entry["bracket_resize_error"] = (
                "protective stop cancellation succeeded but its residual replacement failed"
            )
        else:
            entry.pop("bracket_resize_pending", None)
        return
    entry.pop("bracket_resize_pending", None)
    if trigger and remaining > 0:
        placed = None
        try:
            placed = propr.place_take_profit(asset, direction, float(remaining), float(trigger))
        except Exception as exc:
            placed = {"error": str(exc)}
        if isinstance(placed, dict) and not placed.get("error") and placed.get("take_profit_order_id"):
            entry["take_profit_order_id"] = str(placed["take_profit_order_id"])
            log.warning(
                "Propr mirror: resized the take-profit for trade %s to the residual %.10g",
                trade_id, remaining,
            )
            return
    log.warning(
        "Propr mirror: take-profit for trade %s cancelled after a partial stop fill and "
        "not re-placed (trigger %s) — the stop still protects the residual",
        trade_id, trigger,
    )


def _retire_bracket_filled_legs(propr, state: dict, now: datetime, summary: dict) -> None:
    """PROPR-LEG-2 (Codex P1 on #113): per-LEG stop/take-profit fill attribution.

    Propr nets one position per asset, so when two mirrored legs share the
    SAME side a venue stop fill on leg A leaves the aggregate position
    present — the (asset, side)-keyed venue_missing check can never see it,
    A keeps reporting open, and A's eventual mirror close would reduce what
    is by then B's share of the netted position. The leg's OWN bracket
    orders are its venue identity: a stop/TP order reported ``filled`` means
    the venue consumed exactly this leg's share — retire it as closed at the
    recorded fill price and cancel the surviving sibling leg.

    ``filled`` retires the leg outright; ``partially_filled`` shrinks the
    leg's ledger claim by the newly-filled delta (cumulative-tracked) while
    keeping it open and bracketed, so sibling closes clamp against fresh
    claims. An unreadable order book skips the pass entirely (attribution can
    wait a tick; a wrong retire cannot be waited back)."""
    # close_failed included: an unconfirmed close means the leg's venue share
    # and brackets may still be live — a later bracket fill must still retire
    # it (and drop its claim) exactly like an open leg's.
    open_legs = [
        (tid, e) for tid, e in state.items()
        if isinstance(e, dict) and e.get("status") in _UNRESOLVED_CLAIM_STATUSES
        and (e.get("stop_order_id") or e.get("take_profit_order_id"))
    ]
    if not open_legs:
        return
    orders = _read_order_rows(propr)
    if orders is None:
        log.debug("Propr mirror: order read failed — skipping leg attribution")
        return
    for trade_id, entry in open_legs:
        resize_pending = entry.get("bracket_resize_pending")
        if isinstance(resize_pending, dict):
            filled_leg_key = str(resize_pending.get("filled_leg_key") or "")
            if filled_leg_key in ("stop_order_id", "take_profit_order_id"):
                _resize_sibling_bracket(
                    propr,
                    trade_id,
                    entry,
                    filled_leg_key,
                    orders,
                    float(_num(entry.get("quantity")) or 0.0),
                )
        for leg_key, label in (("stop_order_id", "stop"), ("take_profit_order_id", "take-profit")):
            oid = entry.get(leg_key)
            order = orders.get(str(oid)) if oid else None
            if order is None:
                continue
            status = _order_row_status(order)
            if status == "partially_filled":
                # Book the partially-filled bracket against THIS leg's claim so
                # sibling closes clamp against fresh numbers (Codex P1 on #116).
                # Cumulative-tracked PER BRACKET (stop and TP can both partially
                # fill): the venue reports total filled, we shrink by the
                # newly-filled delta only.
                filled_so_far = float(_order_row_filled_size(order) or 0.0)
                accounted_key = f"bracket_partial_accounted_{leg_key}"
                accounted = float(_num(entry.get(accounted_key)) or 0.0)
                delta = filled_so_far - accounted
                if delta > 1e-12:
                    remaining = max(0.0, float(_num(entry.get("quantity")) or 0.0) - delta)
                    entry[accounted_key] = filled_so_far
                    if remaining <= 1e-12:
                        # The bracket consumed the whole claim piecemeal. Retire
                        # NOW, with the sibling bracket cancelled — a qty-0 leg
                        # left "open" would be closed by the zero-quantity close
                        # branch, stranding a live reduce-only sibling on the
                        # venue that can fire into another leg's share.
                        entry.update({
                            "status": "closed",
                            "reason": (
                                f"{label} order {oid} filled the leg's whole claim in "
                                "partial fills — retired venue-side"
                            ),
                            "exit_price": _order_row_fill_price(order),
                            "closed_quantity": filled_so_far,
                            "closed_at": now.isoformat(),
                            "bracket_filled": label,
                        })
                        summary["bracket_filled"] = summary.get("bracket_filled", 0) + 1
                        log.warning(
                            "Propr mirror: %s for trade %s consumed the whole claim in "
                            "partial fills — retiring the leg and cancelling its sibling",
                            label, trade_id,
                        )
                        _cancel_bracket_legs(
                            propr, propr.normalize_asset(str(entry.get("asset") or "")), entry,
                            trade_id=trade_id,
                        )
                        break
                    entry["quantity"] = round(remaining, 10)
                    entry["reason"] = (
                        f"{label} order {oid} partially filled venue-side "
                        f"({filled_so_far:.10g} total) — leg claim reduced to "
                        f"{remaining:.10g}"
                    )
                    log.warning(
                        "Propr mirror: %s for trade %s partially filled venue-side "
                        "(%.10g total) — shrinking the leg's claim to %.10g",
                        label, trade_id, filled_so_far, remaining,
                    )
                    # PROPR-LEG-3: the surviving bracket still rests at the
                    # ORIGINAL size — resize it to the residual so its trigger
                    # cannot reduce a sibling's share of the netted position.
                    _resize_sibling_bracket(propr, trade_id, entry, leg_key, orders, remaining)
                continue
            if status != "filled":
                continue
            entry.update({
                "status": "closed",
                "reason": (
                    f"{label} order {oid} filled venue-side — the venue consumed this "
                    "leg's share of the netted position"
                ),
                "exit_price": _order_row_fill_price(order),
                "closed_quantity": _order_row_filled_size(order) or entry.get("quantity"),
                "closed_at": now.isoformat(),
                "bracket_filled": label,
            })
            summary["bracket_filled"] = summary.get("bracket_filled", 0) + 1
            log.warning(
                "Propr mirror: %s for trade %s filled venue-side — retiring the leg "
                "and cancelling its sibling bracket order", label, trade_id,
            )
            _cancel_bracket_legs(
                propr, propr.normalize_asset(str(entry.get("asset") or "")), entry,
                trade_id=trade_id,
            )
            break


def _retire_venue_missing_legs(propr, state: dict, venue_keys: set, now: datetime, summary: dict) -> None:
    """PROPR-LEDGER-2: retire tracked-open legs whose venue position is GONE.

    The reverse of the unmanaged report. Propr nets one position per asset, so
    a leg this mirror tracks as open can be consumed venue-side (its stop
    fills, or an opposite-side fill nets it away — observed 2026-07-31, where
    a netted-away leg kept reporting "open" for days). Leaving it "open" keeps
    phantom exposure on the page and burns MAX_CLOSE_ATTEMPTS rejected closes
    when the source finally exits. A leg absent for _VENUE_MISSING_TICKS
    consecutive successful reads is retired as ``venue_missing`` and notified
    once; the hysteresis exists so one partial positions response cannot
    retire a real leg, and a wrongly retired leg still surfaces through the
    unmanaged report, so either failure mode stays visible."""
    for trade_id, entry in state.items():
        # close_failed included so an unresolved close's claim can be proven
        # flat by venue state and released (it counts in the close clamp's
        # sibling claims until then).
        if not isinstance(entry, dict) or entry.get("status") not in _UNRESOLVED_CLAIM_STATUSES:
            continue
        key = _position_key(propr, entry.get("asset"), entry.get("direction"))
        if not key[0] or key in venue_keys:
            entry.pop("venue_missing_ticks", None)
            continue
        misses = int(entry.get("venue_missing_ticks") or 0) + 1
        entry["venue_missing_ticks"] = misses
        if misses < _VENUE_MISSING_TICKS:
            continue
        entry.update({
            "status": "venue_missing",
            "reason": (
                f"no {key[1]} {key[0]} position on the venue for {misses} consecutive "
                "reads — the leg was closed venue-side (stop fill or netted away); "
                "nothing is left for this mirror to manage"
            ),
            "recorded_at": now.isoformat(),
        })
        summary["venue_missing"] = summary.get("venue_missing", 0) + 1
        log.warning(
            "Propr mirror: tracked leg for trade %s (%s %s) has no venue position — "
            "retiring it as venue_missing", trade_id, key[0], key[1],
        )
        _cancel_bracket_legs(propr, key[0], entry, trade_id=trade_id)
        try:
            from forven.notifications import emit_notification
            emit_notification(
                "propr_mirror_venue_missing",
                severity="warning",
                source="propr_mirror",
                title=f"Propr mirrored leg vanished venue-side ({key[0]})",
                summary=(
                    f"{entry.get('strategy')} {key[0]} {key[1]} (trade {trade_id}): the venue "
                    "no longer holds this position — retired from the mirror ledger as "
                    "venue_missing (stop fill or netted away)."
                ),
                body=str(entry.get("reason") or ""),
                dedupe_key=f"propr_mirror_venue_missing:{trade_id}",
            )
        except Exception as exc:
            log.debug("Could not emit propr mirror venue-missing notification: %s", exc)


def _reconcile_unmanaged_positions(propr, state: dict, now: datetime, summary: dict) -> None:
    """PROPR-LEDGER-1: report venue positions this mirror is not tracking.

    KV state is the ONLY record of a mirrored position — a dropped write, a
    wiped key or a crash between the fill and the save leaves a REAL leveraged
    position on the challenge account that nothing here will ever close. Read
    the venue every tick and flag any (asset, side) with no open state entry.

    Deliberately REPORT-only: the challenge account is the operator's, and a
    reduce-only close fired at an unrecognized position would be this module
    placing a real order it never sized against a position it does not
    understand (a hand-placed hedge, a leftover from a previous roster). The
    orphan is surfaced in kv ``forven:propr-mirror:unmanaged`` and notified
    once, so the operator can adopt or flatten it deliberately. A read failure
    is never fatal — the mirror keeps working.

    The same read feeds the reverse check, PROPR-LEDGER-2 (see
    ``_retire_venue_missing_legs``): tracked legs with no venue position left
    are retired instead of tracked forever."""
    try:
        positions = propr.raw_positions() or []
    except Exception as exc:
        log.debug("Propr mirror: venue position read failed: %s", exc)
        return

    venue_keys = _venue_position_keys(propr, positions)
    _retire_venue_missing_legs(propr, state, venue_keys, now, summary)

    tracked: set[tuple[str, str]] = set()
    for entry in state.values():
        if entry.get("status") not in _UNRESOLVED_CLAIM_STATUSES:
            continue
        key = _position_key(propr, entry.get("asset"), entry.get("direction"))
        if key[0]:
            tracked.add(key)

    unmanaged: dict = {}
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        asset = propr.normalize_asset(str(pos.get("asset") or pos.get("coin") or ""))
        side = propr.position_side(pos)
        if not asset or (asset, side) in tracked:
            continue
        unmanaged[f"{asset}:{side}"] = {
            "asset": asset,
            "direction": side,
            "quantity": pos.get("quantity") or pos.get("size"),
            "position_id": pos.get("positionId") or pos.get("id"),
            "seen_at": now.isoformat(),
        }

    try:
        kv_set(UNMANAGED_STATE_KEY, unmanaged)
    except Exception as exc:
        log.debug("Propr mirror: could not persist the unmanaged-position report: %s", exc)
    if not unmanaged:
        return
    summary["unmanaged"] = sorted(unmanaged)
    log.warning(
        "Propr mirror: %d venue position(s) not tracked by mirror state (%s) — this "
        "mirror will never close them; adopt or flatten them on the venue",
        len(unmanaged), ", ".join(sorted(unmanaged)),
    )
    try:
        from forven.notifications import emit_notification
        emit_notification(
            "propr_mirror_unmanaged_position",
            severity="warning",
            source="propr_mirror",
            title="Propr position not tracked by the mirror",
            summary=(
                f"{', '.join(sorted(unmanaged))} open on the challenge account with no "
                "mirror state entry — the mirror will not close it."
            ),
            body=json.dumps(unmanaged, default=str),
            dedupe_key=f"propr_mirror_unmanaged:{','.join(sorted(unmanaged))}",
        )
    except Exception as exc:
        log.debug("Could not emit propr mirror unmanaged notification: %s", exc)


def mirror_tick() -> dict:
    """One observer pass. Cheap no-op unless the flag, the toggle, and a
    non-empty roster all hold. Never raises — the job wrapper logs the summary."""
    if not propr_enabled():
        return {"skipped": "propr disabled"}
    settings = _settings()
    if not mirror_enabled(settings):
        return {"skipped": "mirror disabled"}
    roster = mirror_roster(settings)
    if not roster:
        return {"skipped": "empty roster"}
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        return {"skipped": "sim active"}

    from forven.exchange import propr

    now = datetime.now(timezone.utc)
    state = get_state()
    summary = {"opened": 0, "closed": 0, "errors": 0, "skipped": 0}
    roster_ids = set(roster)

    # --- venue reconcile: state is bookkeeping, the venue is the truth -------
    _reconcile_unmanaged_positions(propr, state, now, summary)
    # A terminal leg can still have a live reduce-only bracket if cancellation
    # failed. Retry those first and block new opens on that asset until cleanup
    # is confirmed.
    _retry_pending_bracket_cancellations(propr, state, summary)
    # PROPR-LEG-2: attribute venue-side stop/TP fills to their exact leg BEFORE
    # the close pass, so a consumed leg is retired instead of closing into a
    # same-side sibling's share of the netted position.
    _retire_bracket_filled_legs(propr, state, now, summary)

    # --- close pass first: reducing risk always outranks adding it ----------
    for trade_id, entry in list(state.items()):
        if entry.get("status") not in ("open",):
            continue
        source_status = _trade_status(trade_id)
        if source_status == "OPEN":
            continue
        try:
            _mirror_close(propr, trade_id, entry, now, state)
            summary["closed" if entry.get("status") == "closed" else "errors"] += 1
        except Exception as exc:
            summary["errors"] += 1
            entry["reason"] = str(exc)
            log.warning("Propr mirror close for %s raised: %s", trade_id, exc)

    # --- account read + PROPR-3 halt evaluation ------------------------------
    # Every active tick (not just when there's something to open) so the
    # day-start equity anchor is captured early and the page's halt readout
    # stays current.
    equity = None
    attempt_payload: dict = {}
    try:
        account = propr.get_account_value() or {}
        equity = _num(account.get("accountValue"))
        attempt_payload = account.get("attempt") if isinstance(account.get("attempt"), dict) else {}
    except Exception as exc:
        log.debug("Propr mirror: account read failed: %s", exc)
    halt = _evaluate_halt(attempt_payload, equity, now) if equity else None

    # --- open pass -----------------------------------------------------------
    try:
        open_rows = _roster_trades(roster_ids)
    except Exception as exc:
        _save_state(state)
        return {**summary, "error": f"trades query failed: {exc}"}

    # RETRY-1: the signal snapshot is only needed when some entry is (or was)
    # in the signal-gated retry path — don't read it on every quiet tick.
    _retry_candidates = any(
        isinstance(e, dict) and (e.get("status") == "failed" or e.get("retry_signal_gated"))
        for e in state.values()
    )
    signal_snapshot = _entry_signal_snapshot(now) if _retry_candidates else None

    to_open = []
    for row in open_rows:
        trade_id = str(row["id"])
        existing = state.get(trade_id)
        signal_gated = bool(existing and existing.get("retry_signal_gated"))
        if existing and existing.get("status") not in ("pending", "error"):
            if existing.get("status") != "failed":
                continue
            # RETRY-1 re-arm: the open failed terminally, but the source trade
            # is still OPEN and the strategy is still emitting this entry —
            # it is still asking for this exact trade, so retry a full round.
            rearmed_at = _parse_when(existing.get("retry_rearmed_at"))
            if rearmed_at and (now - rearmed_at) < timedelta(
                minutes=RETRY_REARM_COOLDOWN_MINUTES
            ):
                continue
            if not _entry_signal_active(row, signal_snapshot):
                continue
            existing.update({
                "status": "pending",
                "attempts": 0,
                "retry_signal_gated": True,
                "retry_rearmed_at": now.isoformat(),
                "reason": "re-armed after terminal failure: entry signal still active",
            })
            summary["rearmed"] = summary.get("rearmed", 0) + 1
            signal_gated = True
        elif signal_gated and not _entry_signal_active(row, signal_snapshot):
            # Mid-round: the signal that justified the late retry is gone —
            # entering NOW would be a trade the strategy is no longer asking
            # for. Stop chasing.
            existing.update({
                "status": "failed",
                "reason": "retry abandoned: entry signal no longer active",
            })
            continue
        sid = _trade_roster_key(row, roster_ids)
        added_at = _parse_when(roster.get(sid or ""))
        opened_at = _parse_when(row.get("opened_at"))
        if added_at and opened_at and opened_at < added_at:
            state[trade_id] = {"status": "skipped", "reason": "opened before strategy joined the roster",
                               "asset": row.get("asset"), "strategy": sid}
            summary["skipped"] += 1
            continue
        # A signal-gated retry is exempt from the freshness window: the ACTIVE
        # entry signal (checked above, every tick) is the stronger form of the
        # same "is this still the trade the strategy took?" question.
        if not signal_gated and opened_at and (now - opened_at) > timedelta(minutes=OPEN_FRESHNESS_MINUTES):
            state[trade_id] = {"status": "skipped", "reason": "entry older than the freshness window",
                               "asset": row.get("asset"), "strategy": sid}
            summary["skipped"] += 1
            continue
        to_open.append(row)

    # PROPR-GLOBAL-HALT-1: the operator's single "stop everything" control has to
    # mean everything. The kill-switch / daily-loss halt / operator STOP used to
    # be invisible here, so the mirror kept placing venue orders every 60s
    # through a system-wide halt. OPEN pass only — the close pass above runs
    # unconditionally (reducing risk is never halted), exactly as
    # basket_live.reconcile_basket_live does it. Trades are not stamped in state,
    # same rationale as the PROPR-3 halt below.
    _global_ok, _global_why = (True, "OK")
    if to_open:
        from forven.exchange.risk import is_trading_allowed
        _global_ok, _global_why = is_trading_allowed()

    if to_open and not _global_ok:
        summary["halted"] = [f"trading halted: {_global_why}"]
    elif to_open and halt and halt.get("halted"):
        # PROPR-3: opens blocked near the challenge limits. Trades are NOT
        # marked in state — if the halt clears inside their freshness window
        # they mirror normally; past it they expire to a stale-skip, which is
        # correct (a delayed entry is a different trade than the strategy took).
        summary["halted"] = halt.get("reasons")
    elif to_open:
        if not equity:
            for row in to_open:
                entry = state.setdefault(str(row["id"]), {"attempts": 0})
                entry.update({"status": "error", "reason": "challenge equity unavailable — fail closed"})
            summary["errors"] += len(to_open)
        else:
            # Remaining daily risk room = the halt line, minus realized daily
            # loss, minus loss-at-stop of every mirrored position still open —
            # so concurrent stop-outs can't stack past the venue's daily cap.
            risk_budget = None
            if halt is not None:
                open_risk = sum(
                    _num(e.get("risk_usd")) or 0.0
                    for e in state.values()
                    if e.get("status") in _UNRESOLVED_CLAIM_STATUSES
                )
                risk_budget = {
                    "remaining": max(
                        0.0,
                        float(halt["daily_halt_at_usd"]) - float(halt["daily_loss"]) - open_risk,
                    )
                }
            for row in to_open[:MAX_OPENS_PER_TICK]:
                try:
                    _mirror_open(propr, row, state, equity, now, risk_budget=risk_budget)
                    status = state.get(str(row["id"]), {}).get("status")
                    if status == "open":
                        summary["opened"] += 1
                    elif status == "skipped":
                        summary["skipped"] += 1
                    elif status == "pending":
                        summary["deferred"] = summary.get("deferred", 0) + 1
                    else:
                        summary["errors"] += 1
                except Exception as exc:
                    summary["errors"] += 1
                    state.setdefault(str(row["id"]), {})["reason"] = str(exc)
                    log.warning("Propr mirror open for %s raised: %s", row["id"], exc)

    # --- prune aged terminal entries ----------------------------------------
    cutoff = now - timedelta(days=_STATE_RETENTION_DAYS)
    for trade_id, entry in list(state.items()):
        if (
            entry.get("status") in ("closed", "skipped", "failed", "venue_missing")
            and not entry.get("bracket_cancel_pending")
        ):
            # Terminal records without their own timestamp (skips/failures) are
            # stamped now so they show on the page for the retention window
            # instead of being pruned in the same tick that wrote them.
            entry.setdefault("recorded_at", now.isoformat())
            stamp = _parse_when(
                entry.get("closed_at") or entry.get("recorded_at") or entry.get("opened_at")
            )
            if stamp is None or stamp < cutoff:
                state.pop(trade_id, None)

    _save_state(state)
    return summary
