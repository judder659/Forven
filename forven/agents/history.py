"""Keep an agent's resent conversation small.

Every round of the tool loop sends the whole conversation again, so a tool
result or a written file stays in every later request. On 2026-10-27 a
strategy-developer task averaged 13.5k input tokens per call (4k a week
earlier) and input was ~92% of all tokens spent, which used up a MiniMax plan's
5-hour allowance in 2-3 hours. Small-context local models hit the same wall.

``compact_old_rounds`` shortens what older rounds carry: long tool results and
long tool-call arguments are cut to their head (backtest results put their
metrics first, so the key numbers stay) with a note on how to get the rest
back. The newest rounds stay whole, and model text and reasoning are never
changed (some providers require reasoning blocks to come back unaltered).
"""

from __future__ import annotations

import json

# Rounds (assistant turns) at the end of the conversation that stay whole.
KEEP_RECENT_ROUNDS = 3
# What an older tool result or tool-call argument keeps.
OLD_TEXT_HEAD_CHARS = 1200

_RESULT_NOTE = (
    "\n[... older tool result shortened to save tokens ({dropped} characters cut). "
    "Call the tool again if you need the full output.]"
)
_ARG_NOTE = "\n[... shortened in history ({dropped} characters cut); the call ran with the full value.]"


def _shorten(text: str, note: str) -> str:
    if len(text) <= OLD_TEXT_HEAD_CHARS + 200:
        return text
    dropped = len(text) - OLD_TEXT_HEAD_CHARS
    return text[:OLD_TEXT_HEAD_CHARS] + note.format(dropped=dropped)


def _shorten_value(value: object) -> object:
    if isinstance(value, str):
        return _shorten(value, _ARG_NOTE)
    if isinstance(value, dict):
        return {key: _shorten_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shorten_value(item) for item in value]
    return value


def _shorten_arguments_json(arguments: str) -> str:
    if len(arguments) <= OLD_TEXT_HEAD_CHARS + 200:
        return arguments
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return arguments
    return json.dumps(_shorten_value(parsed))


def _compact_assistant(message: dict) -> None:
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                block["input"] = _shorten_value(block["input"])
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                function["arguments"] = _shorten_arguments_json(function["arguments"])


def _compact_tool_results(message: dict) -> None:
    content = message.get("content")
    if message.get("role") == "tool":
        if isinstance(content, str):
            message["content"] = _shorten(content, _RESULT_NOTE)
        return
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and isinstance(block.get("content"), str)
            ):
                block["content"] = _shorten(block["content"], _RESULT_NOTE)


def compact_old_rounds(messages: list[dict], keep_recent_rounds: int = KEEP_RECENT_ROUNDS) -> None:
    """Shorten tool results and tool-call arguments outside the newest rounds (in place)."""
    assistant_indexes = [
        index for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    if len(assistant_indexes) <= keep_recent_rounds:
        return
    boundary = assistant_indexes[-keep_recent_rounds] if keep_recent_rounds > 0 else len(messages)
    for message in messages[:boundary]:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            _compact_assistant(message)
        else:
            _compact_tool_results(message)
