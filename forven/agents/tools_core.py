"""Core shared agent tool handlers (shell, files, datasets)."""

import asyncio
import json
import os
import shlex
import signal
import subprocess

from forven.security.env_allowlist import build_subprocess_env
from forven.workspace import append_workspace, read_workspace
from .context import (
    _current_agent_id_var,
    _current_task_display_id_var,
    _normalize_legacy_paths,
)
from .tool_registry import register_tool


_SHELL_TOOL_TIMEOUT_SECONDS = 120
_SHELL_TOOL_CONCURRENCY = 2
_shell_tool_semaphore: asyncio.Semaphore | None = None


# H-S3: program-level denylist applied via shlex token analysis.
# Catches dangerous binaries even when buried in pipelines. Lowercase basename match.
_PROGRAM_DENYLIST = frozenset({
    # Network listeners / reverse-shell capable utilities
    "nc", "ncat", "netcat", "telnet", "socat",
    # Disk / partition / filesystem destruction
    "mkfs", "mkfs.ext2", "mkfs.ext3", "mkfs.ext4", "mkfs.btrfs", "mkfs.xfs",
    "mkfs.fat", "mkfs.vfat", "mkfs.ntfs", "wipefs", "fdisk", "parted",
    "sgdisk", "shred", "blkdiscard",
    # Privilege escalation
    "sudo", "doas", "su", "runas",
    # Account / credential mutation
    "passwd", "chpasswd", "useradd", "userdel", "usermod", "groupadd",
    # Offensive security tools
    "nmap", "masscan", "hydra", "hashcat", "john", "metasploit", "msfconsole",
    "sqlmap", "responder", "mimikatz",
})

# H-S3: opt-in strict allowlist. When FORVEN_SHELL_STRICT_ALLOWLIST=1, only
# commands whose first token is in this set are permitted. Default is off so
# existing agent flows (npm check, pytest, git, etc.) keep working.
_PROGRAM_ALLOWLIST = frozenset({
    # File listing / reading
    "dir", "ls", "cat", "type", "head", "tail", "less", "more",
    "grep", "rg", "egrep", "fgrep", "find", "fd",
    "select-string", "get-childitem", "get-content", "get-item",
    # Echo / shell builtins safe enough for read-only ops
    "echo", "pwd", "cd", "whoami", "date", "uname", "hostname", "id", "env",
    "true", "false", "test", "sleep",
    # Text processing
    "wc", "sort", "uniq", "awk", "sed", "tr", "cut", "tee",
    "jq", "yq", "diff", "patch",
    # Source control
    "git", "gh", "hg",
    # Python
    "python", "python3", "py", "pip", "pip3", "pipx", "uv", "poetry",
    "pytest", "ruff", "mypy", "black", "isort", "flake8",
    # Node ecosystem
    "node", "npm", "npx", "pnpm", "yarn", "bun",
    "tsc", "vite", "svelte-kit", "vitest", "eslint", "prettier",
    # Process info (read-only)
    "ps", "tasklist", "top", "htop",
    # Network read-only
    "ping", "nslookup", "dig", "host", "tracert", "traceroute",
    # Archives (read)
    "tar", "unzip", "gunzip", "zcat",
})


def _shell_strict_allowlist_enabled() -> bool:
    return str(os.environ.get("FORVEN_SHELL_STRICT_ALLOWLIST", "")).strip().lower() in {"1", "true", "yes", "on"}


def _shell_tool_enabled() -> bool:
    """Whether the LLM-driven shell tool is permitted to run at all.

    DISABLED by default for every build. The combination of subprocess
    execution + LLM tool-calling + web-ingested research content is the worst
    prompt-injection surface in the app, so an operator must opt in explicitly.
    """
    return str(os.environ.get("FORVEN_ENABLE_SHELL_TOOL", "")).strip().lower() in {"1", "true", "yes", "on"}


