"""Strategy registry — auto-discovers and manages strategy classes."""

import importlib
import inspect
import json
import logging
import pkgutil
import re
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from forven.strategies.base import BaseStrategy
from forven.strategies.custom_catalog import custom_strategy_status, include_archived_custom_strategies
from forven.strategies.params import canonicalize_params_with_metadata, resolve_strategy_family

log = logging.getLogger("forven.strategies.registry")

# Global registry: strategy_id -> BaseStrategy instance
_registry: dict[str, BaseStrategy] = {}

# Type -> class mapping (populated during discover())
_TYPE_MAP: dict[str, type[BaseStrategy]] = {}
_DYNAMIC_REGIME_CLASS: dict[type, type] = {}
_ARCHIVED_CUSTOM_MODULES: dict[str, str] = {}

# Reserved runtime-type prefix for untrusted-origin (imported, sandbox-only)
# strategies. Their runtime type is ``imported__<module>`` — keyed by MODULE NAME,
# never the module's self-declared TYPE_NAME — so an import can never shadow a real
# builtin/custom type. Only the worker's _TYPE_MAP ever holds these keys.
IMPORTED_TYPE_PREFIX = "imported__"

# Custom modules that failed to import — memoized so repeated discover() calls
# (after a reset, or across the many backtests in a long-lived process) do not
# re-attempt the import and re-emit the same "Skipping custom strategy module"
# warning thousands of times. _FAILED_CUSTOM_LOGGED gates the warning to once per
# module per process. Both are cleared by reset(); a fresh subprocess re-learns
# on its first discover().
_FAILED_CUSTOM_MODULES: set[str] = set()
_FAILED_CUSTOM_LOGGED: set[str] = set()

# get_active() hydration cache. Hydrating every deployed/paper row is seconds of
# GIL-held work (JSON + type resolution + instantiation) and was historically
# re-run PER STRATEGY per scan — dozens of concurrent full sweeps per minute that
# starved the uvicorn event loop (the "event loop stalled" DEGRADED storms).
# Entries: (hydrated_map, rows_signature, monotonic_stamp). Within the TTL the
# cache is returned as-is; after it, a cheap COUNT/MAX(updated_at) signature
# probe decides whether the hydrate actually needs to re-run.
_ACTIVE_CACHE: tuple[dict, tuple | None, float] | None = None
_ACTIVE_CACHE_LOCK = threading.Lock()
_ACTIVE_CACHE_TTL_SECONDS = 10.0

# Bad-row warnings deduped per (strategy_id, error) per process: the same broken
# rows re-fail on every hydrate and were emitting thousands of identical
# warnings a day.
_BAD_ROW_LOGGED: set[tuple[str, str]] = set()

_discovered = False
_builtin_discovered = False
_custom_discovered = False

# Canonical disambiguation map: maps ambiguous or aliased type names (lowercase) to the
# preferred registered runtime type.  Used by resolve_runtime_type() to break ties when
# a case-insensitive exact match or prefix search would otherwise return multiple results.
_DISAMBIGUATION_MAP: dict[str, str] = {
    # SUPERTREND / supertrend → the base supertrend type registered from SUPERTREND.py
    "supertrend": "supertrend",
    # VWAP_trend has three prefix matches (composite / momentum / pullback); composite is canonical
    "vwap_trend": "vwap_trend_composite",
}


def register(strategy: BaseStrategy):
    """Register a strategy instance."""
    _registry[strategy.strategy_id] = strategy
    invalidate_active_cache()
    log.debug("Registered strategy: %s (%s)", strategy.strategy_id, strategy.name)


class RegistryTypeError(Exception):
    """A strategy class failed the abstract-method contract at registration.

    Raised (only when ``raise_on_skip=True``) so the custom-module discover loop
    can record the module in ``_FAILED_CUSTOM_MODULES`` and warn ONCE, instead of
    re-attempting registration and re-warning on every import (~932x/process for a
    persistently-broken generated module).
    """


# Every registered runtime type must be identifier-shaped. All 76 builtin
# TYPE_NAMEs are lowercase snake_case and imported modules register as
# ``imported__<module>``, so this rejects only genuine garbage — the observed
# failure was a codegen'd TYPE_NAME declared as a @property, whose class-level
# getattr yields the property OBJECT and str() turns it into
# "<property object at 0x...>", which then rendered verbatim in the Strategy
# Creator catalog.
_TYPE_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _type_name_validation_error(strategy_type: object) -> str | None:
    """One-line diagnostic if the type name is unusable, else None."""
    if not isinstance(strategy_type, str):
        return (
            f"TYPE_NAME must be a plain string, got {type(strategy_type).__name__} "
            f"{strategy_type!r} (a TYPE_NAME declared as a @property resolves to the "
            "property object itself — declare it as a class attribute string)"
        )
    if not _TYPE_NAME_PATTERN.fullmatch(strategy_type):
        return (
            f"TYPE_NAME {strategy_type!r} is not a valid identifier "
            "(letters, digits and underscores only, max 64 chars)"
        )
    return None


