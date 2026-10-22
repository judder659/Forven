"""Claude Code provider: Forven runs the operator's own signed-in ``claude``.

A fake ``claude`` script stands in for the real program, so these tests need
no Claude login. It answers ``auth status``, one-shot ``-p`` calls, and agent
runs, where it calls Forven's tools over the per-run MCP server exactly as the
real program does.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap

import pytest

import forven.ai as ai
from forven import api_core as ac
from forven import model_routing as mr
from forven.agents import claude_code_runner
from forven.agents.providers import ClaudeCodeProvider, get_provider
from forven.auth import store
from forven.cost_pricing import estimate_cost_usd
from forven.providers import claude_code

FAKE_CLAUDE = r'''
import json, os, sys, urllib.request

args = sys.argv[1:]
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\n")

if args[:2] == ["auth", "status"]:
    logged_in = os.environ.get("FAKE_CLAUDE_LOGGED_IN", "1") == "1"
    print(json.dumps({"loggedIn": logged_in, "authMethod": "claude.ai" if logged_in else "none"}))
    sys.exit(0 if logged_in else 1)

prompt = sys.stdin.read()
fmt = args[args.index("--output-format") + 1]
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
usage = {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3}

if fmt == "json":
    if mode == "limit":
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": "Claude AI usage limit reached|1792700000"}))
        sys.exit(1)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "echo: " + prompt, "usage": usage}))
    sys.exit(0)

config = json.loads(open(args[args.index("--mcp-config") + 1], encoding="utf-8").read())
server = config["mcpServers"]["forven"]

def rpc(method, params, msg_id=1, token=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    headers["Authorization"] = token if token is not None else server["headers"]["Authorization"]
    body = json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}).encode()
    req = urllib.request.Request(server["url"], data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None

def emit(obj):
    print(json.dumps(obj), flush=True)

status, _ = rpc("tools/list", {}, token="Bearer wrong")
emit({"type": "system", "subtype": "init", "bad_token_status": status})
rpc("initialize", {"protocolVersion": "2025-06-18"})
_, listed = rpc("tools/list", {}, 2)
names = [t["name"] for t in listed["result"]["tools"]]
emit({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "plan"},
      {"type": "text", "text": "tools: " + ",".join(names)}]}})
if mode == "max_turns":
    emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "usage": usage})
    sys.exit(1)
_, called = rpc("tools/call", {"name": names[0], "arguments": {"coin": "BTC"}}, 3)
_, missing = rpc("tools/call", {"name": "not_a_tool", "arguments": {}}, 4)
text = called["result"]["content"][0]["text"]
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "got " + text}]}})
emit({"type": "result", "subtype": "success", "is_error": False,
      "result": "done: " + text + " / " + missing["result"]["content"][0]["text"], "usage": usage})
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    launcher = tmp_path / "claude"
    launcher.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        exec "{sys.executable}" "{script}" "$@"
    """), encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FORVEN_CLAUDE_CODE_PATH", str(launcher))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_LOGGED_IN", "1")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    claude_code.clear_auth_cache()
    yield log
    claude_code.clear_auth_cache()


pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake claude launcher is a POSIX shell script")


