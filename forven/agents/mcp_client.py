"""MCP (Model Context Protocol) client — Hermes-inspired Phase 4.

Forven speaks MCP as a *client* so external servers (filesystem, git,
sqlite, custom user servers) can expose tools that agents call through
the same `tool_registry` flow as native Forven tools.

Design constraints (from the upgrade spec, locked):
- **No background services.** Sessions are per-call, opened on demand
  and closed when the work finishes. The Tauri sidecar lifecycle owns
  any orphan subprocesses; if Forven dies, MCP children die with it.
- **Brain-only memory.** Nothing in this module persists agent state.
- **Tauri-buildable.** Pure stdlib + already-vendored httpx; no Docker,
  no extra packaging.
- **Subprocess env scrubbing.** stdio servers spawn through
  `forven.security.env_allowlist.build_subprocess_env`. Caller-supplied
  `env_json` is allowed through but DYLD_*/LD_PRELOAD-shaped names are
  stripped first — defense-in-depth against prompt-injected configs.

Wire format:
- stdio: line-delimited JSON-RPC 2.0 (one message per `\\n`).
- http: JSON-RPC 2.0 in POST body, response in body.

Handshake follows the MCP 2024-11-05 specification: ``initialize`` →
read response → ``notifications/initialized``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from forven.db import get_db, get_db_best_effort
from forven.security.env_allowlist import build_subprocess_env

log = logging.getLogger("forven.agents.mcp_client")


PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "Forven", "version": "0.1"}

DEFAULT_INIT_TIMEOUT = 30.0
DEFAULT_CALL_TIMEOUT = 60.0


def _timeout(default: float) -> float:
    raw = os.environ.get("FORVEN_MCP_TIMEOUT")
    if not raw:
        return default
    try:
        v = float(raw)
        return v if v > 0 else default
    except ValueError:
        return default


# Names that must never reach a child process even when caller-supplied.
# These are the classic linker/loader injection points: LD_PRELOAD on
# Linux, DYLD_INSERT_LIBRARIES on macOS. A prompt-injected `env_json`
# entry could otherwise hijack the child without touching its argv.
_FORBIDDEN_ENV = re.compile(r"^(LD_PRELOAD|LD_LIBRARY_PATH|LD_AUDIT|DYLD_.*)$")


def _scrub_user_env(env_json: dict[str, str]) -> dict[str, str]:
    """Drop forbidden injection keys from caller-supplied env."""
    out: dict[str, str] = {}
    for k, v in (env_json or {}).items():
        if _FORBIDDEN_ENV.match(str(k)):
            log.warning("mcp_client: dropping forbidden env key %r", k)
            continue
        out[str(k)] = str(v)
    return out


# ---------------------------------------------------------------------------
# Config + session state
# ---------------------------------------------------------------------------

@dataclass
class MCPServerConfig:
    """In-memory mirror of an ``mcp_servers`` row."""
    name: str
    transport: str  # 'stdio' | 'http'
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    tools_include: list[str] | None = None
    tools_exclude: list[str] = field(default_factory=list)


@dataclass
class MCPSession:
    """Open MCP session — either subprocess (stdio) or HTTP."""
    config: MCPServerConfig
    transport: str
    proc: asyncio.subprocess.Process | None = None
    http_client: httpx.AsyncClient | None = None
    server_info: dict = field(default_factory=dict)
    server_protocol_version: str = ""
    _next_id: int = 1

    def _new_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _row_to_config(row: Any) -> MCPServerConfig:
    return MCPServerConfig(
        name=row["name"],
        transport=row["transport"],
        command=row["command"],
        args=json.loads(row["args_json"] or "[]"),
        env=json.loads(row["env_json"] or "{}"),
        url=row["url"],
        headers=json.loads(row["headers_json"] or "{}"),
        enabled=bool(row["enabled"]),
        tools_include=(
            json.loads(row["tools_include_json"])
            if row["tools_include_json"] else None
        ),
        tools_exclude=json.loads(row["tools_exclude_json"] or "[]"),
    )


def load_server_config(name: str) -> MCPServerConfig | None:
    """Read an ``mcp_servers`` row by name; return None if missing."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM mcp_servers WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_config(row)


