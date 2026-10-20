"""The paper pool: how many strategies paper-trade at once, who waits, and who leaves.

The 2026-10 gate plan opened the paper door (forven.paper_door) so research
strategies reach forward testing instead of dying on selection-bias tests. A
wide door needs three things so paper stays useful:

* **A cap.** ``max_strategies`` strategies paper-trade at once (0 = no cap). The
  default of 40 comes from a measured scan cost: one strategy's signal plus
  execution pass costs about 45-85 ms per bar close on a 4-core machine, so 40
  strategies fit well inside one 1h bar even on a slow install while leaving the
  live scanner its CPU.
* **An evidence-ranked queue with a diversity cap.** A strategy that passes the
  door while the pool is full (or while its family already holds
  ``max_per_family`` paper strategies, or its market ``max_per_market`` when an
  operator sets one) waits in
  ``paper_queue``, ranked by the evidence the door recorded: deflated Sharpe,
  composite robustness score and out-of-sample Sharpe. Free slots go to the
  highest-ranked waiting strategy first. Waiting is capacity, not merit: it
  never counts towards the repeated-failure auto-archive.
* **Automatic forward retirement.** A paper strategy leaves (archived with
  ``status_reason = paper_retired:<why>``) when its forward record shows it is
  losing: ``retire_min_trades`` closed trades with a negative mean and a t-stat
  below ``retire_max_t_stat``; a forward drawdown of ``retire_max_drawdown_pct``;
  no trade for ``retire_idle_days``; or a sequential live-evidence FAIL (forward
  contradicts its backtest). Live strategies are never touched here.

A LEAF (no first-party imports): every database function takes the caller's
connection, and the glue that needs the rest of Forven lives where that already
is: admission in ``forven.policy`` (``paper_admission_reason``, called by the
gauntlet->paper gate) and the scheduled retire-then-admit pass in
``forven.evolution`` (``paper_pool_tick``). That keeps this module out of the
first-party import cycle (tests/test_finish_db_layering.py).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from statistics import mean, stdev
from typing import Any, Callable, Mapping

log = logging.getLogger("forven.paper_pool")

DEFAULTS: dict[str, Any] = {
    "max_strategies": 40,  # 0 = no cap
    # 0 = no per-market cap. Off by default since 2026-10-20: each paper
    # strategy is its own simulated test, so several on one coin cost nothing
    # but CPU, which max_strategies already bounds.
    "max_per_market": 0,
    "max_per_family": 10,  # 0 = no per-family cap
    "retire_enabled": True,
    "retire_min_trades": 15,
    "retire_max_t_stat": -1.0,
    "retire_max_drawdown_pct": 0.15,
    "retire_idle_days": 60,
    "retire_on_sequential_fail": True,
    # Auto-ports: when a paper strategy's breadth test finds coins where the
    # frozen rule works, the same rule is submitted on up to this many of them as
    # single-coin siblings, each through the normal pipeline (0 = off).
    "auto_port_max": 5,
}

RETIREMENT_ACTOR = "paper_retirement"
ADMISSION_ACTOR = "paper_pool"
LIVE_RECHECK_KEY = "forven:paper_pool:live_recheck"

_PAPER_STAGES = ("paper", "paper_trading")
_KIND_CAPACITY = "capacity"
_KIND_DIVERSITY = "diversity"
# Queue entries not re-confirmed for this long are dropped (the strategy left the
# gauntlet, or its evidence went stale and the next gate run re-queues it).
_QUEUE_TTL_DAYS = 14
# Gauntlet->paper rejection codes the research door no longer blocks on: the
# re-admission report lists recent archives whose last rejection was one of these.
_RECORD_ONLY_REASON_CODES = {
    "wfa_reject",
    "robustness_reject",
    "monte_carlo_reject",
    "holdout_reject",
    "holdout_pending",
    "holdout_budget_exhausted",
    "dsr_unavailable",
    "source_reconciliation_pending",
}
_RECORD_ONLY_TEXT = ("dsr reject", "deflated sharpe", "deflated-sharpe", "jitter", "walk-forward")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_queue (
    strategy_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    market TEXT,
    family TEXT,
    reason TEXT,
    queued_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _int(value: object, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _float(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``paper_pool`` block with defaults filled and values bounded."""
    block = config.get("paper_pool") if isinstance(config, Mapping) else None
    block = block if isinstance(block, Mapping) else {}
    return {
        "max_strategies": max(0, _int(block.get("max_strategies"), DEFAULTS["max_strategies"])),
        "max_per_market": max(0, _int(block.get("max_per_market"), DEFAULTS["max_per_market"])),
        "max_per_family": max(0, _int(block.get("max_per_family"), DEFAULTS["max_per_family"])),
        "retire_enabled": bool(block.get("retire_enabled", DEFAULTS["retire_enabled"])),
        "retire_min_trades": max(2, _int(block.get("retire_min_trades"), DEFAULTS["retire_min_trades"])),
        "retire_max_t_stat": _float(block.get("retire_max_t_stat"), DEFAULTS["retire_max_t_stat"]),
        "retire_max_drawdown_pct": max(
            0.0, _float(block.get("retire_max_drawdown_pct"), DEFAULTS["retire_max_drawdown_pct"])
        ),
        "retire_idle_days": max(0, _int(block.get("retire_idle_days"), DEFAULTS["retire_idle_days"])),
        "retire_on_sequential_fail": bool(
            block.get("retire_on_sequential_fail", DEFAULTS["retire_on_sequential_fail"])
        ),
        "auto_port_max": max(0, min(15, _int(block.get("auto_port_max"), DEFAULTS["auto_port_max"]))),
    }


def ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)


def market_of(symbol: object) -> str:
    """The market a strategy trades, for the diversity cap ("BTC/USDT:USDT" -> "BTC/USDT")."""
    text = str(symbol or "").strip().upper()
    return text.split(":", 1)[0]


# Breadth test verdict (forven.breadth) added to the queue rank: a rule that
# works on most coins goes first, one fitted to its home coin goes last.
BREADTH_RANK_BONUS = {"general": 0.2, "mixed": 0.05, "too_few": 0.0, "home_only": -0.2}


def evidence_score(
    deflated_sharpe: object, robustness_points: object, oos_sharpe: object, breadth_verdict: object = None
) -> float:
    """Rank of a door-passing strategy in the paper queue (higher goes first).

    0.4 x deflated Sharpe (0-1) + 0.3 x robustness score (0-100, scaled to 0-1)
    + 0.3 x out-of-sample Sharpe squashed to 0-1 by tanh, plus the breadth
    verdict's BREADTH_RANK_BONUS. A missing component counts as zero, so
    better-evidenced strategies rank ahead.
    """
    dsr = min(max(_float(deflated_sharpe, 0.0), 0.0), 1.0)
    robustness = min(max(_float(robustness_points, 0.0) / 100.0, 0.0), 1.0)
    oos = max(math.tanh(_float(oos_sharpe, 0.0)), 0.0)
    breadth = BREADTH_RANK_BONUS.get(str(breadth_verdict or ""), 0.0)
    return round(0.4 * dsr + 0.3 * robustness + 0.3 * oos + breadth, 6)


def strategy_row(conn, strategy_id: str):
    return conn.execute(
        "SELECT id, name, type, runtime_type, params, symbol, stage, status, metrics, "
        "deflated_sharpe FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()


def paper_rows(conn) -> list:
    return conn.execute(
        "SELECT id, name, type, runtime_type, params, symbol, stage, stage_changed_at, created_at "
        "FROM strategies WHERE LOWER(TRIM(COALESCE(stage, ''))) IN ('paper', 'paper_trading')"
    ).fetchall()


def _queue_position(conn, strategy_id: str, score: float) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT q.strategy_id, q.score FROM paper_queue q JOIN strategies s ON s.id = q.strategy_id "
        "WHERE LOWER(TRIM(COALESCE(s.stage, ''))) = 'gauntlet' AND q.kind = ?",
        (_KIND_CAPACITY,),
    ).fetchall()
    others = [float(row["score"] or 0.0) for row in rows if str(row["strategy_id"]) != strategy_id]
    ahead = sum(1 for value in others if value > score)
    return ahead + 1, len(others) + 1


