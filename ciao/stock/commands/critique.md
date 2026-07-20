---
description: Multi-model adversarial review — runs the configured critique panel and synthesizes the verdicts.
argument-hint: <topic, artifact, or file path>
---

# Critique: $ARGUMENTS

Run a full **adversarial review** of the target — do not just give your own opinion.

1. **Identify the artifact.** Use `$ARGUMENTS` as the target. If it's a file path (including a `memory-vault/...` vault path), review that file. If it names or references something in the current conversation (e.g. "the plan above", "this draft"), use that content. If `$ARGUMENTS` is empty, review the most recent substantive artifact in the conversation.

2. **Call the `adversarial_review` MCP tool** with the artifact inlined and a sensible `doc_type`/`focus`. It resolves the panel from Settings → Models internally and returns a synthesized markdown report — no file-saving or script invocation needed.

3. **Synthesize the panel's output yourself** — consensus issues first, then high-confidence idiosyncratic ones, call out any verdict disagreement, and end with a single recommendation: ship / revise / block.

If the panel is unavailable or all models fail, say so plainly and fall back to a direct single-model critique instead.
