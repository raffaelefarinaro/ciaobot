---
name: pr
description: Pre-PR quality gate — run /code-review --fix on the branch diff before opening or updating a pull request. Trigger on "open a PR", "create a pull request", "ready for review", "/pr", or when about to push a branch for review.
---

# PR quality gate

> Contributor/project skill — lives in the repo's workspace `skills/` folder, **not** `ciao/stock/skills/`. It is for people working *on* Ciaobot and is deliberately not packaged or shipped to end-user installs. `ciao sync-skills` mirrors it into the runtime-discovered `.claude/skills/` and `.agents/skills/` catalogs. Don't move it into `ciao/stock/`.

Before opening or updating a pull request, run this gate. Do not open the PR until findings are applied or explicitly deferred with the user.

## Steps

1. **Review + apply.** Invoke `/code-review --fix` on the branch diff (commits ahead of upstream plus uncommitted changes). Pass a higher effort for large or risky diffs. If the diff touches auth, secrets, or external input, run `security-review` instead (or in addition).
2. **Inspect.** Read the applied edits with `git diff`. Keep, amend, or discard (`git add -p` / `git checkout -- .`) before committing anything the review wrote.
3. **Open or update the PR** only after step 2 (`gh pr create` / push / update).

Optional after the PR exists: `/code-review --comment` posts findings as inline PR comments.

## Notes

- `/code-review` already covers reuse, simplification, and efficiency findings. Do **not** also require `/simplify` unless you specifically want a cleanup-only autofix without a bug hunt.
- Only skip `/code-review` if it is genuinely absent from the environment (check with the `Skill` tool / `/help`) — say so explicitly and do the equivalent review by hand.
- Release cuts use the same gate on a wider range; see `skills/ciao-release/SKILL.md`.
