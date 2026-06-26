"""Strategy registry — auto-discovers and manages strategy classes."""

import importlib
import inspect
import json
import logging
import pkgutil
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

# Custom modules that failed to import — memoized so repeated discover() calls
# (after a reset, or across the many backtests in a long-lived process) do not
# re-attempt the import and re-emit the same "Skipping custom strategy module"
# warning thousands of times. _FAILED_CUSTOM_LOGGED gates the warning to once per
# module per process. Both are cleared by reset(); a fresh subprocess re-learns
# on its first discover().
_FAILED_CUSTOM_MODULES: set[str] = set()
_FAILED_CUSTOM_LOGGED: set[str] = set()

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
    log.debug("Registered strategy: %s (%s)", strategy.strategy_id, strategy.name)


class RegistryTypeError(Exception):
    """A strategy class failed the abstract-method contract at registration.

    Raised (only when ``raise_on_skip=True``) so the custom-module discover loop
    can record the module in ``_FAILED_CUSTOM_MODULES`` and warn ONCE, instead of
    re-attempting registration and re-warning on every import (~932x/process for a
    persistently-broken generated module).
    """


def register_type(strategy_type: str, cls: type[BaseStrategy], *, raise_on_skip: bool = False):
    """Register a strategy class for a given type string.

    A class missing required abstract methods is normally logged and skipped
    (builtin path keeps this). Custom-module callers pass ``raise_on_skip=True``
    so a persistently-broken generated module is quarantined after one warning
    rather than re-warned on every discover.
    """
    errors = _registry_type_validation_errors(cls)
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


def get_active() -> dict[str, BaseStrategy]:
    """Get strategies eligible for scanning (registered + DB deployed/paper)."""
    active = dict(_registry)
    _load_db_strategies(active)
    return active


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
        _custom_discovered = True

    _discovered = _builtin_discovered and _custom_discovered


def _ensure_active_db_strategy_modules() -> None:
    """Register the runtime class for every strategy in an active stage, even when
    its file uses an archived-style name (``..._sNNNNN.py``) that ``discover()``
    skipped. Without this, a legitimate paper/live strategy whose ``TYPE_NAME``
    differs from its filename is blocked at runtime as "runtime type 'x' is not
    registered" after a restart (the lazy archived loader only resolves when the
    runtime name equals the module name). Bounded to active strategies; best-effort
    and never raises."""
    try:
        from forven.db import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT type AS stype, source_ref FROM strategies "
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
        if not stype or stype in _TYPE_MAP:
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
    if isinstance(cls, type) and type_name:
        register_type(str(type_name), cls, raise_on_skip=raise_on_skip)
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
    type_name = str(type_name)
    if type_name in _TYPE_MAP:
        # Never override an already-registered type from the tolerant path.
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


def assert_custom_module_safe(modname: str) -> None:
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
    from forven.strategies import custom

    source_path: Path | None = None
    for root in list(getattr(custom, "__path__", []) or []):
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


def _load_custom_strategy_module(modname: str) -> None:
    assert_custom_module_safe(modname)
    module = importlib.import_module(f"forven.strategies.custom.{modname}")
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
    try:
        _load_custom_strategy_module(module_name)
    except RegistryTypeError:
        # Broken archived module — don't let the contract error escape the
        # runtime-type resolver; the type simply stays unresolved.
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
    _builtin_discovered = False
    _custom_discovered = False
    _discovered = False