def _program_basename(token: str) -> str:
    """Strip path components and lowercase a program token for matching."""
    if not token:
        return ""
    name = token.replace("\\", "/").split("/")[-1].strip().lower()
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    return name


def _scan_program_tokens(command: str) -> list[str]:
    """Return lowercase basenames of every token that looks like an executable
    (i.e., the first token of each pipeline/command segment)."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return []
    if not tokens:
        return []
    programs: list[str] = []
    expect_program = True
    # Treat these as command separators that begin a new program
    separators = {"|", "||", "&&", ";", "&"}
    for tok in tokens:
        if tok in separators:
            expect_program = True
            continue
        if expect_program:
            programs.append(_program_basename(tok))
            expect_program = False
    return programs


def _get_shell_tool_semaphore() -> asyncio.Semaphore:
    global _shell_tool_semaphore
    if _shell_tool_semaphore is None:
        _shell_tool_semaphore = asyncio.Semaphore(_SHELL_TOOL_CONCURRENCY)
    return _shell_tool_semaphore


def _windows_command_uses_unix_head(command: str) -> bool:
    if os.name != "nt":
        return False
    return any(program == "head" for program in _scan_program_tokens(command))


async def _kill_shell_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate a shell command and any child processes it spawned."""
    pid = getattr(proc, "pid", None)
    if not pid:
        return

    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass


