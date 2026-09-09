"""Backtest result persistence, artifact I/O and the API view-model normalizers.

ARCH-06 step 4: moved VERBATIM out of ``forven.api_core``. What lives here is
the layer between the ``backtest_results`` table plus the on-disk result
artifacts and the JSON the API hands the UI — the row writers, the artifact
readers/writers, the trash table, and the summary/detail/chart normalizers.

What deliberately did NOT move: the three submission endpoints
(``post_backtest_submit``, ``post_optimization_submit``, ``post_backtesting_run``)
and ``_persist_completed_backtest_run``. See the ARCH-06 step 4 note in
``forven.api_core`` for why.

THE SEAM RULE — read this before adding a call to this module
-------------------------------------------------------------
``forven.api_core`` re-exports every name defined here, and the test suite
monkeypatches several of them ON ``forven.api_core``: ``_result_data_dirs`` and
``_ensure_result_data_dir`` (tests/test_backtest_chart_context.py),
``_write_backtest_result_artifacts`` (tests/test_backtesting_type_inference.py),
``_persist_backtest_result_row`` and ``_build_backtest_chart_context_payload``
(tests/test_manual_backtest_api_wiring.py), plus ``get_db`` and ``_now``.

A patch rebinds the ATTRIBUTE on ``forven.api_core``. Code in this module that
called such a name directly would resolve it in THIS module's globals instead
and silently ignore the patch — the test would keep passing while testing
nothing, which is exactly the failure mode that made this extraction get
deferred once already. So every dependency the suite patches is reached through
``core.<name>``: a live lookup into ``forven.api_core``'s module globals, i.e.
the very object a monkeypatch rebinds. (Importing api_core at module scope is
impossible here anyway — api_core imports THIS module to build its shim.)

``tests/test_finish_api_core.py`` enforces the rule mechanically: it scans the
suite for the api_core attributes it patches and fails if any of them is called
from this module by its bare module-local name.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from forven.config import FORVEN_HOME

log = logging.getLogger("forven.api")

_API_CORE = None


class _ApiCoreProxy:
    """Late-bound view of ``forven.api_core``'s module globals.

    Every attribute access is a fresh ``getattr`` on the live module object, so
    a ``monkeypatch.setattr(api_core, name, ...)`` is honoured by the callers
    below. See THE SEAM RULE in the module docstring — this indirection is the
    entire safety argument for the split, not a style choice.
    """

    __slots__ = ()

    def __getattr__(self, name: str):
        global _API_CORE
        if _API_CORE is None:
            from forven import api_core

            _API_CORE = api_core
        return getattr(_API_CORE, name)


core = _ApiCoreProxy()


def _backtest_trash_table(conn):
    """Ensure the backtest trash table exists."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS backtest_result_trash (
            result_id TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL
        )
    """
        )


def _coerce_legacy_metadata_float(value, default: float | None = 0.0) -> float | None:
    """Safely parse common float-like values from legacy metadata.

    API-03/ARCH-02: this used to be called `_coerce_float`, shadowing the strict
    helper defined near the settings writers. Its salvage heuristics ("52.1%",
    "-0.536 to -1.463", thousands separators) are right for archived backtest
    metadata and WRONG for operator-typed risk limits, so it now carries a name
    that says where it belongs. Only the backtest summary/detail normalizers
    below this point may use it.
    """
    fallback_none = default is None
    if value is None:
        return None if fallback_none else float(default)
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None if fallback_none else float(default)

    cleaned = value.strip()
    if not cleaned:
        return float(default)

    # Legacy format examples: "-0.536 to -1.463"
    if " to " in cleaned:
        parts = [p.strip() for p in cleaned.split(" to ") if p.strip()]
        if len(parts) >= 2:
            import re
            nums = []
            for part in parts[:2]:
                m = re.search(r"-?\d+(?:\.\d+)?", part.replace(",", ""))
                if m:
                    try:
                        nums.append(float(m.group(0)))
                    except Exception:
                        pass
            if len(nums) == 2:
                return (nums[0] + nums[1]) / 2.0

    # Percentage strings: "52.1%" -> 52.1
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]

    try:
        return float(cleaned.replace(",", ""))
    except Exception:
        import re
        m = re.search(r"-?\d+(?:\.\d+)?", cleaned.replace(",", ""))
        if m:
            try:
                return float(m.group(0))
            except Exception:
                return None if fallback_none else float(default)
    return None if fallback_none else float(default)


def _record_backtest_sort_time(rec: dict) -> str:
    # Use an old sentinel instead of "now" so records without timestamps
    # do not incorrectly float to the top of history.
    return str(rec.get("metadata", {}).get("recorded_at", "1970-01-01T00:00:00+00:00"))


def _parse_json_blob(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default
    text = value.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _extract_result_type(result_id: str, meta: dict) -> str:
    raw = str((meta or {}).get("result_type") or "").strip().lower()
    if raw in {"backtest", "optimization", "walk_forward", "grid_search"}:
        return raw
    rid = str(result_id or "").strip().lower()
    if rid.startswith("opt_") or rid.startswith("optimization_"):
        return "optimization"
    if rid.startswith("wf_") or rid.startswith("walk_forward_"):
        return "walk_forward"
    if rid.startswith("gs_") or rid.startswith("grid_"):
        return "grid_search"
    return "backtest"


def _result_data_dirs() -> list[str]:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    candidates = [
        os.path.join(repo_root, "data", "results"),
        os.path.join(str(FORVEN_HOME), "data", "results"),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        normalized = os.path.abspath(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.isdir(normalized):
            out.append(normalized)
    return out


def _normalize_equity_points(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    points: list[dict] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        ts = row.get("timestamp") or row.get("time") or row.get("date") or row.get("ts")
        eq = row.get("equity")
        if eq is None:
            eq = row.get("value")
        if eq is None:
            eq = row.get("balance")
        if ts in (None, "") or eq in (None, ""):
            continue
        points.append({"timestamp": str(ts), "equity": _coerce_legacy_metadata_float(eq)})

    # Keep payload size bounded for very dense curves.
    max_points = 5000
    if len(points) > max_points:
        step = max(1, len(points) // max_points)
        reduced = points[::step]
        if reduced[-1]["timestamp"] != points[-1]["timestamp"]:
            reduced.append(points[-1])
        points = reduced
    return points


def _normalize_trade_rows(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    trades: list[dict] = []
    # Compounded $10k equity for deriving dollar PnL from ratio returns.
    # Matches TradingView's default initial_capital=10000 + percent_of_equity=100.
    equity = _BACKTEST_DISPLAY_EQUITY
    for row in value:
        if not isinstance(row, dict):
            continue
        entry_time = row.get("entry_time") or row.get("opened_at") or row.get("open_time")
        exit_time = row.get("exit_time") or row.get("closed_at") or row.get("close_time") or entry_time
        entry_price = row.get("entry_price")
        if entry_price is None:
            entry_price = row.get("entry")
        exit_price = row.get("exit_price")
        if exit_price is None:
            exit_price = row.get("exit")
        if entry_time in (None, "") or entry_price in (None, ""):
            continue
        if exit_time in (None, ""):
            exit_time = entry_time
        if exit_price in (None, ""):
            exit_price = entry_price

        ep = _coerce_legacy_metadata_float(entry_price)
        xp = _coerce_legacy_metadata_float(exit_price)
        raw_pnl = row.get("pnl", row.get("pnl_usd", None))
        stored_pnl = _coerce_legacy_metadata_float(raw_pnl, 0.0)
        raw_pnl_pct = row.get("pnl_pct")
        raw_return_pct = row.get("return_pct")
        raw_return = row.get("return")
        stored_return_pct = _coerce_legacy_metadata_float(raw_return_pct, 0.0)

        # Pull the portfolio-return ratio (decimal form, e.g. 0.0132 = +1.32%).
        # Engine rows use `pnl_pct` as a ratio. Persisted artifact rows use
        # `return_pct` as percent points. A legacy bug wrote both `pnl` and
        # `return_pct` as the same tiny ratio; only that shape should be scaled
        # up to percent points on read. Small percent-point wins/losses like
        # 0.229% are common and must not become 22.9%.
        stored_pnl_equals_return_pct = (
            raw_pnl not in (None, "")
            and raw_return_pct not in (None, "")
            and stored_return_pct != 0.0
            and abs(stored_return_pct) < 1.0
            and abs(stored_pnl - stored_return_pct) < 1e-6
        )
        if raw_pnl_pct not in (None, ""):
            ratio = _coerce_legacy_metadata_float(raw_pnl_pct, 0.0)
            display_return_pct = ratio * 100.0
        elif raw_return_pct not in (None, ""):
            if stored_pnl_equals_return_pct:
                ratio = stored_return_pct
                display_return_pct = ratio * 100.0
            else:
                display_return_pct = stored_return_pct
                ratio = display_return_pct / 100.0
        elif raw_return not in (None, ""):
            ratio = _coerce_legacy_metadata_float(raw_return, 0.0)
            display_return_pct = ratio * 100.0
        else:
            ratio = 0.0

        if ratio == 0.0 and ep and xp and ep != 0:
            ratio = (xp - ep) / ep
            display_return_pct = ratio * 100.0

        # Legacy artifacts wrote `pnl` = `return_pct` = the raw ratio (the bug
        # that prompted this normalizer). Detect that shape — pnl nearly equal
        # to the ratio AND tiny in dollar terms — and recompute from equity.
        stored_pnl_is_ratio_bug = (
            abs(stored_pnl - ratio) < 1e-6
            and abs(stored_pnl) < 1.0
            and ratio != 0.0
        )
        if stored_pnl == 0.0 or stored_pnl_is_ratio_bug:
            dollar_pnl = equity * ratio
        else:
            dollar_pnl = stored_pnl

        trade = {
            "entry_time": str(entry_time),
            "entry_price": ep,
            "exit_time": str(exit_time),
            "exit_price": xp,
            "size": _coerce_legacy_metadata_float(row.get("size", row.get("quantity", 0))),
            "pnl": dollar_pnl,
            "return_pct": display_return_pct,
        }
        equity = max(0.0, equity * (1.0 + ratio))
        if row.get("mae") not in (None, ""):
            trade["mae"] = _coerce_legacy_metadata_float(row.get("mae"))
        if row.get("mfe") not in (None, ""):
            trade["mfe"] = _coerce_legacy_metadata_float(row.get("mfe"))
        if row.get("direction") not in (None, ""):
            trade["direction"] = str(row["direction"])
        if row.get("bars_held") not in (None, ""):
            trade["bars_held"] = int(row["bars_held"])
        if row.get("exit_reason") not in (None, ""):
            trade["exit_reason"] = str(row["exit_reason"])
        if row.get("size_fraction") not in (None, ""):
            trade["size_fraction"] = _coerce_legacy_metadata_float(row.get("size_fraction"))
        if row.get("regime") not in (None, ""):
            trade["regime"] = str(row["regime"])
        trades.append(trade)
    return trades


def _normalize_chart_bars(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    bars: list[dict] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        timestamp = row.get("timestamp") or row.get("time")
        open_ = row.get("open")
        high = row.get("high")
        low = row.get("low")
        close = row.get("close")
        volume = row.get("volume", 0.0)
        if timestamp in (None, "") or open_ in (None, "") or high in (None, "") or low in (None, "") or close in (None, ""):
            continue
        bars.append(
            {
                "timestamp": str(timestamp),
                "open": _coerce_legacy_metadata_float(open_),
                "high": _coerce_legacy_metadata_float(high),
                "low": _coerce_legacy_metadata_float(low),
                "close": _coerce_legacy_metadata_float(close),
                "volume": _coerce_legacy_metadata_float(volume),
            }
        )
    return bars


def _normalize_chart_markers(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    markers: list[dict] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        timestamp = row.get("timestamp") or row.get("time")
        price = row.get("price")
        if timestamp in (None, "") or price in (None, ""):
            continue
        marker = {
            "timestamp": str(timestamp),
            "price": _coerce_legacy_metadata_float(price),
        }
        label = str(row.get("label") or "").strip()
        if label:
            marker["label"] = label
        # Preserve trade side so the chart can draw shorts as red down-arrows above the
        # bar (and covers as green up-arrows) instead of defaulting every marker to long.
        direction = str(row.get("direction") or "").strip().lower()
        if direction in ("long", "short"):
            marker["direction"] = direction
        markers.append(marker)
    return markers


def _normalize_chart_indicator_points(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    points: list[dict] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        timestamp = row.get("timestamp") or row.get("time")
        indicator_value = row.get("value")
        if timestamp in (None, "") or indicator_value in (None, ""):
            continue
        points.append(
            {
                "timestamp": str(timestamp),
                "value": _coerce_legacy_metadata_float(indicator_value),
            }
        )
    return points


def _normalize_chart_indicators(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    indicators: list[dict] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        indicators.append(
            {
                "name": name,
                "color": str(row.get("color") or "").strip() or "#22d3ee",
                "data": _normalize_chart_indicator_points(row.get("data")),
            }
        )
    return indicators


def _normalize_backtest_chart_context_payload(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    warnings_raw = value.get("warnings")
    warnings = []
    if isinstance(warnings_raw, list):
        warnings = [str(item).strip() for item in warnings_raw if str(item or "").strip()]
    return {
        "result_id": str(value.get("result_id") or "").strip(),
        "bars": _normalize_chart_bars(value.get("bars")),
        "entry_markers": _normalize_chart_markers(value.get("entry_markers")),
        "exit_markers": _normalize_chart_markers(value.get("exit_markers")),
        "main_indicators": _normalize_chart_indicators(value.get("main_indicators")),
        "sub_indicators": _normalize_chart_indicators(value.get("sub_indicators")),
        "strategy_name": str(value.get("strategy_name") or "Strategy").strip() or "Strategy",
        "strategy_meta": str(value.get("strategy_meta") or "").strip(),
        "strategy_params": value.get("strategy_params") if isinstance(value.get("strategy_params"), dict) else {},
        "warnings": warnings,
    }


def _safe_result_artifact_key(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-") or "result"


def _load_result_json_artifact(result_id: str, meta: dict, result_type: str, suffix: str) -> tuple[object, str | None]:
    candidates = _result_artifact_candidate_ids(result_id, meta, result_type)
    for base_dir in core._result_data_dirs():
        for candidate in candidates:
            path = os.path.join(base_dir, f"{candidate}_{suffix}.json")
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return _parse_json_blob(fh.read(), None), path
            except Exception:
                continue
    return None, None


def _result_artifact_candidate_ids(result_id: str, meta: dict, result_type: str) -> list[str]:
    candidates: list[str] = []

    def _add(candidate: str):
        candidate = str(candidate or "").strip()
        if not candidate:
            return
        # SECURITY (audit 2026-06-22, L4): sanitize BEFORE the value is joined into
        # an artifact filename. An unsanitized result_id like '..\\..\\secrets'
        # would escape data/results via os.path.join on Windows (the route regex
        # matches backslashes) → arbitrary-file read. Mirror the write side's
        # _safe_result_artifact_key so legit ids are unchanged but separators/.. die.
        candidate = _safe_result_artifact_key(candidate)
        if candidate not in candidates:
            candidates.append(candidate)

    _add(result_id)
    job_id = str((meta or {}).get("job_id") or "").strip()
    if job_id:
        _add(job_id)
        if not job_id.startswith(("bt_", "wf_", "opt_", "gs_")):
            prefix = {
                "backtest": "bt",
                "walk_forward": "wf",
                "optimization": "opt",
                "grid_search": "gs",
            }.get(result_type, "bt")
            _add(f"{prefix}_{job_id}")
    return candidates


def _load_result_artifacts(result_id: str, meta: dict, result_type: str) -> dict:
    candidates = _result_artifact_candidate_ids(result_id, meta, result_type)
    for base_dir in core._result_data_dirs():
        for candidate in candidates:
            eq_path = os.path.join(base_dir, f"{candidate}_equity.json")
            tr_path = os.path.join(base_dir, f"{candidate}_trades.json")
            bm_path = os.path.join(base_dir, f"{candidate}_benchmark.json")
            eqf_path = os.path.join(base_dir, f"{candidate}_equity_full.json")
            bmf_path = os.path.join(base_dir, f"{candidate}_benchmark_full.json")
            if not (os.path.exists(eq_path) or os.path.exists(tr_path) or os.path.exists(bm_path)):
                continue
            try:
                equity_curve = None
                trades = None
                benchmark_curve = None
                equity_curve_full = None
                benchmark_curve_full = None
                if os.path.exists(eq_path):
                    with open(eq_path, "r", encoding="utf-8") as fh:
                        raw = _parse_json_blob(fh.read(), [])
                        parsed = _normalize_equity_points(raw)
                        if parsed:
                            equity_curve = parsed
                if os.path.exists(tr_path):
                    with open(tr_path, "r", encoding="utf-8") as fh:
                        raw = _parse_json_blob(fh.read(), [])
                        parsed = _normalize_trade_rows(raw)
                        if parsed:
                            trades = parsed
                        elif isinstance(raw, list):
                            trades = []
                if os.path.exists(bm_path):
                    with open(bm_path, "r", encoding="utf-8") as fh:
                        raw = _parse_json_blob(fh.read(), [])
                        parsed = _normalize_equity_points(raw)
                        if parsed:
                            benchmark_curve = parsed
                # Full-window (IS+OOS) curves for the entire-timeframe equity chart.
                # Absent on results created before this was added → frontend falls
                # back to the OOS-only curve.
                if os.path.exists(eqf_path):
                    with open(eqf_path, "r", encoding="utf-8") as fh:
                        raw = _parse_json_blob(fh.read(), [])
                        parsed = _normalize_equity_points(raw)
                        if parsed:
                            equity_curve_full = parsed
                if os.path.exists(bmf_path):
                    with open(bmf_path, "r", encoding="utf-8") as fh:
                        raw = _parse_json_blob(fh.read(), [])
                        parsed = _normalize_equity_points(raw)
                        if parsed:
                            benchmark_curve_full = parsed
                return {
                    "equity_curve": equity_curve,
                    "trades": trades,
                    "benchmark_curve": benchmark_curve,
                    "equity_curve_full": equity_curve_full,
                    "benchmark_curve_full": benchmark_curve_full,
                    "source_path": eq_path if os.path.exists(eq_path) else (tr_path if os.path.exists(tr_path) else bm_path),
                }
            except Exception:
                continue
    return {
        "equity_curve": None,
        "trades": None,
        "benchmark_curve": None,
        "equity_curve_full": None,
        "benchmark_curve_full": None,
        "source_path": None,
    }


def _load_backtest_chart_artifact(result_id: str, meta: dict, result_type: str) -> dict | None:
    raw_payload, source_path = _load_result_json_artifact(result_id, meta, result_type, "chart")
    payload = _normalize_backtest_chart_context_payload(raw_payload)
    if payload is None:
        return None
    if not payload.get("result_id"):
        payload["result_id"] = result_id
    payload["source_path"] = source_path
    return payload


def _coerce_iso_datetime(value, fallback: str) -> str:
    if value in (None, ""):
        return fallback
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except Exception:
        return str(value)


def _build_synthetic_equity_curve(summary: dict, config: dict) -> list[dict]:
    total_return_pct = _coerce_legacy_metadata_float(summary.get("total_return"), 0.0)
    initial = _coerce_legacy_metadata_float((config or {}).get("initial_capital"), 10000.0)
    if initial <= 0:
        initial = 10000.0

    end_equity = initial * (1.0 + (total_return_pct / 100.0))
    if end_equity <= 0:
        end_equity = max(1.0, initial * 0.01)

    max_dd_pct = abs(_coerce_legacy_metadata_float(summary.get("max_drawdown"), 0.0))
    trough_factor = max(0.05, min(0.95, 1.0 - (max_dd_pct / 100.0)))
    trough_equity = min(initial, end_equity) * trough_factor

    end_ts_raw = (config or {}).get("end") or summary.get("end") or summary.get("created_at") or core._now()
    start_ts_raw = (config or {}).get("start") or summary.get("start") or end_ts_raw
    end_ts = _coerce_iso_datetime(end_ts_raw, core._now())
    start_ts = _coerce_iso_datetime(start_ts_raw, end_ts)
    try:
        start_dt = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(days=1)
        mid_dt = start_dt + ((end_dt - start_dt) / 2)
        return [
            {"timestamp": start_dt.isoformat(), "equity": round(initial, 6)},
            {"timestamp": mid_dt.isoformat(), "equity": round(trough_equity, 6)},
            {"timestamp": end_dt.isoformat(), "equity": round(end_equity, 6)},
        ]
    except Exception:
        return [
            {"timestamp": start_ts, "equity": round(initial, 6)},
            {"timestamp": end_ts, "equity": round(end_equity, 6)},
        ]


def _build_sqlite_backtest_detail(result_id: str) -> dict | None:
    """Build a result detail payload from the backtest_results SQLite table.

    This is faster and more reliable than ChromaDB and avoids segfaults on
    Windows that occur with certain ChromaDB/ONNX combinations.
    """
    try:
        with core.get_db() as conn:
            row = conn.execute(
                "SELECT * FROM backtest_results WHERE result_id = ?",
                (result_id,),
            ).fetchone()
            if not row:
                return None
    except Exception:
        return None

    import json as _json

    metrics_raw = {}
    try:
        metrics_raw = _json.loads(row["metrics_json"] or "{}")
    except Exception:
        pass
    config_raw = {}
    try:
        config_raw = _json.loads(row["config_json"] or "{}")
    except Exception:
        pass

    result_type = str(row["result_type"] or "backtest")
    strategy_id = str(row["strategy_id"] or "")
    symbol = str(row["symbol"] or "")
    timeframe = str(row["timeframe"] or "1h")
    start_date = str(row["start_date"] or "") or None
    end_date = str(row["end_date"] or "") or None
    created_at = str(row["created_at"] or "")
    raw_status = str(metrics_raw.get("status") or config_raw.get("status") or "succeeded").strip().lower()
    status_aliases = {
        "success": "succeeded",
        "completed": "succeeded",
        "complete": "succeeded",
        "queued": "running",
        "pending": "running",
        "error": "failed",
    }
    status = status_aliases.get(raw_status, raw_status or "succeeded")
    error_detail = str(metrics_raw.get("error") or config_raw.get("error") or "").strip()
    if status == "failed" and not error_detail:
        error_detail = "Run failed before an error message was persisted"
    job_id = str(config_raw.get("job_id") or metrics_raw.get("job_id") or "").strip() or None

    def _mf(key: str, *alt_keys: str, default=0.0):
        for k in (key,) + alt_keys:
            v = metrics_raw.get(k)
            if v is not None and v != "":
                return _coerce_legacy_metadata_float(v, default)
        return default

    total_return = _mf("total_return_pct", "total_return")
    sharpe = _mf("sharpe", "sharpe_ratio")
    sortino = _mf("sortino", "sortino_ratio")
    max_dd = _mf("max_drawdown_pct", "max_drawdown")
    win_rate = _mf("win_rate", "win_rate_pct")
    profit_factor = _mf("profit_factor", "pf")
    total_trades = int(_mf("total_trades", "trades") or 0)
    cagr = _mf("cagr", "cagr_pct", "annualized_return_pct", default=None)
    avg_trade = _mf("avg_trade_pct", "avg_trade", default=None)

    metrics_out = {
        "total_return": total_return,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_dd,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "total_trades": total_trades,
        "sortino_ratio": sortino,
    }
    if cagr is not None:
        metrics_out["cagr"] = cagr
        metrics_out["annualized_return_pct"] = cagr
    if avg_trade is not None:
        metrics_out["avg_trade_pct"] = avg_trade

    # Copy through any extra metrics stored in the JSON blob.
    for k in (
        "calmar_ratio", "omega_ratio", "tail_ratio", "expectancy",
        "recovery_factor", "avg_mae", "avg_mfe", "edge_ratio",
        "avg_trade_duration", "max_drawdown_duration", "backtest_months",
        "monthly_return_pct", "annualized_return_pct",
    ):
        v = metrics_raw.get(k)
        if v is not None and k not in metrics_out:
            metrics_out[k] = _coerce_legacy_metadata_float(v)
    metrics_out["status"] = status
    if error_detail:
        metrics_out["error"] = error_detail
    if metrics_raw.get("best_fitness") is not None:
        metrics_out["best_fitness"] = _coerce_legacy_metadata_float(metrics_raw.get("best_fitness"))
    elif config_raw.get("best_fitness") is not None:
        metrics_out["best_fitness"] = _coerce_legacy_metadata_float(config_raw.get("best_fitness"))
    if metrics_raw.get("n_trials") is not None:
        metrics_out["n_trials"] = int(_coerce_legacy_metadata_float(metrics_raw.get("n_trials"), 0) or 0)
    elif config_raw.get("n_trials") is not None:
        metrics_out["n_trials"] = int(_coerce_legacy_metadata_float(config_raw.get("n_trials"), 0) or 0)
    objective = config_raw.get("objective", metrics_raw.get("objective"))
    if objective is not None:
        metrics_out["objective"] = objective
    if metrics_raw.get("validated") is not None:
        metrics_out["validated"] = bool(metrics_raw.get("validated"))
    elif config_raw.get("validated") is not None:
        metrics_out["validated"] = bool(config_raw.get("validated"))
    wfa_verdict = metrics_raw.get("wfa_verdict", config_raw.get("wfa_verdict"))
    if wfa_verdict is not None:
        metrics_out["wfa_verdict"] = wfa_verdict

    # Try to load artifact files (equity curve, trades, benchmark).
    artifacts = _load_result_artifacts(result_id, config_raw, result_type)

    config_out = dict(config_raw)
    config_out.setdefault("strategy_id", strategy_id)
    config_out.setdefault("symbol", symbol)
    config_out.setdefault("timeframe", timeframe)
    config_out.setdefault("status", status)
    if error_detail:
        config_out.setdefault("error", error_detail)
    if job_id:
        config_out.setdefault("job_id", job_id)

    detail = {
        "id": result_id,
        "result_id": result_id,
        "job_id": job_id or "",
        "strategy_id": strategy_id,
        "strategy_name": config_raw.get("strategy_name", strategy_id),
        "result_type": result_type,
        "symbol": symbol,
        "timeframe": timeframe,
        "start": start_date,
        "end": end_date,
        "created_at": created_at,
        "metrics": metrics_out,
        "config": config_out,
        "status": status,
        "error": error_detail or None,
        "warnings": [],
    }
    if artifacts.get("equity_curve") is not None:
        detail["equity_curve"] = artifacts["equity_curve"]
    if artifacts.get("trades") is not None:
        detail["trades"] = artifacts["trades"]
    if artifacts.get("benchmark_curve") is not None:
        detail["benchmark_curve"] = artifacts["benchmark_curve"]
    if artifacts.get("equity_curve_full") is not None:
        detail["equity_curve_full"] = artifacts["equity_curve_full"]
    if artifacts.get("benchmark_curve_full") is not None:
        detail["benchmark_curve_full"] = artifacts["benchmark_curve_full"]
    return detail


def _build_file_only_backtest_detail(result_id: str) -> dict | None:
    artifacts = _load_result_artifacts(result_id, {}, "backtest")
    equity_curve = artifacts.get("equity_curve")
    trades = artifacts.get("trades")
    benchmark_curve = artifacts.get("benchmark_curve")
    if equity_curve is None and trades is None and benchmark_curve is None:
        return None

    warnings: list[str] = []
    total_trades = len(trades) if isinstance(trades, list) else 0
    win_rate = 0.0
    profit_factor = 0.0
    if isinstance(trades, list) and trades:
        wins = 0
        gains = 0.0
        losses = 0.0
        for trade in trades:
            ret = _coerce_legacy_metadata_float(trade.get("return_pct"), 0.0)
            pnl = _coerce_legacy_metadata_float(trade.get("pnl"), 0.0)
            if ret > 0:
                wins += 1
            if pnl > 0:
                gains += pnl
            elif pnl < 0:
                losses += abs(pnl)
        if total_trades > 0:
            win_rate = (wins / total_trades) * 100.0
        # MATH-01: profit_factor is mathematically infinite with zero losses,
        # not the legacy 10.0 sentinel which silently inflated downstream gates.
        profit_factor = (gains / losses) if losses > 0 else (float("inf") if gains > 0 else 0.0)
    else:
        warnings.append("Trade-level rows are unavailable for this result.")

    total_return = 0.0
    max_drawdown = 0.0
    start = None
    end = None
    if isinstance(equity_curve, list) and equity_curve:
        start = str(equity_curve[0].get("timestamp") or "")
        end = str(equity_curve[-1].get("timestamp") or "")
        start_equity = _coerce_legacy_metadata_float(equity_curve[0].get("equity"), 0.0)
        end_equity = _coerce_legacy_metadata_float(equity_curve[-1].get("equity"), start_equity)
        if start_equity > 0:
            total_return = ((end_equity / start_equity) - 1.0) * 100.0

        peak = 0.0
        max_dd = 0.0
        for point in equity_curve:
            eq = _coerce_legacy_metadata_float(point.get("equity"), 0.0)
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = ((peak - eq) / peak) * 100.0
                if dd > max_dd:
                    max_dd = dd
        max_drawdown = max_dd
    else:
        warnings.append("No persisted equity curve is available for this result.")

    backtest_months = None
    annualized_return_pct = None
    monthly_return_pct = None
    if start and end:
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            delta = (end_dt - start_dt).total_seconds()
            if delta > 0:
                backtest_months = max(1e-6, delta / (60.0 * 60.0 * 24.0 * 30.4375))
                growth = 1.0 + (total_return / 100.0)
                if growth > 0:
                    monthly_return_pct = (pow(growth, 1.0 / backtest_months) - 1.0) * 100.0
                    annualized_return_pct = (pow(growth, 12.0 / backtest_months) - 1.0) * 100.0
        except Exception:
            pass

    config = {
        "start": start,
        "end": end,
    }
    if warnings:
        # Deduplicate while preserving message order.
        unique_warnings: list[str] = []
        seen_warnings: set[str] = set()
        for warning in warnings:
            msg = str(warning or "").strip()
            if not msg or msg in seen_warnings:
                continue
            seen_warnings.add(msg)
            unique_warnings.append(msg)
        if unique_warnings:
            config["warnings"] = unique_warnings

    source_path = artifacts.get("source_path")
    now_ts = core._now()
    if source_path and os.path.exists(str(source_path)):
        try:
            now_ts = datetime.fromtimestamp(os.path.getmtime(str(source_path)), tz=timezone.utc).isoformat()
        except Exception:
            now_ts = core._now()
    return {
        "id": result_id,
        "job_id": f"file:{result_id}",
        "strategy_name": result_id,
        "strategy_id": result_id,
        "lifecycle_strategy_id": (
            str(result_id).upper()
            if re.fullmatch(r"S\d{4,6}", str(result_id), re.IGNORECASE)
            else None
        ),
        "strategy_version": "backtest",
        "symbol": "",
        "timeframe": "1h",
        "created_at": now_ts,
        "metrics": {
            "total_return": total_return,
            "sharpe_ratio": 0.0,
            "monthly_return_pct": monthly_return_pct,
            "annualized_return_pct": annualized_return_pct,
            "backtest_months": backtest_months,
            "max_drawdown": max_drawdown,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "total_trades": total_trades,
            "sortino_ratio": 0.0,
        },
        "config": config,
        "equity_curve": equity_curve,
        "benchmark_curve": benchmark_curve,
        "equity_curve_full": artifacts.get("equity_curve_full"),
        "benchmark_curve_full": artifacts.get("benchmark_curve_full"),
        "trades": trades,
        "result_type": "backtest",
        "verdict": calculate_backtest_verdict({
            "total_trades": total_trades,
            "sharpe_ratio": 0.0,
            "profit_factor": profit_factor,
            "max_drawdown": max_drawdown,
        }),
    }


def calculate_backtest_verdict(metrics: dict) -> str:
    """Calculate an honest backtest verdict based on multiple risk/reward metrics."""
    total_trades = int(metrics.get("total_trades", 0))
    sharpe = float(metrics.get("sharpe_ratio") or metrics.get("sharpe") or 0.0)
    profit_factor = float(metrics.get("profit_factor") or 0.0)
    max_dd = float(metrics.get("max_drawdown") or metrics.get("max_drawdown_pct") or 0.0)

    if total_trades < 3:
        return "Insufficient Data"

    # Promising: Robust sample, healthy risk-adjusted returns, and positive edge
    if total_trades >= 15 and sharpe >= 1.0 and profit_factor >= 1.3 and max_dd < 35:
        return "Promising"

    # Marginal: Could work, but tighter margins or smaller sample
    if total_trades >= 8 and sharpe >= 0.5 and profit_factor >= 1.1 and max_dd < 50:
        return "Marginal"

    # Weak: Low sample, poor risk-adjusted returns, or excessive drawdown
    return "Weak"


def _sqlite_backtest_summaries(
    *,
    strategy: str | None = None,
    symbol: str | None = None,
    lifecycle_id: str | None = None,
    limit: int = 200,
    deleted_ids: set[str] | None = None,
) -> list[dict]:
    """List result summaries from SQLite without touching ChromaDB."""
    normalized_strategy = strategy.strip().lower() if strategy else None
    normalized_symbol = symbol.strip().upper() if symbol else None
    normalized_lifecycle = lifecycle_id.strip().upper() if lifecycle_id else None
    deleted = deleted_ids or set()
    scan_limit = max(int(limit or 200), 1)
    if normalized_strategy or normalized_lifecycle:
        scan_limit = max(scan_limit * 20, 1000)
    scan_limit = min(scan_limit, 10000)

    where = ["(deleted_at IS NULL OR deleted_at = '')"]
    params: list[object] = []
    if normalized_symbol:
        where.append("UPPER(symbol) = ?")
        params.append(normalized_symbol)
    params.append(scan_limit)

    try:
        with core.get_db() as conn:
            rows = conn.execute(
                f"""
                SELECT result_id, strategy_id, result_type, symbol, timeframe,
                       start_date, end_date, metrics_json, config_json, created_at
                FROM backtest_results
                WHERE {' AND '.join(where)}
                ORDER BY datetime(created_at) DESC, result_id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
    except Exception as exc:
        log.warning("SQLite backtest result list failed: %s", exc)
        return []

    def _metric_float(metrics: dict, *keys: str, default=0.0):
        for key in keys:
            value = metrics.get(key)
            if value not in (None, ""):
                return _coerce_legacy_metadata_float(value, default)
        return default

    def _ratio_to_percent(value: object) -> float:
        parsed = _coerce_legacy_metadata_float(value, 0.0)
        if parsed is None:
            return 0.0
        return float(parsed) * 100.0 if -1.0 <= float(parsed) <= 1.0 else float(parsed)

    summaries: list[dict] = []
    for row in rows:
        result_id = str(row["result_id"] or "").strip()
        if not result_id or result_id in deleted:
            continue

        metrics_raw = _parse_json_blob(row["metrics_json"], {})
        if not isinstance(metrics_raw, dict):
            metrics_raw = {}
        config_raw = _parse_json_blob(row["config_json"], {})
        if not isinstance(config_raw, dict):
            config_raw = {}

        strategy_id = str(row["strategy_id"] or config_raw.get("strategy_id") or "").strip()
        strategy_name = str(
            config_raw.get("strategy_name")
            or config_raw.get("strategy")
            or strategy_id
            or "unknown"
        ).strip()
        lifecycle_strategy_id = str(
            config_raw.get("lifecycle_strategy_id")
            or config_raw.get("lifecycle_id")
            or strategy_id
        ).strip()
        row_symbol = str(row["symbol"] or config_raw.get("symbol") or config_raw.get("asset") or "").strip().upper()

        if normalized_strategy:
            haystack = f"{strategy_id} {strategy_name}".lower()
            if normalized_strategy not in haystack:
                continue
        if normalized_lifecycle:
            if normalized_lifecycle not in {strategy_id.upper(), lifecycle_strategy_id.upper()}:
                continue

        total_trades = int(_metric_float(metrics_raw, "total_trades", "trades", default=0.0) or 0)
        profit_factor = _metric_float(metrics_raw, "profit_factor", "pf", default=0.0)
        verdict = str(metrics_raw.get("verdict") or metrics_raw.get("wfa_verdict") or "").strip()
        if not verdict:
            verdict = calculate_backtest_verdict({
                "total_trades": total_trades,
                "sharpe": _metric_float(metrics_raw, "sharpe", "sharpe_ratio", default=0.0),
                "profit_factor": profit_factor,
                "max_drawdown": _metric_float(metrics_raw, "max_drawdown_pct", "max_drawdown", default=0.0),
            })

        summary = {
            "id": result_id,
            "job_id": str(config_raw.get("job_id") or metrics_raw.get("job_id") or f"sqlite:{result_id}"),
            "strategy_name": strategy_name,
            "strategy_id": strategy_id,
            "lifecycle_strategy_id": lifecycle_strategy_id,
            "symbol": row_symbol,
            "timeframe": str(row["timeframe"] or config_raw.get("timeframe") or "1h"),
            "created_at": str(row["created_at"] or ""),
            "start": str(row["start_date"] or config_raw.get("start") or metrics_raw.get("start_date") or ""),
            "end": str(row["end_date"] or config_raw.get("end") or metrics_raw.get("end_date") or ""),
            "total_return": _metric_float(metrics_raw, "total_return_pct", "total_return", default=0.0),
            "monthly_return_pct": core._coerce_optional_float(
                metrics_raw.get("monthly_return_pct", metrics_raw.get("evaluation_monthly_return_pct"))
            ),
            "annualized_return_pct": core._coerce_optional_float(
                metrics_raw.get("annualized_return_pct", metrics_raw.get("evaluation_annualized_return_pct"))
            ),
            "backtest_months": core._coerce_optional_float(
                metrics_raw.get("backtest_months", metrics_raw.get("evaluation_backtest_months"))
            ),
            "sharpe_ratio": _metric_float(metrics_raw, "sharpe", "sharpe_ratio", default=0.0),
            "max_drawdown": _metric_float(metrics_raw, "max_drawdown_pct", "max_drawdown", default=0.0),
            "win_rate": _ratio_to_percent(metrics_raw.get("win_rate")),
            "total_trades": total_trades,
            "profit_factor": profit_factor,
            "result_type": str(row["result_type"] or "backtest"),
            "verdict": verdict,
        }
        if metrics_raw.get("profit_factor_is_infinite") is not None:
            summary["profit_factor_is_infinite"] = bool(metrics_raw.get("profit_factor_is_infinite"))
        summaries.append(summary)
        if len(summaries) >= int(limit or 200):
            break

    return summaries


