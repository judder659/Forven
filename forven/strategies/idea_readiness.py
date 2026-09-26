"""Read-only, pre-generation input checks shared by Creator and candidate agents.

Natural-language detection is deliberately limited to known feed names. A pass
does not certify arbitrary prose, history coverage, signal quality or tradability.
"""
from __future__ import annotations

import re
import importlib

_ALIASES = {
    "funding_rate": r"funding(?:[ _-]rate)?",
    "open_interest": r"open[ _-]interest",
    "ls_ratio": r"(?:long[ /_-]short[ _-]ratio|ls_ratio)",
    "taker_buy_sell_ratio": r"taker(?:[ _-](?:buy[ /_-]sell[ _-]ratio|volume))?",
    "long_liq_usd": r"(?:liquidations?|long_liq_usd|liq_imbalance|short_liq_usd)",
    "basis": r"(?:futures[ _-]basis|basis)",
    "iv_btc": r"(?:iv_btc|btc[ _-](?:dvol|implied volatility))",
    "iv_eth": r"(?:iv_eth|eth[ _-](?:dvol|implied volatility))",
}
_EXTERNAL = {
    "Coin days destroyed": r"(?:coin[ -]days[ -]destroyed|CDD)",
    "HODL waves": r"HODL[ -]waves?",
    "ETF flows": r"ETF[ -](?:net[ -])?(?:flows?|inflows?|outflows?)",
    "VIX": r"VIX",
    "Prediction-market quotes": r"(?:Polymarket|Kalshi)",
    "Options strike-level positioning": r"(?:options?\s+(?:OI|open[ _-]interest)|strike[ -]level\s+OI|max[ -]pain|put[ /-]call\s+OI)",
    "Liquidation heatmaps": r"(?:liquidation[ -](?:cluster[ -])?heatmaps?|Coinglass|Laevitas)",
    "Cross-asset frame joins": r"(?:open_interest_(?:alt|btc|eth)|close_(?:eth|btc)|ETH/BTC\s+ratio|funding[ -]spread|cross[ -]sectional\s+(?:funding|rank))",
    "Aggregate stablecoin supply": r"(?:stablecoin[ -](?:supply|supply[ -]ratio)|stablecoin_supply|SSR\s+oscillator)",
}


def detected_inputs(description: str) -> tuple[list[str], list[str]]:
    # Explicit exclusions should not create requirements. Ambiguous prose still
    # needs human review; this is not a language-model dependency parser.
    clauses = re.split(r"[.;\n]|\bbut\b", description, flags=re.I)
    positive = " ".join(c for c in clauses if not re.search(
        r"\b(?:without|ignore|exclude|do not use|don't use|no need for)\b", c, re.I
    ))
    def matches(pattern: str) -> bool:
        return bool(re.search(r"(?<!\w)(?:" + pattern + r")(?!\w)", positive, re.I))
    return ([key for key, pattern in _ALIASES.items() if matches(pattern)],
            [key for key, pattern in _EXTERNAL.items() if matches(pattern)])


def check_idea_readiness(description: str, symbol: str | None, timeframe: str | None,
                         *, visual: bool = False) -> dict:
    _present_columns = importlib.import_module("forven.strategies.data_availability")._present_columns

    symbol = None if str(symbol or "").strip().lower() in {"", "unspecified", "unknown", "tbd"} else symbol
    timeframe = None if str(timeframe or "").strip().lower() in {"", "unspecified", "unknown", "tbd"} else timeframe

    required, external = detected_inputs(description)
    issues: list[str] = []
    warnings = ["Checks named inputs and local file availability only. Backtesting must still verify usable history, coverage and costs."]
    present: list[str] = []
    if external:
        # These inputs have no mapping in the shared backtest feed catalog.
        # Do not spend a generation call inventing an unverified substitute.
        issues.append("Cannot verify an input integration for: " + ", ".join(external)
                      + ". Integrate these inputs or explicitly revise the idea before retrying. Do not substitute price or funding proxies.")
    if visual:
        _RAW_COLUMNS = importlib.import_module("forven.strategies.builtin.rule_engine")._RAW_COLUMNS
        unsupported = sorted(set(required) - _RAW_COLUMNS)
        if unsupported:
            issues.append("The visual builder cannot express these inputs: " + ", ".join(unsupported) + ". Use a reviewed custom strategy.")
    if not symbol or not timeframe:
        warnings.append("No single market/timeframe is specified; dataset availability has not been checked.")
    else:
        try:
            data = importlib.import_module("forven.data")
            parquet_path, tail_path = data.parquet_path, data.tail_path
            import pyarrow.parquet as pq

            paths = [p for p in (parquet_path(symbol, timeframe), tail_path(symbol, timeframe)) if p.exists()]
            if not paths or not any(pq.read_metadata(p).num_rows > 0 for p in paths):
                issues.append(f"No local candles for {symbol} / {timeframe}. Collect them on the Data page, then retry.")
            if required:
                present = sorted(set(required) & _present_columns(symbol, timeframe))
                missing = sorted(set(required) - set(present))
                if missing:
                    issues.append("Local inputs not found: " + ", ".join(missing) + ". Check their collection on the Data page, then retry.")
        except Exception:
            issues.append("Could not verify local data. Check data health and retry; no generation was started.")
    return {"status": "blocked" if issues else "review" if external or not symbol or not timeframe else "checked",
            "can_generate": not issues, "symbol": symbol, "timeframe": timeframe,
            "required": required + external, "present": present, "issues": issues, "warnings": warnings}


