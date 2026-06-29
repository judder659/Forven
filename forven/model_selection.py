"""Fail-closed model selection & route resolution — the spend-safety keystone.

This module is the single authority for the question *"which (provider, model)
is the bot allowed to call right now?"* It enforces one invariant:

    The bot only ever issues an LLM request for a (provider, model) pair that
    the operator has (a) explicitly CONNECTED in-app AND (b) explicitly
    SELECTED for the relevant slot (an agent, an auxiliary task, a backup, or a
    routing default). If no such route exists for a needed slot, callers fail
    closed by raising ``UnconfiguredRouteError`` — they must NEVER substitute a
    hardcoded default and spend the user's credits on a model they didn't pick.

Two independent gates compose:

* CONNECTED — a provider is "connected" only if the operator explicitly added
  it in the app (persisted in the ``forven:connected-providers`` KV set) AND a
  usable token exists. A key present only in the environment (a stray
  ``OPENAI_API_KEY`` etc.) does NOT count as connected — it cannot authorize
  spend on its own. ``migrate_connected_from_profiles`` seeds the set from
  existing in-app credential profiles so upgrading installs keep working.

* SELECTED — the explicit (provider, model) pairs the operator chose: every
  agent row's model, the auxiliary task models, the backup, the per-provider
  routing defaults, and any user-configured fallback entries. The enabled model
  list (``agent_model_keys``) bounds which models of a connected provider are
  selectable at all.

``resolve_route`` and ``assert_callable`` intersect the two gates. Everything
else in the codebase routes through them; the lowest-level HTTP callers call
``assert_callable`` as a last line of defense.
"""

from __future__ import annotations

import logging

from forven.db import kv_get, kv_set

log = logging.getLogger("forven.model_selection")

_CONNECTED_PROVIDERS_KEY = "forven:connected-providers"
_SETTINGS_STORAGE_KEY = "forven:settings"
# Enforcement is gated so a process that has not run startup migration (tests,
# scripts, a half-upgraded install) is never accidentally locked to fail-closed
# before the connected set is seeded. Production startup calls
# enable_enforcement() right after migrate_connected_from_profiles().
_ENFORCEMENT_KEY = "forven:model-selection-enforced"


def enforcement_enabled() -> bool:
    """Whether the fail-closed gate actively blocks calls (set at startup)."""
    return bool(kv_get(_ENFORCEMENT_KEY, False))


def enable_enforcement() -> None:
    kv_set(_ENFORCEMENT_KEY, True)


def disable_enforcement() -> None:
    kv_set(_ENFORCEMENT_KEY, False)


def ensure_enforcement_armed() -> None:
    """Arm fail-closed enforcement in the CURRENT process, idempotently.

    Spend-safety must not depend on the API lifespan having run first. Every
    process that can issue an LLM call — the Discord bot when it owns the runtime,
    the daemon, and CLI commands — calls this at startup so assert_callable /
    resolve_route are never silently no-ops in a process that spends. Safe to call
    repeatedly and cheap once armed.
    """
    if enforcement_enabled():
        return
    try:
        migrate_connected_from_profiles()
    except Exception:  # pragma: no cover — defence in depth
        import logging
        logging.getLogger(__name__).warning(
            "ensure_enforcement_armed: connected-set migration failed", exc_info=True
        )
    enable_enforcement()


class UnconfiguredRouteError(RuntimeError):
    """Raised when no connected+selected (provider, model) exists for a slot.

    Fail-closed signal: the caller must surface this (loudly) rather than fall
    back to a hardcoded model. ``slot`` names what the operator needs to
    configure (e.g. ``"agent:strategy-developer"``, ``"auxiliary:recall"``).
    """

    def __init__(self, slot: str, detail: str = ""):
        self.slot = slot
        msg = f"No connected & selected model is configured for {slot!r}."
        if detail:
            msg += f" {detail}"
        msg += (
            " The bot will not spend on a model you have not explicitly connected "
            "and selected — configure it in the Agents page."
        )
        super().__init__(msg)


