"""Source identity checks shared by evidence producers and execution."""

import ast
import hashlib
import inspect
import json
from pathlib import Path


def source_identity(runtime_type: str, cls: type | None = None) -> dict:
    """Hash the exact registered module, never a fuzzy type-name match."""
    if runtime_type.startswith("imported__"):
        from forven.strategies.registry import imported_module_exists

        if not imported_module_exists(runtime_type):
            return {}
        module = runtime_type.removeprefix("imported__")
        try:
            digest = hashlib.sha256((Path(__file__).parent / "imported" / f"{module}.py").read_bytes()).hexdigest()
            return {"runtime_type": runtime_type, "module": f"forven.strategies.imported.{module}",
                    "source_sha256": digest, "loaded_source_sha256": digest}
        except OSError:
            return {}
    if cls is None:
        from forven.strategies.registry import _TYPE_MAP

        cls = _TYPE_MAP.get(runtime_type)
    try:
        filename = inspect.getsourcefile(cls) if cls is not None else None
        if not filename:
            return {}
        path = Path(filename)
        current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"runtime_type": runtime_type, "module": cls.__module__,
                "source_sha256": current_hash,
                "loaded_source_sha256": cls.__dict__.get("_forven_source_sha256", current_hash)}
    except (OSError, TypeError):
        return {}


def execution_identity_error(row: dict, runtime_type: str) -> str | None:
    """Refuse new exposure when runtime conflicts with source or saved evidence."""
    if runtime_type.startswith("imported__"):
        return None  # Sandbox module names are a separate, exact identity namespace.
    current = source_identity(runtime_type)
    if current and current.get("loaded_source_sha256", current["source_sha256"]) != current["source_sha256"]:
        return "Strategy module changed after loading; reload and revalidate before new entries"
    source = str(row.get("source_ref") or "")
    if source.lower().endswith(".py"):
        try:
            tree = ast.parse(Path(source).read_text(encoding="utf-8-sig"))
            declared = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "TYPE_NAME" for t in node.targets)
                        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
            if declared and runtime_type not in declared:
                return f"Runtime '{runtime_type}' differs from source TYPE_NAME '{declared[0]}'; revalidation required"
            if current and current["source_sha256"] != hashlib.sha256(Path(source).read_bytes()).hexdigest():
                return "Registered implementation differs from the strategy source reference; revalidation required"
        except (OSError, SyntaxError, UnicodeError):
            return "Strategy source reference is unavailable or invalid; revalidation required"
    metrics = row.get("metrics") or {}
    if isinstance(metrics, str):
        try:
            metrics = json.loads(metrics)
        except (TypeError, ValueError):
            metrics = {}
    accepted = metrics.get("execution_identity") if isinstance(metrics, dict) else None
    if accepted and (not current or accepted != current):
        return "Strategy source changed since its recorded validation; revalidation required"
    return None


def strategy_source_identity(strategy_id: str) -> dict:
    """Capture the registered implementation when a validation job is submitted."""
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute("SELECT type, runtime_type FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    return source_identity(str(row["runtime_type"] or row["type"] or "")) if row else {}


def stale_source_identity(config: dict) -> bool:
    captured = config.get("execution_identity")
    return bool(captured and captured != source_identity(str(captured.get("runtime_type") or "")))