def _coerce_backtest_summary_payload(record: object) -> dict | None:
    if not isinstance(record, dict):
        return None

    rid = str(record.get("id") or "").strip()
    if not rid:
        return None

    # Remote peers may return raw Chroma rows or already-normalized summaries.
    if "metadata" in record and isinstance(record.get("metadata"), dict):
        return _normalize_backtest_summary({"id": rid, "metadata": record.get("metadata") or {}})

    total_trades = int(_coerce_legacy_metadata_float(record.get("total_trades"), 0.0) or 0)
    verdict = str(record.get("verdict") or "").strip()
    if not verdict:
        verdict = "Insufficient Data" if total_trades < 2 else "Promising"

    def _filter_sentinel(value):
        """Filter -999.0 sentinel â†’ None."""
        if value is not None and value == -999.0:
            return None
        return value

    return {
        "id": rid,
        "job_id": str(record.get("job_id") or f"remote:{rid}"),
        "strategy_name": str(record.get("strategy_name") or record.get("strategy_id") or "unknown"),
        "strategy_id": str(record.get("strategy_id") or record.get("lifecycle_strategy_id") or ""),
        "lifecycle_strategy_id": str(record.get("lifecycle_strategy_id") or record.get("strategy_id") or ""),
        "symbol": str(record.get("symbol") or record.get("asset") or ""),
        "timeframe": str(record.get("timeframe") or "1h"),
        "created_at": str(record.get("created_at") or record.get("recorded_at") or "1970-01-01T00:00:00+00:00"),
        "start": str(record.get("start") or record.get("start_date") or record.get("created_at") or ""),
        "end": str(record.get("end") or record.get("end_date") or record.get("created_at") or ""),
        "total_return": _coerce_legacy_metadata_float(record.get("total_return"), 0.0),
        "monthly_return_pct": _filter_sentinel(core._coerce_optional_float(record.get("monthly_return_pct"))),
        "annualized_return_pct": _filter_sentinel(core._coerce_optional_float(record.get("annualized_return_pct"))),
        "backtest_months": _filter_sentinel(core._coerce_optional_float(record.get("backtest_months"))),
        "sharpe_ratio": _coerce_legacy_metadata_float(record.get("sharpe_ratio"), 0.0),
        "max_drawdown": _coerce_legacy_metadata_float(record.get("max_drawdown"), 0.0),
        "win_rate": _coerce_legacy_metadata_float(record.get("win_rate"), 0.0),
        "total_trades": total_trades,
        "profit_factor": _coerce_legacy_metadata_float(record.get("profit_factor"), 0.0),
        "result_type": str(record.get("result_type") or "backtest"),
        "verdict": verdict,
    }