def register_type(strategy_type: str, cls: type[BaseStrategy], *, raise_on_skip: bool = False):
    """Register a strategy class for a given type string.

    A class missing required abstract methods is normally logged and skipped
    (builtin path keeps this). Custom-module callers pass ``raise_on_skip=True``
    so a persistently-broken generated module is quarantined after one warning
    rather than re-warned on every discover.
    """
    errors = _registry_type_validation_errors(cls)
    name_error = _type_name_validation_error(strategy_type)
    if name_error:
        errors = [name_error, *errors]
    if errors:
        if raise_on_skip:
            raise RegistryTypeError(
                f"{getattr(cls, '__module__', '?')}.{getattr(cls, '__name__', '?')}: "
                + "; ".join(errors)
            )
        log.warning(
            "Skipping strategy type registration for '%s' from %s.%s: %s",
            strategy_type,
            getattr(cls, "__module__", type(cls).__module__),
            getattr(cls, "__name__", type(cls).__name__),
            "; ".join(errors),
        )
        return
    _TYPE_MAP[strategy_type] = cls


def get(strategy_id: str) -> BaseStrategy | None:
    """Get a strategy by ID."""
    return _registry.get(strategy_id)


def get_all() -> dict[str, BaseStrategy]:
    """Get all registered strategies."""
    return dict(_registry)


def invalidate_active_cache() -> None:
    """Drop the get_active() hydration cache (next call re-hydrates)."""
    global _ACTIVE_CACHE
    _ACTIVE_CACHE = None


def _active_rows_signature() -> tuple | None:
    """Cheap change signature for the deployed/paper rows (None if unreadable)."""
    try:
        from forven.db import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM strategies "
                "WHERE status IN ('deployed', 'paper')"
            ).fetchone()
        return (int(row[0]), str(row[1]))
    except Exception:
        return None


def get_active() -> dict[str, BaseStrategy]:
    """Get strategies eligible for scanning (registered + DB deployed/paper).

    Hydration is cached: callers throughout a scan (including per-strategy
    fallbacks) share one hydrate instead of each re-reading and re-instantiating
    every row. Staleness is bounded by _ACTIVE_CACHE_TTL_SECONDS, after which a
    signature probe re-hydrates only if the underlying rows actually changed.
    """
    global _ACTIVE_CACHE
    cached = _ACTIVE_CACHE
    now = time.monotonic()
    if cached is not None and (now - cached[2]) < _ACTIVE_CACHE_TTL_SECONDS:
        return dict(cached[0])

    with _ACTIVE_CACHE_LOCK:
        cached = _ACTIVE_CACHE
        now = time.monotonic()
        if cached is not None and (now - cached[2]) < _ACTIVE_CACHE_TTL_SECONDS:
            return dict(cached[0])

        sig = _active_rows_signature()
        if cached is not None and sig is not None and sig == cached[1]:
            _ACTIVE_CACHE = (cached[0], sig, now)
            return dict(cached[0])
        if cached is not None and sig is None:
            # Signature probe failed (DB contention): serve the stale cache
            # rather than pile a full hydrate onto a struggling database.
            _ACTIVE_CACHE = (cached[0], cached[1], now)
            return dict(cached[0])

        active = dict(_registry)
        _load_db_strategies(active)
        # Signature AFTER the hydrate: it may backfill runtime_type (bumping
        # updated_at), and the post-write state is what the cache now mirrors.
        _ACTIVE_CACHE = (active, _active_rows_signature(), time.monotonic())
        return dict(active)


