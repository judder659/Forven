"""Quant Skills — self-learning knowledge store for strategy development.

Stores curated quant insights in agentskills.io-compatible SKILL.md format
on disk.  Insights are extracted from backtest results and consumed by Brain
ideation and external agents (Hermes).

Skill taxonomy:
  regime-{regime}-{indicator}   What works in a specific regime
  failure-{pattern}             Proven anti-patterns to avoid
  indicator-{name}              Cross-regime indicator insights
  combo-{indicators}            Effective indicator combinations
  params-{family}               Optimal parameter ranges
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml

from forven.config import FORVEN_HOME

log = logging.getLogger("forven.quant_skills")

SKILLS_DIR = FORVEN_HOME / "quant-skills"
HYPOTHESES_DIR = SKILLS_DIR / "_hypotheses"
ARCHIVED_DIR = SKILLS_DIR / "_archived"

SkillType = Literal["regime", "failure", "indicator", "combo", "params"]

PROMOTION_THRESHOLD = 3  # backtests before hypothesis → skill
MAX_SKILL_COUNT = 100
CONFIDENCE_DECAY_DAYS = 90
STALE_DAYS = 180


# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class QuantSkill:
    """A curated quant insight in agentskills.io format."""

    name: str
    description: str
    skill_type: SkillType
    metadata: dict = field(default_factory=dict)
    what_works: list[str] = field(default_factory=list)
    what_doesnt_work: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)

    # Convenience accessors for common metadata fields
    @property
    def confidence(self) -> float:
        return float(self.metadata.get("confidence", 0))

    @confidence.setter
    def confidence(self, value: float) -> None:
        self.metadata["confidence"] = str(round(value, 4))

    @property
    def sample_size(self) -> int:
        return int(self.metadata.get("sample_size", 0))

    @sample_size.setter
    def sample_size(self, value: int) -> None:
        self.metadata["sample_size"] = str(value)

    @property
    def regime(self) -> str:
        return self.metadata.get("regime", "")

    @property
    def last_validated(self) -> str:
        return self.metadata.get("last_validated", "")

    @last_validated.setter
    def last_validated(self, value: str) -> None:
        self.metadata["last_validated"] = value

    @property
    def version(self) -> int:
        """1-indexed version. Existing skills without this field default to 1."""
        v = self.metadata.get("version", 1)
        try:
            return int(v)
        except (TypeError, ValueError):
            return 1

    @version.setter
    def version(self, value: int) -> None:
        self.metadata["version"] = int(value)

    @property
    def parent_version(self) -> int | None:
        v = self.metadata.get("parent_version")
        if v in (None, "", "None"):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @parent_version.setter
    def parent_version(self, value: int | None) -> None:
        if value is None:
            self.metadata.pop("parent_version", None)
        else:
            self.metadata["parent_version"] = int(value)

    @property
    def change_summary(self) -> str:
        return self.metadata.get("change_summary", "")

    @change_summary.setter
    def change_summary(self, value: str) -> None:
        self.metadata["change_summary"] = value


@dataclass
class Hypothesis:
    """An unconfirmed observation awaiting enough evidence to become a skill."""

    id: str
    pattern: str
    observation: str
    backtest_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Hypothesis:
        return cls(
            id=data["id"],
            pattern=data["pattern"],
            observation=data.get("observation", ""),
            backtest_ids=data.get("backtest_ids", []),
            created_at=data.get("created_at", ""),
            count=data.get("count", 0),
        )


# ── Directory Helpers ────────────────────────────────────────────────────────


def _ensure_dirs() -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    HYPOTHESES_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVED_DIR.mkdir(parents=True, exist_ok=True)


def _sanitize_name(name: str) -> str:
    """Enforce agentskills.io naming: lowercase alphanumeric + hyphens."""
    sanitized = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
    sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-")
    return sanitized[:64]


# ── SKILL.md I/O ─────────────────────────────────────────────────────────────


def _build_skill_body(skill: QuantSkill) -> str:
    """Render the markdown body (sections after the frontmatter) for a skill."""
    body_parts: list[str] = []
    if skill.what_works:
        body_parts.append("## What Works")
        body_parts.extend(f"- {item}" for item in skill.what_works)
        body_parts.append("")
    if skill.what_doesnt_work:
        body_parts.append("## What Doesn't Work")
        body_parts.extend(f"- {item}" for item in skill.what_doesnt_work)
        body_parts.append("")
    if skill.evidence:
        body_parts.append("## Evidence")
        body_parts.append(
            f"Based on {len(skill.evidence)} backtest(s). "
            f"See [evidence.json](references/evidence.json) for details."
        )
        body_parts.append("")
    return "\n".join(body_parts)


# Phase 3 frontmatter shape (agentskills.io-compatible): `name`, `description`,
# `version` are top-level. All Forven domain fields nest under `metadata.forven`.
# These keys are top-level / promoted out of the metadata dict.
_PROMOTED_KEYS = ("version", "parent_version")


def _build_frontmatter(skill: QuantSkill, name: str) -> dict:
    """Render the YAML frontmatter dict for a skill in the v3 envelope shape."""
    forven_meta: dict = {"type": skill.skill_type}
    for k, v in skill.metadata.items():
        if k in _PROMOTED_KEYS:
            continue  # promoted out of metadata
        # Stringify scalars to keep YAML output stable across reloads.
        forven_meta[k] = str(v) if not isinstance(v, (list, dict)) else v

    fm: dict = {
        "name": name,
        "description": skill.description,
        "version": skill.version,
        "metadata": {"forven": forven_meta},
    }
    return fm


def write_skill(skill: QuantSkill, *, evidence_task_id: str | None = None, created_by: str = "system") -> Path:
    """Write a QuantSkill to disk + append a `quant_skills_history` row.

    Creates ``SKILLS_DIR/{name}/SKILL.md`` and ``references/evidence.json``.
    Returns the path to the SKILL.md file. Also writes a row to the
    `quant_skills_history` table capturing a unified diff between the previous
    SKILL.md body (if any) and the new body, keyed on (skill_name, version).
    """
    _ensure_dirs()
    name = _sanitize_name(skill.name)
    skill_dir = SKILLS_DIR / name
    refs_dir = skill_dir / "references"
    skill_path = skill_dir / "SKILL.md"

    # Capture pre-existing body BEFORE we overwrite, so we can diff for history.
    prior_body = ""
    if skill_path.exists():
        try:
            prior_text = skill_path.read_text(encoding="utf-8")
            fm_match = re.match(r"^---\n(.+?)\n---\n", prior_text, re.DOTALL)
            prior_body = prior_text[fm_match.end():] if fm_match else prior_text
        except OSError:
            prior_body = ""

    skill_dir.mkdir(parents=True, exist_ok=True)
    refs_dir.mkdir(exist_ok=True)

    # Default version to 1 if unset.
    if skill.metadata.get("version") in (None, "", "None"):
        skill.version = 1

    frontmatter = _build_frontmatter(skill, name)
    body = _build_skill_body(skill)

    content = "---\n" + yaml.dump(frontmatter, default_flow_style=False, sort_keys=False) + "---\n\n"
    content += body

    skill_path.write_text(content, encoding="utf-8")

    # Write evidence JSON
    evidence_path = refs_dir / "evidence.json"
    evidence_path.write_text(
        json.dumps(skill.evidence, indent=2, default=str),
        encoding="utf-8",
    )

    # Append history row. Idempotent on (skill_name, version) — re-writes with
    # the same version are a no-op for history (INSERT OR IGNORE).
    body_diff = ""
    if prior_body or skill.version > 1:
        diff_lines = difflib.unified_diff(
            prior_body.splitlines(keepends=True),
            body.splitlines(keepends=True),
            fromfile=f"{name}@v{skill.parent_version or 0}",
            tofile=f"{name}@v{skill.version}",
            n=3,
        )
        body_diff = "".join(diff_lines)

    try:
        from forven.db import get_db  # local import to avoid circular at module load
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO quant_skills_history "
                "(skill_name, version, parent_version, body_diff, change_summary, evidence_task_id, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    int(skill.version),
                    skill.parent_version,
                    body_diff,
                    skill.change_summary,
                    evidence_task_id,
                    created_by,
                ),
            )
    except Exception as exc:  # pragma: no cover - defensive
        # History persistence is best-effort: a transient DB error must not
        # prevent the SKILL.md write from succeeding. Phase 2 set the precedent
        # for fail-open telemetry persistence.
        log.warning("quant_skills_history insert failed for %s v%s: %s", name, skill.version, exc)

    log.info(
        "Wrote quant skill: %s v%d (confidence=%.2f, samples=%d)",
        name, skill.version, skill.confidence, skill.sample_size,
    )
    return skill_path


def _migrate_legacy_frontmatter(fm: dict) -> dict:
    """Normalize either v3-shape or legacy-shape frontmatter into the v3 shape.

    Legacy shape (pre-Phase 3):
        metadata:
          type: regime
          confidence: "..."
          ...

    v3 shape (Phase 3+):
        version: <int>
        metadata:
          forven:
            type: regime
            confidence: "..."

    Returns a normalized dict where Forven domain fields always live under
    `metadata.forven`. Pure function — does not touch disk.
    """
    raw_metadata = fm.get("metadata", {}) if isinstance(fm.get("metadata"), dict) else {}
    if "forven" in raw_metadata and isinstance(raw_metadata["forven"], dict):
        # Already v3.
        return fm
    # Legacy: nest everything currently under metadata into metadata.forven.
    fm = dict(fm)
    fm["metadata"] = {"forven": dict(raw_metadata)}
    return fm


def read_skill(name: str) -> QuantSkill | None:
    """Read a SKILL.md file and parse into a QuantSkill.

    Tolerates BOTH the v3 envelope shape (Forven fields under `metadata.forven`)
    and the legacy shape (Forven fields directly under `metadata`). Legacy
    files are auto-migrated on the next `write_skill` call.
    """
    name = _sanitize_name(name)
    skill_path = SKILLS_DIR / name / "SKILL.md"
    if not skill_path.exists():
        return None

    text = skill_path.read_text(encoding="utf-8")

    # Parse frontmatter
    fm_match = re.match(r"^---\n(.+?)\n---\n", text, re.DOTALL)
    if not fm_match:
        log.warning("Invalid SKILL.md frontmatter in %s", name)
        return None

    try:
        fm = yaml.safe_load(fm_match.group(1))
    except yaml.YAMLError as exc:
        log.warning("YAML parse error in %s: %s", name, exc)
        return None

    fm = _migrate_legacy_frontmatter(fm)

    forven_meta = dict(fm.get("metadata", {}).get("forven", {}))
    skill_type = forven_meta.pop("type", "regime")

    # Promote top-level v3 keys back into metadata for the dataclass round-trip.
    metadata: dict = dict(forven_meta)
    if "version" in fm:
        metadata["version"] = fm["version"]
    elif "version" not in metadata:
        metadata["version"] = 1
    if "parent_version" in fm:
        metadata["parent_version"] = fm["parent_version"]

    body = text[fm_match.end():]

    what_works = _extract_list_section(body, "What Works")
    what_doesnt_work = _extract_list_section(body, "What Doesn't Work")

    # Load evidence
    evidence: list[dict] = []
    evidence_path = SKILLS_DIR / name / "references" / "evidence.json"
    if evidence_path.exists():
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    return QuantSkill(
        name=name,
        description=fm.get("description", ""),
        skill_type=skill_type,
        metadata=metadata,
        what_works=what_works,
        what_doesnt_work=what_doesnt_work,
        evidence=evidence,
    )


def _extract_list_section(body: str, heading: str) -> list[str]:
    """Extract bullet items from a markdown section."""
    pattern = rf"## {re.escape(heading)}\n((?:- .+\n?)+)"
    match = re.search(pattern, body)
    if not match:
        return []
    return [line.lstrip("- ").strip() for line in match.group(1).strip().splitlines() if line.strip()]


def list_skills(skill_type: str | None = None) -> list[QuantSkill]:
    """List all quant skills, optionally filtered by type."""
    _ensure_dirs()
    skills: list[QuantSkill] = []
    for entry in sorted(SKILLS_DIR.iterdir()):
        if entry.name.startswith("_") or not entry.is_dir():
            continue
        skill = read_skill(entry.name)
        if skill is None:
            continue
        if skill_type and skill.skill_type != skill_type:
            continue
        skills.append(skill)
    return skills


def delete_skill(name: str) -> bool:
    """Remove a skill directory entirely."""
    name = _sanitize_name(name)
    skill_dir = SKILLS_DIR / name
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
        log.info("Deleted quant skill: %s", name)
        return True
    return False


# ── Skill Updates ────────────────────────────────────────────────────────────


def update_skill(
    name: str,
    new_evidence: dict,
    new_observations: dict | None = None,
    *,
    evidence_task_id: str | None = None,
    change_summary: str = "",
    created_by: str = "system",
) -> QuantSkill | None:
    """Update an existing skill with new backtest evidence — bumps version.

    ``new_evidence`` is a single backtest result dict.
    ``new_observations`` optionally contains ``what_works`` and ``what_doesnt_work`` lists.
    ``evidence_task_id`` links the new history row to the agent_task that prompted the update.
    ``change_summary`` is a human-readable note for the history entry; defaults to a
    canned string if empty.
    Returns the updated skill or None if not found.
    """
    skill = read_skill(name)
    if skill is None:
        return None

    # Append evidence
    skill.evidence.append(new_evidence)
    skill.sample_size = len(skill.evidence)
    recorded_at = new_evidence.get("recorded_at", new_evidence.get("last_validated", ""))
    try:
        skill.last_validated = datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (TypeError, ValueError, AttributeError):
        skill.last_validated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Merge observations
    if new_observations:
        for item in new_observations.get("what_works", []):
            if item not in skill.what_works:
                skill.what_works.append(item)
        for item in new_observations.get("what_doesnt_work", []):
            if item not in skill.what_doesnt_work:
                skill.what_doesnt_work.append(item)

    # Recalculate confidence
    skill.confidence = _calculate_confidence(skill)

    # Bump version + lineage.
    prior_version = skill.version
    skill.parent_version = prior_version
    skill.version = prior_version + 1
    skill.change_summary = change_summary or (
        f"Added evidence from {evidence_task_id or 'unknown'} (samples={skill.sample_size})"
    )

    write_skill(skill, evidence_task_id=evidence_task_id, created_by=created_by)
    return skill


def list_skill_history(name: str) -> list[dict]:
    """Return all history rows for a skill, ordered version DESC."""
    name = _sanitize_name(name)
    try:
        from forven.db import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, skill_name, version, parent_version, body_diff, "
                "change_summary, evidence_task_id, created_by, created_at "
                "FROM quant_skills_history "
                "WHERE skill_name = ? "
                "ORDER BY version DESC",
                (name,),
            ).fetchall()
    except Exception as exc:
        log.warning("list_skill_history failed for %s: %s", name, exc)
        return []
    return [dict(r) for r in rows]


def get_skill_diff(name: str, from_version: int, to_version: int) -> str:
    """Return a unified diff string covering the changes from ``from_version``
    to ``to_version``.

    Concatenates the ``body_diff`` text stored on each history row in
    (from_version, to_version] in chronological order. Returns empty string
    if either version is unknown or the skill has no recorded diffs in range.
    """
    if from_version == to_version:
        return ""
    name = _sanitize_name(name)
    lo, hi = sorted((int(from_version), int(to_version)))
    try:
        from forven.db import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT version, body_diff FROM quant_skills_history "
                "WHERE skill_name = ? AND version > ? AND version <= ? "
                "ORDER BY version ASC",
                (name, lo, hi),
            ).fetchall()
    except Exception as exc:
        log.warning("get_skill_diff failed for %s: %s", name, exc)
        return ""
    return "\n".join(r["body_diff"] for r in rows if r["body_diff"])


def _calculate_confidence(skill: QuantSkill) -> float:
    """Confidence = weighted consistency score.

    Looks at evidence sharpe/win_rate consistency.  Recent results weighted more.
    """
    if not skill.evidence:
        return 0.0

    now = datetime.now(timezone.utc)
    weighted_positive = 0.0
    total_weight = 0.0

    for ev in skill.evidence:
        # Recency weight: 1.0 for today, decays to 0.3 over CONFIDENCE_DECAY_DAYS
        recorded = ev.get("recorded_at", ev.get("last_validated", ""))
        try:
            rec_dt = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
            age_days = (now - rec_dt).days
        except (ValueError, AttributeError):
            age_days = CONFIDENCE_DECAY_DAYS

        recency_weight = max(0.3, 1.0 - (age_days / CONFIDENCE_DECAY_DAYS) * 0.7)

        # Is this a positive result?
        sharpe = float(ev.get("sharpe", ev.get("avg_sharpe", 0)))
        positive = 1.0 if sharpe > 0.5 else 0.0

        weighted_positive += positive * recency_weight
        total_weight += recency_weight

    if total_weight == 0:
        return 0.0

    return min(1.0, weighted_positive / total_weight)


# ── Hypothesis System ────────────────────────────────────────────────────────


def store_hypothesis(pattern: str, observation: str, backtest_id: str) -> Hypothesis:
    """Store or update a hypothesis.

    If a hypothesis with a matching pattern already exists, increments its count.
    Otherwise creates a new one.
    """
    _ensure_dirs()
    existing = _find_hypothesis_by_pattern(pattern)

    if existing:
        if backtest_id not in existing.backtest_ids:
            existing.backtest_ids.append(backtest_id)
        existing.count = len(existing.backtest_ids)
        if observation and observation != existing.observation:
            existing.observation = observation
        _write_hypothesis(existing)
        log.info("Updated hypothesis %s (count=%d)", existing.id, existing.count)
        return existing

    h_id = _next_hypothesis_id()
    hypothesis = Hypothesis(
        id=h_id,
        pattern=pattern,
        observation=observation,
        backtest_ids=[backtest_id],
        created_at=datetime.now(timezone.utc).isoformat(),
        count=1,
    )
    _write_hypothesis(hypothesis)
    log.info("Created hypothesis %s: %s", h_id, pattern)
    return hypothesis


def _find_hypothesis_by_pattern(pattern: str) -> Hypothesis | None:
    """Find an existing hypothesis with a similar pattern."""
    normalized = _sanitize_name(pattern)
    for h in list_hypotheses():
        if _sanitize_name(h.pattern) == normalized:
            return h
    return None


def _next_hypothesis_id() -> str:
    existing = list_hypotheses()
    max_num = 0
    for h in existing:
        try:
            num = int(h.id.split("-")[1])
            max_num = max(max_num, num)
        except (IndexError, ValueError):
            pass
    return f"h-{max_num + 1:03d}"


def _write_hypothesis(h: Hypothesis) -> None:
    path = HYPOTHESES_DIR / f"{h.id}.json"
    path.write_text(json.dumps(h.to_dict(), indent=2, default=str), encoding="utf-8")


def list_hypotheses() -> list[Hypothesis]:
    """List all pending hypotheses."""
    _ensure_dirs()
    result: list[Hypothesis] = []
    for f in sorted(HYPOTHESES_DIR.glob("h-*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            result.append(Hypothesis.from_dict(data))
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            log.warning("Bad hypothesis file %s: %s", f.name, exc)
    return result


def promote_hypothesis(hypothesis_id: str) -> QuantSkill | None:
    """Promote a hypothesis to a full skill when it has enough evidence.

    Returns the new QuantSkill or None if the hypothesis doesn't qualify.
    """
    h_path = HYPOTHESES_DIR / f"{hypothesis_id}.json"
    if not h_path.exists():
        log.warning("Hypothesis %s not found", hypothesis_id)
        return None

    h = Hypothesis.from_dict(json.loads(h_path.read_text(encoding="utf-8")))

    if h.count < PROMOTION_THRESHOLD:
        log.info("Hypothesis %s has %d samples, needs %d for promotion", h.id, h.count, PROMOTION_THRESHOLD)
        return None

    name = _sanitize_name(h.pattern)
    skill_type = _infer_skill_type(h.pattern)

    # Build initial evidence from backtest IDs
    evidence = [{"backtest_id": bid, "source": "hypothesis_promotion"} for bid in h.backtest_ids]

    skill = QuantSkill(
        name=name,
        description=h.observation,
        skill_type=skill_type,
        metadata={
            "confidence": "0.50",
            "sample_size": str(h.count),
            "last_validated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        },
        what_works=[h.observation] if not h.pattern.startswith("failure") else [],
        what_doesnt_work=[h.observation] if h.pattern.startswith("failure") else [],
        evidence=evidence,
    )

    write_skill(skill)
    h_path.unlink()
    log.info("Promoted hypothesis %s → skill %s", h.id, name)
    return skill


def prune_hypotheses(max_age_days: int = 90) -> int:
    """Remove hypotheses older than max_age_days. Returns count removed."""
    _ensure_dirs()
    cutoff = datetime.now(timezone.utc)
    removed = 0
    for h in list_hypotheses():
        try:
            created = datetime.fromisoformat(h.created_at.replace("Z", "+00:00"))
            if (cutoff - created).days > max_age_days:
                path = HYPOTHESES_DIR / f"{h.id}.json"
                path.unlink(missing_ok=True)
                removed += 1
        except (ValueError, AttributeError):
            pass
    if removed:
        log.info("Pruned %d stale hypotheses", removed)
    return removed


def _infer_skill_type(pattern: str) -> SkillType:
    """Infer skill type from the pattern name."""
    p = pattern.lower()
    if p.startswith("failure"):
        return "failure"
    if p.startswith("regime"):
        return "regime"
    if p.startswith("indicator"):
        return "indicator"
    if p.startswith("combo"):
        return "combo"
    if p.startswith("params"):
        return "params"
    return "regime"


# ── Ideation Context ─────────────────────────────────────────────────────────


def get_ideation_context(regime: str | None = None, limit: int = 5) -> str:
    """Build a markdown context block for Brain ideation prompts.

    Loads the top skills (by confidence) relevant to the given regime.
    Falls back to general skills if regime-specific ones are scarce.
    """
    all_skills = list_skills()
    if not all_skills:
        return ""

    # Filter and sort by relevance
    if regime:
        regime_upper = regime.upper()
        regime_skills = [s for s in all_skills if s.regime.upper() == regime_upper]
        other_skills = [s for s in all_skills if s.regime.upper() != regime_upper]
        ranked = sorted(regime_skills, key=lambda s: s.confidence, reverse=True)
        # Fill with high-confidence general skills
        ranked.extend(sorted(other_skills, key=lambda s: s.confidence, reverse=True))
    else:
        ranked = sorted(all_skills, key=lambda s: s.confidence, reverse=True)

    top = ranked[:limit]
    if not top:
        return ""

    lines = [f"## Learned Knowledge ({len(all_skills)} total insights)\n"]

    works_items: list[str] = []
    avoid_items: list[str] = []

    for skill in top:
        tag = f"[{skill.name}, confidence={skill.confidence:.0%}, n={skill.sample_size}]"
        for item in skill.what_works:
            works_items.append(f"- {item} {tag}")
        for item in skill.what_doesnt_work:
            avoid_items.append(f"- {item} {tag}")

    if works_items:
        lines.append("### What Works")
        lines.extend(works_items)
        lines.append("")
    if avoid_items:
        lines.append("### What to Avoid")
        lines.extend(avoid_items)
        lines.append("")

    return "\n".join(lines)


# ── Consolidation ────────────────────────────────────────────────────────────


def run_consolidation() -> dict:
    """Periodic maintenance: archive weak skills, prune hypotheses.

    Returns a report dict.
    """
    _ensure_dirs()
    report = {"archived": 0, "stale_flagged": 0, "hypotheses_pruned": 0}

    now = datetime.now(timezone.utc)
    all_skills = list_skills()

    for skill in all_skills:
        # Archive low-confidence skills with enough samples
        if skill.confidence < 0.3 and skill.sample_size >= 20:
            _archive_skill(skill.name)
            report["archived"] += 1
            continue

        # Flag stale skills
        if skill.last_validated:
            try:
                last = datetime.strptime(skill.last_validated, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if (now - last).days > STALE_DAYS:
                    log.warning("Stale quant skill: %s (last validated %s)", skill.name, skill.last_validated)
                    report["stale_flagged"] += 1
            except ValueError:
                pass

    report["hypotheses_pruned"] = prune_hypotheses()

    log.info("Consolidation complete: %s", report)
    return report


# ── Three-Level Progressive Disclosure (P3-T04) ─────────────────────────────


_SKILL_VIEW_SECTIONS = {"what_works", "what_doesnt_work", "evidence", "metadata", "history"}


def quant_skills_list() -> list[dict]:
    """Return one summary row per skill — metadata only, no body, no evidence.

    Target token budget: ~2k tokens for 100 skills. Used as the L1 lister
    for Brain — operators see the whole catalog without paying full-body
    cost. Long-form ``description`` is intentionally omitted; call
    ``quant_skill_view(name)`` for the full envelope.
    """
    skills = list_skills()
    summary: list[dict] = []
    for s in skills:
        # Skip falsy fields where empty values would still cost ~10 tokens to
        # serialize. ``regime`` is empty for many skill types (indicator, params).
        row: dict = {
            "name": s.name,
            "type": s.skill_type,
            "confidence": round(s.confidence, 3),
            "samples": s.sample_size,
            "version": s.version,
        }
        if s.regime:
            row["regime"] = s.regime
        summary.append(row)
    return summary


def quant_skill_view(name: str, section: str | None = None) -> dict | list[dict] | None:
    """Return full skill detail (section=None) or a single section (L2/L3).

    Sections supported: ``what_works``, ``what_doesnt_work``, ``evidence``,
    ``metadata``, ``history``. Unknown section raises ValueError.
    """
    if section is None:
        return get_skill_detail(name)

    if section not in _SKILL_VIEW_SECTIONS:
        raise ValueError(
            f"Unknown skill section {section!r}. "
            f"Valid sections: {sorted(_SKILL_VIEW_SECTIONS)}"
        )

    if section == "history":
        return list_skill_history(name)

    skill = read_skill(name)
    if skill is None:
        return None

    if section == "what_works":
        return {"what_works": skill.what_works}
    if section == "what_doesnt_work":
        return {"what_doesnt_work": skill.what_doesnt_work}
    if section == "evidence":
        return {"evidence": skill.evidence}
    if section == "metadata":
        return {"metadata": skill.metadata}
    return None  # unreachable; section validated above


def _estimate_tokens(payload: list | dict) -> int:
    """Rough token estimate using JSON-string length / 4.

    The char/4 heuristic is well-known to overcount JSON content (compact
    structural tokens like ``"`` and ``,`` group into single LLM tokens), so
    treat the result as an upper bound. Good enough for budget guardrails.
    """
    serialized = json.dumps(payload, default=str)
    return max(1, len(serialized) // 4)


def get_skill_detail(name: str) -> dict | None:
    """Return full skill data for the frontend inspector panel."""
    skill = read_skill(name)
    if skill is None:
        return None
    return {
        "name": skill.name,
        "description": skill.description,
        "skill_type": skill.skill_type,
        "confidence": skill.confidence,
        "sample_size": skill.sample_size,
        "regime": skill.regime,
        "last_validated": skill.last_validated,
        "version": skill.version,
        "parent_version": skill.parent_version,
        "change_summary": skill.change_summary,
        "what_works": skill.what_works,
        "what_doesnt_work": skill.what_doesnt_work,
        "evidence": skill.evidence,
        "metadata": skill.metadata,
    }


def get_stats() -> dict:
    """Summary statistics for the pipeline view."""
    _ensure_dirs()
    all_skills = list_skills()
    hypotheses = list_hypotheses()

    # Count archived
    archived_count = 0
    if ARCHIVED_DIR.exists():
        archived_count = sum(1 for d in ARCHIVED_DIR.iterdir() if d.is_dir())

    total_evidence = sum(len(s.evidence) for s in all_skills)
    avg_confidence = 0.0
    if all_skills:
        avg_confidence = round(sum(s.confidence for s in all_skills) / len(all_skills), 3)

    return {
        "total_skills": len(all_skills),
        "total_hypotheses": len(hypotheses),
        "total_archived": archived_count,
        "avg_confidence": avg_confidence,
        "total_evidence": total_evidence,
    }


def dismiss_hypothesis(hypothesis_id: str) -> bool:
    """Delete a hypothesis file. Returns True if found and removed."""
    _ensure_dirs()
    path = HYPOTHESES_DIR / f"{hypothesis_id}.json"
    if path.exists():
        path.unlink()
        log.info("Dismissed hypothesis: %s", hypothesis_id)
        return True
    return False


def force_promote_hypothesis(hypothesis_id: str) -> QuantSkill | None:
    """Promote a hypothesis to a skill regardless of sample count."""
    _ensure_dirs()
    h_path = HYPOTHESES_DIR / f"{hypothesis_id}.json"
    if not h_path.exists():
        return None

    h = Hypothesis.from_dict(json.loads(h_path.read_text(encoding="utf-8")))
    name = _sanitize_name(h.pattern)
    skill_type = _infer_skill_type(h.pattern)

    evidence = [{"backtest_id": bid, "source": "hypothesis_promotion"} for bid in h.backtest_ids]

    skill = QuantSkill(
        name=name,
        description=h.observation,
        skill_type=skill_type,
        metadata={
            "confidence": "0.50",
            "sample_size": str(h.count),
            "last_validated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        },
        what_works=[h.observation] if not h.pattern.startswith("failure") else [],
        what_doesnt_work=[h.observation] if h.pattern.startswith("failure") else [],
        evidence=evidence,
    )

    write_skill(skill)
    h_path.unlink()
    log.info("Force-promoted hypothesis %s → skill %s", hypothesis_id, name)
    return skill


def _archive_skill(name: str) -> None:
    """Archive a skill without overwriting evidence from an earlier version."""
    src = SKILLS_DIR / _sanitize_name(name)
    dst = ARCHIVED_DIR / _sanitize_name(name)
    if src.exists():
        if src.is_symlink() or src.resolve().parent != SKILLS_DIR.resolve():
            raise ValueError("Skill archive source must stay inside the skills directory")
        if dst.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            dst = ARCHIVED_DIR / f"{_sanitize_name(name)}--{stamp}-{uuid4().hex[:8]}"
        if dst.resolve().parent != ARCHIVED_DIR.resolve():
            raise ValueError("Skill archive destination must stay inside the archive directory")
        shutil.move(str(src), str(dst))
        log.info("Archived quant skill: %s", name)
