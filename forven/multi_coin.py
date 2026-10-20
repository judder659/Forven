"""Multi-coin paper containers: one rule, several coins, one record (2026-10 gate plan, phase 5).

A paper strategy whose breadth test (``forven.breadth``) reads ``general`` trades
the same code and parameters on its home coin and on the other coins where the
rule made money in that test, up to ``multi_coin_max_coins``. The list is chosen
by the breadth test, never by hand, and is frozen the first time it is chosen
while the strategy is in paper: later breadth runs do not change it.

Every leg records its trades under the strategy's own id, so the paper record,
retirement and the live gate already see one pooled record. Coins move
together, so pooled trades are not independent: before any gate reads them,
trades closed on the same UTC day are combined into that day's return on the
whole strategy (``pool_by_day``). Ten correlated coins closing on one day count as one day of
evidence, not ten trades.

Capital is split equally: each leg sizes against ``1 / len(coins)`` of the
strategy's paper equity, so the container never risks more than the same
strategy on one coin, and per-leg margin is reserved per coin.

Paper only. The live order path is single-coin; going live with a container
needs that path reviewed for several coins per strategy (it touches
forven/exchange), so the live gate holds containers with ``multi_coin_live_review``.
See docs/multi-coin-live-followup.md.

A LEAF (no first-party imports): callers read and write the KV state.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

KV_PREFIX = "forven:multi_coin:"

DEFAULTS: dict[str, Any] = {
    "multi_coin_enabled": True,
    "multi_coin_max_coins": 6,
}

# Only a general breadth verdict (most other coins positive, median Sharpe above
# zero) is evidence the rule is not fitted to one coin.
ELIGIBLE_VERDICTS = frozenset({"general"})


def kv_key(strategy_id: str) -> str:
    return f"{KV_PREFIX}{strategy_id}"


def settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The multi-coin knobs from ``config['paper_trading']`` with defaults and bounds."""
    block = config.get("paper_trading") if isinstance(config, Mapping) else None
    block = block if isinstance(block, Mapping) else {}
    out = dict(DEFAULTS)
    enabled = block.get("multi_coin_enabled", DEFAULTS["multi_coin_enabled"])
    out["multi_coin_enabled"] = (
        enabled if isinstance(enabled, bool) else str(enabled).strip().lower() not in {"0", "false", "off", "no"}
    )
    door = config.get("paper_door") if isinstance(config, Mapping) else None
    if isinstance(door, Mapping) and str(door.get("mode") or "").strip().lower() == "strict":
        out["multi_coin_enabled"] = False  # the Strict paper preset is the pre-plan paper stage
    try:
        out["multi_coin_max_coins"] = max(2, min(15, int(block.get("multi_coin_max_coins", DEFAULTS["multi_coin_max_coins"]))))
    except (TypeError, ValueError):
        out["multi_coin_max_coins"] = DEFAULTS["multi_coin_max_coins"]
    return out


def choose_coins(breadth_state: Mapping[str, Any] | None, max_coins: int) -> list[str] | None:
    """The coin list a breadth result supports, home coin first, or None.

    Needs a finished, current result with a ``general`` verdict. The home coin
    always trades; the other coins are those that made money in the test (traded,
    positive return), best Sharpe first, up to ``max_coins`` in all. Fewer than two
    coins is no container.
    """
    state = breadth_state if isinstance(breadth_state, Mapping) else {}
    if state.get("status") != "done" or state.get("stale"):
        return None
    summary = state.get("summary") if isinstance(state.get("summary"), Mapping) else {}
    if str(summary.get("verdict") or "") not in ELIGIBLE_VERDICTS:
        return None
    home = str(state.get("home") or summary.get("home") or "").strip().upper()
    if not home:
        return None
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
    coins = [home, *[asset for _sharpe, asset in winners]][: max(int(max_coins), 1)]
    return coins if len(coins) >= 2 else None


def frozen_record(coins: list[str], breadth_state: Mapping[str, Any], frozen_at: str) -> dict[str, Any]:
    summary = breadth_state.get("summary") if isinstance(breadth_state.get("summary"), Mapping) else {}
    return {
        "coins": list(coins),
        "home": coins[0],
        "frozen_at": frozen_at,
        "source": "breadth",
        "breadth_verdict": summary.get("verdict"),
        "breadth_finished_at": breadth_state.get("finished_at"),
    }


def coins_from_record(record: Mapping[str, Any] | None, home: str | None) -> list[str] | None:
    """The frozen coin list, or None when there is none or it no longer fits the home coin."""
    if not isinstance(record, Mapping):
        return None
    coins = [str(c).strip().upper() for c in record.get("coins") or [] if str(c).strip()]
    if len(coins) < 2:
        return None
    if home and coins[0] != str(home).strip().upper():
        return None  # the strategy moved to another coin since the list was frozen
    return coins


def leg_weight(signal_data: Mapping[str, Any] | None) -> float:
    """The share of strategy equity a trade was sized on (1.0 for single-coin trades)."""
    if not isinstance(signal_data, Mapping):
        return 1.0
    try:
        weight = float(signal_data.get("container_weight", 1.0))
    except (TypeError, ValueError):
        return 1.0
    return weight if math.isfinite(weight) and 0 < weight <= 1 else 1.0


