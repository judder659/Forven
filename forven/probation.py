"""Live probation: the database side (2026-10 probation plan).

The rules are in ``forven.probation_rules``; this module reads and writes the
record beside a strategy (table ``strategy_probation``), runs the hourly
watchdog, and gives every automatic move a manual twin:

* **Start probation** (paper -> live_graduated at probation size). Automatic
  only when the operator turned automatic probation on (typed confirmation);
  otherwise eligible strategies are listed as ready. A manual start past a
  failing check needs the typed ``PROBATION`` confirmation.
* **Size steps** up the ladder as live trades close (automatic with automatic
  probation, or by hand with Change size; a hand-set size holds until changed).
* **Graduate to full live** once the live record supports it (automatic only
  when the operator turned it on; by hand with the typed ``GO LIVE``).
* **Demote to paper** when live results leave the backtest's range
  (automatic unless switched off, or by hand). An open live position keeps its
  exchange stop and runs to its own exit first: only live strategies manage
  live positions, so the stage change waits for it while no new entries are taken.
* **Pause entries**: stays in probation, takes no new trades.

The scanner reads ``order_scale`` for every live order: probation strategies
trade at their size, everything else at 1.0.

First-party modules are imported by name (importlib) so the scanner can import
this module without pulling it into the import cycle
(tests/test_finish_db_layering.py).
"""

from __future__ import annotations

import importlib
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from forven import probation_rules as rules

log = logging.getLogger("forven.probation")

AUTOMATION_KV_KEY = "forven:probation:automation"
READY_KV_KEY = "forven:probation:ready"
SKIPS_KV_PREFIX = "forven:probation:skips:"
ACTOR = "probation"
_MAX_SKIPS_KEPT = 20


def _mod(name: str):
    return importlib.import_module(name)


def _db():
    return _mod("forven.db")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json(value: object, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value) if value else default
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


class ProbationError(ValueError):
    """A probation action the caller asked for that cannot happen; ``checks`` says why."""

    def __init__(self, message: str, *, checks: list[dict] | None = None, needs_confirm: str | None = None):
        super().__init__(message)
        self.checks = checks or []
        self.needs_confirm = needs_confirm


# --- settings ------------------------------------------------------------------


def config() -> dict:
    return _mod("forven.policy").load_pipeline_config()


def knobs(cfg: dict | None = None) -> dict[str, Any]:
    return rules.settings(cfg if cfg is not None else config())


def get_automation() -> dict[str, Any]:
    raw = _db().kv_get(AUTOMATION_KV_KEY, None)
    out: dict[str, Any] = dict(rules.automation(raw))
    if isinstance(raw, dict):
        out["changed_by"] = raw.get("changed_by")
        out["changed_at"] = raw.get("changed_at")
    return out


def set_automation(changes: dict, *, actor: str, confirm: str | None = None) -> dict[str, Any]:
    """Change what Forven does on its own.

    Turning automatic entry or graduation ON puts real money to work without a
    per-strategy click, and turning automatic demotion OFF removes a safety net:
    each needs the typed ``PROBATION`` confirmation.
    """
    current = rules.automation(_db().kv_get(AUTOMATION_KV_KEY, None))
    wanted = dict(current)
    for key in rules.AUTOMATION_DEFAULTS:
        if key in changes and changes[key] is not None:
            wanted[key] = bool(changes[key])
    riskier = (
        (wanted["auto_entry"] and not current["auto_entry"])
        or (wanted["auto_graduate"] and not current["auto_graduate"])
        or (not wanted["auto_demote"] and current["auto_demote"])
    )
    if riskier and str(confirm or "").strip().upper() != rules.CONFIRM_PHRASE:
        raise ProbationError(
            f'Type {rules.CONFIRM_PHRASE} to confirm: this lets Forven move real money without asking each time',
            needs_confirm=rules.CONFIRM_PHRASE,
        )
    stored = {**wanted, "changed_by": actor, "changed_at": _iso()}
    _db().kv_set(AUTOMATION_KV_KEY, stored)
    _db().log_activity(
        "warning" if riskier else "info",
        "probation",
        f"Probation automation changed by {actor}: "
        + ", ".join(f"{k}={'on' if v else 'off'}" for k, v in wanted.items()),
        {"automation": wanted, "actor": actor},
    )
    return get_automation()


# --- the record ----------------------------------------------------------------


def _row_to_record(row) -> dict[str, Any] | None:
    if row is None:
        return None
    rec = dict(row)
    rec["size_locked"] = bool(rec.get("size_locked"))
    rec["bypassed"] = _json(rec.pop("bypassed_json", None), [])
    rec["history"] = _json(rec.pop("history_json", None), [])
    rec["last_check"] = _json(rec.pop("last_check_json", None), {})
    rec["in_probation"] = rec.get("state") in rules.IN_PROBATION
    return rec


def get_record(strategy_id: str) -> dict[str, Any] | None:
    with _db().get_db() as conn:
        row = conn.execute("SELECT * FROM strategy_probation WHERE strategy_id = ?", (strategy_id,)).fetchone()
    return _row_to_record(row)


def list_records(states: frozenset[str] | set[str] | None = None) -> list[dict[str, Any]]:
    with _db().get_db() as conn:
        rows = conn.execute("SELECT * FROM strategy_probation ORDER BY entered_at").fetchall()
    out = [_row_to_record(r) for r in rows]
    return [r for r in out if r and (states is None or r["state"] in states)]


