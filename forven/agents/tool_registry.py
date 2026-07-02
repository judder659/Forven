"""Declarative tool registry — single source of truth for agent tools.

Tools self-register via the ``@register_tool`` decorator placed on their
handler functions.  The registry replaces the manual elif dispatch chain
and the separate static definition lists that previously lived in
``tool_definitions.py``.

Usage::

    from forven.agents.tool_registry import register_tool

    @register_tool(
        name="my_tool",
        description="Does a thing.",
        input_schema={"type": "object", "properties": {...}, "required": [...]},
        permissions={"brain"},      # default {"*"} = all agents
        is_async=True,              # default False
        run_in_thread=True,         # default True (ignored when is_async=True)
    )
    def _tool_my_tool(param_a: str, param_b: int = 5) -> str:
        ...

Handler signatures are auto-adapted: the registry inspects parameter names
and extracts matching keys from the AI's ``tool_input`` dict, so handlers
keep clean, typed signatures.
"""

import asyncio
import gzip
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .context import (
    _current_agent_id_var,
    _current_task_display_id_var,
    _current_tools_context_var,
)
from forven.db import get_db
from forven.redact import redact

# ---------------------------------------------------------------------------
# Tool output discipline (Hermes-inspired Phase 0)
# ---------------------------------------------------------------------------
# Caps applied AFTER redaction, BEFORE the agent sees the output. The full
# (post-redaction) output is gzip-persisted to tool_truncations for "expand
# from DB" UI. Per-tool overrides are read from ToolDef.output_caps when set.
DEFAULT_MAX_BYTES = 50 * 1024  # 50 KB
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_CHARS_PER_LINE = 2000
TRUNCATION_LINE_MARKER = "…[truncated]"

log = logging.getLogger("forven.agents.tool_registry")

# Memory tools backed by the in-process ChromaDB vector store. When ChromaDB is
# disabled (FORVEN_DISABLE_CHROMA_IN_PROCESS — a deliberate guard against an ONNX
# segfault on some GPUs), these are no-ops: searches always return empty and the
# store_* tools previously reported FALSE success while persisting nothing. They
# are gated OUT of the advertised toolset while the vector layer is unavailable,
# so agents don't waste calls (or build on memory they think was saved). The gate
# is on live availability, so they auto-return if ChromaDB is ever re-enabled.
_CHROMA_BACKED_TOOL_NAMES: frozenset[str] = frozenset(
    {"search_memory", "store_memory", "search_chroma", "store_chroma"}
)


def _chroma_memory_unavailable() -> bool:
    """True when ChromaDB-backed memory tools should be hidden (vector layer off)."""
    try:
        from forven.vectordb import _in_process_chroma_disabled

        return _in_process_chroma_disabled()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Registry data structures
# ---------------------------------------------------------------------------

@dataclass
class ToolDef:
    """A registered tool: metadata + async dispatcher."""

    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]  # adapted async (dict) -> str
    permissions: frozenset[str] = field(default_factory=lambda: frozenset({"*"}))
    # Phase 5 / P5-T05: tool category for per-context override matching.
    # Defaults to 'general'. MCP tools registered via mcp_router set
    # category='mcp'; research / exchange / destructive tools set their own.
    category: str = "general"


_REGISTRY: dict[str, ToolDef] = {}

