from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import pytest

from forven.api_security import (
    ApiKeyMiddleware,
    CsrfOriginMiddleware,
    assert_safe_bind_host,
    get_allowed_cors_origins,
    require_operator_access,
)


def _build_test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/shutdown")
    def shutdown_route() -> dict[str, str]:
        return {"status": "shutting_down"}

    @app.get("/api/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/operator", dependencies=[Depends(require_operator_access)])
    def operator() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_api_key_middleware_exempts_health(monkeypatch):
    monkeypatch.setenv("FORVEN_API_KEY", "api-key-123")
    client = TestClient(_build_test_app())

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_key_middleware_requires_api_key(monkeypatch):
    monkeypatch.setenv("FORVEN_API_KEY", "api-key-123")
    client = TestClient(_build_test_app())

    missing = client.get("/api/ping")
    allowed = client.get("/api/ping", headers={"X-API-Key": "api-key-123"})

    assert missing.status_code == 401
    assert missing.json()["detail"] == "Invalid or missing API key"
    assert allowed.status_code == 200
    assert allowed.json() == {"ok": True}


def test_operator_routes_require_operator_key_when_configured(monkeypatch):
    monkeypatch.setenv("FORVEN_API_KEY", "api-key-123")
    monkeypatch.setenv("FORVEN_OPERATOR_KEY", "operator-key-456")
    client = TestClient(_build_test_app())

    missing = client.post("/api/operator", headers={"X-API-Key": "api-key-123"})
    allowed = client.post(
        "/api/operator",
        headers={
            "X-API-Key": "api-key-123",
            "X-Operator-Key": "operator-key-456",
        },
    )

    assert missing.status_code == 401
    assert missing.json()["detail"] == "Invalid or missing operator key"
    assert allowed.status_code == 200
    assert allowed.json() == {"ok": True}


def test_api_key_middleware_exempts_shutdown(monkeypatch):
    """A local launcher POSTs /api/shutdown on close without the per-launch key;
    the route has its own 127.0.0.1-only check so skipping auth is safe."""
    monkeypatch.setenv("FORVEN_API_KEY", "api-key-123")
    client = TestClient(_build_test_app())

    response = client.post("/api/shutdown")

    assert response.status_code == 200
    assert response.json() == {"status": "shutting_down"}


def test_allowed_cors_origins_default_to_explicit_local_hosts(monkeypatch):
    monkeypatch.delenv("FORVEN_CORS_ORIGINS", raising=False)
    monkeypatch.setenv("FRONTEND_PORT", "4173")
    monkeypatch.setenv("FORVEN_PORT", "9003")

    origins = get_allowed_cors_origins()

    assert "http://127.0.0.1:4173" in origins
    assert "http://localhost:4173" in origins
    assert "http://127.0.0.1:4174" in origins
    assert "http://localhost:4178" in origins
    assert "*" not in origins


def _build_csrf_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CsrfOriginMiddleware)

    @app.get("/api/read")
    def read() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/write")
    def write() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_csrf_allows_request_without_origin():
    """Local launcher / CLI / server-to-server send no Origin — must pass."""
    client = TestClient(_build_csrf_app())
    assert client.post("/api/write").status_code == 200


def test_csrf_allows_same_origin():
    """A same-origin POST (Origin == the target Host) is not CSRF."""
    client = TestClient(_build_csrf_app())  # base_url http://testserver
    r = client.post("/api/write", headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_csrf_rejects_cross_site_origin(monkeypatch):
    monkeypatch.delenv("FORVEN_CORS_ORIGINS", raising=False)
    client = TestClient(_build_csrf_app())
    r = client.post("/api/write", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert "rejected" in r.json()["detail"].lower()


def test_csrf_allows_cors_allowlisted_origin(monkeypatch):
    monkeypatch.setenv("FORVEN_CORS_ORIGINS", "http://trusted.example")
    client = TestClient(_build_csrf_app())
    r = client.post("/api/write", headers={"Origin": "http://trusted.example"})
    assert r.status_code == 200


def test_csrf_allows_launcher_frontend_fallback_port(monkeypatch):
    monkeypatch.delenv("FORVEN_CORS_ORIGINS", raising=False)
    monkeypatch.setenv("FRONTEND_PORT", "4173")
    client = TestClient(_build_csrf_app())
    r = client.post("/api/write", headers={"Origin": "http://127.0.0.1:4174"})
    assert r.status_code == 200


def test_csrf_ignores_safe_methods(monkeypatch):
    """A cross-origin GET is harmless (CORS hides the body); don't block reads."""
    monkeypatch.delenv("FORVEN_CORS_ORIGINS", raising=False)
    client = TestClient(_build_csrf_app())
    r = client.get("/api/read", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200


def test_csrf_kill_switch_disables_guard(monkeypatch):
    monkeypatch.delenv("FORVEN_CORS_ORIGINS", raising=False)
    monkeypatch.setenv("FORVEN_CSRF_PROTECT", "false")
    client = TestClient(_build_csrf_app())
    r = client.post("/api/write", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200


def test_loopback_bind_needs_no_keys(monkeypatch):
    monkeypatch.delenv("FORVEN_API_KEY", raising=False)
    monkeypatch.delenv("FORVEN_OPERATOR_KEY", raising=False)
    monkeypatch.delenv("FORVEN_AUTH_REQUIRED", raising=False)
    assert_safe_bind_host("127.0.0.1")  # no raise — localhost is fail-open by design


def test_exposed_bind_requires_both_api_and_operator_keys(monkeypatch):
    monkeypatch.delenv("FORVEN_AUTH_REQUIRED", raising=False)
    monkeypatch.setenv("FORVEN_API_KEY", "api-key-123")
    monkeypatch.delenv("FORVEN_OPERATOR_KEY", raising=False)
    # API key alone is NOT enough on a non-loopback bind (two-tier would collapse).
    with pytest.raises(RuntimeError, match="operator key"):
        assert_safe_bind_host("0.0.0.0")
    # Both keys → allowed.
    monkeypatch.setenv("FORVEN_OPERATOR_KEY", "operator-key-456")
    assert_safe_bind_host("0.0.0.0")


def test_operator_access_fails_closed_when_api_key_set_without_operator(monkeypatch):
    monkeypatch.delenv("FORVEN_AUTH_REQUIRED", raising=False)
    monkeypatch.setenv("FORVEN_API_KEY", "api-key-123")
    monkeypatch.delenv("FORVEN_OPERATOR_KEY", raising=False)
    client = TestClient(_build_test_app())
    r = client.post("/api/operator", headers={"X-API-Key": "api-key-123"})
    assert r.status_code == 503
    assert "operator key not configured" in r.json()["detail"].lower()


def test_operator_access_open_on_keyless_localhost_default(monkeypatch):
    monkeypatch.delenv("FORVEN_API_KEY", raising=False)
    monkeypatch.delenv("FORVEN_OPERATOR_KEY", raising=False)
    monkeypatch.delenv("FORVEN_AUTH_REQUIRED", raising=False)
    client = TestClient(_build_test_app())
    assert client.post("/api/operator").status_code == 200
