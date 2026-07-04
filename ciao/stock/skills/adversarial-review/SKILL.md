---
name: adversarial-review
description: Multi-model adversarial review of any artifact before shipping. Spawns separate model calls (OpenRouter / Ollama / Anthropic via the Claude Agent SDK) with different models, collects structured critiques, and synthesizes a weighted review. Trigger on "review", "critique", "red-team", "tear apart", "second opinion", or after producing major artifacts.
---

# Adversarial Review

Sends an artifact to several models that don't share Claude's training distribution, collects structured critiques, and synthesizes them into a single weighted review. The point is to catch what one model alone misses: blind spots, hidden assumptions, weak evidence, contradictions, missing edge cases.

## When to use

- Raffa explicitly asks for a review, critique, second opinion, red-team, or "tear it apart".
- He's about to ship something high-stakes: a PRD, brief, OKR, plan, customer email, exec deck, public post.
- You just produced a substantive artifact yourself and want it pressure-tested before declaring done.
- Two models you've consulted disagree and you want a wider panel to break the tie.

Skip for trivial outputs (one-line answers, simple lookups). The panel costs real tokens and time; reserve it for things worth getting right.

## How it works

A single in-package CLI (`ciao.critique`) calls the Claude Agent SDK once per model in the panel. Each call is a one-shot (`max_turns=1`, no tools); the artifact is inlined in the prompt. Per-model routing is automatic from the model id shape: `owner/model` ids (e.g. `anthropic/claude-sonnet-4.5`) reach OpenRouter when `OPENROUTER_API_KEY` is set, `:tag`/`:cloud` ids reach Ollama, and bare aliases (`opus`/`sonnet`/`haiku`) stay on the Anthropic subscription. The script aggregates the per-model JSON reviews and prints a markdown report.

```bash
$SKILL_DIR/scripts/review.py --input path/to/artifact.md [flags]
```

## Panel selection

**3 models is the right default size.** Two models agreeing is consensus, the third breaks ties. Going to 5+ is mostly redundant — the critiques overlap heavily and you've paid 60% more in time and tokens for marginal new insight. Bump to 5 only when the stakes are unusually high or the artifact is genuinely contested.

Two ways to pick the panel, in order of preference:

1. **No flags (recommended).** The default panel is chosen from what's configured:
   - OpenRouter key set → `anthropic/claude-sonnet-4.5`, `anthropic/claude-haiku-4.5`, `anthropic/claude-opus-4.8`
   - Ollama cloud key set → the configured `sonnet`/`haiku`/`opus` tier models
   - Neither → Anthropic aliases `sonnet`, `haiku`, `opus`
2. **`--models a,b,c`** or `CIAO_REVIEW_MODELS` env var, or the `critique_models` setting in Settings → Models. Explicit override. Model ids use the native shape for their backend (`anthropic/claude-sonnet-4.5`, `kimi-k2.7-code:cloud`, `opus`).

Inspect a panel without running a review:
```bash
$SKILL_DIR/scripts/review.py --print-panel        # default 3
$SKILL_DIR/scripts/review.py --models a,b,c --print-panel
```

Each model gets the same adversarial system prompt and returns structured JSON with: verdict (ship | revise | block), confidence (1-5), summary, strengths, issues (with severity blocking | major | minor | nit), missing items, and sharp questions. The script aggregates and prints a markdown report by default.

## Flags

- `--input <path>` — artifact file. If omitted, reads from stdin.
- `--type <kind>` — `spec`, `prd`, `plan`, `brief`, `okr`, `doc`, `email`, `code`, `idea`, etc.
- `--focus "<topic>"` — narrow the review (e.g. `"security"`, `"mobile edge cases"`).
- `--context "<sentence>"` — what reviewers don't know but should.
- `--models a,b,c` — override the panel explicitly.
- `--print-panel` — resolve and print the panel, then exit.
- `--format json` — raw structured output for downstream processing. Default is markdown.
- `--timeout 90` — per-model timeout in seconds.
- `--max-parallel 8` — max concurrent model calls.

