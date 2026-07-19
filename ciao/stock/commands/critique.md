---
description: Multi-model adversarial review — runs the configured critique panel and synthesizes the verdicts.
argument-hint: <topic, artifact, or file path>
---

# Critique: $ARGUMENTS

Run a full **adversarial review** of the target — do not just give your own opinion.

1. **Identify the artifact.** Use `$ARGUMENTS` as the target. If it's a file path (including a `memory-vault/...` vault path), review that file. If it names or references something in the current conversation (e.g. "the plan above", "this draft"), use that content. If `$ARGUMENTS` is empty, review the most recent substantive artifact in the conversation.

2. **Invoke the adversarial-review skill** (via the Skill tool) to run the multi-model panel. Follow its workflow: save the artifact to a file if needed, pick a sensible `--type` and `--focus`, and run the panel (it uses the panel configured in Settings → Models).

3. **Synthesize the panel's output yourself** — consensus issues first, then high-confidence idiosyncratic ones, call out any verdict disagreement, and end with a single recommendation: ship / revise / block.

If the panel is unavailable or all models fail, say so plainly and fall back to a direct single-model critique instead.