def _save(strategy_id: str, **fields) -> None:
    """Insert or update the record; ``history_event`` appends to its history."""
    event = fields.pop("history_event", None)
    with _db().get_db() as conn:
        row = conn.execute("SELECT * FROM strategy_probation WHERE strategy_id = ?", (strategy_id,)).fetchone()
        current = _row_to_record(row) or {
            "state": rules.STATE_ACTIVE, "size_pct": 100.0, "size_locked": False, "entered_at": None,
            "entered_by": None, "bypassed": [], "history": [], "demotion_count": 0, "last_demoted_at": None,
            "status_reason": None, "last_check": {},
        }
        current.update({k: v for k, v in fields.items()})
        history = list(current.get("history") or [])
        if event:
            history.append({"at": _iso(), **event})
            history = history[-100:]
        conn.execute(
            "INSERT INTO strategy_probation (strategy_id, state, size_pct, size_locked, entered_at, entered_by, "
            "bypassed_json, history_json, demotion_count, last_demoted_at, status_reason, last_check_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(strategy_id) DO UPDATE SET state=excluded.state, size_pct=excluded.size_pct, "
            "size_locked=excluded.size_locked, entered_at=excluded.entered_at, entered_by=excluded.entered_by, "
            "bypassed_json=excluded.bypassed_json, history_json=excluded.history_json, "
            "demotion_count=excluded.demotion_count, last_demoted_at=excluded.last_demoted_at, "
            "status_reason=excluded.status_reason, last_check_json=excluded.last_check_json, "
            "updated_at=excluded.updated_at",
            (
                strategy_id,
                current["state"],
                float(current["size_pct"]),
                1 if current.get("size_locked") else 0,
                current.get("entered_at"),
                current.get("entered_by"),
                json.dumps(current.get("bypassed") or []),
                json.dumps(history),
                int(current.get("demotion_count") or 0),
                current.get("last_demoted_at"),
                current.get("status_reason"),
                json.dumps(current.get("last_check") or {}, default=str),
                _iso(),
            ),
        )


# --- what the scanner asks ------------------------------------------------------


def order_scale(strategy_id: str) -> tuple[float, dict[str, Any] | None]:
    """(size multiplier, probation meta) for a live order of ``strategy_id``.

    1.0 and None for a strategy not in probation. On a read failure the start
    size is returned: shrinking a full-live order by mistake costs less than
    sending a probation strategy's order at full size.
    """
    try:
        rec = get_record(strategy_id)
    except Exception as exc:  # noqa: BLE001 — fail towards the smaller order
        log.warning("Probation record unreadable for %s (%s); sizing at the start size", strategy_id, exc)
        start = float(rules.DEFAULTS["start_size_pct"])
        return start / 100.0, {"size_pct": start, "state": "unknown"}
    if not rec or not rec["in_probation"]:
        return 1.0, None
    size = max(0.0, min(100.0, float(rec["size_pct"] or 0.0)))
    return size / 100.0, {"size_pct": size, "state": rec["state"]}


def blocks_new_entries(strategy_id: str) -> str | None:
    """Why a probation strategy takes no new entries now (paused or being demoted), else None."""
    try:
        rec = get_record(strategy_id)
    except Exception:  # noqa: BLE001 — sizing still applies; a read error is not a pause
        return None
    if rec and rec["state"] in rules.NO_NEW_ENTRIES:
        if rec["state"] == rules.STATE_PAUSED:
            return "probation entries paused"
        return "probation demotion pending (no new entries)"
    return None


def record_skip(strategy_id: str, asset: str, reason: str) -> None:
    """Remember an entry the probation size could not place (shown on the strategy)."""
    try:
        key = f"{SKIPS_KV_PREFIX}{strategy_id}"
        skips = _db().kv_get(key, []) or []
        skips = (skips if isinstance(skips, list) else []) + [{"at": _iso(), "asset": asset, "reason": reason}]
        _db().kv_set(key, skips[-_MAX_SKIPS_KEPT:])
    except Exception as exc:  # noqa: BLE001 — a lost note never blocks trading
        log.debug("Could not record probation skip for %s: %s", strategy_id, exc)


# --- evidence -------------------------------------------------------------------


def _strategy_row(strategy_id: str):
    with _db().get_db() as conn:
        return conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()


def _stage(row) -> str:
    return str((row["stage"] if row else None) or (row["status"] if row else "") or "").strip().lower()


def paper_confirmation(strategy_id: str, since: str | None) -> dict[str, Any]:
    """Closed paper trades since ``since`` that are the backtest engine's own replay.

    A paper trade "matches its backtest" when the kernel (the backtest engine
    run forward) opened and closed it and its P&L is recorded on the backtest's
    basis (``kernel_managed`` + ``pnl_is_equity_fraction``, not a partial close).
    """
    params: list[object] = [strategy_id]
    where = ""
    if since:
        where = " AND datetime(closed_at) >= datetime(?)"
        params.append(since)
    with _db().get_db() as conn:
        rows = conn.execute(
            "SELECT signal_data FROM trades WHERE COALESCE(NULLIF(strategy_id, ''), strategy) = ? "
            "AND status = 'CLOSED' AND LOWER(COALESCE(execution_type, '')) LIKE 'paper%'" + where,
            tuple(params),
        ).fetchall()
    matched = mismatched = 0
    for row in rows:
        sd = _json(row["signal_data"], {})
        if sd.get("kernel_managed") and sd.get("pnl_is_equity_fraction") and not sd.get("close_incomplete"):
            matched += 1
        else:
            mismatched += 1
    return {"matched": matched, "mismatched": mismatched, "closed": len(rows)}


def live_trades(strategy_id: str, since: str | None) -> list[dict[str, Any]]:
    """Closed live trades of ``strategy_id`` opened since ``since``, oldest first."""
    params: list[object] = [strategy_id]
    where = ""
    if since:
        where = " AND datetime(COALESCE(opened_at, created_at)) >= datetime(?)"
        params.append(since)
    with _db().get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE COALESCE(NULLIF(strategy_id, ''), strategy) = ? AND status = 'CLOSED' "
            "AND LOWER(COALESCE(execution_type, 'live')) LIKE 'live%'" + where
            + " ORDER BY datetime(COALESCE(closed_at, opened_at))",
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


