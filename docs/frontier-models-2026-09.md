# Frontier model support — September 8, 2026

The shared catalog in `forven/providers/frontier.py` adds 44 provider/model
choices with corresponding token price estimates. It feeds the existing
settings, agent, auxiliary-model and routing pickers. No saved selections,
connected providers, fallback chains or trading settings are changed.

After restarting the backend, open **Agents → Models**. Search for a model,
enable the choices you want and save; then select them for an agent or routing
slot. **Refresh from providers** also discovers new releases. API availability
still depends on the connected provider account.

| Provider | Added models |
| --- | --- |
| OpenAI | GPT-6 Astra; GPT-5.6 Sol, Terra and Luna; GPT-5.5 Pro; GPT-5.4 Pro and Nano |
| Anthropic | Claude Fable 5.1 and 5; Claude Opus 5; Claude Sonnet 5 |
| Google | Gemini 3.8, 3.7, 3.6 and 3.5 Flash; 3.5 Flash Lite; 3.1 Pro Preview and Flash Lite; 3 Flash Preview |
| xAI | Grok 4.6 |
| DeepSeek | V4 Pro, V4 Flash and V4 Flash Vision Experimental |
| MiniMax | M3 |
| Z.AI | GLM-5.3, GLM-5.3 Flash and GLM-5.2 |
| Mistral | Medium 3.5 |
| OpenRouter | GPT-6 Astra; GPT-5.6 Sol/Terra/Luna; Claude Fable 5.1/5, Opus 5 and Sonnet 5; Gemini 3.8 Flash; Grok 4.6; Qwen3.7 Max/Plus; Kimi K2.6; MiniMax M3; GLM-5.3/Flash |

OpenRouter discovery now includes paid models supporting tools and text output,
alongside free models. Batch and generative media routes are excluded. These
discovered models remain subject to the existing enable-list and spend gates.
Anthropic's invitation-only Mythos models are not seeded as generally available
choices; authorized accounts can still discover them from the provider.

## Request compatibility

- GPT-6 and GPT-5.6 agents use the platform Responses API with API keys. OpenAI
  subscription OAuth continues to use its separate Codex endpoint. Complete
  output items, assistant phase, encrypted reasoning, tool-call IDs, usage and
  truncation status survive tool rounds. Incomplete or disconnected streams
  cannot be treated as completed tool turns.
- GPT-5 Chat Completions use `max_completion_tokens`; modern OpenAI and Claude
  requests omit unsupported sampling parameters.
- Claude streaming preserves thinking signatures and redacted thinking blocks.
  Fable 5.1 uses default adaptive thinking and automatic tool selection.
- Gemini tool signatures and DeepSeek/OpenRouter reasoning metadata are replayed
  for subsequent tool calls.
- Direct Anthropic and DeepSeek now work in the shared completion client used
  by auxiliary tasks, as well as the existing agent adapters.

## Sources and price assumptions

Verified against provider documentation and the public OpenRouter model API on
September 8, 2026:

