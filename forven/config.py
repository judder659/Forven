"""Configuration paths and loading for Forven."""

import json
import os
import shutil
from pathlib import Path

# Historical home directory names we still honor for migration. Order matters:
# earlier entries are merged first, so later-found files do not overwrite.
# `.juddex` (double 'd') was the pre-rename branding; `.judex` is a shorter
# variant used briefly during the rename.
_LEGACY_HOME_NAMES = (".juddex", ".judex")

# DB files were renamed during the Juddex -> Forven rename. When merging from a
# legacy home we map the old filename to its canonical name so the migrated
# data is actually picked up by the app.
_LEGACY_FILE_RENAMES = {
    "juddex.db": "forven.db",
    "juddex.db-journal": "forven.db-journal",
    "juddex.db-wal": "forven.db-wal",
    "juddex.db-shm": "forven.db-shm",
    "juddex_lab.db": "forven_lab.db",
    "juddex_lab.db-journal": "forven_lab.db-journal",
    "juddex_lab.db-wal": "forven_lab.db-wal",
    "juddex_lab.db-shm": "forven_lab.db-shm",
}


def _legacy_homes() -> list[Path]:
    home = Path.home()
    return [home / name for name in _LEGACY_HOME_NAMES]


# Core paths
def _resolve_forven_home() -> Path:
    """Resolve the canonical Forven home path."""
    env_home = os.environ.get("FORVEN_HOME")
    default_home = Path.home() / ".forven"
    legacy_homes = _legacy_homes()

    def _merge_all_legacy(canonical: Path) -> None:
        for legacy_home in legacy_homes:
            if not legacy_home.exists():
                continue
            if not canonical.exists():
                _migrate_legacy_home_if_needed(legacy_home, canonical)
            else:
                _merge_legacy_tree(legacy_home, canonical)

    if env_home:
        requested_home = Path(env_home).expanduser()
        if requested_home in legacy_homes:
            # Keep legacy writes/reads backward-compatible, but migrate into
            # the canonical `.forven` home so the app settles on one canonical home.
            _merge_all_legacy(default_home)
            return default_home
        return requested_home

    # Always keep the canonical home as ~/.forven, but merge legacy home
    # contents on first run so older state/data is preserved.
    _merge_all_legacy(default_home)

    return default_home


def _migrate_legacy_home_if_needed(legacy_home: Path, canonical_home: Path):
    """Migrate old state from a legacy home into `~/.forven` when missing."""
    try:
        if not legacy_home.exists():
            return
        canonical_home = Path(canonical_home)
        # Always go through the merge path so filename renames (e.g.
        # juddex.db -> forven.db) are applied even on a fresh canonical home.
        canonical_home.mkdir(parents=True, exist_ok=True)
        _merge_legacy_tree(legacy_home, canonical_home)

        # If we found legacy state, keep a tiny marker so support tooling can
        # tell that migration was applied. No behavior currently depends on this
        # file, but it helps for troubleshooting.
        marker = canonical_home / ".merged_legacy_marker"
        marker.write_text(f"merged-from-legacy: {legacy_home.name}\n")
    except Exception:
        # Migration is best-effort; never fail process startup because of this.
        pass


def _merge_legacy_tree(source_root: Path, destination_root: Path):
    """Merge source directory into destination, preferring existing destination files.

    Applies `_LEGACY_FILE_RENAMES` at the top level so pre-rename DB files land
    under their canonical Forven names.
    """
    if not source_root.exists():
        return

    destination_root.mkdir(parents=True, exist_ok=True)

    for source_path in sorted(source_root.iterdir()):
        dest_name = _LEGACY_FILE_RENAMES.get(source_path.name, source_path.name)
        destination_path = destination_root / dest_name
        if source_path.is_dir():
            if not destination_path.exists():
                shutil.copytree(source_path, destination_path)
            else:
                _merge_legacy_tree(source_path, destination_path)
            continue

        if not destination_path.exists():
            try:
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination_path)
            except Exception:
                pass


