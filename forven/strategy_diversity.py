from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Iterable

from forven.db import get_db


DEFAULT_RECENT_LIMIT = 80
DEFAULT_SATURATION_THRESHOLD = 0.35
DEFAULT_HARD_SATURATION_THRESHOLD = 0.55
DEFAULT_OUTCOME_WINDOW_DAYS = 90
DEAD_FAMILY_MIN_ATTEMPTS = 8

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

def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """Compile a family pattern with token boundaries at its alphanumeric edges.

    Bare substring matching classified any text containing "pe-rsi-stence",
    "reve-rsi-on" or "dive-rsi-fied" as the rsi family (checked FIRST, so it
    swallowed other families' hypotheses — 80% of the rsi graveyard was such
    false positives). Normalization maps punctuation to "_", so "_", space, "-"
    and "%" all count as separators: "rsi_period"/"RSI(14)" still match, while
    "persistence" no longer does. Edge-only anchoring keeps prefix patterns like
    "bb_" matching "bb_width".
    """
    prefix = r"(?<![a-z0-9])" if re.match(r"[a-z0-9]", pattern) else ""
    suffix = r"(?![a-z0-9])" if re.search(r"[a-z0-9]$", pattern) else ""
    return re.compile(prefix + re.escape(pattern) + suffix)


_FAMILY_PATTERN_REGEXES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (family, tuple(_pattern_regex(p) for p in patterns)) for family, patterns in FAMILY_PATTERNS
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
    for family, regexes in _FAMILY_PATTERN_REGEXES:
        if any(rx.search(normalized) for rx in regexes):
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


def family_outcome_stats(days: int = DEFAULT_OUTCOME_WINDOW_DAYS) -> dict[str, dict[str, int]]:
    """Per-family generation outcomes over a recent window.

    attempts = strategies created in the window; survivors = those whose closed
    paper trades made money. Reaching paper is not survival: gate passes did
    not predict forward results (68 of 83 strategies that traded in paper lost
    money by Sept 2026), so steering by them taught agents to fit the gates.

    Untestable archives (broken code, missing data, never trading) are not
    attempts: they say nothing about the family, and counting them punished
    families the generator merely implemented badly.
    """
    window = -abs(int(days or DEFAULT_OUTCOME_WINDOW_DAYS))
    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.display_id, s.name, s.type, s.runtime_type,
                       s.params, s.metrics, s.notes,
                       CASE WHEN COALESCE(p.paper_pnl, 0) > 0 THEN 1 ELSE 0 END AS survived
                FROM strategies s
                LEFT JOIN (
                    SELECT strategy_id, SUM(net_pnl_pct) AS paper_pnl
                    FROM trades
                    WHERE status = 'CLOSED' AND execution_type = 'paper' AND source = 'paper'
                      AND net_pnl_pct IS NOT NULL
                    GROUP BY strategy_id
                ) p ON p.strategy_id = s.id
                WHERE datetime(COALESCE(s.created_at, '1970-01-01T00:00:00+00:00'))
                      > datetime('now', ? || ' days')
                  AND LOWER(TRIM(COALESCE(s.status_reason, ''))) NOT LIKE 'untestable:%'
                """,
                (str(window),),
            ).fetchall()
    except Exception:
        return {}

    stats: dict[str, dict[str, int]] = {}
    for row in rows:
        family = _row_family(row)
        entry = stats.setdefault(family, {"attempts": 0, "survivors": 0})
        entry["attempts"] += 1
        if row["survived"]:
            entry["survivors"] += 1
    return stats


def render_strategy_diversity_guard(
    *,
    task_description: str = "",
    limit: int = DEFAULT_RECENT_LIMIT,
    threshold: float = DEFAULT_SATURATION_THRESHOLD,
    outcome_window_days: int = DEFAULT_OUTCOME_WINDOW_DAYS,
) -> str:
    saturated = saturated_strategy_families(limit=limit, threshold=threshold)
    outcomes = family_outcome_stats(days=outcome_window_days)
    dead = sorted(
        (
            (family, stats)
            for family, stats in outcomes.items()
            if family != "other"
            and stats["attempts"] >= DEAD_FAMILY_MIN_ATTEMPTS
            and stats["survivors"] == 0
        ),
        key=lambda item: item[1]["attempts"],
        reverse=True,
    )
    alive = sorted(
        ((family, stats) for family, stats in outcomes.items() if stats["survivors"] > 0),
        key=lambda item: item[1]["survivors"],
        reverse=True,
    )
    if not saturated and not dead and not alive:
        return ""

    lines = ["# STRATEGY DIVERSITY GUARD"]
    if saturated:
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

    # Outcome steering: frequency alone can't distinguish an over-mined dead
    # region from a productive one, so surface where recent attempts actually
    # made money in paper vs where nothing has.
    if dead:
        lines.append(
            f"Dead regions (last {int(outcome_window_days)}d, ≥{DEAD_FAMILY_MIN_ATTEMPTS} attempts, none made money in paper):"
        )
        for family, stats in dead[:4]:
            label = FAMILY_LABELS.get(family, family.replace("_", " "))
            lines.append(
                f"- {label}: {stats['attempts']} candidates, 0 survivors. "
                "Do not propose more of these without a structurally different mechanism."
            )
    if alive:
        parts = [
            f"{FAMILY_LABELS.get(family, family.replace('_', ' '))} ({stats['survivors']}/{stats['attempts']} made money in paper)"
            for family, stats in alive[:4]
        ]
        lines.append("Families with strategies that made money in paper: " + ", ".join(parts) + ".")

    if _normalize_text(task_description):
        lines.append(f"- Apply this guard while working on: {_normalize_text(task_description)[:240]}")

    return "\n".join(lines)


def render_failure_taxonomy(*, days: int = 30, limit: int = 8) -> str:
    """Render the structured gate-rejection taxonomy for generation steering.

    Surfaces the top (family × gate × reason × regime) rejection clusters from
    `gate_rejections` so ideation designs away from regions the pipeline has
    already rejected instead of re-mining them.
    """
    try:
        from forven.db import query_failure_taxonomy

        rows = query_failure_taxonomy(days=days)
    except Exception:
        return ""
    if not rows:
        return ""

    lines = [
        f"# FAILURE TAXONOMY (last {int(days)}d)",
        "Top structured rejection patterns from the promotion gates. Treat each as a disproven region: "
        "do not re-propose the same family/mechanism into the same failure mode without explicitly addressing it.",
    ]
    for row in rows[:limit]:
        gate = _normalize_text(row.get("gate")) or "?"
        reason = _normalize_text(row.get("reason_code")) or "unspecified"
        strategy_type = _normalize_text(row.get("strategy_type")) or "unknown family"
        regime = _normalize_text(row.get("regime_context")) or "any regime"
        try:
            count = int(row.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        lines.append(f"- {strategy_type} @ {gate}: {reason} ×{count} ({regime})")
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