def candidate_readiness(task: dict, input_data: dict) -> dict:
    """Use the stored thesis, never infer a market from a default ticker."""
    import json
    get_db = importlib.import_module("forven.db").get_db

    description = str(task.get("description") or task.get("title") or "")
    symbols = [input_data["symbol"]] if input_data.get("symbol") else []
    timeframes = [input_data["timeframe"]] if input_data.get("timeframe") else []
    hypothesis_id = input_data.get("hypothesis_id")
    declared_issues: list[str] = []
    if hypothesis_id:
        with get_db() as conn:
            row = conn.execute("SELECT market_thesis,mechanism,target_assets,target_timeframes,feasibility FROM hypotheses WHERE id=? OR display_id=?", (hypothesis_id, hypothesis_id)).fetchone()
        if row:
            description += "\n" + str(row["market_thesis"] or "") + "\n" + str(row["mechanism"] or "")
            for key, current in (("target_assets", symbols), ("target_timeframes", timeframes)):
                try:
                    values = json.loads(row[key])
                except (TypeError, ValueError):
                    values = []
                if not current and isinstance(values, list):
                    current.extend(v for v in values if isinstance(v, str))
            try:
                declared = json.loads(row["feasibility"]) if row["feasibility"] else None
            except (TypeError, ValueError):
                declared = None
            # Runtime needs the idea declared (cross-asset / multi-timeframe
            # joins, unintegrated inputs) block development until they exist.
            declared_issues = importlib.import_module("forven.hypotheses").feasibility_issues(declared)
    # A single candidate uses one execution frame. Multi-target hypotheses may
    # name several frames, but prose and external reference features are not frames.
    canonical_market_symbol = importlib.import_module("forven.dataeng.coverage").canonical_market_symbol

    unresolved = []
    canonical = []
    for symbol in symbols:
        raw = str(symbol).strip()
        if raw.lower() in {"unspecified", "unknown", "tbd"} or not re.fullmatch(r"[A-Za-z0-9./:_-]+", raw):
            unresolved.append(raw)
        else:
            canonical.append(canonical_market_symbol(re.sub(r"[-_]PERP$", "", raw, flags=re.I)))
    valid_tfs = [tf.strip() for tf in timeframes if re.fullmatch(r"[1-9][0-9]*[mhdwM]", tf.strip())]
    unresolved.extend(tf for tf in timeframes if tf.strip() not in valid_tfs)
    canonical = list(dict.fromkeys(canonical))
    valid_tfs = list(dict.fromkeys(valid_tfs))
    if unresolved or not canonical or not valid_tfs or len(canonical) * len(valid_tfs) > 16:
        report = check_idea_readiness(description, None, None)
        report["issues"].append("Resolve executable markets and candle timeframes before development (up to 16 market/timeframe combinations). Keep holding horizons and external references in the mechanism. Unresolved: " + ", ".join(unresolved or ["market or timeframe not selected"]))
        report["issues"].extend(declared_issues)
        report.update(status="blocked", can_generate=False)
        return report
    reports = [check_idea_readiness(description, symbol, tf) for symbol in canonical for tf in valid_tfs]
    report = dict(reports[0])
    report["datasets"] = [{"symbol": r["symbol"], "timeframe": r["timeframe"], "status": r["status"]} for r in reports]
    report["issues"] = list(dict.fromkeys([*(issue for r in reports for issue in r["issues"]), *declared_issues]))
    report["can_generate"] = not report["issues"]
    report["status"] = "checked" if report["can_generate"] else "blocked"
    return report


def render_research_input_constraints() -> str:
    """Current feed capabilities for research, independent of old workspace notes."""
    _KNOWN_COLUMNS = importlib.import_module("forven.strategies.data_availability")._KNOWN_COLUMNS

    return (
        "# EXECUTABLE RESEARCH REQUIREMENTS\n"
        "Prioritize hypotheses that can be tested with the current dataset inventory. "
        "Use query_data or inspect_data_schema to verify the exact market, timeframe, "
        "required columns, freshness and usable history before calling an idea executable. "
        "A collector's existence or a feed name in DATA_SCHEMA.md is not proof of coverage.\n"
        "Strategy-visible derivatives columns: " + ", ".join(sorted(_KNOWN_COLUMNS)) + ".\n"
        "No verified strategy-frame integration for: " + ", ".join(_EXTERNAL) + ". "
        "Do not build ideas that depend on these, and do not substitute another input. "
        "The current frame contains one asset; a basket or cross-asset feature needs an "
        "implemented join.\n"
        "target_assets must contain executable market symbols only. target_timeframes "
        "must contain candle intervals such as 1h or 4h only. Put holding horizons, "
        "event timing and external reference series in the mechanism. If unavailable "
        "inputs are essential, select a different testable hypothesis for development."
    )
