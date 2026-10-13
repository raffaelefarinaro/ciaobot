# Models and providers

## Which models each mode can use

| Mode | Implementer / fixer | Reviewer |
|---|---|---|
| Subagents (this session) | `haiku` (default) or `sonnet` via `Agent(model: …)` | you, or a fresh `sonnet`/`opus` subagent |
| Orca sessions | any opencode model from the menu below, or `claude --model claude-haiku-5-5` / `claude-sonnet-5-5` | `claude --model claude-sonnet-5-5` (default) or `claude-opus-5-5`, or an OpenAI model through opencode |

## opencode implementer menu

Refresh first — `opencode models | grep -E '^(ollama-cloud|opencode|openrouter)/'` — and drop any entry below that is no longer listed. Smoke-test the chosen one before the first round: `opencode run --standalone -m <model> "Reply with exactly: ok"` from a scratch dir (headless is fine for this one-liner). Default top 10 (as of 2026-09-25, opencode 2.0.16):

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

The same model implements and fixes for the whole loop unless the user switches it. If a model fails twice in a row (no commit, or gates red after its turn), offer the user a switch (suggest the next stronger one) rather than burning rounds.

## Provider rules

- **Anthropic models always run through the Claude Code CLI or the `Agent` tool, never OpenRouter.** They bill to the subscription and always work; `openrouter/anthropic/...` dies the moment OpenRouter is out of credit.
- **OpenAI models run through opencode on the `openai` provider** (`{"model":"openai/<id>"}` in `<wt>/opencode.json`), never `openrouter/openai/...`. That provider is the user's own auth — the fallback when Anthropic quota or OpenRouter credit is the blocker.
- **If the requested model has no working provider, stop and ask.** Never silently substitute a different model or provider.
