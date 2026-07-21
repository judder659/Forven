"""Strategy intake scanner — discovers, validates, and registers new custom strategies."""

from __future__ import annotations

import ast
import importlib
import json
import logging
import pkgutil
import re
from pathlib import Path
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

log = logging.getLogger("forven.strategies.intake")


# Banned third-party libraries. See `tests/test_no_ta_imports.py` for the
# historical rationale. These are rejected at intake time so LLM-generated
# strategy files that import them never land in the registry, even if the
# file happens to import on this machine.
_BANNED_IMPORT_ROOTS: frozenset[str] = frozenset({"ta"})


# Supported intervals for the STORED strategy timeframe. A declared "_timeframe"
# outside this set (typo / no-data interval) falls back to "1h" so it can never
# wedge the gauntlet on an "unsupported interval" error.
_SUPPORTED_TIMEFRAMES: frozenset[str] = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})


def _intended_timeframe(stored_params: object) -> str:
    """Resolve the timeframe to STORE for a drop-zone strategy.

    Reads an optional ``_timeframe`` key from the strategy's stored params
    (mirroring the existing ``_asset`` convention), validated against the data
    layer's supported intervals; falls back to ``"1h"`` when absent, blank, or
    unsupported. The gauntlet gates -- including the initial quick_screen, which
    runs BEFORE timeframe_sweep -- evaluate on the STORED timeframe, so
    hard-coding "1h" made a 4h-only edge die at the 1h quick_screen before the
    sweep could rescue it.
    """
    if not isinstance(stored_params, dict):
        return "1h"
    declared = str(stored_params.get("_timeframe") or "1h").strip().lower() or "1h"
    try:
        from forven.market_data import INTERVAL_TO_MS
        supported = set(INTERVAL_TO_MS)
    except Exception:
        supported = set(_SUPPORTED_TIMEFRAMES)
    return declared if declared in supported else "1h"


def _file_uses_banned_imports(path: Path) -> list[str]:
    """Return a list of banned top-level module names imported anywhere in
    the file (module level or inside function / class bodies). Empty list
    means the file is clean.

    Uses `ast` to avoid false positives on string literals that happen to
    contain "ta".
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # A syntax error is handled elsewhere (the importer will reject it).
        # Don't double-report here.
        return []

    banned_hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.name or "").split(".")[0]
                if root in _BANNED_IMPORT_ROOTS and root not in banned_hits:
                    banned_hits.append(root)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _BANNED_IMPORT_ROOTS and root not in banned_hits:
                banned_hits.append(root)
    return banned_hits


def _extract_embedded_hypothesis_id(path: Path) -> str | None:
    """Read optional hypothesis lineage from a custom strategy source file.

    Auto-intake is the last-resort scheduler path, so it should only mint a
    new strategy container when the file already declares which hypothesis it
    belongs to. We accept either a Python constant or a comment marker to keep
    the format lightweight for generated files.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    patterns = (
        r'^\s*FORVEN_HYPOTHESIS_ID\s*=\s*["\']([^"\']+)["\']',
        r'^\s*#\s*FORVEN_HYPOTHESIS_ID\s*:\s*(\S+)',
        r'^\s*#\s*hypothesis_id\s*:\s*(\S+)',
    )
    for line in source.splitlines():
        for pattern in patterns:
            match = re.match(pattern, line.strip(), flags=re.IGNORECASE)
            if match:
                normalized = str(match.group(1) or "").strip()
                if normalized:
                    return normalized
    return None


@dataclass
class IntakeEntry:
    module_name: str
    type_name: str
    strategy_id: str | None = None
    asset: str = ""
    certified: bool = False
    certification_error: str | None = None
    file_name: str = ""


@dataclass
class IntakeError:
    module_name: str
    error: str
    file_name: str = ""


@dataclass
class IntakeReport:
    scanned: int = 0
    already_known: int = 0
    new_strategies: list[IntakeEntry] = field(default_factory=list)
    errors: list[IntakeError] = field(default_factory=list)
    timestamp: str = ""

    registered: bool = False

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "already_known": self.already_known,
            "new_count": len(self.new_strategies),
            "error_count": len(self.errors),
            "new_strategies": [asdict(s) for s in self.new_strategies],
            "errors": [asdict(e) for e in self.errors],
            "timestamp": self.timestamp,
            "registered": self.registered,
        }


