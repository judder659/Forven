"""Source identity checks shared by evidence producers and execution."""

import ast
import hashlib
import importlib
import inspect
import json
from pathlib import Path, PurePosixPath, PureWindowsPath

_SOURCE_SUBDIRS = ("custom", "imported", "builtin")


def source_ref_stem(source_ref: str) -> str:
    """Module stem of a source path saved on any OS (``C:\\...\\x.py`` -> ``x``)."""
    return PureWindowsPath(str(source_ref or "").strip()).stem


def resolve_source_ref(source_ref: str) -> Path | None:
    """Find a strategy source file, even when its saved path came from another machine.

    ``source_ref`` is an absolute path recorded where the strategy was created. After
    the data folder moves (Windows to Linux, or a different checkout), that path no
    longer exists, but the file usually does under this install's
    ``forven/strategies/<custom|imported|builtin>/``. Re-base onto it by file name.
    """
    ref = str(source_ref or "").strip()
    if not ref:
        return None
    path = Path(ref).expanduser()
    if path.is_file():
        return path
    foreign = PureWindowsPath(ref) if ("\\" in ref or PureWindowsPath(ref).drive) else PurePosixPath(ref)
    parts = foreign.parts
    if len(parts) < 2 or parts[-2] not in _SOURCE_SUBDIRS:
        return None
    candidate = Path(__file__).resolve().parent / parts[-2] / parts[-1]
    return candidate if candidate.is_file() else None


def source_identity(runtime_type: str, cls: type | None = None) -> dict:
    """Hash the exact registered module, never a fuzzy type-name match."""
    if runtime_type.startswith("imported__"):
        imported_module_exists = importlib.import_module("forven.strategies.registry").imported_module_exists

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
        _TYPE_MAP = importlib.import_module("forven.strategies.registry")._TYPE_MAP
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
        source_path = resolve_source_ref(source)
        try:
            if source_path is None:
                raise OSError(source)
            tree = ast.parse(source_path.read_text(encoding="utf-8-sig"))
            declared = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "TYPE_NAME" for t in node.targets)
                        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
            if declared and runtime_type not in declared:
                return f"Runtime '{runtime_type}' differs from source TYPE_NAME '{declared[0]}'; revalidation required"
            if current and current["source_sha256"] != hashlib.sha256(source_path.read_bytes()).hexdigest():
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
    with importlib.import_module("forven.db").get_db() as conn:
        row = conn.execute("SELECT type, runtime_type FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    return source_identity(str(row["runtime_type"] or row["type"] or "")) if row else {}


def stale_source_identity(config: dict) -> bool:
    captured = config.get("execution_identity")
    return bool(captured and captured != source_identity(str(captured.get("runtime_type") or "")))
