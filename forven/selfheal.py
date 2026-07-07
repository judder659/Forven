"""Self-healing code validator: lint, fix, and test agent-generated code."""

import logging

from forven.sandbox import lint_code, run_code
from forven.sandbox.ast_guard import scan_source

log = logging.getLogger("forven.selfheal")

MAX_FIX_ROUNDS = 3


def normalize_generated_strategy_code(code: str) -> str:
    """Normalize agent-generated strategy modules before lint/import checks."""
    normalized = str(code or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    future_imports: list[str] = []
    body_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("from __future__ import "):
            if stripped not in future_imports:
                future_imports.append(stripped)
            continue
        body_lines.append(line)

    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)

    if not future_imports:
        return normalized

    body = "\n".join(body_lines).rstrip()
    return "\n".join([*future_imports, "", body]).rstrip() + "\n"


def validate_strategy_code(code: str) -> dict:
    """Validate agent-generated strategy code through lint + sandbox."""
    current_code = normalize_generated_strategy_code(code)
    all_issues = []

    for round_num in range(MAX_FIX_ROUNDS):
        lint_result = lint_code(current_code)

        if lint_result["passed"]:
            break

        all_issues.extend(lint_result["issues"])

        if lint_result.get("fixed_code"):
            current_code = lint_result["fixed_code"]
            log.debug("Self-heal round %d: applied auto-fix", round_num + 1)
        else:
            break

    final_lint = lint_code(current_code)

    # SECURITY: static-scan the strategy with the AST guard BEFORE executing it.
    # run_code already isolates (secret-stripped env + resource caps), but every
    # other importer (registry/intake/optimizer) runs the guard first; doing the
    # same here removes the one agent path where code reached execution without a
    # scan, and rejects obviously-hostile modules (os/eval/file reads/aliased
    # builtins) before they run at all. We scan the raw strategy, not the wrapped
    # harness (the harness legitimately imports sys/inspect/pandas).
    ast_report = scan_source(current_code)
    if not ast_report.ok:
        findings = "; ".join(
            f"line {f.lineno}: {f.message}" for f in ast_report.findings[:5]
        )
        log.warning("Self-heal validation rejected by AST guard: %s", findings)
        return {
            "valid": False,
            "code": current_code,
            "lint_issues": all_issues,
            "lint_passed": final_lint["passed"],
            "ast_findings": [
                {"lineno": f.lineno, "col": f.col, "kind": f.kind, "message": f.message}
                for f in ast_report.findings
            ],
            "execution_result": {
                "returncode": -1,
                "stdout": "",
                "stderr": f"AST guard blocked execution: {findings}",
                "timed_out": False,
            },
        }

    test_code = _wrap_with_test_harness(current_code)
    exec_result = run_code(test_code, timeout=30, max_memory_mb=256)

    valid = (
        final_lint["passed"]
        and exec_result["returncode"] == 0
        and not exec_result["timed_out"]
    )

    if not valid:
        reasons = []
        if not final_lint["passed"]:
            reasons.append(f"lint: {len(final_lint['issues'])} issues")
        if exec_result["returncode"] != 0:
            reasons.append(f"exec: exit code {exec_result['returncode']}")
        if exec_result["timed_out"]:
            reasons.append("exec: timed out")
        if exec_result["stderr"]:
            reasons.append(f"stderr: {exec_result['stderr'][:200]}")
        log.warning("Self-heal validation failed: %s", "; ".join(reasons))

    return {
        "valid": valid,
        "code": current_code,
        "lint_issues": all_issues,
        "lint_passed": final_lint["passed"],
        "execution_result": {
            "returncode": exec_result["returncode"],
            "stdout": exec_result["stdout"][:2000],
            "stderr": exec_result["stderr"][:1000],
            "timed_out": exec_result["timed_out"],
        },
    }


