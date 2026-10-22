"""Core shared agent tool handlers (files, datasets)."""

import json

from forven.workspace import append_workspace, read_workspace
from .context import (
    _current_agent_id_var,
    _current_task_display_id_var,
)
from .tool_registry import register_tool


@register_tool(
    name="read_file",
    description="Read a file from the Forven workspace (~/.forven/workspace/). Provide path relative to workspace root.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to workspace root, e.g. 'LESSONS.md' or 'memory/2026-02-19.md'"},
        },
        "required": ["path"],
    },
)
def _tool_read_file(path: str) -> str:
    """Read a workspace file."""
    # H-S8: hardened path validation (catches symlink escapes too)
    from forven.workspace import WorkspacePathError, safe_workspace_path
    try:
        safe_workspace_path(path)
    except WorkspacePathError as exc:
        return f"Error: {exc}"
    content = read_workspace(path, optional=True)
    if content is None:
        return f"File not found: {path}"
    return content[:10000]

# Allow-list for write_file tool (P1 pre-beta security): LLM may only write
# to these workspace locations. Anything else is rejected even if the path
# resolves safely inside the workspace. Keep this list narrow — only paths the
# agents actually need for memory/lessons/role updates.
_WRITE_FILE_ALLOWED_FILES = frozenset({
    "LESSONS.md",
    "evolution_journal.md",
})
_WRITE_FILE_ALLOWED_PREFIXES: tuple[str, ...] = (
    "memory/",
    "agents/",
    "narratives/",
    "post_mortems/",
    "lessons/",
    "notes/",
)
_WRITE_FILE_ALLOWED_SUFFIXES: tuple[str, ...] = (".md", ".txt", ".json")


def _write_file_allowed(path: str) -> bool:
    """Return True if the LLM is permitted to write to this workspace path."""
    norm = path.replace("\\", "/").lstrip("/")
    if not norm.endswith(_WRITE_FILE_ALLOWED_SUFFIXES):
        return False
    if norm in _WRITE_FILE_ALLOWED_FILES:
        return True
    return any(norm.startswith(prefix) for prefix in _WRITE_FILE_ALLOWED_PREFIXES)


@register_tool(
    name="write_file",
    description=(
        "Write or append to a file in the Forven workspace (~/.forven/workspace/). "
        "Use for updating memory, lessons, evolution journal. "
        "Writable paths are restricted to: LESSONS.md, evolution_journal.md, "
        "memory/*, agents/*, narratives/*, post_mortems/*, lessons/*, notes/* "
        "(must end in .md, .txt, or .json)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to workspace root"},
            "content": {"type": "string", "description": "Content to write"},
            "append": {"type": "boolean", "description": "If true, append instead of overwrite. Default: true"},
        },
        "required": ["path", "content"],
    },
)
def _tool_write_file(path: str, content: str, append: bool = True) -> str:
    """Write or append to a workspace file."""
    # H-S8: hardened path validation (catches symlink escapes too)
    from forven.workspace import WorkspacePathError, safe_workspace_path
    try:
        safe_workspace_path(path)
    except WorkspacePathError as exc:
        return f"Error: {exc}"
    # Block editing core identity files
    protected = {"SOUL.md", "USER.md", "AGENTS.md", "BACKUPS.md"}
    if path in protected:
        return f"Error: {path} is protected. Only the Brain can edit it with Judder's permission."
    # P1 pre-beta allow-list: reject paths outside the sanctioned areas.
    if not _write_file_allowed(path):
        return (
            f"Error: {path} is not a writable path for the agent. "
            "Allowed: LESSONS.md, evolution_journal.md, or files under "
            "memory/, agents/, narratives/, post_mortems/, lessons/, notes/ "
            "with extension .md/.txt/.json."
        )
    # Post-write verification via the SAME read path agents use: the tool's
    # return value is ground truth, not an assumption. Agents spent a week
    # cross-checking unreliable success/FAILED signals against re-reads
    # (2026-07-06 reports: appends that "succeeded" but never showed up on
    # re-read because the divergent-roots resolution hid them).
    from forven.workspace import read_workspace

    if append:
        append_workspace(path, content)
        after = read_workspace(path, optional=True) or ""
        if content and content.strip() and content.strip() not in after:
            return (
                f"Error: append to {path} did not persist "
                "(post-write verification could not find the content on re-read)"
            )
        return f"Appended {len(content)} chars to {path} (file now {len(after)} chars, verified)"
    else:
        from forven.workspace import write_workspace
        write_workspace(path, content)
        after = read_workspace(path, optional=True) or ""
        if after != content:
            return (
                f"Error: write to {path} did not persist "
                f"(post-write verification read {len(after)} chars, expected {len(content)})"
            )
        return f"Wrote {len(content)} chars to {path} (verified)"