def _record(conn, strategy_id: str, kind: str, score: float, market: str, family: str, reason: str) -> None:
    now = _now().isoformat()
    conn.execute(
        "INSERT INTO paper_queue (strategy_id, kind, score, market, family, reason, queued_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(strategy_id) DO UPDATE SET kind = excluded.kind, score = excluded.score, "
        "market = excluded.market, family = excluded.family, reason = excluded.reason, "
        "updated_at = excluded.updated_at",
        (strategy_id, kind, score, market, family, reason, now, now),
    )


def admission_reason(
    conn,
    strategy_id: str,
    knobs: Mapping[str, Any],
    *,
    family_of: Callable[[Any], str],
    score_of: Callable[[Any], float],
    record: bool = True,
) -> str | None:
    """None when a door-passing strategy may take a paper slot now, else why it waits.

    ``knobs`` is ``settings(config)``; ``family_of`` / ``score_of`` map a strategy
    row to its family and evidence rank. With ``record`` the decision is
    persisted: a waiting strategy is (re)queued with its rank, an admitted one
    leaves the queue. The returned text starts "Paper pool" and contains
    "waiting for a paper slot", which the gate maps to the non-merit
    ``paper_queue_full`` reason code.
    """
    cap = knobs["max_strategies"]
    ensure_schema(conn)
    row = strategy_row(conn, strategy_id)
    if row is None:
        return None
    if str(row["stage"] or "").strip().lower() in _PAPER_STAGES:
        return None
    paper = [r for r in paper_rows(conn) if str(r["id"]) != strategy_id]
    market = market_of(row["symbol"])
    family = family_of(row)
    score = score_of(row)

    reason: str | None = None
    kind = _KIND_CAPACITY
    if knobs["max_per_market"] and market:
        in_market = sum(1 for r in paper if market_of(r["symbol"]) == market)
        if in_market >= knobs["max_per_market"]:
            kind = _KIND_DIVERSITY
            reason = (
                f"Paper pool diversity cap: {in_market} strategies already paper-trade {market} "
                f"(max {knobs['max_per_market']} per market) — waiting for a paper slot"
            )
    if reason is None and knobs["max_per_family"] and family:
        in_family = sum(1 for r in paper if family_of(r) == family)
        if in_family >= knobs["max_per_family"]:
            kind = _KIND_DIVERSITY
            reason = (
                f"Paper pool diversity cap: {in_family} '{family}' strategies already paper-trade "
                f"(max {knobs['max_per_family']} per family) — waiting for a paper slot"
            )
    if reason is None and cap:
        if record:
            # Rank among the waiting strategies, including this one's current score.
            _record(conn, strategy_id, _KIND_CAPACITY, score, market, family, "")
        position, waiting = _queue_position(conn, strategy_id, score)
        free = cap - len(paper)
        if free <= 0:
            reason = (
                f"Paper pool full: {len(paper)}/{cap} strategies in paper — waiting for a paper slot "
                f"(queue position {position} of {waiting}, ranked by recorded evidence)"
            )
        elif position > free:
            reason = (
                f"Paper pool: {free} free slot(s) go to higher-ranked waiting strategies first — "
                f"waiting for a paper slot (queue position {position} of {waiting})"
            )
    if record:
        if reason is None:
            conn.execute("DELETE FROM paper_queue WHERE strategy_id = ?", (strategy_id,))
        else:
            _record(conn, strategy_id, kind, score, market, family, reason)
    return reason


