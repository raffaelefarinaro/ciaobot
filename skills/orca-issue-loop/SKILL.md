---
name: orca-issue-loop
description: Ship a Ciaobot bug fix, enhancement or feature end-to-end through Orca — the big model (you) writes an implementation-grade plan as a GitHub issue, a cheap opencode model implements it in an Orca worktree off develop, a fresh big-model session reviews the PR, the cheap model fixes, up to 5 review rounds, then merge into develop. Trigger on "/orca-issue-loop", "implement issue #N with orca", "ship this with a cheap model", "plan it as an issue and have opencode build it", "run the issue loop", or any request to plan in GitHub and delegate implementation to deepseek/glm/kimi/other cheap models with big-model review.
---

# Orca issue loop

> Contributor/project skill — lives in the repo's workspace `skills/` folder, **not** `ciao/stock/skills/`. It is for people working *on* Ciaobot and is not shipped to end-user installs.

You are the **orchestrator**. You own the plan, every gate, every GitHub write, and the merge. Workers are short-lived sessions that each do one job in one worktree and exit. Never trust a worker's claim that tests pass — re-run the gates yourself.

```
plan (you) → issue ─┬─ worktree off develop
                    ├─ R0  implement  (cheap, opencode run)  → you verify, push, comment issue, open draft PR
                    ├─ R1  review     (big, your CLI)        → verdict comment on PR
                    ├─ R1  fix        (cheap)                → you verify, push, comment issue
                    ├─ …  up to 5 review rounds
                    └─ APPROVE + CI green → merge into develop, close issue, remove worktree
```

Constants:

| | |
|---|---|
| Repo | `raffaelefarinaro/ciaobot` · Orca repo id `b2c750fd-6af9-4382-8bbb-fa998a38f83d` |
| Base | `develop` (PRs target develop; `main` is release-only — `Closes #N` does **not** auto-close on a develop merge) |
| Worktrees | `~/orca/workspaces/ciaobot/<name>`, branch auto-named `raffaelefarinaro/<name>` |
| Prompt files | `~/orca/workspaces/ciaobot/plans/issue-<N>/` (outside git) |
| Records | `plans/issue-<N>/record.md` + the running `plans/loop-state.md` (outside git, §0.1) |
| Max review rounds | 5 |

Prompt templates: `references/prompts.md`. Issue plan template: `references/plan-template.md`. Read both before starting.

## 0. Maximize parallelism (default, not an exception)

The queue is usually a **merge** order, not a **work** order. Most issues in a chain are only sequenced because a PR must merge first, not because the next one cannot be *written* concurrently. Your default is to run as many loops at once as the machine and the dependencies allow; serializing is the exception you justify.

**Find the parallelism before you start anything.** For each queued issue, list the files and subsystems it touches (the plan names them). Two loops may run concurrently when:

- their file sets do not overlap (or overlap only in a file one of them is not editing), **and**
- neither consumes the other's *unmerged* code — an API, a route, a function, a schema. Importing a sibling's finished-and-merged work is fine; importing its open PR is not.

If a "blocked" issue is only blocked by merge order and not by code, start it. If it is genuinely blocked (its plan calls an endpoint the other PR adds), do not fake a shim — sequence it.

**Split anything oversized so it can parallelize.** A plan that needs more than ~8 files or two subsystems is not one loop; split it into children (`Part of #N`) with a checklist on the parent (see §1). A child with no dependency on the rest can run immediately — split a big, mostly-independent prerequisite out and start it now rather than after the parent's first child merges. Prefer a dependency graph of small children over one long chain.

**How parallel loops share one coordinator:**

- **One Run for all of them.** A coordinator terminal binds exactly one Run; create it once and write the id to `plans/shared-run.env`, then every loop's `worker-start` uses `--run <that run>` so all deliveries arrive in one inbox. Do not create a Run per loop.
- **One waiter for the Run.** The `check --wait` waiter is a single slot per Run. Keep one `plans/wait-any.sh` running for all loops and route each delivery to the right issue by `payload.dispatchId`; never start a second waiter (it fails with `waiter_exists`, and killing one can leave its `check --wait` child holding the slot — kill `orchestration check --run` too before restarting).
- **A practical concurrency cap.** Three to four cheap workers at once is the sweet spot on one Mac: each gate run is a full `pytest -n auto tests/` (~70s) plus `npm test`/`mypy`, so more than that mostly contends on CPU and slows every loop. Start the next loop as soon as one settles. Reviewers are I/O-bound and can overlap with implementers freely.
- **One active session per worktree, always.** Parallelism is across *issues*, never two sessions in one checkout (§4).