@register_tool(
    name="list_local_datasets",
    description=(
        "List the OHLCV datasets Forven has: symbol, timeframe, rows, date range and the "
        "dataset_id that forven_run_backtest, forven_run_optimization and forven_run_verdict take."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol_filter": {"type": "string", "description": "Optional filter by symbol e.g. 'BTC'"},
            "timeframe_filter": {"type": "string", "description": "Optional exact timeframe e.g. '4h'"},
        },
        "required": [],
    },
)
def _tool_list_local_datasets(symbol_filter: str | None = None, timeframe_filter: str | None = None) -> str:
    """List the dataset catalog (the same one dataset ids resolve against)."""
    import importlib

    from forven.dataeng.dead_markets import drop_dead_datasets

    # The catalog in forven.api_domains.data (import cycle with api_core, so
    # read through importlib as forven.backtest_dataset_ids does) also covers a
    # remote data engine when one is configured.
    rows = importlib.import_module("forven.api_domains.data").get_datasets_stub()
    # Markets whose data stopped for good are left out: nothing built on them can be tested.
    datasets = drop_dead_datasets([dict(row) for row in rows or [] if isinstance(row, dict)])
    if symbol_filter:
        s = symbol_filter.upper()
        datasets = [d for d in datasets if s in str(d.get("symbol") or "").upper()]
    if timeframe_filter:
        datasets = [d for d in datasets if str(d.get("timeframe") or "").strip() == timeframe_filter.strip()]

    if not datasets:
        return "No local datasets found."

    lines = ["Local datasets (dataset_id in brackets):"]
    for d in datasets:
        lines.append(
            f"- {d.get('symbol')} {d.get('timeframe')} [{d.get('id')}] ({d.get('row_count')} rows) | "
            f"{str(d.get('start_ts') or '')[:10]} to {str(d.get('end_ts') or '')[:10]}"
        )
    return "\n".join(lines)

@register_tool(
    name="fetch_exchange_data",
    description=(
        "Fetch OHLCV historical data from a CCXT-supported exchange and store it locally as a parquet dataset. "
        "Use this BEFORE backtesting to ensure the data you need is available. "
        "Supported exchanges: binance (default), bybit, okx, coinbase, kraken, bitfinex, kucoin. "
        "Data is automatically cached — subsequent calls for the same symbol/timeframe merge new bars."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Trading pair e.g. 'BTC/USDT', 'ETH/USDT', 'SOL/USDT'"},
            "timeframe": {"type": "string", "description": "Candle interval: 1m, 5m, 15m, 1h, 4h, 1d (default 1h)"},
            "exchange": {"type": "string", "description": "Exchange to fetch from: binance, bybit, okx, coinbase, kraken (default: binance)"},
            "bars": {"type": "integer", "description": "Number of bars to fetch (default 1000, max 50000)"},
        },
        "required": ["symbol"],
    },
)
def _tool_fetch_exchange_data(params: dict) -> str:
    """Fetch OHLCV data from a CCXT exchange and store locally."""
    from forven.data import fetch_ohlcv_chunked

    symbol = params.get("symbol", "")
    if not symbol:
        return "Error: 'symbol' is required (e.g., 'BTC/USDT')"

    timeframe = params.get("timeframe", "1h")
    exchange = params.get("exchange", "binance")
    bars = min(max(int(params.get("bars", 1000)), 1), 50000)

    try:
        result = fetch_ohlcv_chunked(
            symbol=symbol,
            timeframe=timeframe,
            exchange_id=exchange,
            limit=bars,
        )
        payload = {
            "status": "success",
            "symbol": result.get("symbol", symbol),
            "timeframe": result.get("timeframe", timeframe),
            "exchange": exchange,
            "total_rows": result.get("row_count", 0),
            "bars_fetched": result.get("bars_fetched", 0),
            "bars_new": result.get("bars_new", 0),
            "date_range": f"{result.get('start_ts', '?')} to {result.get('end_ts', '?')}",
        }
        if result.get("warning"):
            # e.g. Kraken only serves the most-recent ~720 candles — tell the
            # agent so it doesn't assume it got full history.
            payload["warning"] = result.get("warning")
        return json.dumps(payload, indent=2)
    except RuntimeError as e:
        if "ccxt is not installed" in str(e):
            return "Error: ccxt library is not installed. Install with: pip install ccxt"
        return f"Error fetching data: {e}"
    except Exception as e:
        return f"Error fetching data from {exchange}: {e}"


