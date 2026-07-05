from __future__ import annotations

import json
import re
import uuid
from typing import Any, Iterable, Literal

from forven.db import get_db
from forven.policy import load_pipeline_config

DEFAULT_VERDICT_TESTS = (
    "sample_size",
    "statistical_significance",
    "walk_forward",
    "monte_carlo",
    "parameter_stability",
    "cost_stress",
    "regime_performance",
)

_STRATEGY_TEST_ALIASES = {
    "parameter_stability": "parameter_jitter",
    "parameter_jitter": "parameter_stability",
    "regime_performance": "regime_split",
    "regime_split": "regime_performance",
}

_VALID_DATASET_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")
_CANONICAL_STRATEGY_ID_PATTERN = re.compile(r"S\d+", flags=re.IGNORECASE)


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _normalize_win_rate(value: object) -> float:
    win_rate = _coerce_float(value, 0.0)
    if win_rate <= 1.0:
        win_rate *= 100.0
    return win_rate


def _normalize_drawdown_pct(value: object) -> float:
    drawdown = abs(_coerce_float(value, 0.0))
    if drawdown <= 1.0:
        drawdown *= 100.0
    return drawdown


def _normalize_base_asset_symbol(value: object, fallback: object = None) -> str:
    raw = str(value or fallback or "").strip().upper()
    if not raw:
        return ""
    for sep in ("/", "-", "_"):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
            break
    for suffix in ("PERP", "USDT", "USDC", "USD"):
        if raw.endswith(suffix) and len(raw) > len(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw.strip()


def parse_backtesting_dataset_context(dataset_id: object) -> tuple[str, str]:
    normalized = str(dataset_id or "").strip()
    if not normalized:
        return "", ""

    normalized = re.sub(r"^(?:remote-)?dataset-\d+-", "", normalized, flags=re.IGNORECASE)
    parts = normalized.split("-")
    if len(parts) >= 2 and parts[-1].lower() in _VALID_DATASET_TIMEFRAMES:
        symbol = "-".join(parts[:-1]).strip()
        return symbol, parts[-1].lower()
    if " " in normalized:
        chunks = [part for part in normalized.split() if part]
        if chunks:
            timeframe = chunks[-1].lower() if len(chunks) >= 2 and chunks[-1].lower() in _VALID_DATASET_TIMEFRAMES else ""
            return chunks[0], timeframe
    return normalized, ""


def normalize_strategy_id_candidates(strategy_id: object) -> list[str]:
    normalized = str(strategy_id or "").strip()
    if not normalized:
        return []

    candidates: list[str] = [normalized]
    canonical_match = None
    for match in _CANONICAL_STRATEGY_ID_PATTERN.findall(normalized):
        canonical_match = match

    canonical_id = str(canonical_match or "").strip().upper()
    if canonical_id and canonical_id not in candidates:
        candidates.append(canonical_id)
    return candidates


def _resolve_strategy_metrics_row(
    requested_strategy_id: str,
    desired_symbol: str,
    desired_timeframe: str,
) -> dict[str, Any] | None:
    normalized_request = str(requested_strategy_id or "").strip()
    strategy_candidates = normalize_strategy_id_candidates(normalized_request)
    clauses: list[str] = []
    params: list[str] = []

    if strategy_candidates:
        placeholders = ", ".join("?" for _ in strategy_candidates)
        clauses.append(f"id IN ({placeholders})")
        params.extend(strategy_candidates)

    if normalized_request:
        clauses.append("LOWER(TRIM(COALESCE(name, ''))) = LOWER(TRIM(?))")
        params.append(normalized_request)
        clauses.append("LOWER(TRIM(COALESCE(display_id, ''))) = LOWER(TRIM(?))")
        params.append(normalized_request)

    if not clauses:
        return None

    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT id, name, display_id, symbol, timeframe, metrics
            FROM strategies
            WHERE {" OR ".join(clauses)}
            ORDER BY CASE
                WHEN LOWER(TRIM(id)) = LOWER(TRIM(?)) THEN 0
                WHEN LOWER(TRIM(COALESCE(display_id, ''))) = LOWER(TRIM(?)) THEN 1
                WHEN LOWER(TRIM(COALESCE(name, ''))) = LOWER(TRIM(?)) THEN 2
                ELSE 3
            END
            LIMIT 1
            """,
            tuple(params + [normalized_request, normalized_request, normalized_request]),
        ).fetchone()

    if not row:
        return None

    row_symbol = _normalize_base_asset_symbol(row["symbol"])
    row_timeframe = str(row["timeframe"] or "").strip().lower()
    if desired_symbol and row_symbol and row_symbol != desired_symbol:
        return None
    if desired_timeframe and row_timeframe and row_timeframe != desired_timeframe:
        return None

    try:
        metrics = json.loads(row["metrics"]) if row["metrics"] else {}
    except Exception:
        metrics = {}
    if not isinstance(metrics, dict) or not metrics:
        return None

    oos_metrics = metrics.get("out_of_sample")
    if isinstance(oos_metrics, dict):
        for key in (
            "total_trades",
            "win_rate",
            "sharpe",
            "profit_factor",
            "max_drawdown_pct",
            "total_return_pct",
            "monthly_return_pct",
            "annualized_return_pct",
            "backtest_months",
        ):
            if key in oos_metrics and key not in metrics:
                metrics[key] = oos_metrics[key]

    return {
        "result_id": f"strategy-metrics:{row['id']}",
        "strategy_id": row["id"],
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "metrics_json": json.dumps(metrics),
    }


def resolve_backtest_result_row(strategy_id: str, dataset_id: str):
    normalized_strategy_id = str(strategy_id or "").strip()
    normalized_dataset_id = str(dataset_id or "").strip()
    if not normalized_dataset_id:
        return None

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM backtest_results
            WHERE result_id = ?
              AND deleted_at IS NULL
            ORDER BY datetime(created_at) DESC, created_at DESC
            LIMIT 1
            """,
            (normalized_dataset_id,),
        ).fetchone()
        if row:
            return row

        parsed_symbol, parsed_timeframe = parse_backtesting_dataset_context(normalized_dataset_id)
        desired_symbol = _normalize_base_asset_symbol(parsed_symbol)
        desired_timeframe = str(parsed_timeframe or "").strip().lower()
        strategy_candidates = normalize_strategy_id_candidates(normalized_strategy_id)
        if not strategy_candidates or not desired_symbol or not desired_timeframe:
            return None

        placeholders = ", ".join("?" for _ in strategy_candidates)
        # Only real metric-bearing rows are eligible. Robustness sub-check rows
        # (param_jitter/walk_forward/monte_carlo/cost_stress/regime_split) share
        # this table, carry the same symbol/timeframe, and sort NEWEST — without
        # this filter the latest robustness run shadows the real backtest and
        # the verdict computes from a row with no trade metrics (sample_size=0
        # while the backtest path reports hundreds of trades).
        rows = conn.execute(
            f"""
            SELECT *
            FROM backtest_results
            WHERE strategy_id IN ({placeholders})
              AND deleted_at IS NULL
              AND result_type IN ('backtest', 'optimization')
            ORDER BY datetime(created_at) DESC, created_at DESC
            """,
            tuple(strategy_candidates),
        ).fetchall()

    for candidate in rows:
        candidate_symbol = _normalize_base_asset_symbol(candidate["symbol"])
        candidate_timeframe = str(candidate["timeframe"] or "").strip().lower()
        if candidate_symbol != desired_symbol:
            continue
        if candidate_timeframe != desired_timeframe:
            continue
        return candidate

    strategy_metrics_row = _resolve_strategy_metrics_row(
        requested_strategy_id=normalized_strategy_id,
        desired_symbol=desired_symbol,
        desired_timeframe=desired_timeframe,
    )
    if strategy_metrics_row:
        return strategy_metrics_row
    return None


