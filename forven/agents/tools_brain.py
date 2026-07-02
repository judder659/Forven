"""Brain orchestration tool handlers."""

import difflib
import json

from forven import brain_memory as _brain_memory
from forven.db import get_db
from .context import _current_agent_id_var, _current_task_display_id_var
from .tool_definitions import BRAIN_AGENT_IDS
from .tool_registry import register_tool
from forven.roster import normalize_agent_id as _normalize_agent_id


def _suggest_known_families(stype: str, *, n: int = 3) -> list[str]:
    """Return up to N existing families whose names are close to `stype`.

    Used to turn the flat "runtime type X has no registered class" rejection
    into a pointer toward the correct existing family, so agents stop minting
    new family names.
    """
    from forven.strategies.params import SUPPORTED_PARAM_FAMILIES

    normalized = str(stype or "").strip().lower()
    if not normalized:
        return []
    families = sorted(SUPPORTED_PARAM_FAMILIES)
    # Prefer close-name matches; fall back to substring overlap.
    close = difflib.get_close_matches(normalized, families, n=n, cutoff=0.5)
    if close:
        return close
    tokens = {t for t in normalized.replace("-", "_").split("_") if t}
    scored = []
    for fam in families:
        fam_tokens = set(fam.split("_"))
        overlap = len(tokens & fam_tokens)
        if overlap:
            scored.append((overlap, fam))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [fam for _, fam in scored[:n]]


def _current_brain_payload() -> dict:
    display_id = str(_current_task_display_id_var.get() or "").strip()
    if not display_id.startswith("B"):
        return {}
    try:
        brain_task_id = int(display_id[1:])
    except ValueError:
        return {}
    with get_db() as conn:
        row = conn.execute("SELECT payload FROM tasks WHERE id = ?", (brain_task_id,)).fetchone()
    payload = json.loads(row["payload"]) if row and isinstance(row["payload"], str) else (row["payload"] if row else {})
    return payload if isinstance(payload, dict) else {}


def _reject_bootstrap_quant_research_assignment(agent_id: str, task_type: str) -> str | None:
    if agent_id != "quant-researcher":
        return None
    if str(task_type or "").strip().lower() not in {"research", "general", "manual", "analysis"}:
        return None
    payload = _current_brain_payload()
    if str(payload.get("source") or "").strip().lower() != "bootstrap":
        return None
    return (
        "Bootstrap must begin with the strategy-developer swarm creating first-class hypotheses "
        "and immediate strategy candidates. Defer quant-researcher support research until after "
        "the first strategy-developer hypothesis wave is underway."
    )