def build_strategy_from_row(row: Mapping[str, object]) -> BaseStrategy:
    """Instantiate a strategy from a DB-style row without mutating storage."""
    discover()

    data = dict(row or {})
    sid = str(data.get("id") or "").strip() or "<unknown>"
    stype = str(data.get("type") or "").strip()
    if not stype:
        raise ValueError("missing strategy type")

    raw_params = data.get("params", {})
    if isinstance(raw_params, str):
        try:
            params = json.loads(raw_params or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid params JSON: {exc}") from exc
    elif isinstance(raw_params, dict):
        params = dict(raw_params)
    else:
        raise ValueError("params must be a JSON object or dict")
    if not isinstance(params, dict):
        raise ValueError("params must decode to an object")

    resolved_runtime_type, runtime_meta = resolve_runtime_type(
        stype,
        data.get("runtime_type"),
    )
    if not resolved_runtime_type:
        raise ValueError(str(runtime_meta.get("blocked_reason") or "missing runtime type"))

    canonical_params, canonical_meta = canonicalize_params_with_metadata(
        resolved_runtime_type,
        params,
    )

    compatible_regimes = _parse_json_list(data.get("compatible_regimes"))
    raw_metrics = data.get("metrics", {})
    if isinstance(raw_metrics, str):
        try:
            metrics = json.loads(raw_metrics or "{}")
        except (TypeError, json.JSONDecodeError):
            metrics = {}
    elif isinstance(raw_metrics, dict):
        metrics = dict(raw_metrics)
    else:
        metrics = {}
    if not compatible_regimes:
        compatible_regimes = _parse_json_list(metrics.get("compatible_regimes"))
    is_all_rounder = bool(metrics.get("is_all_rounder", False))

    if data.get("symbol"):
        canonical_params["_asset"] = data["symbol"]

    # Untrusted-origin (sandbox-only) strategies are NEVER instantiated from their
    # real class in the trusted parent — the parent doesn't even have it. Build the
    # non-executing proxy; all signal generation routes to the worker by type+params.
    if bool(data.get("sandbox_only")) or runtime_meta.get("sandbox_only") or resolved_runtime_type.startswith(IMPORTED_TYPE_PREFIX):
        from forven.strategies.sandbox_proxy import SandboxOnlyStrategy

        strategy = SandboxOnlyStrategy(sid, canonical_params, runtime_type=resolved_runtime_type)
        _attach_runtime_metadata(
            strategy,
            family_type=resolve_strategy_family(stype),
            runtime_type=resolved_runtime_type,
            runtime_source=str(runtime_meta.get("source") or "sandbox_only"),
            param_meta=canonical_meta,
        )
        _inject_regime_metadata(strategy, compatible_regimes, is_all_rounder)
        return strategy

    cls = _TYPE_MAP.get(resolved_runtime_type)
    if not cls:
        raise ValueError(f"runtime type '{resolved_runtime_type}' is not registered")

    strategy = cls(sid, canonical_params)
    _attach_runtime_metadata(
        strategy,
        family_type=resolve_strategy_family(stype),
        runtime_type=resolved_runtime_type,
        runtime_source=str(runtime_meta.get("source") or "registry"),
        param_meta=canonical_meta,
    )
    _inject_regime_metadata(strategy, compatible_regimes, is_all_rounder)
    return strategy


def runtime_unloadable_reason(strategy_type: object, runtime_type: object) -> str | None:
    """Return why a strategy's runtime cannot load, or None if it resolves.

    Uses the same resolution the paper runtime uses, so callers flag exactly
    the strategies whose paper sessions would sit blocked with
    "runtime type 'x' is not registered".
    """
    normalized_type = str(strategy_type or "").strip()
    normalized_runtime = str(runtime_type or "").strip()
    if not normalized_type and not normalized_runtime:
        return "strategy has no type or runtime_type"
    try:
        discover()
        resolved, meta = resolve_runtime_type(normalized_type or None, normalized_runtime or None)
    except Exception as exc:
        return f"runtime resolution error: {exc}"
    if resolved:
        return None
    blocked = (meta or {}).get("blocked_reason")
    return str(blocked or "runtime type could not be resolved")


def discover(include_custom: bool = True):
    """Auto-discover strategy classes in forven.strategies.builtin and custom.

    Idempotent — safe to call multiple times.
    """
    global _builtin_discovered, _custom_discovered, _discovered
    if include_custom and _discovered:
        return
    if not include_custom and _builtin_discovered:
        return
    if not _builtin_discovered:
        _registry.clear()
        try:
            from forven.strategies import builtin
        except ImportError as e:
            log.warning("Could not discover builtin strategies: %s", e)
        else:
            loaded_builtin = 0
            skipped_builtin = 0
            for _importer, modname, _ispkg in pkgutil.iter_modules(builtin.__path__):
                try:
                    _load_builtin_strategy_module(modname)
                    loaded_builtin += 1
                except Exception as e:
                    log.warning("Skipping builtin strategy module %s: %s", modname, e)
                    skipped_builtin += 1
            log.info(
                "Discovered %d builtin strategies, %d types (%d modules loaded, %d skipped)",
                len(_registry),
                len(_TYPE_MAP),
                loaded_builtin,
                skipped_builtin,
            )
        _builtin_discovered = True

    if include_custom and not _custom_discovered:
        try:
            from forven.strategies import custom
        except ImportError:
            _custom_discovered = True
            _discovered = _builtin_discovered and _custom_discovered
            return
        include_archived = include_archived_custom_strategies()
        loaded_custom = 0
        skipped_archived = 0
        skipped_errors = 0
        skipped_known_broken = 0
        for _importer, modname, _ispkg in pkgutil.iter_modules(custom.__path__):
            if not modname or modname == "__init__":
                continue
            # Already known to fail import this process — skip silently so a
            # broken module isn't re-imported (and re-warned) on every discover.
            if modname in _FAILED_CUSTOM_MODULES:
                skipped_known_broken += 1
                continue
            if custom_strategy_status(modname) == "archived":
                _ARCHIVED_CUSTOM_MODULES[modname.lower()] = modname
                if not include_archived:
                    skipped_archived += 1
                    continue
            try:
                _load_custom_strategy_module(modname)
                loaded_custom += 1
            except Exception as e:
                _FAILED_CUSTOM_MODULES.add(modname)
                # Warn once per module per process; stay quiet on later discovers.
                if modname not in _FAILED_CUSTOM_LOGGED:
                    _FAILED_CUSTOM_LOGGED.add(modname)
                    log.warning("Skipping custom strategy module %s: %s", modname, e)
                skipped_errors += 1
        log.info(
            "Custom strategies loaded: %d module(s), %d archived skipped, %d errors skipped, "
            "%d known-broken skipped, %d total types now",
            loaded_custom,
            skipped_archived,
            skipped_errors,
            skipped_known_broken,
            len(_TYPE_MAP),
        )
        # Ensure every active-stage strategy's runtime class is registered, even
        # when its file uses an archived-style name (..._sNNNNN.py) that the scan
        # above skipped — otherwise a legit paper/live strategy whose TYPE_NAME
        # differs from its filename is blocked as "runtime type not registered".
        _ensure_active_db_strategy_modules()
        # Worker-ONLY: also load untrusted-origin (imported/) strategies so the
        # sandbox can run them by type+params. The trusted PARENT never enters this
        # branch (FORVEN_IN_STRATEGY_WORKER is unset there), so author-controlled
        # imported code is imported exclusively inside the locked-down subprocess.
        if _in_strategy_worker():
            _discover_imported_modules()
        _custom_discovered = True

    _discovered = _builtin_discovered and _custom_discovered


def imported_module_exists(runtime_type: object) -> bool:
    """Parent-safe existence probe for an untrusted-origin (imported) type.

    True iff the namespaced type maps to a real module file under
    ``forven/strategies/imported/``. Pure filesystem check — the module is NEVER
    imported here (author-controlled code must only execute in the sandbox
    worker). Used by the certification gate and the scanner load gate so a
    genuinely imported strategy is executable via the worker proxy while a
    fabricated ``imported__*`` name (the PHANTOM-1 class) still fails closed.
    """
    name = str(runtime_type or "").strip()
    if not name.startswith(IMPORTED_TYPE_PREFIX):
        return False
    module = name[len(IMPORTED_TYPE_PREFIX):]
    # Module names are generated slugs; refuse anything path-traversal-shaped.
    if not module or not all(ch.isalnum() or ch == "_" for ch in module):
        return False
    return (Path(__file__).resolve().parent / "imported" / f"{module}.py").is_file()


def imported_runtime_type(module_name: str) -> str:
    """The namespaced runtime-type key for an untrusted-origin (imported) strategy.

    Imported strategies are routed/executed by MODULE NAME under a reserved prefix
    (never their self-declared TYPE_NAME), so a malicious import can never shadow or
    hijack a builtin/custom type in the worker's registry (e.g. declaring
    ``TYPE_NAME='macd'`` to take over real macd execution). The module name is unique
    within forven/strategies/imported/ and the prefix can't collide with a real type."""
    return f"{IMPORTED_TYPE_PREFIX}{str(module_name).strip()}"


def _discover_imported_modules() -> None:
    """Worker-ONLY: import every untrusted-origin strategy under
    ``forven.strategies.imported`` and register it in the worker's _TYPE_MAP under a
    NAMESPACED key (``imported__<module>``) so the sandbox can run it by type+params.

    Hard-guarded to the worker process: if ever called in the trusted parent it is a
    no-op, so author-controlled imported code can never execute in the host. The
    namespaced key (NOT the module's self-declared TYPE_NAME) prevents an imported
    module from overriding a real builtin/custom type via last-writer-wins."""
    if not _in_strategy_worker():
        return
    try:
        from forven.strategies import imported
    except ImportError:
        return
    for _importer, modname, _ispkg in pkgutil.iter_modules(imported.__path__):
        if not modname or modname == "__init__":
            continue
        try:
            assert_custom_module_safe(modname, package="imported")
            module = importlib.import_module(f"forven.strategies.imported.{modname}")
            cls = _resolve_module_strategy_class(module)
            if cls is None:
                raise RegistryTypeError(f"no single BaseStrategy subclass in imported.{modname}")
            errors = _registry_type_validation_errors(cls)
            if errors:
                raise RegistryTypeError("; ".join(errors))
            _TYPE_MAP[imported_runtime_type(modname)] = cls
        except Exception as e:
            if modname not in _FAILED_CUSTOM_LOGGED:
                _FAILED_CUSTOM_LOGGED.add(modname)
                log.warning("Skipping imported strategy module %s: %s", modname, e)


def _resolve_module_strategy_class(module) -> type | None:
    """Resolve the single BaseStrategy subclass a strategy module exports (via
    STRATEGY_CLASS, a string class-name, or a lone subclass). Returns None if
    ambiguous/absent. Shared by imported discovery and the worker validator."""
    strategy_cls = getattr(module, "STRATEGY_CLASS", None)
    if isinstance(strategy_cls, str):
        strategy_cls = getattr(module, strategy_cls, None)
    if isinstance(strategy_cls, type) and issubclass(strategy_cls, BaseStrategy):
        return strategy_cls
    subclasses = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, BaseStrategy)
        and obj is not BaseStrategy
        and getattr(obj, "__module__", None) == module.__name__
    ]
    return subclasses[0] if len(subclasses) == 1 else None