def open_live_positions(strategy_id: str) -> int:
    with _db().get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE COALESCE(NULLIF(strategy_id, ''), strategy) = ? "
            "AND status = 'OPEN' AND LOWER(COALESCE(execution_type, 'live')) NOT IN "
            "('paper', 'paper_challenger', 'simulation')",
            (strategy_id,),
        ).fetchone()
    return int(row["n"] or 0) if row else 0


def backtest_returns(strategy_id: str, cfg: dict) -> tuple[list[float], dict]:
    try:
        returns, info = _mod("forven.gauntlet.deflated_sharpe").load_backtest_trade_returns(strategy_id, cfg)
    except Exception as exc:  # noqa: BLE001 — no backtest series means no backtest comparison
        log.warning("Backtest trade returns unavailable for %s: %s", strategy_id, exc)
        return [], {"reason": f"error: {exc}"}
    return rules.normalise_backtest_returns(returns), info


def _cost_stats(strategy_id: str, trades: list[dict]) -> dict[str, Any]:
    row = _strategy_row(strategy_id)
    params = _json(row["params"] if row else None, {})
    try:
        _, fee_bps, slip_bps = _mod("forven.scanner")._resolve_trade_assumptions(params)
    except Exception:  # noqa: BLE001 — the engine defaults
        fee_bps, slip_bps = 4.5, 2.0
    skews = [
        float(t.get("entry_slippage_bps") or 0.0) + float(t.get("exit_slippage_bps") or 0.0)
        for t in trades
        if t.get("entry_slippage_bps") is not None or t.get("exit_slippage_bps") is not None
    ]
    modeled = 2.0 * (float(fee_bps) + float(slip_bps))
    measured = (sum(skews) / len(skews) + 2.0 * float(fee_bps)) if skews else None
    return {"trades": len(skews), "measured_bps": measured, "modeled_bps": modeled}


def _mismatched_live(trades: list[dict]) -> int:
    return sum(1 for t in trades if not _json(t.get("signal_data"), {}).get("kernel_managed"))


def _sequential_state(live: list[float], backtest: list[float], k: dict, cfg: dict) -> str | None:
    live_evidence = _mod("forven.live_evidence")
    gate = live_evidence.settings(cfg)
    try:
        result = live_evidence.sequential_evidence(
            live,
            backtest,
            alpha=gate["live_sequential_alpha"],
            backtest_weight=gate["live_backtest_weight"],
            pool_size=1,
            min_trades=int(k["demote_sequential_min_trades"]),
            correction="none",
        )
    except Exception as exc:  # noqa: BLE001 — no test result is no trigger
        log.debug("Sequential test unavailable: %s", exc)
        return None
    return str(result.get("state") or "") or None


def live_record(strategy_id: str, rec: dict | None, cfg: dict | None = None) -> dict[str, Any]:
    """The live record since probation began and every demotion trigger it has hit."""
    cfg = cfg if cfg is not None else config()
    k = rules.settings(cfg)
    since = (rec or {}).get("entered_at")
    trades = live_trades(strategy_id, since)
    live = [r for r in (rules.margin_return(t) for t in trades) if r is not None]
    backtest, bt_info = backtest_returns(strategy_id, cfg)
    cost = _cost_stats(strategy_id, trades)
    mismatched = _mismatched_live(trades)
    sequential = _sequential_state(live, backtest, k, cfg) if live else None
    hits = rules.demotion_reasons(
        live, backtest, k, sequential_state=sequential, cost=cost, mismatched_trades=mismatched
    )
    bt_dd = rules.max_drawdown(backtest) if backtest else None
    return {
        "live_trades": len(trades),
        "live_return_pct": round(sum(live) * 100.0, 3) if live else 0.0,
        "live_drawdown_pct": round(rules.max_drawdown(live) * 100.0, 3) if live else 0.0,
        "drawdown_limit_pct": (
            round(bt_dd * float(k["demote_drawdown_multiple"]) * 100.0, 3) if bt_dd else None
        ),
        "losing_streak": rules.longest_losing_streak(live),
        "losing_streak_limit": (rules.longest_losing_streak(backtest) + int(k["demote_streak_extra"])) if backtest else None,
        "backtest_trades": len(backtest),
        "backtest_source": bt_info,
        "sequential_state": sequential,
        "cost": cost,
        "mismatched_trades": mismatched,
        "triggers": hits,
    }


# --- entry ----------------------------------------------------------------------


def _check(key: str, label: str, ok: bool | None, detail: str, *, bypassable: bool = True) -> dict[str, Any]:
    return {"key": key, "label": label, "ok": ok, "detail": detail, "bypassable": bypassable}