@register_tool(
    name="assign_agent_task",
    description=(
        "Assign a task to one of your agents. Only the Brain can do this. "
        "Agents: quant-researcher, simulation-agent, strategy-developer, risk-manager, "
        "full-stack-engineer, brain. "
        "The agent will execute the task with tool access and report back."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": (
                    "Agent to assign to: quant-researcher, simulation-agent (backtest-engineer alias), strategy-developer, "
                    "risk-manager, full-stack-engineer, brain."
                ),
                "enum": BRAIN_AGENT_IDS,
            },
            "task_type": {
                "type": "string",
                "description": "Task category: research, backtest, risk_audit, execution, analysis",
            },
            "title": {"type": "string", "description": "Short task title (shown in dashboard)"},
            "description": {
                "type": "string",
                "description": "Detailed task description. Be specific about what the agent should do, what tools to use, and what output you expect.",
            },
        },
        "required": ["agent_id", "task_type", "title", "description"],
    },
    permissions={"brain", None},
)
def _tool_assign_agent_task(params: dict) -> str:
    """Assign a task to an agent (Brain-only tool)."""
    from forven.brain import assign_task

    agent_id = _normalize_agent_id(params["agent_id"])
    task_type = params.get("task_type", "general")
    title = params["title"]
    description = params["description"]
    strategy_id = params.get("strategy_id") or None
    input_data: dict | None = None

    bootstrap_guard = _reject_bootstrap_quant_research_assignment(agent_id, task_type)
    if bootstrap_guard:
        return bootstrap_guard

    # Preserve originating Discord channel context when Brain assigns tasks.
    # This keeps operator-visible updates in the same room (e.g. #general).
    try:
        display_id = str(_current_task_display_id_var.get() or "").strip()
        if display_id.startswith("B"):
            brain_task_id = int(display_id[1:])
            with get_db() as conn:
                row = conn.execute("SELECT payload FROM tasks WHERE id = ?", (brain_task_id,)).fetchone()
            payload = json.loads(row["payload"]) if row and isinstance(row["payload"], str) else (row["payload"] if row else {})
            if isinstance(payload, dict):
                channel = str(payload.get("channel") or "").strip()
                if channel:
                    input_data = {"_channel": channel}
        elif display_id.startswith("T"):
            agent_task_id = int(display_id[1:])
            with get_db() as conn:
                row = conn.execute("SELECT input_data FROM agent_tasks WHERE id = ?", (agent_task_id,)).fetchone()
            raw_input = row["input_data"] if row else None
            parsed_input = json.loads(raw_input) if isinstance(raw_input, str) else raw_input
            if isinstance(parsed_input, dict):
                channel = str(parsed_input.get("_channel") or "").strip()
                if channel:
                    input_data = {"_channel": channel}
    except Exception:
        pass

    # Verify agent exists
    with get_db() as conn:
        exists = conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
        available = [str(r["id"]) for r in conn.execute("SELECT id FROM agents ORDER BY id").fetchall()]
    if not exists:
        return (
            f"Agent '{agent_id}' not found. Available: "
            f"{', '.join(available)}"
        )

    task_id = assign_task(
        agent_id, task_type, title, description, input_data=input_data, strategy_id=strategy_id
    )

    # Link the created task to the Brain decision that spawned it (when the
    # runtime worker recorded one for this cycle) so decision → task → outcome
    # joins work.
    try:
        from forven.brain_decisions import get_active_decision_id, link_agent_task

        decision_id = get_active_decision_id()
        if decision_id and task_id:
            link_agent_task(int(task_id), int(decision_id))
    except Exception:
        pass

    return f"Task assigned to {agent_id}: {title}"


@register_tool(
    name="promote_strategy",
    description=(
        "Promote or retire a strategy to a new lifecycle status. "
        "Valid statuses: quick_screen, research_only, gauntlet, paper, live_graduated, archived, rejected."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy ID to promote/retire"},
            "new_status": {
                "type": "string",
                "description": "New status",
                "enum": ["quick_screen", "research_only", "gauntlet", "paper", "live_graduated", "archived", "rejected"],
            },
        },
        "required": ["strategy_id", "new_status"],
    },
    permissions={"brain", None},
)
def _tool_promote_strategy(params: dict) -> str:
    """Promote/retire a strategy (Brain-only tool)."""
    from forven.brain import promote_strategy

    strategy_id = params["strategy_id"]
    new_status = params["new_status"]

    success, msg = promote_strategy(strategy_id, new_status)
    if success:
        try:
            from forven.reporter import broadcast_agent_task
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(broadcast_agent_task(
                    "brain", f"🚀 Strategy Promoted: {strategy_id}", 
                    f"New Status: **{new_status}**"
                ))
        except Exception:
            pass
        return f"Strategy {strategy_id} promoted to {new_status}"
    return f"Strategy {strategy_id} promotion failed: {msg}"


