"""Workspace identity system — manages .md files that define AI behavior and continuity."""

import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console

from forven.config import OPENCLAW_WORKSPACE, WORKSPACE_DIR, LEGACY_WORKSPACE_DIR, ensure_dirs

console = Console()
log = logging.getLogger("forven.workspace")


class WorkspacePathError(ValueError):
    """Raised when a requested workspace-relative path escapes the workspace
    root or is otherwise unsafe (absolute, traversal, symlink-outside, etc.)."""


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_workspace_path(rel_path: str, root: Path | None = None) -> Path:
    """H-S8: resolve a workspace-relative path to an absolute Path, refusing
    anything that escapes the workspace root via traversal, absolute prefix,
    or symlink. Caller must catch WorkspacePathError.

    The returned path may not exist yet (write paths). Symlink resolution is
    applied to the deepest existing ancestor so symlinked subdirectories that
    point outside the workspace are still detected.
    """
    if not isinstance(rel_path, str) or not rel_path:
        raise WorkspacePathError("Path must be a non-empty string")
    raw = rel_path.replace("\\", "/").strip()
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise WorkspacePathError(f"Absolute paths not allowed: {rel_path!r}")
    parts = [p for p in raw.split("/") if p]
    if any(p == ".." for p in parts):
        raise WorkspacePathError(f"Path traversal not allowed: {rel_path!r}")

    base = (root or WORKSPACE_DIR).resolve()
    candidate = base.joinpath(*parts)

    # Resolve through the deepest existing ancestor so a symlinked subdir
    # pointing outside the workspace is detected even if the leaf doesn't
    # exist yet.
    probe = candidate
    while True:
        if probe.exists() or probe == probe.parent:
            try:
                resolved_ancestor = probe.resolve()
            except OSError as exc:
                raise WorkspacePathError(f"Failed to resolve path: {exc}") from exc
            break
        probe = probe.parent

    if not _is_within(resolved_ancestor, base):
        raise WorkspacePathError(
            f"Path resolves outside workspace root: {rel_path!r} → {resolved_ancestor}"
        )
    return candidate

# Core workspace files
CORE_FILES = [
    "SOUL.md",
    "IDENTITY.md",
    "USER.md",
    "AGENTS.md",
    "TOOLS.md",
    "DATA_SCHEMA.md",
    "HEARTBEAT.md",
    "BACKUPS.md",
    "LESSONS.md",
    "evolution_journal.md",
]


def read_workspace(filename: str, optional: bool = False) -> str | None:
    """Read a workspace file. Returns None if optional and missing."""
    # Prefer the richest available copy. When both legacy/canonical workspaces
    # exist and contain the same file, the longest non-empty text wins. This
    # preserves custom, richer role/soul content if one directory has better data.
    best_text: str | None = None
    best_score: int = -1
    last_seen: str | None = None
    for root in _workspace_roots():
        path = root / filename
        if not path.exists():
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            score = len(text.strip())
            if score > best_score:
                best_score = score
                best_text = text
            if last_seen is None:
                last_seen = text
        except OSError:
            continue

    if best_text is not None:
        return best_text

    if last_seen is not None:
        return last_seen

    if optional:
        return None

    raise FileNotFoundError(f"Workspace file not found: {WORKSPACE_DIR / filename}")


def _workspace_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()

    for root in (WORKSPACE_DIR, LEGACY_WORKSPACE_DIR):
        if root in seen:
            continue
        seen.add(root)
        if root and root.exists():
            roots.append(root)

    if not roots:
        roots.append(WORKSPACE_DIR)

    return roots


