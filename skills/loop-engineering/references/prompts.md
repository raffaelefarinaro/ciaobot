# Worker prompts

Fill the `<…>` fields and keep each prompt at `~/orca/workspaces/ciaobot/plans/issue-<N>/r<K>-<role>.md`. Orca dispatches "Read and follow <path>" with its `worker_done` preamble. Native subagents receive the filled prompt as task text or an absolute path, using their runtime's actual interface.

## Addy skills in worker prompts

Every template names the addy `agent-skills` the worker must follow. Fill `<SKILLS>` for the runtime:

- **Claude worker** (native `Agent` or a `claude` Orca session): `Invoke the agent-skills:<name> skill`. Claude loads the installed plugin by name. `/reload-plugins` applies only when it was installed after the current Claude session began.
- **Codex or OpenCode worker**: give absolute local paths: `Read and follow <ADDY>/skills/<name>/SKILL.md`, where `<ADDY>` is resolved before dispatch. These runtimes do not load the Claude plugin by name. Do not paste skill text into the prompt.

Workers follow the skills' *discipline* (thin slices, red→green, five-axis review), not their file conventions: no `SPEC.md`, `tasks/plan.md` or `tasks/todo.md` in the repo — the GitHub issue is the spec and plan. Workers in all native subagent modes make no commits; the primary commits. Orca workers follow the Orca prompt's commit instructions.

## Native-subagent deltas

Apply these edits for Claude `Agent`, Codex collaboration workers and OpenCode task workers, not Orca sessions:

- Replace every `worker_done` step with: "Your final message is your report; it is returned to the primary orchestrator."
- Implement/Fix: replace the commit step with: "Run no git commands. The primary orchestrator commits."
- Review: remove the entire Orca-specific `AUTHORIZED` GitHub-write paragraph. Replace every instruction to post inline findings or a summary comment with: "Return your findings and exactly one verdict in the required marker/format to the primary. Do not post to GitHub." The primary must post the returned verdict with `gh pr comment` per SKILL.md §7; this is required, not optional.
- Give the worker the exact absolute issue worktree and file scope. Do not imply it can edit shared main checkout or other worktrees.
- Codex dispatch uses the current `spawn_agent` metadata and task name. If the primary sets a model override, it uses the exact exposed model ID and `fork_turns: "none"`; include the complete brief because no prior turns are inherited. Fix rounds use `followup_task` for an idle worker; `send_message` alone does not restart it.
- OpenCode dispatch uses the actual task tool schema exposed by that runtime and a configured subagent. Check its exact model; an omitted subagent model inherits the primary. Do not copy Claude `Agent` or Codex collaboration syntax into an OpenCode task.
- Claude uses `Agent`/`SendMessage` semantics. Do not assume its plugin or tool lifecycle in Codex or OpenCode.
- For native review workers, adapt Review steps 2, 4 and 5: inspect with read-only git commands such as `git diff`, `git log` and `git show`; return inline findings and the single marker-formatted verdict to the primary; do not run `gh pr comment` or any other GitHub write. The primary posts that verdict per SKILL.md §7. Preserve the template's GitHub-posting instructions only for an Orca reviewer explicitly authorized by the Orca review workflow.

## Implement (R0, cheap model)

```markdown
You are implementing GitHub issue #<N> in the Ciaobot repo. You are in the worktree <path> on branch `raffaelefarinaro/<name>`. Stay on this branch.

1. Read the plan: `gh issue view <N> --repo raffaelefarinaro/ciaobot`. The "Implementation plan", "Tests" and "Out of scope" sections are binding. Then read AGENTS.md.
   Follow these skills while you work: <SKILLS: incremental-implementation, test-driven-development; + frontend-ui-engineering if web/ changes; + debugging-and-error-recovery when a gate goes red>. The issue is your spec and task list — do not create SPEC.md or tasks/ files.
2. Implement the plan task by task, one thin slice at a time: for each task write the listed test first and watch it fail, then make it pass. Do not change anything outside it. Do not refactor, rename, or reformat unrelated code. If a step is impossible as written, stop, do not improvise — explain why in your final message.
3. Add or update exactly the tests listed. Do not edit any other test to make it pass.
4. Run the commands in the plan's "Verify" section, plus:
   `~/repos/ciaobot/.venv/bin/mypy ciao`
   `PYTHONPATH=$PWD ~/repos/ciaobot/.venv/bin/python -m pytest -n auto tests/ -q`
   <if web: `cd web && npm test && npm run build`>
   Fix failures caused by your change. Always use `PYTHONPATH=$PWD` — without it pytest tests a different checkout.
5. Commit everything with one commit: `git add -A && git commit -m "<fix|feat>(<area>): <summary> (#<N>)"`. Never commit `web/node_modules` or `desktop/runtime` symlinks. Do NOT push. Do NOT use gh to write anything.
6. Final message: the commit sha, a bullet list of what changed per file, the exact test/gate results (paste the summary lines), and anything from the plan you could not do.
7. When done, send `worker_done` as your Orca preamble describes, with `--outcome succeeded` (or `failed` if you could not complete the plan) and the commit sha in the summary.
```