# Phase 5 / P5-T05: per-context default-OFF rules. Most tool calls in any
# context are allowed by default; the rules below carve out contexts where
# a category should be denied unless an explicit override re-enables it.
_CONTEXT_DEFAULT_DENY: dict[str, frozenset[str]] = {
    # Scheduled (cron) routines should not be running open-ended research
    # without explicit operator approval — research tools may have their own
    # rate limits and side effects. Catastrophic tools (factory_reset) have no
    # legitimate autonomous use: a prompt-injected string in any agent output
    # the brain reads must never be able to wipe the pipeline DB.
    "scheduled": frozenset({"research", "catastrophic", "codegen"}),
    # Recovery context (post-failure retry) should not run destructive tools
    # like archive/delete.
    "recovery": frozenset({"destructive", "catastrophic", "codegen"}),
    # Research context: ingests the most untrusted content; never catastrophic,
    # and never arbitrary code execution (audit 2026-06-22, H4) — a prompt-injected
    # research page must not be able to reach run_code / raw-code writes.
    "research": frozenset({"catastrophic", "codegen"}),
    # Code-authoring autonomous tasks (develop_candidate) legitimately need codegen
    # (register_strategy) AND research (read the operator-seeded hypothesis
    # artifact), so a full default-deny would break strategy authoring. But a
    # prompt-injected develop task must never reach a catastrophic primitive
    # (factory_reset wipes the pipeline DB), so deny that. The real containment for
    # the inject→register→execute chain is the AST guard (P1.2) + out-of-process
    # execution (Phase 2, see the 2026-06 security-hardening plan). (audit P1.1)
    "develop": frozenset({"catastrophic"}),
}

VALID_CONTEXTS: tuple[str, ...] = ("scheduled", "interactive", "recovery", "research", "develop")

# Canonical role tokens come from the roster (execution-trader retired
# 2026-06-30 — deliberately absent so no agent can be granted its tools).
from forven.roster import CANONICAL_AGENT_ROLES as _CANONICAL_AGENT_ROLES


def _role_tokens_from_text(value: object) -> set[str]:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return set()

    direct = normalized.replace(" ", "-")
    tokens: set[str] = set()
    if direct in _CANONICAL_AGENT_ROLES:
        tokens.add(direct)

    alias_checks = {
        "brain": ("brain", "orchestrator"),
        "full-stack-engineer": ("full stack engineer", "full-stack-engineer"),
        "quant-researcher": ("quant researcher", "quant-researcher"),
        "risk-manager": ("risk manager", "risk-manager"),
        "simulation-agent": ("simulation agent", "simulation-agent"),
        "strategy-developer": ("strategy developer", "strategy-developer"),
    }
    for canonical_role, phrases in alias_checks.items():
        if any(phrase in normalized for phrase in phrases):
            tokens.add(canonical_role)

    semantic_checks = {
        "strategy-developer": (
            "generate market hypotheses",
            "hypothesis-to-strategy",
            "testable strategy container logic",
        ),
        "quant-researcher": (
            "market structure",
            "benchmark external ideas",
            "data-gap discovery",
        ),
    }
    for canonical_role, phrases in semantic_checks.items():
        if any(phrase in normalized for phrase in phrases):
            tokens.add(canonical_role)

    return tokens


def _permission_subjects(agent_id: str | None) -> frozenset[str | None]:
    subjects: set[str | None] = {agent_id}
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        subjects.add(None)
        return frozenset(subjects)
    try:
        with get_db() as conn:
            row = conn.execute("SELECT name, role FROM agents WHERE id = ?", (normalized_agent_id,)).fetchone()
    except Exception:
        row = None
    if row:
        for token in _role_tokens_from_text(row["name"]):
            subjects.add(f"role:{token}")
        for token in _role_tokens_from_text(row["role"]):
            subjects.add(f"role:{token}")
    # Phase 4 / P4-T05: MCP server grants — explicit per-agent only.
    # The `*` wildcard is never added here; ``mcp:<server>`` subjects must
    # be granted by row in ``agent_mcp_grants``.
    try:
        with get_db() as conn:
            grants = conn.execute(
                "SELECT server_name FROM agent_mcp_grants WHERE agent_id = ?",
                (normalized_agent_id,),
            ).fetchall()
            for grant in grants:
                server = str(grant["server_name"] or "").strip()
                if server:
                    subjects.add(f"mcp:{server}")
    except Exception:
        pass
    return frozenset(subjects)


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def register_tool(
    name: str,
    description: str,
    input_schema: dict,
    *,
    permissions: set[str] | None = None,
    is_async: bool = False,
    run_in_thread: bool = True,
    category: str = "general",
) -> Callable:
    """Decorator that registers a tool handler in the global registry.

    The original function is returned *unchanged* so it can still be called
    directly in tests or by other code.

    ``category`` (Phase 5 / P5-T05) drives per-context filtering. Common
    values: ``general`` (default), ``research``, ``exchange``, ``destructive``,
    ``mcp``. Per-context overrides reference category via ``category:<name>``.
    """
    perms = frozenset(permissions) if permissions else frozenset({"*"})

    def decorator(fn: Callable) -> Callable:
        adapter = _build_adapter(fn, is_async=is_async, run_in_thread=run_in_thread)

        _REGISTRY[name] = ToolDef(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=adapter,
            permissions=perms,
            category=str(category or "general"),
        )
        return fn  # return original — not the adapter

    return decorator