FORVEN_HOME = _resolve_forven_home()
# Primary legacy home kept for backward compatibility with callers that
# reference the `LEGACY_FORVEN_HOME` constant. Use `_legacy_homes()` when all
# historical names must be considered.
LEGACY_FORVEN_HOME = Path.home() / ".juddex"
FORVEN_DB = FORVEN_HOME / "forven.db"
FORVEN_LAB_DB = FORVEN_HOME / "forven_lab.db"
AUTH_FILE = FORVEN_HOME / "auth.json"
CONFIG_FILE = FORVEN_HOME / "config.json"
WORKSPACE_DIR = FORVEN_HOME / "workspace"
LEGACY_WORKSPACE_DIR = LEGACY_FORVEN_HOME / "workspace"

# OpenClaw paths (for migration)
OPENCLAW_HOME = Path.home() / ".openclaw"
OPENCLAW_AUTH = OPENCLAW_HOME / "agents" / "main" / "agent" / "auth-profiles.json"
OPENCLAW_WORKSPACE = OPENCLAW_HOME / "workspace"


def ensure_dirs():
    """Create all required directories."""
    FORVEN_HOME.mkdir(parents=True, exist_ok=True)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    (WORKSPACE_DIR / "memory").mkdir(exist_ok=True)
    (WORKSPACE_DIR / "agents").mkdir(exist_ok=True)


def load_config() -> dict:
    """Load config.json, returning empty dict if missing."""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict):
    """Atomically write config.json."""
    ensure_dirs()
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(CONFIG_FILE)


