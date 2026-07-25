from __future__ import annotations

import socket
import time
import urllib.request

import pytest
from fastapi import HTTPException

from forven import api_core


def test_oauth_status_rejects_non_oauth_provider():
    # lmstudio is a supported auth provider but has no OAuth flow,
    # so the function's own guard (not _normalize_auth_provider's) fires.
    with pytest.raises(HTTPException) as exc:
        api_core.get_auth_provider_oauth_status("lmstudio", "any-state")
    assert exc.value.status_code == 400
    assert "unsupported oauth provider" in str(exc.value.detail).lower()


def test_oauth_status_returns_expired_when_session_missing(monkeypatch):
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", {})
    result = api_core.get_auth_provider_oauth_status("openai", "missing-state")
    assert result["status"] == "expired"


def test_oauth_cancel_releases_session(monkeypatch):
    sessions = {"openai": {"state-abc": {"created_at": 9_999_999_999.0}}}
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", sessions)

    result = api_core.cancel_auth_provider_oauth("openai", "state-abc")
    assert result == {"ok": True, "provider": "openai"}
    assert "state-abc" not in sessions.get("openai", {})


def test_oauth_cancel_idempotent_when_session_missing(monkeypatch):
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", {})
    result = api_core.cancel_auth_provider_oauth("openai", "nope")
    assert result == {"ok": True, "provider": "openai"}


def test_callback_listener_captures_code(monkeypatch):
    from forven.auth.callback_listener import LoopbackCallbackListener

    listener = LoopbackCallbackListener(port=1455, ttl_seconds=10)
    listener.start()
    try:
        urllib.request.urlopen(
            "http://127.0.0.1:1455/auth/callback?code=AUTH123&state=xyz",
            timeout=2,
        ).read()

        for _ in range(20):
            if listener.code is not None:
                break
            time.sleep(0.05)

        assert listener.code == "AUTH123"
        assert listener.state == "xyz"
    finally:
        listener.shutdown()


def test_callback_listener_notifies_callback_side_effect():
    from forven.auth.callback_listener import LoopbackCallbackListener

    captured: dict = {}
    listener = LoopbackCallbackListener(
        port=1455,
        ttl_seconds=10,
        on_callback=lambda code, state: captured.update({"code": code, "state": state}),
    )
    listener.start()
    try:
        urllib.request.urlopen(
            "http://127.0.0.1:1455/auth/callback?code=AUTH456&state=side",
            timeout=2,
        ).read()

        for _ in range(20):
            if captured:
                break
            time.sleep(0.05)

        assert captured == {"code": "AUTH456", "state": "side"}
    finally:
        listener.shutdown()