@register_tool(
    name="run_shell",
    description=(
        "Execute a shell command and return stdout/stderr. This runtime is Windows, "
        "so prefer cmd/PowerShell syntax such as dir, type, Get-ChildItem, and "
        "Select-String instead of Unix commands like ls or find. Max 120s timeout."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to execute"},
        },
        "required": ["command"],
    },
    is_async=True,
)
async def _tool_run_shell(command: str) -> str:
    """Execute a shell command with timeout."""
    # The run_shell tool is DISABLED by default for every build — the
    # combination of subprocess execution, LLM tool-calling, and web-ingested
    # research content is the worst prompt-injection surface we have. An
    # operator must opt in with FORVEN_ENABLE_SHELL_TOOL=1 (and should also set
    # FORVEN_SHELL_STRICT_ALLOWLIST=1). See security audit 2026-04-23.
    if not _shell_tool_enabled():
        return (
            "Blocked: the run_shell tool is disabled by default. Set "
            "FORVEN_ENABLE_SHELL_TOOL=1 to enable it (at your own risk; consider "
            "also FORVEN_SHELL_STRICT_ALLOWLIST=1). Use structured tools "
            "(read_file, write_file, etc.) instead."
        )
    command = command.replace("\n", " ").strip()
    command = _normalize_legacy_paths(command)

    # P2-T07: Tirith-style structured scan. Fail-closed on critical, log
    # high/medium for after-the-fact review. Strict mode (operator opt-in
    # via sandbox.shell_guard_strict=true) upgrades all tiers to block.
    try:
        from forven.sandbox.shell_guard import evaluate_for_run_shell
        allowed, shell_report = evaluate_for_run_shell(command)
        if not allowed:
            top = shell_report.findings[0] if shell_report.findings else None
            return (
                f"Blocked by shell_guard ({shell_report.severity}): "
                f"{top.message if top else 'critical pattern'}"
            )
    except Exception:  # noqa: BLE001 — fail-open by design
        pass

    # Safety: block destructive and dangerous commands
    blocked_exact = ["rm -rf /", "mkfs", "dd if=", "> /dev/", ":(){ :|:& };:"]
    blocked_patterns = [
        # Reverse shells and network exfiltration
        "/dev/tcp/", "/dev/udp/", "bash -i", "sh -i",
        "nc -e", "ncat -e", "nc -l", "ncat -l",
        # Piped downloads (curl/wget to shell)
        "curl|", "curl |", "wget|", "wget |",
        "curl -o-|", "wget -O-|",
        # Python/perl/ruby reverse shells
        "python -c", "python3 -c", "perl -e", "ruby -e",
        # System destruction
        "rm -rf /", "rm -rf /*", "chmod -R 777 /",
        "chown -R", "mkfs.", "wipefs",
        # Credential theft
        "/etc/shadow", "/etc/passwd",
        # Fork bombs and resource exhaustion
        ":()", ".bashrc", ".bash_profile",
        # Disable safety
        "--no-preserve-root",
    ]
    cmd_lower = command.lower()
    for b in blocked_exact:
        if b in command:
            return f"Blocked: dangerous command pattern '{b}'"
    for b in blocked_patterns:
        if b in cmd_lower:
            return f"Blocked: dangerous command pattern '{b}'"

    # H-S3: structured token analysis. Reject any pipeline segment whose program
    # is in the denylist, regardless of substring obfuscation.
    programs = _scan_program_tokens(command)
    for prog in programs:
        if prog in _PROGRAM_DENYLIST:
            return f"Blocked: program '{prog}' is denylisted (H-S3)"

    if _windows_command_uses_unix_head(command):
        return (
            "Blocked: Unix `head` pipelines leak subprocesses on Windows in this runtime. "
            "Use PowerShell `Select-Object -First N` or a structured tool instead."
        )

    # H-S3: optional strict allowlist mode. Off by default for backward
    # compatibility; opt in via FORVEN_SHELL_STRICT_ALLOWLIST=1.
    if _shell_strict_allowlist_enabled() and programs:
        for prog in programs:
            if prog and prog not in _PROGRAM_ALLOWLIST:
                return (
                    f"Blocked: program '{prog}' not in strict allowlist "
                    f"(H-S3 strict mode). Set FORVEN_SHELL_STRICT_ALLOWLIST=0 to disable."
                )

    # CONSOLE-2: CREATE_NO_WINDOW alongside the process group — the supervised
    # backend is console-detached, so a shell child without it pops a visible
    # console window per command. Never DETACHED_PROCESS (overrides NO_WINDOW).
    creationflags = (
        (subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        if os.name == "nt" else 0
    )
    start_new_session = os.name != "nt"
    async with _get_shell_tool_semaphore():
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=build_subprocess_env(),
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=_SHELL_TOOL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await _kill_shell_process_tree(proc)
            return f"Command timed out after {_SHELL_TOOL_TIMEOUT_SECONDS} seconds"
        except asyncio.CancelledError:
            await _kill_shell_process_tree(proc)
            raise

    output = stdout.decode()[:5000]
    if stderr.decode().strip():
        output += f"\nSTDERR: {stderr.decode()[:2000]}"
    if proc.returncode != 0:
        output += f"\nExit code: {proc.returncode}"
    return output or "(no output)"

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
    description="List OHLCV datasets available in the local Forven storage. Returns symbol, timeframe, and row count.",
    input_schema={
        "type": "object",
        "properties": {
            "symbol_filter": {"type": "string", "description": "Optional filter by symbol e.g. 'BTC'"},
        },
        "required": [],
    },
)
def _tool_list_local_datasets(symbol_filter: str | None = None) -> str:
    """List OHLCV datasets available in local Forven storage."""
    from forven.data import scan_datasets
    datasets = scan_datasets()
    if symbol_filter:
        s = symbol_filter.upper()
        datasets = [d for d in datasets if s in d["symbol"].upper()]
    
    if not datasets:
        return "No local datasets found."
    
    lines = ["Local datasets:"]
    for d in datasets:
        lines.append(f"- {d['symbol']} {d['timeframe']} ({d['row_count']} rows) | {d['start_ts'][:10]} to {d['end_ts'][:10]}")
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
    try:
        # Max limit 1000 for safety
        requested_limit = max(min(int(limit or 100), 1000), 1)
        result = dataset_ohlcv(symbol, timeframe, limit=requested_limit)
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