def _calls(log) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_registered_like_any_other_provider():
    assert isinstance(get_provider("claude-code"), ClaudeCodeProvider)
    assert "claude-code" in mr._SUPPORTED_PROVIDERS
    assert mr.get_default_model_for_provider("claude-code") == "sonnet"
    assert ai.normalize_provider_and_model("auto", "claude-code:opus") == ("claude-code", "opus")
    assert ai.normalize_provider_and_model("claude-code", "sonnet") == ("claude-code", "sonnet")
    assert "claude-code" in ac._SUPPORTED_AUTH_PROVIDERS
    # Usage comes out of the operator's own Claude plan: Forven prices it at $0.
    assert estimate_cost_usd("claude-code", "opus", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == 0.0


def test_auth_status_reads_the_local_login(fake_claude, monkeypatch):
    status = claude_code.auth_status(force=True)
    assert status["installed"] and status["logged_in"]
    assert status["auth_method"] == "claude.ai"

    monkeypatch.setenv("FAKE_CLAUDE_LOGGED_IN", "0")
    status = claude_code.auth_status(force=True)
    assert status["installed"] and not status["logged_in"]
    assert "claude auth login" in status["error"]
    with pytest.raises(claude_code.ClaudeCodeUnavailable):
        claude_code.require_ready()


def test_auth_status_when_not_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("FORVEN_CLAUDE_CODE_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: None)
    claude_code.clear_auth_cache()
    status = claude_code.auth_status(force=True)
    assert not status["installed"]
    with pytest.raises(claude_code.ClaudeCodeUnavailable, match="not installed"):
        claude_code.require_ready()
    claude_code.clear_auth_cache()


def test_one_shot_call_switches_off_claude_code_tools(fake_claude):
    text, usage = asyncio.run(claude_code.call_text(
        "haiku", [{"role": "user", "content": "hello"}], "Be terse.",
    ))
    assert text == "echo: hello"
    assert usage == {"input_tokens": 15, "output_tokens": 3}
    args = _calls(fake_claude)[-1]
    assert args[0] == "-p"
    assert args[args.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert args[args.index("--model") + 1] == "haiku"


def test_one_shot_usage_limit_reads_as_a_rate_limit(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    with pytest.raises(claude_code.ClaudeCodeError) as excinfo:
        asyncio.run(claude_code.call_text("sonnet", [{"role": "user", "content": "hi"}], None))
    assert ai._is_rate_limit_exception(excinfo.value)


def test_history_is_flattened_for_the_prompt():
    prompt = claude_code.messages_to_prompt([
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        {"role": "user", "content": "second"},
    ])
    assert "[USER]\nfirst" in prompt and "[ASSISTANT]\nanswer" in prompt and prompt.index("first") < prompt.index("second")
    assert claude_code.messages_to_prompt([{"role": "user", "content": "only"}]) == "only"


def test_agent_run_serves_forven_tools_over_mcp(fake_claude):
    calls: list[tuple[str, dict]] = []
    seen: dict[str, list] = {"assistant": [], "done": [], "started": []}

    async def execute(name, args):
        calls.append((name, args))
        return f"{args['coin']} is 64000"

    tools = [{
        "name": "get_price",
        "description": "Latest price",
        "input_schema": {"type": "object", "properties": {"coin": {"type": "string"}}},
    }]
    text, usage, stop = asyncio.run(claude_code_runner.run_agent_task(
        model_id="sonnet",
        messages=[{"role": "user", "content": "price please"}],
        system="You are a Forven agent.",
        tools=tools,
        max_turns=7,
        execute=execute,
        on_tool_start=lambda: seen["started"].append(True),
        on_tool_done=lambda name, args, out: seen["done"].append((name, out)),
        on_assistant=lambda t, r, u: seen["assistant"].append((t, r)),
    ))
    assert stop == "success"
    assert calls == [("get_price", {"coin": "BTC"})]
    assert text == "done: BTC is 64000 / Error: unknown tool 'not_a_tool'."
    assert seen["done"] == [("get_price", "BTC is 64000")]
    assert seen["started"] == [True]
    assert seen["assistant"][0] == ("tools: get_price", "plan")
    assert usage == {"input_tokens": 15, "output_tokens": 3}

    args = _calls(fake_claude)[-1]
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--allowedTools") + 1] == "mcp__forven"
    assert args[args.index("--max-turns") + 1] == "7"


def test_mcp_server_refuses_a_wrong_token(fake_claude):
    lines: list[dict] = []

    async def execute(name, args):
        return "ok"

    asyncio.run(claude_code_runner.run_agent_task(
        model_id="sonnet", messages=[{"role": "user", "content": "x"}], system="s",
        tools=[{"name": "t", "description": "", "input_schema": {"type": "object"}}],
        max_turns=3, execute=execute,
        on_assistant=lambda t, r, u: lines.append({"text": t}),
    ))
    # The fake reports the status its wrong-token request got in its init line;
    # check the server code path directly as well.
    server = claude_code_runner._ToolServer([], asyncio.Queue())
    status, _ = asyncio.run(server._dispatch("POST", {"authorization": "Bearer nope"}, b"{}"))
    assert status == 401


def test_agent_run_out_of_turns_returns_last_text(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "max_turns")

    async def execute(name, args):
        return "unused"

    text, _usage, stop = asyncio.run(claude_code_runner.run_agent_task(
        model_id="sonnet", messages=[{"role": "user", "content": "x"}], system="s",
        tools=[{"name": "t", "description": "", "input_schema": {"type": "object"}}],
        max_turns=1, execute=execute,
    ))
    assert stop == "error_max_turns"
    assert text == "tools: t"


def test_connect_checks_the_login_and_stores_no_secret(forven_db, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_LOGGED_IN", "0")
    with pytest.raises(ac.HTTPException) as excinfo:
        ac.upsert_auth_provider("claude-code", ac.AuthProviderProfileBody())
    assert "claude auth login" in excinfo.value.detail

    monkeypatch.setenv("FAKE_CLAUDE_LOGGED_IN", "1")
    assert ac.upsert_auth_provider("claude-code", ac.AuthProviderProfileBody()) == {"ok": True, "provider": "claude-code"}
    profile = store.get_profile("claude-code")
    assert profile and not any(k in profile for k in ("access", "token", "api_key", "refresh"))
    assert store.get_token("claude-code") == ""

    payload = ac._build_auth_provider_payload("claude-code")
    assert payload["connect_mode"] == "cli"
    assert payload["status"] == "active" and payload["connected"] is True
    assert payload["cli"]["auth_method"] == "claude.ai"

    # Signed out later: still listed as connected, with a sign-in prompt.
    monkeypatch.setenv("FAKE_CLAUDE_LOGGED_IN", "0")
    claude_code.clear_auth_cache()
    payload = ac._build_auth_provider_payload("claude-code")
    assert payload["status"] == "needs_reauth" and payload["reconnect_required"] is True
    assert payload["connected"] is False

    ac.delete_auth_provider("claude-code")
    assert ac._build_auth_provider_payload("claude-code")["status"] == "not_configured"


def test_call_ai_routes_to_claude_code(fake_claude, monkeypatch):
    monkeypatch.setattr("forven.model_selection.assert_callable", lambda *a, **k: None)
    monkeypatch.setattr(ai, "get_token", lambda provider: "")
    text = asyncio.run(ai.call_ai("claude-code", "sonnet", prompt="hi there", fallback=False))
    assert text == "echo: hi there"


def test_runner_hands_the_whole_task_to_claude_code(monkeypatch):
    from forven.agents import runner
    from forven.agents.execution_state import ToolLimitReached

    monkeypatch.setattr("forven.model_selection.assert_callable", lambda *a, **k: None)
    recorded: list[tuple] = []
    monkeypatch.setattr(runner, "record_response", lambda *a: recorded.append(a))
    executed: list[str] = []

    async def fake_execute(name, args):
        executed.append(name)
        return "ok"

    monkeypatch.setattr(runner, "_execute_tool", fake_execute)

    async def fake_run(**kwargs):
        assert kwargs["max_turns"] == runner.MAX_TOOL_ROUNDS
        kwargs["on_tool_start"]()
        out = await kwargs["execute"]("create_strategy", {"x": 1})
        kwargs["on_tool_done"]("create_strategy", {"x": 1}, out)
        return "finished", {"input_tokens": 7, "output_tokens": 2}, kwargs["model_id"]

    monkeypatch.setattr(claude_code_runner, "run_agent_task", fake_run)
    progress: dict = {}
    text, usage = asyncio.run(runner._call_with_tools_single(
        "claude-code", "success", [{"role": "user", "content": "go"}], "sys",
        tools=[{"name": "create_strategy", "description": "", "input_schema": {"type": "object"}}],
        progress=progress,
    ))
    assert (text, usage["total_tokens"]) == ("finished", 9)
    assert executed == ["create_strategy"] and progress["tools_executed"] is True
    assert recorded == [("claude-code", "success", {"input_tokens": 7, "output_tokens": 2})]

    with pytest.raises(ToolLimitReached):
        asyncio.run(runner._call_with_tools_single(
            "claude-code", "error_max_turns", [{"role": "user", "content": "go"}], "sys",
            tools=[{"name": "create_strategy", "description": "", "input_schema": {"type": "object"}}],
        ))