# ---------------------------------------------------------------------------
# Adapter builder (maps dict → function args automatically)
# ---------------------------------------------------------------------------

def _build_adapter(
    fn: Callable,
    *,
    is_async: bool,
    run_in_thread: bool,
) -> Callable[[dict], Any]:
    """Return an ``async def adapter(params: dict) -> str`` that calls *fn*."""
    sig = inspect.signature(fn)
    params_list = list(sig.parameters.values())

    # Detect signature shape
    takes_single_dict = (
        len(params_list) == 1
        and params_list[0].name in ("params", "payload", "tool_input", "tool_payload")
    )
    takes_no_args = len(params_list) == 0

    # Choose the right calling convention + threading wrapper
    if is_async:
        if takes_single_dict:
            async def _a(p: dict) -> str:
                return await fn(p)
        elif takes_no_args:
            async def _a(p: dict) -> str:
                return await fn()
        else:
            async def _a(p: dict) -> str:
                return await fn(**_extract_kwargs(sig, p))
    elif run_in_thread:
        if takes_single_dict:
            async def _a(p: dict) -> str:
                return await asyncio.to_thread(fn, p)
        elif takes_no_args:
            async def _a(p: dict) -> str:
                return await asyncio.to_thread(fn)
        else:
            async def _a(p: dict) -> str:
                kw = _extract_kwargs(sig, p)
                return await asyncio.to_thread(fn, **kw)
    else:
        # Sync handler called in-loop (no thread). Rare — only if explicitly
        # marked ``run_in_thread=False, is_async=False``.
        if takes_single_dict:
            async def _a(p: dict) -> str:
                return fn(p)
        elif takes_no_args:
            async def _a(p: dict) -> str:
                return fn()
        else:
            async def _a(p: dict) -> str:
                return fn(**_extract_kwargs(sig, p))

    return _a


def _extract_kwargs(sig: inspect.Signature, params: dict) -> dict:
    """Extract keyword arguments from *params* based on *sig*."""
    kwargs: dict[str, Any] = {}
    for pname, param in sig.parameters.items():
        if pname in params:
            kwargs[pname] = params[pname]
        # else: use the function's default (or let TypeError bubble if required)
    return kwargs


# ---------------------------------------------------------------------------
# Public query / dispatch API
# ---------------------------------------------------------------------------

def _load_toolset_overrides(agent_id: str | None, context: str) -> dict[str, bool]:
    """Return ``{rule_key: enabled}`` map for (agent_id, context).

    Rule keys can be exact tool names, ``mcp:<server>``, or ``category:<cat>``.
    """
    if not agent_id:
        return {}
    out: dict[str, bool] = {}
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT tool_name, enabled FROM agent_toolset_overrides "
                "WHERE agent_id = ? AND context = ?",
                (str(agent_id), str(context)),
            ).fetchall()
        for row in rows:
            key = str(row["tool_name"] or "").strip()
            if key:
                out[key] = bool(int(row["enabled"]))
    except Exception:
        return {}
    return out


