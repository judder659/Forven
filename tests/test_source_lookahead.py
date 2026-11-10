"""Static negative-shift look-ahead guard (S20977, S21756).

The truncation probe misses a future read the strategy never fires on, or one
only its vectorized signals make. ``.shift(-N)`` in the source is enough.
"""

import json
from datetime import datetime, timezone

import pytest

from forven.db import get_db
from forven.strategies.source_lookahead import (
    imported_runtime_negative_shift_reason,
    negative_shift_lines,
    negative_shift_reason,
)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("x = df['close'].shift(-1)\n", [1]),
        ("x = df.close.shift(periods=-2)\n", [1]),
        ("n = 3\nx = s.shift(-n)\n", [2]),
        ("x = s.shift(1)\ny = s.shift()\n", []),
        ("x = s.shift(-0)\n", []),
        ("x = s.shift(periods=4)\n", []),
        ("def f(:\n", []),
    ],
)
def test_negative_shift_lines(source, expected):
    assert negative_shift_lines(source) == expected


def test_reason_names_lines():
    reason = negative_shift_reason([12, 40])
    assert "line 12, 40" in reason
    assert negative_shift_reason([]) is None


def _imported_dir(monkeypatch, tmp_path):
    from forven.strategies import imported

    monkeypatch.setattr(imported, "__file__", str(tmp_path / "__init__.py"))
    return tmp_path


def test_imported_runtime_reason(monkeypatch, tmp_path):
    d = _imported_dir(monkeypatch, tmp_path)
    (d / "peeks.py").write_text("def generate_signals(df):\n    return df.close.shift(-1) > df.close\n")
    (d / "clean.py").write_text("def generate_signals(df):\n    return df.close.shift(1) > df.close\n")
    assert "line 2" in imported_runtime_negative_shift_reason("imported__peeks")
    assert imported_runtime_negative_shift_reason("imported__clean") is None
    assert imported_runtime_negative_shift_reason("imported__missing") is None
    assert imported_runtime_negative_shift_reason("imported__../peeks") is None
    assert imported_runtime_negative_shift_reason("rsi_momentum") is None


def test_intake_archives_file_that_reads_next_bar(forven_db, monkeypatch, tmp_path):
    from forven.strategies.intake import register_imported_strategy_file

    d = _imported_dir(monkeypatch, tmp_path)
    module = "next_bar_fixture"
    (d / f"{module}.py").write_text(
        f'TYPE_NAME = "{module}"\n'
        "def generate_signals(df):\n"
        "    return df['close'].shift(-1) > df['close']\n"
    )
    monkeypatch.setattr("forven.sandbox.strategy_worker._reset_worker", lambda: None)
    result = register_imported_strategy_file(
        module_name=module,
        source="agent_register",
        _validated_meta={
            "ok": True, "type_name": module, "asset": "BTC", "certified": True,
            "canonical_params": {"window": 10}, "lookahead_verifiable": False,
        },
    )
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage FROM strategies WHERE id=?", (result["strategy_id"],)
        ).fetchone()
    assert row["stage"] == "archived"
    assert "reads future bars" in json.dumps(result)


def test_paper_promotion_blocked_for_next_bar_code(forven_db, monkeypatch, tmp_path):
    from forven.policy import evaluate_promotion

    d = _imported_dir(monkeypatch, tmp_path)
    (d / "peeky.py").write_text("def generate_signals(df):\n    return df.close.shift(-1) > 0\n")
    monkeypatch.setattr("forven.strategies.identity.execution_identity_error", lambda *a, **k: None)
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, runtime_type, symbol, timeframe, params, metrics, "
            "status, owner, stage, stage_changed_at, created_at, updated_at) "
            "VALUES ('S9', 'S9', 'imported__peeky', 'imported__peeky', 'ETH', '4h', '{}', '{}', "
            "'gauntlet', 'brain', 'gauntlet', ?, ?, ?)",
            (now, now, now),
        )
        conn.commit()
    for to_stage in ("paper", "live_graduated"):
        ok, reason = evaluate_promotion("S9", "gauntlet", to_stage, record_rejection=False)
        assert not ok
        assert "reads future bars" in reason
