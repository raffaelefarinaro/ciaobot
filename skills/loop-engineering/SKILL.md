---
name: loop-engineering
description: Fix Ciaobot GitHub issues end-to-end as a loop driven by Addy Osmani's agent-skills — with no argument it takes every open issue not labelled not_ready; given issue numbers, an idea or a plan, it targets that. It asks once whether to run workers as Orca sessions or native subagents in this session, and which workflow tiers/models and review topology to use, then runs spec → plan → build → review → merge into develop for each issue, in parallel where the dependency graph allows. Trigger on "/loop-engineering", "fix the open issues", "work the issue backlog", "implement issue #N with orca", "ship this with a cheap model", "plan it as an issue and have opencode/haiku build it", "run the issue loop".
---

# Loop engineering

> Contributor/project skill — lives in the repo's workspace `skills/` folder, **not** `ciao/stock/skills/`. It is for people working *on* Ciaobot and is not shipped to end-user installs.

You are the **orchestrator**. This skill is the loop; the craft inside each phase comes from the addy `agent-skills` plugin. You own the target list, the plans, every gate, every GitHub write and every merge. Workers do one job and stop. Never trust a worker's claim that tests pass — re-run the gates yourself.

```
intake (§1) → ask once (§2) → per issue:
  spec+plan → issue (§3) ─┬─ worktree off develop
                          ├─ R0  implement   (worker)  → you gate, push, open draft PR   (§5, §6)
                          ├─ R1  review      (fresh)   → verdict                         (§7)
                          ├─ R1  fix         (worker)  → you gate, push
                          ├─ …   up to 5 review rounds
                          └─ APPROVE + CI green → merge into develop, close issue      (§9)
```

| | |
|---|---|
| Repo | `raffaelefarinaro/ciaobot` · Orca repo id `b2c750fd-6af9-4382-8bbb-fa998a38f83d` |
| Base | `develop` (PRs target develop; `Closes #N` does **not** auto-close on a develop merge) |
| Loop state | `~/orca/workspaces/ciaobot/plans/` — `issue-<N>/{plan,record,r<K>-<role>}.md`, `loop-state.md` (outside git) |
| Max review rounds | 5 |

References: `references/prompts.md` (worker prompts), `references/plan-template.md` (issue body), `references/models.md` (model menu, provider rules), `references/orca-mode.md` / `references/subagent-mode.md` (mechanics of each execution mode, with their traps). Read the mode file you use before the first worker.

## 0. Addy skills: where each phase comes from

Resolve the local addy skill directory once before dispatch. Claude workers invoke the installed plugin by name; Codex and OpenCode workers read its skills from absolute local paths. The usual install path is discovered from Claude's plugin registry:

```bash
ADDY=$(jq -r '.plugins["agent-skills@addy-agent-skills"][0].installPath' ~/.claude/plugins/installed_plugins.json)
ls "$ADDY/skills" >/dev/null
```

If the files are missing, stop and ask the user to install `agent-skills@addy-agent-skills`. `/reload-plugins` is only for a Claude session that predates the install; Codex and OpenCode need the absolute paths in their prompts, not a Claude plugin reload.

| Phase | Who | Addy skill(s) |
|---|---|---|
| Vague idea → concrete ask | you | `idea-refine`; `interview-me` only if the user wants to be interviewed |
| Spec | you | `spec-driven-development` → the issue's Problem / design / acceptance / out-of-scope |
| Plan | you | `planning-and-task-breakdown` → the issue's numbered tasks; split oversized issues |
| Implement / fix | worker | `incremental-implementation` + `test-driven-development`; `frontend-ui-engineering` for `web/`; `debugging-and-error-recovery` on red |
| Review | reviewer | `code-review-and-quality` (five axes); `security-and-hardening` for auth/boundary diffs |
| Risky-diff gate | you | Fan out `code-reviewer`, `security-auditor` and `test-engineer` roles through the selected runtime, each following the matching addy skill, then merge their reports |
| Commit hygiene | you | `git-workflow-and-versioning` |