def _resolve_tool_enabled(
    tool: ToolDef,
    overrides: dict[str, bool],
    context: str,
) -> bool:
    """Override-resolution order: exact name > mcp:<server> > category:<cat> > default."""
    if tool.name in overrides:
        return overrides[tool.name]

    if tool.name.startswith("mcp_"):
        # MCP tools are registered as ``mcp_<server>__<tool>``.
        # Allow either ``mcp:<server>`` or ``mcp:*`` rules.
        try:
            after_prefix = tool.name[len("mcp_"):]
            server = after_prefix.split("__", 1)[0]
        except Exception:
            server = ""
        if server and f"mcp:{server}" in overrides:
            return overrides[f"mcp:{server}"]
        if "mcp:*" in overrides:
            return overrides["mcp:*"]

    cat_key = f"category:{tool.category}"
    if cat_key in overrides:
        return overrides[cat_key]

    # Default: deny if context's default-deny set lists this category.
    deny_set = _CONTEXT_DEFAULT_DENY.get(context, frozenset())
    if tool.category in deny_set:
        return False
    return True


def get_tools_for_agent(
    agent_id: str | None,
    context: str | None = None,
) -> list[dict]:
    """Return Anthropic-format tool definitions available to *agent_id*.

    Phase 5 / P5-T05: ``context`` filters via ``agent_toolset_overrides``
    when one of ``scheduled|interactive|recovery|research``. ``None`` (default)
    keeps the legacy permission-only behavior.
    """
    result: list[dict] = []
    subjects = _permission_subjects(agent_id)
    overrides: dict[str, bool] = {}
    use_context = context in VALID_CONTEXTS
    if use_context:
        overrides = _load_toolset_overrides(agent_id, context or "")
    hide_chroma = _chroma_memory_unavailable()
    for tool in _REGISTRY.values():
        if hide_chroma and tool.name in _CHROMA_BACKED_TOOL_NAMES:
            continue
        if "*" not in tool.permissions and not any(
            subject in tool.permissions for subject in subjects
        ):
            continue
        if use_context and not _resolve_tool_enabled(tool, overrides, context or ""):
            continue
        result.append({
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        })
    return result


def filter_tools_for_context(
    tools: list[dict],
    agent_id: str | None,
    context: str | None,
) -> list[dict]:
    """Drop tools whose category is denied in *context* (Phase 5 / P5-T05).

    Operates on an existing list of Anthropic-format tool dicts (``{name,
    description, input_schema}``) — used by callers that build a static tool
    union (e.g. the Brain executor) rather than going through
    ``get_tools_for_agent``. Applies the same override-resolution as
    ``_resolve_tool_enabled`` (exact > mcp > category > context default-deny).

    No-op when *context* is None or not one of ``VALID_CONTEXTS``. Tool names
    not present in the registry are kept (fail open on unknown names).
    """
    hide_chroma = _chroma_memory_unavailable()
    if context not in VALID_CONTEXTS:
        # Still drop dead chroma-backed memory tools even without a context.
        return [e for e in tools if not (hide_chroma and e.get("name") in _CHROMA_BACKED_TOOL_NAMES)]
    overrides = _load_toolset_overrides(agent_id, context or "")
    out: list[dict] = []
    for entry in tools:
        if hide_chroma and entry.get("name") in _CHROMA_BACKED_TOOL_NAMES:
            continue
        tool = _REGISTRY.get(entry.get("name"))
        if tool is None or _resolve_tool_enabled(tool, overrides, context or ""):
            out.append(entry)
    return out