def _ensure_active_db_strategy_modules() -> None:
    """Register the runtime class for every strategy in an active stage, even when
    its file uses an archived-style name (``..._sNNNNN.py``) that ``discover()``
    skipped. Without this, a legitimate paper/live strategy whose ``TYPE_NAME``
    differs from its filename is blocked at runtime as "runtime type 'x' is not
    registered" after a restart (the lazy archived loader only resolves when the
    runtime name equals the module name). Bounded to active strategies; best-effort
    and never raises."""
    # The isolated strategy worker is DB-jailed (forven.db refuses connections when
    # FORVEN_IN_STRATEGY_WORKER is set) and resolves the one type it needs per-request
    # from the filesystem/imported scan — it never needs this parent-side convenience
    # sweep. Skip it explicitly so worker startup stays clean (rather than relying on
    # the broad except below to swallow the jail's RuntimeError every spawn).
    if _in_strategy_worker():
        return
    try:
        from forven.db import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT type AS stype, runtime_type AS rtype, source_ref FROM strategies "
                "WHERE LOWER(TRIM(COALESCE(stage, ''))) IN "
                "('paper', 'paper_trading', 'live_graduated', 'deployed', 'gauntlet', 'quick_screen') "
                "AND LOWER(TRIM(COALESCE(status, ''))) NOT IN ('archived', 'rejected')"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - best-effort registration sweep
        log.warning("active-strategy registration sweep: DB read failed: %s", exc)
        return
    loaded = 0
    for row in rows:
        stype = str(row["stype"] or "").strip()
        rtype = str(row["rtype"] or "").strip()
        # Untrusted-origin (imported, sandbox-only) types are NEVER imported into the
        # trusted parent — their class lives only in the worker. Dropzone rows carry
        # the imported__ prefix in runtime_type while their bare `type` keeps the
        # author's TYPE_NAME, so both columns must be checked or the sweep attempts a
        # doomed forven.strategies.custom.dropzone_* import for every imported row.
        if (
            not stype
            or stype in _TYPE_MAP
            or stype.startswith(IMPORTED_TYPE_PREFIX)
            or rtype.startswith(IMPORTED_TYPE_PREFIX)
        ):
            continue
        src = str(row["source_ref"] or "").strip()
        if not src:
            continue
        modname = Path(src).stem
        if not modname or modname in _FAILED_CUSTOM_MODULES:
            continue
        try:
            _load_custom_strategy_module(modname)
            loaded += 1
        except Exception as exc:  # noqa: BLE001 - quarantine a broken module, warn once
            _FAILED_CUSTOM_MODULES.add(modname)
            if modname not in _FAILED_CUSTOM_LOGGED:
                _FAILED_CUSTOM_LOGGED.add(modname)
                log.warning(
                    "active-strategy registration: module %s (type=%s) failed: %s",
                    modname, stype, exc,
                )
    if loaded:
        log.info(
            "active-strategy registration sweep: registered %d module(s) backing "
            "active strategies that the archived-name filter had skipped",
            loaded,
        )