def _describe_strategy(strategy_type: str | None, params: dict) -> str:
    """Generate a plain-English description from strategy type and params.

    Standalone version that doesn't require instantiating strategy objects â€”
    used for results already stored in ChromaDB.
    """
    if not strategy_type:
        return ""
    st = strategy_type.strip().lower()
    if st == "rsi_momentum":
        rsi_p = params.get("rsi_period", 14)
        rsi_entry = params.get("rsi_entry", 40)
        rsi_exit = params.get("rsi_exit", 60)
        ema_fast = params.get("ema_fast", 50)
        ema_slow = params.get("ema_slow", 200)
        return (
            f"Buys when the {rsi_p}-period RSI bounces up from below {rsi_entry} "
            f"while price is above the {ema_fast} and {ema_slow}-bar moving averages. "
            f"Sells when RSI drops below {rsi_exit}."
        )
    if st == "bollinger":
        bb_p = params.get("bb_period", 20)
        bb_std = params.get("bb_std", 2.0)
        return (
            f"Buys when price breaks above the upper Bollinger Band "
            f"({bb_p}-period, {bb_std} std dev) while in an uptrend. "
            f"Sells when price falls back to the middle band."
        )
    if st == "ema_cross":
        fast = params.get("ema_fast", 20)
        slow = params.get("ema_slow", 50)
        regime = params.get("ema_regime", 200)
        return (
            f"Buys when the {fast}-bar moving average crosses above the "
            f"{slow}-bar average. Uses a {regime}-bar trend filter. "
            f"Sells on the reverse crossover."
        )
    if st == "macd":
        fast = params.get("fast", 5)
        slow = params.get("slow", 13)
        sig = params.get("signal", 3)
        return (
            f"Uses MACD ({fast}/{slow}/{sig}) to track momentum. "
            f"Buys when MACD crosses above the signal line in an uptrend. "
            f"Sells on the reverse crossover."
        )
    if st == "keltner":
        kp = params.get("kc_period", 20)
        km = params.get("kc_mult", 2.0)
        return (
            f"Buys when price breaks above the upper Keltner Channel "
            f"({kp}-period, {km}x ATR) while in an uptrend. "
            f"Sells when price falls to the middle line."
        )
    if st == "stochastic":
        k_period = params.get("k_period", 14)
        k_os = params.get("k_oversold", 20)
        k_ob = params.get("k_overbought", 80)
        direction = params.get("direction", "long")
        if direction == "long":
            return (
                f"Buys when the {k_period}-period Stochastic bounces from "
                f"oversold (below {k_os}). "
                f"Sells at overbought (above {k_ob})."
            )
        return (
            f"Shorts when the {k_period}-period Stochastic drops from "
            f"overbought (above {k_ob}). "
            f"Covers at oversold (below {k_os})."
        )
    if st == "funding":
        # Mirrors FundingStrategy.default_params (per-hour funding convention).
        threshold = params.get("entry_threshold", 0.00000375)
        threshold_pct = float(threshold) * 100
        return (
            f"Buys when crypto futures funding becomes extremely negative "
            f"(shorts overpaying longs, below -{threshold_pct:.4f}%). "
            f"Exits when funding normalizes."
        )
    return ""


