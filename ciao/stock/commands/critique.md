---
description: Multi-model adversarial review — runs the configured critique panel and synthesizes the verdicts.
argument-hint: <topic, artifact, or file path>
---

# Critique: $ARGUMENTS

Run a full **adversarial review** of the target — do not just give your own opinion.

1. **Identify the artifact.** Use `$ARGUMENTS` as the target. If it's a file path (including a `memory-vault/...` vault path), review that file. If it names or references something in the current conversation (e.g. "the plan above", "this draft"), write that content to a scratch file and review the file. If `$ARGUMENTS` is empty, review the most recent substantive artifact in the conversation.
2. **Run the panel** with `ciao critique --input <artifact-file> --type <spec|plan|doc|code|...> --focus <optional focus>` (add `--context` for extra author context). Use `ciao`, not `python3 -m ciao.critique`: a packaged install puts only the `ciao` wrapper on PATH, and an external `python3` has neither the module nor its dependencies. It resolves the panel from Settings → Models, sends the artifact to every model on it, and prints a synthesized markdown report; pass `--models` to override the panel for this run.
3. **Synthesize the panel's output yourself** — consensus issues first, then high-confidence idiosyncratic ones, call out any verdict disagreement, and end with a single recommendation: ship / revise / block.

If the panel is unavailable or all models fail, say so plainly and fall back to a direct single-model critique instead.