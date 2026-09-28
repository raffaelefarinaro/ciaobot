# Worker prompts

Fill the `<…>` fields and write each to `~/orca/workspaces/ciaobot/plans/issue-<N>/r<K>-<role>.md`. The dispatched task spec only says "Read and follow <path>" (Orca adds its own preamble with the `worker_done` command), so the file is the whole brief.

## Implement (R0, cheap model)

```markdown
You are implementing GitHub issue #<N> in the Ciaobot repo. You are in the worktree <path> on branch `raffaelefarinaro/<name>`. Stay on this branch.

1. Read the plan: `gh issue view <N> --repo raffaelefarinaro/ciaobot`. The "Implementation plan", "Tests" and "Out of scope" sections are binding. Then read AGENTS.md.
2. Implement the plan step by step. Do not change anything outside it. Do not refactor, rename, or reformat unrelated code. If a step is impossible as written, stop, do not improvise — explain why in your final message.
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

1. Read the plan and history: `gh issue view <N> --repo raffaelefarinaro/ciaobot --comments` and `gh pr view <PR> --comments`. <If K>1: also read the previous verdict comments and check each earlier finding was actually addressed.>
2. Run `/code-review high <PR> --comment` to post inline findings on the PR. <Use `max` for large or risky diffs.>
3. Separately check the diff (`git diff origin/develop...HEAD`) against the plan:
   - every step and every listed test implemented;
   - nothing outside the plan or listed in "Out of scope" changed;
   - no existing test weakened or deleted;
   - acceptance criteria met.
4. Post exactly ONE summary comment with `gh pr comment <PR> --body-file <tmpfile>`. Its first line must be exactly:
   `<!-- orca-issue-loop round=<K> verdict=APPROVE -->` or `<!-- orca-issue-loop round=<K> verdict=CHANGES -->`
   Then: `## Review round <K>: APPROVE|CHANGES`, and for CHANGES a numbered list of required fixes, each with file:line, the problem, and the concrete fix — specific enough for a less capable model to apply without judgment. Mark optional nits as "(optional)"; they never block APPROVE.
   APPROVE only when there are no correctness bugs, the plan is fully implemented, and nothing out of scope changed.
5. Final message: the verdict and the URL of the summary comment.
6. When done, send `worker_done` as your Orca preamble describes, with `--outcome succeeded` and a summary starting with the verdict (`APPROVE` or `CHANGES`) and the summary comment URL. Use `--outcome failed` only if you could not post the review.
```

## Fix (R<K>, cheap model)

```markdown
You are fixing review findings on issue #<N> in the Ciaobot repo. You are in the worktree <path> on branch `raffaelefarinaro/<name>`. Stay on this branch.

1. Read the plan: `gh issue view <N> --repo raffaelefarinaro/ciaobot`.
2. Read the review to address: `gh api <verdict comment API URL>` (and the inline comments: `gh api repos/raffaelefarinaro/ciaobot/pulls/<PR>/comments`).
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
