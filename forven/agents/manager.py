"""Agent team manager — create, edit, delete, and inspect agents."""

import logging
from datetime import datetime, timezone
from pathlib import Path

from forven.config import LEGACY_WORKSPACE_DIR, WORKSPACE_DIR
from forven.ai import normalize_provider_and_model
from forven.db import get_db, init_db, get_agents, normalize_agent_visibility
from forven.workspace import read_workspace, write_workspace

log = logging.getLogger("forven.agents.manager")

# Per-agent identity files. Each sub-agent gets its own copy of SOUL.md and
# AGENTS.md (seeded from the shipped templates, lightly personalized) plus a
# bespoke ROLE.md. A single GLOBAL IDENTITY.md (mission/risk) is shared by all
# and is intentionally NOT made per-agent.
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "templates" / "workspace"


def _render_role_md(name: str, role: str, instructions: str | None) -> str:
    """Build the per-agent ROLE.md from its concise role and full instructions."""
    role_content = f"# {name}\n\n{role}\n"
    if instructions:
        role_content += f"\n## Instructions\n\n{instructions}\n"
    return role_content


def _load_template(filename: str) -> str:
    """Read a shipped workspace template (SOUL.md / AGENTS.md).

    Falls back to whatever the workspace currently has for that file (the
    global copy seeded by init_workspace) and finally to a minimal stub, so a
    missing template never blocks agent seeding.
    """
    src = _TEMPLATE_DIR / filename
    try:
        if src.exists():
            text = src.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                return text
    except OSError:
        pass
    # Fall back to the global workspace copy if the template is unavailable.
    global_copy = read_workspace(filename, optional=True)
    if global_copy and global_copy.strip():
        return global_copy
    return f"# {filename.replace('.md', '')}\n"


def _render_soul_md(name: str, role: str) -> str:
    """Personalize the shipped SOUL template with a per-agent header."""
    base = _load_template("SOUL.md")
    header = (
        f"# {name} — Identity\n\n"
        f"You are **{name}**, part of Forven. Your responsibility: {role}\n\n"
        "Your ROLE.md defines the detailed mandate. The shared principles follow.\n\n---\n\n"
    )
    return header + base


def _render_agents_md(name: str, role: str) -> str:
    """Personalize the shipped AGENTS template with a per-agent header."""
    base = _load_template("AGENTS.md")
    header = (
        f"# {name} — Workspace Guide\n\n"
        f"This is **{name}**'s workspace ({role}). The shared operating guide follows.\n\n---\n\n"
    )
    return header + base


def _has_nonempty_workspace_file(rel_path: str) -> bool:
    """True when ``rel_path`` already exists with non-empty content in any
    workspace root. Used so self-heal never overwrites real content."""
    existing = read_workspace(rel_path, optional=True)
    return bool(existing and existing.strip())


def ensure_agent_identity_files(
    agent_id: str,
    name: str,
    role: str,
    instructions: str | None = None,
) -> list[str]:
    """Idempotently (re)create any MISSING per-agent identity files.

    Writes ``agents/<id>/SOUL.md``, ``agents/<id>/AGENTS.md``, and
    ``agents/<id>/ROLE.md`` only when they are absent or empty — never
    overwrites existing non-empty content. Returns the list of relative paths
    that were written. Safe to call on every startup and from create_agent.
    """
    agent_id = str(agent_id or "").strip()
    if not agent_id:
        return []

    agent_dir = WORKSPACE_DIR / "agents" / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "memory").mkdir(exist_ok=True)
    (agent_dir / "outputs").mkdir(exist_ok=True)

    written: list[str] = []
    plan = (
        ("SOUL.md", lambda: _render_soul_md(name, role)),
        ("AGENTS.md", lambda: _render_agents_md(name, role)),
        ("ROLE.md", lambda: _render_role_md(name, role, instructions)),
    )
    for filename, renderer in plan:
        rel = f"agents/{agent_id}/{filename}"
        if _has_nonempty_workspace_file(rel):
            continue
        write_workspace(rel, renderer())
        written.append(rel)

    if written:
        log.info("Seeded per-agent identity files for %s: %s", agent_id, written)
    return written


