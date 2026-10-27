"""A strategy's saved source_ref is an absolute path from the machine that created it.
After the data folder moves to another OS or checkout (Windows ``C:\\Users\\...`` paths
read on Linux, on 2026-10-26), active strategies must still register and pass the
execution identity check instead of being blocked as "runtime type not registered" /
"Strategy source reference is unavailable or invalid"."""

import importlib
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import forven.strategies.custom as custom_pkg
import forven.strategies.registry as reg
from forven.db import get_db
from forven.strategies.identity import execution_identity_error, resolve_source_ref, source_ref_stem

MODNAME = "zz_portable_ref_s99992"
TYPE_NAME = "zz_portable_ref_type"
WINDOWS_REF = rf"C:\Users\Someone\Projects\forven\forven\strategies\custom\{MODNAME}.py"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_strategy() -> Path:
    src = Path(custom_pkg.__file__).resolve().parent / f"{MODNAME}.py"
    src.write_text(textwrap.dedent(f'''
        from forven.strategies.base import BaseStrategy, Signal

        TYPE_NAME = "{TYPE_NAME}"

        class ZZPortableRef(BaseStrategy):
            @property
            def name(self):
                return "zz-portable-ref"
            @property
            def asset(self):
                return self.params.get("_asset", "BTC")
            @property
            def strategy_type(self):
                return TYPE_NAME
            @property
            def default_params(self):
                return {{"_asset": "BTC"}}
            def generate_signal(self, df):
                return Signal()

        STRATEGY_CLASS = ZZPortableRef
    '''), encoding="utf-8")
    importlib.invalidate_caches()
    return src


def test_resolve_source_ref_rebases_foreign_paths():
    src = _write_strategy()
    try:
        assert source_ref_stem(WINDOWS_REF) == MODNAME
        assert resolve_source_ref(WINDOWS_REF) == src
        assert resolve_source_ref(f"/srv/other/forven/strategies/custom/{MODNAME}.py") == src
        assert resolve_source_ref(str(src)) == src
        assert resolve_source_ref(r"C:\elsewhere\custom\missing_strategy.py") is None
        assert resolve_source_ref(rf"C:\elsewhere\notes\{MODNAME}.py") is None
        assert resolve_source_ref("") is None
    finally:
        src.unlink(missing_ok=True)


def test_active_strategy_with_windows_source_ref_registers_and_passes_identity(forven_db):
    src = _write_strategy()
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, "
                "status, owner, stage, stage_changed_at, created_at, updated_at, source_ref) "
                "VALUES (?, ?, ?, 'BTC', '1h', '{}', '{}', 'paper', 'brain', 'paper', ?, ?, ?, ?)",
                ("S-PORTABLE", "zz", TYPE_NAME, _now(), _now(), _now(), WINDOWS_REF),
            )
            conn.commit()

        reg._TYPE_MAP.pop(TYPE_NAME, None)
        reg._ensure_active_db_strategy_modules()
        assert TYPE_NAME in reg._TYPE_MAP

        row = {"source_ref": WINDOWS_REF, "metrics": "{}"}
        assert execution_identity_error(row, TYPE_NAME) is None
    finally:
        reg._TYPE_MAP.pop(TYPE_NAME, None)
        reg._FAILED_CUSTOM_MODULES.discard(MODNAME)
        src.unlink(missing_ok=True)