def compute_effective_toolset(
    agent_id: str | None,
    context: str,
) -> list[dict]:
    """Return ``[{name, category, enabled, source}]`` for every tool the
    agent has BASE permission for, with the per-context override applied.

    ``source`` is ``"override:<rule_key>"`` when an override fired,
    ``"default-deny"`` when context default-deny applied,
    or ``"default-allow"`` otherwise. Used by the toolset-matrix UI preview.
    """
    if context not in VALID_CONTEXTS:
        return []
    subjects = _permission_subjects(agent_id)
    overrides = _load_toolset_overrides(agent_id, context)
    deny_set = _CONTEXT_DEFAULT_DENY.get(context, frozenset())
    out: list[dict] = []
    for tool in _REGISTRY.values():
        if "*" not in tool.permissions and not any(
            subject in tool.permissions for subject in subjects
        ):
            continue

        # Determine the matching rule + result.
        source = "default-allow"
        enabled = True
        if tool.name in overrides:
            enabled = overrides[tool.name]
            source = f"override:tool:{tool.name}"
        elif tool.name.startswith("mcp_"):
            after_prefix = tool.name[len("mcp_"):]
            server = after_prefix.split("__", 1)[0] if "__" in after_prefix else ""
            if server and f"mcp:{server}" in overrides:
                enabled = overrides[f"mcp:{server}"]
                source = f"override:mcp:{server}"
            elif "mcp:*" in overrides:
                enabled = overrides["mcp:*"]
                source = "override:mcp:*"
            elif f"category:{tool.category}" in overrides:
                enabled = overrides[f"category:{tool.category}"]
                source = f"override:category:{tool.category}"
            elif tool.category in deny_set:
                enabled = False
                source = "default-deny"
        elif f"category:{tool.category}" in overrides:
            enabled = overrides[f"category:{tool.category}"]
            source = f"override:category:{tool.category}"
        elif tool.category in deny_set:
            enabled = False
            source = "default-deny"

        out.append({
            "name": tool.name,
            "category": tool.category,
            "enabled": enabled,
            "source": source,
        })
    out.sort(key=lambda x: (x["category"], x["name"]))
    return out


def list_tool_categories() -> list[str]:
    """Return distinct tool categories registered in the global registry."""
    cats = sorted({tool.category for tool in _REGISTRY.values()})
    return cats


def set_tool_category(tool_name: str, category: str) -> bool:
    """Re-categorize a registered tool. Returns True if the tool exists."""
    tool = _REGISTRY.get(tool_name)
    if tool is None:
        return False
    tool.category = str(category or "general")
    return True


# Phase 5 / P5-T05: post-registration categorization patterns.
# Names matching one of these prefixes get the listed category. Applied by
# ``apply_default_categorization`` which is called once at app startup
# (after all @register_tool decorators have run).
_DEFAULT_CATEGORY_PATTERNS: list[tuple[str, str]] = [
    # MCP tools (registered via mcp_router) — already namespaced.
    ("mcp_", "mcp"),
    # Research / external-source ingest.
    ("discover_", "research"),
    ("inspect_", "research"),
    ("ingest_url", "research"),
    ("research_", "research"),
    # Exchange / market data. The agent order/position tools were deleted
    # outright (agents have NO order path); only market-data tools remain.
    # The generic prefixes match nothing today but fail-safe future tools.
    ("fetch_exchange_data", "exchange"),
    ("get_local_ohlcv", "exchange"),
    ("exchange_", "exchange"),
    ("get_market_data", "exchange"),
    ("get_ohlcv", "exchange"),
    ("get_ticker", "exchange"),
    # Codegen: arbitrary-code-execution / raw-code-write / strategy-registration
    # tools (audit 2026-06-22 H4; extended 2026-06-28 P1.1). Denied in
    # research/scheduled/recovery so an untrusted-content-driven agent cannot reach
    # an arbitrary-Python primitive. register_strategy / *_register_strategy_file
    # take a raw `code`/file argument that is imported IN-PROCESS, so they ARE
    # codegen. The autonomous develop_candidate flow runs in the 'develop' context
    # (which does NOT deny codegen), so tagging them here closes the
    # research/scheduled/recovery registration hole without breaking authoring.
    ("run_code", "codegen"),
    ("deepdive_write_strategy_code", "codegen"),
    ("register_strategy", "codegen"),
    ("forven_register_strategy_file", "codegen"),
    ("assistant_register_strategy_file", "codegen"),
    # Catastrophic: tools with NO legitimate autonomous use. factory_reset
    # wipes the entire pipeline DB (strategies, trades, settings, logs) — it
    # must only ever run from an operator-interactive context. Kept separate
    # from 'destructive' because archive_/transition_stage ARE legitimate in
    # autonomous cycles.
    ("factory_reset", "catastrophic"),
    # Destructive / lifecycle-altering (the generic prefixes match nothing
    # today but are kept for forward-compat).
    ("archive_", "destructive"),
    ("delete_", "destructive"),
    ("transition_stage", "destructive"),
    ("kill_", "destructive"),
]