def _normalize_backtest_summary(record: dict) -> dict:
    """Map Chroma backtest metadata to the `/results` UI schema."""
    meta = record.get("metadata", {}) or {}
    rid = str(record.get("id") or "").strip() or "unknown"
    config_meta = _parse_json_blob(meta.get("config_json"), {})
    if not isinstance(config_meta, dict):
        config_meta = {}
    created = str(
        meta.get("recorded_at")
        or config_meta.get("created_at")
        or config_meta.get("created")
        or "1970-01-01T00:00:00+00:00"
    )
    result_type = _extract_result_type(rid, meta)

    def _meta_float(*keys: str, default=None):
        for key in keys:
            if key in meta and meta.get(key) not in (None, ""):
                return _coerce_legacy_metadata_float(meta.get(key))
            if key in config_meta and config_meta.get(key) not in (None, ""):
                return _coerce_legacy_metadata_float(config_meta.get(key))
        return default

    def _ratio_to_percent_points(value):
        """Convert a 0-1 ratio to percent points (for win_rate only)."""
        if value is None:
            return 0.0
        v = float(value)
        return v * 100.0 if abs(v) <= 1.0 else v

    def _as_percent_points(value):
        """Values already in percent points (total_return, max_drawdown). Pass through."""
        if value is None:
            return 0.0
        return float(value)

    start_value = str(meta.get("start_date") or meta.get("start") or config_meta.get("start") or created)
    end_value = str(meta.get("end_date") or meta.get("end") or config_meta.get("end") or created)

    total_return_raw = _meta_float("total_return", "total_return_pct")
    total_return = _ratio_to_percent_points(total_return_raw)

    monthly_return_raw = _meta_float("monthly_return_pct")
    monthly_return = monthly_return_raw if monthly_return_raw is not None else None

    annualized_return_raw = _meta_float("annualized_return_pct")
    annualized_return = annualized_return_raw if annualized_return_raw is not None else None

    backtest_months = _meta_float("backtest_months")
    derived_backtest_months = None

    # Filter -999.0 sentinel values (legacy rows used them for absent metrics).
    if monthly_return is not None and monthly_return == -999.0:
        monthly_return = None
    if annualized_return is not None and annualized_return == -999.0:
        annualized_return = None
    if backtest_months is not None and backtest_months <= 0:
        backtest_months = None
    try:
        start_dt = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
        delta = (end_dt - start_dt).total_seconds()
        if delta > 0:
            derived_backtest_months = delta / (60.0 * 60.0 * 24.0 * 30.4375)
    except Exception:
        derived_backtest_months = None
    # Keep span aligned with the displayed date range when it is available.
    if derived_backtest_months is not None and derived_backtest_months > 0:
        backtest_months = derived_backtest_months

    has_nonzero_return = abs(total_return) > 1e-9
    monthly_placeholder = monthly_return is not None and abs(monthly_return) < 1e-9
    annualized_placeholder = annualized_return is not None and abs(annualized_return) < 1e-9

    if has_nonzero_return and (monthly_return is None or monthly_placeholder):
        growth = 1.0 + (total_return / 100.0)
        calc_months = backtest_months if backtest_months and backtest_months > 0 else 1.0
        if growth > 0:
            monthly_return = (pow(growth, 1.0 / calc_months) - 1.0) * 100.0
        else:
            monthly_return = total_return / calc_months

    if has_nonzero_return and (annualized_return is None or annualized_placeholder) and backtest_months and backtest_months > 0:
        growth = 1.0 + (total_return / 100.0)
        if growth > 0:
            annualized_return = (pow(growth, 12.0 / backtest_months) - 1.0) * 100.0
        else:
            annualized_return = (total_return / backtest_months) * 12.0

    # Derive monthly/annualized return from total_return + date range when
    # absent â€” same geometric mean formula as _build_file_only_backtest_detail.
    total_return_pct = _ratio_to_percent_points(total_return_raw)
    if total_return_pct != 0.0 and (monthly_return is None or monthly_return == 0.0):
        start_str = str(meta.get("start_date") or meta.get("start") or config_meta.get("start") or "")
        end_str = str(meta.get("end_date") or meta.get("end") or config_meta.get("end") or "")
        if start_str and end_str:
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                delta = (end_dt - start_dt).total_seconds()
                if delta > 0:
                    derived_months = max(1e-6, delta / (60.0 * 60.0 * 24.0 * 30.4375))
                    growth = 1.0 + (total_return_pct / 100.0)
                    if growth > 0:
                        monthly_return = (pow(growth, 1.0 / derived_months) - 1.0) * 100.0
                        annualized_return = (pow(growth, 12.0 / derived_months) - 1.0) * 100.0
                        if backtest_months is None:
                            backtest_months = derived_months
            except Exception:
                pass

    max_drawdown_raw = _meta_float("max_drawdown", "max_drawdown_pct")
    win_rate_raw = _meta_float("win_rate")
    total_trades = int(_meta_float("total_trades", default=0) or 0)
    
    # Standardize metadata for verdict calculation
    standardized_metrics = {
        "total_trades": total_trades,
        "sharpe": _meta_float("sharpe", "sharpe_ratio", default=0.0),
        "profit_factor": _meta_float("profit_factor", default=0.0),
        "max_drawdown": _as_percent_points(max_drawdown_raw),
    }

    verdict_raw = meta.get("verdict") or meta.get("wfa_verdict")
    if isinstance(verdict_raw, bool):
        verdict = "Robust" if verdict_raw else "Not Robust"
    elif verdict_raw in (None, ""):
        verdict = calculate_backtest_verdict(standardized_metrics)
    else:
        verdict = str(verdict_raw)

    # Prefer config_json strategy_name (user-facing friendly name from submit body)
    # over metadata strategy_name (which may be a resolved internal ID like "agent-test").
    strategy_name = str(
        config_meta.get("strategy_name")
        or meta.get("strategy_name")
        or config_meta.get("strategy_id")
        or meta.get("strategy_id")
        or "unknown"
    )
    strategy_id = str(
        meta.get("strategy_id")
        or config_meta.get("strategy_id")
        or meta.get("lifecycle_strategy_id")
        or config_meta.get("lifecycle_strategy_id")
        or strategy_name
    ).strip()
    # Generate plain-English description from strategy type + params
    _stype = str(
        meta.get("strategy_type")
        or config_meta.get("strategy_type")
        or ""
    ).strip().lower() or None
    if not _stype:
        _stype = core._infer_strategy_type_from_name(strategy_name) or core._infer_strategy_type_from_name(strategy_id)
    _sparams = _parse_json_blob(meta.get("params_json"), None)
    if not isinstance(_sparams, dict):
        _sparams = config_meta.get("params", {})
    if not isinstance(_sparams, dict):
        _sparams = {}
    description = _describe_strategy(_stype, _sparams)

    return {
        "id": rid,
        "job_id": str(meta.get("job_id") or f"chroma:{rid}"),
        "strategy_name": strategy_name,
        "strategy_id": str(meta.get("strategy_id") or config_meta.get("strategy_id") or ""),
        "lifecycle_strategy_id": str(
            meta.get("lifecycle_strategy_id")
            or config_meta.get("lifecycle_strategy_id")
            or meta.get("strategy_id")
            or config_meta.get("strategy_id")
            or ""
        ),
        "symbol": str(meta.get("asset") or config_meta.get("symbol") or config_meta.get("asset") or ""),
        "timeframe": str(meta.get("timeframe") or config_meta.get("timeframe") or "1h"),
        "created_at": created,
        "start": str(meta.get("start_date") or meta.get("start") or config_meta.get("start") or created),
        "end": str(meta.get("end_date") or meta.get("end") or config_meta.get("end") or created),
        "total_return": total_return_pct,
        "monthly_return_pct": monthly_return if monthly_return is not None else None,
        "annualized_return_pct": annualized_return if annualized_return is not None else None,
        "backtest_months": backtest_months,
        "sharpe_ratio": _meta_float("sharpe", "sharpe_ratio", default=0.0),
        "max_drawdown": _as_percent_points(max_drawdown_raw),
        "win_rate": _ratio_to_percent_points(win_rate_raw),
        "total_trades": total_trades,
        "profit_factor": _meta_float("profit_factor", default=0.0),
        "result_type": result_type,
        "verdict": verdict,
        "description": description,
    }


