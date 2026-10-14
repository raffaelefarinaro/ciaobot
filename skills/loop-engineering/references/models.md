# Models and providers

## Workflow tiers and runtime IDs

These are role equivalents, not interchangeable model IDs:

| Workflow tier | Claude equivalent | Codex equivalent | Use |
|---|---|---|---|
| Luna | Haiku | Luna-tier model | Default implementer/fixer for a concrete plan |
| Terra | Sonnet | Terra-tier model, if exposed | Normal reviewer or harder implementation |
| Sol | Opus | Sol-tier model | Strongest review, planning, or difficult fixes |

Ask the current runtime for its exact model IDs before dispatch. In Codex, use the model IDs exposed in the current tool metadata; examples in this environment include `gpt-6.1-sol` and `gpt-6-luna`. Do not invent an ID such as `gpt-6-terra`: if the requested tier has no clearly corresponding available model, ask the user which exact available model to use. In OpenCode, use `opencode models` and the configured agent's `model`; values use `provider/model-id`. In Claude Code, preserve its actual model selection. Do not silently change a user's selected model or provider.

| Mode | Implementer / fixer | Reviewer |
|---|---|---|
| Claude subagents | Haiku/Luna (default) or Sonnet/Terra via `Agent(model: …)` | primary, or a fresh Sonnet/Terra or Opus/Sol agent |
| Codex subagents | Luna tier by default; exact model ID from current Codex metadata | primary, or a fresh Terra/Sonnet or Sol/Opus agent |
| OpenCode subagents | Luna tier by default; exact available `provider/model-id` on the selected configured agent | primary, or a fresh Terra/Sonnet or Sol/Opus agent |
| Orca sessions | Any currently available OpenCode model, or an explicitly selected Claude CLI model | any explicitly selected supported reviewer model |

The primary/Orca orchestrator owns the selected model policy for the whole loop. If a model fails twice in a row (no change, or gates red after its turn), offer a stronger model rather than silently switching or burning rounds.

## OpenCode implementer menu

Refresh before selection — `opencode models` — and use only IDs it lists. The old ranked list below is historical guidance, not proof that a provider or model is available now. Smoke-test a chosen model before its first round when its provider is configured. OpenCode agents can override the model; if an agent omits `model`, its subagent inherits the invoking primary's model. Check this before dispatch so the user's requested tier is not silently changed.

The previous menu (as of 2026-09-25, OpenCode 2.0.16) was:

| # | Model | Notes |
|---|---|---|
| 1 | `ollama-cloud/deepseek-v4.1-flash` | default; fast, strong at following explicit plans |
| 2 | `ollama-cloud/glm-5.3-flash` | fast, good at multi-file edits |
| 3 | `ollama-cloud/kimi-k2.7-code` | code-tuned; slower, better on tricky logic |
| 4 | `ollama-cloud/minimax-m3` | long context; good for large test files |
| 5 | `ollama-cloud/qwen3.5:397b` | big open model; slowest of the list |
| 6 | `ollama-cloud/glm-5.3` | non-flash GLM; more careful, slower |
| 7 | `ollama-cloud/deepseek-v4-pro` | stronger DeepSeek when flash struggles |
| 8 | `opencode/mimo-v2.6-flash-free` | free tier |
| 9 | `opencode/nemotron-3-ultra-free` | free tier |
| 10 | `openrouter/stealth/space-bunny-alpha` | stealth model; followed the #564 plan exactly |

## Provider rules

- Anthropic models run through the Claude Code CLI or Claude `Agent`; never route them through OpenRouter. Native OpenCode may use an explicitly selected, available Anthropic provider/model, but never silently reroute one.
- Native Codex uses its own exact tool model IDs. In Orca or native OpenCode mode, OpenAI models use the selected OpenAI provider (`openai/<id>`), never `openrouter/openai/...`.
- If the requested model/provider has no working runtime, stop and ask. Do not substitute a different model, provider, or tier without permission.

For OpenCode agent model and permission configuration, see [the official Agents docs](https://opencode.ai/docs/agents/).
