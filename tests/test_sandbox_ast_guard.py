"""Tests for forven.sandbox.ast_guard (P2-T02).

Covers the 10 acceptance points from plan.md:
clean strategy passes; `import os` blocked; `from os import path` blocked;
`__import__('os')` blocked; top-level `exec(code)` blocked; oversized file
blocked; >1500-line file blocked; syntax error reported gracefully; multi-
issue file reports all findings, not just the first; `scan_source('')`
returns `ok=True`, empty findings.
"""
from __future__ import annotations

import textwrap
from pathlib import Path


from forven.sandbox.ast_guard import (
    MAX_FILE_BYTES,
    MAX_LINES,
    AstReport,
    Finding,
    scan_file,
    scan_source,
)


CLEAN_STRATEGY = textwrap.dedent(
    """
    import math
    import numpy as np
    import pandas as pd
    from forven.market_data_view import get_ohlcv

    def generate_signal(symbol: str, timeframe: str) -> dict:
        df = get_ohlcv(symbol, timeframe, bars=200)
        rsi = 100 - 100 / (1 + df['close'].pct_change().rolling(14).mean())
        if rsi.iloc[-1] < 30:
            return {"action": "buy", "confidence": 0.7}
        return {"action": "hold", "confidence": 0.0}
    """
).strip()


def test_clean_strategy_passes():
    report = scan_source(CLEAN_STRATEGY)
    assert isinstance(report, AstReport)
    assert report.ok is True
    assert report.findings == []


def test_import_os_blocked():
    report = scan_source("import os\n")
    assert report.ok is False
    kinds = [f.kind for f in report.findings]
    assert "forbidden_import" in kinds
    assert any("os" in f.message for f in report.findings)


def test_from_os_import_path_blocked():
    report = scan_source("from os import path\n")
    assert report.ok is False
    assert any(f.kind == "forbidden_import" for f in report.findings)


def test_dunder_import_blocked():
    report = scan_source("x = __import__('os')\n")
    assert report.ok is False
    assert any(f.kind == "dynamic_exec" for f in report.findings)
    assert any("__import__" in f.message for f in report.findings)


def test_dunder_import_of_safe_constant_module_allowed():
    # __import__("pandas") is exactly equivalent to `import pandas`, which is
    # allowed — a common codegen idiom. It must NOT be flagged.
    assert scan_source('df = __import__("pandas").DataFrame()\n').ok is True
    assert scan_source("s = __import__('numpy').array([1])\n").ok is True


def test_dunder_import_of_forbidden_constant_still_blocked():
    for mod in ("os", "socket", "ctypes", "subprocess"):
        report = scan_source(f"x = __import__({mod!r})\n")
        assert report.ok is False, f"__import__({mod!r}) should be blocked"


def test_dunder_import_of_nonconstant_argument_blocked():
    # The real obfuscation primitive: a dynamic (non-constant) module name.
    report = scan_source("m = 'o' + 's'\nx = __import__(m)\n")
    assert report.ok is False
    assert any("__import__" in f.message for f in report.findings)


def test_top_level_exec_blocked():
    report = scan_source("exec('print(1)')\n")
    assert report.ok is False
    assert any(f.kind == "dynamic_exec" for f in report.findings)


def test_eval_blocked():
    report = scan_source("y = eval('1+1')\n")
    assert report.ok is False
    assert any(f.kind == "dynamic_exec" for f in report.findings)


def test_compile_blocked():
    report = scan_source("c = compile('1', '<s>', 'eval')\n")
    assert report.ok is False
    assert any(f.kind == "dynamic_exec" for f in report.findings)


def test_getattr_builtins_blocked():
    report = scan_source("ev = getattr(__builtins__, 'eval')\n")
    assert report.ok is False
    assert any(f.kind == "dynamic_exec" for f in report.findings)


def test_subprocess_import_blocked():
    report = scan_source("import subprocess\n")
    assert report.ok is False
    assert any(f.kind == "forbidden_import" for f in report.findings)


def test_socket_import_blocked():
    report = scan_source("import socket\n")
    assert report.ok is False