def queue_snapshot(conn, limit: int = 100) -> list[dict[str, Any]]:
    """Waiting strategies, best evidence first."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT q.* FROM paper_queue q JOIN strategies s ON s.id = q.strategy_id "
        "WHERE LOWER(TRIM(COALESCE(s.stage, ''))) = 'gauntlet' "
        "ORDER BY q.score DESC, q.queued_at ASC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def prune_queue(conn) -> int:
    """Drop queue entries whose strategy left the gauntlet or that went stale."""
    ensure_schema(conn)
    stale = (_now() - timedelta(days=_QUEUE_TTL_DAYS)).isoformat()
    cur = conn.execute(
        "DELETE FROM paper_queue WHERE strategy_id NOT IN "
        "(SELECT id FROM strategies WHERE LOWER(TRIM(COALESCE(stage, ''))) = 'gauntlet') "
        "OR datetime(updated_at) < datetime(?)",
        (stale,),
    )
    return int(cur.rowcount or 0)


def pool_summary(knobs: Mapping[str, Any], in_paper: int, queue: list[dict[str, Any]]) -> dict[str, Any]:
    """Occupancy, limits and the queue head, for the gate report and pipeline explain."""
    return {
        "in_paper": int(in_paper),
        "max_strategies": knobs["max_strategies"],
        "max_per_market": knobs["max_per_market"],
        "max_per_family": knobs["max_per_family"],
        "waiting": len(queue),
        "queue": queue,
        "retirement": {key: value for key, value in knobs.items() if key.startswith("retire_")},
    }


def parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _max_drawdown(pnls: list[float]) -> float:
    equity = peak = 1.0
    worst = 0.0
    for pnl in pnls:
        equity *= 1.0 + pnl
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, 1.0 - equity / peak)
    return worst


def retirement_reason(
    pnls: list[float],
    *,
    knobs: Mapping[str, Any],
    days_idle: float | None,
    has_open_trade: bool,
    sequential_state: str | None = None,
    sequential_reason: str = "",
) -> tuple[str, str] | None:
    """(code, detail) when a paper strategy's forward record retires it, else None. Pure."""
    n = len(pnls)
    if n >= knobs["retire_min_trades"] and n >= 2:
        avg = mean(pnls)
        sd = stdev(pnls)
        t_stat = avg / (sd / math.sqrt(n)) if sd > 0 else (-math.inf if avg < 0 else 0.0)
        if avg < 0 and t_stat < knobs["retire_max_t_stat"]:
            return "losing", f"{n} forward trades, mean {avg:+.4f}, t {t_stat:.2f} below {knobs['retire_max_t_stat']:.2f}"
    if knobs["retire_max_drawdown_pct"] > 0 and pnls:
        drawdown = _max_drawdown(pnls)
        if drawdown >= knobs["retire_max_drawdown_pct"]:
            return "drawdown", f"forward drawdown {drawdown:.1%} reached {knobs['retire_max_drawdown_pct']:.0%}"
    if knobs["retire_on_sequential_fail"] and sequential_state == "fail":
        return "sequential_fail", sequential_reason or "forward trades contradict the backtest"
    if knobs["retire_idle_days"] and not has_open_trade and days_idle is not None and days_idle >= knobs["retire_idle_days"]:
        return "idle", f"no trade for {int(days_idle)} days (limit {knobs['retire_idle_days']})"
    return None


def trade_activity(conn, strategy_id: str, since: str | None) -> tuple[bool, str | None]:
    """(has an open paper trade, latest paper trade timestamp since paper entry)."""
    params: list[object] = [strategy_id]
    where_since = ""
    if since:
        where_since = " AND datetime(COALESCE(closed_at, opened_at, created_at)) >= datetime(?)"
        params.append(since)
    row = conn.execute(
        "SELECT SUM(CASE WHEN UPPER(COALESCE(status, '')) = 'OPEN' THEN 1 ELSE 0 END) AS open_count, "
        "MAX(COALESCE(closed_at, opened_at, created_at)) AS last_at FROM trades "
        "WHERE COALESCE(strategy_id, strategy) = ? "
        "AND LOWER(COALESCE(execution_type, '')) LIKE 'paper%'" + where_since,
        tuple(params),
    ).fetchone()
    if row is None:
        return False, None
    return bool(row["open_count"] or 0), row["last_at"]


