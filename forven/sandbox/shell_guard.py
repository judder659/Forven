"""Tirith-style shell-command scanner (P2-T07).

Pre-flight regex-based scan layered on top of ``run_shell`` (in
:mod:`forven.agents.tools_core`). The existing inline blocklist there
catches obvious patterns; this module adds:

- Severity tiers (low/medium/high/critical) so the operator can choose
  fail-closed vs warn-allow per category.
- Structured ``ShellReport`` with findings the router/UI can render.
- Strict mode (``sandbox.shell_guard_strict=true``) that upgrades all
  tiers to fail-closed.

Design contract:

* **Fail-closed for critical** (always — even with strict=false). These
  are unambiguously destructive: ``rm -rf /``, fork bombs, ``mkfs``, raw
  device writes.
* **Warn-but-allow** for high/medium/low when strict=false. Findings are
  returned to the caller (``run_shell``) and logged for after-the-fact review.
* **Fail-open** if the scanner module itself raises (regex compile error,
  etc.) — log a WARN and let ``run_shell`` proceed. Better to run a shell
  command unprotected than to brick the platform.
"""
from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger("forven.sandbox.shell_guard")

Severity = Literal["low", "medium", "high", "critical"]


@dataclass
class ShellFinding:
    severity: Severity
    pattern: str
    message: str


@dataclass
class ShellReport:
    ok: bool
    severity: Severity
    findings: list[ShellFinding] = field(default_factory=list)


# ---- Pattern table --------------------------------------------------------
# Tuples of (severity, compiled regex, message). Order doesn't matter — we
# scan all patterns and pick the highest severity for the report's `severity`
# field. Patterns are case-insensitive unless they need case for clarity.

_PATTERNS: list[tuple[Severity, re.Pattern[str], str]] = [
    # ---- critical (always fail-closed) -----------------------------------
    ("critical", re.compile(r"\brm\s+-rf\s+/(\s|$)"), "rm -rf /"),
    ("critical", re.compile(r"\brm\s+-rf\s+~(\s|/|$)"), "rm -rf ~"),
    ("critical", re.compile(r"\brm\s+-rf\s+/\*"), "rm -rf /*"),
    ("critical", re.compile(r"\brm\s+-rf\s+--no-preserve-root\b"), "rm -rf --no-preserve-root"),
    ("critical", re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    ("critical", re.compile(r"\bmkfs(\.[a-z0-9]+)?\b"), "mkfs filesystem wipe"),
    ("critical", re.compile(r"\bwipefs\b"), "wipefs filesystem wipe"),
    ("critical", re.compile(r"\bdd\b.*\bif=.*\bof=/dev/sd[a-z]"), "dd to raw disk"),
    ("critical", re.compile(r">\s*/dev/sd[a-z]"), "redirect to raw disk"),
    # ---- high (warn-allow by default) ------------------------------------
    ("high", re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sh|bash|zsh|fish)\b"), "pipe-to-shell download"),
    ("high", re.compile(r"\bchmod\b\s+-?R?\s*0?777\b"), "chmod 777"),
    ("high", re.compile(r"\bchown\b\s+(-R\s+)?(root|0)(\s|:|$)"), "chown to root"),
    ("high", re.compile(r"\bnc\s+-l(p)?\b"), "netcat listener"),
    ("high", re.compile(r"\bbash\s+-i\b"), "interactive bash reverse shell"),
    ("high", re.compile(r"/dev/tcp/"), "/dev/tcp reverse shell"),
    ("high", re.compile(r"\b(python|python3|perl|ruby)\s+-c\s+.*(socket|exec)"), "scripted reverse shell"),
    # ---- medium (warn-allow) ---------------------------------------------
    ("medium", re.compile(r"\bprintenv\b"), "env enumeration"),
    ("medium", re.compile(r"\benv\b\s*$", re.MULTILINE), "env enumeration"),
    ("medium", re.compile(r"~/\.ssh/"), "ssh key access"),
    ("medium", re.compile(r"/etc/(passwd|shadow)"), "credential file access"),
    ("medium", re.compile(r"\bset\s*$", re.MULTILINE), "shell variable dump"),
    # ---- low (informational) ---------------------------------------------
    ("low", re.compile(r"\bsudo\b"), "sudo invocation"),
]


_SEVERITY_RANK: dict[Severity, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _to_string(cmd: str | list[str]) -> str:
    if isinstance(cmd, list):
        try:
            return shlex.join(cmd)
        except Exception:  # noqa: BLE001
            return " ".join(str(p) for p in cmd)
    return cmd


def scan_command(cmd: str | list[str]) -> ShellReport:
    """Scan *cmd* against the pattern table.

    Never raises — on internal error returns ``ShellReport(ok=True, ...)``
    so the caller fails open (matching T08's symmetric fall-back).
    """
    try:
        text = _to_string(cmd)
    except Exception as exc:  # noqa: BLE001
        log.warning("shell_guard could not stringify cmd: %s — failing open", exc)
        return ShellReport(ok=True, severity="low")

    findings: list[ShellFinding] = []
    try:
        for severity, pat, message in _PATTERNS:
            if pat.search(text):
                findings.append(ShellFinding(
                    severity=severity, pattern=pat.pattern, message=message
                ))
    except Exception as exc:  # noqa: BLE001
        log.warning("shell_guard regex scan failed: %s — failing open", exc)
        return ShellReport(ok=True, severity="low")

    if not findings:
        return ShellReport(ok=True, severity="low")

    # Pick the highest severity for the report-level severity field.
    top_rank = max(_SEVERITY_RANK[f.severity] for f in findings)
    top_severity: Severity
    for sev, rank in _SEVERITY_RANK.items():
        if rank == top_rank:
            top_severity = sev  # noqa: PLW2901 — first match wins on tie
            break
    else:
        top_severity = "low"

    # Fail-closed only for critical by default. Strict mode (operator
    # opts in via sandbox.shell_guard_strict=true) is decided by the
    # CALLER reading settings — keep this module pure-functional.
    ok = top_rank < _SEVERITY_RANK["critical"]
    return ShellReport(ok=ok, severity=top_severity, findings=findings)


def is_strict_mode_enabled() -> bool:
    """Helper for callers that want to upgrade non-critical findings.

    Reads ``sandbox_shell_guard_strict`` from the persisted settings dict
    (managed by ``forven.api_core.get_settings``). Falls back to False if
    settings can't be loaded — fail-open by design.
    """
    try:
        from forven.api_core import get_settings  # noqa: PLC0415

        s = get_settings()
        if isinstance(s, dict):
            return bool(s.get("sandbox_shell_guard_strict", False))
        return bool(getattr(s, "sandbox_shell_guard_strict", False))
    except Exception:  # noqa: BLE001
        return False


def evaluate_for_run_shell(cmd: str | list[str]) -> tuple[bool, ShellReport]:
    """Convenience for run_shell wiring: returns (allowed, report).

    *allowed* is False iff the report is critical OR strict mode is on
    AND any finding exists. The caller surfaces findings of any severity to
    the operator regardless of allow/block.
    """
    report = scan_command(cmd)
    if not report.ok:
        return False, report  # critical, fail-closed
    if report.findings and is_strict_mode_enabled():
        return False, report  # strict mode upgrades to fail-closed
    return True, report
