from __future__ import annotations

import logging
import os
import secrets
from typing import Iterable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse


log = logging.getLogger("forven.security")


_API_KEY_HEADER = "x-api-key"
_OPERATOR_KEY_HEADER = "x-operator-key"
_AUTHORIZATION_HEADER = "authorization"
_API_EXEMPT_PATH_PREFIXES = (
    "/api/health",
    "/api/webhooks/",
    # /api/shutdown has its own 127.0.0.1-only check and is called by the
    # local launcher/controller on close; keeping it auth-exempt lets a local
    # process trigger a graceful uvicorn teardown without needing the per-launch
    # key. Worst case: a local process DoS's our own app — annoying, not a
    # data/wallet risk.
    "/api/shutdown",
)

_TRUTHY = {"1", "true", "yes", "on"}


def _normalize_secret(value: object) -> str:
    return str(value or "").strip()


def _read_env_secret(name: str) -> str:
    return _normalize_secret(os.environ.get(name))


def _env_truthy(name: str) -> bool:
    return _normalize_secret(os.environ.get(name)).lower() in _TRUTHY


def _read_authorization_token(request: HTTPConnection) -> str:
    header = _normalize_secret(request.headers.get(_AUTHORIZATION_HEADER))
    if not header:
        return ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return _normalize_secret(token)


def _request_secret_matches(
    request: HTTPConnection,
    expected: str,
    *,
    header_names: Iterable[str],
    allow_authorization: bool = True,
) -> bool:
    normalized_expected = _normalize_secret(expected)
    if not normalized_expected:
        return True

    candidates: list[str] = []
    for header_name in header_names:
        candidate = _normalize_secret(request.headers.get(header_name))
        if candidate:
            candidates.append(candidate)
    if allow_authorization:
        bearer = _read_authorization_token(request)
        if bearer:
            candidates.append(bearer)

    return any(secrets.compare_digest(candidate, normalized_expected) for candidate in candidates)


def _is_api_key_exempt_path(path: str) -> bool:
    normalized_path = str(path or "").strip()
    if not normalized_path.startswith("/api/"):
        return True
    return any(normalized_path.startswith(prefix) for prefix in _API_EXEMPT_PATH_PREFIXES)


def _auth_required() -> bool:
    return _env_truthy("FORVEN_AUTH_REQUIRED")


def is_loopback_host(host: str) -> bool:
    """True if *host* keeps the API reachable only from the local machine."""
    h = (host or "").strip().lower().strip("[]")
    return h in {"127.0.0.1", "::1", "localhost", ""} or h.startswith("127.")


def resolved_bind_host() -> str:
    """The host uvicorn will actually bind to, mirroring the launcher precedence.

    SECURITY (audit 2026-06-22, M6): both start_all.sh and start_all.ps1 accept
    ``FORVEN_HOST`` as a ``--host`` fallback, but the fail-closed guard used to
    read only ``FORVEN_BIND_HOST`` — so ``FORVEN_HOST=0.0.0.0`` with no API key
    would boot an unauthenticated API on a public interface, guard none the wiser.
    Resolve the same way the launchers do so the guard can never diverge from the
    actual bind.
    """
    for name in ("FORVEN_BIND_HOST", "FORVEN_HOST"):
        value = _normalize_secret(os.environ.get(name))
        if value:
            return value
    return "127.0.0.1"


def assert_safe_bind_host(bind_host: str) -> None:
    """Refuse to start an UNAUTHENTICATED API on a non-loopback interface.

    Auth is fail-open on loopback, which is safe for the documented single-user
    localhost setup. But binding to 0.0.0.0 / a LAN IP without an API key would
    expose the code-execution and order-management endpoints to the network, so
    we fail closed there: an exposed bind must come with FORVEN_API_KEY (or an
    explicit FORVEN_AUTH_REQUIRED=true).
    """
    if is_loopback_host(bind_host):
        return
    if _read_env_secret("FORVEN_API_KEY") or _auth_required():
        return
    raise RuntimeError(
        f"Refusing to start: the API is bound to {bind_host!r} (reachable beyond "
        "localhost) but FORVEN_API_KEY is not set. Set FORVEN_API_KEY (and "
        "FORVEN_OPERATOR_KEY) in your .env, or bind to 127.0.0.1."
    )