## Provider routing

There is no separate CLI to install. The engine uses the Claude Agent SDK (`claude_agent_sdk.query`) with `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` env injection — the same path Ciaobot uses for chats and automations. Model ids resolve as:

- `owner/model` (no `:`) → OpenRouter (`https://openrouter.ai/api`) when `OPENROUTER_API_KEY` is set.
- `:tag` / `:cloud` ids → Ollama (cloud when `CIAO_OLLAMA_API_KEY` is set, local daemon for local models).
- bare aliases (`opus`/`sonnet`/`haiku`) and `claude-*` ids → Anthropic subscription (no env override).

## Workflow

1. **Decide what to review.** Save the artifact to a file (or pipe via stdin). Vault artifacts (`memory-vault/...`) can be passed directly.
2. **Pick `--type` and `--focus`.** A PRD reviewed as `--type prd --focus "mobile edge cases"` gets sharper feedback than a generic doc review.
3. **Run the script.** Stream the markdown output; it's already structured per-model with verdicts, severities, and quotes.
4. **Synthesize.** This is your job, not the script's. Read every per-model verdict and produce a single opinionated review for Raffa:
   - **Consensus issues** first — anything two or more models flagged.
   - **Idiosyncratic but high-confidence** issues second — one model raised them but with confidence 4-5.
   - **Quietly drop** generic best-practice padding that doesn't apply.
   - **Call out disagreement** when models split on a verdict.
   - **End with a single recommendation:** ship, revise (top 2-3 changes), or block (with the dealbreaker).
5. **Second opinion (optional).** Rerun with `--models` set to a different lineup and merge.

## Tone of the synthesis

Match the CLAUDE.md house style: direct, no flattery, concrete fixes. Don't soften the panel's criticism just because Raffa wrote the artifact. If the panel agrees the artifact is solid, say so plainly and move on.

## Examples

**Review a PRD with mobile focus, default panel:**
```bash
$SKILL_DIR/scripts/review.py \
  --input memory-vault/work/projects/active/handheld-flow/PRD.md \
  --type prd \
  --focus "mobile edge cases, iOS vs Android divergence"
```

**Quick gut-check with a fast/cheap Ollama panel:**
```bash
$SKILL_DIR/scripts/review.py --input /tmp/post.md --type "linkedin post" \
  --models deepseek-v4-flash:cloud,gemma4:31b-cloud
```

**Pipe a plan from stdin, get JSON:**
```bash
cat plan.md | $SKILL_DIR/scripts/review.py --type plan --format json > /tmp/review.json
```

**Custom OpenRouter panel:**
```bash
$SKILL_DIR/scripts/review.py --input draft.md --type doc \
  --models anthropic/claude-sonnet-4.5,anthropic/claude-haiku-4.5,anthropic/claude-opus-4.8
```

## Cost and time

- Each model call is one turn, no looping. Expect 20-60s per model in parallel.
- Costs depend on the configured backends (OpenRouter, Ollama Cloud, Anthropic subscription).
- If the artifact is huge (>50k tokens) the panel will be slow and expensive. Trim or summarize before fan-out.

## Failure modes

- A single model failing (timeout, unparseable JSON) is fine — the script marks it failed and the others still produce a useful review. Mention failures in the synthesis if more than one model dropped out.
- If all models fail, check that the intended backend is configured (e.g. `OPENROUTER_API_KEY` for `owner/model` ids, `CIAO_OLLAMA_API_KEY` for `:cloud` ids). Try a single model manually:
  - OpenRouter: `ANTHROPIC_BASE_URL=https://openrouter.ai/api ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY ANTHROPIC_API_KEY= claude -p --model anthropic/claude-haiku-4.5 "say hi"`
  - Ollama: `ANTHROPIC_BASE_URL=https://ollama.com ANTHROPIC_AUTH_TOKEN=$CIAO_OLLAMA_API_KEY ANTHROPIC_API_KEY= claude -p --model kimi-k2.7-code:cloud "say hi"`
