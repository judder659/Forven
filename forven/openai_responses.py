"""Platform API-key Responses transport using the shared SSE protocol parser."""

from typing import AsyncIterator

from forven.codex_responses import stream_responses


async def stream_openai_responses(
    token: str,
    model: str,
    *,
    instructions: str | None,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_output_tokens: int = 8192,
    response_schema: dict | None = None,
    response_schema_name: str = "structured_response",
) -> AsyncIterator[dict]:
    """Keep API keys on the platform endpoint, separate from subscription auth."""
    async for event in stream_responses(
        model, instructions=instructions, messages=messages, tools=tools,
        endpoint="https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        max_output_tokens=max_output_tokens,
        preserve_system_messages=True,
        response_schema=response_schema, response_schema_name=response_schema_name,
    ):
        yield event


async def call_openai_responses(
    token: str,
    model: str,
    *,
    instructions: str | None,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_output_tokens: int = 8192,
    response_schema: dict | None = None,
    response_schema_name: str = "structured_response",
) -> dict:
    """Drain a stream without losing tool calls, usage or truncation status."""
    async for event in stream_openai_responses(
        token, model, instructions=instructions, messages=messages, tools=tools,
        max_output_tokens=max_output_tokens,
        response_schema=response_schema, response_schema_name=response_schema_name,
    ):
        if event.get("type") == "done":
            return event
    raise RuntimeError("OpenAI Responses API returned no completed response")