def entry_check(strategy_id: str, cfg: dict | None = None, *, submit_holdout: bool = False) -> dict[str, Any]:
    """Can this paper strategy start probation? Every check, passing or not.

    ``ok`` is True (met), False (not met) or None (pending, e.g. a held-back
    test still running). ``eligible`` needs every check met.
    """
    cfg = cfg if cfg is not None else config()
    k = rules.settings(cfg)
    policy = _mod("forven.policy")
    row = _strategy_row(strategy_id)
    if row is None:
        raise ProbationError(f"Strategy {strategy_id} not found")
    stage = _stage(row)
    checks: list[dict[str, Any]] = []
    checks.append(_check(
        "stage", "In paper", stage in {"paper", "paper_trading"},
        "in paper" if stage in {"paper", "paper_trading"} else f"in {stage or 'unknown'}; probation starts from paper",
    ))

    since = str(row["stage_changed_at"] or "").strip() or None
    started = _parse_ts(since)
    paper_days = (_now() - started).days if started else 0
    metrics = _json(row["metrics"], {})
    pace, avg_trade = policy._backtest_pace(metrics)
    slow = pace is not None and pace < float(k["slow_pace_per_month"])
    confirmation = paper_confirmation(strategy_id, since)
    if slow:
        need, need_days = int(k["slow_confirm_trades"]), int(k["slow_confirm_days"])
        ok = confirmation["matched"] >= need and paper_days >= need_days
        detail = (
            f"{confirmation['matched']} of {need} matching paper trade(s) and {paper_days} of {need_days} days "
            f"(slow strategy: backtest trades {pace:.1f} times a month)"
        )
    else:
        need, need_days = int(k["confirm_trades"]), 0
        ok = confirmation["matched"] >= need
        detail = f"{confirmation['matched']} of {need} paper trades match the backtest replay"
    if confirmation["mismatched"]:
        detail += f"; {confirmation['mismatched']} paper trade(s) were not the replay's own"
    checks.append(_check("paper_confirmation", "Paper trades match the backtest", ok, detail))

    try:
        paper = policy._check_paper_return(strategy_id)
        extra = paper[2] if len(paper) > 2 and isinstance(paper[2], dict) else {}
        paper_return = float(extra.get("current") or 0.0)
        checks.append(_check(
            "paper_breakeven", "Paper at breakeven or better", paper_return >= 0,
            f"paper result {paper_return:+.2f}% since it entered paper",
        ))
    except Exception as exc:  # noqa: BLE001 — fail closed before real money
        checks.append(_check("paper_breakeven", "Paper at breakeven or better", False, f"paper result unavailable: {exc}"))

    coverage = _mod("forven.paper_pool").cost_coverage(avg_trade)
    want = float(k["min_cost_coverage"])
    if coverage is None:
        checks.append(_check("edge_covers_costs", "Edge covers costs", False, "the backtest's average trade is not measured"))
    else:
        checks.append(_check(
            "edge_covers_costs", "Edge covers costs", coverage >= want,
            f"average trade covers {coverage:.1f}x the round-trip cost (need {want:g}x)",
        ))

    try:
        exec_state = _mod("forven.strategies.execution_check").execution_check(strategy_id)
        executable = exec_state.get("executable")
        checks.append(_check(
            "execution_verified", "Execution settings verified", bool(executable),
            "the strategy runs the settings its backtest validated" if executable
            else str(exec_state.get("reason") or "execution settings are not verified"),
            bypassable=False,
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("execution_verified", "Execution settings verified", False, f"unavailable: {exc}", bypassable=False))

    try:
        fresh, fresh_detail = policy._check_engine_artifact_freshness(strategy_id)
        checks.append(_check("engine_fresh", "Results from the current engine", bool(fresh), str(fresh_detail)))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("engine_fresh", "Results from the current engine", False, f"unverifiable: {exc}"))

    if not _mod("forven.paper_door").holdout_at_paper(cfg):
        try:
            holdout = policy.holdout_gate_reason(strategy_id, submit=submit_holdout, stage="live")
        except Exception as exc:  # noqa: BLE001
            holdout = (f"held-back test unavailable: {exc}", "error")
        if holdout is None:
            checks.append(_check("held_back", "Held-back test", True, "passed or not required"))
        else:
            pending = "pending" in str(holdout[1] or "")
            checks.append(_check("held_back", "Held-back test", None if pending else False, str(holdout[0])))

    try:
        with _db().get_db() as conn:
            refusal = _mod("forven.exchange.risk").go_live_refusal(
                conn, strategy_id, candidate_weight=float(k["start_size_pct"]) / 100.0
            )
    except Exception as exc:  # noqa: BLE001
        refusal = f"live admission check unavailable: {exc}"
    checks.append(_check(
        "live_admission", "Room on the live account", refusal is None,
        refusal or "no other live strategy trades this coin and side, and the wallet can hold it",
        bypassable=False,
    ))

    rec = get_record(strategy_id)
    if rec and rec.get("last_demoted_at"):
        demoted = _parse_ts(rec["last_demoted_at"])
        wait = int(k["reentry_cooldown_days"])
        if demoted is not None:
            days = (_now() - demoted).days
            checks.append(_check(
                "cooldown", "Cooldown after demotion", days >= wait,
                f"demoted {days} of {wait} days ago",
            ))

    in_now = len(list_records(rules.IN_PROBATION))
    cap = int(k["max_strategies"])
    if cap:
        checks.append(_check(
            "capacity", "Room in probation", in_now < cap, f"{in_now} of {cap} strategies in probation",
        ))

    coins = policy.strategy_container_coins(strategy_id, row["symbol"]) if stage.startswith("paper") else None
    notes = []
    if coins and len(coins) > 1:
        notes.append(
            f"Paper trades {len(coins)} coins; probation trades the home coin {coins[0]} only, "
            "and the other coins resume in paper if it is demoted"
        )
    return {
        "strategy_id": strategy_id,
        "eligible": all(c["ok"] is True for c in checks),
        "checks": checks,
        "failing": [c for c in checks if c["ok"] is not True],
        "slow": slow,
        "trades_per_month": None if pace is None else round(pace, 2),
        "paper_days": paper_days,
        "confirmation": confirmation,
        "need_trades": int(k["slow_confirm_trades"] if slow else k["confirm_trades"]),
        "need_days": int(k["slow_confirm_days"]) if slow else 0,
        "notes": notes,
    }


def _notify(strategy_id: str, title: str, summary: str, *, severity: str = "info", kind: str) -> None:
    try:
        _mod("forven.notifications").emit_notification(
            "pipeline_transition",
            severity=severity,
            source="probation",
            title=title,
            summary=summary,
            metadata={"strategy_id": strategy_id, "probation": kind, "execution_type": "live"},
            dedupe_key=f"probation:{kind}:{strategy_id}:{_now().strftime('%Y%m%d%H')}",
        )
    except Exception as exc:  # noqa: BLE001 — a lost notification never blocks a move
        log.warning("Probation notification failed for %s: %s", strategy_id, exc)


def _label(strategy_id: str) -> str:
    row = _strategy_row(strategy_id)
    if not row:
        return strategy_id
    return f"{row['display_id'] or strategy_id} ({row['symbol'] or '?'} {row['timeframe'] or ''})".strip()


