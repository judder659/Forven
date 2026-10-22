"""Run a whole agent task through the operator's own ``claude`` program.

With API providers the runner drives the tool loop itself, one model call per
round. Claude Code runs its own loop, so here Forven SERVES its tools instead:
a tiny MCP server on a random localhost port lists the task's tools, and every
tool call ``claude`` makes is handed back to the runner's own task, which runs
it with the same ``execute_tool`` path, execution state, ownership checks and
transcript as any other provider.

The MCP server only answers requests carrying this run's random bearer token
and only listens on 127.0.0.1. Claude Code's own shell, file and web tools are
switched off (``--tools ""``), so the model can do nothing but call Forven's
tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from forven.providers import claude_code

log = logging.getLogger("forven.agents.claude_code_runner")

MCP_SERVER_NAME = "forven"
_MCP_PROTOCOL_VERSION = "2025-06-18"
_TOOL_RESULT_CHARS = 5000
_MAX_REQUEST_BYTES = 8 * 1024 * 1024


class _ToolServer:
    """Minimal MCP "streamable HTTP" server answering with plain JSON.

    Each ``tools/call`` is put on ``requests`` as ``(name, arguments, future)``
    and answered once the runner sets the future, so the tool runs in the
    runner's task and sees its context variables.
    """

    def __init__(self, tools: list[dict], requests: asyncio.Queue) -> None:
        self.tools = tools
        self.requests = requests
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2)
            except (TimeoutError, asyncio.TimeoutError):
                pass

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def mcp_config(self) -> dict:
        return {
            "mcpServers": {
                MCP_SERVER_NAME: {
                    "type": "http",
                    "url": self.url,
                    "headers": {"Authorization": f"Bearer {self.token}"},
                }
            }
        }

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                request_line = await reader.readline()
                if not request_line:
                    break
                parts = request_line.decode("latin-1").split()
                if len(parts) < 2:
                    break
                method = parts[0].upper()
                headers: dict[str, str] = {}
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                    key, _, value = line.decode("latin-1").partition(":")
                    headers[key.strip().lower()] = value.strip()
                length = int(headers.get("content-length") or 0)
                if length > _MAX_REQUEST_BYTES:
                    await self._respond(writer, 413, None)
                    break
                body = await reader.readexactly(length) if length else b""
                status, payload = await self._dispatch(method, headers, body)
                await self._respond(writer, status, payload)
                if headers.get("connection", "").lower() == "close":
                    break
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            writer.close()

    async def _respond(self, writer: asyncio.StreamWriter, status: int, payload: object) -> None:
        reasons = {200: "OK", 202: "Accepted", 401: "Unauthorized", 405: "Method Not Allowed", 413: "Payload Too Large"}
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        head = [f"HTTP/1.1 {status} {reasons.get(status, 'Error')}", f"Content-Length: {len(body)}"]
        if payload is not None:
            head.append("Content-Type: application/json")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
        await writer.drain()

    async def _dispatch(self, method: str, headers: dict[str, str], body: bytes) -> tuple[int, object]:
        if not secrets.compare_digest(headers.get("authorization", ""), f"Bearer {self.token}"):
            return 401, {"error": "unauthorized"}
        if method != "POST":
            # No server-initiated stream; the spec allows refusing GET.
            return 405, None
        try:
            message = json.loads(body or b"null")
        except ValueError:
            return 200, _rpc_error(None, -32700, "Parse error")
        if isinstance(message, list):
            replies = [r for r in [await self._rpc(m) for m in message] if r is not None]
            return (200, replies) if replies else (202, None)
        reply = await self._rpc(message)
        return (200, reply) if reply is not None else (202, None)

    async def _rpc(self, message: object) -> dict | None:
        if not isinstance(message, dict):
            return _rpc_error(None, -32600, "Invalid request")
        msg_id = message.get("id")
        method = message.get("method")
        if msg_id is None:
            return None  # notification, or a response to us: nothing to answer
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            return _rpc_result(msg_id, {
                "protocolVersion": str(params.get("protocolVersion") or _MCP_PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": MCP_SERVER_NAME, "version": "1"},
            })
        if method == "ping":
            return _rpc_result(msg_id, {})
        if method == "tools/list":
            return _rpc_result(msg_id, {"tools": [
                {
                    "name": tool["name"],
                    "description": str(tool.get("description") or ""),
                    "inputSchema": tool.get("input_schema") or {"type": "object", "properties": {}},
                }
                for tool in self.tools
            ]})
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            await self.requests.put(("tool", name, arguments, future))
            text, is_error = await future
            return _rpc_result(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
        return _rpc_error(msg_id, -32601, f"Method not found: {method}")


def _rpc_result(msg_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


ToolExecutor = Callable[[str, dict], Awaitable[str]]
EventWriter = Callable[..., None]


async def run_agent_task(
    *,
    model_id: str,
    messages: list[dict],
    system: str,
    tools: list[dict],
    max_turns: int,
    execute: ToolExecutor,
    on_tool_start: Callable[[], None] | None = None,
    on_tool_done: Callable[[str, dict, str], None] | None = None,
    on_assistant: Callable[[str, str | None, dict], None] | None = None,
    on_activity: Callable[[], None] | None = None,
) -> tuple[str, dict, str]:
    """Run one agent task in ``claude``. Returns ``(final_text, usage, stop)``.

    ``stop`` is the result subtype: ``"success"``, or ``"error_max_turns"``
    when the task used all its turns. Other failures raise ClaudeCodeError.
    Callbacks let the runner keep its transcript, checkpoint and progress
    watchdog current without this module importing the runner.
    """
    cli = claude_code.require_ready()
    tool_names = {tool["name"] for tool in tools}
    requests: asyncio.Queue = asyncio.Queue()
    server = _ToolServer(tools, requests)
    await server.start()
    run: claude_code.Run | None = None
    # The process is killed before its working directory is removed (the
    # finally below runs first), so a cancelled task never leaves claude
    # running in a folder Windows then refuses to delete.
    with tempfile.TemporaryDirectory(prefix="forven-claude-", ignore_cleanup_errors=True) as workdir:
        try:
            system_file = Path(workdir) / "system.md"
            system_file.write_text(system or "You are a Forven agent.", encoding="utf-8")
            mcp_file = Path(workdir) / "mcp.json"
            mcp_file.write_text(json.dumps(server.mcp_config()), encoding="utf-8")
            args = claude_code.base_args(cli, model_id, str(system_file)) + [
                "--output-format", "stream-json", "--verbose",
                "--mcp-config", str(mcp_file),
                "--allowedTools", f"mcp__{MCP_SERVER_NAME}",
                "--permission-mode", "dontAsk",
                "--max-turns", str(max(1, int(max_turns))),
            ]
            run = claude_code.Run(args, claude_code.messages_to_prompt(messages), workdir)
            loop = asyncio.get_running_loop()
            run.start(on_line=lambda line: loop.call_soon_threadsafe(requests.put_nowait, ("line", line)))

            result: dict | None = None
            last_text = ""
            while True:
                event = await requests.get()
                if event[0] == "tool":
                    _, name, arguments, future = event
                    if name not in tool_names:
                        future.set_result((f"Error: unknown tool {name!r}.", True))
                        continue
                    if on_tool_start is not None:
                        on_tool_start()
                    try:
                        output = str(await execute(name, arguments))
                        future.set_result((output[:_TOOL_RESULT_CHARS], False))
                    except Exception as exc:  # noqa: BLE001 - the model sees the failure, like other providers
                        output = f"Error: {exc}"
                        future.set_result((output, True))
                    if on_tool_done is not None:
                        on_tool_done(name, arguments, output)
                    continue

                line = event[1]
                if line is None:
                    break
                if on_activity is not None:
                    on_activity()
                data = claude_code._parse_json_object(line)
                if not data:
                    continue
                kind = data.get("type")
                if kind == "assistant":
                    message = data.get("message") if isinstance(data.get("message"), dict) else {}
                    blocks = message.get("content") if isinstance(message.get("content"), list) else []
                    text = "\n".join(
                        b["text"] for b in blocks
                        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                    ).strip()
                    reasoning = "\n".join(
                        b["thinking"] for b in blocks
                        if isinstance(b, dict) and b.get("type") == "thinking" and isinstance(b.get("thinking"), str)
                    ).strip() or None
                    if text:
                        last_text = text
                    if (text or reasoning) and on_assistant is not None:
                        on_assistant(text, reasoning, message.get("usage") if isinstance(message.get("usage"), dict) else {})
                elif kind == "result":
                    result = data
        finally:
            if run is not None:
                run.kill()
            await server.close()

    if result is None:
        raise claude_code.error_from_result(None, stderr=run.stderr if run else "", returncode=run.returncode if run else None)
    subtype = str(result.get("subtype") or "")
    if subtype == "error_max_turns":
        return last_text, claude_code.usage_from_result(result), subtype
    if result.get("is_error") or subtype != "success":
        raise claude_code.error_from_result(result, stderr=run.stderr, returncode=run.returncode)
    final = result.get("result") if isinstance(result.get("result"), str) else ""
    return (final or last_text), claude_code.usage_from_result(result), subtype