Claude workers invoke installed skills and reviewer personas by name (`agent-skills:<name>`); only a Claude session that predates plugin installation needs `/reload-plugins`. Codex and OpenCode workers receive absolute local `SKILL.md` paths and persona-specific tasks through their own native delegation tools; they do not load Claude agent types. `references/prompts.md` defines the runtime-specific prompt deltas. Use the skills' discipline, not their file conventions: the GitHub issue is the spec and plan — never write `SPEC.md` or `tasks/` into the repo.

## 1. Intake: what to work on

Decide the target from what the user said:

- **Nothing beyond the command** → the open backlog:
  ```bash
  gh issue list --repo raffaelefarinaro/ciaobot --state open --limit 200 \
    --json number,title,labels,body -q '[.[] | select(all(.labels[]; .name != "not_ready"))]'
  gh pr list --repo raffaelefarinaro/ciaobot --state open --json number,headRefName,body
  ```
  Drop every issue labelled `not_ready` and every issue that already has an open PR or is in flight in `plans/loop-state.md`. Flag (don't drop) `report`-labelled ones — they are auto-opened and may be noise.
- **Issue numbers** (`#1323 1326`) → exactly those, even if `not_ready` (say so, then proceed).
- **An idea, a description or a plan** → that is the target. Run `idea-refine` if it is still vague, then it becomes one or more new issues in §3. Do not touch the backlog.

Triage each target in one line: size (S/M/L), files/subsystems it touches, dependencies on other targets, and anything that is a **decision only the user can make** (those go into §2, not into a guess).

## 2. Ask the user — once, using the host runtime's question tool

Show the triage table, then ask only for choices the user has not already made. Use Claude's `AskUserQuestion`, Codex's current input tool, or OpenCode's `question` tool when available; if the host has no question tool, put the choices in one bundled chat question. Never ask again for a choice already stated in the request.

1. **Execution mode**
   - *Orca sessions* — one worktree and visible terminal per worker; use the provider/model the user selects. → `references/orca-mode.md`
   - *This session, native subagents* — use the current host's native delegation tools: Claude Code `Agent`, Codex collaboration tools, or OpenCode's task tool/configured subagents. No Orca sessions. → `references/subagent-mode.md`
2. **Implementer model** — choose the requested workflow tier and resolve its exact available model ID for that runtime in `references/models.md`. Luna/Haiku is the default for implementation-grade plans; Terra/Sonnet for harder logic; preserve an explicit model choice.
3. **Reviewer topology**
   - *Primary reviews implementers* — the primary orchestrator reads the diff, report and gates itself; no reviewer worker.
   - *Fresh reviewer subagent/session* (recommended) — Terra/Sonnet by default, Sol/Opus for the strongest. The primary still judges every finding.
   - *Ship fan-out* — three parallel personas (`code-reviewer`, `security-auditor`, `test-engineer`) merged by you; slowest, for auth/boundary/release-plumbing diffs. Even when not chosen, use it automatically for those diffs (§9).
4. **Scope confirmation** — the issue list to run (all ready targets by default), plus any batched decision questions from triage.

Then run without further check-ins. Ask again only for the escalations in §8–§9, or when a requested model has no working provider (`references/models.md`).

## 3. Spec and plan → GitHub issue (you)

For each target, with `spec-driven-development` then `planning-and-task-breakdown`:

- **Existing issue** — `gh issue view N --comments`. If it already has a plan at `references/plan-template.md`'s level of detail, reuse it. Otherwise investigate and add the plan to the body, keeping the reporter's text under `## Report`.
- **New idea** — file it: title prefix `[Bug]` → label `bug`, `[Feature]` → `enhancement` (see `ciao-support`); `gh issue create … --body-file plans/issue-draft.md`, then move the draft to `plans/issue-<N>/plan.md`.

The plan is written for a model that will **not** explore: exact files, functions and line anchors, the change in each task, the tests (file + name + what they assert), the verify commands, and explicit out-of-scope items. If you cannot write a task concretely, keep reading. Read `AGENTS.md`, `docs/DEVELOPMENT.md`, and `web/README.md` + `DESIGN.md` for anything under `web/`.

**Delegate the investigation for a big plan.** When planning means reading many modules (a removal across subsystems, release plumbing, anything you would otherwise read 20+ files for), delegate a read-only planning role through the selected runtime's native subagent interface with the plan template, the "written for a model that will not explore" rule, and the base sha. Ask for tasks with text anchors, an explicit keep-list with reasons, a precondition check, and grep-based verify steps. Plan small issues yourself meanwhile. Before publishing the plan, check its precondition and spot-check a few anchors yourself — you own the plan. Do not assume every runtime has a Claude agent type named `Plan`.

**Split anything oversized**: more than ~8 files or two subsystems is not one loop. File children (`Part of #N`), keep a checklist comment on the parent, and plan each child against current `develop` just before it runs.

## 4. Parallelism, records, the whole graph

- **Parallel by default.** The queue is a merge order, not a work order. Two loops run concurrently when their file sets do not overlap and neither consumes the other's *unmerged* code. Genuinely blocked → sequence it; never fake a shim. Cap 3–4 implementers at once (each runs the full suite); reviewers overlap freely.
- **One active worker per worktree, always.** Parallelism is across issues.
- **Orca mode:** one Run and one waiter for all loops (`references/orca-mode.md`). **Native subagent mode:** launch independent implementers through the current runtime's native delegation interface; use its own follow-up and completion semantics (`references/subagent-mode.md`).
- **Records.** `plans/loop-state.md` has one line per active issue (worktree, branch, PR, round, status). `plans/issue-<N>/record.md` states what is *true in the code* — shipped shape, files, deviations from the plan with the ruling, gates run, follow-ups, PR and merge sha. Update it whenever you push and at merge.
- **Finish the graph, not a wave.** Work the ready set continuously; when a child merges, start the next ready one unasked. A parent closes only when all its children merge. Issues the loop itself opens (deferred findings, split-outs) join the graph and are cleared the same way unless the user defers them. If you must stop, say exactly what is unbuilt and why.

## 5. Implement round (worker)

1. Create the worktree off `origin/develop` per the mode file; confirm the base is current.
2. Fill `references/prompts.md` (Implement) with the addy skills and runtime deltas. Keep the prompt at `plans/issue-<N>/r0-implement.md` for the record; pass it as text or by absolute path according to the selected runtime.
3. Launch the worker per the mode file and collect its report using that runtime's completion semantics. Never relaunch on silence.

## 6. After every worker turn (you)

1. `git status --short` and `git log --oneline origin/develop..HEAD` (Orca) / `git diff --stat` (subagents). No change → failed turn.
2. Diff vs plan: every task done, nothing out of scope, `tests/` changes match the plan's test list, no existing test weakened.
3. Remove `web/node_modules` / `desktop/runtime` symlinks; make sure they are not staged.
4. Run the CI gates from `AGENTS.md` in the worktree (skip ones the diff can't affect, and say which):
   ```bash
   ~/repos/ciaobot/.venv/bin/mypy ciao
   PYTHONPATH=$PWD ~/repos/ciaobot/.venv/bin/python -m pytest -n auto tests/ -q
   cd web && npm test && npm run build          # only if web/ changed; Node >= 22.22.2
   ```
   Red → do not push; the failures go into an immediate fix turn (does not consume a review round; a second consecutive red turn does).
5. Commit (subagent mode), push, `git ls-remote origin <branch>` to confirm. Compare to the reported commit — a stray engine-backup commit can land on the branch (`references/orca-mode.md`, Traps).
6. Comment on the issue: `R<K> <implement|fix> by \`<model>\`: [\`<sha7>\`](commit link) — <summary>. Gates: mypy ✅ pytest ✅ web ✅/n.a.`
7. After R0 only: `gh pr create --base develop --draft --title "<title without prefix> (#<N>)" --body "Implements #<N>. Implemented by \`<model>\` via loop-engineering."`
8. For UI changes, look at it in the browser before the review round (AGENTS.md: every feature is visually inspected).

## 7. Review round (fresh reviewer, or you)

Run the reviewer the user chose in §2 with the Review prompt. Whoever reviews uses `code-review-and-quality` and produces **one** verdict whose first line is exactly:

```
<!-- loop-engineering round=<K> verdict=APPROVE|CHANGES -->
```

followed by `## Review round <K>: …` and, for CHANGES, numbered Critical/Important findings with file:line and a concrete fix a weaker model can apply without judgment. Suggestions are "(optional)" and never block. An Orca reviewer posts it on the PR itself; a subagent reviewer returns it and you post it with `gh pr comment`.

No marker → re-run once, then review it yourself. Mirror the verdict to the issue.

**Judge the findings before passing them on — including the "Optional" ones.** Reviewers regularly file a real defect as a suggestion and approve anyway (on #1323 a sonnet reviewer noted a too-loose regex as optional and approved; it would have kept the bug the issue was about). Read every optional note and promote any that is a correctness or safety gap to a required fix; when several reviewers independently suggest the same thing, treat it as required. Drop findings you disagree with (say so in the fix prompt) and add anything the reviewer missed — you are accountable for the merge. `CHANGES` → fix round (Fix prompt, same implementer) → §6 → review round K+1. `APPROVE` → §9.

## 8. Round budget

Hard cap **5** review rounds. If the implementer fails twice in a row (no change, or gates red after its turn), offer a stronger model rather than burning rounds. After round 5 without `APPROVE`: stop, leave the PR draft, comment what is still open, and hand the decision to the user (merge as is, you fix it, switch model, or abandon).

## 9. Merge (your call)

Merging is the orchestrator's decision from the evidence — verdict, your own gate run, CI, risk. Do not ask per PR. Escalate only for the genuinely irreversible or the unverifiable: anything touching the operator's live machine or credentials, force-pushing a shared branch, deleting data, release plumbing or secrets, or a failing check you cannot explain. Diffs touching auth/secrets/`docs/REMOTE_BOUNDARY.md`, `desktop/` or release plumbing get the ship fan-out and a security review (plus the `desktop/src-tauri` commands from `docs/DEVELOPMENT.md` if relevant) from you first; green and understood → merge.

**Scope the security review to the PR.** The built-in `/security-review` diffs the branch against `main`, so on a develop-based branch it drags in every unreleased develop commit (80+ unrelated files on the #1303 run). Run it as a subagent instead, with the same instructions, pointed at `git diff origin/develop...HEAD` in the worktree.

1. `git fetch origin && git merge-base --is-ancestor origin/develop HEAD`; if develop moved, merge it, resolve, re-run §6 gates, push. A conflicted merge gets one more review round.
2. Before the **last** PR of a batch, merge all remaining branches onto `origin/develop` in a scratch worktree and run the full gates once.
3. `gh pr ready <PR>` then `gh pr checks <PR> --watch --fail-fast`. Red CI → fix round. A required check that never reports → escalate.
4. `gh pr merge <PR> --repo raffaelefarinaro/ciaobot --merge --delete-branch`.
5. `gh issue close <N> --comment "Merged into develop via #<PR> (<merge sha link>). Ships in the next release."`; tick it on the parent's checklist.
6. Remove the worktree per the mode file; set the record to `merged #<PR>` with the sha; update `loop-state.md`.
7. Report: issue, PR, merge commit, rounds, models, anything deferred.

When the whole target list is done, give one summary table (issue → PR → rounds → model → status) and list anything left unbuilt with the reason.