**Keep the fan-out visible.** Maintain `plans/loop-state.md` (§0.1) with a section per active issue (worktree, branch, PR, round, current status) so a take-over — or you, after a compaction — can see what is running without reading GitHub.

## 0.1 Keep the implementation record current

Alongside the plan, each loop keeps an **implementation record**: `plans/issue-<N>/record.md`. It documents the implementation as it actually shipped, not the intent. It is for the people and models who read the repo later (and for the next issue in the chain), so it must be current at every merge.

Write it when the first worker commits, and rewrite the affected part after every fix round and at merge. Keep it tight and factual:

```markdown
# #<N> — <title> (<status: implementing | in review | merged #PR>)

**Shipped:** <one paragraph: what the change is, in the code as it landed.>
**Files:** <path> — <what it does now>.
**Decisions & deviations:** <a plan detail that changed, why, and the ruling that made it; link the review comment.>
**Verification:** <the exact gates run and their results.>
**Follow-ups:** <issues filed or deferred, by number.>
**PR / merge:** <PR link> · <merge sha>
```

Rules:
- Update `record.md` in the same turn you push a worker's commit, and again on merge (the merge sha and the final status). Do not leave a merged issue's record saying "implementing".
- A record states what is **true in the code**, not what the plan proposed. When review changes the design, the record carries the shipped shape and a one-line note that it superseded the plan.
- The parent/child split, the round history, and any deferred finding go in the record, not only in scattered PR comments.
- `plans/loop-state.md` carries the one-line cross-issue view (what is in flight, what merged); each `record.md` carries the depth for one issue.

## 1. Plan → GitHub issue (you, big model)

**Starting from an existing issue `#N`:** `gh issue view N --comments`. If it already has a plan matching the template's level of detail, reuse it. Otherwise investigate and add the plan (edit the body, keeping the reporter's text under `## Report`).

**Starting from a description:** investigate the codebase properly — read the files you will name, find the tests that cover them, and read `AGENTS.md` / `docs/DEVELOPMENT.md` / `web/README.md` / `DESIGN.md` where relevant. Then fill `references/plan-template.md`.

The plan is written for a model that will **not** explore on its own. It must name exact files, functions and line anchors, the change in each, the new tests (file + test name + what they assert), the commands to verify, and explicit out-of-scope items. If you cannot write a step concretely, you have not investigated enough — keep reading. Split work that needs more than ~8 files or two subsystems into separate child issues (`Part of #N`), track them as a checklist comment on the parent, and run the loop on one child at a time, planning each against the current `develop` just before it runs.

Classification (from `ciao-support`): title prefix `[Bug]` → label `bug`; `[Feature]` → label `enhancement`; small improvements to existing behavior are `[Feature]`/`enhancement` too.

Before creating, ask the user **once**, in a single message:
1. A 5-line summary of the plan (title, label, files touched, tests added, risk).
2. The implementer model — show the numbered list from §2 and let them pick a number or type any `provider/model`.
3. The reviewer — default is your own CLI and model (e.g. `claude --model <your model id>`); they may override.

Then create it:

```bash
gh issue create --repo raffaelefarinaro/ciaobot --title "[Bug] …" --label bug --body-file ~/orca/workspaces/ciaobot/plans/issue-draft.md
```

Move the draft to `plans/issue-<N>/plan.md` once you have the number.

## 2. Implementer model menu

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

## 3. Worktree

```bash
orca worktree create --repo id:b2c750fd-6af9-4382-8bbb-fa998a38f83d --name issue-<N>-<slug> \
  --base-branch develop --issue <N> --no-parent --json
```

Save `result.worktree.id` (the full `<repoId>::<path>`) and the path. `git -C <path> fetch origin && git -C <path> log -1 origin/develop` — the base must be current `origin/develop`; if the local develop lagged, `git -C <path> reset --hard origin/develop` before any worker runs (nothing to lose yet). Write the plan against the worktree, not the main checkout — the main checkout may be on another branch or behind.