def _parse_bool(value) -> bool:
    """Parse truthy/falsy values for config toggles."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    if isinstance(value, int):
        return bool(value)
    return False


def is_beta_build() -> bool:
    """True when the app is running inside the packaged (Tauri) beta build.

    The Rust launcher injects FORVEN_ENV=beta when it spawns python. Dev runs
    (`python -m forven.api` from a checkout) leave this unset. Used to
    hard-lock paper trading so a beta tester cannot accidentally — or be
    tricked by prompt injection — flip to live execution.
    """
    return os.environ.get("FORVEN_ENV", "").strip().lower() == "beta"


def get_execution_mode() -> str:
    """Get execution mode: 'paper' or 'live'.

    Paper mode records trades in SQLite only.
    Live mode also sends orders to HyperLiquid via the execution-trader agent.

    In beta builds this function ALWAYS returns 'paper' regardless of config
    or env, to keep testers off live trading no matter what got written to
    settings. The lock is here (at the read site) as well as at the write
    site so a stale 'live' value in config.json can never take effect.
    """
    if is_beta_build():
        return "paper"
    mode = os.environ.get("FORVEN_EXECUTION_MODE")
    if mode and mode in ("paper", "live"):
        return mode
    cfg = load_config()
    return cfg.get("execution_mode", "paper")


def set_execution_mode(mode: str):
    """Set execution mode. Only 'paper' is a supported value.

    Live/mainnet trading is NOT a supported feature of this open-source build:
    this refuses anything but 'paper' unconditionally, so the ops endpoint
    (/api/ops/execution-mode) and any agent tool that tries to flip the switch
    get a loud error instead of silently going live. Forven ships with paper
    trading + Hyperliquid testnet only.

    A user who deliberately forces 'live' out-of-band (FORVEN_EXECUTION_MODE env
    or hand-editing config.json) is accepting their own risk — that's why the
    read path (get_execution_mode) still honours such an override and the
    fail-closed Rule 0c margin guard in exchange/risk.py still applies to it.
    """
    if mode != "paper":
        raise ValueError(
            f"Unsupported execution mode: {mode!r}. This build supports paper "
            f"trading and Hyperliquid testnet only; live/mainnet trading is not "
            f"a supported feature."
        )
    cfg = load_config()
    cfg["execution_mode"] = mode
    save_config(cfg)


def propr_enabled() -> bool:
    """Hidden Propr.xyz prop-firm integration switch (PROPR-1).

    Deliberately NOT in the settings manifest or the Settings UI — like the
    FORVEN_ALLOW_MAINNET guard, an operator must already know this exists to
    turn it on: set FORVEN_PROPR_ENABLED=1 in the environment, or hand-write
    "propr_enabled": true into config.json. The flag controls VISIBILITY only
    (the Propr nav page + /api/propr routes); OPENING a Propr position
    additionally requires FORVEN_ALLOW_PROPR_LIVE=1 (see forven.exchange.propr —
    Propr has no testnet, every entry is real challenge-account money). Closes,
    protective legs and cancels need only this flag: refusing an exit strands a
    live position rather than protecting it (PROPR-PERM-2).

    Beta builds are unconditionally OFF, same reasoning as the paper lock in
    get_execution_mode: a packaged-build tester must not be able to reach a
    real-money venue no matter what got written to config.
    """
    if is_beta_build():
        return False
    env = os.environ.get("FORVEN_PROPR_ENABLED")
    if env is not None and str(env).strip():
        return _parse_bool(env)
    try:
        return _parse_bool(load_config().get("propr_enabled"))
    except Exception:
        return False


def _parse_float(value, default: float) -> float:
    """Parse float-like values with fallback."""
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return float(default)
    try:
        cleaned = str(value).strip()
        if not cleaned:
            return float(default)
        return float(cleaned)
    except Exception:
        return float(default)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _settings_blob_value(key: str):
    """Read `key` from the unified settings KV blob (``forven:settings``), or None.

    This is the store the Settings UI *and* the paper service write to. The regime
    getters consult it (after any env override) so a change made through either
    path reaches the live gate without a separate config.json mirror. Falls back
    to None on any failure so callers can use their config.json default.
    """
    try:
        from forven.db import kv_get
        blob = kv_get("forven:settings", {})
        if isinstance(blob, dict) and key in blob:
            return blob.get(key)
    except Exception:
        pass
    return None


def get_strict_regime_gating() -> bool:
    """Whether incompatible/low-confidence regimes should block execution."""
    env_val = os.environ.get("FORVEN_STRICT_REGIME_GATING")
    if env_val is not None:
        return _parse_bool(env_val)

    blob_val = _settings_blob_value("strict_regime_gating")
    if blob_val is not None:
        return _parse_bool(blob_val)

    cfg = load_config()
    return _parse_bool(cfg.get("strict_regime_gating", True))


def get_backup_ai_provider() -> str:
    """Backup AI provider to fall back to when the primary provider's credentials
    are unusable. ``'none'`` (default) disables fallback. Wired setting: env override,
    then the Settings KV blob, then config.json. Always lower-cased."""
    env_val = os.environ.get("FORVEN_BACKUP_AI_PROVIDER")
    if env_val is not None:
        return str(env_val).strip().lower()

    blob_val = _settings_blob_value("backup_ai_provider")
    if blob_val is not None:
        return str(blob_val).strip().lower()

    cfg = load_config()
    return str(cfg.get("backup_ai_provider", "none")).strip().lower()


def get_backup_ai_model() -> str:
    """Model id for the backup AI provider. Empty = use the provider's default."""
    env_val = os.environ.get("FORVEN_BACKUP_AI_MODEL")
    if env_val is not None:
        return str(env_val).strip()

    blob_val = _settings_blob_value("backup_ai_model")
    if blob_val is not None:
        return str(blob_val).strip()

    cfg = load_config()
    return str(cfg.get("backup_ai_model", "")).strip()


def get_regime_min_confidence() -> float:
    """Minimum confidence required when strict regime gating is enabled."""
    env_val = os.environ.get("FORVEN_REGIME_MIN_CONFIDENCE")
    if env_val is not None:
        return _clamp(_parse_float(env_val, 0.3), 0.0, 1.0)

    blob_val = _settings_blob_value("regime_min_confidence")
    if blob_val is not None:
        return _clamp(_parse_float(blob_val, 0.3), 0.0, 1.0)

    cfg = load_config()
    return _clamp(_parse_float(cfg.get("regime_min_confidence", 0.3), 0.3), 0.0, 1.0)


_REGIME_GATE_MODES = ("off", "observe", "enforce")


