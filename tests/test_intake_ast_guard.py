"""Lead-2 (part A): every custom-code ingress path must run the AST guard before
importing the module in-process. The agent path previously imported on a ruff
pass alone — asymmetric with the manual authoring path which scans. The guard
now lives in intake.register_custom_strategy_file, the shared chokepoint.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

from forven.strategies import custom as custom_pkg
from forven.strategies import intake as intake_mod
from forven.strategies import registry

_CLEAN = """\
import pandas as pd
from forven.strategies.base import BaseStrategy, Signal


class GuardOkStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return 'Guard OK'

    @property
    def asset(self) -> str:
        return 'BTC'

    @property
    def strategy_type(self) -> str:
        return TYPE_NAME

    @property
    def default_params(self) -> dict:
        return {'risk_pct': 0.01, 'leverage': 1.0}

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        price = float(df['close'].iloc[-1]) if 'close' in df and len(df.index) else 0.0
        return Signal(price=price)


STRATEGY_CLASS = GuardOkStrategy
TYPE_NAME = 'guard_ok_test'
"""

# Same clean body, but with an exfiltration primitive injected at top level.
_MALICIOUS = _CLEAN.replace(
    "import pandas as pd\n",
    "import pandas as pd\nimport os\nimport socket\n",
).replace("guard_ok_test", "guard_evil_test").replace(
    "GuardOkStrategy", "GuardEvilStrategy"
).replace("'Guard OK'", "'Guard Evil'")


def _point_custom_dir(monkeypatch, tmp_path):
    d = tmp_path / "custom"
    d.mkdir()
    monkeypatch.setattr(custom_pkg, "__path__", [str(d)])
    monkeypatch.setattr(custom_pkg, "__file__", str(d / "__init__.py"))
    registry.reset()
    importlib.invalidate_caches()
    return d


def test_malicious_top_level_import_is_rejected_before_import(forven_db, monkeypatch, tmp_path):
    d = _point_custom_dir(monkeypatch, tmp_path)
    f = d / "btc_guard_evil_test.py"
    f.write_text(_MALICIOUS, encoding="utf-8")
    sys.modules.pop("forven.strategies.custom.btc_guard_evil_test", None)

    # registry.assert_custom_module_safe raises ImportError (not ValueError) and
    # intake does not wrap it. NOTE: routers/strategies.py catches only ValueError
    # around this call, so via the HTTP ingress this surfaces as a 500 rather than
    # a 400 carrying the security reason.
    with pytest.raises(ImportError) as exc:
        intake_mod.register_custom_strategy_file(file_path=str(f))
    msg = str(exc.value).lower()
    assert "ast security guard" in msg
    assert "'os'" in msg and "'socket'" in msg      # names the offending imports
    # The security property under test: NOT imported in-process.
    assert "forven.strategies.custom.btc_guard_evil_test" not in sys.modules


def test_clean_strategy_still_registers(forven_db):
    # This path spawns the sandbox validator SUBPROCESS, which re-imports
    # forven.strategies.custom from scratch — a monkeypatched __path__ in the
    # parent is invisible to it ("No module named
    # forven.strategies.custom.btc_guard_ok_test"). So the module has to live in
    # the real package dir for the duration of the test.
    real_dir = pathlib.Path(custom_pkg.__file__).parent
    f = real_dir / "btc_guard_ok_test.py"
    modname = "forven.strategies.custom.btc_guard_ok_test"
    f.write_text(_CLEAN, encoding="utf-8")
    sys.modules.pop(modname, None)
    registry.reset()
    importlib.invalidate_caches()
    try:
        result = intake_mod.register_custom_strategy_file(file_path=str(f))
        assert result["strategy_id"]
        assert result["module_name"] == "btc_guard_ok_test"
    finally:
        f.unlink(missing_ok=True)
        sys.modules.pop(modname, None)
        registry.reset()
        importlib.invalidate_caches()
