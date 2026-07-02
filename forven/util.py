"""Shared utilities for Forven."""

import base64
import hashlib
import logging
import math
import os
import secrets
from typing import Any

log = logging.getLogger("forven")


def is_remote() -> bool:
    """Detect if running in a remote/headless environment (SSH, codespaces, etc.)."""
    indicators = [
        "SSH_CLIENT",
        "SSH_TTY",
        "SSH_CONNECTION",
        "REMOTE_CONTAINERS",
        "CODESPACES",
    ]
    if any(os.environ.get(k) for k in indicators):
        return True
    # Headless Linux: no DISPLAY and no WAYLAND_DISPLAY
    if os.name == "posix" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return True
    return False


def generate_pkce() -> tuple[str, str]:
    """Generate PKCE verifier and S256 challenge."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def generate_state() -> str:
    """Generate a random OAuth state parameter."""
    return secrets.token_urlsafe(32)


def normalize_stage(value: str | None) -> str:
    """Consolidated logic to map lifecycle aliases to canonical stages.

    Tradable pipeline:
    quick_screen -> gauntlet -> paper -> live_graduated

    Side lane:
    research_only

    Terminal states:
    archived, rejected, backtest_failed
    """
    normalized = str(value or "").strip().lower()
    if not normalized:
        return "quick_screen"
    
    aliases = {
        # quick_screen aliases
        "researching": "quick_screen",
        "developing": "quick_screen",
        "ideation": "quick_screen",
        "candidate": "quick_screen",
        "generated": "quick_screen",

        # research_only aliases
        "research_only": "research_only",
        "research-only": "research_only",
        "researchonly": "research_only",
        
        # gauntlet aliases
        "backtesting": "gauntlet",
        "testing": "gauntlet",
        "validation": "gauntlet",
        "ranked": "gauntlet",
        
        # paper aliases
        "paper_trading": "paper",
        "papertrading": "paper",
        "paper-trading": "paper",
        "paper_queued": "paper",
        "paper_running": "paper",
        "paper_evaluated": "paper",
        "paper_staging": "paper",
        
        # live_graduated aliases
        "deployed": "live_graduated",
        "live": "live_graduated",
        "execution": "live_graduated",
        "review": "live_graduated",
        "ceo_review": "live_graduated",
        "ceoreview": "live_graduated",
        "ceo-review": "live_graduated",
        "promoted": "live_graduated",
        
        # Terminal states
        "retired": "archived",
        "trash": "archived",
        "killed": "archived",
        "deprecated": "archived",
        "failed": "rejected",
        "backtest_failed": "backtest_failed",
        "backtest-failed": "backtest_failed",
        "backtestfailed": "backtest_failed",
    }
    
    mapped = aliases.get(normalized, normalized)
    valid = {
        "quick_screen",
        "research_only",
        "gauntlet",
        "paper",
        "live_graduated",
        "archived",
        "rejected",
        "backtest_failed",
    }
    return mapped if mapped in valid else "quick_screen"


def params_fingerprint(params: Any) -> str | None:
    """Stable short fingerprint of a strategy params blob (dict or JSON string).

    Robustness validation rows stamp the fingerprint of the params they were run
    against; the gauntlet status compares it to the strategy's CURRENT params to
    flag results as STALE once the params they validated have changed. Returns
    None for empty/unparseable input (callers treat that as "staleness unknown").
    """
    import json

    if params is None:
        return None
    value = params
    if isinstance(value, (bytes, str)):
        text = value.decode() if isinstance(value, bytes) else value
        text = text.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except Exception:
            value = text
    try:
        canonical = json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def sanitize_json_floats(value: Any) -> Any:
    """Recursively replace non-finite floats (nan, +/-inf) with None.

    Starlette's JSONResponse rejects these with `ValueError: Out of range
    float values are not JSON compliant`. Metrics like profit_factor can
    legitimately be +inf (zero losses); a sibling `*_is_infinite` flag
    preserves the signal once the wire value is None.
    """
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: sanitize_json_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_json_floats(v) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize_json_floats(v) for v in value)
    return value
