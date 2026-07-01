"""Token storage, auto-refresh, and migration for Forven auth."""

import json
import os
import shutil
import time
import logging
from datetime import datetime, timezone

import httpx
from filelock import FileLock
from rich.console import Console
from rich.table import Table

from forven.config import AUTH_FILE, OPENCLAW_AUTH, ensure_dirs
from forven.secret_storage import decrypt_secret, encrypt_secret

console = Console()
log = logging.getLogger("forven.auth.store")

LOCK_PATH = AUTH_FILE.with_suffix(".lock")
REFRESH_BUFFER_MS = 5 * 60 * 1000  # 5 minutes before expiry
_SUPPORTED_AUTH_PROVIDERS = {"openai", "minimax", "lmstudio", "zai", "openrouter", "anthropic", "deepseek", "groq", "gemini", "cerebras", "mistral", "xai", "together", "nvidia", "opencode-zen", "opencode-go"}
_AUTH_SECRET_FIELDS = {"access", "refresh", "token", "id_token", "api_key", "api_secret"}

# Runtime-only marker attached to a profile whose ciphertext could not be
# decrypted with the current key. Never serialized to disk — stripped on save.
# Preserving the profile (rather than dropping it) keeps the original
# ciphertext intact so it becomes readable again if the correct key returns.
_OPAQUE_MARKER = "__opaque__"
_AUTH_BACKUP_COUNT = 3

