"""Untrusted-origin (imported__*) strategies and the parent-side gates.

The registry deliberately resolves imported types to themselves and executes
them via the sandbox worker proxy — but the scanner load gate and the
PHANTOM-1 certification gate only accepted `_TYPE_MAP` classes, silently
quarantining every dropzone import from paper execution (S06898 / S07678 /
S07689, found 2026-07-21). These tests pin the fix: an imported type whose
module file exists certifies and loads; a fabricated imported name still
fails closed.
"""

from pathlib import Path

from forven.strategies import registry
from forven.strategies.certification import certify_execution_strategy
from forven.strategies.params import is_known_runtime_type


def _imported_dir() -> Path:
    return Path(registry.__file__).resolve().parent / "imported"


def test_imported_type_with_module_file_certifies():
    name = "test_imported_gate_probe_c4f2a"
    probe = _imported_dir() / f"{name}.py"
    probe.write_text("# gate-probe artifact; never imported by the parent\n", encoding="utf-8")
    try:
        rt = f"imported__{name}"
        assert registry.imported_module_exists(rt) is True
        # Certification mode (PHANTOM-1 gate) must accept it: its concrete
        # runtime is the sandbox worker proxy.
        assert is_known_runtime_type(rt, require_runtime_class=True) is True
        cert = certify_execution_strategy(rt, {})
        assert cert.unregistered_runtime_type is False
    finally:
        probe.unlink()


def test_fabricated_imported_type_still_fails_closed():
    rt = "imported__no_such_module_phantom_guard"
    assert registry.imported_module_exists(rt) is False
    assert is_known_runtime_type(rt, require_runtime_class=True) is False
    cert = certify_execution_strategy(rt, {})
    assert cert.unregistered_runtime_type is True
    assert cert.certified is False


def test_traversal_shaped_imported_names_are_refused():
    assert registry.imported_module_exists("imported__../evil") is False
    assert registry.imported_module_exists("imported__a.b") is False
    assert registry.imported_module_exists("imported__") is False
    assert registry.imported_module_exists("not_imported_at_all") is False