def readmit_candidates(conn, *, days: int = 30, limit: int = 100) -> list[dict[str, Any]]:
    """Recent archives whose last gauntlet->paper rejection the research door no longer blocks on.

    A report only: nothing is re-queued until the operator runs ``requeue``.
    """
    cutoff = (_now() - timedelta(days=int(days))).isoformat()
    rows = conn.execute(
        """
        SELECT s.id, s.symbol, s.timeframe, s.stage_changed_at, g.reason_code, g.reason_text
        FROM strategies s
        JOIN gate_rejections g ON g.id = (
            SELECT id FROM gate_rejections WHERE strategy_id = s.id AND gate = 'gauntlet'
            ORDER BY datetime(created_at) DESC, id DESC LIMIT 1
        )
        WHERE LOWER(TRIM(COALESCE(s.stage, ''))) = 'archived'
          AND datetime(s.stage_changed_at) >= datetime(?)
          AND COALESCE(s.status_reason, '') NOT LIKE 'untestable:%'
        ORDER BY datetime(s.stage_changed_at) DESC
        """,
        (cutoff,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        code = str(row["reason_code"] or "")
        text = str(row["reason_text"] or "").lower()
        if code in _RECORD_ONLY_REASON_CODES or any(marker in text for marker in _RECORD_ONLY_TEXT):
            out.append({
                "strategy_id": row["id"],
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "archived_at": row["stage_changed_at"],
                "last_rejection_code": code,
                "last_rejection": row["reason_text"],
            })
            if len(out) >= int(limit):
                break
    return out


# Breadth readings that show the rule working on several other coins: general
# (most other coins made money) or mixed (some did). home_only and too_few do not.
PORT_VERDICTS = frozenset({"general", "mixed"})
PORT_ACTOR = "paper_auto_port"
PORT_KV_PREFIX = "forven:paper_ports:"


def port_coins(breadth_state: Mapping[str, Any] | None, max_ports: int) -> list[str]:
    """Coins to port a paper strategy's frozen rule to, best Sharpe first.

    Needs a finished, current breadth result reading ``general`` or ``mixed``.
    A coin qualifies when the rule traded there and made money in the test; the
    home coin never does (the strategy already trades it).
    """
    state = breadth_state if isinstance(breadth_state, Mapping) else {}
    if state.get("status") != "done" or state.get("stale") or max_ports <= 0:
        return []
    summary = state.get("summary") if isinstance(state.get("summary"), Mapping) else {}
    if str(summary.get("verdict") or "") not in PORT_VERDICTS:
        return []
    home = str(state.get("home") or summary.get("home") or "").strip().upper()
    winners: list[tuple[float, str]] = []
    for row in state.get("rows") or []:
        if not isinstance(row, Mapping) or row.get("error"):
            continue
        asset = str(row.get("asset") or "").strip().upper()
        if not asset or asset == home:
            continue
        try:
            trades = int(row.get("trades") or 0)
            ret = float(row.get("return_pct"))
        except (TypeError, ValueError):
            continue
        if trades <= 0 or not math.isfinite(ret) or ret <= 0:
            continue
        try:
            sharpe = float(row.get("sharpe"))
            sharpe = sharpe if math.isfinite(sharpe) else -math.inf
        except (TypeError, ValueError):
            sharpe = -math.inf
        winners.append((sharpe, asset))
    winners.sort(key=lambda item: (-item[0], item[1]))
    return [asset for _sharpe, asset in winners][: int(max_ports)]
