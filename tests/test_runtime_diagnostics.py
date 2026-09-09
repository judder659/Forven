import threading
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forven.control_plane.runtime_diagnostics import runtime_thread_snapshot
from forven.routers.ops import router


def test_snapshot_identifies_waiting_worker_without_exposing_locals() -> None:
    ready = threading.Event()
    release = threading.Event()

    def blocked_worker() -> None:
        sensitive_local = "do-not-expose-credentials"
        ready.set()
        release.wait(timeout=5)
        assert sensitive_local

    worker = threading.Thread(target=blocked_worker, name="diagnostic-blocked-worker")
    worker.start()
    try:
        assert ready.wait(timeout=2)
        snapshot = runtime_thread_snapshot()
        row = next(t for t in snapshot["threads"] if t["name"] == worker.name)
        assert any(frame["function"] == "blocked_worker" for frame in row["stack"])
        assert any(frame["function"] == "wait" for frame in row["stack"])
        assert "do-not-expose-credentials" not in str(snapshot)
        assert all(set(frame) == {"file", "function", "line"} for frame in row["stack"])
    finally:
        release.set()
        worker.join(timeout=2)


@pytest.mark.parametrize("endpoint,field", [
    ("/api/system/runtime/threads", "threads"),
    ("/api/system/runtime/health-profile", "profile"),
])
@pytest.mark.usefixtures("forven_db")
def test_thread_endpoint_requires_operator_key(monkeypatch: pytest.MonkeyPatch, endpoint: str, field: str) -> None:
    monkeypatch.setenv("FORVEN_API_KEY", "api-test-key")
    monkeypatch.setenv("FORVEN_OPERATOR_KEY", "operator-test-key")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        denied = client.get(endpoint, headers={"x-api-key": "api-test-key"})
        assert denied.status_code == 401
        allowed = client.get(endpoint, headers={
            "x-api-key": "api-test-key", "x-operator-key": "operator-test-key",
        })
        assert allowed.status_code == 200
        assert allowed.json()[field]


@pytest.mark.usefixtures("forven_db")
def test_dead_runtime_thread_degrades_health_even_with_fresh_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.control_plane import runtime_diagnostics as diagnostics, status
    from forven.db import kv_set
    from datetime import datetime, timezone

    monkeypatch.setattr(diagnostics, "_runtime_threads", {})
    thread = threading.Thread(target=lambda: None, name="forven-failed-test-loop")
    diagnostics.track_runtime_thread(thread)
    thread.start()
    thread.join(timeout=2)
    kv_set("scheduler:last_progress_at", datetime.now(timezone.utc).isoformat())
    report = status.health_check()
    assert any("forven-failed-test-loop" in issue for issue in report["issues"])
    assert report["status"] == "degraded"
    diagnostics.mark_runtime_thread_declined(thread)
    assert not any("forven-failed-test-loop" in issue for issue in status.health_check()["issues"])