# --------------------------------------------------------------------------- #
# Connected providers
# --------------------------------------------------------------------------- #

def list_connected_providers() -> set[str]:
    """Providers the operator explicitly connected in-app (persisted set)."""
    raw = kv_get(_CONNECTED_PROVIDERS_KEY, [])
    if not isinstance(raw, list):
        return set()
    return {str(p).strip().lower() for p in raw if str(p).strip()}


def _save_connected_providers(providers: set[str]) -> None:
    kv_set(_CONNECTED_PROVIDERS_KEY, sorted(providers))


def mark_provider_connected(provider: str) -> None:
    """Record that the operator connected ``provider`` in-app (idempotent)."""
    p = str(provider or "").strip().lower()
    if not p:
        return
    current = list_connected_providers()
    if p not in current:
        current.add(p)
        _save_connected_providers(current)
        log.info("provider %s marked connected (in-app)", p)


def unmark_provider_connected(provider: str) -> None:
    """Forget an in-app connection (operator disconnected the provider)."""
    p = str(provider or "").strip().lower()
    current = list_connected_providers()
    if p in current:
        current.discard(p)
        _save_connected_providers(current)
        log.info("provider %s unmarked (disconnected)", p)


# Local OpenAI-compatible servers (e.g. LM Studio) are keyless: get_token()
# returns "" by design and there is no spend to authorize. For these a
# configured profile (a base URL) is enough to be usable — requiring a bearer
# token would make them permanently unconnectable AND uncallable.
_KEYLESS_PROVIDERS = {"lmstudio", "claude-cli"}


def _provider_has_token(provider: str) -> bool:
    """Whether a usable credential exists for ``provider`` (keyless-aware)."""
    p = str(provider or "").strip().lower()
    try:
        from forven.auth.store import get_profile, get_token

        if p in _KEYLESS_PROVIDERS:
            return get_profile(p) is not None
        return bool(get_token(p))
    except Exception:
        return False


def provider_is_connected(provider: str) -> bool:
    """True iff the operator connected ``provider`` in-app AND a token resolves.

    The in-app connection record is required: a provider whose key exists only
    as an environment variable is intentionally NOT considered connected, so a
    stray env var can never authorize spend on a provider the operator never
    chose.
    """
    p = str(provider or "").strip().lower()
    if not p:
        return False
    if p not in list_connected_providers():
        return False
    return _provider_has_token(p)


def migrate_connected_from_profiles() -> set[str]:
    """One-shot: seed the connected set from existing in-app credential profiles.

    Run at startup so installs that configured providers before this gate
    existed keep working without re-connecting. Only providers with a stored
    auth profile (added through the app) are seeded — providers credentialed
    only via env vars are deliberately NOT auto-connected.
    """
    try:
        from forven.auth.store import load_auth
    except Exception:
        return list_connected_providers()

    seeded = list_connected_providers()
    before = set(seeded)
    try:
        profiles = (load_auth() or {}).get("profiles", {})
    except Exception:
        profiles = {}
    for key in profiles:
        provider = str(key).partition(":")[0].strip().lower()
        if provider:
            seeded.add(provider)
    if seeded != before:
        _save_connected_providers(seeded)
        log.info("connected providers seeded from profiles: %s", sorted(seeded - before))
    return seeded


# --------------------------------------------------------------------------- #
# Selected (provider, model) pairs
# --------------------------------------------------------------------------- #

def _enabled_model_keys() -> set[tuple[str, str]]:
    """The operator's enabled model list (agent_model_keys) as (provider, model)."""
    settings = kv_get(_SETTINGS_STORAGE_KEY, {})
    raw = settings.get("agent_model_keys") if isinstance(settings, dict) else None
    pairs: set[tuple[str, str]] = set()
    if isinstance(raw, list):
        for item in raw:
            provider, sep, model = str(item).partition(":")
            if sep and provider.strip() and model.strip():
                pairs.add((provider.strip().lower(), model.strip()))
    return pairs