def record_status(name: str, ok: bool, error: str | None = None) -> None:
    """Best-effort status update; never raises."""
    status = "ok" if ok else "error"
    err = (error or "")[:500] if not ok else None
    try:
        with get_db_best_effort(timeout_seconds=0.5) as conn:
            conn.execute(
                "UPDATE mcp_servers SET last_status = ?, "
                "last_status_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), "
                "last_error = ? WHERE name = ?",
                (status, err, name),
            )
    except Exception as exc:
        log.debug("mcp_client.record_status(%s) skipped: %s", name, exc)


# ---------------------------------------------------------------------------
# JSON-RPC framing
# ---------------------------------------------------------------------------

class MCPProtocolError(RuntimeError):
    """Server returned a JSON-RPC error or malformed payload."""


async def _stdio_send(session: MCPSession, msg: dict) -> None:
    proc = session.proc
    if proc is None or proc.stdin is None:
        raise MCPProtocolError("stdio session has no stdin")
    line = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
    proc.stdin.write(line)
    await proc.stdin.drain()


async def _stdio_read_response(session: MCPSession, expected_id: int, timeout: float) -> dict:
    """Read JSON-RPC messages until one matches expected_id.

    Notifications (no ``id``) are logged and skipped — we don't
    subscribe to them in Phase 4 (deferred to a later phase along with
    sampling/elicitation).
    """
    proc = session.proc
    if proc is None or proc.stdout is None:
        raise MCPProtocolError("stdio session has no stdout")

    async def _read_one() -> dict:
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise MCPProtocolError("stdio server closed stream before response")
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError as exc:
                raise MCPProtocolError(f"non-JSON line from server: {exc}") from exc
            if not isinstance(msg, dict):
                continue
            if "id" in msg and msg.get("id") == expected_id:
                return msg
            # notification or unrelated response — log and keep reading
            log.debug("mcp_client: skipping unrelated message: %s", msg)

    return await asyncio.wait_for(_read_one(), timeout=timeout)


async def _http_request(session: MCPSession, msg: dict, timeout: float) -> dict:
    client = session.http_client
    if client is None:
        raise MCPProtocolError("http session has no client")
    url = session.config.url or ""
    if not url:
        raise MCPProtocolError("http session has no url")
    headers = {"content-type": "application/json", **session.config.headers}
    resp = await client.post(url, json=msg, headers=headers, timeout=timeout)
    resp.raise_for_status()
    try:
        body = resp.json()
    except ValueError as exc:
        raise MCPProtocolError(f"non-JSON HTTP response: {exc}") from exc
    if not isinstance(body, dict):
        raise MCPProtocolError(f"unexpected HTTP body type: {type(body).__name__}")
    return body


async def _rpc_call(session: MCPSession, method: str, params: dict | None, timeout: float) -> dict:
    """Send one JSON-RPC request, return the ``result`` dict.

    Raises MCPProtocolError on JSON-RPC error.
    """
    rid = session._new_id()
    msg: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params

    if session.transport == "stdio":
        await _stdio_send(session, msg)
        response = await _stdio_read_response(session, rid, timeout)
    else:
        response = await _http_request(session, msg, timeout)

    if "error" in response:
        err = response["error"] or {}
        code = err.get("code", "?")
        message = err.get("message", "unknown error")
        raise MCPProtocolError(f"{method} failed [{code}]: {message}")
    return response.get("result") or {}


