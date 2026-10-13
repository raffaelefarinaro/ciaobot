# Orca mode — mechanics

Used when the user picked **Orca sessions** in the intake (SKILL.md §2). Every worker — implementer, fixer, reviewer — is a visible interactive agent chat in its own Orca terminal tab, started and supervised through `orca orchestration`. The orchestrator (you) stays in its own Orca terminal as the coordinator.

Parallel loops share one Run and one waiter (SKILL.md §4). Concurrency cap: 3–4 cheap workers on one Mac.

## Worktree

```bash
orca worktree create --repo id:b2c750fd-6af9-4382-8bbb-fa998a38f83d --name issue-<N>-<slug> \
  --base-branch develop --issue <N> --no-parent --json
```

Save `result.worktree.id` (the full `<repoId>::<path>`) and the path. `git -C <path> fetch origin && git -C <path> log -1 origin/develop` — the base must be current `origin/develop`; if the local develop lagged, `git -C <path> reset --hard origin/develop` before any worker runs (nothing to lose yet). Write the plan against the worktree, not the main checkout — the main checkout may be on another branch or behind.

Frontend tooling (see memory: worktree setup): if the plan touches `web/`, symlink `web/node_modules` from `~/repos/ciaobot/web/node_modules`; if it touches `desktop/`, symlink `desktop/runtime`. These symlinks escape `.gitignore` — you delete them before every push (SKILL.md §6).

Set the card: `orca worktree set --worktree id:<wt> --comment "R0 implementing (<model>)" --workspace-status in-progress --json`.

## Running a worker session

Every session — implement, review, fix — is a normal interactive agent chat in its own Orca terminal tab, started and supervised through `orca orchestration`, so the user can watch and type into it like any other Orca session. Never use headless `opencode run` / `claude -p` for workers.

**Once per loop**, from your own Orca terminal (you are the coordinator):

```bash
orca orchestration run-create --objective "loop-engineering #<N>" --json      # save result.run.id as <run>
```

**Model for opencode** — Orca's `--model` does not apply to opencode; it reads the project config. Before the first cheap-worker session, write `<wt>/opencode.json` and git-exclude it (the exclude file is shared by all worktrees, so this is one line, once):

```bash
printf '{"$schema":"https://opencode.ai/config.json","model":"<model>"}\n' > <wt>/opencode.json
EX=$(git -C <wt> rev-parse --path-format=absolute --git-path info/exclude); grep -qx '/opencode.json' "$EX" || echo '/opencode.json' >> "$EX"
```

The TUI footer shows the active model (e.g. "Space Bunny Alpha OpenRouter") — check it with `orca terminal read` on the first launch.

**Per session:**

1. Write the round's prompt to `plans/issue-<N>/r<K>-<role>.md` from `references/prompts.md`.
2. Open the agent TUI in a visible tab and wait until it is ready — a prompt typed during startup is silently lost (seen with `worker-start --agent opencode2`: dispatch "accepted", TUI input box empty):

   ```bash
   # cheap worker
   orca terminal create --worktree id:<wt> --title "#<N> r<K> <role>" --command opencode --json
   # big reviewer (Claude Code orchestrator; `codex -m <model>` if you are Codex)
   orca terminal create --worktree id:<wt> --title "#<N> r<K> review" --command 'claude --model <reviewer model> --permission-mode bypassPermissions' --json

   orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json   # require wait.satisfied
   ```

   **Anthropic models always run through the Claude Code CLI, never OpenRouter.** The
   reviewer is a `claude` session, so it uses the Claude Code subscription and its own
   model ids — do not point it at `openrouter/anthropic/...`, and do not launch the
   reviewer as an `opencode` session with an Anthropic model in `opencode.json`
   (OpenRouter may simply be out of credit, which fails the round on arrival with
   "requires more credits, or fewer max_tokens"; see Traps). The reviewer models are
   `claude --model claude-sonnet-5-5` (default) or `claude --model claude-opus-5-5`
   (strongest), both billed to the subscription. For a headless one-off review,
   `claude -p "<prompt>" --model claude-opus-5-5 --allowedTools "Read,Glob,Grep,Bash"`
   works and is fine to smoke-test with.

   **OpenAI models run through opencode on the `openai` provider, not OpenRouter.** When
   a reviewer or implementer is an OpenAI model (`openai/gpt-6.1-sol`,
   `openai/gpt-6-luna`, …), launch an `opencode` session and write that id into
   `<wt>/opencode.json` (`{"model":"openai/gpt-6.1-sol"}`) — the `openai` provider is
   the user's own OpenAI auth and is billed there, so it is the choice when Anthropic
   quota or OpenRouter credit is the blocker. Do not route an OpenAI model through
   `openrouter/openai/...`. **If the model you were asked to use has no working
   provider — not the Claude Code CLI, not an opencode provider, not OpenRouter —
   stop and ask the user** rather than silently substituting a different model or a
   different provider.