@register_tool(
    name="get_local_ohlcv",
    description=(
        "Read OHLCV bars from a local dataset. Use for data analysis and strategy "
        "ideation. Set include_enrichment=true to join the derivative streams the "
        "strategy engine sees (funding_rate, open_interest, taker_buy_sell_ratio, "
        "ls_ratio, basis, iv_btc/iv_eth, liquidation columns) — plain calls return "
        "OHLCV only, which does NOT mean the enrichment data is missing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Symbol e.g. 'BTC/USDT' or 'BTC-USDT'"},
            "timeframe": {"type": "string", "description": "Timeframe e.g. '1h', '4h', '1d'"},
            "limit": {"type": "integer", "description": "Number of bars to retrieve (default 100, max 1000)"},
            "include_enrichment": {
                "type": "boolean",
                "description": (
                    "Join derivative enrichment columns (funding/OI/flow/basis/IV) "
                    "onto the bars, as the strategy engine does. Default false."
                ),
            },
        },
        "required": ["symbol", "timeframe"],
    },
)
def _tool_get_local_ohlcv(
    symbol: str, timeframe: str, limit: int = 100, include_enrichment: bool = False
) -> str:
    """Read OHLCV bars from a local dataset, optionally enriched.

    Agents refused to mint OI/funding-keyed strategies because this tool's
    bare OHLCV response read as "enrichment columns don't exist" (2026-07-06
    H01622 report + data gap) — the columns join at ENGINE time via
    DataManager.enrich, they were just never surfaced on this read tool.
    """
    from forven.data import dataset_ohlcv
    from forven.research_contract import research_read_cutoff

    try:
        # Max limit 1000 for safety
        requested_limit = max(min(int(limit or 100), 1000), 1)
        # Research holdout: agents are research, so they read only up to the cutoff.
        cutoff = research_read_cutoff()
        result = dataset_ohlcv(symbol, timeframe, limit=requested_limit, before=cutoff)
        data = result.get("data", [])
        if not data:
            return f"No data found for {symbol} {timeframe}"

        enrichment_error = None
        if include_enrichment:
            try:
                import pandas as pd

                from forven.data_manager import data_manager

                frame = pd.DataFrame(data)
                enriched = data_manager.enrich(frame, symbol, timeframe)
                data = json.loads(enriched.to_json(orient="records", date_format="iso"))
            except Exception as exc:
                enrichment_error = str(exc)

        payload = {
            "symbol": result["symbol"],
            "timeframe": result["timeframe"],
            "row_count": result["row_count"],
            "bars": data,
        }
        if cutoff is not None:
            payload["research_holdout"] = (
                f"bars end before {cutoff.date().isoformat()}; later data is held back from research"
            )
        if include_enrichment:
            payload["enriched"] = enrichment_error is None
            if enrichment_error:
                payload["enrichment_error"] = enrichment_error
        return json.dumps(payload, indent=2)
    except FileNotFoundError:
        return f"Dataset not found: {symbol} {timeframe}. Use list_local_datasets to see what is available."
    except Exception as e:
        return f"Error loading OHLCV: {e}"


@register_tool(
    name="request_fix",
    description=(
        "Report a code-level bug you cannot resolve to the operator's triage queue. "
        "Use this when you encounter a bug, broken import, API error, or infrastructure issue "
        "that you cannot resolve with your own tools. It records the bug for human / Claude-Code "
        "review (a notification + the review log) — NO autonomous code change is made; the system "
        "is fixed through the normal dev workflow. Provide a clear description of the problem, what "
        "you tried, and what files/systems are affected."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short summary of the problem (shown in the operator triage queue)"},
            "description": {
                "type": "string",
                "description": "Detailed problem description: what failed, error messages, what you already tried, affected files/systems",
            },
            "severity": {
                "type": "string",
                "enum": ["low", "medium", "high", "critical"],
                "description": "Impact severity. Default: medium",
            },
            "context": {"type": "object", "description": "Optional context: error traces, file paths, strategy_id, etc."},
        },
        "required": ["title", "description"],
    },
)
def _tool_request_fix(params: dict) -> str:
    """Report a code-level problem to the operator bug-triage queue (report-only)."""
    title = str(params.get("title", "")).strip()
    description = str(params.get("description", "")).strip()
    if not title or not description:
        return "Error: both 'title' and 'description' are required."

    severity = str(params.get("severity", "medium")).strip().lower()
    if severity not in ("low", "medium", "high", "critical"):
        severity = "medium"

    context = params.get("context") or {}
    requesting_agent = _current_agent_id_var.get()
    requesting_task = _current_task_display_id_var.get()

    try:
        from forven.brain import escalate_to_engineer
        result = escalate_to_engineer(
            title=title,
            description=description,
            requesting_agent=requesting_agent,
            requesting_task_id=requesting_task,
            severity=severity,
            context=context if isinstance(context, dict) else {},
        )
        return json.dumps({
            "status": result.get("status", "reported"),
            "queue": result.get("queue", "operator_triage"),
            "message": (
                f"Bug reported to the operator triage queue (severity={severity}). "
                f"No autonomous code change is made; it will be fixed via the normal dev workflow."
            ),
        })
    except Exception as e:
        return f"Bug report failed: {e}"