def get_regime_gate_mode() -> str:
    """Direction×regime entry gate mode: 'off' | 'observe' | 'enforce'.

    'observe' (the shipping default) shadow-logs would-have-blocked entries to
    the regime_gate_events ledger without touching execution; 'enforce' vetoes
    them; 'off' disables blocking AND logging. Evidence base: the 2026-07-05
    graveyard audit — longs opened while the causal classifier read TREND_DOWN
    or HIGH_VOL were the dominant loss engine, robust to dedup/equal-weighting
    and to strictly-prior-bar labels.
    """
    for raw in (
        os.environ.get("FORVEN_REGIME_GATE_MODE"),
        _settings_blob_value("regime_gate_mode"),
        load_config().get("regime_gate_mode"),
    ):
        if raw is not None:
            mode = str(raw).strip().lower()
            if mode in _REGIME_GATE_MODES:
                return mode
            return "observe"
    return "observe"


def _parse_regime_csv(raw: object) -> set[str] | None:
    from forven.regime import normalize_regime_label

    if raw is None:
        return None
    if isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        parts = str(raw).split(",")
    resolved = {normalize_regime_label(p) for p in parts}
    return {r for r in resolved if r}


def get_regime_gate_block_long() -> set[str]:
    """Regimes in which the gate vetoes LONG entries. CSV of regime labels."""
    for raw in (
        os.environ.get("FORVEN_REGIME_GATE_BLOCK_LONG"),
        _settings_blob_value("regime_gate_block_long"),
    ):
        parsed = _parse_regime_csv(raw)
        if parsed is not None:
            return parsed
    parsed = _parse_regime_csv(load_config().get("regime_gate_block_long"))
    if parsed is not None:
        return parsed
    return {"TREND_DOWN", "HIGH_VOL"}


def get_regime_gate_block_short() -> set[str]:
    """Regimes in which the gate vetoes SHORT entries. Default: none —
    the audit found shorts net-positive in every regime bucket."""
    for raw in (
        os.environ.get("FORVEN_REGIME_GATE_BLOCK_SHORT"),
        _settings_blob_value("regime_gate_block_short"),
    ):
        parsed = _parse_regime_csv(raw)
        if parsed is not None:
            return parsed
    parsed = _parse_regime_csv(load_config().get("regime_gate_block_short"))
    if parsed is not None:
        return parsed
    return set()


def get_regime_gate_min_confidence() -> float:
    """Live-detector confidence below which a hostile regime call is NOT acted on.

    Only consulted when the cached live detector agrees on the hostile label —
    the kernel's entry-bar label (the audit's evidence base) carries no
    confidence and gates on its own. 0.0 makes the gate unconditional.
    """
    env_val = os.environ.get("FORVEN_REGIME_GATE_MIN_CONFIDENCE")
    if env_val is not None:
        return _clamp(_parse_float(env_val, 0.6), 0.0, 1.0)

    blob_val = _settings_blob_value("regime_gate_min_confidence")
    if blob_val is not None:
        return _clamp(_parse_float(blob_val, 0.6), 0.0, 1.0)

    cfg = load_config()
    return _clamp(_parse_float(cfg.get("regime_gate_min_confidence", 0.6), 0.6), 0.0, 1.0)


def get_allow_unknown_regime_strategies() -> bool:
    """Whether unknown strategy types bypass strict regime compatibility checks."""
    env_val = os.environ.get("FORVEN_ALLOW_UNKNOWN_REGIME_STRATEGIES")
    if env_val is not None:
        return _parse_bool(env_val)

    blob_val = _settings_blob_value("allow_unknown_regime_strategies")
    if blob_val is not None:
        return _parse_bool(blob_val)

    cfg = load_config()
    return _parse_bool(cfg.get("allow_unknown_regime_strategies", False))


# ---------------------------------------------------------------------------
# Polygon.io API key
# ---------------------------------------------------------------------------

