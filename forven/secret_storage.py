from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from filelock import FileLock

from forven import config as cfg


log = logging.getLogger("forven.secret_storage")
_ENCRYPTION_PREFIX = "fernet:"
_KEY_FILE_NAME = ".forven_key"


def _restrict_to_owner(path: Path) -> None:
    """Restrict a secret file to the current user only (audit 2026-06-22, L1).

    POSIX: chmod 0600. Windows: ``os.chmod`` only toggles the read-only bit and
    does NOT restrict ACLs, so on a multi-user/shared box the Fernet master key
    could be world-readable. Use icacls to drop inherited ACEs and grant the
    current user full control. Best-effort — never raises (a failure leaves the
    default ACL, which on a stock single-user box is already owner/SYSTEM/Admins).
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if not sys.platform.startswith("win"):
        return
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if not user:
        return
    try:
        import subprocess

        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort hardening
        log.debug("icacls hardening skipped for %s: %s", path, exc)

_CACHED_FERNET: Fernet | None = None
_CACHED_KEY_BYTES: bytes | None = None


def _preferred_key_path() -> Path:
    """Return the preferred key-file path — outside any cloud-sync folder.

    Windows: %LOCALAPPDATA%\\Forven\\.forven_key (not synced by OneDrive/Drive).
    Others:  $XDG_CONFIG_HOME/forven/.forven_key, else ~/.config/forven/.forven_key.
    """
    if sys.platform.startswith("win"):
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "Forven" / _KEY_FILE_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "forven" / _KEY_FILE_NAME


def _legacy_key_path() -> Path:
    return Path(cfg.FORVEN_HOME) / _KEY_FILE_NAME


def _key_path() -> Path:
    """Resolve the live key path.

    Prefers the non-synced path; falls back to the legacy in-home path when
    that's the only one present, so existing installs keep working until the
    first successful migration.
    """
    preferred = _preferred_key_path()
    if preferred.exists():
        return preferred
    legacy = _legacy_key_path()
    if legacy.exists():
        return legacy
    return preferred


def _migrate_legacy_key_if_needed() -> None:
    """Copy an existing legacy key to the preferred path once.

    Leaves the legacy file in place; subsequent loads read the preferred copy.
    Best-effort — any failure just falls through to the legacy path at read time.
    """
    preferred = _preferred_key_path()
    if preferred.exists():
        return
    legacy = _legacy_key_path()
    if not legacy.exists():
        return
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        preferred.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
        _restrict_to_owner(preferred)
        log.info("Migrated encryption key from %s to %s", legacy, preferred)
    except Exception as exc:
        log.warning("Could not migrate encryption key to %s: %s", preferred, exc)


def _has_existing_encrypted_data() -> bool:
    """True if an auth.json with any fernet:-prefixed value exists.

    Used as a guard against silently generating a fresh key that would orphan
    the ciphertext. Best-effort: any read/parse failure returns False so we
    don't wedge startup on a malformed store.
    """
    try:
        auth_file = Path(cfg.AUTH_FILE)
        if not auth_file.exists():
            return False
        return _ENCRYPTION_PREFIX in auth_file.read_text(encoding="utf-8")
    except Exception:
        return False


def _load_fernet_key() -> bytes:
    """Resolve the Fernet key for this process.

    Priority:
      1. $FORVEN_ENCRYPTION_KEY (explicit override).
      2. Preferred key file, else legacy key file.
      3. In-process cached key (when the file transiently disappears — this
         prevents regenerating a fresh key that would orphan existing ciphertext).
      4. Newly generated key, written to the preferred path under a file lock.
    """
    configured = str(os.environ.get("FORVEN_ENCRYPTION_KEY") or "").strip()
    if configured:
        return configured.encode("utf-8")

    cfg.ensure_dirs()
    _migrate_legacy_key_if_needed()

    live_path = _key_path()
    if live_path.exists():
        return live_path.read_text(encoding="utf-8").strip().encode("utf-8")

    # File is missing. If we already loaded a key this process, reuse it and
    # try to restore the file — NEVER silently generate a fresh key that would
    # orphan existing encrypted data.
    if _CACHED_KEY_BYTES is not None:
        log.warning(
            "Encryption key file missing at %s; reusing cached in-process key.",
            live_path,
        )
        try:
            live_path.parent.mkdir(parents=True, exist_ok=True)
            live_path.write_text(_CACHED_KEY_BYTES.decode("utf-8") + "\n", encoding="utf-8")
            _restrict_to_owner(live_path)
        except Exception as exc:
            log.warning("Could not restore key file at %s: %s", live_path, exc)
        return _CACHED_KEY_BYTES

    # First-ever key for this install. Generate under a file lock so concurrent
    # process starts don't each write their own.
    preferred = _preferred_key_path()
    preferred.parent.mkdir(parents=True, exist_ok=True)
    lock_path = preferred.with_suffix(preferred.suffix + ".lock")
    with FileLock(str(lock_path), timeout=10):
        # Re-check under lock — another process may have written it already.
        if preferred.exists():
            return preferred.read_text(encoding="utf-8").strip().encode("utf-8")
        # Refuse to generate a fresh key when ciphertext already exists at
        # cfg.AUTH_FILE — a fresh key would orphan every configured provider.
        # This has happened before: a test with a tmp FORVEN_HOME ran without
        # patching _preferred_key_path, so it wrote a fresh key to the real
        # %LOCALAPPDATA%/Forven/ location while the live auth.json stayed
        # encrypted with the legacy key.
        if _has_existing_encrypted_data():
            raise RuntimeError(
                f"Refusing to generate a fresh Fernet key at {preferred}: "
                f"encrypted data already exists but no matching key was found. "
                f"Restore the original key to {preferred} or set "
                f"FORVEN_ENCRYPTION_KEY before continuing."
            )
        generated = Fernet.generate_key()
        preferred.write_text(generated.decode("utf-8") + "\n", encoding="utf-8")
        _restrict_to_owner(preferred)
        log.info("Generated local encryption key at %s", preferred)
        return generated


def _get_fernet() -> Fernet:
    global _CACHED_FERNET, _CACHED_KEY_BYTES
    key = _load_fernet_key()
    if _CACHED_FERNET is not None and _CACHED_KEY_BYTES == key:
        return _CACHED_FERNET
    _CACHED_KEY_BYTES = key
    _CACHED_FERNET = Fernet(key)
    return _CACHED_FERNET


def _reset_cache_for_tests() -> None:
    """Clear the in-process key cache. Intended for tests only."""
    global _CACHED_FERNET, _CACHED_KEY_BYTES
    _CACHED_FERNET = None
    _CACHED_KEY_BYTES = None


def is_encrypted_secret(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_ENCRYPTION_PREFIX)


def encrypt_secret(value: str) -> str:
    secret = str(value or "")
    if not secret:
        return ""
    if is_encrypted_secret(secret):
        return secret
    token = _get_fernet().encrypt(secret.encode("utf-8")).decode("utf-8")
    return f"{_ENCRYPTION_PREFIX}{token}"


def decrypt_secret(value: str) -> str:
    secret = str(value or "")
    if not secret or not is_encrypted_secret(secret):
        return secret
    token = secret[len(_ENCRYPTION_PREFIX):].encode("utf-8")
    return _get_fernet().decrypt(token).decode("utf-8")
