# Task-eval results

Each file here records one run of `ciao eval tasks` (see [TASK_EVAL.md](../TASK_EVAL.md)): the engine commit, the provider and model of every column, and pass counts and cost per task. The release process adds one file per release; anyone can reproduce a file from the commit it names.

| File | Engine | Models |
|---|---|---|
| [v1.9.0.md](v1.9.0.md) | v1.9.0 candidate, develop `a60fae98` | claude/haiku, opencode/deepseek-v4.1-flash |
| [2026-11-02.md](2026-11-02.md) | develop `06c0dd8e` (after 1.8.1) | claude/haiku, opencode/deepseek-v4.1-flash, opencode/gpt-6-luna |
| [memory-comparison-2026-11.md](memory-comparison-2026-11.md) | develop, Nov 2026 (before and after #2716, #2737) | claude-haiku-5-5 answering, claude-sonnet-5-5 judging |

The memory comparison is a different kind of record: Ciaobot's memory against other local memory systems on LongMemEval-S, a probe of a real vault, and the prompt evals that came out of it. The file describes how to reproduce it.

## Recording a run

1. Install the commit under test in its own venv, from a detached worktree (`docs/TASK_EVAL.md`, "Running it").
2. Run the whole suite, three repeats per model:
   ```bash
   ciao eval tasks run --provider claude --model haiku --repeats 3 --out runs/claude-haiku.jsonl
   ciao eval tasks run --provider opencode --model <provider/model> --repeats 3 --out runs/<model>.jsonl
   ```
3. `ciao eval tasks report runs/*.jsonl` prints the per-task table. Copy it into a new `YYYY-MM-DD.md` (or `vX.Y.Z.md` for a release) with the summary, what changed since the last file, and every check that turned out unfair and was fixed.
4. Keep the JSONL out of the repository. The fixture is synthetic, but the files are large and the summary is what readers need.

Read the numbers with these limits in mind: three repeats per task, one machine, and pass/fail decided by deterministic checks on the end state. A check that rejects a correct answer is a harness bug: fix the check, re-run the task, and say so in the file.