def create_agent(
    agent_id: str,
    name: str,
    role: str,
    model: str = "openai",
    model_id: str | None = None,
    schedule_type: str | None = None,
    schedule_expr: str | None = None,
    visibility: str = "visible",
    instructions: str | None = None,
) -> dict:
    """Create a new agent."""
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    normalized_model, normalized_model_id = normalize_provider_and_model(model, model_id)
    normalized_visibility = normalize_agent_visibility(visibility)

    with get_db() as conn:
        conn.execute(
            """INSERT INTO agents
            (id, name, role, model, model_id, schedule_type, schedule_expr, enabled, visibility, instructions, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
            (
                agent_id,
                name,
                role,
                normalized_model,
                normalized_model_id,
                schedule_type,
                schedule_expr,
                normalized_visibility,
                instructions,
                now,
                now,
            ),
        )

    # Create the agent's workspace directory and seed its per-agent identity
    # files (SOUL.md, AGENTS.md, ROLE.md). ensure_agent_identity_files creates
    # the directory tree and only writes files that are missing/empty, so a
    # re-create never clobbers operator-customized content.
    ensure_agent_identity_files(agent_id, name, role, instructions)

    log.info("Created agent: %s (%s)", name, agent_id)
    return {"id": agent_id, "name": name, "role": role, "model": model}


def _has_role_md(agent_id: str) -> bool:
    """Return True when any known role filename exists for the agent."""
    if not agent_id:
        return False
    names = ("ROLE.md", "role.md")
    for workspace_root in (WORKSPACE_DIR, LEGACY_WORKSPACE_DIR):
        if not workspace_root:
            continue
        if not workspace_root.exists():
            continue
        for name in names:
            if (workspace_root / "agents" / agent_id / name).exists():
                return True
    return False


def update_agent(agent_id: str, **kwargs):
    """Update agent fields."""
    allowed = {
        "name",
        "role",
        "model",
        "model_id",
        "schedule_type",
        "schedule_expr",
        "enabled",
        "visibility",
        "instructions",
        "discord_token",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}

    if not updates:
        return

    requested_model = updates.get("model")
    requested_model_id = updates.get("model_id")
    if requested_model is not None or requested_model_id is not None:
        with get_db() as conn:
            existing = conn.execute("SELECT model, model_id FROM agents WHERE id = ?", (agent_id,)).fetchone()
        existing_model = existing["model"] if existing else None
        existing_model_id = existing["model_id"] if existing else None
        normalized_model, normalized_model_id = normalize_provider_and_model(
            requested_model if requested_model is not None else existing_model,
            requested_model_id if requested_model_id is not None else existing_model_id,
        )
        updates["model"] = normalized_model
        updates["model_id"] = normalized_model_id
    if "visibility" in updates:
        updates["visibility"] = normalize_agent_visibility(updates.get("visibility"))

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [agent_id]

    with get_db() as conn:
        conn.execute(f"UPDATE agents SET {set_clause} WHERE id = ?", values)

    log.info("Updated agent %s: %s", agent_id, list(updates.keys()))


def delete_agent(agent_id: str):
    """Delete an agent (keeps workspace files).

    Reassigns any strategies the agent owned to the Brain so no strategy is left
    owned by a now-deleted agent (the Brain always exists and re-delegates by
    stage). The agent's own tasks are removed with it.
    """
    normalized = str(agent_id or "").strip()
    with get_db() as conn:
        if normalized and normalized != "brain":
            conn.execute(
                "UPDATE strategies SET owner = 'brain' WHERE owner = ?",
                (normalized,),
            )
        conn.execute("DELETE FROM agent_tasks WHERE agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    log.info("Deleted agent: %s", agent_id)


def inspect_agent(agent_id: str) -> dict:
    """Get full agent details including recent tasks and memory."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if not row:
            return {"error": f"Agent not found: {agent_id}"}
        agent = dict(row)

        # Recent tasks
        tasks = conn.execute(
            "SELECT id, type, title, status, created_at, completed_at FROM agent_tasks WHERE agent_id = ? ORDER BY created_at DESC LIMIT 10",
            (agent_id,),
        ).fetchall()
        agent["recent_tasks"] = [dict(t) for t in tasks]

        # Pending task count
        pending = conn.execute(
            "SELECT COUNT(*) as c FROM agent_tasks WHERE agent_id = ? AND status = 'pending'",
            (agent_id,),
        ).fetchone()
        agent["pending_tasks"] = pending["c"]

    # Check for ROLE.md
    agent["has_role_md"] = _has_role_md(agent_id)

    return agent


def list_agents_with_stats() -> list[dict]:
    """List all agents with task stats."""
    agents = get_agents()
    for agent in agents:
        with get_db() as conn:
            stats = conn.execute(
                """SELECT
                    COUNT(*) FILTER (WHERE status='pending') as pending,
                    COUNT(*) FILTER (WHERE status='done') as completed,
                    COUNT(*) FILTER (WHERE status='failed') as failed,
                    MAX(completed_at) as last_completed
                FROM agent_tasks WHERE agent_id = ?""",
                (agent["id"],),
            ).fetchone()
            if stats:
                agent["pending_tasks"] = stats["pending"]
                agent["completed_tasks"] = stats["completed"]
                agent["failed_tasks"] = stats["failed"]
                agent["last_completed"] = stats["last_completed"]
    return agents