Frontend tooling (see memory: worktree setup): if the plan touches `web/`, symlink `web/node_modules` from `~/repos/ciaobot/web/node_modules`; if it touches `desktop/`, symlink `desktop/runtime`. These symlinks escape `.gitignore` — you delete them before every push (§5).

Set the card: `orca worktree set --worktree id:<wt> --comment "R0 implementing (<model>)" --workspace-status in-progress --json`.

## 4. Running a worker session

Every session — implement, review, fix — is a normal interactive agent chat in its own Orca terminal tab, started and supervised through `orca orchestration`, so the user can watch and type into it like any other Orca session. Never use headless `opencode run` / `claude -p` for workers.

**Once per loop**, from your own Orca terminal (you are the coordinator):

```bash
orca orchestration run-create --objective "orca-issue-loop #<N>" --json      # save result.run.id as <run>
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

## 5. After every cheap-worker turn (you)

1. `git -C <wt> status --short` and `git -C <wt> log --oneline origin/develop..HEAD`. No new commit → failed turn. Uncommitted leftovers → inspect; commit them yourself only if they belong to the plan.
2. Review the diff against the plan yourself: every step done, nothing out of scope, `tests/` changes match the plan's test list.
3. Remove the `node_modules` / `runtime` symlinks if present, and make sure they are not staged.
4. Run the CI gates from `AGENTS.md` in the worktree (skip the ones the diff can't affect, say which):
   ```bash
   cd <wt> && ~/repos/ciaobot/.venv/bin/mypy ciao
   cd <wt> && PYTHONPATH=$PWD ~/repos/ciaobot/.venv/bin/python -m pytest -n auto tests/ -q
   cd <wt>/web && npm test && npm run build     # only if web/ changed; Node ≥ 20.19
   ```
   `npm run build` rewrites tracked `static/index.html` — commit that output with the change. Red gates → do **not** push; write the failures into the next fix prompt (see §7).
5. Push: `git -C <wt> push -u origin HEAD`, then confirm with `git ls-remote origin <branch>` (a quiet push can fail silently).
6. Comment on the issue with the commit link:
   ```bash
   gh issue comment <N> --repo raffaelefarinaro/ciaobot --body "R<K> <implement|fix> by \`<model>\`: [\`<sha7>\`](https://github.com/raffaelefarinaro/ciaobot/commit/<sha>) — <one-line summary>. Gates: mypy ✅ pytest ✅ web ✅/n.a."
   ```
7. After R0 only, open a draft PR:
   ```bash
   gh pr create --repo raffaelefarinaro/ciaobot --base develop --head raffaelefarinaro/issue-<N>-<slug> --draft \
     --title "<issue title without the [Bug]/[Feature] prefix> (#<N>)" --body "Implements #<N>. Plan: #<N>. Implemented by \`<model>\` via orca-issue-loop."
   ```
   Put the PR link in the issue comment and the Orca card comment.

## 6. Review round (fresh big-model session)

Launch the reviewer per §4 with the review prompt. It runs `/code-review` on the PR, checks the diff against the plan's acceptance criteria, posts inline comments, and posts **one** verdict comment on the PR whose first line is exactly:

```
<!-- orca-issue-loop round=<K> verdict=APPROVE|CHANGES -->
```

Read it back: `gh pr view <PR> --repo raffaelefarinaro/ciaobot --comments --json comments -q '.comments[-1].body'`. No marker → the review failed; re-run it once, then do the review yourself. Mirror the verdict to the issue: `gh issue comment <N> --body "R<K> review: <verdict> — <PR comment URL>"`.

- `APPROVE` → §8.
- `CHANGES` → fix round: fix prompt points at the verdict comment URL; run the cheap worker; then §5; then review round K+1.

Judge the findings before sending them on: drop ones you disagree with (say so in the fix prompt, so the worker doesn't act on them) and add anything the reviewer missed. You are accountable for the merge, not the reviewer.

## 7. Round budget

A round = one review. Hard cap **5**. A cheap-worker turn that left gates red gets one immediate retry with the failures pasted in, and that retry does not consume a review round; a second consecutive red turn does.

After round 5 without `APPROVE`: stop. Leave the PR as draft, comment on the issue with what is still open, set the card to `in-review` with a comment, and hand the decision to the user (merge as is, you fix it yourself, switch model and continue, or abandon).

## 8. Merge

**Merging is the orchestrator's call.** You own the go/no-go for every merge — the same way you own the plan, the gates, and the GitHub writes. Do **not** ask the user to approve a merge issue by issue; that choice is yours to make from the evidence (review verdict, your own re-run of the gates, CI, and the risk of the diff). Report merges as they happen (`§8.7`); do not gate them on a reply.

Judge the risk yourself:

- **Routine merge** (feature/fix, tests green, reviewer approved, CI green): merge it. This is the default and it needs no one's permission.
- **Escalate to the user only when the action is genuinely irreversible or touches their live machine or credentials** — for example running a real installer/service/`launchctl` on the operator's host, force-pushing a shared branch, deleting data, changing release plumbing or secrets, or merging something the orchestrator cannot evaluate (an unexplained failing check, an auth/boundary change it does not understand). State the specific risk and the decision you need; do not escalate merely because a change is large or in a sensitive directory.
- When the diff touches auth/secrets/remote boundary (`docs/REMOTE_BOUNDARY.md`), `desktop/`, or release plumbing, run the extra gate it needs (`security-review`, the `desktop/src-tauri` commands in `docs/DEVELOPMENT.md`) **yourself**; if that gate is green and you understand the change, merge it. Escalate only if the gate is red or the change is outside what you can verify.

Merge procedure:

1. Rebase check: `git -C <wt> fetch origin && git -C <wt> merge-base --is-ancestor origin/develop HEAD`. If develop moved, `git merge origin/develop`, resolve, re-run §5 gates, push. A conflicted merge is re-reviewed (one more review round, within the cap).
2. `gh pr ready <PR>` then `gh pr checks <PR> --watch --fail-fast`. Red CI → treat as a fix round. A required check that never reports is an escalation, not a merge.
3. `gh pr merge <PR> --repo raffaelefarinaro/ciaobot --merge --delete-branch` (the repo uses merge commits).
4. `gh issue close <N> --repo raffaelefarinaro/ciaobot --comment "Merged into develop via #<PR> (<merge sha link>). Ships in the next release."` — needed because develop isn't the default branch. Tick the child on the parent's checklist comment if there is one.
5. `orca worktree set --worktree id:<wt> --workspace-status completed --comment "merged #<PR>" --json`, then `orca worktree rm --worktree id:<wt> --force --json` (the branch is merged; nothing is lost).
6. Update the records: set `plans/issue-<N>/record.md` to `merged #<PR>` with the merge sha and the final shipped shape (§0.1), and mark the issue merged in `plans/loop-state.md`.
7. Report to the user: issue, PR, merge commit, rounds used, model, anything deferred into follow-up issues.