@register_tool(
    name="create_strategy",
    description=(
        "Create a new strategy in the database with status 'quick_screen' by default. "
        "Any params your strategy needs are accepted — composite strategies mixing multiple indicator "
        "families are encouraged. Canonical param names get automatic alias resolution for chart overlays. "
        "Set research_only=true to store an experimental strategy outside the tradable pipeline."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Unique strategy ID (e.g., 'btc-rsi-momentum-v2')"},
            "hypothesis_id": {"type": "string", "description": "Parent hypothesis ID for this strategy."},
            "crucible_id": {"type": "string", "description": "Planner-approved crucible/hypothesis ID for this candidate."},
            "name": {"type": "string", "description": "Human-readable strategy name"},
            "strategy_type": {
                "type": "string",
                "description": (
                    "Strategy family name. Can be any pre-built family (e.g. stochastic, macd, rsi_momentum, "
                    "williams_r, donchian, bollinger, ema_cross) or a novel composite name. "
                    "Composite strategies mixing indicators from multiple families are encouraged."
                ),
            },
            "symbol": {"type": "string", "description": "Trading symbol: BTC, ETH, SOL"},
            "params": {"type": "object", "description": "Strategy parameters dict — any params your strategy needs are accepted"},
            "timeframe": {"type": "string", "description": "Timeframe: 1h, 4h, 1d (default: 1h)"},
            "notes": {"type": "string", "description": "Notes about the strategy hypothesis"},
            "research_only": {
                "type": "boolean",
                "description": "Store the strategy in the non-tradable research_only lane instead of quick_screen.",
            },
            "model": {"type": "string", "description": "AI provider that created this strategy (auto-detected if omitted)"},
            "model_id": {"type": "string", "description": "AI model ID that created this strategy (auto-detected if omitted)"},
        },
        "required": ["strategy_id", "hypothesis_id", "name", "strategy_type", "symbol", "params"],
    },
    permissions={"brain", None},
)
def _tool_create_strategy(params: dict) -> str:
    """Create a new strategy (Brain-only tool)."""
    from forven.brain import create_strategy
    from forven.ai import normalize_provider_and_model
    from forven.agents.tools_research import assert_hypothesis_spawn_allowed
    from forven.crucible_tasks import validate_candidate_strategy_creation
    from forven.strategies.certification import certify_execution_strategy

    crucible_id = str(params.get("crucible_id") or params.get("hypothesis_id") or "").strip()
    hypothesis_id = str(params.get("hypothesis_id") or crucible_id).strip()
    if not hypothesis_id:
        return "Error creating strategy: hypothesis_id is required for all new strategies."

    # Resolve which AI model is creating this strategy
    strat_model = params.get("model")
    strat_model_id = params.get("model_id")
    if not strat_model:
        # Auto-detect from the running agent's config
        agent_id = _current_agent_id_var.get()
        if agent_id:
            try:
                with get_db() as conn:
                    agent_row = conn.execute(
                        "SELECT model, model_id FROM agents WHERE id = ?",
                        (agent_id,),
                    ).fetchone()
                if agent_row:
                    strat_model = agent_row["model"] or "openai"
                    strat_model_id = agent_row["model_id"]
            except Exception:
                pass
    if strat_model:
        try:
            strat_model, strat_model_id = normalize_provider_and_model(
                strat_model, strat_model_id,
            )
        except Exception:
            pass

    research_only = bool(params.get("research_only"))
    agent_id = str(_current_agent_id_var.get() or "").strip()
    task_display_id = str(_current_task_display_id_var.get() or "").strip()
    validation = validate_candidate_strategy_creation(crucible_id, agent_id, task_display_id, hypothesis_id)
    if not validation.allowed:
        return f"Error creating strategy: {validation.reason}"
    crucible_id = str(validation.crucible_id or crucible_id).strip()
    hypothesis_id = str(validation.hypothesis_id or hypothesis_id).strip()

    certification = certify_execution_strategy(
        params.get("strategy_type"),
        params.get("params"),
    )
    certification_error = certification.format_error(context="creation")
    # Orphan runtime types are always rejected — even research_only can't use
    # them because nothing downstream can execute, optimize, or promote them.
    if certification.unregistered_runtime_type:
        requested = str(params.get("strategy_type") or "").strip()
        suggestions = _suggest_known_families(requested)
        suggestion_hint = (
            f" Closest existing families: {', '.join(suggestions)}."
            if suggestions else ""
        )
        return (
            f"Error creating strategy: runtime type '{requested}' has no "
            "registered class and is not a known param family. Pick an "
            "existing TYPE_NAME from SUPPORTED_PARAM_FAMILIES instead of "
            "inventing a new one — do not claim the strategy was created."
            + suggestion_hint
        )
    if certification_error and not research_only:
        return f"Error creating strategy: {certification_error}"

    try:
        assert_hypothesis_spawn_allowed(hypothesis_id)
    except ValueError as exc:
        return f"Error creating strategy: {exc}"

    result = create_strategy(
        strategy_id=params["strategy_id"],
        hypothesis_id=hypothesis_id,
        name=params["name"],
        strategy_type=params["strategy_type"],
        symbol=params["symbol"],
        params=certification.canonical_params,
        timeframe=params.get("timeframe", "1h"),
        notes=params.get("notes", ""),
        model=strat_model,
        model_id=strat_model_id,
        research_only=research_only,
        origin_crucible_id=crucible_id if agent_id else None,
        origin_agent_id=agent_id or None,
        origin_task_id=task_display_id or None,
        origin_model=strat_model_id or strat_model,
    )
    if not isinstance(result, dict):
        return "Error creating strategy: unexpected response"
    if result.get("error"):
        return f"Error creating strategy: {result['error']}"
    strategy_id = str(result.get("id") or "").strip()
    status = str(result.get("status") or "").strip()
    if not strategy_id or not status:
        return "Error creating strategy: incomplete response"
    return f"Strategy created: {strategy_id} (status: {status}, model: {strat_model}/{strat_model_id})"


