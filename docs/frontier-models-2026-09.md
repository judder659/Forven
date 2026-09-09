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