- [OpenAI models](https://developers.openai.com/api/docs/models),
  [GPT-6 guide](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra),
  [pricing](https://developers.openai.com/api/docs/pricing).
- [Claude models](https://platform.claude.com/docs/en/models/overview),
  [Fable 5.1 migration](https://platform.claude.com/docs/en/models/fable-5-1/migration-guide),
  [thinking](https://platform.claude.com/docs/en/build-with-claude/thinking).
- [Gemini models](https://ai.google.dev/gemini-api/docs/models) and
  [pricing](https://ai.google.dev/gemini-api/docs/pricing).
- [Grok models and pricing](https://docs.x.ai/developers/models).
- [DeepSeek models and pricing](https://api-docs.deepseek.com/quick_start/pricing/).
- [MiniMax pricing](https://platform.minimax.io/subscribe/token-plan?tab=api-enterprise).
- [Z.AI pricing](https://docs.z.ai/guides/overview/pricing).
- [Mistral Medium 3.5](https://docs.mistral.ai/models/mistral-medium-3-5-26-04).
- [OpenRouter live model catalog](https://openrouter.ai/api/v1/models).

Prices are estimates for standard text input/output, not provider invoices.
DeepSeek uses conservative peak pricing; GLM-5.3 Flash uses regular list pricing
rather than the promotion ending September 9. Gemini 3.6–3.8 Flash introductory
rates expire December 31, 2026. Cache discounts, cache write charges, long-context
premiums, priority/batch tiers, media and tool fees are not represented by the
existing two-rate estimator. Recheck prices before relying on these estimates
for those workloads.

OpenRouter prices are separately verified where they differ from the native
provider: GPT-5.6 Sol is $2/$10 there versus $4/$20 natively at this snapshot.
Fable 5.1's native ID is `claude-fable-5-1`, while its OpenRouter slug is
`anthropic/claude-fable-5.1`.

Validation uses mocked HTTP/SSE provider contracts and existing regression
tests; it does not make billable inference requests or claim account access to
every listed model.

## September 22 refresh

Every change below was checked against provider documentation and the live
OpenRouter catalog on September 22, 2026. Restart the backend to load it.

| Provider | Added choices |
| --- | --- |
| OpenAI | GPT-6 Sol and GPT-6 Luna |
| Anthropic | Claude Opus 5.5; the still-served Opus 4.8, 4.6, 4.5 and Sonnet 4.5 |
| Google | Gemma 4 31B and 26B A4B (free tier only; supports function calling) |
| xAI | Grok 4.7, 4.5 and 4.3; Grok Build 0.1 |
| DeepSeek | V4.1 Flash (`deepseek-flash`) |
| Z.AI | GLM-5.3 FlashX |
| Mistral | Small 4 (`mistral-small-2603`) and Large 3 (`mistral-large-2512`) |
| Groq / Cerebras | Qwen3.8 27B (preview on Groq) / Qwen 3.8 27B |
| Together | Kimi K3; GLM-5.3 and Flash; MiniMax M3; DeepSeek V4.1 Flash and V4 Pro; GPT-OSS 120B |
| NVIDIA NIM | Nemotron 3 Ultra; DeepSeek V4.1 Flash; Kimi K3; GLM-5.3 and Flash; Gemma 4 31B |
| OpenCode Zen | Kimi K3 and K2.7 Code; GLM-5.3 and Flash; DeepSeek V4.1 Flash and V4 Pro; MiniMax M3 |
| OpenCode GO | GLM-5.3 and Flash; Kimi K3; DeepSeek V4.1 Flash; MiMo V2.6 Pro and Flash |
| OpenRouter | GPT-6 Sol/Luna; Claude Opus 5.5; Grok 4.7; DeepSeek V4.1 Flash; Qwen3.8 Max/Flash; Kimi K3; GLM-5.3 FlashX; MiMo V2.6 Pro |

Seeds the vendor has retired, or that Forven's adapter cannot call, are no
longer offered. A saved selection of one still appears as "(configured)".

- OpenAI: `o1-mini`, `o1-preview`, `gpt-4-0125-preview` and
  `gpt-4-vision-preview` are shut down. `codex-5.3`, `codex-5.3-extra-high`
  and `codex-5.3-ultra` are not model IDs on the Codex backend or the API.
- Anthropic: Claude 3.5 Sonnet and 3.5 Haiku.
- DeepSeek: `deepseek-chat` and `deepseek-reasoner` were discontinued on July
  24. `deepseek-v4-flash` and its vision variant are now served by V4.1 Flash
  and keep price rows at that rate.
- Google: Gemini 2.0 Flash and 1.5 Flash. The Gemini API no longer serves Gemma 3.
- xAI: Grok 4, Grok 3, Grok 3 Mini and Grok Code Fast 1.
- Groq: Kimi K2 Instruct and Qwen3 32B. Cerebras: Llama 3.3 70B, Llama 3.1 8B
  and Qwen 3 32B. Mistral: Magistral Small and Nemo (retired July 31).
  Together: Llama 3.1 8B, Qwen 2.5 72B, DeepSeek V3 and Mixtral 8x7B.
- OpenCode Zen: Grok Code, Qwen3 Coder and Kimi K2 are retired. Zen serves
  Claude and GPT only on endpoints the Zen adapter does not call.
  OpenCode GO's Kimi K2.7 ID is corrected to `kimi-k2.7-code`.

Price rows now follow current list prices: Z.AI GLM-5.1 $1.40/$4.40, GLM-5
$1.00/$3.20, GLM-4.5/4.6/4.7 $0.60/$2.20 and GLM-4.5 Air $0.20/$1.10;
OpenRouter GLM-5.3 $0.65/$2.05; Together Llama 3.3 70B $1.04/$1.04. Claude
Opus 4.6–4.8 and the dated Haiku 4.5 ID were previously unpriced. Gemma 4 stays
unpriced, because a $0 row would also price OpenRouter's paid Gemma route as free.

Built-in defaults that named a retired model, or one closed to new accounts,
now use the vendor's named successor. A saved routing policy keeps its values,
and the spend gate still blocks any model that is not enabled.

| Default | Was | Now |
| --- | --- | --- |
| DeepSeek | `deepseek-chat` | `deepseek-flash` |
| Gemini | `gemini-2.5-flash-lite` | `gemini-3.5-flash-lite` |
| xAI | `grok-3-mini` | `grok-4.3` |
| Cerebras | `llama-3.3-70b` | `gpt-oss-120b` |
| OpenCode Zen | `grok-code` | `big-pickle` |
| Skill extraction (OpenRouter) | `anthropic/claude-3-5-sonnet` | `anthropic/claude-sonnet-5` |

Claude Opus 5.5 always thinks and rejects sampling parameters and forced tool
choice. Forven's Claude adapters send neither, and the agent loop only appends
history, which its preserved thinking requires.

OpenCode GO documents MiniMax and Qwen on its Anthropic-style `/messages`
endpoint, while the GO adapter calls `/chat/completions`. The existing MiniMax
and Qwen GO seeds are unchanged but unverified; new GO seeds are
chat-completions models only.

Sources: [OpenAI models](https://developers.openai.com/api/docs/models),
[pricing](https://developers.openai.com/api/docs/pricing) and
[deprecations](https://developers.openai.com/api/docs/deprecations);
[Codex models](https://learn.chatgpt.com/docs/models);
[Claude models](https://platform.claude.com/docs/en/about-claude/models/overview);
[xAI models](https://docs.x.ai/developers/models) and
[May 15 retirement](https://docs.x.ai/developers/migration/may-15-retirement);
[DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/) and
[changelog](https://api-docs.deepseek.com/updates);
[Z.AI pricing](https://docs.z.ai/guides/overview/pricing);
[Gemini models](https://ai.google.dev/gemini-api/docs/models),
[deprecations](https://ai.google.dev/gemini-api/docs/deprecations) and
[pricing](https://ai.google.dev/gemini-api/docs/pricing);
[Gemma on the Gemini API](https://ai.google.dev/gemma/docs/core/gemma_on_gemini_api);
[Mistral models](https://docs.mistral.ai/getting-started/models/models_overview/) and
[pricing](https://mistral.ai/pricing/api/);
[Groq models](https://console.groq.com/docs/models);
[Cerebras models](https://inference-docs.cerebras.ai/models/overview);
[Together models](https://docs.together.ai/docs/serverless-models);
[OpenCode Zen](https://opencode.ai/docs/zen/) and [GO](https://opencode.ai/docs/go/);
[OpenRouter catalog](https://openrouter.ai/api/v1/models).