## Traps

- **opencode has no `--model` flag in its TUI and Orca's `--model` skips opencode.** The model comes from `<wt>/opencode.json` (§4); git-exclude it or `git add -A` commits it.
- **Headless runs are invisible and unreliable — don't use them for workers.** `opencode run` without `--standalone` returns within seconds while the session keeps working (seen on #564 R0), and `terminal create --command '<cmd>'` leaves an interactive shell behind, so `--for exit` never fires (#564 R1). If you ever need a one-off headless smoke test, use `opencode run --standalone … ; exit`.
- **`terminal wait` timing out still prints a result.** Read `wait.satisfied`.
- **Cheap models claim green tests they never ran**, and sometimes edit tests to pass. Check the diff of `tests/` against the plan's test list every round.
- **The main venv is an editable install of `~/repos/ciaobot`**, not the worktree — without `PYTHONPATH=$PWD` you test the wrong code.
- **Don't let two workers share a worktree at once.** One session per worktree at a time; the reviewer starts only after the fixer's `worker_done` and release.
- **Issue plan edits after R0 are the source of truth.** If review shows the plan was wrong, fix the plan in the issue body first (note the change in a comment), then send the fix round.
- **Other sessions share the main checkout.** Untracked files there (this skill included, until committed) can vanish under another session's git operations. Keep loop state under `~/orca/workspaces/ciaobot/plans/`.