3. Dispatch the task into that terminal. Orca injects the worker preamble (Task/Dispatch IDs, how to send `worker_done`):

   ```bash
   orca orchestration worker-start --run <run> --worktree id:<wt> --terminal <handle> \
     --task-title "#<N> r<K> <role>" --spec "Read and follow ~/orca/workspaces/ciaobot/plans/issue-<N>/r<K>-<role>.md" --json
   ```
   Save `result.dispatchId`. Exit code non-zero → read `failedStage`; do not relaunch blindly. If `stage` stays `input_accepted` and `orca orchestration worker-read --dispatch <id> --source auto --json` shows an empty input box after a minute, the prompt was lost: `worker-abandon --dispatch <id>`, then `worker-start --task <taskId> --retry-of <id> --worktree id:<wt> --terminal <handle>`.
4. Wait for the worker (run this in a background shell so you are notified):

   ```bash
   orca orchestration check --run <run> --wait --types "worker_done,escalation,question" --timeout-ms 900000 --json
   ```
   Output can hold more than one JSON document — decode them in sequence. Workers also send `heartbeat` messages ("alive", with a `phase`); they prove liveness only — ack them and keep waiting. When Orca prompts you with "You have N orchestration messages", run an unfiltered `check --run <run>` so heartbeats don't pile up at the head of the queue, and ack them. Validate `payload.dispatchId` is the one you started; answer a `question` with `orca orchestration reply`; ack each delivery with `check --ack <deliveryId>`. A timeout is a checkpoint: inspect with `worker-read` / `worker-list --json` and keep waiting; never relaunch on silence. `outcome: failed` → treat the round as failed.
5. After `worker_done`: verify the facts yourself (commit sha exists, PR comment posted), then `orca orchestration worker-release --dispatch <id> --json` to close the session.