def _load_builtin_strategy_module(modname: str) -> None:
    module = importlib.import_module(f"forven.strategies.builtin.{modname}")
    if hasattr(module, "STRATEGIES"):
        for sid, cls, params in module.STRATEGIES:
            register(cls(sid, params))
    if hasattr(module, "STRATEGY_CLASS") and hasattr(module, "TYPE_NAME"):
        register_type(module.TYPE_NAME, module.STRATEGY_CLASS)


def _register_module_type_tolerant(module, *, raise_on_skip: bool = False) -> None:
    """Register a custom module's TYPE_NAME -> class.

    A well-formed module (module-level ``STRATEGY_CLASS`` class + ``TYPE_NAME``)
    keeps the historical last-writer-wins behavior, so existing strategies keep
    resolving to the exact same class they do today.

    For common codegen contract slips — ``STRATEGY_CLASS`` declared as a string,
    no module-level ``STRATEGY_CLASS`` at all, or ``TYPE_NAME`` declared only as a
    class attribute — a tolerant fallback recovers the type. To avoid changing
    resolution for anything already registered, the fallback only FILLS GAPS:
    it never overrides an existing type.
    """
    cls = getattr(module, "STRATEGY_CLASS", None)
    type_name = getattr(module, "TYPE_NAME", None)

    # Explicit, well-formed declaration → preserve existing behavior exactly.
    # Passed raw (no str() coercion) so a non-string TYPE_NAME gets the precise
    # register_type diagnostic instead of being smuggled in as its repr.
    if isinstance(cls, type) and type_name:
        register_type(type_name, cls, raise_on_skip=raise_on_skip)
        return

    # Tolerant fallback (gap-fill only). Resolve the class first.
    if not isinstance(cls, type):
        candidates = [
            obj
            for obj in vars(module).values()
            if isinstance(obj, type)
            and issubclass(obj, BaseStrategy)
            and obj is not BaseStrategy
            and getattr(obj, "__module__", None) == module.__name__
        ]
        if len(candidates) != 1:
            return
        cls = candidates[0]
    if not type_name:
        type_name = getattr(cls, "TYPE_NAME", None)
    if not type_name:
        return
    if isinstance(type_name, str) and type_name in _TYPE_MAP:
        # Never override an already-registered type from the tolerant path.
        # (Non-strings can never be registered, so they skip straight to the
        # register_type name validation for a precise diagnostic.)
        return
    register_type(type_name, cls, raise_on_skip=raise_on_skip)


# AST-guard verdict cache. assert_custom_module_safe is re-entered on EVERY
# backtest, optimization, and archived-runtime-type resolution (backtest.py /
# optimizer.py call it per run), each time re-reading and re-parsing the same
# source through the guard — a profile showed this at ~16% of single-worker CPU
# under a busy gauntlet. Cache the verdict keyed by (path, mtime_ns, ctime_ns,
# size): a touched/rewritten file changes mtime_ns (and, on POSIX, ctime_ns on
# ANY write) or size, forcing a fresh scan. NOTE: this trusts stat metadata, not
# content — an actor who can write the file AND preserve all of mtime_ns,
# ctime_ns and size (e.g. os.utime after a same-size swap) could evade the
# re-scan. That is acceptable defense-in-depth here (writing the operator's own
# strategy file already implies host access; the guard's primary job is the
# code-ingress path), but it is NOT a content-integrity guarantee. Dict get/set
# is atomic under the GIL, so a cold-miss race just rescans once (harmless) — no
# lock needed on this hot path.
_SCAN_VERDICT_CACHE: dict[tuple[str, int, int, int], tuple[bool, str]] = {}


def _in_strategy_worker() -> bool:
    """True only inside the locked-down sandbox subprocess (set via the worker env).

    Used to gate worker-ONLY behaviour: the trusted parent never imports
    untrusted-origin (``forven.strategies.imported``) modules; the worker does."""
    import os

    return bool(os.environ.get("FORVEN_IN_STRATEGY_WORKER"))