def assert_auth_keys_configured() -> None:
    """Call at app startup. Refuse to start if auth is required but unset."""
    api_key = _read_env_secret("FORVEN_API_KEY")
    operator_key = _read_env_secret("FORVEN_OPERATOR_KEY")
    required = _auth_required()
    if required and not api_key:
        raise RuntimeError(
            "FORVEN_AUTH_REQUIRED=true but FORVEN_API_KEY is unset. "
            "Set FORVEN_API_KEY in .env or unset FORVEN_AUTH_REQUIRED for dev."
        )
    if required and not operator_key:
        raise RuntimeError(
            "FORVEN_AUTH_REQUIRED=true but FORVEN_OPERATOR_KEY is unset. "
            "Set FORVEN_OPERATOR_KEY in .env or unset FORVEN_AUTH_REQUIRED for dev."
        )
    if not api_key:
        log.warning(
            "FORVEN_API_KEY is unset — API is unauthenticated. "
            "Set FORVEN_AUTH_REQUIRED=true to refuse start without keys."
        )
    if not operator_key:
        log.warning(
            "FORVEN_OPERATOR_KEY is unset — operator endpoints are unauthenticated. "
            "Set FORVEN_AUTH_REQUIRED=true to refuse start without keys."
        )
    # Fail closed if the API is exposed beyond loopback without a key. Use the
    # launcher-aware resolver (FORVEN_BIND_HOST → FORVEN_HOST → 127.0.0.1) so the
    # FORVEN_HOST alias cannot smuggle a public bind past the guard (M6).
    assert_safe_bind_host(resolved_bind_host())


def require_api_access(request: HTTPConnection) -> None:
    expected_api_key = _read_env_secret("FORVEN_API_KEY")
    if not expected_api_key:
        if _auth_required():
            raise HTTPException(status_code=503, detail="API key not configured")
        return
    if _request_secret_matches(request, expected_api_key, header_names=(_API_KEY_HEADER,)):
        return
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_operator_access(request: HTTPConnection) -> None:
    require_api_access(request)
    expected_operator_key = _read_env_secret("FORVEN_OPERATOR_KEY")
    if not expected_operator_key:
        if _auth_required():
            raise HTTPException(status_code=503, detail="Operator key not configured")
        return
    if _request_secret_matches(request, expected_operator_key, header_names=(_OPERATOR_KEY_HEADER,)):
        return
    raise HTTPException(status_code=401, detail="Invalid or missing operator key")


async def require_api_access_ws(ws: HTTPConnection) -> bool:
    """Authorize a WebSocket handshake (audit 2026-06-22, L3).

    Starlette's BaseHTTPMiddleware only sees ``http`` scopes, so the live WS
    endpoints bypass ApiKeyMiddleware entirely. This restores parity: fail-open
    when no FORVEN_API_KEY is set (the default localhost setup), but when a key
    IS configured, require it via a ``?key=``/``?api_key=`` query param, the
    ``x-api-key`` header, or a Bearer token. Returns True if allowed; on mismatch
    it closes the socket (1008) and returns False so the caller just returns.
    """
    expected = _read_env_secret("FORVEN_API_KEY")
    if not expected:
        return True

    candidates: list[str] = []
    try:
        params = ws.query_params
        for name in ("key", "api_key", "apikey"):
            value = _normalize_secret(params.get(name))
            if value:
                candidates.append(value)
    except Exception:  # noqa: BLE001
        pass
    try:
        header_value = _normalize_secret(ws.headers.get(_API_KEY_HEADER))
        if header_value:
            candidates.append(header_value)
        bearer = _read_authorization_token(ws)
        if bearer:
            candidates.append(bearer)
    except Exception:  # noqa: BLE001
        pass

    if any(secrets.compare_digest(candidate, expected) for candidate in candidates):
        return True
    try:
        await ws.close(code=1008)
    except Exception:  # noqa: BLE001
        pass
    return False


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method.upper() == "OPTIONS" or _is_api_key_exempt_path(request.url.path):
            return await call_next(request)
        try:
            require_api_access(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)


def _normalize_origin(value: object) -> str:
    raw = _normalize_secret(value)
    if not raw or raw == "*":
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return raw.rstrip("/")


def get_allowed_cors_origins() -> list[str]:
    configured = _normalize_secret(os.environ.get("FORVEN_CORS_ORIGINS"))
    if configured:
        values = configured.split(",")
    else:
        frontend_port = _normalize_secret(os.environ.get("FRONTEND_PORT")) or "5173"
        api_port = _normalize_secret(os.environ.get("FORVEN_PORT")) or "8003"
        values = [
            f"http://127.0.0.1:{frontend_port}",
            f"http://localhost:{frontend_port}",
            f"http://127.0.0.1:{api_port}",
            f"http://localhost:{api_port}",
            _normalize_secret(os.environ.get("FORVEN_CLIENT_BASE")),
        ]

    origins: list[str] = []
    seen: set[str] = set()
    for value in values:
        origin = _normalize_origin(value)
        if not origin or origin in seen:
            continue
        seen.add(origin)
        origins.append(origin)
    return origins