def test_urllib_submodule_blocked():
    """`urllib.request` resolves to top-level `urllib`, which is forbidden."""
    report = scan_source("import urllib.request\n")
    assert report.ok is False
    assert any(f.kind == "forbidden_import" for f in report.findings)


def test_oversized_file_blocked():
    """File-size cap is enforced before parsing."""
    big_source = "x = 1\n"
    report = scan_source(big_source, file_size_bytes=MAX_FILE_BYTES + 1)
    assert report.ok is False
    assert any(f.kind == "file_too_large" for f in report.findings)


def test_too_many_lines_blocked():
    source = "\n".join(["x = 1"] * (MAX_LINES + 5)) + "\n"
    report = scan_source(source)
    assert report.ok is False
    assert any(f.kind == "too_many_lines" for f in report.findings)


def test_syntax_error_reported_gracefully():
    """Bad syntax must NOT raise — it's reported as a finding."""
    report = scan_source("def broken(:\n    pass\n")
    assert report.ok is False
    assert any(f.kind == "syntax_error" for f in report.findings)


def test_multi_issue_file_reports_all_findings():
    """The visitor must record ALL violations in a single pass."""
    source = textwrap.dedent(
        """
        import os
        import subprocess
        import socket
        x = eval('1+1')
        y = exec('print(2)')
        z = __import__('ctypes')
        """
    ).strip() + "\n"
    report = scan_source(source)
    assert report.ok is False
    # 3 forbidden imports + 3 dynamic_exec findings minimum
    forbidden_imports = [f for f in report.findings if f.kind == "forbidden_import"]
    dyn_exec = [f for f in report.findings if f.kind == "dynamic_exec"]
    assert len(forbidden_imports) >= 3
    assert len(dyn_exec) >= 3


def test_empty_source_returns_ok():
    report = scan_source("")
    assert report.ok is True
    assert report.findings == []
    assert report.line_count == 0


def test_findings_have_line_numbers():
    """Findings must carry useful line/col info for debugging."""
    source = "x = 1\nimport os\n"
    report = scan_source(source)
    forbidden = [f for f in report.findings if f.kind == "forbidden_import"]
    assert len(forbidden) == 1
    assert forbidden[0].lineno == 2


def test_scan_file_round_trip(tmp_path: Path):
    p = tmp_path / "strat.py"
    p.write_text("import os\n", encoding="utf-8")
    report = scan_file(p)
    assert report.ok is False
    assert any(f.kind == "forbidden_import" for f in report.findings)
    assert report.file_size_bytes > 0


def test_scan_file_rejects_non_utf8_bytes(tmp_path: Path):
    """Non-UTF-8 source is REJECTED (fail-closed), never silently re-decoded.

    The old guard fell back to latin-1, scanning a *different* program than the one
    CPython would actually compile (a non-UTF-8 file without a coding cookie is a
    SyntaxError at import). The hardened guard refuses it outright rather than
    reinterpreting the bytes (audit R4)."""
    p = tmp_path / "strat_latin1.py"
    # 0xff is invalid UTF-8 but valid latin-1
    p.write_bytes(b"# header \xff\nimport os\n")
    report = scan_file(p)
    assert report.ok is False
    assert any(f.kind == "forbidden_encoding" for f in report.findings)


def test_scan_file_rejects_utf7_coding_cookie(tmp_path: Path):
    """A PEP 263 coding cookie that is not utf-8/ascii is rejected at the byte
    level — the scan/compile encoding differential was a confirmed in-process RCE."""
    p = tmp_path / "strat_cookie.py"
    p.write_bytes(b"# coding: utf-7\nimport pandas as pd\n")
    report = scan_file(p)
    assert report.ok is False
    assert any(f.kind == "forbidden_encoding" for f in report.findings)


def test_clean_strategy_via_scan_file(tmp_path: Path):
    p = tmp_path / "clean.py"
    p.write_text(CLEAN_STRATEGY, encoding="utf-8")
    report = scan_file(p)
    assert report.ok is True
    assert report.findings == []


def test_finding_dataclass_shape():
    f = Finding(
        kind="forbidden_import",
        lineno=1,
        col=0,
        message="msg",
        node_repr="repr",
    )
    assert f.kind == "forbidden_import"
    assert f.lineno == 1
