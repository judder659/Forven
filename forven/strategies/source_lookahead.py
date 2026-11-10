"""Static check for strategy code that reads the next bar.

The right-truncation probe (lookahead_probe) only sees a leak when the strategy
fires on its synthetic walks, and a vectorized ``generate_signals`` can peek
ahead while the per-bar ``generate_signal`` the probe also calls cannot. S20977
built half its entries from ``close.shift(-1)`` and passed; S21756 never fired
on the synthetic data, was marked "unverifiable" and reached paper with a
``shift(-1)`` confirmation (2026-10-29, 11-01). ``.shift(-N)`` in strategy code
means "the value from N bars later", so finding one in the source is evidence
enough. Pure ``ast`` reading: the author's code is never imported or run.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _is_negative(node: ast.AST | None) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        operand = node.operand
        # -0 is not a look ahead; any other negated number or name is.
        return not (isinstance(operand, ast.Constant) and operand.value == 0)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value < 0
    return False


def negative_shift_lines(source: str) -> list[int]:
    """Line numbers of every ``.shift(-N)`` / ``.shift(periods=-N)`` call."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "shift"):
            continue
        periods = node.args[0] if node.args else next(
            (kw.value for kw in node.keywords if kw.arg == "periods"), None
        )
        if _is_negative(periods):
            lines.append(int(node.lineno))
    return sorted(set(lines))


def file_negative_shift_lines(path: Path) -> list[int]:
    try:
        return negative_shift_lines(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def negative_shift_reason(lines: list[int]) -> str | None:
    if not lines:
        return None
    shown = ", ".join(str(n) for n in lines[:5])
    return (
        f"reads future bars: .shift(-N) takes the value from a later bar (line {shown}). "
        "Use .shift(N) with N > 0, or act on the bar after the signal."
    )


def imported_runtime_negative_shift_reason(runtime_type: object) -> str | None:
    """The look-ahead reason for an imported strategy's module file, or None."""
    from forven.strategies import imported as imported_pkg
    from forven.strategies.registry import IMPORTED_TYPE_PREFIX

    name = str(runtime_type or "").strip()
    if not name.startswith(IMPORTED_TYPE_PREFIX):
        return None
    module = name[len(IMPORTED_TYPE_PREFIX):]
    if not module or not all(ch.isalnum() or ch == "_" for ch in module):
        return None
    path = Path(imported_pkg.__file__).resolve().parent / f"{module}.py"
    if not path.is_file():
        return None
    return negative_shift_reason(file_negative_shift_lines(path))