def _wrap_with_test_harness(code: str) -> str:
    """Wrap strategy code with a runtime validation harness."""
    code = normalize_generated_strategy_code(code)
    future_lines: list[str] = []
    body_lines: list[str] = []
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("from __future__ import "):
            future_lines.append(stripped)
        else:
            body_lines.append(line)
    future_block = "\n".join(dict.fromkeys(future_lines))
    if future_block:
        future_block += "\n"
    code_body = "\n".join(body_lines).strip()
    return f'''\
{future_block}
import sys

# Agent-generated code
{code_body}

# Test harness
import inspect
import numpy as np
import pandas as pd

from forven.strategies.base import BaseStrategy, Signal, DirectionalSignals

index = pd.date_range("2025-01-01", periods=100, freq="h", tz="UTC")
close = np.linspace(100.0, 110.0, num=100)
open_ = np.concatenate(([close[0]], close[:-1]))
high = np.maximum(open_, close) + 0.5
low = np.minimum(open_, close) - 0.5
volume = np.linspace(1000.0, 2000.0, num=100)
dummy_df = pd.DataFrame(
    {{
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }},
    index=index,
)

strategy_classes = []
for name, obj in list(globals().items()):
    if not inspect.isclass(obj):
        continue
    if obj is BaseStrategy:
        continue
    try:
        if issubclass(obj, BaseStrategy):
            strategy_classes.append((name, obj))
    except Exception:
        continue

if not strategy_classes:
    print(
        "ERROR: No BaseStrategy subclass found. The module must define a class extending "
        "BaseStrategy (`from forven.strategies.base import BaseStrategy, Signal`) and "
        "export STRATEGY_CLASS = <that class>."
    )
    sys.exit(1)

for cls_name, cls in strategy_classes:
    print(f"Found strategy class: {{cls_name}}")

    required = ["name", "asset", "strategy_type", "default_params"]
    for attr in required:
        if not hasattr(cls, attr):
            print(f"ERROR: Missing required attribute: {{attr}}")
            sys.exit(1)

    try:
        instance = cls("test_id", {{}})
    except Exception as exc:
        print(f"ERROR: Could not instantiate {{cls_name}}: {{exc}}")
        sys.exit(1)

    try:
        signal = instance.generate_signal(dummy_df.copy())
    except Exception as exc:
        print(f"ERROR: generate_signal failed for {{cls_name}}: {{exc}}")
        sys.exit(1)

    if isinstance(signal, Signal):
        signal_payload = signal.to_dict()
    elif isinstance(signal, dict):
        signal_payload = dict(signal)
    else:
        # Name the full contract: agents have burned 4+ registration attempts
        # cycling float/int/np.int64/Series against the bare "invalid type"
        # message and then guessing a nonexistent Signal-enum module (2026-07-07
        # S06308 hollow-mint cluster).
        print(
            f"ERROR: generate_signal returned invalid type for {{cls_name}}: {{type(signal).__name__}}. "
            "Return the Signal DATACLASS (`from forven.strategies.base import Signal` -- the only "
            "allowlisted import path; there is no forven.strategies.signals module) or a dict with "
            "entry_signal/exit_signal keys. Signal is not an enum (no BUY/SELL/NEUTRAL members): "
            "no-trade is Signal(), an entry is Signal(entry_signal=True, direction='long', "
            "price=float(df['close'].iloc[-1])). Never return float/int/numpy scalars/Series."
        )
        sys.exit(1)

    required_signal_keys = {{"entry_signal", "exit_signal"}}
    missing_signal_keys = sorted(required_signal_keys.difference(signal_payload))
    if missing_signal_keys:
        print(
            f"ERROR: Missing signal keys for {{cls_name}}: {{', '.join(missing_signal_keys)}}. "
            "Dict signals need boolean entry_signal AND exit_signal keys; simpler is to return "
            "Signal(entry_signal=..., exit_signal=...) from forven.strategies.base."
        )
        sys.exit(1)

    for key in ("entry_signal", "exit_signal", "price", "confidence", "direction"):
        value = signal_payload.get(key)
        if isinstance(value, (pd.Series, np.ndarray, list, tuple)):
            print(
                f"ERROR: {{key}} must be a scalar value from generate_signal(); "
                "implement generate_signals(df) for vectorized Series output."
            )
            sys.exit(1)

    # Validate the VECTORIZED generate_signals(df) path. The backtester prefers
    # it over the scalar generate_signal() loop, so a malformed vectorized payload
    # (wrong shape, raised exception) passes this validation but blows up at
    # backtest time, leaving the strategy with no metrics and burying it. Mirror
    # the backtester contract (backtest.py: DirectionalSignals | 2/4-tuple | None)
    # here so the codegen retry can repair it in-conversation.
    if cls.generate_signals is not BaseStrategy.generate_signals:
        try:
            vec_payload = instance.generate_signals(dummy_df.copy())
        except NotImplementedError:
            vec_payload = None
        except Exception as exc:
            print(f"ERROR: generate_signals(df) raised for {{cls_name}}: {{exc}}")
            sys.exit(1)
        if vec_payload is not None and not (
            isinstance(vec_payload, DirectionalSignals)
            or (isinstance(vec_payload, (tuple, list)) and len(vec_payload) in (2, 4))
        ):
            print(
                f"ERROR: generate_signals(df) for {{cls_name}} must return "
                "(entry_signals, exit_signals), DirectionalSignals, a 4-series "
                f"payload, or None — got {{type(vec_payload).__name__}}"
            )
            sys.exit(1)

    print(f"Validated {{cls_name}} with direction={{signal_payload.get('direction', 'long')}}")

print("SELFHEAL_OK")
'''
