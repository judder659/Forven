"""C-1 regression: custom strategy modules are AST-scanned BEFORE the in-process
import in the runtime loader (registry.assert_custom_module_safe). A module with
a forbidden top-level import / exec must raise (so discover() skips it) and must
never be imported into the live process.
"""
from __future__ import annotations

import sys

import pytest

from forven.strategies import custom as custom_pkg
from forven.strategies import registry


def _point_custom_dir(monkeypatch, tmp_path):
    d = tmp_path / "custom"
    d.mkdir()
    monkeypatch.setattr(custom_pkg, "__path__", [str(d)])
    monkeypatch.setattr(custom_pkg, "__file__", str(d / "__init__.py"))
    return d


_SAFE = "import pandas as pd\nTYPE_NAME = 'x'\n"
_FORBIDDEN_IMPORT = "import os\nimport pandas as pd\nTYPE_NAME = 'x'\n"
_DYNEXEC = "import pandas as pd\nx = eval('1+1')\nTYPE_NAME = 'x'\n"
_SAFE_DUNDER = 'df = __import__("pandas")\nTYPE_NAME = "x"\n'


def test_safe_module_passes(monkeypatch, tmp_path):
    d = _point_custom_dir(monkeypatch, tmp_path)
    (d / "safe_mod.py").write_text(_SAFE, encoding="utf-8")
    registry.assert_custom_module_safe("safe_mod")  # must not raise


def test_safe_constant_dunder_import_passes(monkeypatch, tmp_path):
    d = _point_custom_dir(monkeypatch, tmp_path)
    (d / "dunder_mod.py").write_text(_SAFE_DUNDER, encoding="utf-8")
    registry.assert_custom_module_safe("dunder_mod")  # must not raise


def test_forbidden_import_raises_and_is_not_imported(monkeypatch, tmp_path):
    d = _point_custom_dir(monkeypatch, tmp_path)
    (d / "evil_os_mod.py").write_text(_FORBIDDEN_IMPORT, encoding="utf-8")
    with pytest.raises(ImportError) as exc:
        registry.assert_custom_module_safe("evil_os_mod")
    assert "security guard" in str(exc.value).lower()
    assert "forven.strategies.custom.evil_os_mod" not in sys.modules


def test_dynamic_exec_raises(monkeypatch, tmp_path):
    d = _point_custom_dir(monkeypatch, tmp_path)
    (d / "evil_eval_mod.py").write_text(_DYNEXEC, encoding="utf-8")
    with pytest.raises(ImportError):
        registry.assert_custom_module_safe("evil_eval_mod")


def test_missing_file_is_noop(monkeypatch, tmp_path):
    _point_custom_dir(monkeypatch, tmp_path)
    # No file on disk (namespace/pkg style) — nothing to scan, must not raise.
    registry.assert_custom_module_safe("does_not_exist")


def _count_scans(monkeypatch):
    """Wrap scan_source so we can count how many times it actually parses."""
    import forven.sandbox.ast_guard as guard

    calls = {"n": 0}
    real_scan = guard.scan_source

    def _spy(src, *args, **kwargs):
        calls["n"] += 1
        return real_scan(src, *args, **kwargs)

    monkeypatch.setattr("forven.sandbox.ast_guard.scan_source", _spy)
    return calls


def test_unchanged_module_is_scanned_once(monkeypatch, tmp_path):
    """Re-entry (every backtest/optimization) must hit the verdict cache, not
    re-read + re-parse the unchanged source — the ~16% CPU win."""
    d = _point_custom_dir(monkeypatch, tmp_path)
    (d / "cached_mod.py").write_text(_SAFE, encoding="utf-8")
    registry._SCAN_VERDICT_CACHE.clear()
    calls = _count_scans(monkeypatch)

    for _ in range(3):
        registry.assert_custom_module_safe("cached_mod")  # must not raise
    assert calls["n"] == 1  # scanned once, then served from cache


def test_file_edit_invalidates_cache(monkeypatch, tmp_path):
    """A changed file (different mtime/size) must force a fresh scan so the
    security guarantee is preserved."""
    d = _point_custom_dir(monkeypatch, tmp_path)
    f = d / "edit_mod.py"
    f.write_text(_SAFE, encoding="utf-8")
    registry._SCAN_VERDICT_CACHE.clear()
    calls = _count_scans(monkeypatch)

    registry.assert_custom_module_safe("edit_mod")
    f.write_text(_SAFE + "# changed (different size)\n", encoding="utf-8")
    registry.assert_custom_module_safe("edit_mod")
    assert calls["n"] == 2


def test_cached_rejection_still_raises(monkeypatch, tmp_path):
    """A rejected module served from the cache must still raise — the cache
    never weakens the guard."""
    d = _point_custom_dir(monkeypatch, tmp_path)
    (d / "evil_cached.py").write_text(_FORBIDDEN_IMPORT, encoding="utf-8")
    registry._SCAN_VERDICT_CACHE.clear()

    with pytest.raises(ImportError):
        registry.assert_custom_module_safe("evil_cached")
    with pytest.raises(ImportError):  # served from cached (rejected) verdict
        registry.assert_custom_module_safe("evil_cached")
