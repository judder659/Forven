"""Invalid candidates must produce actionable HTTP errors without partial intake."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pathlib import Path

import pytest

from forven.db import get_db
from forven.routers.lifecycle import router


def test_unknown_market_returns_validation_error_without_creating_strategy(
    forven_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forven import data

    # Symbol validation intentionally fails open on a wholly empty installation.
    # Give this isolated installation a known market to validate against.
    lake = tmp_path / "market-data"
    (lake / "BTC-USDT").mkdir(parents=True)
    monkeypatch.setattr(data, "DATA_DIR", lake)
    app = FastAPI()
    app.include_router(router)
    with get_db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    with TestClient(app) as api:
        response = api.post("/api/lifecycle/strategies", json={
            "name": "Invalid market control", "type": "stochastic",
            "symbol": "NONEXISTENT/USDT", "timeframe": "1h",
            "definition_json": {"params": {}},
        })
    assert response.status_code == 422, response.text
    assert "unknown market symbol" in response.json()["detail"]
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == before