def _free_port() -> int:
    """A port nobody is using, chosen by the OS.

    These tests used to hardcode 1455. On Linux a just-closed listener leaves the
    port in TIME_WAIT, so a plain bind() to the same number fails with
    EADDRINUSE (errno 98) — Windows is more forgiving, which is why it only ever
    broke in CI. Asking the kernel for a fresh port per test removes both the
    TIME_WAIT race and any collision with whatever else the runner has open.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_callback_listener_shutdown_releases_port():
    from forven.auth.callback_listener import LoopbackCallbackListener

    port = _free_port()
    listener = LoopbackCallbackListener(port=port, ttl_seconds=10)
    listener.start()
    listener.shutdown()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    finally:
        sock.close()


def test_callback_listener_bind_failure_signaled():
    from forven.auth.callback_listener import LoopbackCallbackListener

    port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Deliberately NO SO_REUSEADDR here: the blocker must hold the port
    # EXCLUSIVELY so the listener's bind genuinely fails. On Windows
    # SO_REUSEADDR behaves like SO_REUSEPORT and would let the listener bind the
    # same port anyway, making this assert vacuous.
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        listener = LoopbackCallbackListener(port=port, ttl_seconds=2)
        ok = listener.start()
        assert ok is False
        assert listener.bind_error is not None
    finally:
        blocker.close()


def test_openai_start_returns_auto_callback_true_when_listener_binds(monkeypatch):
    started: dict = {}

    class _FakeListener:
        def __init__(self, port, ttl_seconds, on_callback=None):
            started["port"] = port
            started["ttl"] = ttl_seconds
            self.on_callback = on_callback
            self.code = None
            self.bind_error = None

        def start(self):
            return True

        def shutdown(self):
            pass

        def expired(self):
            return False

    monkeypatch.setattr(api_core, "LoopbackCallbackListener", _FakeListener, raising=False)
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", {})

    result = api_core._build_openai_oauth_start()
    assert result["auto_callback"] is True
    assert result["state"]
    assert "code_verifier" not in result  # kept server-side in auto-mode
    assert started["port"] == 1455


def test_openai_start_falls_back_to_manual_when_bind_fails(monkeypatch):
    class _FakeListener:
        def __init__(self, port, ttl_seconds, on_callback=None):
            self.on_callback = on_callback
            self.bind_error = "address in use"

        def start(self):
            return False

        def shutdown(self):
            pass

        def expired(self):
            return False

    monkeypatch.setattr(api_core, "LoopbackCallbackListener", _FakeListener, raising=False)
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", {})

    result = api_core._build_openai_oauth_start()
    assert result["auto_callback"] is False
    assert result["code_verifier"]  # manual flow needs verifier client-side


def test_openai_status_awaiting_when_listener_has_no_code(monkeypatch):
    class _Listener:
        code = None
        state = None
        def expired(self): return False
        def shutdown(self): pass

    sessions = {"openai": {"st1": {
        "created_at": time.time(),
        "code_verifier": "v",
        "listener": _Listener(),
        "auto_callback": True,
    }}}
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", sessions)
    assert api_core.get_auth_provider_oauth_status("openai", "st1") == {"status": "awaiting_user"}


def test_openai_status_completes_when_listener_has_code(monkeypatch):
    class _Listener:
        code = "AUTHCODE"
        state = "st1"
        def expired(self): return False
        def shutdown(self): pass

    sessions = {"openai": {"st1": {
        "created_at": time.time(),
        "code_verifier": "verif",
        "listener": _Listener(),
        "auto_callback": True,
    }}}
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", sessions)

    captured = {}
    def _fake_complete(state, code, code_verifier):
        captured["state"] = state
        captured["code"] = code
        captured["verifier"] = code_verifier

    monkeypatch.setattr(api_core, "_complete_openai_oauth", _fake_complete)

    result = api_core.get_auth_provider_oauth_status("openai", "st1")
    assert result["status"] == "complete"
    assert captured == {"state": "st1", "code": "AUTHCODE", "verifier": "verif"}


def test_openai_status_completes_when_callback_was_recorded_by_state(monkeypatch):
    class _Listener:
        code = None
        state = None
        def expired(self): return False
        def shutdown(self): pass

    sessions = {"openai": {"st1": {
        "created_at": time.time(),
        "code_verifier": "verif",
        "listener": _Listener(),
        "auto_callback": True,
    }}}
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", sessions)
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_CALLBACKS", {"openai": {"st1": "RECORDED"}})

    captured = {}
    def _fake_complete(state, code, code_verifier):
        captured["state"] = state
        captured["code"] = code
        captured["verifier"] = code_verifier

    monkeypatch.setattr(api_core, "_complete_openai_oauth", _fake_complete)

    result = api_core.get_auth_provider_oauth_status("openai", "st1")
    assert result["status"] == "complete"
    assert captured == {"state": "st1", "code": "RECORDED", "verifier": "verif"}


def test_openai_callback_finalizer_completes_and_stores_result(monkeypatch):
    class _Listener:
        def __init__(self):
            self.shutdown_called = False

        def shutdown(self):
            self.shutdown_called = True

    listener = _Listener()
    sessions = {"openai": {"st1": {
        "created_at": time.time(),
        "code_verifier": "verif",
        "listener": listener,
        "auto_callback": True,
    }}}
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", sessions)
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_CALLBACKS", {})
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_RESULTS", {}, raising=False)

    captured = {}

    def _fake_complete(state, code, code_verifier):
        captured["state"] = state
        captured["code"] = code
        captured["verifier"] = code_verifier

    monkeypatch.setattr(api_core, "_complete_openai_oauth", _fake_complete)

    api_core._finalize_openai_callback("http://localhost:1455/auth/callback?code=AUTHCODE&state=st1", "st1")

    assert captured == {"state": "st1", "code": "AUTHCODE", "verifier": "verif"}
    assert api_core._AUTH_OAUTH_RESULTS["openai"]["st1"] == {"status": "complete"}
    assert listener.shutdown_called is True


def test_openai_status_returns_background_result_after_session_consumed(monkeypatch):
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", {})
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_RESULTS", {"openai": {"st1": {"status": "complete"}}}, raising=False)

    result = api_core.get_auth_provider_oauth_status("openai", "st1")

    assert result == {"status": "complete"}


def test_auth_providers_payload_exposes_last_refresh_error(monkeypatch):
    profile = {
        "type": "oauth",
        "provider": "openai",
        "access": "tok",
        "expires": int(time.time() * 1000) + 60_000,
        "last_refresh_error": "refresh denied",
        "last_refresh_at": int(time.time() * 1000) - 1000,
    }
    monkeypatch.setattr(
        api_core, "get_profile",
        lambda p: dict(profile) if p == "openai" else None,
    )
    payload = api_core._build_auth_provider_payload("openai")
    assert payload["last_refresh_error"] == "refresh denied"
    assert payload["status"] == "needs_reauth"


def test_refresh_failure_records_last_refresh_error(monkeypatch):
    from forven.auth import store as auth_store

    profile = {
        "type": "oauth",
        "provider": "openai",
        "access": "expired",
        "refresh": "ref",
        "expires": 1,  # forces _is_expired True
    }
    monkeypatch.setattr(auth_store, "get_profile", lambda provider: dict(profile))

    saved: dict = {}
    monkeypatch.setattr(
        auth_store, "upsert_profile",
        lambda provider, prof: saved.update({provider: prof}),
    )

    def _failing_refresher(prof):
        raise RuntimeError("refresh denied")

    monkeypatch.setitem(auth_store.REFRESHERS, "openai", _failing_refresher)

    with pytest.raises(RuntimeError):
        auth_store.get_token("openai")

    assert saved["openai"]["last_refresh_error"] == "refresh denied"
    assert isinstance(saved["openai"]["last_refresh_at"], int)


def test_complete_minimax_oauth_delegates_to_status(monkeypatch):
    called: dict = {}

    def _fake_status(provider, state):
        called["provider"] = provider
        called["state"] = state
        return {"status": "complete"}

    monkeypatch.setattr(api_core, "get_auth_provider_oauth_status", _fake_status)
    monkeypatch.setattr(
        api_core, "_build_auth_provider_payload",
        lambda p: {"status": "active"},
    )

    body = api_core.AuthProviderOAuthCompleteBody(state="st1")
    result = api_core.complete_auth_provider_oauth("minimax", body)
    assert result["ok"] is True
    assert called == {"provider": "minimax", "state": "st1"}


def _minimax_session(monkeypatch, **overrides):
    base = {
        "created_at": time.time(),
        "code_verifier": "v",
        "user_code": "USER123",
        "interval": 2,
        "max_attempts": 300,
    }
    base.update(overrides)
    sessions = {"minimax": {"st1": base}}
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", sessions)


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def test_minimax_status_pending(monkeypatch):
    _minimax_session(monkeypatch)
    monkeypatch.setattr(
        api_core.httpx, "post",
        lambda *a, **k: _Resp(400, {"error": "authorization_pending"}),
    )
    result = api_core.get_auth_provider_oauth_status("minimax", "st1")
    assert result["status"] == "awaiting_user"


def test_minimax_status_pending_from_nested_error_on_non_400(monkeypatch):
    _minimax_session(monkeypatch)
    monkeypatch.setattr(
        api_core.httpx, "post",
        lambda *a, **k: _Resp(403, {"error": {"code": "authorization_pending"}}),
    )
    result = api_core.get_auth_provider_oauth_status("minimax", "st1")
    assert result["status"] == "awaiting_user"


def test_minimax_status_slow_down_returns_interval(monkeypatch):
    _minimax_session(monkeypatch, interval=2)
    monkeypatch.setattr(
        api_core.httpx, "post",
        lambda *a, **k: _Resp(400, {"error": "slow_down"}),
    )
    result = api_core.get_auth_provider_oauth_status("minimax", "st1")
    assert result["status"] == "slow_down"
    assert result["interval"] >= 3


def test_minimax_status_complete_persists_profile(monkeypatch):
    _minimax_session(monkeypatch)
    monkeypatch.setattr(
        api_core.httpx, "post",
        lambda *a, **k: _Resp(200, {
            "access_token": "tok",
            "refresh_token": "ref",
            "expires_in": 3600,
        }),
    )
    saved: dict = {}
    monkeypatch.setattr(
        api_core, "upsert_profile",
        lambda provider, profile: saved.update({provider: profile}),
    )

    result = api_core.get_auth_provider_oauth_status("minimax", "st1")
    assert result["status"] == "complete"
    assert saved["minimax"]["access"] == "tok"


def test_minimax_status_complete_persists_nested_token_payload(monkeypatch):
    _minimax_session(monkeypatch)
    monkeypatch.setattr(
        api_core.httpx, "post",
        lambda *a, **k: _Resp(200, {
            "data": {
                "access_token": "nested-tok",
                "refresh_token": "nested-ref",
                "expires_in": 3600,
            }
        }),
    )
    saved: dict = {}
    monkeypatch.setattr(
        api_core, "upsert_profile",
        lambda provider, profile: saved.update({provider: profile}),
    )

    result = api_core.get_auth_provider_oauth_status("minimax", "st1")

    assert result["status"] == "complete"
    assert saved["minimax"]["access"] == "nested-tok"
    assert saved["minimax"]["refresh"] == "nested-ref"


def test_minimax_status_missing_token_keeps_waiting(monkeypatch):
    _minimax_session(monkeypatch)
    monkeypatch.setattr(
        api_core.httpx, "post",
        lambda *a, **k: _Resp(200, {"code": "authorization_pending"}),
    )

    result = api_core.get_auth_provider_oauth_status("minimax", "st1")

    assert result["status"] == "awaiting_user"


def test_minimax_status_denied(monkeypatch):
    _minimax_session(monkeypatch)
    monkeypatch.setattr(
        api_core.httpx, "post",
        lambda *a, **k: _Resp(400, {"error": "access_denied"}),
    )
    result = api_core.get_auth_provider_oauth_status("minimax", "st1")
    assert result["status"] == "denied"


def test_complete_openai_oauth_uses_stored_verifier_when_body_omits_it(monkeypatch):
    sessions = {"openai": {"st1": {
        "created_at": time.time(),
        "code_verifier": "from-session",
    }}}
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", sessions)
    monkeypatch.setattr(api_core, "upsert_profile", lambda *a, **k: None)

    captured: dict = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "tok", "expires_in": 3600}

    def _fake_post(url, data, headers, timeout):
        captured["data"] = data
        return _FakeResp()

    monkeypatch.setattr(api_core.httpx, "post", _fake_post)

    api_core._complete_openai_oauth("st1", "code-xyz", None)
    assert captured["data"]["code_verifier"] == "from-session"


def test_prune_shuts_down_listeners_on_expired_sessions(monkeypatch):
    shutdown_calls: list[str] = []

    class _RecordingListener:
        def __init__(self, name: str):
            self.name = name

        def shutdown(self):
            shutdown_calls.append(self.name)

    sessions = {
        "openai": {
            "stale": {
                "created_at": 0.0,  # ancient
                "listener": _RecordingListener("stale"),
            },
            "fresh": {
                "created_at": time.time(),
                "listener": _RecordingListener("fresh"),
            },
        }
    }
    monkeypatch.setattr(api_core, "_AUTH_OAUTH_SESSIONS", sessions)

    api_core._prune_auth_oauth_sessions()

    assert shutdown_calls == ["stale"]
    assert "stale" not in sessions["openai"]
    assert "fresh" in sessions["openai"]