def assert_custom_module_safe(modname: str, package: str = "custom") -> None:
    """C-1: statically AST-scan a custom strategy module BEFORE it is imported
    into the live process.

    Custom modules' top-level code executes with host privileges (os.environ
    secrets, the decrypted Fernet key in memory, exchange creds). The subprocess
    sandbox isolates one-shot validation/backtests, but the runtime registry and
    optimizer must import the class IN-PROCESS to call generate_signal on each
    candle tick — there is no per-tick subprocess. So the proportionate floor for
    the in-process path is the static guard: reject forbidden imports
    (os/subprocess/socket/urllib/…), exec/eval, and dunder access before import.

    Raises ImportError if the module fails the guard, so callers skip+log it like
    any other broken module. Modules with no resolvable .py file (namespace
    packages) pass through untouched.
    """
    pkg = importlib.import_module(f"forven.strategies.{package}")

    source_path: Path | None = None
    for root in list(getattr(pkg, "__path__", []) or []):
        candidate = Path(root) / f"{modname}.py"
        if candidate.is_file():
            source_path = candidate
            break
    if source_path is None:
        return

    # Stat-cheap freshness key; a cache hit skips the read + AST parse entirely.
    try:
        st = source_path.stat()
        cache_key: tuple[str, int, int, int] | None = (
            str(source_path), st.st_mtime_ns, st.st_ctime_ns, st.st_size,
        )
    except OSError:
        cache_key = None
    if cache_key is not None:
        cached = _SCAN_VERDICT_CACHE.get(cache_key)
        if cached is not None:
            ok, findings = cached
            if ok:
                return
            raise ImportError(
                f"custom.{modname} rejected by the AST security guard: {findings}"
            )

    try:
        from forven.sandbox.ast_guard import scan_source

        report = scan_source(source_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # never import unscanned code if the guard itself fails
        raise ImportError(f"security scan failed for custom.{modname}: {exc}") from exc
    if not report.ok:
        findings = "; ".join(
            f"line {f.lineno}: {f.message}" for f in report.findings[:5]
        )
        if cache_key is not None:
            _SCAN_VERDICT_CACHE[cache_key] = (False, findings)
        raise ImportError(
            f"custom.{modname} rejected by the AST security guard: {findings}"
        )
    if cache_key is not None:
        _SCAN_VERDICT_CACHE[cache_key] = (True, "")


def _load_custom_strategy_module(modname: str, package: str = "custom") -> None:
    assert_custom_module_safe(modname, package=package)
    module = importlib.import_module(f"forven.strategies.{package}.{modname}")
    if hasattr(module, "STRATEGIES"):
        for sid, cls, params in module.STRATEGIES:
            register(cls(sid, params))
    # raise_on_skip=True: a class that fails the abstract contract raises
    # RegistryTypeError so discover() records the module as broken and warns once,
    # rather than re-attempting (and re-warning) on every import.
    _register_module_type_tolerant(module, raise_on_skip=True)


def _load_archived_custom_runtime_type(runtime_name: str) -> bool:
    module_name = _ARCHIVED_CUSTOM_MODULES.get(str(runtime_name or "").strip().lower())
    if not module_name:
        return False
    if module_name in _FAILED_CUSTOM_MODULES:
        return False
    try:
        _load_custom_strategy_module(module_name)
    except (RegistryTypeError, ImportError):
        # Broken/guard-rejected archived module — don't let the error escape the
        # runtime-type resolver (the type simply stays unresolved), and remember
        # the failure so every later hydrate doesn't re-read and re-execute the
        # same broken file.
        _FAILED_CUSTOM_MODULES.add(module_name)
        return False
    return str(runtime_name or "").strip() in _TYPE_MAP


def _load_db_strategies(target: dict):
    """Load strategies from SQLite with status='deployed'|'paper'."""
    try:
        from forven.db import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM strategies WHERE status IN ('deployed', 'paper')"
            ).fetchall()
    except Exception as e:
        log.warning("Could not load DB strategies: %s", e)
        return

    try:
        from forven.db import get_db
        with get_db() as conn:
            for raw_row in rows:
                row = dict(raw_row)
                sid = str(row.get("id") or "").strip() or "<unknown>"
                try:
                    stype = str(row.get("type") or "").strip()
                    if not stype:
                        raise ValueError("missing strategy type")

                    raw_params = row.get("params", "{}")
                    try:
                        params = json.loads(raw_params or "{}")
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise ValueError(f"invalid params JSON: {exc}") from exc
                    if not isinstance(params, dict):
                        raise ValueError("params must decode to an object")

                    resolved_runtime_type, runtime_meta = resolve_runtime_type(
                        stype,
                        row.get("runtime_type"),
                    )
                    if not resolved_runtime_type:
                        raise ValueError(runtime_meta["blocked_reason"])

                    canonical_params, canonical_meta = canonicalize_params_with_metadata(
                        resolved_runtime_type,
                        params,
                    )

                    compatible_regimes = _parse_json_list(row.get("compatible_regimes"))
                    is_all_rounder = False
                    try:
                        metrics = json.loads(row.get("metrics", "{}") or "{}")
                        if not compatible_regimes:
                            compatible_regimes = _parse_json_list(metrics.get("compatible_regimes"))
                        is_all_rounder = bool(metrics.get("is_all_rounder", False))
                    except (TypeError, json.JSONDecodeError):
                        pass

                    if row.get("symbol"):
                        canonical_params["_asset"] = row["symbol"]

                    runtime_type_value = str(row.get("runtime_type") or "").strip()
                    if runtime_type_value != resolved_runtime_type:
                        conn.execute(
                            "UPDATE strategies SET runtime_type = ?, updated_at = ? WHERE id = ?",
                            (
                                resolved_runtime_type,
                                datetime.now(timezone.utc).isoformat(),
                                sid,
                            ),
                        )

                    if sid in target:
                        strategy = target[sid]
                        strategy.params = {**strategy.default_params, **canonical_params}
                        _attach_runtime_metadata(
                            strategy,
                            family_type=resolve_strategy_family(stype),
                            runtime_type=resolved_runtime_type,
                            runtime_source=str(runtime_meta.get("source") or "registry"),
                            param_meta=canonical_meta,
                        )
                        _inject_regime_metadata(strategy, compatible_regimes, is_all_rounder)
                        continue

                    # Untrusted-origin (sandbox-only) strategies promoted to paper/live
                    # are hydrated as the non-executing proxy — the real class is never
                    # imported in the parent; the scanner routes execution to the worker.
                    if bool(row.get("sandbox_only")) or runtime_meta.get("sandbox_only") or resolved_runtime_type.startswith(IMPORTED_TYPE_PREFIX):
                        from forven.strategies.sandbox_proxy import SandboxOnlyStrategy

                        strategy = SandboxOnlyStrategy(sid, canonical_params, runtime_type=resolved_runtime_type)
                    else:
                        cls = _TYPE_MAP.get(resolved_runtime_type)
                        if not cls:
                            raise ValueError(f"runtime type '{resolved_runtime_type}' is not registered")
                        strategy = cls(sid, canonical_params)
                    _attach_runtime_metadata(
                        strategy,
                        family_type=resolve_strategy_family(stype),
                        runtime_type=resolved_runtime_type,
                        runtime_source=str(runtime_meta.get("source") or "registry"),
                        param_meta=canonical_meta,
                    )
                    _inject_regime_metadata(strategy, compatible_regimes, is_all_rounder)
                    target[sid] = strategy
                except Exception as row_exc:
                    dedupe_key = (sid, str(row_exc))
                    if dedupe_key in _BAD_ROW_LOGGED:
                        log.debug("Skipping bad strategy row %s: %s", sid, row_exc)
                    else:
                        _BAD_ROW_LOGGED.add(dedupe_key)
                        log.warning("Skipping bad strategy row %s: %s", sid, row_exc)
    except Exception as e:
        log.warning("Could not hydrate DB strategies: %s", e)


