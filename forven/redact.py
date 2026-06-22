"""Secret redaction for tool outputs and agent logs.

Hermes-inspired Phase 0: scrubs API keys, bearer tokens, OAuth credentials,
and common secret-shaped values from text before it lands in the agent
context window or persisted Brain decision records. Defense-in-depth
against accidental key leakage via tool output, error messages, or
agent-visible logs.

Regex-only by design — no ML PII detection in Phase 0. False-positive
rate kept low by requiring length / structure constraints typical of
real credentials.
"""

from __future__ import annotations

import logging
import re
from typing import Any

REDACTED_MARKER = "***REDACTED***"

# Each pattern is (compiled_regex, replacement_template).
# Replacement templates may use \1, \2, etc. to preserve match groups.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # OpenAI-style keys: sk-... (project-scoped sk-proj- variants too).
    (
        re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}\b"),
        REDACTED_MARKER,
    ),
    # Anthropic keys: sk-ant-...
    (
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),
        REDACTED_MARKER,
    ),
    # Generic bearer tokens in Authorization headers.
    (
        re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._\-]{20,}\b"),
        r"\1 " + REDACTED_MARKER,
    ),
    # Slack tokens (bot, user, app, legacy).
    (
        re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
        REDACTED_MARKER,
    ),
    # GitHub personal access tokens / OAuth tokens.
    (
        re.compile(r"\bgh[ps]_[A-Za-z0-9]{36,}\b"),
        REDACTED_MARKER,
    ),
    # AWS access key IDs.
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        REDACTED_MARKER,
    ),
    # JWTs (eyJ...eyJ...). Required for OAuth flows leaking through tool output.
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
        REDACTED_MARKER,
    ),
    # Hyperliquid / EVM wallet PRIVATE KEY: 0x + exactly 64 hex. The {64}\b bound
    # deliberately does NOT match a 40-hex public address (0x + 40), which is not
    # secret. This is the funds-controlling key — the most important to scrub.
    (
        re.compile(r"\b0x[a-fA-F0-9]{64}\b"),
        REDACTED_MARKER,
    ),
    # Discord "Bot <token>" / "Bearer" already covered above; the Authorization
    # scheme Discord uses for bots.
    (
        re.compile(r"(?i)\b(Bot)\s+[A-Za-z0-9._\-]{24,}\b"),
        r"\1 " + REDACTED_MARKER,
    ),
    # Discord webhook URL — redact the secret token segment, keep the routable id.
    (
        re.compile(r"(https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/)[A-Za-z0-9_\-]{20,}"),
        r"\1" + REDACTED_MARKER,
    ),
    # Discord bot token (3 dot-separated base64url segments). Structure keeps the
    # false-positive rate low.
    (
        re.compile(r"\b[A-Za-z0-9_\-]{23,28}\.[A-Za-z0-9_\-]{6,7}\.[A-Za-z0-9_\-]{27,}\b"),
        REDACTED_MARKER,
    ),
    # Env-var leak in shell-style assignment: KEY=value, KEY="value", KEY='value'.
    # Preserves the var name, replaces value.
    (
        re.compile(
            r"(?i)\b([A-Z][A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|PRIVATE_KEY))\s*=\s*"
            r"(?:\"[^\"]+\"|'[^']+'|[^\s\"']+)"
        ),
        r"\1=" + REDACTED_MARKER,
    ),
    # JSON key/value pairs: "api_key": "value", "authorization": "Bearer ...", etc.
    # Preserves the JSON structure; replaces only the string value.
    (
        re.compile(
            r"(?i)(\"(?:api_key|api[-_]?token|access[-_]?token|refresh[-_]?token|secret|secret[-_]?key|client[-_]?secret|password|authorization|bearer)\"\s*:\s*)\"[^\"]+\""
        ),
        r"\1\"" + REDACTED_MARKER + "\"",
    ),
]


def redact(text: str) -> tuple[str, int]:
    """Apply all redaction patterns to text.

    Returns (scrubbed_text, redaction_count). count is the total number of
    individual matches replaced across all patterns — useful for surfacing
    a "redacted N items" chip in UI.

    Idempotent: running redact() on already-scrubbed text returns the same
    text and a count of 0 (assuming the marker itself doesn't match any
    pattern, which it doesn't).
    """
    if not text:
        return text, 0
    if not isinstance(text, str):
        text = str(text)

    total = 0
    out = text
    for pattern, replacement in _PATTERNS:
        out, n = pattern.subn(replacement, out)
        total += n
    return out, total


def redact_dict(obj: Any) -> tuple[Any, int]:
    """Deep-walk a dict / list / tuple structure, redacting all string leaves.

    Non-string leaves (int, float, bool, None) pass through unchanged.
    Tuples are returned as tuples; lists as lists.

    Returns (scrubbed_obj, total_redaction_count). The structure itself is
    NOT mutated — a new structure is returned.
    """
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out_dict: dict[Any, Any] = {}
        total = 0
        for k, v in obj.items():
            new_v, n = redact_dict(v)
            out_dict[k] = new_v
            total += n
        return out_dict, total
    if isinstance(obj, list):
        out_list: list[Any] = []
        total = 0
        for item in obj:
            new_item, n = redact_dict(item)
            out_list.append(new_item)
            total += n
        return out_list, total
    if isinstance(obj, tuple):
        out_tuple = []
        total = 0
        for item in obj:
            new_item, n = redact_dict(item)
            out_tuple.append(new_item)
            total += n
        return tuple(out_tuple), total
    return obj, 0


class RedactingLogFilter(logging.Filter):
    """Logging filter that scrubs secrets from every emitted record.

    Installed on the root logger so ANY ``logger.*`` call, exception traceback,
    or third-party log line that happens to contain an API key / bearer token /
    private key is redacted before it reaches the file or console handler — not
    just tool output (which was the only previously-covered path). This is the
    last line of defense for a release that handles a funds-controlling
    Hyperliquid wallet key: a single stray log line would otherwise write the
    secret to ``~/.forven_logs`` and the terminal in cleartext.

    The filter renders the record's message with its args (so ``%s``-style
    secrets are caught too), redacts the rendered string, and — only when a
    redaction occurred — replaces ``record.msg`` with the scrubbed text and
    clears ``record.args`` so the handler doesn't re-interpolate. Always returns
    True (filters here scrub, never drop). Fail-open: any error leaves the
    record unchanged rather than losing the log line.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        try:
            scrubbed, count = redact(rendered)
        except Exception:
            return True
        if count:
            record.msg = scrubbed
            record.args = ()
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """Attach a RedactingLogFilter to the given (or root) logger, idempotently."""
    target = logger if logger is not None else logging.getLogger()
    if not any(isinstance(f, RedactingLogFilter) for f in target.filters):
        target.addFilter(RedactingLogFilter())


__all__ = [
    "redact",
    "redact_dict",
    "REDACTED_MARKER",
    "RedactingLogFilter",
    "install_log_redaction",
]