@dataclass
class IntakeRegistration:
    module_name: str
    type_name: str
    strategy_id: str | None = None
    asset: str = ""
    certified: bool = False
    certification_error: str | None = None
    file_name: str = ""
    source: str = ""
    source_ref: str = ""
    stage: str = "quick_screen"
    session_id: str | None = None
    # Lookahead / data-leak probe outcome (GATE B). When the vectorized signals
    # read future bars, the strategy registers as research_only (inert — the
    # gauntlet backfill only picks up quick_screen/gauntlet) with the reason here.
    lookahead_blocked: bool = False
    lookahead_reason: str | None = None
    # Data-availability probe outcome (GATE D). When a required enrichment feed
    # is unavailable and cannot be auto-downloaded, the strategy registers as
    # research_only — it can never produce a trading backtest until the data
    # exists (S05577/S05838 phantom class).
    data_blocked: bool = False
    data_block_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def scan_custom_strategies(*, register: bool = False) -> dict:
    """Scan custom/ for new strategy modules, validate, and optionally register them.

    When ``register=False`` (default), performs a dry-run: discovers and
    validates all strategy files but does NOT create DB containers or load
    modules into the runtime registry.  This prevents accidental bulk
    registration after a database reset.

    When ``register=True``, behaves as before: validates, registers in the
    runtime registry, and creates DB containers for new strategies.

    Returns a dict report suitable for JSON serialization.
    """
    from forven.strategies import custom
    from forven.strategies import registry
    from forven.strategies.custom_catalog import (
        custom_strategy_status,
        include_archived_custom_strategies,
    )
    from forven.strategies.certification import certify_execution_strategy

    report = IntakeReport(timestamp=datetime.now(timezone.utc).isoformat())
    custom_dir = Path(custom.__file__).resolve().parent
    include_archived = include_archived_custom_strategies()

    # Ensure registry has been discovered at least once
    registry.discover(include_custom=False)

    known_types = set(registry._TYPE_MAP.keys())

    for _importer, modname, _ispkg in pkgutil.iter_modules(custom.__path__):
        if not modname or modname == "__init__":
            continue

        module_status = custom_strategy_status(modname)
        if module_status == "ignored":
            continue

        report.scanned += 1
        if module_status == "archived" and not include_archived:
            report.already_known += 1
            continue

        fqn = f"forven.strategies.custom.{modname}"
        file_name = f"{modname}.py"
        source_ref = str((custom_dir / file_name).resolve())

        # Banned-import gate — reject before we even try to import so that
        # lazy `ta` imports (which don't fail until runtime) are caught.
        banned = _file_uses_banned_imports(Path(source_ref))
        if banned:
            report.errors.append(IntakeError(
                module_name=modname,
                error=(
                    f"Banned imports: {', '.join(banned)}. "
                    "These libraries are forbidden — use native pandas/numpy "
                    "instead. See forven/strategies/STRATEGY_TEMPLATE.md."
                ),
                file_name=file_name,
            ))
            continue

        # SECURITY (audit 2026-06-22, C1): AST-scan BEFORE importing in-process.
        # This bulk scan loop previously imported every custom/*.py with only the
        # `ta` banned-import gate — strictly weaker than every sibling importer
        # (register_custom_strategy_file, _load_custom_strategy_module, optimizer),
        # which all run the static guard first. A planted module's top-level code
        # would otherwise execute in the host API process.
        try:
            registry.assert_custom_module_safe(modname)
        except Exception as exc:
            report.errors.append(IntakeError(
                module_name=modname,
                error=f"Rejected by security scan: {exc}",
                file_name=file_name,
            ))
            continue

        # Custom/generated modules are untrusted. Import, construction and probes
        # run only in the locked-down worker; the API process consumes metadata.
        try:
            from forven.sandbox.strategy_worker import validate_custom_module_isolated

            meta = validate_custom_module_isolated(modname, package="custom")
        except Exception as exc:
            report.errors.append(IntakeError(
                module_name=modname,
                error=f"Isolated validation failed: {exc}",
                file_name=file_name,
            ))
            continue
        if not meta.get("ok"):
            report.errors.append(IntakeError(
                module_name=modname,
                error=f"Isolated validation failed: {meta.get('error') or 'unknown error'}",
                file_name=file_name,
            ))
            continue

        type_name = str(meta.get("type_name") or "").strip()
        asset = str(meta.get("asset") or "BTC").strip() or "BTC"
        certified = bool(meta.get("certified"))
        cert_error = meta.get("cert_error")
        lookahead_reason = meta.get("lookahead_reason")
        crash_reason = meta.get("execution_crash_reason")
        if lookahead_reason or crash_reason:
            certified = False
            cert_error = cert_error or lookahead_reason or crash_reason
        existing_strategy = _find_existing_strategy_container(
            type_name=type_name,
            source_ref=source_ref,
        )
        if existing_strategy:
            report.already_known += 1
            continue

        strategy_id = None
        if register:
            try:
                registration = _register_custom_strategy_sandboxed(
                    modname=modname,
                    source_ref=source_ref,
                    file_name=file_name,
                    source="intake_scan",
                    hypothesis_id=None,
                    session_id=None,
                    origin_task_id=None,
                    validated_meta=meta,
                )
                strategy_id = str(registration.get("strategy_id") or "") or None
                certified = bool(registration.get("certified"))
                cert_error = registration.get("certification_error")
            except Exception as exc:
                report.errors.append(IntakeError(
                    module_name=modname,
                    error=f"Sandbox registration failed: {exc}",
                    file_name=file_name,
                ))
                continue

        report.new_strategies.append(IntakeEntry(
            module_name=modname,
            type_name=type_name,
            strategy_id=strategy_id,
            asset=asset.upper(),
            certified=certified,
            certification_error=cert_error,
            file_name=file_name,
        ))
        continue

        # Try importing
        try:
            mod = importlib.import_module(fqn)
        except Exception as exc:
            report.errors.append(IntakeError(
                module_name=modname,
                error=f"Import failed: {exc}",
                file_name=file_name,
            ))
            continue

        # Validate required exports
        strategy_cls = getattr(mod, "STRATEGY_CLASS", None)
        type_name = getattr(mod, "TYPE_NAME", None)

        if not strategy_cls:
            report.errors.append(IntakeError(
                module_name=modname,
                error="Missing STRATEGY_CLASS export",
                file_name=file_name,
            ))
            continue

        if not type_name:
            report.errors.append(IntakeError(
                module_name=modname,
                error="Missing TYPE_NAME export",
                file_name=file_name,
            ))
            continue

        # Validate class
        validation_errors = registry._registry_type_validation_errors(strategy_cls)
        if validation_errors:
            report.errors.append(IntakeError(
                module_name=modname,
                error=f"Class validation: {'; '.join(validation_errors)}",
                file_name=file_name,
            ))
            continue

        # Probe for default params and asset
        try:
            probe = strategy_cls("__probe__", {})
            default_params = probe.default_params
            raw_asset = probe.asset if hasattr(probe, "asset") else "BTC"
            asset = str(raw_asset) if not isinstance(raw_asset, str) else raw_asset
            asset = asset.strip() or "BTC"
        except Exception as exc:
            report.errors.append(IntakeError(
                module_name=modname,
                error=f"Could not instantiate: {exc}",
                file_name=file_name,
            ))
            continue

        # Certification check
        cert = certify_execution_strategy(type_name, default_params)
        certified = cert.certified
        cert_error = cert.primary_blocking_reason()

        # GATE B: lookahead / data-leak probe (see register_custom_strategy_file).
        # A future-bar leak routes the strategy to research_only (inert) instead
        # of the quick_screen funnel.
        from forven.strategies.lookahead_probe import detect_execution_crash, detect_lookahead

        lookahead_reason = detect_lookahead(probe)
        if lookahead_reason:
            certified = False
            if not cert_error:
                cert_error = lookahead_reason
            log.warning(
                "Intake: lookahead detected in %s (type=%s) — registering as research_only: %s",
                modname, type_name, lookahead_reason,
            )

        # GATE C: execution smoke probe. Reject a strategy that crashes on a clean
        # synthetic run (canonically a per-bar self.position read) here, rather
        # than letting it die with a cryptic error three gates later. Downgrading
        # certified routes it to research_only (initial_stage derives from it below).
        crash_reason = detect_execution_crash(probe)
        if crash_reason:
            certified = False
            if not cert_error:
                cert_error = crash_reason
            log.warning(
                "Intake: execution smoke test failed for %s (type=%s) — registering as research_only: %s",
                modname, type_name, crash_reason,
            )

        # GATE D: data-availability probe (see register_custom_strategy_file).
        # A strategy needing an unavailable, non-downloadable feed can never
        # produce a trading backtest — park it as research_only at birth.
        data_block_reason = None
        try:
            from forven.strategies.data_availability import evaluate_data_availability

            _avail = evaluate_data_availability(
                type_name,
                asset.upper(),
                _intended_timeframe(default_params),
                strategy_cls=strategy_cls,
            )
            if _avail is not None and _avail.blocked:
                data_block_reason = _avail.error or "required data feed unavailable"
        except Exception as exc:
            log.warning(
                "Intake: data-availability probe errored for %s (parking): %s",
                modname, exc,
            )
            data_block_reason = f"data-availability check unavailable: {exc}"
        if data_block_reason:
            if not cert_error:
                cert_error = data_block_reason
            log.warning(
                "Intake: required data feed unavailable for %s (type=%s) — registering as research_only: %s",
                modname, type_name, data_block_reason,
            )

        existing_strategy = _find_existing_strategy_container(
            type_name=type_name,
            source_ref=source_ref,
        )

        if existing_strategy:
            report.already_known += 1
            continue

        # --- DRY-RUN vs REGISTER split ---
        # In dry-run mode (register=False), record the strategy as discoverable
        # but skip runtime registration and DB container creation.
        strategy_id = None

        if register:
            # Register in the runtime registry
            if type_name not in known_types:
                try:
                    registry._load_custom_strategy_module(modname)
                    known_types.add(type_name)
                except Exception as exc:
                    report.errors.append(IntakeError(
                        module_name=modname,
                        error=f"Registration failed: {exc}",
                        file_name=file_name,
                    ))
                    continue

            # Create DB container. ``certified`` may have been downgraded by the
            # lookahead probe above, so derive the stage from the (possibly
            # downgraded) local flag rather than cert.certified alone — a leak
            # forces research_only even when certification itself passed. A
            # data-blocked strategy is also parked regardless of certification.
            initial_stage = "quick_screen" if (certified and not data_block_reason) else "research_only"
            try:
                from forven.db import create_strategy_container, get_db
                stored_params = cert.canonical_params if certified else default_params
                with get_db() as conn:
                    sid, _display, _base = create_strategy_container(
                        conn=conn,
                        name=f"{asset}-{type_name}-intake",
                        type_=type_name,
                        symbol=asset.upper(),
                        timeframe=_intended_timeframe(stored_params),
                        params=stored_params,
                        stage=initial_stage,
                    )
                    strategy_id = sid
                    if data_block_reason:
                        conn.execute(
                            "UPDATE strategies SET status_reason = ? WHERE id = ?",
                            (f"data_blocked: {data_block_reason[:400]}", strategy_id),
                        )
            except Exception as exc:
                log.warning("Intake: DB container creation failed for %s: %s", modname, exc)

        entry = IntakeEntry(
            module_name=modname,
            type_name=type_name,
            strategy_id=strategy_id,
            asset=asset.upper(),
            certified=certified,
            certification_error=cert_error,
            file_name=file_name,
        )
        report.new_strategies.append(entry)

        if register:
            log.info(
                "Intake: registered %s (type=%s, certified=%s, id=%s)",
                modname, type_name, certified, strategy_id,
            )
        else:
            log.debug(
                "Intake: discovered %s (type=%s, certified=%s) — dry run, not registered",
                modname, type_name, certified,
            )

    report.registered = register

    # Log activity
    mode = "registered" if register else "dry-run"
    try:
        from forven.db import log_activity
        log_activity(
            "info",
            "strategy_intake",
            f"Intake scan ({mode}): {len(report.new_strategies)} new, {report.already_known} known, {len(report.errors)} errors",
            {"new_count": len(report.new_strategies), "error_count": len(report.errors), "registered": register},
        )
    except Exception:
        pass

    return report.to_dict()