def _parse_json_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str)]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v) for v in parsed if isinstance(v, str)]
        except json.JSONDecodeError:
            return []
    return []


def _registry_type_validation_errors(cls: object) -> list[str]:
    if not inspect.isclass(cls):
        return ["not a class"]
    if not issubclass(cls, BaseStrategy):
        return ["not a BaseStrategy subclass"]

    errors: list[str] = []
    abstract_methods = sorted(getattr(cls, "__abstractmethods__", ()) or ())
    if abstract_methods:
        errors.append(f"abstract methods: {', '.join(abstract_methods)}")

    try:
        inspect.signature(cls).bind_partial("__probe__", {})
    except Exception as exc:
        errors.append(f"constructor incompatible with (strategy_id, params): {exc}")

    return errors


def _inject_regime_metadata(strategy: BaseStrategy, compatible_regimes: list[str], is_all_rounder: bool):
    """Attach dynamic regime metadata to a strategy instance."""
    dynamic_cls = _get_dynamic_regime_class(type(strategy))
    if type(strategy) is not dynamic_cls:
        strategy.__class__ = dynamic_cls

    # Keep metadata in params for visibility in logs/debugging.
    strategy.params["_compatible_regimes"] = list(compatible_regimes)
    strategy.params["_is_all_rounder"] = bool(is_all_rounder)

    # Runtime attributes used by scanner gating and external inspection.
    strategy.compatible_regimes = list(compatible_regimes)
    setattr(strategy, "dynamic_compatible_regimes", list(compatible_regimes))
    setattr(strategy, "is_all_rounder", bool(is_all_rounder))


def _attach_runtime_metadata(
    strategy: BaseStrategy,
    *,
    family_type: str,
    runtime_type: str,
    runtime_source: str,
    param_meta,
) -> None:
    setattr(strategy, "family_type", family_type)
    setattr(strategy, "runtime_type", runtime_type)
    setattr(strategy, "runtime_source", runtime_source)
    setattr(strategy, "param_alias_resolutions", dict(param_meta.alias_resolutions))
    setattr(strategy, "param_unknown_params", list(param_meta.unknown_params))
    setattr(strategy, "param_unsupported_rule_blobs", list(param_meta.unsupported_rule_blobs))


