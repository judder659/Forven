from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Iterable

from forven.db import get_db


DEFAULT_RECENT_LIMIT = 80
DEFAULT_SATURATION_THRESHOLD = 0.35
DEFAULT_HARD_SATURATION_THRESHOLD = 0.55

FAMILY_LABELS = {
    "rsi": "RSI / oscillator momentum",
    "stochastic": "stochastic oscillator",
    "williams_r": "Williams %R oscillator",
    "macd": "MACD momentum",
    "ema": "EMA trend",
    "bollinger": "Bollinger / band mean reversion",
    "donchian": "Donchian breakout",
    "keltner": "Keltner channel",
    "vwap": "VWAP execution/mean reversion",
    "supertrend": "Supertrend",
    "adx": "ADX trend strength",
    "orb": "opening range breakout",
    "funding": "funding/carry",
    "volume": "volume/order-flow",
    "cross_asset": "cross-asset/relative value",
    "other": "other",
}

FAMILY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rsi", ("rsi", "connors")),
    ("stochastic", ("stochastic", "stoch", "kdj")),
    ("williams_r", ("williams_r", "williams-r", "williams %r", "williams")),
    ("macd", ("macd", "ppo", "trix")),
    ("ema", ("ema", "dema", "tema", "moving_average", "moving average")),
    ("bollinger", ("bollinger", "bb_", "band_reversion", "mean_reversion", "zscore")),
    ("donchian", ("donchian",)),
    ("keltner", ("keltner",)),
    ("vwap", ("vwap",)),
    ("supertrend", ("supertrend",)),
    ("adx", ("adx", "aroon")),
    ("orb", ("orb", "opening_range", "opening range")),
    ("funding", ("funding", "basis", "carry", "perp")),
    ("volume", ("volume", "obv", "mfi", "chaikin", "adl", "taker", "liquidation")),
    ("cross_asset", ("cross_asset", "cross-asset", "dominance", "relative_value", "relative value", "rotation")),
)

def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _flatten_payload(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except Exception:
        return str(value)


def infer_strategy_family(*values: Any) -> str:
    text = " ".join(_flatten_payload(value) for value in values)
    normalized = re.sub(r"[^a-z0-9_% -]+", "_", text.lower())
    for family, patterns in FAMILY_PATTERNS:
        if any(pattern in normalized for pattern in patterns):
            return family
    return "other"


def _row_family(row: Any) -> str:
    if hasattr(row, "get"):
        getter = row.get
    else:
        keys = set(row.keys())

        def getter(key: str, default: Any = None) -> Any:
            return row[key] if key in keys else default

    return infer_strategy_family(
        getter("type"),
        getter("runtime_type"),
        getter("name"),
        getter("display_id"),
        getter("id"),
        getter("params"),
        getter("metrics"),
        getter("notes"),
    )


def recent_strategy_family_counts(limit: int = DEFAULT_RECENT_LIMIT) -> dict[str, Any]:
    normalized_limit = max(1, min(int(limit or DEFAULT_RECENT_LIMIT), 500))
    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT id, display_id, name, type, runtime_type, params, metrics, notes, created_at, updated_at
                FROM strategies
                ORDER BY datetime(COALESCE(updated_at, created_at, '1970-01-01T00:00:00+00:00')) DESC, id DESC
                LIMIT ?
                """,
                (normalized_limit,),
            ).fetchall()
    except Exception:
        return {"total": 0, "counts": {}, "shares": {}, "top_family": None}

    counts = Counter(_row_family(row) for row in rows)
    total = sum(counts.values())
    shares = {family: count / total for family, count in counts.items()} if total else {}
    top_family = counts.most_common(1)[0][0] if counts else None
    return {
        "total": total,
        "counts": dict(counts),
        "shares": shares,
        "top_family": top_family,
    }


def saturated_strategy_families(
    *,
    limit: int = DEFAULT_RECENT_LIMIT,
    threshold: float = DEFAULT_SATURATION_THRESHOLD,
) -> list[dict[str, Any]]:
    stats = recent_strategy_family_counts(limit=limit)
    total = int(stats.get("total") or 0)
    if total <= 0:
        return []
    counts = stats.get("counts") if isinstance(stats.get("counts"), dict) else {}
    shares = stats.get("shares") if isinstance(stats.get("shares"), dict) else {}
    saturated: list[dict[str, Any]] = []
    for family, count in counts.items():
        share = float(shares.get(family) or 0.0)
        if share >= threshold:
            saturated.append(
                {
                    "family": family,
                    "label": FAMILY_LABELS.get(family, family.replace("_", " ")),
                    "count": int(count),
                    "share": share,
                    "total": total,
                    "severity": "hard" if share >= DEFAULT_HARD_SATURATION_THRESHOLD else "soft",
                }
            )
    saturated.sort(key=lambda item: (item["share"], item["count"]), reverse=True)
    return saturated


def render_strategy_diversity_guard(
    *,
    task_description: str = "",
    limit: int = DEFAULT_RECENT_LIMIT,
    threshold: float = DEFAULT_SATURATION_THRESHOLD,
) -> str:
    saturated = saturated_strategy_families(limit=limit, threshold=threshold)
    if not saturated:
        return ""

    lines = ["# STRATEGY DIVERSITY GUARD"]
    lines.append(
        "Recent strategy memory is family-skewed. Treat saturated families as overrepresented prior art, not inspiration."
    )
    for item in saturated[:4]:
        pct = round(float(item["share"]) * 100)
        lines.append(f"- {item['label']}: {item['count']}/{item['total']} recent strategies ({pct}%).")

    # Family-agnostic guidance: steer away from whichever families are saturated on
    # THIS instance. (The old RSI-specific carve-out was a fossil from a past RSI
    # flood and unfairly singled out one family — removed so every family is treated
    # the same, driven purely by this instance's own saturation.)
    labels = [str(item["label"]) for item in saturated[:3]]
    lines.append("- Prefer families outside the saturated set: " + ", ".join(labels) + ".")

    if _normalize_text(task_description):
        lines.append(f"- Apply this guard while working on: {_normalize_text(task_description)[:240]}")

    return "\n".join(lines)


def filter_recall_records_for_diversity(records: Iterable[dict[str, Any]], *, max_family_share: float = 0.4) -> list[dict[str, Any]]:
    """Limit overrepresented families in retrieved examples.

    This is intentionally generic: callers can pass Chroma flattened records and
    get back a list where no family dominates the examples shown to an agent.
    """
    output: list[dict[str, Any]] = []
    family_counts: Counter[str] = Counter()
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        family = infer_strategy_family(record.get("document"), metadata)
        projected_total = len(output) + 1
        projected_share = (family_counts[family] + 1) / projected_total
        if projected_total > 3 and projected_share > max_family_share:
            continue
        output.append(record)
        family_counts[family] += 1
    return output