def _normalize_backtest_detail(record: dict) -> dict:
    summary = _normalize_backtest_summary(record)
    meta = record.get("metadata", {}) or {}
    result_type = str(summary.get("result_type") or "backtest")
    config = _parse_json_blob(meta.get("config_json"), {})
    if not isinstance(config, dict):
        config = {}

    # Backward compatibility: hydrate rerun-critical fields from legacy metadata.
    if "params" not in config:
        parsed_params = _parse_json_blob(meta.get("params_json"), None)
        if isinstance(parsed_params, dict):
            config["params"] = parsed_params
    if "definition_json" not in config:
        parsed_definition = _parse_json_blob(meta.get("definition_json"), None)
        if isinstance(parsed_definition, dict):
            config["definition_json"] = parsed_definition
    if "strategy_id" not in config and meta.get("strategy_id"):
        config["strategy_id"] = str(meta.get("strategy_id"))
    if "strategy_name" not in config and summary.get("strategy_name"):
        config["strategy_name"] = str(summary.get("strategy_name"))
    if "symbol" not in config and summary.get("symbol"):
        config["symbol"] = str(summary.get("symbol"))
    if "timeframe" not in config and summary.get("timeframe"):
        config["timeframe"] = str(summary.get("timeframe"))

    warnings = config.get("warnings")
    if isinstance(warnings, list):
        warnings_out = [str(w).strip() for w in warnings if str(w).strip()]
    else:
        warnings_out = []

    def _meta_float(*keys: str, default=None):
        for key in keys:
            if key in meta and meta.get(key) not in (None, ""):
                return _coerce_legacy_metadata_float(meta.get(key))
            if key in config and config.get(key) not in (None, ""):
                return _coerce_legacy_metadata_float(config.get(key))
        return default

    metrics = {
        "total_return": summary["total_return"],
        "sharpe_ratio": summary["sharpe_ratio"],
        "monthly_return_pct": summary.get("monthly_return_pct"),
        "annualized_return_pct": summary.get("annualized_return_pct"),
        "backtest_months": summary.get("backtest_months"),
        "max_drawdown": summary["max_drawdown"],
        "win_rate": summary["win_rate"],
        "profit_factor": summary["profit_factor"],
        "total_trades": summary["total_trades"],
        "sortino_ratio": _meta_float("sortino", "sortino_ratio", default=0.0),
    }

    if _meta_float("cagr", "cagr_pct") is not None:
        metrics["cagr"] = _meta_float("cagr", "cagr_pct")
    elif summary.get("annualized_return_pct") is not None:
        metrics["cagr"] = summary.get("annualized_return_pct")

    for key in (
        "calmar_ratio",
        "omega_ratio",
        "tail_ratio",
        "value_at_risk",
        "expected_shortfall",
        "beta",
        "alpha",
        "max_drawdown_duration",
        "avg_drawdown_duration",
        "avg_mae",
        "avg_mfe",
        "edge_ratio",
        "avg_trade_duration",
        "expectancy",
        "recovery_factor",
    ):
        val = _meta_float(key)
        if val is not None:
            metrics[key] = val

    if result_type == "optimization":
        best_params = _parse_json_blob(meta.get("best_params_json"), {})
        if isinstance(best_params, dict) and best_params:
            metrics["best_params"] = best_params
        objective = str(meta.get("objective") or config.get("objective") or "sharpe_ratio")
        metrics["objective"] = objective
        n_trials = int(_meta_float("n_trials", default=0) or 0)
        if n_trials > 0:
            metrics["n_trials"] = n_trials
        best_value = _meta_float("best_value", "best_fitness", "fitness")
        if best_value is not None:
            metrics["best_value"] = best_value
        trials_summary = _parse_json_blob(meta.get("trials_summary_json"), [])
        if isinstance(trials_summary, list) and trials_summary:
            metrics["trials_summary"] = trials_summary

        optimization_cfg = config.get("optimization")
        if not isinstance(optimization_cfg, dict):
            optimization_cfg = {}
        optimization_cfg.setdefault("objective", objective)
        if n_trials > 0:
            optimization_cfg.setdefault("n_trials", n_trials)
        config["optimization"] = optimization_cfg

    if result_type == "walk_forward":
        folds: list[dict] = []
        splits = _parse_json_blob(meta.get("splits_json"), [])
        if isinstance(splits, list):
            for idx, split in enumerate(splits):
                if not isinstance(split, dict):
                    continue
                is_metrics = split.get("in_sample") if isinstance(split.get("in_sample"), dict) else {}
                oos_metrics = split.get("out_of_sample") if isinstance(split.get("out_of_sample"), dict) else {}
                fold_number = int(_coerce_legacy_metadata_float(split.get("split", idx + 1), idx + 1) or (idx + 1))
                fold = {
                    "fold_index": max(0, fold_number - 1),
                    "train_start": str(split.get("train_start") or summary.get("start") or summary.get("created_at")),
                    "train_end": str(split.get("train_end") or summary.get("end") or summary.get("created_at")),
                    "test_start": str(split.get("test_start") or summary.get("start") or summary.get("created_at")),
                    "test_end": str(split.get("test_end") or summary.get("end") or summary.get("created_at")),
                    "train_metric": _coerce_legacy_metadata_float(is_metrics.get("sharpe", is_metrics.get("objective", 0.0))),
                    "test_metric": _coerce_legacy_metadata_float(oos_metrics.get("sharpe", oos_metrics.get("objective", 0.0))),
                }
                if isinstance(split.get("best_params"), dict) and split.get("best_params"):
                    fold["best_params"] = split.get("best_params")
                folds.append(fold)

        avg_train = _meta_float("avg_is_sharpe", "avg_train_metric")
        avg_test = _meta_float("avg_oos_sharpe", "avg_test_metric")
        if avg_train is not None:
            metrics["avg_train_metric"] = avg_train
        if avg_test is not None:
            metrics["avg_test_metric"] = avg_test
        overfit = _meta_float("degradation", "overfitting_ratio")
        if overfit is not None:
            metrics["overfitting_ratio"] = overfit

        robust_params = _parse_json_blob(meta.get("best_params_json"), {})
        if isinstance(robust_params, dict) and robust_params:
            metrics["most_robust_params"] = robust_params
        if folds:
            metrics["n_folds"] = folds
            config["folds"] = folds

        walk_forward_cfg = config.get("walk_forward")
        if not isinstance(walk_forward_cfg, dict):
            walk_forward_cfg = {}
        cv_method = str(meta.get("cv_method") or config.get("cv_method") or "rolling")
        walk_forward_cfg.setdefault("cv_method", cv_method)
        n_splits = int(_meta_float("n_splits", default=0) or 0)
        if n_splits > 0:
            walk_forward_cfg.setdefault("n_splits", n_splits)
        train_ratio = _meta_float("train_ratio")
        if train_ratio is not None:
            walk_forward_cfg.setdefault("train_ratio", train_ratio)
        config["walk_forward"] = walk_forward_cfg

    if "start" not in config and summary.get("start"):
        config["start"] = summary.get("start")
    if "end" not in config and summary.get("end"):
        config["end"] = summary.get("end")

    artifacts = _load_result_artifacts(summary["id"], meta, result_type)
    equity_curve = artifacts.get("equity_curve")
    trades = artifacts.get("trades")
    benchmark_curve = artifacts.get("benchmark_curve")

    if equity_curve is None:
        if summary.get("total_trades", 0) > 0:
            synthetic_curve = _build_synthetic_equity_curve(summary, config)
            if synthetic_curve:
                equity_curve = synthetic_curve
                warnings_out.append(
                    "Displayed equity curve is reconstructed from summary metrics because raw curve data was not persisted."
                )
        else:
            warnings_out.append("No persisted equity curve is available for this result.")

    if trades is None and summary.get("total_trades", 0) > 0:
        warnings_out.append(
            "Trade-level rows are unavailable for this result. Showing aggregate metrics only."
        )

    # Keep warning messages stable and deduplicated.
    deduped_warnings: list[str] = []
    seen_warnings: set[str] = set()
    for warning in warnings_out:
        msg = str(warning or "").strip()
        if not msg or msg in seen_warnings:
            continue
        seen_warnings.add(msg)
        deduped_warnings.append(msg)
    if deduped_warnings:
        config["warnings"] = deduped_warnings

    return {
        "id": summary["id"],
        "job_id": summary["job_id"],
        "strategy_name": summary["strategy_name"],
        "strategy_id": summary.get("strategy_id"),
        "lifecycle_strategy_id": summary.get("lifecycle_strategy_id"),
        "strategy_version": str(meta.get("strategy_version") or result_type),
        "symbol": summary["symbol"],
        "timeframe": summary["timeframe"],
        "created_at": summary["created_at"],
        "metrics": metrics,
        "config": config,
        "equity_curve": equity_curve,
        "benchmark_curve": benchmark_curve,
        "equity_curve_full": artifacts.get("equity_curve_full"),
        "benchmark_curve_full": artifacts.get("benchmark_curve_full"),
        "trades": trades,
        "result_type": result_type,
        "verdict": summary["verdict"],
        "description": summary.get("description", ""),
    }


