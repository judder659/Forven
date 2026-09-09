"""Creator draft revision and Forge execution handoff regressions."""
import json

import pytest
from fastapi import HTTPException
from forven.routers import strategy_library as library

SPEC = {
    "indicators": [], "params": {},
    "entry_long": {"conditions": [{"left": "close", "op": ">", "right": 100}]},
}


def create():
    return library.create_library_entry(library.LibraryCreateBody(name="Draft", spec=SPEC))


@pytest.mark.parametrize("change", [
    {"spec": {**SPEC, "params": {"threshold": 42}}}, {"symbol": "ETH/USDT"},
    {"timeframe": "15m"}, {"params": {"leverage": 2}}, {"kind": "code", "code": "new code"},
])
def test_definition_changes_invalidate_evidence(forven_db, change):
    entry = create()
    library.update_library_entry(entry["id"], library.LibraryUpdateBody(status="tested", last_result_id="old-result", expected_version=1))
    updated = library.update_library_entry(entry["id"], library.LibraryUpdateBody(**change, expected_version=1))
    assert updated["version"] == 2
    assert updated["status"] == "draft"
    assert updated["last_result_id"] is None
    assert updated["forge_strategy_id"] is None
    with pytest.raises(HTTPException) as error:
        library.update_library_entry(entry["id"], library.LibraryUpdateBody(status="tested", last_result_id="late-result", expected_version=1))
    assert error.value.status_code == 409


def test_metadata_and_identical_save_preserve_revision(forven_db):
    entry = create()
    result = library.update_library_entry(entry["id"], library.LibraryUpdateBody(name="Renamed", spec=SPEC, expected_version=1))
    assert result["version"] == 1


def test_stale_save_and_forge_are_rejected(forven_db):
    entry = create()
    library.update_library_entry(entry["id"], library.LibraryUpdateBody(timeframe="4h"))
    with pytest.raises(HTTPException) as error:
        library.update_library_entry(entry["id"], library.LibraryUpdateBody(timeframe="15m", expected_version=1))
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        library.send_library_entry_to_forge(entry["id"], library.LibraryForgeBody(expected_version=1))
    assert error.value.status_code == 409


def test_visual_forge_persists_profile_used_by_engine(forven_db):
    from forven.db import get_db
    from forven.strategies.backtest import execution_controls_from_params
    profile = {"sizing_mode": "fraction", "stop_loss_pct": 3.0, "risk_per_trade": 0.02}
    entry = library.create_library_entry(library.LibraryCreateBody(
        name="Configured", spec=SPEC, params={
            "execution_profile": profile, "leverage": 2, "trade_mode": "long_only",
            "_creator_context": {"fee_bps": 10, "slippage_bps": 5},
        },
    ))
    result = library.send_library_entry_to_forge(entry["id"], library.LibraryForgeBody(expected_version=1))
    with get_db() as conn:
        row = conn.execute("SELECT params, stage FROM strategies WHERE id = ?", (result["forge"]["strategy_id"],)).fetchone()
    params = json.loads(row["params"])
    assert params["spec"] == SPEC
    assert execution_controls_from_params(params) == profile
    assert params["leverage"] == 2
    assert params["_creator_context"]["fee_bps"] == 10
    assert row["stage"] == "quick_screen"
