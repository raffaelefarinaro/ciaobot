---
name: adversarial-review
description: Multi-model adversarial review of any artifact before shipping. Spawns separate model calls with different models, collects structured critiques, and synthesizes a weighted review. Trigger on "review", "critique", "red-team", "tear apart", "second opinion", or after producing major artifacts.
---

# Adversarial Review

Sends an artifact to several models that don't share Claude's training distribution, collects structured critiques, and synthesizes them into a single weighted review. The point is to catch what one model alone misses: blind spots, hidden assumptions, weak evidence, contradictions, missing edge cases.

## When to use

- The user explicitly asks for a review, critique, second opinion, red-team, or "tear it apart".
- They're about to ship something high-stakes: a PRD, brief, OKR, plan, customer email, exec deck, public post.
- You just produced a substantive artifact yourself and want it pressure-tested before declaring done.
- Two models you've consulted disagree and you want a wider panel to break the tie.

Skip for trivial outputs (one-line answers, simple lookups). The panel costs real tokens and time; reserve it for things worth getting right.

## How it works

A single in-package CLI (`ciao.critique`) calls the Claude Agent SDK once per model in the panel. Each call is a one-shot (`max_turns=1`, no tools); the artifact is inlined in the prompt. Per-model routing is automatic from the model id shape: `owner/model` ids reach OpenRouter when configured, `:tag`/`:cloud` ids reach Ollama, and bare aliases (`opus`/`sonnet`/`haiku`) stay on the Anthropic subscription.

```bash
$SKILL_DIR/scripts/review.py --input path/to/artifact.md [flags]
```

## Panel selection

**3 models is the right default size.** Two models agreeing is consensus; the third breaks ties. Going to 5+ is mostly redundant — the critiques overlap heavily for marginal new insight. Bump to 5 only when stakes are unusually high or the artifact is genuinely contested.

**Do not hardcode models in the skill.** The panel comes from Ciaobot configuration:

1. **Settings → Models → Adversarial review panel** (persisted in `.runtime/app_settings.json` as `critique_models`). This is the normal source of truth.
2. **`--models a,b,c`** on the CLI for a one-off override.
3. **`CIAO_REVIEW_MODELS`** env var when set.

Inspect the resolved panel without running a review:

```bash
$SKILL_DIR/scripts/review.py --print-panel
$SKILL_DIR/scripts/review.py --models a,b,c --print-panel
```

Each model gets the same adversarial system prompt and returns structured JSON with: verdict (ship | revise | block), confidence (1-5), summary, strengths, issues (with severity blocking | major | minor | nit), missing items, and sharp questions. The script aggregates and prints a markdown report by default.

## Flags

- `--input <path>` — artifact file. If omitted, reads from stdin.
- `--type <kind>` — `spec`, `prd`, `plan`, `brief`, `okr`, `doc`, `email`, `code`, `idea`, etc.
- `--focus "<topic>"` — narrow the review (e.g. `"security"`, `"mobile edge cases"`).
- `--context "<sentence>"` — what reviewers don't know but should.
- `--models a,b,c` — override the configured panel for this run only.
- `--print-panel` — resolve and print the panel from Ciaobot settings, then exit.
- `--format json` — raw structured output for downstream processing. Default is markdown.
- `--timeout 120` — per-model timeout in seconds.
- `--max-parallel 8` — max concurrent model calls.

## Workflow

1. **Decide what to review.** Save the artifact to a file (or pipe via stdin). Vault artifacts (`memory-vault/...`) can be passed directly.
2. **Pick `--type` and `--focus`.** A PRD reviewed as `--type prd --focus "mobile edge cases"` gets sharper feedback than a generic doc review.
3. **Run the script.** Stream the markdown output; it's already structured per-model with verdicts, severities, and quotes.
4. **Synthesize.** This is your job, not the script's. Read every per-model verdict and produce a single opinionated review for the user:
   - **Consensus issues** first — anything two or more models flagged.
   - **Idiosyncratic but high-confidence** issues second — one model raised them but with confidence 4-5.
   - **Quietly drop** generic best-practice padding that doesn't apply.
   - **Call out disagreement** when models split on a verdict.
   - **End with a single recommendation:** ship, revise (top 2-3 changes), or block (with the dealbreaker).
5. **Second opinion (optional).** Rerun with `--models` set to a different lineup and merge.

## Tone of the synthesis

Direct, no flattery, concrete fixes. Don't soften the panel's criticism just because the user wrote the artifact. If the panel agrees the artifact is solid, say so plainly and move on.

## Examples

**Review a PRD with mobile focus (uses Settings panel):**
```bash
$SKILL_DIR/scripts/review.py \
  --input memory-vault/Workspace/drafts/prd.md \
  --type prd \
  --focus "mobile edge cases, iOS vs Android divergence"
```

**One-off override panel:**
```bash
$SKILL_DIR/scripts/review.py --input /tmp/post.md --type doc \
  --models model-a,model-b,model-c
```

**Pipe a plan from stdin, get JSON:**
```bash
cat plan.md | $SKILL_DIR/scripts/review.py --type plan --format json > /tmp/review.json
```

## Cost and time

- Each model call is one turn, no looping. Expect 20-60s per model in parallel.
- Costs depend on the configured backends (OpenRouter, Ollama Cloud, Anthropic subscription).
- If the artifact is huge (>50k tokens) the panel will be slow and expensive. Trim or summarize before fan-out.

## Failure modes

- A single model failing (timeout, unparseable JSON) is fine — the script marks it failed and the others still produce a useful review. Mention failures in the synthesis if more than one model dropped out.
- If all models fail, check Settings → Models and that the intended backend is configured for the panel ids. Try `--print-panel` to see what Ciaobot resolved.
