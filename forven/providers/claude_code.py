"""Claude Code provider: run the operator's own signed-in ``claude`` program.

Forven never handles a Claude login. The operator installs Claude Code and
signs in with ``claude auth login`` in a terminal (Anthropic's own flow); Forven
then starts ``claude -p`` for each model call and reads the answer back, the same
way apps like Block's Buzz run Claude Code. Usage is billed to whatever account
``claude`` is signed in with. Anthropic's terms allow this as long as the binary
is unmodified and Forven never collects, stores or forwards Claude credentials:
https://code.claude.com/docs/en/legal-and-compliance

This module has no DB, agent or FastAPI dependency. The agent tool loop that
serves Forven's tools to ``claude`` over MCP lives in
``forven.agents.claude_code_runner``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("forven.providers.claude_code")

PROVIDER = "claude-code"
DISPLAY_NAME = "Claude Code"
LOGIN_COMMAND = "claude auth login"
INSTALL_URL = "https://code.claude.com/docs/en/setup"

# Model aliases the claude CLI accepts. There is no model list to fetch without
# an API key, so the choices are fixed; --model also takes a full model id.
MODEL_ALIASES: tuple[tuple[str, str], ...] = (
    ("sonnet", "Claude Code Sonnet (latest)"),
    ("opus", "Claude Code Opus (latest)"),
    ("haiku", "Claude Code Haiku (latest)"),
    ("fable", "Claude Code Fable (latest)"),
)
DEFAULT_MODEL = "sonnet"

# One-shot answers (Brain chat, bot decisions, summaries) are short; a whole
# agent task is bounded by the runner's own task timeouts, not this.
_ONE_SHOT_TIMEOUT_SECONDS = 600.0
_AUTH_STATUS_TTL_SECONDS = 60.0
_STDERR_TAIL_CHARS = 4000

# Phrases claude prints when the signed-in plan has run out for now. They are
# re-raised with "rate limit" in the message so the existing fallback and
# provider-health code treats them like any provider's 429.
_USAGE_LIMIT_HINTS = ("usage limit", "rate limit", "limit reached", "limit will reset", "overloaded", "429")
_SIGNED_OUT_HINTS = ("not logged in", "please run /login", "invalid api key", "authentication_error", "oauth token")

_auth_cache: dict[str, object] = {}
_auth_cache_lock = threading.Lock()


class ClaudeCodeError(RuntimeError):
    """A claude run failed; the message is safe to show to the operator."""


class ClaudeCodeUnavailable(ClaudeCodeError):
    """claude is missing or not signed in, so no call was made."""


def _windows() -> bool:
    return os.name == "nt"


def find_cli(explicit: str | None = None) -> str | None:
    """Return the path of the claude executable, or None when it isn't installed.

    On Windows the real ``claude.exe`` is preferred over an npm ``claude.cmd``
    shim: a batch shim mangles quoted arguments when started directly.
    """
    candidates: list[str] = []
    for value in (explicit, os.environ.get("FORVEN_CLAUDE_CODE_PATH")):
        if value and str(value).strip():
            candidates.append(str(value).strip())
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    if _windows():
        home = Path(os.environ.get("USERPROFILE") or Path.home())
        native = home / ".local" / "bin" / "claude.exe"
        if native.is_file():
            return str(native)
        found = shutil.which("claude.exe")
        if found:
            return found
    return shutil.which("claude")


def _creationflags() -> int:
    # No console window flashing up for every model call on Windows.
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if _windows() else 0


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # Forven's own encryption key never needs to reach another program.
    env.pop("FORVEN_MASTER_KEY", None)
    # claude reads its login from its own config; keep its output plain.
    env.setdefault("NO_COLOR", "1")
    return env


def auth_status(*, force: bool = False, cli_path: str | None = None) -> dict:
    """Describe the local claude install: installed, signed in, and how.

    Returns ``{"installed", "logged_in", "auth_method", "cli_path", "error"}``.
    Cached for a minute so the Settings page and every call don't each start a
    process.
    """
    now = time.monotonic()
    with _auth_cache_lock:
        cached = _auth_cache.get("status")
        if not force and cached and now - float(_auth_cache.get("at") or 0) < _AUTH_STATUS_TTL_SECONDS:
            return dict(cached)  # type: ignore[arg-type]

    path = find_cli(cli_path)
    status: dict = {"installed": bool(path), "logged_in": False, "auth_method": None, "cli_path": path, "error": None}
    if not path:
        status["error"] = "Claude Code is not installed on this computer."
    else:
        try:
            proc = subprocess.run(
                [path, "auth", "status"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, env=_child_env(), creationflags=_creationflags(),
            )
            data = _parse_json_object(proc.stdout)
            if data is not None:
                status["logged_in"] = bool(data.get("loggedIn"))
                method = str(data.get("authMethod") or "").strip() or None
                status["auth_method"] = None if method == "none" else method
            else:
                status["logged_in"] = proc.returncode == 0
            if not status["logged_in"]:
                status["error"] = f"Claude Code is not signed in. Run `{LOGIN_COMMAND}` in a terminal."
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator verbatim
            status["error"] = f"Could not run claude: {exc}"

    with _auth_cache_lock:
        _auth_cache["status"] = dict(status)
        _auth_cache["at"] = now
    return status


def clear_auth_cache() -> None:
    with _auth_cache_lock:
        _auth_cache.clear()


def require_ready(cli_path: str | None = None) -> str:
    """Return the claude path, or raise ClaudeCodeUnavailable with the fix."""
    status = auth_status(cli_path=cli_path)
    if not status["installed"]:
        raise ClaudeCodeUnavailable(f"{status['error']} Install it from {INSTALL_URL}.")
    if not status["logged_in"]:
        # The operator may have just signed in; look again before refusing.
        status = auth_status(force=True, cli_path=cli_path)
        if not status["logged_in"]:
            raise ClaudeCodeUnavailable(str(status["error"] or "Claude Code is not signed in."))
    return str(status["cli_path"])


def _parse_json_object(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _block_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in (None, "text") and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "tool_result":
                    parts.append(f"[tool result] {_block_text(block.get('content'))}")
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool call] {block.get('name')}({json.dumps(block.get('input'), default=str)})")
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return json.dumps(content, default=str)


def messages_to_prompt(messages: list[dict]) -> str:
    """Flatten a chat history into one prompt for ``claude -p``.

    A single user message is passed through as-is; a longer history is written
    out turn by turn so the model sees the whole conversation.
    """
    turns = [m for m in messages or [] if isinstance(m, dict) and m.get("role") != "system"]
    if len(turns) == 1 and turns[0].get("role") == "user":
        return _block_text(turns[0].get("content"))
    lines: list[str] = []
    for message in turns:
        text = _block_text(message.get("content")).strip()
        if text:
            lines.append(f"[{str(message.get('role') or 'user').upper()}]\n{text}")
    if lines:
        lines.append("Reply as the assistant to the last message above.")
    return "\n\n".join(lines)


def schema_instruction(response_schema: dict | None) -> str:
    if not response_schema:
        return ""
    return (
        "\n\nReply with ONLY a JSON value that matches this JSON Schema, with no prose "
        "and no code fences:\n" + json.dumps(response_schema)
    )


def usage_from_result(result: dict) -> dict:
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    input_tokens = sum(
        int(usage.get(key) or 0)
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    output_tokens = int(usage.get("output_tokens") or 0)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def error_from_result(result: dict | None, *, stderr: str = "", returncode: int | None = None) -> ClaudeCodeError:
    """Turn a failed run into an error whose wording the fallback code understands."""
    detail = ""
    if result:
        errors = result.get("errors")
        detail = str(result.get("result") or "").strip()
        if not detail and isinstance(errors, list):
            detail = "; ".join(str(e) for e in errors if e)
        detail = detail or str(result.get("subtype") or "")
    if not detail:
        detail = (stderr or "").strip()[-_STDERR_TAIL_CHARS:] or f"claude exited with code {returncode}"
    lowered = detail.lower()
    if any(hint in lowered for hint in _SIGNED_OUT_HINTS):
        clear_auth_cache()
        return ClaudeCodeUnavailable(f"Claude Code is not signed in ({detail}). Run `{LOGIN_COMMAND}` in a terminal.")
    if any(hint in lowered for hint in _USAGE_LIMIT_HINTS):
        return ClaudeCodeError(f"claude-code rate limit: {detail}")
    return ClaudeCodeError(f"claude-code: {detail}")


def base_args(cli: str, model: str, system_prompt_file: str) -> list[str]:
    """Flags every Forven run uses.

    Claude Code's own tools (shell, file edits, web) are switched off, only
    MCP servers Forven passes are loaded, and nothing is written to the
    operator's Claude Code session history.
    """
    args = [
        cli, "-p",
        "--tools", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt-file", system_prompt_file,
    ]
    if model:
        args += ["--model", model]
    return args


class Run:
    """One ``claude`` process with its stdout read line by line on a thread.

    Works under any asyncio event loop (uvicorn on Windows may run a selector
    loop, which can't start subprocesses). ``lines`` receives each stdout line,
    then ``None`` once the process has exited.
    """

    def __init__(self, args: list[str], stdin_text: str, cwd: str) -> None:
        self.args = args
        self.stdin_text = stdin_text
        self.cwd = cwd
        self.lines: asyncio.Queue[str | None] = asyncio.Queue()
        self.returncode: int | None = None
        self._stderr: list[str] = []
        self._proc: subprocess.Popen | None = None

    @property
    def stderr(self) -> str:
        return "".join(self._stderr)[-_STDERR_TAIL_CHARS:]

    def start(self, on_line: Callable[[str | None], None] | None = None) -> None:
        loop = asyncio.get_running_loop()
        push = on_line or (lambda line: loop.call_soon_threadsafe(self.lines.put_nowait, line))
        self._proc = subprocess.Popen(
            self.args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=self.cwd, env=_child_env(), creationflags=_creationflags(),
        )
        proc = self._proc

        def _feed() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(self.stdin_text)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                self._stderr.append(line)
                if len(self._stderr) > 200:
                    del self._stderr[:100]

        def _read() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                push(line)
            self.returncode = proc.wait()
            push(None)

        for target in (_feed, _drain_stderr, _read):
            threading.Thread(target=target, daemon=True, name=f"claude-code-{target.__name__}").start()

    def kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass


async def call_text(
    model: str,
    messages: list[dict],
    system: str | None,
    *,
    response_schema: dict | None = None,
    timeout: float = _ONE_SHOT_TIMEOUT_SECONDS,
) -> tuple[str, dict]:
    """One model answer with no tools. Returns ``(text, usage)``."""
    cli = require_ready()
    prompt = messages_to_prompt(messages)
    with tempfile.TemporaryDirectory(prefix="forven-claude-", ignore_cleanup_errors=True) as workdir:
        system_file = Path(workdir) / "system.md"
        system_file.write_text((system or "You are a helpful assistant.") + schema_instruction(response_schema), encoding="utf-8")
        args = base_args(cli, model, str(system_file)) + ["--output-format", "json"]
        run = Run(args, prompt, workdir)
        run.start()
        chunks: list[str] = []
        try:
            async with asyncio.timeout(timeout):
                while (line := await run.lines.get()) is not None:
                    chunks.append(line)
        except TimeoutError:
            run.kill()
            raise ClaudeCodeError(f"claude-code: no answer within {int(timeout)}s") from None
        finally:
            run.kill()

    result = _parse_json_object("".join(chunks))
    if result is None:
        # Some versions print a JSON array of events; the result is the last one.
        try:
            events = json.loads("".join(chunks))
            result = next((e for e in reversed(events) if isinstance(e, dict) and e.get("type") == "result"), None)
        except ValueError:
            result = None
    if result is None or result.get("is_error") or run.returncode not in (0, None):
        raise error_from_result(result, stderr=run.stderr, returncode=run.returncode)
    text = result.get("result")
    if not isinstance(text, str) or not text.strip():
        raise ClaudeCodeError("claude-code: the answer was empty")
    usage = usage_from_result(result)
    log.info("claude-code/%s: %d input, %d output tokens", model, usage["input_tokens"], usage["output_tokens"])
    return text, usage


def model_catalog() -> list[dict]:
    return [{"provider": PROVIDER, "model_id": model_id, "label": label} for model_id, label in MODEL_ALIASES]