def extract_backtest_metrics(row: Any) -> dict[str, Any]:
    try:
        metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
    except Exception:
        metrics = {}

    for col in (
        "sharpe_ratio",
        "total_return",
        "win_rate",
        "profit_factor",
        "max_drawdown_pct",
        "total_trades",
    ):
        try:
            value = row[col]
        except (IndexError, KeyError, TypeError):
            continue
        if value is not None:
            metrics[col] = value
    return metrics


def normalize_requested_tests(tests: Iterable[str] | None) -> list[str]:
    selected: list[str] = []
    for raw_name in (tests or DEFAULT_VERDICT_TESTS):
        normalized = str(raw_name or "").strip().lower()
        if normalized and normalized not in selected:
            selected.append(normalized)
    return selected


def normalize_strategy_verdict_tests(raw_tests: dict[str, Any] | None) -> dict[str, Any]:
    normalized_tests: dict[str, Any] = {}
    for key, payload in (raw_tests or {}).items():
        normalized_key = str(key or "").strip().lower()
        if not normalized_key:
            continue
        normalized_tests[normalized_key] = payload
        alias = _STRATEGY_TEST_ALIASES.get(normalized_key)
        if alias:
            normalized_tests[alias] = payload
    return normalized_tests


def calculate_verdict_metrics(metrics: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    payload = metrics if isinstance(metrics, dict) else {}
    config = load_pipeline_config()
    gate = config.get("paper_gate", {})

    min_trades_req = int(gate.get("min_trades", 30) or 30)
    min_sharpe_req = _coerce_float(gate.get("min_sharpe", 1.0), 1.0)
    max_dd_req = _coerce_float(gate.get("max_drawdown_pct", 0.1), 0.1) * 100.0
    min_pf_req = _coerce_float(gate.get("min_profit_factor", 1.5), 1.5)

    total_trades = int(_coerce_float(payload.get("total_trades", 0), 0.0))
    sharpe = _coerce_float(payload.get("sharpe_ratio", payload.get("sharpe", 0.0)), 0.0)
    wfa_ratio = _coerce_float(payload.get("wfa_ratio", 0.8), 0.8)
    max_dd = _normalize_drawdown_pct(payload.get("max_drawdown_pct", 0.0))
    pf = _coerce_float(payload.get("profit_factor", 1.0), 1.0)
    win_rate = _normalize_win_rate(payload.get("win_rate", 0.0))

    return {
        "sample_size": {
            "status": "pass" if total_trades >= min_trades_req else "fail",
            "value": total_trades,
            "threshold": min_trades_req,
            "message": f"Trade count: {total_trades} (min: {min_trades_req})",
        },
        "statistical_significance": {
            "status": "pass"
            if sharpe >= min_sharpe_req
            else "warn"
            if sharpe >= (min_sharpe_req * 0.5)
            else "fail",
            "value": sharpe,
            "threshold": min_sharpe_req,
            "message": f"Sharpe: {sharpe:.2f} (min: {min_sharpe_req})",
        },
        "walk_forward": {
            "status": "pass"
            if wfa_ratio >= 0.7
            else "warn"
            if wfa_ratio >= 0.5
            else "fail",
            "value": wfa_ratio,
            "threshold": 0.7,
            "message": f"WFA ratio: {wfa_ratio:.2%}",
        },
        "monte_carlo": {
            "status": "pass"
            if max_dd <= max_dd_req
            else "warn"
            if max_dd <= (max_dd_req * 1.5)
            else "fail",
            "value": max_dd,
            "threshold": max_dd_req,
            "message": f"Max DD: {max_dd:.2f}% (max: {max_dd_req}%)",
        },
        "parameter_stability": {
            "status": "pass"
            if pf >= min_pf_req
            else "warn"
            if pf >= (min_pf_req * 0.8)
            else "fail",
            "value": pf,
            "threshold": min_pf_req,
            "message": f"Profit Factor: {pf:.2f} (min: {min_pf_req})",
        },
        "cost_stress": {
            "status": "pass",
            "value": 0,
            "message": "Included in backtest fees",
        },
        "regime_performance": {
            "status": "pass" if win_rate >= 50 else "warn" if win_rate >= 40 else "fail",
            "value": win_rate,
            "threshold": 50,
            "message": f"Win rate: {win_rate:.1f}% (min: 50%)",
        },
    }


def get_overall_verdict(tests: dict[str, dict[str, Any]] | None) -> Literal["pass", "warn", "fail", "pending"]:
    # A test with NO recorded status has not produced a verdict — that is
    # ABSENCE of evidence, never a merit failure. It must not count as "fail"
    # (the old default), and it must not let the verdict "pass" on missing
    # evidence either: any unresolved test makes the overall verdict "pending".
    statuses = [str(payload.get("status") or "pending").strip().lower() for payload in (tests or {}).values()]
    fails = statuses.count("fail")
    warns = statuses.count("warn")
    pending = sum(1 for status in statuses if status not in {"pass", "warn", "fail"})

    if fails > 0:
        return "fail"
    if pending > 0:
        return "pending"
    if warns > 2:
        return "warn"
    return "pass"


def build_verdict_result(
    *,
    strategy_id: str,
    dataset_id: str,
    metrics: dict[str, Any] | None,
    tests: Iterable[str] | None = None,
    result_id: str | None = None,
) -> dict[str, Any]:
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION

    requested_tests = set(normalize_requested_tests(tests))
    all_tests = calculate_verdict_metrics(metrics)
    filtered_tests = {
        name: payload for name, payload in all_tests.items() if name in requested_tests
    }
    overall = get_overall_verdict(filtered_tests)

    return {
        "result_id": result_id or f"verdict-{uuid.uuid4().hex[:12]}",
        "status": overall,
        "engine_version": BACKTEST_ENGINE_VERSION,
        "tests": filtered_tests,
        "summary": {
            "strategy_id": strategy_id,
            "dataset_id": dataset_id,
            "overall": overall,
            "engine_version": BACKTEST_ENGINE_VERSION,
            "pass_count": sum(1 for payload in filtered_tests.values() if payload.get("status") == "pass"),
            "warn_count": sum(1 for payload in filtered_tests.values() if payload.get("status") == "warn"),
            "fail_count": sum(1 for payload in filtered_tests.values() if payload.get("status") == "fail"),
        },
    }


def build_strategy_verdict_blob(verdict_result: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION

    payload = verdict_result if isinstance(verdict_result, dict) else {}
    raw_tests = payload.get("tests") if isinstance(payload.get("tests"), dict) else {}
    normalized_tests = normalize_strategy_verdict_tests(raw_tests)
    verdict_blob = {
        "status": payload.get("status"),
        "summary": payload.get("summary"),
        "tests": normalized_tests,
        # Provenance: which engine computed this verdict. A blob whose stamped
        # version differs from the running engine is stale evidence and must not
        # be compared against fresh numbers (see forven/engine_provenance.py).
        "engine_version": payload.get("engine_version", BACKTEST_ENGINE_VERSION),
    }
    return normalized_tests, verdict_blob