def _is_operator(actor: str) -> bool:
    return actor.lower() != ACTOR


def start(
    strategy_id: str,
    *,
    actor: str,
    confirm: str | None = None,
    bypass: bool = False,
    reason: str = "",
    cfg: dict | None = None,
) -> dict[str, Any]:
    """Move a paper strategy into probation (live at the start size).

    An operator (any actor but the watchdog) always types ``PROBATION``. A
    failing check blocks unless ``bypass``; checks that protect the account
    itself (execution settings, room on the live account) cannot be bypassed.
    A gauntlet strategy may skip paper with ``bypass``: it is moved to paper
    first so its execution settings are captured.
    """
    cfg = cfg if cfg is not None else config()
    k = rules.settings(cfg)
    operator = _is_operator(actor)
    if operator and str(confirm or "").strip().upper() != rules.CONFIRM_PHRASE:
        raise ProbationError(
            f"Type {rules.CONFIRM_PHRASE} to confirm: this trades real money", needs_confirm=rules.CONFIRM_PHRASE
        )
    row = _strategy_row(strategy_id)
    if row is None:
        raise ProbationError(f"Strategy {strategy_id} not found")
    brain = _mod("forven.brain")
    if _stage(row) == "gauntlet":
        if not (operator and bypass):
            raise ProbationError("Probation starts from paper; skipping paper needs an operator bypass")
        moved = brain.transition_stage(
            strategy_id, "paper", reason=f"Skip paper into probation: {reason}".strip(), actor=actor, force=True
        )
        if str(moved.get("to") or "") != "paper":
            raise ProbationError(f"Could not move to paper first: {moved.get('blocked_reason') or 'blocked'}")
    report = entry_check(strategy_id, cfg, submit_holdout=True)
    failing = report["failing"]
    hard = [c for c in failing if not c["bypassable"] or c["key"] == "stage"]
    if hard:
        raise ProbationError(
            "Cannot start probation: " + "; ".join(f"{c['label']}: {c['detail']}" for c in hard), checks=report["checks"]
        )
    if failing and not (operator and bypass):
        raise ProbationError(
            "Not ready for probation: " + "; ".join(f"{c['label']}: {c['detail']}" for c in failing),
            checks=report["checks"],
        )
    rec_before = get_record(strategy_id)
    transition = brain.transition_stage(
        strategy_id,
        "live_graduated",
        reason=reason or ("Operator started probation" if operator else "Paper confirmation passed: probation"),
        actor=actor,
        probation_entry=True,
    )
    if str(transition.get("to") or "") != "live_graduated":
        raise ProbationError(
            f"Probation blocked: {transition.get('blocked_reason') or 'the stage change was refused'}",
            checks=report["checks"],
        )
    size = float(k["start_size_pct"])
    bypassed = [c["key"] for c in failing]
    _save(
        strategy_id,
        state=rules.STATE_ACTIVE,
        size_pct=size,
        size_locked=False,
        entered_at=_iso(),
        entered_by=actor,
        bypassed=bypassed,
        demotion_count=int((rec_before or {}).get("demotion_count") or 0),
        last_demoted_at=(rec_before or {}).get("last_demoted_at"),
        status_reason=None,
        history_event={"event": "started", "actor": actor, "size_pct": size, "bypassed": bypassed},
    )
    _db().log_activity(
        "warning", "probation",
        f"{strategy_id} started probation at {size:g}% size ({actor})"
        + (f"; bypassed: {', '.join(bypassed)}" if bypassed else ""),
        {"strategy_id": strategy_id, "actor": actor, "bypassed": bypassed},
    )
    _notify(
        strategy_id,
        f"Probation started: {_label(strategy_id)}",
        f"Trading real money at {size:g}% size. Full size after {k['graduate_trades']} live trades if it keeps to its backtest."
        + (f" Checks bypassed: {', '.join(bypassed)}." if bypassed else ""),
        severity="warn",
        kind="started",
    )
    return status(strategy_id, cfg)


def set_size(strategy_id: str, size_pct: float | None, *, actor: str, confirm: str | None = None) -> dict[str, Any]:
    """Set a probation strategy's size by hand (it holds until changed), or None to follow the ladder again.

    100% is graduation: use ``graduate``.
    """
    rec = get_record(strategy_id)
    if not rec or not rec["in_probation"]:
        raise ProbationError(f"{strategy_id} is not in probation")
    if size_pct is None:
        k = knobs()
        size = rules.ladder_size_pct(len(live_trades(strategy_id, rec["entered_at"])), k)
        size = min(size, float(k["step_size_pct"]))
        _save(strategy_id, size_pct=size, size_locked=False,
              history_event={"event": "ladder_resumed", "actor": actor, "size_pct": size})
        return status(strategy_id)
    size = float(size_pct)
    if not math.isfinite(size) or size <= 0 or size > 100:
        raise ProbationError("Size must be between 1 and 100 percent")
    if size >= 100:
        return graduate(strategy_id, actor=actor, confirm=confirm, bypass=True)
    _save(strategy_id, size_pct=size, size_locked=True,
          history_event={"event": "size_set", "actor": actor, "size_pct": size})
    _db().log_activity("info", "probation", f"{strategy_id} probation size set to {size:g}% by {actor}",
                       {"strategy_id": strategy_id, "size_pct": size})
    return status(strategy_id)


