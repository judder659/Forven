"""Strategy container import/export (portability) tests.

Covers the versioned export envelope and the import path that recreates a
strategy as a fresh quick_screen container without touching the source.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from forven import strategy_lifecycle as lifecycle
from forven.db import create_strategy_container, get_db


def _make_macd_container() -> tuple[str, str]:
    """A certified param-family container that round-trips cleanly."""
    with get_db() as conn:
        sid, display_id, _ = create_strategy_container(
            conn=conn,
            name="macd-source",
            type_="macd",
            symbol="BTC",
            timeframe="1h",
            params={"fast": 12, "slow": 26, "signal": 9},
        )
    return sid, display_id


def test_build_container_export_envelope_shape(forven_db):
    sid, _ = _make_macd_container()

    env = lifecycle.build_container_export(sid)

    meta = env["forven_export"]
    assert meta["kind"] == "strategy_container"
    assert meta["version"] == "1.0"
    assert meta["source_strategy_id"] == sid
    assert meta["exported_at"]
    # Full snapshot rides along inside the envelope.
    for key in ("strategy", "configuration", "history", "execution", "events"):
        assert key in env
    assert env["configuration"]["type"] == "macd"


def test_export_import_round_trip_creates_new_quick_screen(forven_db):
    sid, _ = _make_macd_container()

    env = lifecycle.build_container_export(sid)
    result = lifecycle.import_strategy_container(env)

    assert result["ok"] is True
    new_id = result["strategy_id"]
    assert new_id and new_id != sid  # never overwrites the source
    assert result["stage"] == "quick_screen"
    assert result["source_strategy_id"] == sid

    with get_db() as conn:
        row = conn.execute(
            "SELECT type, source, source_ref, stage FROM strategies WHERE id = ?",
            (new_id,),
        ).fetchone()
        # Source container is untouched.
        src = conn.execute(
            "SELECT stage FROM strategies WHERE id = ?", (sid,)
        ).fetchone()

    assert row is not None
    assert row["type"] == "macd"  # authoritative type survives the round-trip
    assert row["source"] == "import"
    assert row["source_ref"] == sid
    assert row["stage"] == "quick_screen"
    assert src["stage"] == "quick_screen"


def test_import_warns_history_not_replayed(forven_db):
    sid, _ = _make_macd_container()
    env = lifecycle.build_container_export(sid)
    # Force a non-empty history so the "not imported" warning fires deterministically.
    env["history"] = {"all": [{"result_id": "BR-x"}], "backtests": [{"result_id": "BR-x"}]}

    result = lifecycle.import_strategy_container(env)

    assert result["ok"] is True
    assert any("not imported" in str(w).lower() for w in result["warnings"])


def test_import_rejects_missing_envelope(forven_db):
    with pytest.raises(HTTPException) as exc:
        lifecycle.import_strategy_container({"strategy": {}, "configuration": {}})
    assert exc.value.status_code == 400


def test_import_rejects_non_object_payload(forven_db):
    with pytest.raises(HTTPException) as exc:
        lifecycle.import_strategy_container("not-a-dict")
    assert exc.value.status_code == 400


def test_import_rejects_unsupported_version(forven_db):
    env = {
        "forven_export": {"kind": "strategy_container", "version": "9.9"},
        "configuration": {
            "type": "macd",
            "symbol": "BTC",
            "timeframe": "1h",
            "params": {"fast": 12, "slow": 26, "signal": 9},
        },
    }
    with pytest.raises(HTTPException) as exc:
        lifecycle.import_strategy_container(env)
    assert exc.value.status_code == 400


def test_import_rejects_unregistered_runtime_type(forven_db):
    # A code-class strategy whose runtime type isn't registered on this machine
    # cannot be reconstructed from params alone — surfaced as ok:false, not a crash.
    env = {
        "forven_export": {
            "kind": "strategy_container",
            "version": "1.0",
            "source_strategy_id": "S99999",
        },
        "configuration": {
            "type": "totally_made_up_family_xyz",
            "symbol": "BTC",
            "timeframe": "1h",
            "params": {"alpha": 1, "beta": 2},
        },
    }

    result = lifecycle.import_strategy_container(env)

    assert result["ok"] is False
    assert result["error"]