async def _rpc_notify(session: MCPSession, method: str, params: dict | None = None) -> None:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if session.transport == "stdio":
        await _stdio_send(session, msg)
    else:
        # Notifications don't expect a body but we still post.
        client = session.http_client
        if client is None or not session.config.url:
            return
        try:
            await client.post(
                session.config.url,
                json=msg,
                headers={"content-type": "application/json", **session.config.headers},
                timeout=5.0,
            )
        except Exception as exc:
            log.debug("mcp_client: notify %s failed (non-fatal): %s", method, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def connect(config: MCPServerConfig) -> MCPSession:
    """Open a transport and run the MCP initialize handshake.

    Caller is responsible for ``close(session)`` when done. Records
    success/failure to the ``mcp_servers`` row.
    """
    session = MCPSession(config=config, transport=config.transport)
    init_timeout = _timeout(DEFAULT_INIT_TIMEOUT)

    try:
        if config.transport == "stdio":
            if not config.command:
                raise MCPProtocolError("stdio config requires command")
            scrubbed_extra = _scrub_user_env(config.env)
            env = build_subprocess_env(extra=scrubbed_extra)
            session.proc = await asyncio.create_subprocess_exec(
                config.command,
                *config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        elif config.transport == "http":
            if not config.url:
                raise MCPProtocolError("http config requires url")
            # Phase 5 / P5-T02b: SSRF guard on MCP HTTP servers.
            # An operator-configured MCP URL pointing at 169.254.169.254 or a
            # link-local address is rejected. Localhost/127.0.0.1 are allowed
            # only when FORVEN_ALLOW_LOCAL_MCP=1 (developer opt-in).
            from forven.security.url_safety import (
                UnsafeUrlError,
                validate_public_url,
            )
            allow_local = bool(os.environ.get("FORVEN_ALLOW_LOCAL_MCP", "").strip())
            try:
                if allow_local:
                    # Use a permissive variant: only block obvious metadata
                    # endpoints, allow loopback / private ranges since the
                    # operator opted in.
                    from urllib.parse import urlparse
                    parsed = urlparse(config.url)
                    if parsed.scheme not in ("http", "https"):
                        raise UnsafeUrlError(
                            f"scheme not allowed: {parsed.scheme!r}"
                        )
                    host = (parsed.hostname or "").strip().lower()
                    if host in {"metadata.google.internal", "metadata.goog"}:
                        raise UnsafeUrlError(
                            f"hostname {host!r} is forbidden"
                        )
                    # Explicitly block link-local IMDS even with opt-in.
                    if host in {"169.254.169.254", "fd00:ec2::254"}:
                        raise UnsafeUrlError(
                            "cloud metadata endpoint is forbidden"
                        )
                else:
                    # SECURITY (audit 2026-06-22, L6): resolve DNS too, so a
                    # hostname that resolves to an RFC1918/loopback address (or a
                    # DNS-rebinding record) is rejected — static-only validation
                    # missed that. validate_public_url_static remains imported for
                    # the allow_local diagnostics path above.
                    validate_public_url(config.url)
            except UnsafeUrlError as exc:
                raise MCPProtocolError(f"refused unsafe MCP URL: {exc}") from exc
            session.http_client = httpx.AsyncClient(timeout=init_timeout)
        else:
            raise MCPProtocolError(f"unknown transport: {config.transport}")

        result = await _rpc_call(
            session,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": CLIENT_INFO,
            },
            timeout=init_timeout,
        )

        server_proto = str(result.get("protocolVersion", "")).strip()
        if not server_proto:
            raise MCPProtocolError("server did not return protocolVersion")
        session.server_protocol_version = server_proto
        session.server_info = result.get("serverInfo") or {}

        # Acknowledge handshake.
        await _rpc_notify(session, "notifications/initialized")

        record_status(config.name, ok=True)
        return session
    except Exception as exc:
        await close(session)
        record_status(config.name, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise


async def list_tools(session: MCPSession) -> list[dict]:
    """Return the server's tool list (raw MCP shape)."""
    result = await _rpc_call(session, "tools/list", None, timeout=_timeout(DEFAULT_CALL_TIMEOUT))
    tools = result.get("tools") or []
    if not isinstance(tools, list):
        return []
    return [t for t in tools if isinstance(t, dict)]


async def call_tool(session: MCPSession, tool_name: str, arguments: dict) -> str:
    """Invoke a tool. Returns a single concatenated text result.

    MCP tool results are content-block lists ([{type:"text",text:"..."},
    {type:"image",...}]). Phase 4 surfaces only text blocks; image/blob
    handling lands when an agent UI needs it.
    """
    result = await _rpc_call(
        session,
        "tools/call",
        {"name": tool_name, "arguments": arguments or {}},
        timeout=_timeout(DEFAULT_CALL_TIMEOUT),
    )
    is_error = bool(result.get("isError"))
    content = result.get("content") or []
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
    text = "\n".join(p for p in parts if p).strip()
    if is_error:
        return f"[mcp error] {text or 'tool reported isError without text'}"
    return text


async def close(session: MCPSession) -> None:
    """Tear down a session — never raises."""
    if session.proc is not None:
        proc = session.proc
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass
        session.proc = None
    if session.http_client is not None:
        try:
            await session.http_client.aclose()
        except Exception:
            pass
        session.http_client = None


# ---------------------------------------------------------------------------
# Tool registry integration (Phase 4 / P4-T06)
# ---------------------------------------------------------------------------

def _mcp_tool_name(server: str, tool: str) -> str:
    """Forven-namespaced tool name: ``mcp_<server>_<tool>``.

    Server and tool names from MCP spec are typically [a-z0-9_-]; we
    keep them as-is so the namespace stays human-recognizable.
    """
    return f"mcp_{server}_{tool}"


def _filter_tools(
    raw: list[dict],
    include: list[str] | None,
    exclude: list[str],
) -> list[dict]:
    inc = set(include) if include else None
    exc = set(exclude or [])
    out: list[dict] = []
    for t in raw:
        name = str(t.get("name", "")).strip()
        if not name:
            continue
        if inc is not None and name not in inc:
            continue
        if name in exc:
            continue
        out.append(t)
    return out


async def register_server_tools(name: str) -> int:
    """Connect, list tools, register each under the global tool_registry.

    Returns the count of newly-registered tools. Records last_status to
    the row (success or failure). Does NOT raise on per-tool registration
    issues — startup must not be blocked by one bad server.
    """
    from forven.agents.tool_registry import _REGISTRY, ToolDef

    config = load_server_config(name)
    if config is None:
        log.warning("mcp_client.register: server %r not found", name)
        return 0
    if not config.enabled:
        log.info("mcp_client.register: server %r disabled, skipping", name)
        return 0

    session = await connect(config)
    try:
        raw_tools = await list_tools(session)
    finally:
        await close(session)

    filtered = _filter_tools(raw_tools, config.tools_include, config.tools_exclude)
    perms = frozenset({f"mcp:{name}"})

    registered = 0
    for tool in filtered:
        tool_name = str(tool.get("name", "")).strip()
        if not tool_name:
            continue
        registry_name = _mcp_tool_name(name, tool_name)
        description = str(tool.get("description", "") or f"MCP tool {tool_name} ({name})")
        schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {
            "type": "object", "properties": {},
        }
        handler = _make_mcp_handler(name, tool_name)
        _REGISTRY[registry_name] = ToolDef(
            name=registry_name,
            description=description,
            input_schema=schema,
            handler=handler,
            permissions=perms,
        )
        registered += 1

    log.info("mcp_client: registered %d tool(s) from server %r", registered, name)
    return registered


def _make_mcp_handler(server_name: str, tool_name: str):
    """Return an async tool-registry handler that opens a fresh session per call.

    The closure captures only strings (server_name, tool_name) — no
    mutable state — so concurrent calls are safe. A new subprocess /
    HTTP client is opened per invocation; if cost becomes a concern we
    can add per-turn caching at the registry level later.
    """
    async def handler(params: dict) -> str:
        config = load_server_config(server_name)
        if config is None:
            return f"[mcp error] server {server_name!r} not configured"
        try:
            session = await connect(config)
        except Exception as exc:
            return f"[mcp error] connect to {server_name!r} failed: {exc}"
        try:
            return await call_tool(session, tool_name, params or {})
        except MCPProtocolError as exc:
            return f"[mcp error] {exc}"
        finally:
            await close(session)

    return handler


def unregister_server_tools(name: str) -> int:
    """Remove all ``mcp_<name>_*`` entries from the tool registry.

    Returns the number removed. Safe to call when the server was never
    registered.
    """
    from forven.agents.tool_registry import _REGISTRY

    prefix = f"mcp_{name}_"
    removed = [k for k in _REGISTRY.keys() if k.startswith(prefix)]
    for key in removed:
        _REGISTRY.pop(key, None)
    if removed:
        log.info("mcp_client: unregistered %d tool(s) from server %r", len(removed), name)
    return len(removed)


async def register_all_enabled_servers() -> dict[str, int]:
    """Iterate enabled rows in mcp_servers and register each.

    Per-server failures are logged and suppressed — startup must not
    crash because one MCP server config is broken. Returns a
    ``{server_name: tool_count}`` map for diagnostics.
    """
    out: dict[str, int] = {}
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT name FROM mcp_servers WHERE enabled = 1"
            ).fetchall()
    except Exception as exc:
        log.warning("mcp_client.register_all: db query failed: %s", exc)
        return out

    for row in rows:
        server = str(row["name"])
        try:
            count = await register_server_tools(server)
            out[server] = count
        except Exception as exc:
            log.warning("mcp_client.register_all: %r failed: %s", server, exc)
            out[server] = 0
    return out


__all__ = [
    "MCPServerConfig",
    "MCPSession",
    "MCPProtocolError",
    "PROTOCOL_VERSION",
    "load_server_config",
    "record_status",
    "connect",
    "list_tools",
    "call_tool",
    "close",
    "register_server_tools",
    "unregister_server_tools",
    "register_all_enabled_servers",
]