def _agent_selections() -> set[tuple[str, str]]:
    """Every enabled agent's explicitly-configured (provider, model)."""
    pairs: set[tuple[str, str]] = set()
    try:
        from forven.db import get_db

        with get_db() as conn:
            rows = conn.execute(
                "SELECT model, model_id FROM agents WHERE enabled = 1"
            ).fetchall()
        for row in rows:
            provider = str(row["model"] or "").strip().lower()
            model = str(row["model_id"] or "").strip()
            if provider and model:
                pairs.add((provider, model))
    except Exception:
        pass
    return pairs


def _routing_selections() -> set[tuple[str, str]]:
    """Explicit (provider, model) pairs from the routing policy & backup.

    Includes per-provider default models, auxiliary task models, configured
    fallback entries, and the operator's backup choice. These are the operator
    surfaces (Routing & Fallbacks tab) that write the policy.
    """
    pairs: set[tuple[str, str]] = set()
    try:
        from forven.model_routing import get_model_routing

        policy = get_model_routing()
    except Exception:
        policy = {}

    for provider, model in (policy.get("default_models") or {}).items():
        if provider and model:
            pairs.add((str(provider).strip().lower(), str(model).strip()))

    for entry in (policy.get("auxiliary") or {}).values():
        if isinstance(entry, dict) and entry.get("provider") and entry.get("model_id"):
            pairs.add((str(entry["provider"]).strip().lower(), str(entry["model_id"]).strip()))

    for chain in (policy.get("fallback_chains") or {}).values():
        for entry in chain or []:
            if isinstance(entry, dict) and entry.get("provider") and entry.get("model_id"):
                pairs.add((str(entry["provider"]).strip().lower(), str(entry["model_id"]).strip()))

    try:
        from forven.config import get_backup_ai_model, get_backup_ai_provider

        bp = str(get_backup_ai_provider() or "").strip().lower()
        bm = str(get_backup_ai_model() or "").strip()
        if bp and bp != "none" and bm:
            pairs.add((bp, bm))
    except Exception:
        pass

    return pairs


def allowed_pairs() -> set[tuple[str, str]]:
    """All (provider, model) pairs the bot may call: SELECTED ∩ CONNECTED.

    A pair is allowed only if its provider is connected in-app and the model
    appears in at least one operator selection surface (agent row, enabled
    model list, routing default, auxiliary slot, fallback entry, or backup).
    """
    connected = list_connected_providers()
    candidate = (
        _enabled_model_keys()
        | _agent_selections()
        | _routing_selections()
    )
    return {
        (provider, model)
        for (provider, model) in candidate
        if provider in connected and _provider_has_token(provider)
    }


def _pair_allowed(provider: str, model: str, allowed: set[tuple[str, str]]) -> bool:
    """Membership test that ignores MODEL casing.

    A model id is the same model regardless of case (the catalog's canonical
    ``MiniMax-M2.7`` vs a stored/lowercased ``minimax-m2.7``); the provider is
    already case-normalized. Matching the model case-insensitively means a
    selection in any casing authorizes a call in any casing — otherwise a
    genuinely connected+selected model is wrongly rejected on a mere case
    mismatch (the exact "minimax connected but model not selected" false
    negative operators hit).
    """
    p = str(provider or "").strip().lower()
    m = str(model or "").strip()
    if (p, m) in allowed:
        return True
    ml = m.lower()
    return any(ap == p and am.lower() == ml for (ap, am) in allowed)


def is_callable(provider: str, model: str) -> bool:
    """Whether a specific (provider, model) is connected AND selected."""
    p = str(provider or "").strip().lower()
    m = str(model or "").strip()
    if not p or not m:
        return False
    return _pair_allowed(p, m, allowed_pairs())