def _get_backtest_result_deleted_ids(conn) -> set[str]:
    _backtest_trash_table(conn)
    rows = conn.execute("SELECT result_id FROM backtest_result_trash").fetchall()
    return {r["result_id"] for r in rows}


def _set_backtest_result_trash(conn, result_id: str, deleted: bool = True):
    if not result_id:
        return
    _backtest_trash_table(conn)
    if deleted:
        deleted_at = core._now()
        conn.execute(
            "INSERT OR REPLACE INTO backtest_result_trash (result_id, deleted_at) VALUES (?, ?)",
            (result_id, deleted_at),
        )
        try:
            conn.execute(
                "UPDATE backtest_results SET deleted_at = ? WHERE result_id = ?",
                (deleted_at, result_id),
            )
        except Exception:
            pass
    else:
        conn.execute("DELETE FROM backtest_result_trash WHERE result_id = ?", (result_id,))
        try:
            conn.execute(
                "UPDATE backtest_results SET deleted_at = NULL WHERE result_id = ?",
                (result_id,),
            )
        except Exception:
            pass


def _persist_backtest_result_row(
    *,
    result_id: str,
    strategy_id: str,
    result_type: str,
    symbol: str,
    timeframe: str,
    start_date: str | None,
    end_date: str | None,
    metrics: dict | None,
    config: dict | None,
    created_at: str | None = None,
) -> None:
    normalized_result_id = str(result_id or "").strip()
    normalized_strategy_id = str(strategy_id or "").strip()
    if not normalized_result_id:
        raise ValueError("result_id is required")
    if not normalized_strategy_id:
        raise ValueError("strategy_id is required")

    from forven.data_provenance import stamp_data_fingerprint
    from forven.engine_provenance import stamp_engine_version

    created_value = str(created_at or core._now()).strip() or core._now()
    start_value = str(start_date or "").strip() or None
    end_value = str(end_date or "").strip() or None
    result_type_value = str(result_type or "backtest").strip().lower() or "backtest"
    symbol_value = str(symbol or "").strip().upper()
    timeframe_value = str(timeframe or "").strip() or "1h"

    metrics_json = json.dumps(metrics or {}, separators=(",", ":"), default=str)
    # Provenance stamps: the ENGINE version that produced the numbers, and the
    # DATA semantics they were scored on (per-stream cadence + scale). Either
    # changing makes this artifact stale evidence (engine_provenance /
    # data_provenance module docstrings).
    stamped_config = stamp_data_fingerprint(
        stamp_engine_version(config), symbol_value, timeframe_value
    )
    if isinstance(metrics, dict) and metrics.get("execution_identity"):
        stamped_config.setdefault("execution_identity", metrics["execution_identity"])
    if isinstance(metrics, dict) and metrics.get("execution_contract"):
        stamped_config["execution_contract"] = metrics["execution_contract"]
    config_json = json.dumps(stamped_config, separators=(",", ":"), default=str)

    with core.get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backtest_results (
                result_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
                result_type TEXT NOT NULL DEFAULT 'backtest',
                symbol TEXT NOT NULL DEFAULT '',
                timeframe TEXT NOT NULL DEFAULT '1h',
                start_date TEXT,
                end_date TEXT,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                config_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
                deleted_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id,
                strategy_id,
                result_type,
                symbol,
                timeframe,
                start_date,
                end_date,
                metrics_json,
                config_json,
                created_at,
                deleted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(result_id) DO UPDATE SET
                strategy_id = excluded.strategy_id,
                result_type = excluded.result_type,
                symbol = excluded.symbol,
                timeframe = excluded.timeframe,
                start_date = excluded.start_date,
                end_date = excluded.end_date,
                metrics_json = excluded.metrics_json,
                config_json = excluded.config_json,
                created_at = excluded.created_at
            """,
            (
                normalized_result_id,
                normalized_strategy_id,
                result_type_value,
                symbol_value,
                timeframe_value,
                start_value,
                end_value,
                metrics_json,
                config_json,
                created_value,
            ),
        )


def _update_optimization_result_row(*, result_id: str, metrics: dict, config: dict) -> None:
    """Update an existing backtest_results row with final optimization data."""
    from forven.engine_provenance import stamp_engine_version

    metrics_json = json.dumps(metrics or {}, separators=(",", ":"), default=str)
    config_json = json.dumps(stamp_engine_version(config), separators=(",", ":"), default=str)
    with core.get_db() as conn:
        conn.execute(
            "UPDATE backtest_results SET metrics_json = ?, config_json = ? WHERE result_id = ?",
            (metrics_json, config_json, str(result_id).strip()),
        )


def _build_backtest_document(
    *,
    strategy_id: str,
    strategy_type: str,
    asset: str,
    metrics: dict,
) -> str:
    sharpe = _coerce_legacy_metadata_float(metrics.get("sharpe"), 0.0)
    total_return = _coerce_legacy_metadata_float(metrics.get("total_return_pct"), 0.0)
    if abs(total_return) <= 1.0:
        total_return *= 100.0
    win_rate = _coerce_legacy_metadata_float(metrics.get("win_rate"), 0.0)
    if abs(win_rate) <= 1.0:
        win_rate *= 100.0
    profit_factor = _coerce_legacy_metadata_float(metrics.get("profit_factor"), 0.0)
    max_drawdown = _coerce_legacy_metadata_float(metrics.get("max_drawdown_pct"), 0.0)
    if abs(max_drawdown) <= 1.0:
        max_drawdown *= 100.0
    return (
        f"Backtest {strategy_id} ({strategy_type}) on {asset}: "
        f"Sharpe={sharpe:.3f}, Return={total_return:.3f}%, "
        f"WinRate={win_rate:.2f}%, PF={profit_factor:.3f}, MaxDD={max_drawdown:.3f}%."
    )


def _ensure_result_data_dir() -> str:
    for existing in core._result_data_dirs():
        if existing:
            os.makedirs(existing, exist_ok=True)
            return existing
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    target = os.path.abspath(os.path.join(repo_root, "data", "results"))
    os.makedirs(target, exist_ok=True)
    return target


_BACKTEST_DISPLAY_EQUITY = 10_000.0


def _normalize_trade_artifact_rows(raw_rows: object) -> list[dict]:
    if not isinstance(raw_rows, list):
        return []
    normalized: list[dict] = []
    # Compound from a fixed $10k starting equity (matches TradingView's
    # default initial_capital=10000 + percent_of_equity=100). Each trade's
    # dollar PnL is sized off the equity at its entry, then equity compounds.
    equity = _BACKTEST_DISPLAY_EQUITY
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        # Engine emits `pnl_pct` as a ratio (0.0132 = +1.32%). Prefer the raw
        # ratio when present; treat a pre-existing `return_pct` field as the
        # same ratio shape for back-compat.
        ratio = _coerce_legacy_metadata_float(row.get("pnl_pct"), None)
        if ratio is None:
            ratio = _coerce_legacy_metadata_float(row.get("return_pct"), None)
        if ratio is None:
            ratio = 0.0
        pnl_raw = _coerce_legacy_metadata_float(row.get("pnl"), None)
        if pnl_raw is None:
            pnl_raw = equity * ratio
        trade_row: dict = {
            "entry_time": str(row.get("entry_time") or row.get("entry_ts") or ""),
            "entry_price": _coerce_legacy_metadata_float(row.get("entry_price"), 0.0),
            "exit_time": str(row.get("exit_time") or row.get("exit_ts") or row.get("entry_time") or ""),
            "exit_price": _coerce_legacy_metadata_float(row.get("exit_price"), 0.0),
            "size": _coerce_legacy_metadata_float(row.get("size"), 1.0),
            "pnl": _coerce_legacy_metadata_float(pnl_raw, 0.0),
            "return_pct": _coerce_legacy_metadata_float(ratio * 100.0, 0.0),
            # Preserve the raw engine ratio so the read-side normalizer keeps the
            # exact per-trade return rather than re-deriving it from price.
            "pnl_pct": _coerce_legacy_metadata_float(row.get("pnl_pct"), ratio),
        }
        # Carry through descriptive fields (only when present) so the result
        # viewer can show direction / hold time / MAE-MFE and the manual
        # backtester's exit reason + position size_fraction. `regime` is the
        # kernel's causal entry-bar label — per-regime analysis needs it to
        # survive persistence (2026-07-05 graveyard audit re-classified 35k
        # trades from candles because it was dropped here).
        for key in ("direction", "exit_reason", "regime"):
            if row.get(key) not in (None, ""):
                trade_row[key] = str(row[key])
        if row.get("bars_held") not in (None, ""):
            trade_row["bars_held"] = int(_coerce_legacy_metadata_float(row.get("bars_held"), 0.0))
        for key in ("mae", "mfe", "size_fraction"):
            if row.get(key) not in (None, ""):
                trade_row[key] = _coerce_legacy_metadata_float(row.get(key))
        normalized.append(trade_row)
        equity = max(0.0, equity * (1.0 + ratio))
    return normalized


def _build_backtest_chart_context_payload(
    *,
    result_id: str,
    asset: str,
    timeframe: str,
    start_date: str | None,
    end_date: str | None,
    strategy_name: str,
    strategy_type: str | None,
    strategy_params: dict | None,
    trades: object,
    warnings: list[str] | None = None,
) -> dict | None:
    try:
        from forven.strategies import backtest as backtest_mod

        payload = backtest_mod.build_backtest_chart_context(
            asset=asset,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            strategy_name=strategy_name,
            strategy_type=strategy_type,
            strategy_params=strategy_params if isinstance(strategy_params, dict) else {},
            trades=trades,
            extra_warnings=warnings or [],
        )
    except Exception as exc:
        log.warning("Failed to build backtest chart context for %s: %s", result_id, exc)
        return None

    normalized = _normalize_backtest_chart_context_payload(payload)
    if normalized is None:
        return None
    normalized["result_id"] = result_id
    return normalized


def _write_backtest_result_artifacts(
    result_id: str,
    job_id: str,
    trades: object,
    equity_curve: list | None = None,
    benchmark_curve: list | None = None,
    equity_curve_full: list | None = None,
    benchmark_curve_full: list | None = None,
):
    target_dir = core._ensure_result_data_dir()

    rows = _normalize_trade_artifact_rows(trades)
    if rows:
        payload = json.dumps(rows, separators=(",", ":"))
        for key in (result_id, job_id):
            safe_key = _safe_result_artifact_key(key)
            path = os.path.join(target_dir, f"{safe_key}_trades.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(payload)

    if equity_curve and isinstance(equity_curve, list) and len(equity_curve) > 0:
        eq_payload = json.dumps(equity_curve, separators=(",", ":"))
        for key in (result_id, job_id):
            safe_key = _safe_result_artifact_key(key)
            path = os.path.join(target_dir, f"{safe_key}_equity.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(eq_payload)

    if benchmark_curve and isinstance(benchmark_curve, list) and len(benchmark_curve) > 0:
        bm_payload = json.dumps(benchmark_curve, separators=(",", ":"))
        for key in (result_id, job_id):
            safe_key = _safe_result_artifact_key(key)
            path = os.path.join(target_dir, f"{safe_key}_benchmark.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(bm_payload)

    # Full-window (IS+OOS) curves for the entire-timeframe equity chart, persisted
    # alongside the OOS-only curves above (which back the OOS metrics/heatmap).
    if equity_curve_full and isinstance(equity_curve_full, list) and len(equity_curve_full) > 0:
        eqf_payload = json.dumps(equity_curve_full, separators=(",", ":"))
        for key in (result_id, job_id):
            safe_key = _safe_result_artifact_key(key)
            path = os.path.join(target_dir, f"{safe_key}_equity_full.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(eqf_payload)

    if benchmark_curve_full and isinstance(benchmark_curve_full, list) and len(benchmark_curve_full) > 0:
        bmf_payload = json.dumps(benchmark_curve_full, separators=(",", ":"))
        for key in (result_id, job_id):
            safe_key = _safe_result_artifact_key(key)
            path = os.path.join(target_dir, f"{safe_key}_benchmark_full.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(bmf_payload)


def _write_backtest_chart_artifacts(result_id: str, job_id: str, chart_context: object):
    normalized = _normalize_backtest_chart_context_payload(chart_context)
    if normalized is None:
        return
    normalized["result_id"] = result_id
    target_dir = core._ensure_result_data_dir()
    payload = json.dumps(normalized, separators=(",", ":"))
    for key in (result_id, job_id):
        safe_key = _safe_result_artifact_key(key)
        path = os.path.join(target_dir, f"{safe_key}_chart.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload)
