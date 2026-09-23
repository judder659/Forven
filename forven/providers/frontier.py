"""Verified September 2026 model seeds, shared by discovery and cost estimates.

These are choices, never automatic routing defaults. Sources and pricing
limitations are recorded in docs/frontier-models-2026-09.md.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FrontierModel:
    provider: str
    model_id: str
    label: str
    rate: tuple[float, float]  # USD per million input/output tokens


FRONTIER_MODELS: tuple[FrontierModel, ...] = (
    FrontierModel("openai", "gpt-6-astra", "OpenAI GPT-6 Astra", (10, 50)),
    FrontierModel("openai", "gpt-6-sol", "OpenAI GPT-6 Sol", (2, 10)),
    FrontierModel("openai", "gpt-6-luna", "OpenAI GPT-6 Luna", (0.1, 0.5)),
    FrontierModel("openai", "gpt-5.6-sol", "OpenAI GPT-5.6 Sol", (4, 20)),
    FrontierModel("openai", "gpt-5.6-terra", "OpenAI GPT-5.6 Terra", (2, 12)),
    FrontierModel("openai", "gpt-5.6-luna", "OpenAI GPT-5.6 Luna", (0.2, 1.2)),
    FrontierModel("openai", "gpt-5.5-pro", "OpenAI GPT-5.5 Pro", (30, 180)),
    FrontierModel("openai", "gpt-5.4-pro", "OpenAI GPT-5.4 Pro", (30, 180)),
    FrontierModel("openai", "gpt-5.4-nano", "OpenAI GPT-5.4 Nano", (0.2, 1.25)),
    FrontierModel("anthropic", "claude-fable-5-1", "Anthropic Claude Fable 5.1", (10, 50)),
    FrontierModel("anthropic", "claude-opus-5-5", "Anthropic Claude Opus 5.5", (4, 20)),
    FrontierModel("anthropic", "claude-fable-5", "Anthropic Claude Fable 5", (10, 50)),
    FrontierModel("anthropic", "claude-opus-5", "Anthropic Claude Opus 5", (5, 25)),
    FrontierModel("anthropic", "claude-sonnet-5", "Anthropic Claude Sonnet 5", (2, 10)),
    FrontierModel("gemini", "gemini-3.8-flash", "Google Gemini 3.8 Flash", (0.75, 3.75)),
    FrontierModel("gemini", "gemini-3.7-flash", "Google Gemini 3.7 Flash", (0.75, 3.75)),
    FrontierModel("gemini", "gemini-3.6-flash", "Google Gemini 3.6 Flash", (0.75, 3.75)),
    FrontierModel("gemini", "gemini-3.5-flash", "Google Gemini 3.5 Flash", (1.5, 9)),
    FrontierModel("gemini", "gemini-3.5-flash-lite", "Google Gemini 3.5 Flash Lite", (0.3, 2.5)),
    FrontierModel("gemini", "gemini-3.1-pro-preview", "Google Gemini 3.1 Pro (Preview)", (2, 12)),
    FrontierModel("gemini", "gemini-3.1-flash-lite", "Google Gemini 3.1 Flash Lite", (0.25, 1.5)),
    FrontierModel("gemini", "gemini-3-flash-preview", "Google Gemini 3 Flash (Preview)", (0.5, 3)),
    FrontierModel("xai", "grok-4.7", "xAI Grok 4.7", (2, 6)),
    FrontierModel("xai", "grok-4.6", "xAI Grok 4.6", (2, 6)),
    # DeepSeek peak prices; off-peak usage can cost less. "deepseek-flash" is
    # V4.1 Flash; the retired V4 Flash names are priced in cost_pricing.
    FrontierModel("deepseek", "deepseek-flash", "DeepSeek V4.1 Flash", (0.3, 1.2)),
    FrontierModel("deepseek", "deepseek-v4-pro", "DeepSeek V4 Pro", (1.32, 3.96)),
    FrontierModel("minimax", "MiniMax-M3", "MiniMax M3", (0.3, 1.2)),
    FrontierModel("zai", "glm-5.3", "Z.AI GLM-5.3", (1.4, 4.4)),
    # Use the regular list price rather than the promotion ending September 9.
    FrontierModel("zai", "glm-5.3-flash", "Z.AI GLM-5.3 Flash", (0.15, 0.5)),
    FrontierModel("zai", "glm-5.3-flashx", "Z.AI GLM-5.3 FlashX", (0.37, 1.25)),
    FrontierModel("zai", "glm-5.2", "Z.AI GLM-5.2", (1.4, 4.4)),
    FrontierModel("mistral", "mistral-medium-3-5", "Mistral Medium 3.5", (1.5, 7.5)),
    # Gateway IDs are independently verified: Claude versions use a dot on
    # OpenRouter, and gateway prices can differ from the native provider's.
    FrontierModel("openrouter", "openai/gpt-6-astra", "OpenRouter GPT-6 Astra", (10, 50)),
    FrontierModel("openrouter", "openai/gpt-6-sol", "OpenRouter GPT-6 Sol", (2, 10)),
    FrontierModel("openrouter", "openai/gpt-6-luna", "OpenRouter GPT-6 Luna", (0.1, 0.5)),
    FrontierModel("openrouter", "openai/gpt-5.6-sol", "OpenRouter GPT-5.6 Sol", (2, 10)),
    FrontierModel("openrouter", "openai/gpt-5.6-terra", "OpenRouter GPT-5.6 Terra", (2, 12)),
    FrontierModel("openrouter", "openai/gpt-5.6-luna", "OpenRouter GPT-5.6 Luna", (0.2, 1.2)),
    FrontierModel("openrouter", "anthropic/claude-fable-5.1", "OpenRouter Claude Fable 5.1", (10, 50)),
    FrontierModel("openrouter", "anthropic/claude-opus-5.5", "OpenRouter Claude Opus 5.5", (4, 20)),
    FrontierModel("openrouter", "anthropic/claude-fable-5", "OpenRouter Claude Fable 5", (10, 50)),
    FrontierModel("openrouter", "anthropic/claude-opus-5", "OpenRouter Claude Opus 5", (5, 25)),
    FrontierModel("openrouter", "anthropic/claude-sonnet-5", "OpenRouter Claude Sonnet 5", (2, 10)),
    FrontierModel("openrouter", "google/gemini-3.8-flash", "OpenRouter Gemini 3.8 Flash", (0.75, 3.75)),
    FrontierModel("openrouter", "x-ai/grok-4.7", "OpenRouter Grok 4.7", (1.6, 4.8)),
    FrontierModel("openrouter", "x-ai/grok-4.6", "OpenRouter Grok 4.6", (2, 6)),
    FrontierModel("openrouter", "deepseek/deepseek-v4.1-flash", "OpenRouter DeepSeek V4.1 Flash", (0.15, 0.6)),
    FrontierModel("openrouter", "qwen/qwen3.8-max-0902", "OpenRouter Qwen3.8 Max", (2, 6)),
    FrontierModel("openrouter", "qwen/qwen3.8-flash", "OpenRouter Qwen3.8 Flash", (0.15, 0.47)),
    FrontierModel("openrouter", "qwen/qwen3.7-max", "OpenRouter Qwen3.7 Max", (1.475, 4.425)),
    FrontierModel("openrouter", "qwen/qwen3.7-plus", "OpenRouter Qwen3.7 Plus", (0.32, 1.28)),
    FrontierModel("openrouter", "moonshotai/kimi-k3", "OpenRouter Kimi K3", (3, 15)),
    FrontierModel("openrouter", "moonshotai/kimi-k2.6", "OpenRouter Kimi K2.6", (0.95, 4)),
    FrontierModel("openrouter", "minimax/minimax-m3", "OpenRouter MiniMax M3", (0.3, 1.2)),
    FrontierModel("openrouter", "z-ai/glm-5.3", "OpenRouter GLM-5.3", (0.6538, 2.0548)),
    FrontierModel("openrouter", "z-ai/glm-5.3-flash", "OpenRouter GLM-5.3 Flash", (0.15, 0.5)),
    FrontierModel("openrouter", "z-ai/glm-5.3-flashx", "OpenRouter GLM-5.3 FlashX", (0.37, 1.25)),
    FrontierModel("openrouter", "xiaomi/mimo-v2.6-pro", "OpenRouter MiMo V2.6 Pro", (0.435, 0.87)),
)


def uses_openai_responses(model_id: str) -> bool:
    """Models requiring Responses for reasoning with tools (first-party only)."""
    model = model_id.strip().lower()
    return model.startswith(("gpt-6", "gpt-5.6")) or model in {
        "gpt-5.5-pro", "gpt-5.4-pro",
    }


def claude_uses_fixed_sampling(model_id: str) -> bool:
    """Modern Claude models reject custom sampling even without thinking."""
    model = model_id.lower().rsplit("/", 1)[-1]
    return model.startswith((
        "claude-fable-", "claude-mythos-", "claude-opus-5", "claude-sonnet-5",
        "claude-opus-4-7", "claude-opus-4-8", "claude-opus-4.7", "claude-opus-4.8",
    ))


def chat_generation_params(
    provider: str, model_id: str, max_tokens: int, temperature: float,
) -> dict:
    """Respect native token-budget names and fixed-sampling model families."""
    model = model_id.lower().rsplit("/", 1)[-1]
    openai_reasoning = provider == "openai" and (
        model.startswith(("gpt-5", "gpt-6"))
        or (len(model) > 1 and model[0] == "o" and model[1].isdigit())
    )
    params: dict = {"max_completion_tokens" if openai_reasoning else "max_tokens": max_tokens}
    if not (
        openai_reasoning or claude_uses_fixed_sampling(model)
        or model.startswith(("gpt-6", "gpt-5.6"))
    ):
        params["temperature"] = temperature
    return params