@register_tool(
    name="factory_reset",
    description=(
        "Perform a factory reset of specific data categories. USE WITH EXTREME CAUTION. "
        "Categories: pipeline_data, agent_task_history, trade_history, activity_log, ai_memory, "
        "scheduler_jobs, settings, credentials, system_docs. "
        "Every category NOT listed in 'keep_categories' is wiped. If 'keep_categories' is "
        "omitted, only the default-keep categories are preserved (currently just 'credentials') "
        "and everything else is wiped."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "keep_categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of categories to KEEP (not wipe). E.g. ['credentials', 'settings']",
            },
        },
    },
    permissions={"brain", None},
)
def _tool_factory_reset(params: dict) -> str:
    """Perform a factory reset of specific data categories (Brain-only tool)."""
    from forven.db import factory_reset, FACTORY_RESET_CATEGORIES

    keep = params.get("keep_categories", None)
    if isinstance(keep, str):
        keep = [k.strip().lower() for k in keep.split(",") if k.strip()]
    if not keep:
        # Unspecified/empty from an agent keeps the safe default_keep set (e.g.
        # credentials) rather than wiping everything. Only the operator UI's
        # explicit, typed-confirmed all-unchecked path may pass [] to wipe all.
        keep = None

    # Validate categories
    all_valid = set(FACTORY_RESET_CATEGORIES.keys())
    invalid = [k for k in (keep or []) if k not in all_valid]
    if invalid:
        return f"Error: Invalid categories to keep: {', '.join(invalid)}. Valid: {', '.join(all_valid)}"

    try:
        result = factory_reset(keep_categories=keep)
        wiped = result.get("wiped", [])
        kept = result.get("kept", [])
        return f"Factory reset complete. Wiped: {', '.join(wiped) or 'nothing'}. Kept: {', '.join(kept) or 'nothing'}."
    except Exception as e:
        return f"Factory reset failed: {e}"