def write_workspace(filename: str, content: str):
    """Write a workspace file (atomic)."""
    ensure_dirs()

    def _write(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        # replace is safer on Windows than rename for existing targets.
        tmp.replace(path)

    _write(WORKSPACE_DIR / filename)
    # Mirror writes to legacy workspace to keep `.judex` and `.forven` in sync.
    if LEGACY_WORKSPACE_DIR and LEGACY_WORKSPACE_DIR != WORKSPACE_DIR:
        try:
            _write(LEGACY_WORKSPACE_DIR / filename)
        except Exception:
            pass


_APPEND_LOCK = threading.Lock()


def append_workspace(filename: str, content: str):
    """Append to a workspace file.

    Reconciles the divergent-roots trap: ``read_workspace`` returns the
    LONGEST copy across the canonical and legacy roots, while appends
    previously hit each root independently (legacy failures silently
    swallowed). Once the roots diverged with the legacy copy ahead, an append
    landed on the shorter canonical copy and every subsequent read returned
    the stale legacy copy WITHOUT it — "write_file returned success, the file
    did not grow" (2026-07-06 agent reports on memory/*.md). Now the append
    bases on the same best copy the reader resolves, and the RESULT is
    written to both roots atomically, so reader and writer always agree.
    """
    with _APPEND_LOCK:
        base = read_workspace(filename, optional=True) or ""
        write_workspace(filename, base + content)


def today_memory_path() -> str:
    """Get the path for today's memory file."""
    return f"memory/{date.today().isoformat()}.md"


def yesterday_memory_path() -> str:
    """Get the path for yesterday's memory file."""
    return f"memory/{(date.today() - timedelta(days=1)).isoformat()}.md"


def list_workspace_files() -> list[str]:
    """List all files in the workspace."""
    seen: dict[str, Path] = {}
    for root in _workspace_roots():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                rel = str(path.relative_to(root))
                should_replace = False
                if rel not in seen:
                    should_replace = True
                else:
                    try:
                        should_replace = path.stat().st_mtime > seen[rel].stat().st_mtime
                    except OSError:
                        should_replace = False

                if should_replace:
                    seen[rel] = path

    return sorted(seen.keys())


def init_workspace(force: bool = False):
    """Initialize workspace — copy from OpenClaw or create defaults."""
    ensure_dirs()

    if OPENCLAW_WORKSPACE.exists():
        _migrate_from_openclaw(force)
    else:
        _create_defaults()


def _migrate_from_openclaw(force: bool):
    """Copy workspace files from OpenClaw."""
    migrated = 0

    # Copy core .md files
    for filename in CORE_FILES:
        src = OPENCLAW_WORKSPACE / filename
        dst = WORKSPACE_DIR / filename
        if src.exists() and (force or not dst.exists()):
            shutil.copy2(src, dst)
            migrated += 1
            console.print(f"  [green]Copied {filename}[/green]")

    # Copy LESSONS.md from trading directory if not in root
    lessons_src = OPENCLAW_WORKSPACE / "trading" / "LESSONS.md"
    lessons_dst = WORKSPACE_DIR / "LESSONS.md"
    if lessons_src.exists() and (force or not lessons_dst.exists()):
        shutil.copy2(lessons_src, lessons_dst)
        if "LESSONS.md" not in [f for f in CORE_FILES]:
            migrated += 1
            console.print("  [green]Copied trading/LESSONS.md[/green]")

    # Copy memory directory
    memory_src = OPENCLAW_WORKSPACE / "memory"
    if memory_src.exists():
        memory_dst = WORKSPACE_DIR / "memory"
        memory_dst.mkdir(exist_ok=True)
        for f in sorted(memory_src.glob("*.md")):
            dst = memory_dst / f.name
            if force or not dst.exists():
                shutil.copy2(f, dst)
                migrated += 1
        console.print("  [green]Copied memory files[/green]")

    # Copy evolution_journal.md from trading if in trading dir
    ej_src = OPENCLAW_WORKSPACE / "trading" / "evolution_journal.md"
    ej_dst = WORKSPACE_DIR / "evolution_journal.md"
    if ej_src.exists() and (force or not ej_dst.exists()):
        shutil.copy2(ej_src, ej_dst)
        console.print("  [green]Copied trading/evolution_journal.md[/green]")

    console.print(f"\n[bold green]Migrated {migrated} workspace files from OpenClaw[/bold green]")


def _create_defaults():
    """Create default workspace files from templates shipped with Forven."""
    template_dir = Path(__file__).parent.parent / "templates" / "workspace"

    template_files = [
        "SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md", "TOOLS.md", "DATA_SCHEMA.md",
        "HEARTBEAT.md", "BACKUPS.md", "LESSONS.md", "evolution_journal.md",
    ]

    for filename in template_files:
        dst = WORKSPACE_DIR / filename
        if dst.exists():
            continue
        src = template_dir / filename
        if src.exists():
            shutil.copy2(src, dst)
            console.print(f"  [green]Created {filename} (from template)[/green]")
        else:
            dst.write_text(f"# {filename.replace('.md', '')}\n", encoding="utf-8")
            console.print(f"  [dim]Created {filename} (minimal)[/dim]")

    (WORKSPACE_DIR / "memory").mkdir(exist_ok=True)
    (WORKSPACE_DIR / "agents").mkdir(exist_ok=True)

    console.print("[bold green]Default workspace created from templates[/bold green]")


# ---------------------------------------------------------------------------
# Operator profile (Phase 6 / P6-T01)
# ---------------------------------------------------------------------------

_RISK_APPETITES = {"conservative", "balanced", "aggressive"}
_RESPONSE_STYLES = {"terse", "conversational", "verbose"}


@dataclass
class OperatorPreferences:
    notification_channels: list[str] = field(default_factory=list)
    quiet_hours: str | None = None
    risk_appetite: str | None = None
    response_style: str | None = None


@dataclass
class OperatorProfile:
    """Structured view of USER.md.

    Any field may be ``None`` / empty when the corresponding key is missing
    from the YAML frontmatter. Callers must treat the whole object as a hint
    for Brain context, not a contract.
    """

    name: str | None = None
    timezone: str | None = None
    starting_capital_usd: float | None = None
    risk_per_trade_pct: float | None = None
    exchange: str | None = None
    asset_universe: str | None = None
    preferences: OperatorPreferences = field(default_factory=OperatorPreferences)
    rules: list[str] = field(default_factory=list)
    body: str = ""
    parse_error: str | None = None

    @property
    def has_structured(self) -> bool:
        return any(
            v not in (None, "", [], {})
            for v in (
                self.name,
                self.timezone,
                self.starting_capital_usd,
                self.risk_per_trade_pct,
                self.exchange,
                self.asset_universe,
                self.preferences.notification_channels,
                self.preferences.quiet_hours,
                self.preferences.risk_appetite,
                self.preferences.response_style,
                self.rules,
            )
        )


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return (frontmatter_str, body). Frontmatter is None when absent."""
    if not text.startswith("---"):
        return None, text
    rest = text[3:]
    if rest.startswith("\n"):
        rest = rest[1:]
    elif rest.startswith("\r\n"):
        rest = rest[2:]
    if rest.startswith("---"):
        # Empty frontmatter: ``---\n---\n`` with nothing between the fences.
        body = rest[3:]
        if body.startswith("\n"):
            body = body[1:]
        elif body.startswith("\r\n"):
            body = body[2:]
        return "", body
    closer_idx = rest.find("\n---")
    if closer_idx == -1:
        return None, text
    fm = rest[:closer_idx]
    body = rest[closer_idx + len("\n---"):]
    if body.startswith("\n"):
        body = body[1:]
    elif body.startswith("\r\n"):
        body = body[2:]
    return fm, body


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _parse_profile_dict(data: dict[str, Any]) -> OperatorProfile:
    prefs_raw = data.get("preferences") or {}
    if not isinstance(prefs_raw, dict):
        prefs_raw = {}

    risk_appetite = prefs_raw.get("risk_appetite")
    if isinstance(risk_appetite, str):
        risk_appetite = risk_appetite.strip().lower() or None
        if risk_appetite is not None and risk_appetite not in _RISK_APPETITES:
            risk_appetite = None
    else:
        risk_appetite = None

    response_style = prefs_raw.get("response_style")
    if isinstance(response_style, str):
        response_style = response_style.strip().lower() or None
        if response_style is not None and response_style not in _RESPONSE_STYLES:
            response_style = None
    else:
        response_style = None

    prefs = OperatorPreferences(
        notification_channels=_coerce_str_list(prefs_raw.get("notification_channels")),
        quiet_hours=(prefs_raw.get("quiet_hours") or None) if isinstance(prefs_raw.get("quiet_hours"), str) else None,
        risk_appetite=risk_appetite,
        response_style=response_style,
    )

    return OperatorProfile(
        name=str(data["name"]).strip() if isinstance(data.get("name"), (str, int, float)) and str(data.get("name")).strip() else None,
        timezone=str(data["timezone"]).strip() if isinstance(data.get("timezone"), str) and data["timezone"].strip() else None,
        starting_capital_usd=_coerce_float(data.get("starting_capital_usd")),
        risk_per_trade_pct=_coerce_float(data.get("risk_per_trade_pct")),
        exchange=str(data["exchange"]).strip() if isinstance(data.get("exchange"), str) and data["exchange"].strip() else None,
        asset_universe=str(data["asset_universe"]).strip() if isinstance(data.get("asset_universe"), str) and data["asset_universe"].strip() else None,
        preferences=prefs,
        rules=_coerce_str_list(data.get("rules")),
    )


def read_operator_profile() -> OperatorProfile | None:
    """Parse ``USER.md`` into a structured profile.

    Returns ``None`` when ``USER.md`` is missing entirely. When the file
    exists but has no parseable frontmatter, returns a body-only profile so
    callers can still surface the prose to Brain.
    """
    raw = read_workspace("USER.md", optional=True)
    if raw is None:
        return None
    fm_str, body = _split_frontmatter(raw)
    profile = OperatorProfile(body=body.rstrip())
    if fm_str is None:
        return profile
    try:
        data = yaml.safe_load(fm_str) or {}
    except yaml.YAMLError as exc:
        log.warning("USER.md frontmatter parse failed: %s", exc)
        profile.parse_error = str(exc)
        return profile
    if not isinstance(data, dict):
        profile.parse_error = "frontmatter must be a YAML mapping"
        return profile
    parsed = _parse_profile_dict(data)
    parsed.body = body.rstrip()
    return parsed


def _profile_to_frontmatter_dict(profile: OperatorProfile) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if profile.name:
        out["name"] = profile.name
    if profile.timezone:
        out["timezone"] = profile.timezone
    if profile.starting_capital_usd is not None:
        out["starting_capital_usd"] = profile.starting_capital_usd
    if profile.risk_per_trade_pct is not None:
        out["risk_per_trade_pct"] = profile.risk_per_trade_pct
    if profile.exchange:
        out["exchange"] = profile.exchange
    if profile.asset_universe:
        out["asset_universe"] = profile.asset_universe

    prefs: dict[str, Any] = {}
    if profile.preferences.notification_channels:
        prefs["notification_channels"] = list(profile.preferences.notification_channels)
    if profile.preferences.quiet_hours:
        prefs["quiet_hours"] = profile.preferences.quiet_hours
    if profile.preferences.risk_appetite:
        prefs["risk_appetite"] = profile.preferences.risk_appetite
    if profile.preferences.response_style:
        prefs["response_style"] = profile.preferences.response_style
    if prefs:
        out["preferences"] = prefs

    if profile.rules:
        out["rules"] = list(profile.rules)
    return out


def write_operator_profile(profile: OperatorProfile) -> None:
    """Serialize ``profile`` back to ``USER.md`` (preserves body verbatim).

    Frontmatter is omitted entirely when the structured fields are all empty,
    so a body-only profile round-trips without empty ``---\\n---`` blocks.
    """
    fm_dict = _profile_to_frontmatter_dict(profile)
    body = profile.body or ""
    if not body.endswith("\n") and body:
        body = body + "\n"
    if fm_dict:
        fm_str = yaml.safe_dump(
            fm_dict,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        ).strip()
        text = f"---\n{fm_str}\n---\n{body}"
    else:
        text = body
    write_workspace("USER.md", text)


__all__ = [
    "OperatorPreferences",
    "OperatorProfile",
    "WorkspacePathError",
    "append_workspace",
    "init_workspace",
    "list_workspace_files",
    "read_operator_profile",
    "read_workspace",
    "safe_workspace_path",
    "today_memory_path",
    "write_operator_profile",
    "write_workspace",
    "yesterday_memory_path",
]