def graduation_check(strategy_id: str, rec: dict, cfg: dict | None = None, record: dict | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else config()
    k = rules.settings(cfg)
    record = record if record is not None else live_record(strategy_id, rec, cfg)
    need = int(k["graduate_trades"])
    checks = [
        _check("live_trades", "Live trades", record["live_trades"] >= need, f"{record['live_trades']} of {need} live trades"),
        _check("live_result", "Live result", record["live_return_pct"] >= 0, f"live total {record['live_return_pct']:+.2f}%"),
        _check("no_triggers", "Inside the backtest's range", not record["triggers"],
               "; ".join(t["label"] for t in record["triggers"]) or "no demotion trigger hit"),
    ]
    live_evidence = _mod("forven.live_evidence")
    if live_evidence.settings(cfg)["live_require_breadth"]:
        why = live_evidence.breadth_reason(_mod("forven.policy").strategy_breadth(strategy_id))
        checks.append(_check("breadth", "Breadth test", why is None, why or "works on other coins"))
    return {"eligible": all(c["ok"] is True for c in checks), "checks": checks,
            "failing": [c for c in checks if c["ok"] is not True]}


def graduate(
    strategy_id: str, *, actor: str, confirm: str | None = None, bypass: bool = False, cfg: dict | None = None
) -> dict[str, Any]:
    """Full size: the probation ends and the strategy stays live at 100%.

    An operator types ``GO LIVE``; a failing graduation check blocks unless ``bypass``.
    """
    cfg = cfg if cfg is not None else config()
    rec = get_record(strategy_id)
    if not rec or not rec["in_probation"]:
        raise ProbationError(f"{strategy_id} is not in probation")
    operator = _is_operator(actor)
    phrase = _mod("forven.exchange.risk").GO_LIVE_CONFIRM_PHRASE
    if operator and str(confirm or "").strip().upper() != phrase:
        raise ProbationError(f"Type {phrase} to confirm full size", needs_confirm=phrase)
    report = graduation_check(strategy_id, rec, cfg)
    if report["failing"] and not (operator and bypass):
        raise ProbationError(
            "Not ready for full size: " + "; ".join(f"{c['label']}: {c['detail']}" for c in report["failing"]),
            checks=report["checks"],
        )
    bypassed = [c["key"] for c in report["failing"]]
    _save(strategy_id, state=rules.STATE_GRADUATED, size_pct=100.0, size_locked=False, status_reason=None,
          history_event={"event": "graduated", "actor": actor, "bypassed": bypassed})
    _db().log_activity("warning", "probation", f"{strategy_id} graduated from probation to full size ({actor})",
                       {"strategy_id": strategy_id, "bypassed": bypassed})
    _notify(strategy_id, f"Full size: {_label(strategy_id)}",
            "Probation complete: the strategy now trades live at full size.", kind="graduated")
    return status(strategy_id, cfg)


def pause(strategy_id: str, paused: bool, *, actor: str) -> dict[str, Any]:
    rec = get_record(strategy_id)
    if not rec or rec["state"] not in {rules.STATE_ACTIVE, rules.STATE_PAUSED}:
        raise ProbationError(f"{strategy_id} is not in probation")
    state = rules.STATE_PAUSED if paused else rules.STATE_ACTIVE
    _save(strategy_id, state=state, history_event={"event": "paused" if paused else "resumed", "actor": actor})
    _db().log_activity("info", "probation", f"{strategy_id} probation entries {'paused' if paused else 'resumed'} by {actor}",
                       {"strategy_id": strategy_id})
    return status(strategy_id)


def demote(strategy_id: str, *, actor: str, reason: str, triggers: list[dict] | None = None) -> dict[str, Any]:
    """Send a probation (or graduated) strategy back to paper.

    No new entries from now; the stage changes once no live position is open
    (an open position keeps its exchange stop and runs to its own exit).
    """
    rec = get_record(strategy_id)
    row = _strategy_row(strategy_id)
    if row is None:
        raise ProbationError(f"Strategy {strategy_id} not found")
    if _stage(row) != "live_graduated":
        raise ProbationError(f"{strategy_id} is not live")
    if rec is None:
        _save(strategy_id, state=rules.STATE_DEMOTING, size_pct=100.0, entered_at=None, entered_by=None)
    _save(strategy_id, state=rules.STATE_DEMOTING, status_reason=reason,
          history_event={"event": "demotion_started", "actor": actor, "reason": reason,
                         "triggers": [t.get("key") for t in triggers or []]})
    _db().log_activity("warning", "probation", f"{strategy_id} demotion to paper started ({actor}): {reason}",
                       {"strategy_id": strategy_id, "triggers": triggers or []})
    _notify(strategy_id, f"Demoting to paper: {_label(strategy_id)}",
            f"{reason}. No new live entries; it moves to paper once any open position closes.",
            severity="warn", kind="demoting")
    _finish_demotion(strategy_id)
    return status(strategy_id)


def _finish_demotion(strategy_id: str, cfg: dict | None = None) -> bool:
    """Complete a pending demotion when no live position is open. True when done."""
    rec = get_record(strategy_id)
    if not rec or rec["state"] != rules.STATE_DEMOTING:
        return False
    row = _strategy_row(strategy_id)
    if row is not None and _stage(row) == "live_graduated":
        if open_live_positions(strategy_id):
            return False
        moved = _mod("forven.brain").transition_stage(
            strategy_id, "paper", reason=f"Probation demotion: {rec.get('status_reason') or 'demoted'}",
            actor=ACTOR, force=True,
        )
        if str(moved.get("to") or "") != "paper":
            log.warning("Probation demotion of %s blocked: %s", strategy_id, moved.get("blocked_reason"))
            return False
    count = int(rec.get("demotion_count") or 0) + 1
    _save(strategy_id, state=rules.STATE_DEMOTED, demotion_count=count, last_demoted_at=_iso(),
          history_event={"event": "demoted", "reason": rec.get("status_reason"), "demotion_count": count})
    k = knobs(cfg)
    limit = int(k["archive_after_demotions"])
    if limit and count >= limit:
        try:
            _mod("forven.brain").transition_stage(
                strategy_id, "archived", reason=f"Demoted from probation {count} times", actor=ACTOR, force=True,
                evidence={"status_reason": f"probation_demoted:{count}"},
            )
            _save(strategy_id, history_event={"event": "archived", "demotion_count": count})
        except Exception as exc:  # noqa: BLE001 — it stays in paper; retirement can still take it
            log.warning("Archiving %s after %d probation demotions failed: %s", strategy_id, count, exc)
    _notify(strategy_id, f"Back in paper: {_label(strategy_id)}",
            f"Demoted from probation: {rec.get('status_reason') or 'demoted'}."
            + (f" Archived after {count} demotions." if limit and count >= limit else ""),
            severity="warn", kind="demoted")
    return True


# --- status for the screens ----------------------------------------------------


def status(strategy_id: str, cfg: dict | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else config()
    k = rules.settings(cfg)
    row = _strategy_row(strategy_id)
    if row is None:
        raise ProbationError(f"Strategy {strategy_id} not found")
    stage = _stage(row)
    rec = get_record(strategy_id)
    out: dict[str, Any] = {
        "strategy_id": strategy_id,
        "stage": stage,
        "record": rec,
        "in_probation": bool(rec and rec["in_probation"] and stage == "live_graduated"),
        "automation": get_automation(),
        "ladder": {
            "start_size_pct": k["start_size_pct"], "step_trades": k["step_trades"],
            "step_size_pct": k["step_size_pct"], "graduate_trades": k["graduate_trades"],
        },
        "skips": _db().kv_get(f"{SKIPS_KV_PREFIX}{strategy_id}", []) or [],
    }
    if rec and stage == "live_graduated" and rec["state"] in rules.IN_PROBATION | {rules.STATE_GRADUATED}:
        record = live_record(strategy_id, rec, cfg)
        out["live"] = record
        out["open_positions"] = open_live_positions(strategy_id)
        out["next_step"] = rules.next_step(record["live_trades"], k) if rec["in_probation"] else None
        out["ladder_size_pct"] = rules.ladder_size_pct(record["live_trades"], k)
        if rec["in_probation"]:
            out["graduation"] = graduation_check(strategy_id, rec, cfg, record)
    elif stage in {"paper", "paper_trading"}:
        out["entry"] = entry_check(strategy_id, cfg)
    return out


def overview(cfg: dict | None = None) -> dict[str, Any]:
    """Every strategy in probation (with its live record) and the ready list."""
    cfg = cfg if cfg is not None else config()
    k = rules.settings(cfg)
    rows = []
    for rec in list_records(rules.IN_PROBATION):
        row = _strategy_row(rec["strategy_id"])
        if row is None or _stage(row) != "live_graduated":
            continue
        try:
            record = live_record(rec["strategy_id"], rec, cfg)
        except Exception as exc:  # noqa: BLE001 — one unreadable record must not hide the rest
            record = {"error": str(exc), "live_trades": 0, "live_return_pct": 0.0, "triggers": []}
        rows.append({
            "strategy_id": rec["strategy_id"],
            "display_id": row["display_id"] or rec["strategy_id"],
            "name": row["display_name"] if "display_name" in row.keys() and row["display_name"] else row["name"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "state": rec["state"],
            "size_pct": rec["size_pct"],
            "size_locked": rec["size_locked"],
            "entered_at": rec["entered_at"],
            "live_trades": record.get("live_trades", 0),
            "live_return_pct": record.get("live_return_pct", 0.0),
            "triggers": record.get("triggers", []),
            "next_step": rules.next_step(int(record.get("live_trades") or 0), k),
        })
    return {
        "strategies": rows,
        "ladder": {"start_size_pct": k["start_size_pct"], "step_trades": k["step_trades"],
                   "step_size_pct": k["step_size_pct"], "graduate_trades": k["graduate_trades"]},
        "max_strategies": k["max_strategies"],
        "automation": get_automation(),
        "ready": ready_cache(),
    }


def ready_cache() -> dict[str, Any]:
    """The watchdog's last entry checks of every paper strategy (refreshed hourly)."""
    return _db().kv_get(READY_KV_KEY, None) or {"checked_at": None, "strategies": []}


def stage_badges(strategy_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """{strategy_id: {state, size_pct}} for strategies in probation: the lists' badge."""
    out = {}
    try:
        for rec in list_records(rules.IN_PROBATION):
            if strategy_ids is None or rec["strategy_id"] in strategy_ids:
                out[rec["strategy_id"]] = {"state": rec["state"], "size_pct": rec["size_pct"]}
    except Exception as exc:  # noqa: BLE001 — a list without badges still renders
        log.debug("Probation badges unavailable: %s", exc)
    return out


# --- the watchdog ----------------------------------------------------------------


def tick(cfg: dict | None = None) -> dict[str, Any]:
    """Hourly: finish pending demotions, demote on triggers, step sizes, graduate, admit.

    What it does on its own follows the automation switches; with everything
    off it only finishes demotions an operator started and refreshes the ready list.
    """
    cfg = cfg if cfg is not None else config()
    k = rules.settings(cfg)
    auto = get_automation()
    out: dict[str, Any] = {"demoted": [], "demoting": [], "stepped": [], "graduated": [], "started": [], "ready": []}
    try:
        _watch_full_live()
    except Exception as exc:  # noqa: BLE001
        log.warning("Probation could not adopt full-size live strategies: %s", exc)
    for rec in list_records():
        sid = rec["strategy_id"]
        try:
            _tick_one(sid, rec, cfg, k, auto, out)
        except Exception as exc:  # noqa: BLE001 — one strategy's failure never stops the rest
            log.warning("Probation tick failed for %s: %s", sid, exc)
    try:
        _refresh_ready(cfg, k, auto, out)
    except Exception as exc:  # noqa: BLE001
        log.warning("Probation ready-list refresh failed: %s", exc)
    return out


def _watch_full_live() -> None:
    """Watch every full-size live strategy with the probation triggers.

    A strategy that reached full size without probation (before it existed, or
    by an operator skipping it) gets a ``graduated`` record from its live start,
    so a demotion trigger drops it to probation like any graduate.
    """
    with _db().get_db() as conn:
        rows = conn.execute(
            "SELECT s.id, s.stage_changed_at, p.state FROM strategies s "
            "LEFT JOIN strategy_probation p ON p.strategy_id = s.id "
            "WHERE LOWER(TRIM(COALESCE(s.stage, ''))) IN ('live_graduated', 'deployed')"
        ).fetchall()
    for row in rows:
        if row["state"] in rules.IN_PROBATION | {rules.STATE_GRADUATED}:
            continue
        _save(str(row["id"]), state=rules.STATE_GRADUATED, size_pct=100.0, size_locked=False,
              entered_at=row["stage_changed_at"] or _iso(), entered_by="full_live", status_reason=None,
              history_event={"event": "watching_full_live"})


def _tick_one(sid: str, rec: dict, cfg: dict, k: dict, auto: dict, out: dict) -> None:
    state = rec["state"]
    if state == rules.STATE_DEMOTING:
        if _finish_demotion(sid, cfg):
            out["demoted"].append(sid)
        return
    if state not in rules.IN_PROBATION | {rules.STATE_GRADUATED}:
        return
    row = _strategy_row(sid)
    if row is None or _stage(row) != "live_graduated":
        # Moved out of live by something else (an operator demotion, an archive):
        # the probation is over without counting as a probation demotion.
        _save(sid, state=rules.STATE_DEMOTED, status_reason=f"left live ({_stage(row) if row else 'deleted'})",
              history_event={"event": "ended_outside_probation", "stage": _stage(row) if row else None})
        return
    record = live_record(sid, rec, cfg)
    _save(sid, last_check={"at": _iso(), "live_trades": record["live_trades"],
                           "live_return_pct": record["live_return_pct"], "triggers": record["triggers"]})
    if record["triggers"]:
        reason = "; ".join(t["label"] for t in record["triggers"])
        if state == rules.STATE_GRADUATED:
            if auto["auto_demote"]:
                # Full live drops to probation first, not straight to paper.
                size = float(k["start_size_pct"])
                _save(sid, state=rules.STATE_ACTIVE, size_pct=size, size_locked=False, entered_at=_iso(),
                      status_reason=reason,
                      history_event={"event": "back_to_probation", "reason": reason})
                _notify(sid, f"Back to probation: {_label(sid)}",
                        f"{reason}. Trading at {size:g}% size again.", severity="warn", kind="back_to_probation")
                out["demoting"].append(sid)
            return
        if auto["auto_demote"]:
            demote(sid, actor=ACTOR, reason=reason, triggers=record["triggers"])
            out["demoting"].append(sid)
        return
    if state != rules.STATE_ACTIVE:
        return
    ladder = rules.ladder_size_pct(record["live_trades"], k)
    if ladder >= 100.0:
        report = graduation_check(sid, rec, cfg, record)
        if report["eligible"] and auto["auto_graduate"]:
            graduate(sid, actor=ACTOR, cfg=cfg)
            out["graduated"].append(sid)
        elif report["eligible"] and not any(h.get("event") == "graduation_ready" for h in rec["history"][-5:]):
            _save(sid, history_event={"event": "graduation_ready"})
            _notify(sid, f"Ready for full size: {_label(sid)}",
                    f"{record['live_trades']} live trades inside its backtest's range. Graduate it on its page.",
                    kind="graduation_ready")
        ladder = float(k["step_size_pct"])
    if auto["auto_entry"] and not rec["size_locked"] and ladder > float(rec["size_pct"]):
        _save(sid, size_pct=ladder, history_event={"event": "size_step", "actor": ACTOR, "size_pct": ladder})
        _notify(sid, f"Probation size up: {_label(sid)}",
                f"{record['live_trades']} live trades inside its backtest's range: now {ladder:g}% size.",
                kind="size_step")
        out["stepped"].append(sid)


def _refresh_ready(cfg: dict, k: dict, auto: dict, out: dict) -> None:
    with _db().get_db() as conn:
        rows = conn.execute(
            "SELECT id, display_id, name, symbol, timeframe FROM strategies "
            "WHERE LOWER(TRIM(COALESCE(stage, ''))) IN ('paper', 'paper_trading') "
            "ORDER BY COALESCE(stage_changed_at, created_at)"
        ).fetchall()
    previous = {s["strategy_id"]: s for s in ((_db().kv_get(READY_KV_KEY, None) or {}).get("strategies") or [])}
    ready: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row["id"])
        try:
            report = entry_check(sid, cfg)
        except Exception as exc:  # noqa: BLE001
            log.debug("Probation entry check failed for %s: %s", sid, exc)
            continue
        entry = {
            "strategy_id": sid,
            "display_id": row["display_id"] or sid,
            "name": row["name"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "eligible": report["eligible"],
            "failing": [
                {"key": c["key"], "label": c["label"], "detail": c["detail"], "ok": c["ok"]}
                for c in report["failing"]
            ],
            "confirmation": report["confirmation"],
            "slow": report["slow"],
            "paper_days": report["paper_days"],
            "checked_at": _iso(),
        }
        if report["eligible"] and auto["auto_entry"]:
            try:
                start(sid, actor=ACTOR, cfg=cfg)
                out["started"].append(sid)
                continue
            except ProbationError as exc:
                entry["eligible"] = False
                entry["failing"].append({"key": "start", "label": "Start refused", "detail": str(exc)})
        if entry["eligible"]:
            out["ready"].append(sid)
            if not previous.get(sid, {}).get("eligible"):
                _notify(sid, f"Ready for probation: {_label(sid)}",
                        "Its paper trades match its backtest. Start probation on its page or on the Live trades page.",
                        kind="ready")
        ready.append(entry)
    ready.sort(key=lambda e: (not e["eligible"], len(e["failing"])))
    _db().kv_set(READY_KV_KEY, {"checked_at": _iso(), "strategies": ready})