@register_tool(
    name="memory",
    description=(
        "Read or mutate the Brain's persistent operational memory (capped at "
        f"{_brain_memory.MAX_MEMORY_CHARS} chars). Brain-only. Actions: "
        "'view' (returns body + metadata), 'add' (append content with newline "
        "separator), 'replace' (overwrite full body), 'remove' (delete first "
        "occurrence of `needle`). Cap violations are returned as a structured "
        "error envelope, not raised. Use this for cross-cycle notes the Brain "
        "needs to remember; quant agents stay stateless."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["view", "add", "replace", "remove"],
                "description": "What to do with memory.",
            },
            "content": {
                "type": "string",
                "description": "Body for 'add' or 'replace'. Ignored for 'view' and 'remove'.",
            },
            "needle": {
                "type": "string",
                "description": "Substring to remove from the body. Required for 'remove'.",
            },
        },
        "required": ["action"],
    },
    permissions={"brain", None},
)
def _tool_memory(params: dict) -> str:
    """Read/mutate Brain memory (Brain-only tool)."""
    action = str(params.get("action") or "").strip().lower()
    actor = str(_current_agent_id_var.get() or "brain").strip() or "brain"

    try:
        if action == "view":
            return json.dumps({"ok": True, **_brain_memory.get_memory_with_meta()})

        if action == "add":
            content = str(params.get("content") or "")
            if not content:
                return json.dumps(
                    {"ok": False, "error": "missing_content", "action": action}
                )
            return json.dumps(_brain_memory.add_memory(content, mutated_by=actor))

        if action == "replace":
            content = str(params.get("content") or "")
            return json.dumps(_brain_memory.set_memory(content, mutated_by=actor))

        if action == "remove":
            needle = str(params.get("needle") or "")
            if not needle:
                return json.dumps(
                    {"ok": False, "error": "missing_needle", "action": action}
                )
            return json.dumps(_brain_memory.remove_memory_section(needle, mutated_by=actor))

        return json.dumps(
            {"ok": False, "error": "invalid_action", "action": action}
        )
    except _brain_memory.BrainMemoryTooLargeError as exc:
        return json.dumps(
            {
                "ok": False,
                "error": "memory_cap_exceeded",
                "current_chars": exc.current_len,
                "attempted_chars": exc.attempted_len,
                "cap": exc.cap,
            }
        )


@register_tool(
    name="recall_similar_situation",
    description=(
        "Brain-only hybrid recall over prior decisions and agent tasks. "
        "Returns a re-ranked list of matches plus a synthesized summary. "
        "Uses FTS5 for candidate retrieval and an auxiliary LLM to re-rank "
        "and summarize. Falls back to FTS5-only with empty summary if the "
        "auxiliary model is unreachable. Scope filters the source tables."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text query describing the situation to look up.",
            },
            "scope": {
                "type": "string",
                "enum": ["all", "decisions", "tasks"],
                "description": "Which source tables to search. Default: all.",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of hits to return after re-rank. Default: 5, max: 20.",
            },
        },
        "required": ["query"],
    },
    permissions={"brain", None},
)
def _tool_recall_similar_situation(params: dict) -> str:
    """Brain-only recall tool — wraps :func:`forven.recall.recall_similar_situation`."""
    from forven.recall import recall_similar_situation

    query = str(params.get("query") or "").strip()
    if not query:
        return json.dumps({"ok": False, "error": "missing_query"})

    scope_raw = str(params.get("scope") or "all").strip().lower()
    if scope_raw not in ("all", "decisions", "tasks"):
        scope_raw = "all"

    raw_limit = params.get("limit")
    if raw_limit is None:
        limit = 5
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 5
    limit = max(1, min(limit, 20))

    try:
        result = recall_similar_situation(query, scope=scope_raw, limit=limit)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": "recall_failed", "detail": str(exc)})

    return json.dumps({"ok": True, **result})