def _register_custom_strategy_sandboxed(
    *,
    modname: str,
    source_ref: str,
    file_name: str,
    source: str,
    hypothesis_id: str | None,
    session_id: str | None,
    origin_task_id: str | None,
    validated_meta: dict | None = None,
) -> dict:
    """Register generated custom code without importing it in the API process."""
    import hashlib

    from forven.sandbox.strategy_worker import validate_custom_module_isolated
    from forven.strategies import imported as imported_pkg
    from forven.strategies import registry

    source_path = Path(source_ref).resolve()
    banned = _file_uses_banned_imports(source_path)
    if banned:
        raise ValueError(
            f"{file_name} uses banned imports: {', '.join(banned)}. "
            "Use native pandas/numpy."
        )
    registry.assert_custom_module_safe(modname)

    meta = dict(validated_meta) if isinstance(validated_meta, dict) else validate_custom_module_isolated(
        modname,
        package="custom",
    )
    if not meta.get("ok"):
        raise ValueError(
            f"isolated strategy validation failed: {meta.get('error') or 'unknown error'}"
        )
    type_name = str(meta.get("type_name") or "").strip()
    if not type_name:
        raise ValueError(f"{file_name} is missing a valid TYPE_NAME")
    existing = _find_existing_strategy_container(
        type_name=type_name,
        source_ref=str(source_path),
        ignore_terminal=True,
    )
    if existing:
        existing_id = str(existing.get("id") or "").strip() or "<unknown>"
        existing_stage = str(existing.get("stage") or "").strip() or "unknown"
        raise ValueError(
            f"Strategy '{type_name}' is already registered as {existing_id} "
            f"(stage={existing_stage}, still active)"
        )

    content = source_path.read_text(encoding="utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    sandbox_module = f"dropzone_{modname}_{digest}"
    imported_dir = Path(imported_pkg.__file__).resolve().parent
    target = imported_dir / f"{sandbox_module}.py"
    moved_file = False
    if target.exists():
        if target.read_text(encoding="utf-8") != content:
            raise ValueError(f"sandbox module collision at {target}")
    else:
        source_path.replace(target)
        moved_file = True

    try:
        registered = register_imported_strategy_file(
            module_name=sandbox_module,
            source=source,
            source_id=str(source_path),
            _validated_meta=meta,
            _container_type=type_name,
            _source_ref=str(target),
            _hypothesis_id=hypothesis_id,
            _origin_task_id=origin_task_id,
            _session_id=session_id,
        )
    except Exception:
        if moved_file and target.exists():
            target.replace(source_path)
        raise

    if not moved_file and source_path != target:
        source_path.unlink(missing_ok=True)

    payload = IntakeRegistration(
        strategy_id=str(registered["strategy_id"]),
        module_name=modname,
        type_name=type_name,
        asset=str(registered.get("asset") or "BTC"),
        certified=bool(registered.get("certified")),
        certification_error=registered.get("certification_error"),
        file_name=file_name,
        source=source,
        source_ref=str(target),
        stage=str(registered.get("stage") or "research_only"),
        session_id=session_id,
        lookahead_blocked=bool(registered.get("lookahead_blocked")),
        lookahead_reason=registered.get("certification_error")
        if registered.get("lookahead_blocked")
        else None,
    ).to_dict()
    payload["runtime_type"] = registered.get("runtime_type")
    payload["sandbox_only"] = True
    return payload


def register_custom_strategy_file(
    *,
    file_path: str | None = None,
    module_name: str | None = None,
    source: str = "ai_dropzone",
    hypothesis_id: str | None = None,
    session_id: str | None = None,
    origin_task_id: str | None = None,
) -> dict:
    """Register one custom strategy module for the AI Drop Zone workflow.

    If session_id is provided, it must reference an existing row in
    ai_dropzone_sessions; the strategy row is tagged with it so session
    detail views can surface what was generated during the session.
    """
    from forven.strategies import registry
    from forven.strategies.certification import certify_execution_strategy
    from forven.db import create_strategy_container, get_db, log_activity
    from forven.ai_dropzone_sessions import (
        record_strategy_in_session,
        session_exists,
    )

    clean_session_id = str(session_id or "").strip() or None
    if clean_session_id and not session_exists(clean_session_id):
        raise ValueError(f"Unknown AI Drop Zone session: {clean_session_id}")

    registry.discover(include_custom=False)

    modname, source_ref, file_name = _resolve_targeted_custom_module(
        file_path=file_path,
        module_name=module_name,
    )
    if source == "auto_intake" and not str(hypothesis_id or "").strip():
        inferred_hypothesis_id = _extract_embedded_hypothesis_id(Path(source_ref))
        if not inferred_hypothesis_id:
            raise ValueError(
                f"{file_name} is missing embedded hypothesis_id for auto_intake registration"
            )
        hypothesis_id = inferred_hypothesis_id
    return _register_custom_strategy_sandboxed(
        modname=modname,
        source_ref=source_ref,
        file_name=file_name,
        source=source,
        hypothesis_id=hypothesis_id,
        session_id=clean_session_id,
        origin_task_id=origin_task_id,
    )

    known_types = set(registry._TYPE_MAP.keys())

    # Banned-import gate — reject before we even try to import so that
    # lazy `ta` imports (which don't fail until runtime) are caught.
    banned = _file_uses_banned_imports(Path(source_ref))
    if banned:
        raise ValueError(
            f"{file_name} uses banned imports: {', '.join(banned)}. "
            "These libraries are forbidden — rewrite using native pandas/numpy. "
            "See forven/strategies/STRATEGY_TEMPLATE.md."
        )

    # SECURITY (Lead-2): this file is imported into the live API process below,
    # so its top-level code runs with host privileges (os.environ secrets, the
    # decrypted Fernet key in memory, exchange creds). Run the static AST guard
    # — forbidden imports (os/subprocess/socket/urllib/…), dynamic exec/eval,
    # dunder access — and REJECT before importing. This makes every code-ingress
    # path symmetric with the manual authoring path (api_core.scan_custom_strategy),
    # which already scans; the agent path previously imported on a ruff-pass alone.
    try:
        from forven.sandbox.ast_guard import scan_source

        _scan_report = scan_source(Path(source_ref).read_text(encoding="utf-8"))
    except Exception as exc:  # never import unscanned code if the guard itself fails
        raise ValueError(f"Security scan failed for {file_name}: {exc}") from exc
    if not _scan_report.ok:
        _scan_findings = "; ".join(
            f"line {f.lineno}: {f.message}" for f in _scan_report.findings[:10]
        )
        raise ValueError(
            f"{file_name} rejected by the security scan: {_scan_findings}"
        )

    importlib.invalidate_caches()
    fqn = f"forven.strategies.custom.{modname}"
    try:
        if fqn in _imported_modules():
            import sys

            module = importlib.reload(sys.modules[fqn])
        else:
            module = importlib.import_module(fqn)
    except Exception as exc:
        raise ValueError(f"Import failed for {file_name}: {exc}") from exc

    strategy_cls = getattr(module, "STRATEGY_CLASS", None)
    # A STRATEGY_CLASS declared as the class *name* (a string) is a common codegen
    # slip; resolve it to the actual attribute before falling back.
    if isinstance(strategy_cls, str):
        strategy_cls = getattr(module, strategy_cls, None)
    if not isinstance(strategy_cls, type):
        # Tolerate a module that omits (or mis-declares) the module-level
        # STRATEGY_CLASS but defines exactly one BaseStrategy subclass — matches
        # the tolerant discovery path so anything the app auto-registers can also
        # be re-registered/imported.
        _subclasses = [
            obj
            for obj in vars(module).values()
            if isinstance(obj, type)
            and issubclass(obj, registry.BaseStrategy)
            and obj is not registry.BaseStrategy
            and getattr(obj, "__module__", None) == module.__name__
        ]
        if len(_subclasses) == 1:
            strategy_cls = _subclasses[0]
    type_name = getattr(module, "TYPE_NAME", None)
    if not type_name and strategy_cls is not None:
        # Fall back to a class-level TYPE_NAME when not declared at module level.
        type_name = getattr(strategy_cls, "TYPE_NAME", None)

    if not strategy_cls:
        raise ValueError(f"{file_name} is missing STRATEGY_CLASS")
    if not type_name:
        raise ValueError(f"{file_name} is missing TYPE_NAME")
    if type_name in known_types:
        raise ValueError(f"TYPE_NAME '{type_name}' is already registered")

    validation_errors = registry._registry_type_validation_errors(strategy_cls)
    if validation_errors:
        raise ValueError(f"Class validation failed: {'; '.join(validation_errors)}")

    try:
        probe = strategy_cls("__probe__", {})
        default_params = probe.default_params
        asset = probe.asset if hasattr(probe, "asset") else "BTC"
    except Exception as exc:
        raise ValueError(f"Could not instantiate {file_name}: {exc}") from exc

    cert = certify_execution_strategy(type_name, default_params)
    certified = cert.certified
    cert_error = cert.primary_blocking_reason()

    # GATE B: registration-time lookahead / data-leak probe. A strategy whose
    # vectorized generate_signals reads future bars (e.g. a `.shift(-1)`) is
    # routed to research_only — inert, since the gauntlet backfill only picks up
    # quick_screen/gauntlet — instead of entering the normal funnel toward paper.
    from forven.strategies.lookahead_probe import detect_execution_crash, detect_lookahead

    lookahead_reason = detect_lookahead(probe)
    lookahead_blocked = bool(lookahead_reason)
    if lookahead_blocked:
        # Treat as not-certified for stage/params purposes: research_only + raw
        # params (canonical_params is only meaningful for a certified strategy).
        certified = False
        if not cert_error:
            cert_error = lookahead_reason
        log.warning(
            "Targeted intake: lookahead detected in %s (type=%s) — registering as research_only: %s",
            modname, type_name, lookahead_reason,
        )

    # GATE C: execution smoke probe. A strategy that raises on a clean synthetic
    # run (canonically: a per-bar generate_signal reading self.position, which the
    # engine never injects) would crash every backtest with a cryptic error three
    # gates later. Catch it here and route to research_only with the real reason.
    crash_reason = detect_execution_crash(probe)
    if crash_reason:
        certified = False
        if not cert_error:
            cert_error = crash_reason
        log.warning(
            "Targeted intake: execution smoke test failed for %s (type=%s) — registering as research_only: %s",
            modname, type_name, crash_reason,
        )

    # GATE D: data-availability probe. A strategy that reads enrichment feed
    # columns (funding/liquidations/ls_ratio/...) that don't exist for its
    # target symbol and cannot be auto-downloaded can never produce a trading
    # backtest — every downstream backtest correctly aborts, but the candidate
    # then burns planner/repair cycles as a phantom (S05577, S05838). Park it
    # at birth as research_only with the real reason; fetchable feeds are
    # auto-downloaded by the probe itself, so this only fires on genuinely
    # unavailable data. Internal errors park the candidate in research_only.
    data_block_reason: str | None = None
    try:
        from forven.strategies.data_availability import evaluate_data_availability

        _avail = evaluate_data_availability(
            type_name,
            str(asset).upper(),
            _intended_timeframe(default_params),
            strategy_cls=strategy_cls,
        )
        if _avail is not None and _avail.blocked:
            data_block_reason = _avail.error or "required data feed unavailable"
    except Exception as exc:
        log.warning(
            "Targeted intake: data-availability probe errored for %s (parking): %s",
            modname, exc,
        )
        data_block_reason = f"data-availability check unavailable: {exc}"
    if data_block_reason:
        log.warning(
            "Targeted intake: required data feed unavailable for %s (type=%s) — registering as research_only: %s",
            modname, type_name, data_block_reason,
        )

    initial_stage = (
        "research_only"
        if (lookahead_blocked or crash_reason or data_block_reason)
        else "quick_screen"
    )

    existing_strategy = _find_existing_strategy_container(
        type_name=type_name,
        source_ref=source_ref,
        ignore_terminal=True,
    )
    if existing_strategy:
        existing_id = str(existing_strategy.get("id") or "").strip() or "<unknown>"
        existing_stage = str(existing_strategy.get("stage") or "").strip() or "unknown"
        raise ValueError(
            f"Strategy '{type_name}' is already registered as {existing_id} "
            f"(stage={existing_stage}, still active). A rejected/archived sibling "
            "would not block this — archive the active holder first if you mean "
            "to replace it."
        )

    try:
        registry._load_custom_strategy_module(modname)
    except Exception as exc:
        raise ValueError(f"Registration failed for {file_name}: {exc}") from exc

    stored_params = cert.canonical_params if (certified and not lookahead_blocked) else default_params
    with get_db() as conn:
        strategy_id, _display, _base = create_strategy_container(
            conn=conn,
            name=f"{asset}-{type_name}-intake",
            type_=type_name,
            symbol=str(asset).upper(),
            timeframe=_intended_timeframe(stored_params),
            params=stored_params,
            stage=initial_stage,
            source=source,
            source_ref=source_ref,
            hypothesis_id=hypothesis_id,
            origin_task_id=origin_task_id,
        )
        if clean_session_id:
            record_strategy_in_session(
                conn, session_id=clean_session_id, strategy_id=strategy_id
            )
        if data_block_reason:
            conn.execute(
                "UPDATE strategies SET status_reason = ? WHERE id = ?",
                (f"data_blocked: {data_block_reason[:400]}", strategy_id),
            )

    registration = IntakeRegistration(
        module_name=modname,
        type_name=type_name,
        strategy_id=strategy_id,
        asset=str(asset).upper(),
        certified=certified,
        certification_error=cert_error,
        file_name=file_name,
        source=source,
        source_ref=source_ref,
        stage=initial_stage,
        session_id=clean_session_id,
        lookahead_blocked=lookahead_blocked,
        lookahead_reason=lookahead_reason,
        data_blocked=bool(data_block_reason),
        data_block_reason=data_block_reason,
    )

    log_activity(
        "info",
        "strategy_intake",
        f"Targeted intake: registered {modname} as {strategy_id} from {source}",
        {
            "mode": "register_file",
            "strategy_id": strategy_id,
            "module_name": modname,
            "type_name": type_name,
            "source": source,
            "source_ref": source_ref,
            "stage": initial_stage,
            "session_id": clean_session_id,
            "lookahead_blocked": lookahead_blocked,
            "lookahead_reason": lookahead_reason,
            "data_blocked": bool(data_block_reason),
            "data_block_reason": data_block_reason,
        },
    )

    log.info(
        "Targeted intake: registered %s (type=%s, id=%s, source=%s)",
        modname,
        type_name,
        strategy_id,
        source,
    )
    return registration.to_dict()


def register_imported_strategy_file(
    *,
    module_name: str,
    source: str = "import",
    source_id: str | None = None,
    _validated_meta: dict | None = None,
    _container_type: str | None = None,
    _source_ref: str | None = None,
    _hypothesis_id: str | None = None,
    _origin_task_id: str | None = None,
    _session_id: str | None = None,
) -> dict:
    """Register an UNTRUSTED-ORIGIN (imported / shared) strategy as a SANDBOX-ONLY
    container — WITHOUT importing the author's code into the trusted parent.

    The module must already be written to ``forven/strategies/imported/<module>.py``.
    Validation (import + __init__ + certify + lookahead) runs in the locked-down
    worker subprocess; only JSON metadata returns. The DB row is flagged
    ``sandbox_only=1`` with ``runtime_type=imported__<module>``, so every later
    execution (gauntlet/backtest/scanner) is routed through the worker and the parent
    never imports the module. See the 2026-06 strategy-import security audit (R2).
    """
    from pathlib import Path

    from forven.strategies import imported as imported_pkg
    from forven.strategies.registry import imported_runtime_type
    from forven.sandbox.strategy_worker import (
        validate_custom_module_isolated,
        _reset_worker,
    )
    from forven.db import create_strategy_container, get_db, log_activity

    modname = str(module_name or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", modname):
        raise ValueError(f"invalid imported module name: {module_name!r}")

    imported_dir = Path(imported_pkg.__file__).resolve().parent
    target = imported_dir / f"{modname}.py"
    if not target.exists():
        raise ValueError(f"imported strategy file not found: {target}")

    # Pre-spawn static checks in the parent (SCAN ONLY — never executes the code):
    # banned-import gate + AST guard, so an obviously-unsafe module need not even
    # reach the worker. The worker re-scans before importing (belt-and-suspenders).
    banned = _file_uses_banned_imports(target)
    if banned:
        raise ValueError(
            f"{modname}.py uses banned imports: {', '.join(banned)}. "
            "Use native pandas/numpy."
        )
    from forven.sandbox.ast_guard import scan_source

    report = scan_source(target.read_text(encoding="utf-8"))
    if not report.ok:
        findings = "; ".join(f"line {f.lineno}: {f.message}" for f in report.findings[:5])
        raise ValueError(f"{modname}.py rejected by the security scan: {findings}")

    # Validate OUT-OF-PROCESS — the worker imports + probes; the parent never does.
    meta = dict(_validated_meta) if isinstance(_validated_meta, dict) else validate_custom_module_isolated(
        modname,
        package="imported",
    )
    if not meta.get("ok"):
        raise ValueError(
            f"imported strategy validation failed: {meta.get('error') or 'unknown error'}"
        )

    # Fail-closed on cross-asset / multi-source imports. There is NO parent-side
    # cross-asset enrichment, and the sandbox worker is network/DB-jailed (R3), so a
    # strategy that needs a SECOND asset's series cannot be fed it — it would run on a
    # single-asset frame and silently emit empty/garbage signals. Reject at import
    # rather than register a strategy that can never produce correct signals. (The real
    # class never reaches the parent; the worker captured data_requirements() for us.)
    data_reqs = meta.get("data_requirements")
    if isinstance(data_reqs, list):
        distinct_assets = {
            str(r.get("asset") or "").strip().upper()
            for r in data_reqs
            if isinstance(r, dict) and r.get("asset")
        }
        if len(distinct_assets) > 1:
            raise ValueError(
                f"{modname}.py declares a cross-asset data requirement "
                f"({', '.join(sorted(distinct_assets))}). Cross-asset / multi-source "
                "imported strategies are not supported: the sandbox cannot supply a "
                "second asset's data, so the strategy would run on incomplete data. "
                "Import a single-asset strategy instead."
            )

    runtime_type = imported_runtime_type(modname)
    declared_type = str(meta.get("type_name") or "").strip()
    container_type = str(_container_type or "").strip() or runtime_type
    stored_source_ref = str(_source_ref or "").strip() or str(target)
    existing_strategy = _find_existing_strategy_container(
        type_name=declared_type or container_type,
        source_ref=stored_source_ref,
        ignore_terminal=True,
    )
    if existing_strategy:
        existing_id = str(existing_strategy.get("id") or "").strip() or "<unknown>"
        raise ValueError(f"strategy is already registered as {existing_id}")
    asset = str(meta.get("asset") or "BTC").strip() or "BTC"
    certified = bool(meta.get("certified"))
    cert_error = meta.get("cert_error")
    lookahead_blocked = bool(meta.get("lookahead_blocked"))
    if lookahead_blocked:
        certified = False
        if not cert_error:
            cert_error = meta.get("lookahead_reason")
    # Execution smoke probe ran in the worker (the untrusted class never reaches
    # the parent). A crash on clean synthetic data → research_only with the reason.
    execution_crash_reason = meta.get("execution_crash_reason")
    if execution_crash_reason:
        certified = False
        if not cert_error:
            cert_error = execution_crash_reason
    initial_stage = "research_only" if (lookahead_blocked or execution_crash_reason or not certified) else "quick_screen"
    stored_params = (
        meta.get("canonical_params") if (certified and not lookahead_blocked) else meta.get("default_params")
    ) or {}
    if not isinstance(stored_params, dict):
        stored_params = {}
    # Carry the validated data requirements so the parent-side proxy reports the real
    # (single-source) declaration instead of BaseStrategy's default — otherwise the
    # proxy is silently wrong about what data the strategy needs.
    if isinstance(data_reqs, list) and data_reqs:
        stored_params.setdefault("_data_requirements", data_reqs)
    # Carry the optimizable parameter space so the optimizer can tune the imported
    # strategy (its real parameter_space() is in the absent class). Without this the
    # optimizer returns {} for sandbox-only types and silently never tunes them.
    param_space = meta.get("parameter_space")
    if isinstance(param_space, dict) and param_space:
        stored_params.setdefault("_parameter_space", param_space)

    with get_db() as conn:
        strategy_id, _display, _base = create_strategy_container(
            conn=conn,
            name=f"{asset}-{modname}-import",
            type_=runtime_type,
            symbol=asset.upper(),
            timeframe=_intended_timeframe(stored_params),
            params=stored_params,
            stage=initial_stage,
            source=source,
            source_ref=stored_source_ref,
            hypothesis_id=_hypothesis_id,
            origin_task_id=_origin_task_id,
            sandbox_only=True,
        )
        if container_type != runtime_type:
            conn.execute(
                "UPDATE strategies SET type = ?, runtime_type = ? WHERE id = ?",
                (container_type, runtime_type, strategy_id),
            )
        if _session_id:
            from forven.ai_dropzone_sessions import record_strategy_in_session

            record_strategy_in_session(
                conn,
                session_id=_session_id,
                strategy_id=strategy_id,
            )
        if cert_error:
            conn.execute(
                "UPDATE strategies SET status_reason = ? WHERE id = ?",
                (f"sandbox_validation: {str(cert_error)[:400]}", strategy_id),
            )

    # A running persistent worker discovered imported/ at its startup; force a
    # respawn so the next isolated execution call sees the newly-written module.
    try:
        _reset_worker()
    except Exception:
        pass

    log_activity(
        "info",
        "strategy_intake",
        f"Imported (sandbox-only) {modname} as {strategy_id}",
        {
            "mode": "import_sandbox",
            "strategy_id": strategy_id,
            "module_name": modname,
            "runtime_type": runtime_type,
            "certified": certified,
            "sandbox_only": True,
            "source_id": source_id,
        },
    )
    log.info(
        "Imported (sandbox-only) %s as %s (runtime_type=%s, certified=%s)",
        modname, strategy_id, runtime_type, certified,
    )
    return {
        "strategy_id": strategy_id,
        "module_name": modname,
        "runtime_type": runtime_type,
        "type_name": declared_type or container_type,
        "asset": asset.upper(),
        "certified": certified,
        "certification_error": cert_error,
        "stage": initial_stage,
        "sandbox_only": True,
        "lookahead_blocked": lookahead_blocked,
    }


def auto_intake_recent_files(*, max_age_minutes: int = 10) -> dict:
    """Register only recently-modified custom strategy files.

    Unlike ``scan_custom_strategies(register=True)`` which processes ALL
    files, this only looks at files modified within ``max_age_minutes``.
    Safe to run on a scheduler without risk of bulk-registering hundreds
    of old files after a database reset.
    """
    import time as _time

    from forven.strategies import custom

    custom_dir = Path(custom.__file__).resolve().parent
    cutoff = _time.time() - (max_age_minutes * 60)

    recent_files = []
    for f in custom_dir.iterdir():
        if f.suffix == ".py" and f.name != "__init__.py" and f.stat().st_mtime >= cutoff:
            recent_files.append(f)

    if not recent_files:
        return {"registered": 0, "checked": 0, "errors": []}

    registered = 0
    errors = []
    for f in recent_files:
        try:
            result = register_custom_strategy_file(file_path=str(f), source="auto_intake")
            registered += 1
            log.info("Auto-intake: registered %s as %s", f.name, result.get("strategy_id"))
        except ValueError as exc:
            # Already registered or validation failure — expected, skip
            if "already registered" not in str(exc).lower():
                errors.append({"file": f.name, "error": str(exc)})
        except Exception as exc:
            errors.append({"file": f.name, "error": str(exc)})

    return {"registered": registered, "checked": len(recent_files), "errors": errors}


def get_recent_intake_events(limit: int = 20) -> dict:
    """Return recently ingested strategies from DB activity log."""
    try:
        from forven.db import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM activity_log WHERE source = 'strategy_intake' ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            events = []
            for row in rows:
                entry = dict(row)
                if entry.get("data"):
                    try:
                        entry["data"] = json.loads(entry["data"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                events.append(entry)

            # Intake registers new AI Drop Zone strategies at quick_screen (certified)
            # or research_only (failed certification). Include both so the UI surfaces
            # what was just ingested, not strategies that were later promoted.
            strat_rows = conn.execute(
                "SELECT id, name, type, symbol, timeframe, status, stage, source, created_at "
                "FROM strategies WHERE stage IN ('quick_screen', 'research_only') "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            strategies = [dict(r) for r in strat_rows]

            return {"events": events, "strategies": strategies}
    except Exception as exc:
        log.warning("Failed to fetch recent intake events: %s", exc)
        return {"events": [], "strategies": []}


def _imported_modules() -> set[str]:
    """Return the set of currently imported module names."""
    import sys
    return set(sys.modules.keys())


def _resolve_targeted_custom_module(
    *,
    file_path: str | None,
    module_name: str | None,
) -> tuple[str, str, str]:
    from forven.strategies import custom

    raw_path = str(file_path or "").strip()
    raw_module = str(module_name or "").strip()
    if bool(raw_path) == bool(raw_module):
        raise ValueError("Provide exactly one of file_path or module_name")

    custom_dir = Path(custom.__file__).resolve().parent

    if raw_path:
        target_path = Path(raw_path).expanduser().resolve()
        if not target_path.exists() or not target_path.is_file():
            raise ValueError(f"Strategy file not found: {target_path}")
        if target_path.suffix.lower() != ".py":
            raise ValueError("Strategy file must be a .py module")
        if target_path.name == "__init__.py":
            raise ValueError("__init__.py is not a strategy module")
        try:
            target_path.relative_to(custom_dir)
        except ValueError as exc:
            raise ValueError(f"Strategy file must live under {custom_dir}") from exc
        return target_path.stem, str(target_path), target_path.name

    normalized_module = raw_module
    if normalized_module.startswith("forven.strategies.custom."):
        normalized_module = normalized_module.split(".")[-1]
    if not normalized_module or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in normalized_module):
        raise ValueError(f"Invalid module name: {raw_module}")

    target_path = custom_dir / f"{normalized_module}.py"
    if not target_path.exists():
        raise ValueError(f"Strategy module not found: {target_path}")
    return normalized_module, raw_module, target_path.name


def _find_existing_strategy_container(
    *,
    type_name: str,
    source_ref: str | None = None,
    ignore_terminal: bool = False,
) -> dict[str, object] | None:
    """Find a strategy container already holding this carrier type name.

    ``ignore_terminal=True`` (NAMESPACE-1, targeted registration): a
    rejected/archived/trashed holder does NOT consume the name — one failed
    sibling used to make a novel-composite carrier permanently single-slot
    (S06020 blocked every microprice_drift_lead retry). The bulk discovery
    sweep keeps the default (terminal holders count as known — it must not
    re-mint containers for archived strategies)."""
    from forven.db import get_db

    normalized_type = str(type_name or "").strip().lower()
    if not normalized_type:
        return None

    clauses = [
        "LOWER(TRIM(COALESCE(type, ''))) = ?",
        "LOWER(TRIM(COALESCE(runtime_type, ''))) = ?",
    ]
    params: list[str] = [normalized_type, normalized_type]

    normalized_source_ref = str(source_ref or "").strip().lower()
    if normalized_source_ref:
        clauses.append("LOWER(TRIM(COALESCE(source_ref, ''))) = ?")
        params.append(normalized_source_ref)

        source_name = Path(normalized_source_ref).name.strip().lower()
        if source_name and source_name != normalized_source_ref:
            clauses.append("LOWER(TRIM(COALESCE(source_ref, ''))) = ?")
            params.append(source_name)

    terminal_filter = ""
    if ignore_terminal:
        terminal_filter = (
            " AND LOWER(TRIM(COALESCE(stage, ''))) NOT IN "
            "('rejected', 'archived', 'backtest_failed', 'trash')"
        )

    query = (
        "SELECT id, type, runtime_type, source_ref, stage, created_at "
        f"FROM strategies WHERE ({' OR '.join(clauses)}){terminal_filter} "
        "ORDER BY created_at DESC LIMIT 1"
    )

    with get_db() as conn:
        row = conn.execute(query, tuple(params)).fetchone()
    return dict(row) if row else None