def get_polygon_api_key() -> str | None:
    """Read Polygon API key from env var or Settings API Keys store.

    Priority: POLYGON_API_KEY env var > KV store (Settings > API Keys).
    """
    env_key = os.environ.get("POLYGON_API_KEY", "").strip()
    if env_key:
        return env_key

    # Read from the Settings > API Keys KV store (same store the UI writes to)
    try:
        from forven.db import kv_get
        from forven.secret_storage import decrypt_secret
        store = kv_get("forven:settings:api-keys", {})
        if isinstance(store, dict):
            entry = store.get("polygon")
            if isinstance(entry, dict):
                value = str(entry.get("value") or "").strip()
                if value:
                    return decrypt_secret(value)
            elif isinstance(entry, str) and entry.strip():
                return decrypt_secret(entry)
    except Exception:
        pass

    # Fallback: auth.json file
    if AUTH_FILE.exists():
        try:
            auth = json.loads(AUTH_FILE.read_text())
            entry = auth.get("polygon") or auth.get("polygon_api_key")
            if isinstance(entry, dict):
                return str(entry.get("value") or entry.get("key") or "").strip() or None
            if isinstance(entry, str) and entry.strip():
                return entry.strip()
        except Exception:
            pass
    return None


def redact_api_key(key: str | None) -> str:
    """Redact an API key for safe logging — show only last 4 chars."""
    if not key:
        return "***"
    if len(key) <= 4:
        return "***"
    return f"***{key[-4:]}"


def ensure_state_dir_bootstrapped() -> None:
    """On first packaged run, seed $FORVEN_HOME/.env from $FORVEN_DEFAULT_ENV.

    No-op when the target .env already exists, when FORVEN_HOME is unset, or when
    FORVEN_DEFAULT_ENV is unset or points to a missing file. Dev runs are
    unaffected.
    """
    home_env = os.environ.get("FORVEN_HOME")
    if not home_env:
        return
    default_env = os.environ.get("FORVEN_DEFAULT_ENV")
    if not default_env:
        return
    source = Path(default_env)
    if not source.is_file():
        return
    home = Path(home_env)
    home.mkdir(parents=True, exist_ok=True)
    target = home / ".env"
    if target.exists():
        return
    shutil.copyfile(source, target)


def ensure_seed_data_bootstrapped() -> int:
    """On first packaged run, seed `$FORVEN_HOME/data/ohlcv/` from bundled
    parquets so agents, backtests, and the dashboard have something to render
    immediately instead of an empty install.

    Copies every file under `$FORVEN_DEFAULT_SEED_DATA/ohlcv/<SYMBOL>/*.parquet`
    into `$FORVEN_HOME/data/ohlcv/<SYMBOL>/` unless the target already exists
    (so a returning user's accumulated data is never overwritten). Dev runs
    (where FORVEN_HOME is unset) are a no-op — the repo-relative `data/ohlcv`
    is already populated.

    Returns the number of seed files actually copied. 0 means "nothing to do"
    (no env vars, source missing, or everything already present).
    """
    home_env = os.environ.get("FORVEN_HOME")
    if not home_env:
        return 0
    seed_root = os.environ.get("FORVEN_DEFAULT_SEED_DATA")
    if not seed_root:
        return 0
    source_root = Path(seed_root) / "ohlcv"
    if not source_root.is_dir():
        return 0

    target_root = Path(home_env) / "data" / "ohlcv"
    target_root.mkdir(parents=True, exist_ok=True)

    copied = 0
    for symbol_dir in sorted(source_root.iterdir()):
        if not symbol_dir.is_dir():
            continue
        dest_symbol = target_root / symbol_dir.name
        dest_symbol.mkdir(parents=True, exist_ok=True)
        for seed_file in sorted(symbol_dir.iterdir()):
            if not seed_file.is_file():
                continue
            dest_file = dest_symbol / seed_file.name
            if dest_file.exists():
                # Respect existing data: either the daemon already wrote fresher
                # bars or a previous install seeded this same file.
                continue
            try:
                shutil.copyfile(seed_file, dest_file)
                copied += 1
            except OSError:
                # Best-effort: one unreadable seed file shouldn't abort the rest.
                continue
    return copied