def apply_default_categorization() -> int:
    """Walk the registry and assign categories from the prefix table.

    Returns the count of tools updated. Idempotent — running twice is fine.
    Tools whose @register_tool already specified a non-'general' category
    are left alone.
    """
    updated = 0
    for tool in _REGISTRY.values():
        if tool.category != "general":
            continue
        for prefix, cat in _DEFAULT_CATEGORY_PATTERNS:
            if tool.name.startswith(prefix):
                tool.category = cat
                updated += 1
                break
    return updated


def _persist_truncation(
    *,
    tool_name: str,
    task_display_id: str | None,
    agent_id: str | None,
    original_bytes: int,
    truncated_bytes: int,
    original_lines: int,
    truncated_lines: int,
    redaction_count: int,
    cap_fired: str,
    full_output: str,
) -> int | None:
    """Insert a tool_truncations row with gzip-compressed full output.

    Returns the inserted row's id, or None if the write fails (the cap-fired
    output still flows back to the agent — persistence failure is non-fatal).
    """
    try:
        compressed = gzip.compress(full_output.encode("utf-8"), compresslevel=6)
        with get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO tool_truncations (
                    task_display_id, agent_id, tool_name,
                    original_bytes, truncated_bytes,
                    original_lines, truncated_lines,
                    redaction_count, cap_fired, full_output
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_display_id,
                    agent_id,
                    tool_name,
                    int(original_bytes),
                    int(truncated_bytes),
                    int(original_lines),
                    int(truncated_lines),
                    int(redaction_count),
                    cap_fired,
                    compressed,
                ),
            )
            return int(cur.lastrowid) if cur.lastrowid else None
    except Exception as exc:
        log.warning("tool_truncations persist failed: %s", exc)
        return None


def _process_tool_output(
    raw_output: Any,
    *,
    tool_name: str,
    task_display_id: str | None,
    agent_id: str | None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_lines: int = DEFAULT_MAX_LINES,
    max_chars_per_line: int = DEFAULT_MAX_CHARS_PER_LINE,
) -> str:
    """Redact secrets, then apply byte/line/per-line caps. Persist full output
    if any cap fired and append a footer pointing at the persisted row.

    Order is significant: redact FIRST so that any secret bytes near the end
    of a 100KB output are scrubbed in the persisted full_output too — not
    just the visible portion.
    """
    text = raw_output if isinstance(raw_output, str) else str(raw_output)

    # Step 1: redact (always — even if no truncation happens).
    redacted, redaction_count = redact(text)

    original_bytes = len(redacted.encode("utf-8"))
    raw_lines = redacted.split("\n")
    original_lines_count = len(raw_lines)
    chars_cap_fired = any(len(line) > max_chars_per_line for line in raw_lines)

    # Step 2: per-line truncation.
    if chars_cap_fired:
        capped_lines = [
            (line[:max_chars_per_line] + TRUNCATION_LINE_MARKER)
            if len(line) > max_chars_per_line
            else line
            for line in raw_lines
        ]
    else:
        capped_lines = raw_lines

    # Step 3: line-count cap.
    lines_cap_fired = original_lines_count > max_lines
    if lines_cap_fired:
        capped_lines = capped_lines[:max_lines]

    candidate = "\n".join(capped_lines)
    candidate_bytes = candidate.encode("utf-8")

    # Step 4: total byte cap.
    bytes_cap_fired = len(candidate_bytes) > max_bytes
    if bytes_cap_fired:
        candidate = candidate_bytes[:max_bytes].decode("utf-8", errors="ignore")
        candidate_bytes_len = len(candidate.encode("utf-8"))
    else:
        candidate_bytes_len = len(candidate_bytes)

    cap_fired_flags: list[str] = []
    if bytes_cap_fired:
        cap_fired_flags.append("bytes")
    if lines_cap_fired:
        cap_fired_flags.append("lines")
    if chars_cap_fired:
        cap_fired_flags.append("chars_per_line")

    if cap_fired_flags:
        truncation_id = _persist_truncation(
            tool_name=tool_name,
            task_display_id=task_display_id,
            agent_id=agent_id,
            original_bytes=original_bytes,
            truncated_bytes=candidate_bytes_len,
            original_lines=original_lines_count,
            truncated_lines=len(candidate.split("\n")),
            redaction_count=redaction_count,
            cap_fired=",".join(cap_fired_flags),
            full_output=redacted,
        )
        ref = (
            f"tool_truncations.id={truncation_id}"
            if truncation_id is not None
            else "persistence_failed"
        )
        footer = (
            f"\n\n---\n[output truncated: {','.join(cap_fired_flags)} cap fired; "
            f"{original_bytes} bytes \u2192 {candidate_bytes_len} bytes; "
            f"full output stored as {ref}]"
        )
        return candidate + footer

    return candidate