def resolve_runtime_type(strategy_type: str | None, runtime_type: str | None = None) -> tuple[str | None, dict]:
    normalized_type = str(strategy_type or "").strip()
    normalized_runtime = str(runtime_type or "").strip()

    # Untrusted-origin (imported, sandbox-only) types resolve to themselves WITHOUT
    # any in-parent import — the real class lives only in the worker. Resolve early
    # so the parent never treats them as "not registered" and never tries to load
    # the module in-process.
    if normalized_runtime.startswith(IMPORTED_TYPE_PREFIX):
        return normalized_runtime, {"source": "sandbox_only", "sandbox_only": True, "blocked_reason": None}
    if normalized_type.startswith(IMPORTED_TYPE_PREFIX):
        return normalized_type, {"source": "sandbox_only", "sandbox_only": True, "blocked_reason": None}

    if normalized_runtime and normalized_runtime not in _TYPE_MAP:
        _load_archived_custom_runtime_type(normalized_runtime)
    if normalized_type and normalized_type not in _TYPE_MAP:
        _load_archived_custom_runtime_type(normalized_type)

    if normalized_runtime and normalized_runtime in _TYPE_MAP:
        return normalized_runtime, {"source": "runtime_type", "blocked_reason": None}

    if normalized_type and normalized_type in _TYPE_MAP:
        source = "family_type" if not normalized_runtime else "family_type_fallback"
        if normalized_runtime and normalized_runtime not in _TYPE_MAP:
            log.warning(
                "Runtime type '%s' not registered for '%s'; falling back to family type",
                normalized_runtime,
                normalized_type,
            )
        return normalized_type, {"source": source, "blocked_reason": None}

    if normalized_runtime and custom_strategy_status(normalized_runtime) == "archived":
        return normalized_runtime, {"source": "archived_runtime_type", "blocked_reason": None}
    if normalized_type and custom_strategy_status(normalized_type) == "archived":
        return normalized_type, {"source": "archived_strategy_type", "blocked_reason": None}

    if normalized_type:
        # Case-insensitive exact match against registered types.
        type_lower = normalized_type.lower()
        ci_match = next((k for k in _TYPE_MAP if k.lower() == type_lower), None)
        if ci_match:
            log.debug(
                "Resolved strategy type '%s' via case-insensitive match -> '%s'",
                normalized_type,
                ci_match,
            )
            return ci_match, {"source": "type_ci_match", "blocked_reason": None}

        # Canonical disambiguation map: resolves known ambiguous/aliased type names.
        canonical = _DISAMBIGUATION_MAP.get(type_lower)
        if canonical and canonical in _TYPE_MAP:
            log.debug(
                "Resolved strategy type '%s' via disambiguation map -> '%s'",
                normalized_type,
                canonical,
            )
            return canonical, {"source": "type_disambiguation_map", "blocked_reason": None}

        prefix = f"{type_lower}_"
        matches = sorted(
            key
            for key in _TYPE_MAP
            if str(key).strip().lower().startswith(prefix)
        )
        if len(matches) == 1:
            log.warning(
                "Resolved strategy type '%s' via unique runtime prefix match -> '%s'",
                normalized_type,
                matches[0],
            )
            return matches[0], {"source": "type_prefix_match", "blocked_reason": None}
        if len(matches) > 1:
            # Check disambiguation map before giving up on ambiguous prefix matches.
            canonical = _DISAMBIGUATION_MAP.get(type_lower)
            if canonical and canonical in _TYPE_MAP:
                log.warning(
                    "Resolved ambiguous strategy type '%s' via disambiguation map -> '%s'",
                    normalized_type,
                    canonical,
                )
                return canonical, {"source": "type_disambiguation_map", "blocked_reason": None}
            return None, {
                "source": "blocked",
                "blocked_reason": (
                    f"ambiguous runtime type for '{normalized_type}': {', '.join(matches[:5])}"
                ),
            }

    if normalized_runtime:
        return None, {
            "source": "blocked",
            "blocked_reason": f"runtime type '{normalized_runtime}' is not registered",
        }

    return None, {
        "source": "blocked",
        "blocked_reason": f"no runtime type registered for '{normalized_type}'",
    }


def _get_dynamic_regime_class(base_cls: type) -> type:
    """Create/get a subclass with a writable compatible_regimes property."""
    if getattr(base_cls, "_dynamic_regime_enabled", False):
        return base_cls

    cached = _DYNAMIC_REGIME_CLASS.get(base_cls)
    if cached:
        return cached

    class DynamicRegimeStrategy(base_cls):  # type: ignore[misc, valid-type]
        _dynamic_regime_enabled = True

        @property
        def compatible_regimes(self) -> set[str]:
            override = getattr(self, "_compatible_regimes_override", None)
            if override is not None:
                return set(override)
            return super().compatible_regimes

        @compatible_regimes.setter
        def compatible_regimes(self, value):
            if value is None:
                self._compatible_regimes_override = []
            elif isinstance(value, (list, tuple, set)):
                self._compatible_regimes_override = [str(v) for v in value]
            else:
                self._compatible_regimes_override = [str(value)]

    DynamicRegimeStrategy.__name__ = f"{base_cls.__name__}DynamicRegime"
    _DYNAMIC_REGIME_CLASS[base_cls] = DynamicRegimeStrategy
    return DynamicRegimeStrategy


def reset():
    """Reset registry state. Used for testing."""
    global _builtin_discovered, _custom_discovered, _discovered
    _registry.clear()
    _TYPE_MAP.clear()
    _DYNAMIC_REGIME_CLASS.clear()
    _ARCHIVED_CUSTOM_MODULES.clear()
    _FAILED_CUSTOM_MODULES.clear()
    _FAILED_CUSTOM_LOGGED.clear()
    _SCAN_VERDICT_CACHE.clear()
    _BAD_ROW_LOGGED.clear()
    invalidate_active_cache()
    _builtin_discovered = False
    _custom_discovered = False
    _discovered = False
