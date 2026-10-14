# Native subagent mode — mechanics

Use this when the user selects **this session with subagents**. No Orca sessions or terminals: the current primary stays the orchestrator, creates or selects the issue worktree, and delegates through its own runtime. Read `references/models.md` before selecting models. Never copy tool syntax or lifecycle assumptions from one runtime into another.

A review cycle can use either topology:

- The primary coordinates implementers and reviews their output itself.
- The primary coordinates implementers and a fresh reviewer subagent for each review round. This is the default for substantial changes.

In either topology, the primary reads every report and diff, independently runs the gates, decides which findings to fix, and owns every commit, push, GitHub write and merge. Separate reviewers are fresh workers, never the implementer reviewing its own changes.

## Shared worktree rules

Create each worktree from current `origin/develop` yourself, then give the worker its exact absolute path and tell it to stay there. In the main checkout, the established shape is:

```bash
git -C ~/repos/ciaobot fetch origin
git -C ~/repos/ciaobot worktree add -b raffaelefarinaro/issue-<N>-<slug> \
  ~/repos/ciaobot/.claude/worktrees/issue-<N> origin/develop
```

Do not use Claude `Agent(isolation: "worktree")`: it branches from `main`, which can lag `develop`. Keep one active worker per worktree. Implement/Fix workers run no git commands. Review-only workers may use read-only `git diff`, `git log` and `git show`, but never git mutations, commits or pushes. All native workers are forbidden GitHub writes and Orca lifecycle commands. They return reports to the primary. Follow `references/prompts.md` for per-role instructions.

When a worktree needs `web/`, symlink `web/node_modules` from the main checkout for checks, then remove the symlink before every commit; `.gitignore` does not exclude a symlink. Never use bare `git stash` or `stash pop`: the stash stack is shared across worktrees and sessions.

After each worker, inspect `git status --short` and the diff/stat against the plan; check exact staged scope before committing. The primary runs gates from the worktree. The main venv is an editable install of the main checkout, so Python tests need `PYTHONPATH=$PWD`. After pushing, confirm the remote SHA with `git ls-remote`. Before merging the last PR in a batch, combine remaining branches in a scratch worktree and run the full gates. After merge, remove the issue worktree using the runtime's safe cleanup procedure. These checks remain primary-owned in every runtime.

## Claude Code

Use native `Agent` calls; reviewer personas can use their configured `agent-skills:*` agent types. Follow the Claude `SendMessage`/agent completion semantics exposed in the current session. To continue a worker for a fix, message its active agent; use a fresh implementer only when its context is clearly polluted. Invoke addy skills by name. Reload Claude plugins only when they were installed after this Claude session began; this applies only to Claude.

## Codex

Use the native collaboration tools exposed by the current Codex session (`spawn_agent`, `followup_task`, `send_message`, `wait_agent`). Give every spawn a self-contained message with the plan/review, exact worktree path, scope and no-git/no-GitHub boundaries. Spawn independent workers in parallel only for independent issues. Resolve model overrides from current tool metadata; do not invent model IDs or translate a workflow alias into an unlisted ID. If an explicit model override is set, use `fork_turns: "none"` and include all context the worker needs in its message; otherwise omit the override and let it inherit the session model.

A `send_message` reaches a running worker but does not start an idle one. Use `followup_task` to restart an idle worker for its next fix round. `wait_agent` waits for a completion or update; inspect the returned final report rather than assuming success. A worker's claimed test result is not a gate: the primary reruns all required checks.

Workers read addy skills from the absolute local `SKILL.md` paths supplied in their task. Do not tell Codex workers to invoke a Claude plugin skill by name unless that skill is independently installed in their Codex environment.

## OpenCode

Use OpenCode's native task tool and configured agents, not Orca sessions and not Codex/Claude tool syntax. Inspect the task tool's actual schema in the running OpenCode session before dispatch; do not guess argument names or assume Codex follow-up semantics. Discover configured agents with `opencode debug agents` and available exact model IDs with `opencode models` (verified on OpenCode 2.0.27). An agent should have `mode: subagent`; check its effective `model` before dispatch because a subagent without an explicit model inherits the primary's model. The primary agent's `permission.task` must allow the chosen subagent. Keep a reviewer read-only (`edit: deny`) where the configuration supports it. If the runtime exposes no suitable task tool or configured agent, do not edit live config to make one; use another user-approved mode or ask. A temporary config inside the issue worktree is within this workflow when needed for the already-authorized issue. Never edit the live/global operator configuration.

The primary can review implementer output itself, or delegate a fresh reviewer task per round. In both cases, the primary owns the verdict, checks every report and reruns gates. OpenCode task results and session navigation are runtime-specific; use only the controls surfaced by that OpenCode version.

Read [OpenCode's official Agents docs](https://opencode.ai/docs/agents/) for configured agent model, mode and task-permission behavior. OpenCode workers read addy skills from absolute `SKILL.md` paths, not Claude plugin names.