def assert_callable(provider: str, model: str, *, slot: str = "request") -> None:
    """Last-line-of-defense gate. Raise ``UnconfiguredRouteError`` if not allowed.

    Called by the lowest-level HTTP callers so that even if a chain leaks an
    unconfigured provider/model, no outbound request to a paid API is issued for
    a model the operator did not connect AND select.

    No-op until enforcement is enabled at startup, so an un-migrated process
    (tests, scripts) is never locked closed before the connected set is seeded.
    """
    if not enforcement_enabled():
        return
    p = str(provider or "").strip().lower()
    m = str(model or "").strip()
    if not provider_is_connected(p):
        raise UnconfiguredRouteError(
            slot, detail=f"Provider {p!r} is not connected in-app."
        )
    if not _pair_allowed(p, m, allowed_pairs()):
        raise UnconfiguredRouteError(
            slot,
            detail=(
                f"Provider {p!r} is connected, but model {m!r} is not enabled/selected. "
                f"Enable it under Agents → Models, or pick a connected+enabled model "
                f"for this slot under Routing & Fallbacks."
            ),
        )


def resolve_route(
    slot: str,
    provider: str | None,
    model: str | None,
    *,
    fallbacks: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Return the ordered, fail-closed route for a slot.

    The returned list contains only (provider, model) pairs that are connected
    AND selected: the requested primary first (if allowed), then each provided
    fallback (if allowed), de-duplicated. Raises ``UnconfiguredRouteError`` when
    nothing is callable — callers must surface that, never substitute a default.

    ``fallbacks`` are the operator's explicit per-slot fallback pairs; an empty
    list means "no fallback" (fail closed), never the legacy hardcoded chain.

    When enforcement is disabled (un-migrated process), the requested pair and
    fallbacks pass through unfiltered so callers behave as before — the
    connected/selected gate only bites once startup has enabled it.
    """
    enforced = enforcement_enabled()
    allowed = allowed_pairs() if enforced else None
    route: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(p: object, m: object) -> None:
        pair = (str(p or "").strip().lower(), str(m or "").strip())
        if not pair[0] or not pair[1] or pair in seen:
            return
        if enforced and not _pair_allowed(pair[0], pair[1], allowed):
            return
        seen.add(pair)
        route.append(pair)

    _add(provider, model)
    for fb_provider, fb_model in (fallbacks or []):
        _add(fb_provider, fb_model)

    if not route:
        raise UnconfiguredRouteError(
            slot,
            detail=(
                f"Requested {str(provider or '').strip().lower()}/"
                f"{str(model or '').strip()} is not connected & selected."
            ),
        )
    return route


def _policy_slot_fallbacks(slot_key: str) -> list[tuple[str, str]]:
    """Read the operator-configured fallback list for a slot key from the policy."""
    try:
        from forven.model_routing import get_model_routing

        chain = (get_model_routing().get("fallback_chains") or {}).get(slot_key) or []
    except Exception:
        chain = []
    out: list[tuple[str, str]] = []
    for entry in chain:
        if isinstance(entry, dict) and entry.get("provider") and entry.get("model_id"):
            out.append((str(entry["provider"]).strip().lower(), str(entry["model_id"]).strip()))
    return out


def resolve_agent_route(agent_id: str, provider: str, model: str) -> list[tuple[str, str]]:
    """Fail-closed route for an agent: its model + its per-agent fallback chain.

    The per-agent fallback list is stored under ``fallback_chains['agent:<id>']``
    (Routing tab). Falls back, after the agent's own chain, to the operator's
    global backup so a configured backup still applies.
    """
    fallbacks = _policy_slot_fallbacks(f"agent:{agent_id}")
    # Append the global backup (provider+model) as a final hop if configured.
    try:
        from forven.config import get_backup_ai_model, get_backup_ai_provider

        bp = str(get_backup_ai_provider() or "").strip().lower()
        bm = str(get_backup_ai_model() or "").strip()
        if bp and bp != "none" and bm and (bp, bm) not in fallbacks:
            fallbacks = [*fallbacks, (bp, bm)]
    except Exception:
        pass
    return resolve_route(f"agent:{agent_id}", provider, model, fallbacks=fallbacks)