# Dedupe decrypt-failure warnings per-process: `_build_auth_provider_payload`
# is called once per provider per Settings page load and re-walks the store
# each time, so naive logging floods the log with the same line.
_WARNED_OPAQUE_FINGERPRINTS: set[str] = set()
_ENV_ACCESS_TOKEN_KEYS = {
    "openai": ("OPENAI_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "lmstudio": ("LMSTUDIO_API_KEY",),
    "zai": ("ZAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "cerebras": ("CEREBRAS_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "xai": ("XAI_API_KEY", "GROK_API_KEY"),
    "together": ("TOGETHER_API_KEY", "TOGETHER_AI_API_KEY"),
    "nvidia": ("NVIDIA_API_KEY",),
    "opencode-zen": ("OPENCODE_ZEN_API_KEY", "OPENCODE_API_KEY"),
    "opencode-go": ("OPENCODE_GO_API_KEY",),
}
_ENV_BASE_URL_KEYS = {
    "lmstudio": ("LMSTUDIO_BASE_URL",),
    "zai": ("ZAI_BASE_URL", "ANTHROPIC_BASE_URL"),
    "anthropic": ("ANTHROPIC_BASE_URL",),
    "deepseek": ("DEEPSEEK_BASE_URL",),
    "groq": ("GROQ_BASE_URL",),
    "gemini": ("GEMINI_BASE_URL",),
    "cerebras": ("CEREBRAS_BASE_URL",),
    "mistral": ("MISTRAL_BASE_URL",),
    "xai": ("XAI_BASE_URL",),
    "together": ("TOGETHER_BASE_URL",),
    "opencode-zen": ("OPENCODE_ZEN_BASE_URL",),
    "opencode-go": ("OPENCODE_GO_BASE_URL",),
}


def _sanitize_auth_store(store: dict) -> dict:
    """Drop unsupported provider profiles and normalize legacy keys to canonical names."""
    if not isinstance(store, dict):
        return {"version": 1, "profiles": {}}

    try:
        version = int(store.get("version", 1))
    except Exception:
        version = 1

    raw_profiles = store.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raw_profiles = {}

    cleaned_profiles: dict = {}
    for raw_key, raw_profile in raw_profiles.items():
        if not isinstance(raw_key, str) or not isinstance(raw_profile, dict):
            continue
        provider, sep, profile_name = raw_key.partition(":")
        if sep != ":":
            continue

        provider_key = provider.strip().lower()
        if provider_key not in _SUPPORTED_AUTH_PROVIDERS:
            continue

        suffix = profile_name.strip() or "default"
        profile = dict(raw_profile)
        profile["provider"] = provider_key
        cleaned_profiles[f"{provider_key}:{suffix}"] = profile

    return {
        "version": version,
        "profiles": cleaned_profiles,
    }


def _lock():
    return FileLock(str(LOCK_PATH), timeout=10)


def _first_env_value(keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _env_profile(provider: str) -> dict:
    normalized_provider = str(provider or "").strip().lower()
    token = _first_env_value(_ENV_ACCESS_TOKEN_KEYS.get(normalized_provider, ()))
    base_url = _first_env_value(_ENV_BASE_URL_KEYS.get(normalized_provider, ()))

    profile: dict[str, str] = {}
    if token:
        profile["access"] = token
    if base_url:
        profile["base_url"] = base_url.rstrip("/")
    return profile


def _decrypt_profile(profile: dict) -> dict:
    decrypted = dict(profile)
    for field in _AUTH_SECRET_FIELDS:
        value = decrypted.get(field)
        if isinstance(value, str) and value:
            decrypted[field] = decrypt_secret(value)
    return decrypted


def _encrypt_profile(profile: dict) -> dict:
    encrypted = dict(profile)
    encrypted.pop(_OPAQUE_MARKER, None)
    for field in _AUTH_SECRET_FIELDS:
        value = encrypted.get(field)
        if isinstance(value, str) and value.strip():
            # encrypt_secret is a no-op on already-encrypted values, so opaque
            # profiles (which still carry the original ciphertext in each
            # secret field) round-trip to disk unchanged.
            encrypted[field] = encrypt_secret(value)
    return encrypted


def _decrypt_auth_store(store: dict) -> dict:
    sanitized = _sanitize_auth_store(store)
    profiles = sanitized.get("profiles", {})
    if not isinstance(profiles, dict):
        sanitized["profiles"] = {}
        return sanitized
    decrypted_profiles: dict = {}
    for key, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        try:
            decrypted_profiles[key] = _decrypt_profile(profile)
        except Exception as exc:
            fingerprint = f"{key}:" + "|".join(
                str(profile.get(f, "")) for f in sorted(_AUTH_SECRET_FIELDS)
            )
            if fingerprint not in _WARNED_OPAQUE_FINGERPRINTS:
                log.warning(
                    "Auth profile %s could not be decrypted (%s); marking opaque "
                    "and preserving ciphertext so it is not overwritten.",
                    key,
                    exc,
                )
                _WARNED_OPAQUE_FINGERPRINTS.add(fingerprint)
            opaque = dict(profile)
            opaque[_OPAQUE_MARKER] = True
            decrypted_profiles[key] = opaque
    sanitized["profiles"] = decrypted_profiles
    return sanitized


def _encrypt_auth_store(store: dict) -> dict:
    sanitized = _sanitize_auth_store(store)
    profiles = sanitized.get("profiles", {})
    if not isinstance(profiles, dict):
        sanitized["profiles"] = {}
        return sanitized
    sanitized["profiles"] = {
        key: _encrypt_profile(profile)
        for key, profile in profiles.items()
        if isinstance(profile, dict)
    }
    return sanitized


def is_profile_opaque(profile: dict | None) -> bool:
    """Return True if this profile is a placeholder for undecryptable data."""
    return bool(isinstance(profile, dict) and profile.get(_OPAQUE_MARKER))


def load_auth() -> dict:
    """Load auth.json, self-healing from a backup if it is missing or corrupt.

    auth.json can be lost or corrupted (an interrupted/half-written save, an
    external cleanup, a swapped encryption key). Previously that meant the app
    silently ran with NO credentials until an operator noticed and re-logged-in —
    a recurring "it dropped our auth" failure. We already keep rotating backups, so
    on a missing/undecryptable file we now restore from the newest usable backup.
    A valid-but-empty store (e.g. after an intentional logout) decrypts fine and is
    returned as-is — recovery only fires on missing/corrupt, so a logout is honoured.
    """
    if AUTH_FILE.exists():
        try:
            return _decrypt_auth_store(json.loads(AUTH_FILE.read_text()))
        except Exception as exc:
            log.warning("Failed to load auth store from %s: %s", AUTH_FILE, exc)
            recovered = _recover_auth_from_backups()
            return recovered if recovered is not None else {"version": 1, "profiles": {}}
    recovered = _recover_auth_from_backups()
    return recovered if recovered is not None else {"version": 1, "profiles": {}}


def _bak_path(n: int):
    return AUTH_FILE.with_name(AUTH_FILE.name + f".bak.{n}")


def _rotate_auth_backups():
    """Rotate auth.json.bak.1 → .bak.2 → .bak.{N} and snapshot current file.

    Best-effort: a backup failure must never block a save.
    """
    try:
        if not AUTH_FILE.exists():
            return
        oldest = _bak_path(_AUTH_BACKUP_COUNT)
        if oldest.exists():
            try:
                oldest.unlink()
            except OSError:
                pass
        for i in range(_AUTH_BACKUP_COUNT - 1, 0, -1):
            src = _bak_path(i)
            dst = _bak_path(i + 1)
            if src.exists():
                src.replace(dst)
        shutil.copy2(AUTH_FILE, _bak_path(1))
    except Exception as exc:
        log.warning("Could not rotate auth.json backups: %s", exc)


def _store_has_usable_profile(store: dict | None) -> bool:
    """True if the store holds at least one decryptable (non-opaque) profile."""
    if not isinstance(store, dict):
        return False
    profiles = store.get("profiles") or {}
    return any(
        isinstance(v, dict) and not is_profile_opaque(v)
        for v in profiles.values()
    )


def _recover_auth_from_backups() -> dict | None:
    """Restore auth.json from the newest backup that decrypts to usable creds.

    Returns the recovered store (and rewrites auth.json from the backup) or None if
    no backup is usable. Best-effort: a copy failure still returns the decrypted
    store so the running process keeps working even if the rewrite fails.
    """
    for i in range(1, _AUTH_BACKUP_COUNT + 1):
        bak = _bak_path(i)
        if not bak.exists():
            continue
        try:
            store = _decrypt_auth_store(json.loads(bak.read_text()))
        except Exception:
            continue
        if not _store_has_usable_profile(store):
            continue
        try:
            ensure_dirs()
            shutil.copy2(bak, AUTH_FILE)
            log.warning(
                "auth.json was missing/corrupt — self-healed from %s", bak.name
            )
        except Exception as exc:
            log.warning("auth recovery: could not rewrite auth.json from %s: %s", bak.name, exc)
        return store
    return None


def _save_auth_unlocked(store: dict):
    """Write auth.json atomically (caller must hold lock)."""
    ensure_dirs()
    _rotate_auth_backups()
    store = _encrypt_auth_store(store)
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2) + "\n")
    tmp.replace(AUTH_FILE)


def save_auth(store: dict):
    """Atomically write auth.json with file locking."""
    with _lock():
        _save_auth_unlocked(store)


def upsert_profile(provider: str, credential: dict):
    """Write or update a credential profile."""
    with _lock():
        store = load_auth()
        profile_id = f"{provider}:default"
        store["profiles"][profile_id] = credential
        _save_auth_unlocked(store)


def delete_profile(provider: str) -> bool:
    """Delete a credential profile and return whether anything was removed."""
    with _lock():
        store = load_auth()
        profile_id = f"{provider}:default"
        removed = bool(store["profiles"].pop(profile_id, None))
        if removed:
            _save_auth_unlocked(store)
        return removed


def get_profile(provider: str) -> dict | None:
    """Get a provider's credential profile.

    Opaque profiles (those whose ciphertext could not be decrypted with the
    current key) are hidden from consumers — callers must not see ciphertext
    as a plaintext token. An env override still wins, since it represents an
    explicit runtime credential independent of the stored ciphertext.
    """
    store = load_auth()
    stored = store["profiles"].get(f"{provider}:default")
    env_override = _env_profile(provider)
    if is_profile_opaque(stored):
        if env_override:
            env_override["provider"] = str(provider or "").strip().lower()
            return env_override
        return None
    if stored and env_override:
        merged = dict(stored)
        merged.update(env_override)
        return merged
    if stored:
        return stored
    if env_override:
        env_override["provider"] = str(provider or "").strip().lower()
        return env_override
    return None


def _is_expired(profile: dict) -> bool:
    """Check if a token is expired (with buffer)."""
    expires = profile.get("expires")
    if not expires:
        return False  # No expiry info — assume valid
    return time.time() * 1000 >= expires - REFRESH_BUFFER_MS


def _refresh_openai(profile: dict) -> dict:
    """Refresh OpenAI OAuth token."""
    from forven.auth.openai import CLIENT_ID, TOKEN_URL

    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": profile["refresh"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    profile["access"] = data["access_token"]
    if data.get("refresh_token"):
        profile["refresh"] = data["refresh_token"]
    if data.get("expires_in"):
        profile["expires"] = int(time.time() * 1000 + data["expires_in"] * 1000)
    # Extract account ID from JWT (H-S5: validated via safe helper)
    from forven.auth import safe_extract_chatgpt_account_id
    account_id = safe_extract_chatgpt_account_id(data["access_token"])
    if account_id:
        profile["accountId"] = account_id
    return profile


def _refresh_minimax(profile: dict) -> dict:
    """Refresh MiniMax OAuth token."""
    from forven.auth.minimax import CLIENT_ID, TOKEN_URL

    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": profile["refresh"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    profile["access"] = data["access_token"]
    if data.get("refresh_token"):
        profile["refresh"] = data["refresh_token"]
    if data.get("expired_in"):  # MiniMax uses expired_in (unix ms)
        profile["expires"] = data["expired_in"]
    elif data.get("expires_in"):
        profile["expires"] = int(time.time() * 1000 + data["expires_in"] * 1000)
    return profile


REFRESHERS = {
    "openai": _refresh_openai,
    "minimax": _refresh_minimax,
}


def get_token(provider: str) -> str:
    """Return a valid access token, auto-refreshing if expired."""
    profile = get_profile(provider)
    if not profile:
        raise ValueError(f"No auth profile for {provider}. Run: forven auth login {provider}")

    if provider == "lmstudio":
        # LM Studio is a local OpenAI-compatible endpoint and may not require auth.
        return str(
            profile.get("access")
            or profile.get("token")
            or profile.get("api_key")
            or ""
        ).strip()

    if _is_expired(profile):
        refresher = REFRESHERS.get(provider)
        if refresher and profile.get("refresh"):
            try:
                profile = refresher(profile)
                profile.pop("last_refresh_error", None)
                profile.pop("last_refresh_at", None)
                upsert_profile(provider, profile)
                console.print(f"[green]Refreshed {provider} token[/green]")
            except Exception as e:
                profile["last_refresh_error"] = str(e)[:500]
                profile["last_refresh_at"] = int(time.time() * 1000)
                try:
                    upsert_profile(provider, profile)
                except Exception:
                    pass
                raise RuntimeError(f"Failed to refresh {provider} token: {e}") from e
        else:
            raise RuntimeError(f"{provider} token expired and no refresh available. Run: forven auth login {provider}")

    # Return the access token (field name varies by migration source)
    return profile.get("access") or profile.get("token", "")


class CredentialError(ValueError):
    """Raised when a provider's credentials are not usable (missing/opaque/expired).

    Subclasses ``ValueError`` so existing ``except ValueError``/``except Exception``
    handlers keep working, while callers that care (the brain-task failure path)
    can catch it specifically to pause a routine and surface a clear alert.
    """

    def __init__(self, provider: str, status: str = "missing"):
        self.provider = str(provider or "").strip().lower() or "the configured provider"
        self.status = status
        if status == "opaque":
            msg = (
                f"{self.provider} credentials exist but could not be decrypted "
                f"(encryption-key mismatch). Restore the original key or re-add the "
                f"credential in Settings > Agents > AI providers."
            )
        elif status == "expired":
            msg = (
                f"{self.provider} token is expired and could not be refreshed. "
                f"Re-authenticate in Settings > Agents > AI providers "
                f"(or run: forven auth login {self.provider})."
            )
        else:  # missing / unknown
            msg = (
                f"{self.provider} has no API credentials configured. Add them in "
                f"Settings > Agents > AI providers (or run: forven auth login {self.provider})."
            )
        super().__init__(msg)


def credential_status(provider: str) -> str:
    """Classify a provider's stored credential: ``'ok' | 'missing' | 'opaque' | 'expired'``.

    Lets callers tell *why* a provider is unusable so the fix is actionable:
    ``missing`` -> add/login; ``opaque`` -> ciphertext can't be decrypted with the
    current key (restore the key or re-add); ``expired`` -> token expired and refresh
    failed (re-authenticate).
    """
    prov = str(provider or "").strip().lower()
    if not prov:
        return "missing"
    # An explicit env-var credential always counts as usable.
    if _env_profile(prov):
        return "ok"
    store = load_auth()
    stored = store.get("profiles", {}).get(f"{prov}:default")
    if stored is None:
        return "missing"
    if is_profile_opaque(stored):
        return "opaque"
    try:
        tok = get_token(prov)
    except Exception:
        return "expired"
    # A present, decryptable profile that yields no token is effectively unconfigured
    # (corrupt/partial write). lmstudio legitimately needs no token.
    if prov == "lmstudio" or tok:
        return "ok"
    return "missing"


def get_status_rows() -> list[tuple[str, str, str]]:
    """Get auth status as table rows: (provider, status, expires)."""
    store = load_auth()
    rows = []
    for provider in ["openai", "minimax", "lmstudio"]:
        profile = store["profiles"].get(f"{provider}:default")
        if not profile:
            rows.append((provider, "[red]Not configured[/red]", "-"))
            continue

        if provider == "lmstudio":
            base_url = str(profile.get("base_url") or "").strip()
            if base_url:
                rows.append((provider, "[green]Active[/green]", base_url))
            else:
                rows.append((provider, "[red]Not configured[/red]", "-"))
            continue

        expires = profile.get("expires")
        if not expires:
            rows.append((provider, "[green]Active[/green]", "No expiry"))
            continue

        now_ms = time.time() * 1000
        if now_ms >= expires:
            rows.append((provider, "[red]Expired[/red]", _format_ts(expires)))
        elif now_ms >= expires - REFRESH_BUFFER_MS:
            rows.append((provider, "[yellow]Expiring soon[/yellow]", _format_ts(expires)))
        else:
            remaining = expires - now_ms
            hours = int(remaining / 3600000)
            days = hours // 24
            if days > 0:
                exp_str = f"{days}d {hours % 24}h remaining"
            else:
                exp_str = f"{hours}h remaining"
            rows.append((provider, "[green]Active[/green]", exp_str))

    return rows


def _format_ts(ms: int | float) -> str:
    """Format a millisecond timestamp."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def display_status():
    """Print auth status table to console."""
    table = Table(title="Forven Auth Status")
    table.add_column("Provider", style="bold")
    table.add_column("Status")
    table.add_column("Expires")
    for row in get_status_rows():
        table.add_row(*row)
    console.print(table)


def migrate_from_openclaw():
    """Import tokens from OpenClaw's auth-profiles.json."""
    if not OPENCLAW_AUTH.exists():
        console.print("[red]OpenClaw auth not found at:[/red]", str(OPENCLAW_AUTH))
        return

    oc = json.loads(OPENCLAW_AUTH.read_text())
    oc_profiles = oc.get("profiles", {})
    store = load_auth()
    migrated = 0

    # Map OpenClaw provider names to Forven names
    provider_map = {
        "minimax-portal": "minimax",
        "openai-codex": "openai",
    }

    for oc_key, oc_profile in oc_profiles.items():
        oc_provider = oc_profile.get("provider", oc_key.split(":")[0])
        forven_provider = provider_map.get(oc_provider)
        if not forven_provider:
            console.print(f"  [dim]Skipping unknown provider: {oc_provider}[/dim]")
            continue

        forven_key = f"{forven_provider}:default"

        # Build normalized profile
        profile = {"type": oc_profile.get("type", "oauth"), "provider": forven_provider}

        if oc_profile.get("access"):
            profile["access"] = oc_profile["access"]
        elif oc_profile.get("token"):
            profile["access"] = oc_profile["token"]

        if oc_profile.get("refresh"):
            profile["refresh"] = oc_profile["refresh"]
        if oc_profile.get("expires"):
            profile["expires"] = oc_profile["expires"]
        if oc_profile.get("accountId"):
            profile["accountId"] = oc_profile["accountId"]

        store["profiles"][forven_key] = profile
        migrated += 1
        console.print(f"  [green]Migrated {forven_provider}[/green]")

    save_auth(store)
    console.print(f"\n[bold green]Migrated {migrated} provider(s) from OpenClaw[/bold green]")


def run_login(provider: str):
    """Run the OAuth flow for a provider."""
    flows = {
        "openai": "forven.auth.openai",
        "minimax": "forven.auth.minimax",
    }
    import importlib
    mod = importlib.import_module(flows[provider])
    profile = mod.login()
    upsert_profile(provider, profile)
    console.print(f"[bold green]Authenticated with {provider}[/bold green]")


def force_refresh(provider: str):
    """Force a token refresh for a provider."""
    profile = get_profile(provider)
    if not profile:
        console.print(f"[red]No auth profile for {provider}[/red]")
        return

    refresher = REFRESHERS.get(provider)
    if not refresher:
        console.print(f"[red]No refresh flow for {provider}[/red]")
        return

    if not profile.get("refresh"):
        console.print(f"[red]No refresh token for {provider}[/red]")
        return

    profile = refresher(profile)
    upsert_profile(provider, profile)
    console.print(f"[bold green]Refreshed {provider} token[/bold green]")


def interactive_configure():
    """Interactive setup — select providers and run OAuth flows."""
    from rich.prompt import Prompt

    console.print("\n[bold]Forven Configuration[/bold]\n")

    # Check for existing OpenClaw tokens
    if OPENCLAW_AUTH.exists():
        store = load_auth()
        if not store["profiles"]:
            console.print("[dim]Found OpenClaw tokens. Migrating...[/dim]")
            migrate_from_openclaw()
            console.print()

    while True:
        console.print("Available providers:")
        console.print("  1. OpenAI (GPT/Codex)")
        console.print("  2. MiniMax")
        console.print("  3. Show status")
        console.print("  4. Done")

        choice = Prompt.ask("\nSelect", choices=["1", "2", "3", "4"], default="4")

        if choice == "1":
            run_login("openai")
        elif choice == "2":
            run_login("minimax")
        elif choice == "3":
            display_status()
        elif choice == "4":
            break

        console.print()

    display_status()
    console.print("\n[bold green]Configuration complete.[/bold green]")
