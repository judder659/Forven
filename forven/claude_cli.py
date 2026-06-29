"""Local Claude Code CLI as an LLM provider backend.

Shells out to the ``claude`` binary in headless print mode
(``claude -p --output-format json``) and returns the model's text. This uses
the operator's local Claude Code session/subscription — there is no API key
and no per-token API spend, so it is a *keyless* provider like ``lmstudio``.

The CLI is itself an agent with its own tool loop; when we use it purely as a
text LLM we (a) replace its system prompt with the caller's via
``--system-prompt``, (b) disable every tool so it can never touch the
filesystem or run commands, and (c) strip the per-machine dynamic system-prompt
sections (cwd/git/memory/env) to cut the ~16k-token overhead and avoid leaking
host context into the prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

log = logging.getLogger("forven.claude_cli")

DEFAULT_BIN = "claude"
DEFAULT_TIMEOUT = 180.0

# Tools the CLI must never use while acting as a plain text LLM. Passed to
# ``--disallowed-tools`` (variadic). Belt-and-suspenders: the prompts we send
# never ask for tools, but a borrowed model should not be able to shell out.
_DISALLOWED_TOOLS = (
    "Bash", "Edit", "Write", "Read", "NotebookEdit",
    "WebFetch", "WebSearch", "Task", "Glob", "Grep",
)

# Short model_ids we expose in the catalog map straight onto the CLI's model
# aliases; anything else is passed through unchanged (full model names work).
_MODEL_ALIASES = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"}

# Neutral working dir so the CLI does not pick up the project's CLAUDE.md or
# git status. Created lazily on first call.
_CWD = os.path.join(os.path.expanduser("~/.forven"), "claude_cli_cwd")


def _binary_exists(binary: str) -> bool:
    if os.path.sep in binary:
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    return shutil.which(binary) is not None


# Common install locations checked when ``claude`` is not on PATH. The server
# process (uvicorn) often runs with a minimal PATH that omits ``~/.local/bin``
# where the per-user CLI installs, so PATH lookup alone is not enough.
_FALLBACK_BIN_PATHS = (
    os.path.expanduser("~/.local/bin/claude"),
    "/usr/local/bin/claude",
    "/usr/bin/claude",
    os.path.expanduser("~/.claude/local/claude"),
)


def _default_binary() -> str:
    """Resolve the binary to an ABSOLUTE path when possible (env > PATH > known
    install locations), never consulting the auth store.

    Returning an absolute path matters: the server process may not have the
    CLI's install dir on PATH, so ``create_subprocess_exec`` needs the full
    path. Used by ``get_profile`` synthesis and ``binary_available`` so they
    cannot recurse back into ``get_profile`` (which builds the profile).
    """
    env = os.environ.get("CLAUDE_CLI_BIN", "").strip()
    if env:
        return env
    found = shutil.which(DEFAULT_BIN)
    if found:
        return found
    for path in _FALLBACK_BIN_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return DEFAULT_BIN


def binary_available() -> bool:
    """True when the ``claude`` binary is reachable (no auth-store lookup)."""
    return _binary_exists(_default_binary())


def resolve_binary() -> str:
    """Resolve the ``claude`` executable at call time: profile override > default.

    Safe to consult the auth store here: a stored profile short-circuits before
    synthesis, and the synthetic profile's ``base_url`` is itself
    ``_default_binary()`` — so no recursion.
    """
    try:
        from forven.auth.store import get_profile

        profile = get_profile("claude-cli") or {}
    except Exception:
        profile = {}
    candidate = str(profile.get("bin") or profile.get("base_url") or "").strip()
    return candidate or _default_binary()


def _map_model(model: str | None) -> str:
    raw = str(model or "").strip()
    if not raw:
        return "sonnet"
    if ":" in raw:  # tolerate a leftover provider prefix
        raw = raw.split(":", 1)[1].strip()
    return _MODEL_ALIASES.get(raw.lower(), raw)


def _serialize_content(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(item.get("text") or item.get("content") or "").strip()
            else:
                text = ""
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "").strip()
    return str(content).strip()


def flatten_messages(messages: list[dict] | None) -> str:
    """Flatten a chat history into a single prompt transcript for ``-p``."""
    if not messages:
        return ""
    if len(messages) == 1:
        only = _serialize_content(messages[0].get("content"))
        if only:
            return only
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user").strip().upper() or "USER"
        content = _serialize_content(message.get("content"))
        if content:
            parts.append(f"{role}: {content}")
    return "\n\n".join(parts)


async def run_claude_cli(
    *,
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[str, dict]:
    """Run ``claude -p`` for a single text turn. Returns ``(text, usage)``.

    Raises ``RuntimeError`` on a non-zero exit, unparseable output, an error
    result, or a timeout — so the caller's provider-health classification and
    fallback chain see a normal exception.
    """
    binary = resolve_binary()
    args = [binary, "-p", "--output-format", "json", "--model", _map_model(model),
            "--exclude-dynamic-system-prompt-sections"]
    sys_prompt = str(system or "").strip()
    if sys_prompt:
        args += ["--system-prompt", sys_prompt]
    args += ["--disallowed-tools", *_DISALLOWED_TOOLS]

    try:
        os.makedirs(_CWD, exist_ok=True)
        cwd = _CWD
    except Exception:
        cwd = None

    # The server's PATH may omit the CLI's install dir; make sure the binary's
    # own directory and ~/.local/bin are reachable for any helper it spawns.
    env = dict(os.environ)
    extra_paths = [os.path.dirname(binary), os.path.expanduser("~/.local/bin")]
    existing = env.get("PATH", "")
    prefix = os.pathsep.join(p for p in extra_paths if p and p not in existing.split(os.pathsep))
    if prefix:
        env["PATH"] = prefix + (os.pathsep + existing if existing else "")

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"claude CLI not found ({binary!r}). Install Claude Code or set "
            f"CLAUDE_CLI_BIN."
        ) from exc

    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=(prompt or "").encode("utf-8")), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError(f"claude CLI timed out after {timeout:.0f}s") from exc

    if proc.returncode != 0:
        detail = (err or b"").decode("utf-8", "replace").strip()[:500]
        raise RuntimeError(
            f"claude CLI exited {proc.returncode}: {detail or 'no stderr'}"
        )

    try:
        data = json.loads((out or b"").decode("utf-8", "replace"))
    except Exception as exc:
        raise RuntimeError("claude CLI returned non-JSON output") from exc

    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        status = data.get("api_error_status") or data.get("subtype") or "error"
        raise RuntimeError(f"claude CLI error result: {status}")

    text = str(data.get("result") or "").strip()
    raw_usage = data.get("usage") or {}
    usage = {
        "input_tokens": int(raw_usage.get("input_tokens", 0) or 0),
        "output_tokens": int(raw_usage.get("output_tokens", 0) or 0),
        "cost_usd": data.get("total_cost_usd"),
    }
    log.info(
        "claude-cli/%s: %s in / %s out tokens",
        _map_model(model), usage["input_tokens"], usage["output_tokens"],
    )
    return text, usage