def pool_by_day(rows: Iterable[tuple[object, object, object]]) -> list[float]:
    """Container return per UTC day from ``(closed_at, pnl, weight)`` rows, oldest first.

    ``pnl`` is the trade's return on the equity it was sized on and ``weight`` that
    equity's share of the strategy (``leg_weight``), so a day's return on the whole
    strategy is the weighted sum of its trades. ``closed_at`` is an ISO timestamp;
    its first 10 characters name the day.
    """
    days: dict[str, float] = {}
    for closed_at, pnl, weight in rows:
        try:
            value = float(pnl) * float(weight)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        day = str(closed_at or "")[:10]
        days[day] = days.get(day, 0.0) + value
    return [days[day] for day in sorted(days)]


# --- What the UI shows (read side) ---------------------------------------------------
# The strategy row keeps its home symbol, so the UI reads the frozen list and each
# coin's share of the record from these helpers. Callers read the KV record.


def coin_key(value: object) -> str:
    """``UNI``, ``UNI/USDT``, ``UNI-USDT``, ``UNIUSDT`` and ``UNI/USDT:USDT`` all read as ``UNI``."""
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    for sep in ("/", "-", "_", ":"):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
            break
    for suffix in ("PERP", "USDT", "USDC", "USD"):
        if raw.endswith(suffix) and len(raw) > len(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw.strip()


def leg_symbol(coin: str, home_symbol: str) -> str:
    """The pair a leg trades: the coin against the home symbol's quote (``UNI/USDT``)."""
    home = str(home_symbol or "").strip().upper()
    quote = "USDT"
    for sep in ("/", "-"):
        if sep in home:
            quote = home.split(sep, 1)[1].split(":")[0] or quote
            break
    return f"{coin}/{quote}"


def trade_coin(trade: Mapping[str, Any], home_symbol: str) -> str:
    """The coin a trade row traded: its ``asset``, else the strategy's home coin."""
    return coin_key(trade.get("asset")) or coin_key(trade.get("symbol")) or coin_key(home_symbol)


def container_view(
    record: Mapping[str, Any] | None,
    symbol: str | None,
    config: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """What the UI shows of a strategy's frozen record (``kv_key``), or None for single-coin.

    ``trading`` is False when the record exists but multi-coin paper is switched
    off (Settings, or the Strict paper preset): the list is kept, only the home
    coin trades.
    """
    coins = coins_from_record(record, coin_key(symbol) or None)
    if not coins or not isinstance(record, Mapping):
        return None
    home_symbol = str(symbol or "").strip().upper()
    return {
        "coins": coins,
        "symbols": [leg_symbol(coin, home_symbol) for coin in coins],
        "home": coins[0],
        "frozen_at": str(record.get("frozen_at") or "") or None,
        "source": str(record.get("source") or "") or None,
        "breadth_verdict": record.get("breadth_verdict"),
        "breadth_finished_at": record.get("breadth_finished_at"),
        "weight": round(1.0 / len(coins), 6),
        "trading": bool(settings(config)["multi_coin_enabled"]),
    }


def _finite(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def coin_breakdown(
    closed: Iterable[Mapping[str, Any]],
    open_positions: Iterable[Mapping[str, Any]],
    coins: list[str] | None,
    home: str,
) -> list[dict[str, Any]]:
    """Each coin's forward record: container coins first (in list order), then any other coin traded.

    ``closed`` rows carry ``coin``, ``pnl`` (USD, may be None) and ``closed_at``;
    ``open_positions`` rows carry ``coin``, ``side`` and ``unrealized_pnl``.
    """
    order = [c for c in (coins or [home]) if c]
    rows: dict[str, dict[str, Any]] = {}

    def row(coin: str) -> dict[str, Any]:
        if coin not in rows:
            rows[coin] = {
                "coin": coin,
                "home": coin == home,
                "in_container": coin in order,
                "closed_count": 0,
                "wins": 0,
                "win_rate_pct": None,
                "realized_pnl_usd": 0.0,
                "open_count": 0,
                "open_sides": [],
                "unrealized_pnl_usd": 0.0,
                "last_closed_at": None,
            }
        return rows[coin]

    for coin in order:
        row(coin)
    for trade in closed:
        coin = str(trade.get("coin") or home)
        entry = row(coin)
        entry["closed_count"] += 1
        pnl = _finite(trade.get("pnl"))
        if pnl is not None:
            entry["realized_pnl_usd"] += pnl
            if pnl > 0:
                entry["wins"] += 1
        closed_at = str(trade.get("closed_at") or "") or None
        if closed_at and (entry["last_closed_at"] is None or closed_at > entry["last_closed_at"]):
            entry["last_closed_at"] = closed_at
    for position in open_positions:
        coin = str(position.get("coin") or home)
        entry = row(coin)
        entry["open_count"] += 1
        side = str(position.get("side") or "").strip().lower()
        if side and side not in entry["open_sides"]:
            entry["open_sides"].append(side)
        entry["unrealized_pnl_usd"] += _finite(position.get("unrealized_pnl")) or 0.0

    for entry in rows.values():
        if entry["closed_count"]:
            entry["win_rate_pct"] = round(entry["wins"] / entry["closed_count"] * 100.0, 4)
        entry["realized_pnl_usd"] = round(entry["realized_pnl_usd"], 4)
        entry["unrealized_pnl_usd"] = round(entry["unrealized_pnl_usd"], 4)

    rank = {coin: index for index, coin in enumerate(order)}
    return sorted(rows.values(), key=lambda e: (rank.get(e["coin"], len(rank)), e["coin"]))