## Review (R<K>, big model, fresh session)

```markdown
You are reviewing PR #<PR> for issue #<N> in raffaelefarinaro/ciaobot, review round <K> of 5. You are in the worktree <path>. Do not edit files, commit, or push — review only.

AUTHORIZED, on PR #<PR> only: posting review comments, per steps 2 and 4 below. Your Orca preamble forbids GitHub writes, and this is the single documented exception — the reviewer is the one worker type allowed to write to the PR (§4). Everything else stays prohibited: no labels, no ready/merge/close, no review approval or merge, no issue writes, no comments on any other issue or PR, no `git push`, no branch switch, no file edits, no commits. Read-only `gh` commands (`gh pr view`, `gh issue view`, `gh api` GET) are expected and fine. If a comment fails to post, do not retry more than once — report `--outcome failed` with the error so the coordinator can post it. Do not stop to ask whether posting the verdict comment is allowed; it is.

1. Read the plan and history: `gh issue view <N> --repo raffaelefarinaro/ciaobot --comments` and `gh pr view <PR> --comments`. <If K>1: also read the previous verdict comments and check each earlier finding was actually addressed.>
2. Review using <SKILLS: code-review-and-quality; + security-and-hardening if the diff touches auth, secrets, `/api` or `/ws` routes or `docs/REMOTE_BOUNDARY.md`; + performance-optimization if it touches a hot path>. Cover all five axes (correctness, readability, architecture, security, performance) and tag each finding Critical / Important / Suggestion. Post inline findings on the PR. If a `/code-review` command is available in this environment (`ls .opencode/command .claude/commands 2>/dev/null`), use `/code-review high <PR> --comment` and read its output critically — do not paste it. If there is no such command, do the review yourself from `git diff origin/develop...HEAD` and post findings directly. **Do not stall on a missing command and do not treat its absence as a blocker:** the verdict comment in step 4 is the deliverable and inline comments are optional. This repo does not currently ship `/code-review`, and a worker that assumes it does will waste the round.
3. Separately check the diff (`git diff origin/develop...HEAD`) against the plan:
   - every step and every listed test implemented;
   - nothing outside the plan or listed in "Out of scope" changed;
   - no existing test weakened or deleted;
   - acceptance criteria met.
4. Post exactly ONE summary comment with `gh pr comment <PR> --body-file <tmpfile>`. Its first line must be exactly:
   `<!-- loop-engineering round=<K> verdict=APPROVE -->` or `<!-- loop-engineering round=<K> verdict=CHANGES -->`
   Then: `## Review round <K>: APPROVE|CHANGES`, and for CHANGES a numbered list of required fixes, each with file:line, the problem, and the concrete fix — specific enough for a less capable model to apply without judgment. Mark optional nits as "(optional)"; they never block APPROVE.
   APPROVE only when there are no Critical or Important findings, the plan is fully implemented, and nothing out of scope changed. Suggestions are "(optional)" and never block.
5. Final message: the verdict and the URL of the summary comment.
6. When done, send `worker_done` as your Orca preamble describes, with `--outcome succeeded` and a summary starting with the verdict (`APPROVE` or `CHANGES`) and the summary comment URL. Use `--outcome failed` only if you could not post the review.
```

## Fix (R<K>, cheap model)

```markdown
You are fixing review findings on issue #<N> in the Ciaobot repo. You are in the worktree <path> on branch `raffaelefarinaro/<name>`. Stay on this branch.

1. Read the plan: `gh issue view <N> --repo raffaelefarinaro/ciaobot`.
2. Read the review to address: `gh api <verdict comment API URL>` (and the inline comments: `gh api repos/raffaelefarinaro/ciaobot/pulls/<PR>/comments`).
   Follow <SKILLS: debugging-and-error-recovery; test-driven-development — a bug finding gets a failing test first (Prove-It)>.
3. Fix these findings (numbers from the review): <list>. Do NOT act on: <findings the orchestrator rejected, with one-line reason>. <Additional items from the orchestrator, if any.>
   <If this is a gate-failure retry instead: the following gates failed — paste output — fix the cause without editing unrelated tests.>
4. Change nothing else. Run the same gates as before:
   `~/repos/ciaobot/.venv/bin/mypy ciao`
   `PYTHONPATH=$PWD ~/repos/ciaobot/.venv/bin/python -m pytest -n auto tests/ -q`
   <if web: `cd web && npm test && npm run build`>
5. One commit: `git add -A && git commit -m "fix: address review round <K> (#<N>)"`. Do NOT push. Do NOT use gh to write anything.
6. Final message: the commit sha, each finding number → what you did (or why you could not), and the gate results.
7. When done, send `worker_done` as your Orca preamble describes, with `--outcome succeeded` (or `failed` if you could not complete the plan) and the commit sha in the summary.
```