Workers never push, never touch GitHub (except the reviewer's PR comments), never switch branches. You do all of that. One active session per worktree at a time.

## Traps

- **Write every prompt file to an absolute `~/orca/...` path, and verify it exists before dispatching.** The plan paths in this skill are all under `~/orca/workspaces/ciaobot/`, which is *outside* the repo — but a file-writing tool invoked with a path relative to the checkout (`…/repos/ciaobot/../orca/…`) silently lands in `/Users/<you>/repos/orca/…` instead, while `mkdir -p ~/orca/…` creates the real directory and leaves it empty. The two look fine until a worker reports the task file is missing. **A wrong-path dispatch is indistinguishable from a lost prompt** (see the `input_accepted` trap below): the fix is `worker-abandon` plus a `--retry-of` re-dispatch, and retrying with the same wrong path burns a second round. Before every `worker-start`, `ls -l` the exact path you put in `--spec`. When a worker does ask, answer with the path and the fact that you got it wrong — do not abandon and re-dispatch, since the session is waiting and healthy.
- **A worker may stop to ask whether a GitHub write is allowed, and that is correct behaviour — answer, do not re-dispatch.** The Orca preamble forbids GitHub writes while this skill's Review step requires the verdict comment, so a careful worker hits the conflict and asks instead of guessing. `references/prompts.md`'s Review template now states the carve-out inline, but a session started from an older copy of that file will still ask. The reviewer is the one worker type allowed to write to its PR (see "Running a worker session"); authorise exactly that, keep everything else prohibited, and say so in one message.
- **opencode has no `--model` flag in its TUI and Orca's `--model` skips opencode.** The model comes from `<wt>/opencode.json` (see "Running a worker session"); git-exclude it or `git add -A` commits it.
- **Anthropic reviewer models go through Claude Code, not OpenRouter.** The reviewer is a `claude` session; `claude-sonnet-5-5` / `claude-opus-5-5` are subscription-billed and always work. Launching the reviewer as an `opencode` session with `openrouter/anthropic/...` fails the instant OpenRouter is out of credit — the TUI shows `This request requires more credits, or fewer max_tokens … can only afford N`, the round dies before reading the diff, and no verdict is posted. If you see that, do not retry OpenRouter: relaunch the round as a `claude` reviewer. Confirm the balance with `curl -s https://openrouter.ai/api/v1/credits -H "Authorization: Bearer $OPENROUTER_API_KEY"` before blaming the reviewer for a silent non-answer.
- **OpenAI models go through opencode on the `openai` provider, not OpenRouter.** Set `<wt>/opencode.json` to `{"model":"openai/<id>"}`; the `openai` provider is the user's own auth and bills there, which is what to use when OpenRouter credit is exhausted. If the model you were asked to run has no working provider (not the Claude Code CLI, not an opencode provider, not OpenRouter), **stop and ask the user** — do not silently substitute another model or provider.
- **Headless runs are invisible and unreliable — don't use them for workers.** `opencode run` without `--standalone` returns within seconds while the session keeps working (seen on #564 R0), and `terminal create --command '<cmd>'` leaves an interactive shell behind, so `--for exit` never fires (#564 R1). If you ever need a one-off headless smoke test, use `opencode run --standalone … ; exit`.
- **`terminal wait` timing out still prints a result.** Read `wait.satisfied`.
- **Cheap models claim green tests they never ran**, and sometimes edit tests to pass. Check the diff of `tests/` against the plan's test list every round.
- **The main venv is an editable install of `~/repos/ciaobot`**, not the worktree — without `PYTHONPATH=$PWD` you test the wrong code.
- **Don't let two workers share a worktree at once.** One session per worktree at a time; the reviewer starts only after the fixer's `worker_done` and release.
- **Killing a worker's TUI does not stop the session — only `worker-abandon`/`interrupt` does.** A worker reported `process_exited` (OS-killed, e.g. OOM) can still be running inside the shared `opencode serve` backend and keep editing its worktree: the TUI client dies, the server-side session does not. There is no client process to find, so `pgrep`/`lsof` for `opencode` misses it. Symptoms: files keep changing under a finisher, or stray `pytest`/`orca … heartbeat` processes with the worktree as cwd. When you stand a worker down, `worker-abandon` the dispatch *and* interrupt its session (`opencode api post /api/session/<id>/interrupt`, and find it via `GET /api/session/active` + matching `location.directory`); then confirm the tree is stable (git status hash unchanged over a few seconds) before letting the replacement commit. Never start a second session in a worktree whose first session was only killed — and keep concurrent heavy workers to ~2 on one Mac; a third plus a full suite can OOM-kill an agent mid-write.
- **Issue plan edits after R0 are the source of truth.** If review shows the plan was wrong, fix the plan in the issue body first (note the change in a comment), then send the fix round.
- **A worker that boots the engine in its worktree can push stray commits onto the PR branch.** Once the automatic memory-backup service (#689) exists, any engine a worker starts against its worktree commits the durable scope (`AGENTS.md`, vault, a dev `.env`/`AGENTS.md.lock` left by tests) and pushes it to the branch's upstream — so `git ls-remote origin <branch>` can be ahead of the commit the worker reported, with a `backup <ts>Z` / `<branch> session commit <ts>` message. Always compare the local commit to `git ls-remote origin <branch>` before opening the PR; drop the stray with `git push --force-with-lease` (the branch is yours) and remove the untracked test scaffolding so it does not recur.
- **Other sessions share the main checkout.** Untracked files there (this skill included, until committed) can vanish under another session's git operations. Keep loop state under `~/orca/workspaces/ciaobot/plans/`.