async def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Dispatch a tool call — replaces the old elif chain in runner.py.

    Handles permission gating, heartbeat logging, execution, duration
    tracking, and audit logging.
    """
    tool_payload = tool_input if isinstance(tool_input, dict) else {}
    current_agent_id = _current_agent_id_var.get()
    current_task_display_id = _current_task_display_id_var.get()
    started = time.monotonic()
    result = ""

    try:
        # ── Heartbeat ──
        agent_source = f"agent:{current_agent_id or 'unknown'}"
        heartbeat_message = f"Using tool: {tool_name}..."
        # Append a preview of the first string argument for debugging.
        for v in tool_payload.values():
            if isinstance(v, str) and v.strip():
                heartbeat_message = f"{heartbeat_message} {v.replace(chr(10), ' ').strip()[:180]}"
                break
        try:
            from forven.db import log_activity
            log_activity("heartbeat", agent_source, heartbeat_message)
        except Exception:
            pass

        # ── Lookup ──
        tool = _REGISTRY.get(tool_name)
        if not tool:
            result = f"Unknown tool: {tool_name}"
        else:
            # ── Permission check ──
            subjects = _permission_subjects(current_agent_id)
            current_context = _current_tools_context_var.get()
            if "*" not in tool.permissions and not any(subject in tool.permissions for subject in subjects):
                result = (
                    f"Permission denied: only "
                    f"{', '.join(sorted(p for p in tool.permissions if p is not None))} "
                    f"can use '{tool_name}'"
                )
            elif (
                current_context in VALID_CONTEXTS
                and not _resolve_tool_enabled(
                    tool, _load_toolset_overrides(current_agent_id, current_context or ""), current_context or ""
                )
            ):
                # Defense in depth: even if a denied tool slips into the model's
                # tool list, refuse to dispatch it in this context. List-hiding
                # alone is not an authorization boundary.
                result = (
                    f"Tool '{tool_name}' (category '{tool.category}') is disabled "
                    f"in the '{current_context}' context."
                )
            else:
                try:
                    result = await asyncio.wait_for(tool.handler(tool_payload), timeout=120)
                except asyncio.TimeoutError:
                    result = f"Tool '{tool_name}' timed out after 120s"

    except Exception as e:
        result = f"Tool error: {e}"

    # ── Redact + truncate (Hermes-inspired Phase 0) ──
    # Applied to whatever string `result` ended up as — including error
    # paths, permission denials, and successful tool returns. Errors are
    # the most likely vector for accidental secret leakage (stack traces
    # quoting env vars), so redaction is unconditional.
    try:
        result = _process_tool_output(
            result,
            tool_name=tool_name,
            task_display_id=current_task_display_id,
            agent_id=current_agent_id,
        )
    except Exception as exc:
        log.warning("tool output post-processing failed for %s: %s", tool_name, exc)

    # ── Audit log ──
    duration_ms = int((time.monotonic() - started) * 1000)
    if current_task_display_id:
        try:
            from forven.db import log_tool_call
            log_tool_call(
                current_task_display_id,
                current_agent_id,
                tool_name,
                tool_payload,
                str(result)[:500],
                duration_ms,
            )
        except Exception as exc:
            log.debug("Task tool audit log skipped for %s: %s", current_task_display_id, exc)

    return result