@register_tool(
    name="propose_skill_update",
    description=(
        "Brain-only. Propose a curated edit to a quant skill (description, "
        "what_works/what_doesnt_work bullets, metadata fields). The proposal is "
        "queued as a `skill_update_proposal` approval — the operator reviews and "
        "approves before the SKILL.md is re-written. Use this when Brain has "
        "synthesized new insight from outcome closure or recall and wants to "
        "persist it; do NOT bypass via direct file writes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Existing skill name (e.g. 'regime-trend-rsi').",
            },
            "proposed_description": {
                "type": "string",
                "description": "Optional new description text (omit to leave unchanged).",
            },
            "add_what_works": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Bullets to append to what_works (deduped).",
            },
            "add_what_doesnt_work": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Bullets to append to what_doesnt_work (deduped).",
            },
            "metadata_updates": {
                "type": "object",
                "description": "Shallow metadata overrides (e.g. {'regime': 'TRENDING'}). Cannot set confidence directly — that flows from outcome closure.",
            },
            "rationale": {
                "type": "string",
                "description": "Why this update is warranted — shown to the operator.",
            },
            "evidence_decisions": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Optional list of decision/task ids supporting the change.",
            },
        },
        "required": ["skill_name", "rationale"],
    },
    permissions={"brain", None},
)
def _tool_propose_skill_update(params: dict) -> str:
    """Queue a `skill_update_proposal` approval for a curated skill edit."""
    from forven import quant_skills as qs
    from forven.db import create_approval

    skill_name = qs._sanitize_name(str(params.get("skill_name") or "").strip())
    if not skill_name:
        return json.dumps({"ok": False, "error": "missing_skill_name"})

    skill = qs.read_skill(skill_name)
    if skill is None:
        return json.dumps({"ok": False, "error": "skill_not_found", "skill_name": skill_name})

    rationale = str(params.get("rationale") or "").strip()
    if not rationale:
        return json.dumps({"ok": False, "error": "missing_rationale"})

    add_works_raw = params.get("add_what_works") or []
    add_misses_raw = params.get("add_what_doesnt_work") or []
    if not isinstance(add_works_raw, list) or not isinstance(add_misses_raw, list):
        return json.dumps({"ok": False, "error": "invalid_bullet_lists"})
    add_what_works = [str(x).strip() for x in add_works_raw if str(x).strip()]
    add_what_doesnt_work = [str(x).strip() for x in add_misses_raw if str(x).strip()]

    metadata_updates_raw = params.get("metadata_updates") or {}
    if not isinstance(metadata_updates_raw, dict):
        return json.dumps({"ok": False, "error": "metadata_updates_must_be_object"})
    # Strip protected fields — confidence flows from outcome closure only.
    metadata_updates = {
        str(k): v for k, v in metadata_updates_raw.items()
        if str(k) not in ("confidence", "sample_size")
    }

    proposed_description_raw = params.get("proposed_description")
    proposed_description = (
        str(proposed_description_raw).strip()
        if proposed_description_raw is not None and str(proposed_description_raw).strip()
        else None
    )

    evidence_decisions_raw = params.get("evidence_decisions") or []
    if not isinstance(evidence_decisions_raw, list):
        return json.dumps({"ok": False, "error": "invalid_evidence_decisions"})
    evidence_decisions: list[int] = []
    for x in evidence_decisions_raw:
        try:
            evidence_decisions.append(int(x))
        except (TypeError, ValueError):
            continue

    if (
        proposed_description is None
        and not add_what_works
        and not add_what_doesnt_work
        and not metadata_updates
    ):
        return json.dumps({"ok": False, "error": "no_changes_proposed"})

    actor = str(_current_agent_id_var.get() or "brain") or "brain"
    approval_reason = f"Skill update proposed for {skill_name}: {rationale[:240]}"
    payload = {
        "skill_name": skill_name,
        "current_version": skill.version,
        "proposed_description": proposed_description,
        "add_what_works": add_what_works,
        "add_what_doesnt_work": add_what_doesnt_work,
        "metadata_updates": metadata_updates,
        "rationale": rationale,
        "evidence_decisions": evidence_decisions,
        "proposed_by": actor,
    }

    try:
        approval_id = create_approval(
            "skill_update_proposal",
            target_type="quant_skill",
            target_id=skill_name,
            requested_status=None,
            status="pending_approval",
            actor=actor,
            reason=approval_reason,
            payload=payload,
            owner="ceo",
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": "approval_create_failed", "detail": str(exc)})

    return json.dumps({
        "ok": True,
        "approval_id": int(approval_id),
        "skill_name": skill_name,
        "current_version": skill.version,
    })


@register_tool(
    name="create_routine",
    description=(
        "Propose a new scheduled brain routine — an NL prompt that runs on a "
        "cron expression with a specific tools_context (scheduled/research/"
        "interactive/recovery) and optional curated skills. The routine is "
        "queued as a `routine_create` approval; the operator must approve it "
        "before it begins firing on schedule. Use this when the Brain wants "
        "to set up an autonomous repeating workflow (e.g. weekly post-mortem "
        "sweep, hourly regime check)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short unique name for the routine (e.g. 'weekly-postmortem-sweep').",
            },
            "prompt": {
                "type": "string",
                "description": "Natural-language instruction the Brain will receive when the routine fires.",
            },
            "cron_expr": {
                "type": "string",
                "description": "Standard 5-field cron expression in UTC (e.g. '0 14 * * MON').",
            },
            "tools_context": {
                "type": "string",
                "enum": ["scheduled", "interactive", "recovery", "research"],
                "description": (
                    "Tools_context for the routine's brain_invoke task. Defaults to 'scheduled' "
                    "(no research tools)."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of curated skill names to inject into the routine context.",
            },
            "rationale": {
                "type": "string",
                "description": "Why this routine should exist — shown to the operator on the approval card.",
            },
        },
        "required": ["name", "prompt", "cron_expr", "rationale"],
    },
    permissions={"brain", None},
    category="general",
)
def _tool_create_routine(params: dict) -> str:
    """Queue a `routine_create` approval. The routine is materialized only on
    operator approval — Brain cannot self-schedule.
    """
    from forven.control_plane.routines import RoutineValidationError, _validate_cron, _validate_context, get_routine_by_name
    from forven.db import create_approval

    name = str(params.get("name") or "").strip()
    prompt = str(params.get("prompt") or "").strip()
    cron_expr = str(params.get("cron_expr") or "").strip()
    rationale = str(params.get("rationale") or "").strip()
    tools_context = str(params.get("tools_context") or "scheduled").strip() or "scheduled"
    skills_raw = params.get("skills") or []
    if not isinstance(skills_raw, list):
        return json.dumps({"ok": False, "error": "skills_must_be_array"})
    skills = [str(s).strip() for s in skills_raw if str(s).strip()]

    if not name:
        return json.dumps({"ok": False, "error": "missing_name"})
    if not prompt:
        return json.dumps({"ok": False, "error": "missing_prompt"})
    if not cron_expr:
        return json.dumps({"ok": False, "error": "missing_cron_expr"})
    if not rationale:
        return json.dumps({"ok": False, "error": "missing_rationale"})

    try:
        _validate_cron(cron_expr)
    except RoutineValidationError as exc:
        return json.dumps({"ok": False, "error": "invalid_cron_expr", "detail": str(exc)})
    try:
        _validate_context(tools_context)
    except RoutineValidationError as exc:
        return json.dumps({"ok": False, "error": "invalid_tools_context", "detail": str(exc)})

    if get_routine_by_name(name) is not None:
        return json.dumps({"ok": False, "error": "routine_name_taken", "name": name})

    actor = str(_current_agent_id_var.get() or "brain") or "brain"
    approval_payload = {
        "name": name,
        "prompt": prompt,
        "cron_expr": cron_expr,
        "tools_context": tools_context,
        "skills": skills,
        "rationale": rationale,
        "proposed_by": actor,
    }
    try:
        approval_id = create_approval(
            "routine_create",
            target_type="brain_routine",
            target_id=name,
            requested_status=None,
            status="pending_approval",
            actor=actor,
            reason=f"Routine create proposed: {name} — {rationale[:240]}",
            payload=approval_payload,
            owner="ceo",
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": "approval_create_failed", "detail": str(exc)})

    return json.dumps({
        "ok": True,
        "approval_id": int(approval_id),
        "name": name,
        "cron_expr": cron_expr,
    })
