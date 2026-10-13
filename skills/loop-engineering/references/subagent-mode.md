# Subagent mode — mechanics

Used when the user picked **this session with subagents** in the intake (SKILL.md §2). No Orca terminals: you stay the orchestrator in this chat, and each implementer, fixer or reviewer is a Claude Code `Agent` call. Only Claude models are available here (`sonnet`, `haiku`, `opus`); an opencode model means Orca mode.

Proven on 2026-10-11: 11 issues, Opus planning → Haiku implementers → orchestrator gating and merging, one fix round each on average.

## Worktree

Do **not** use `Agent(isolation: "worktree")`: it cuts the worktree from `main` (behind `develop`) and the worktree guard refuses git inside it. Make the worktree yourself:

```bash
git -C ~/repos/ciaobot fetch origin
git -C ~/repos/ciaobot worktree add -b raffaelefarinaro/issue-<N>-<slug> ~/repos/ciaobot/.claude/worktrees/issue-<N> origin/develop
```

If the plan touches `web/`, symlink `web/node_modules` from `~/repos/ciaobot/web/node_modules` (delete it before every commit — `.gitignore` misses symlinks).

## Launching a worker

One `Agent` call per role and round. Workers run **no git at all** — you commit, push, open the PR and merge.

```
Agent(
  description: "#<N> r<K> <implement|fix|review>",
  subagent_type: "general-purpose",          # reviewer: "agent-skills:code-reviewer"; fan-out adds "agent-skills:security-auditor" + "agent-skills:test-engineer"
  model: "<sonnet|haiku|opus>",               # the model the user picked for this role
  prompt: <the filled template from references/prompts.md, with the "Subagent mode" deltas applied>
)
```

- Independent issues: issue all their implementer `Agent` calls in **one message** so they run in parallel (cap 3–4; each runs the full pytest suite).
- A subagent runs in the background; you are notified when it finishes. Do not poll, and do not predict its result.
- To send a fix round to the same implementer with its context intact, `SendMessage` to its agent id; start a fresh `Agent` only if its context is clearly polluted.
- Workers invoke addy skills by name (`Invoke the agent-skills:<name> skill`); the reviewer persona types already carry their review framework and load `agent-skills:code-review-and-quality` themselves.
- The reviewer is always a **fresh** agent (or you — SKILL.md §7). Never let the implementer review its own work.

## Commit and push (you)

After the worker returns, apply SKILL.md §6 (gates, diff check), then:

```bash
cd ~/repos/ciaobot/.claude/worktrees/issue-<N>
rm -f web/node_modules
git add -A && git status --short          # check for foreign or stray files first
git commit -m "<fix|feat>(<area>): <summary> (#<N>)"
git push -u origin HEAD && git ls-remote origin raffaelefarinaro/issue-<N>-<slug>
```

## Cleanup

After merge: `git -C ~/repos/ciaobot worktree remove ~/repos/ciaobot/.claude/worktrees/issue-<N> --force`.

## Traps

- **Never use bare `git stash` / `stash pop`.** The stash stack is shared across all worktrees and sessions; a pop on a clean tree once applied another session's old stash. Use WIP commits.
- **In zsh, `set -- $x` does not word-split.** Loop over issue numbers with explicit args.
- **Before merging the last PR of a batch**, merge all remaining branches onto `origin/develop` in a scratch worktree and run the full gates once — it catches cross-PR test interactions that no single PR shows.
- **The main venv is an editable install of `~/repos/ciaobot`**, not the worktree — every pytest run needs `PYTHONPATH=$PWD` from the worktree root.
- **Subagents claim green tests they never ran.** Re-run the gates yourself every round.
