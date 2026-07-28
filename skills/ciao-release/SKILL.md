---
name: ciao-release
description: How to cut a Ciaobot release — the patch/minor/major convention, the pre-release checklist (dependencies, docs, capabilities skill, /code-review --fix gate via /pr), the prepare-release command, what merging into main triggers, and the known traps. Trigger on "release", "cut a release", "publish", "bump the version", "ship a new version", "prepare-release", or any question about how Ciaobot versioning and publishing work.
---

# Ciaobot Release

> Contributor/project skill — lives in the repo's workspace `skills/` folder, **not** `ciao/stock/skills/`. It is for people working *on* Ciaobot and is deliberately not packaged or shipped to end-user installs. `ciao sync-skills` mirrors it into `.claude/skills/` (Claude Code) and `.agents/skills/` (Codex). Don't move it into `ciao/stock/`.

Authoritative procedure for cutting a Ciaobot release. `develop` is the source line; `main` is publish-only — **merging a release PR into `main` is the trigger** for everything downstream (tag → GitHub release → PyPI → Homebrew tap). You never build artifacts or tag by hand.

Canonical companions: `docs/DEVELOPMENT.md` (§ "Branching and releases") and `ciao/release.py`. When this skill and the code disagree, the code wins — say so and update this skill.

## Versioning convention (SemVer-by-impact)

Pick the bump from user-facing impact, not diff size:

- **patch** (`--bump patch`, default) — bug fixes, internal refactors, doc/test/CI changes, dependency bumps with no behavior change. Nothing new the user can do.
- **minor** (`--bump minor`) — any new user-facing capability or notable behavior change (e.g. conversation forks, cross-provider consultations, a new provider/backend, a new page). Backward-compatible.
- **major** (`--bump major`) — breaking changes to what users or their data depend on: vault layout/format, workspace layout, the CLI surface/flags, the PWA API contract, or config that requires manual migration.

When unsure between patch and minor, ask: "could a user notice something new or different?" If yes → minor.

**Check the bump against the actual diff, not the request.** A user asking for "a patch release" is naming the ritual, not auditing the scope — and scope often grows after the branch is cut. Read the changelog you just generated: if it has an `### Added` section with real features, it is a minor. Say so and let the user decide; v0.6.0 was requested as a patch and had to be renumbered mid-flight after the convention above was applied to what had actually landed. Renumbering costs a rewrite of every version-bearing file, the changelog heading, the branch name, and the PR — cheap, but cheaper still before the PR is open.

## Before you cut — the pre-release checklist

Do these on `develop` (or a short prep branch merged into develop) **before** running prepare-release:

0. **Merging a PR into the release means re-reviewing it.** A PR that was green against `develop` can still break the release branch: it may conflict with something already cut, and resolving that conflict by hand is exactly where behavior gets dropped. If a PR lands after the branch exists, merge it into `develop`, then `git merge develop` into the release branch, then re-run the review gate on the merge — do not trust the PR's own green CI to cover the merged result.
1. **Survey open PRs and issues.** Before cutting, list what's outstanding — `gh pr list --state open` and `gh issue list --state open` on `raffaelefarinaro/ciaobot`. Surface them to the user and **ask** whether any open PRs should land in this release (merge into `develop` first) and whether any reported issues should be fixed before cutting. Never auto-merge PRs or auto-close issues — the user decides what's in scope for the release. Once decisions are made, merge the chosen PRs / land the fixes on `develop` before continuing.
2. **Fresh review of the release surface — mandatory, blocking step.** Take a clean look at everything shipping since the last release tag — `git log --oneline <last-tag>..develop` and `git diff <last-tag>..develop`. Read it as a reviewer, not the author. Then, **before running `prepare-release`**, run the same quality gate as `/pr` on that range — this is not conditional on convenience, it's a required gate like step 1:
   - `/code-review --fix <last-tag>..develop` — correctness plus reuse/simplification/efficiency; pass a **higher effort** (or `ultra`) for a release. If the diff touches auth, secrets, or external input, run `security-review` instead (or in addition).
   - Inspect applied edits with `git diff`; keep, amend, or discard before committing. Explicitly tell the user why any finding is deferred.
   A release is the checkpoint where small messes get paid down, not deferred further. `/code-review` nominally covers the cleanup angles too, but on a large diff its findings cap spends every slot on correctness and the cleanup tail is dropped — so on a release, **run `/simplify` as well**. In v0.6.0 it was the only pass that caught a merge resolution which had silently deleted a feature's markup while leaving ~90 lines of its script wired up and unreachable. Only skip `/code-review` if it is genuinely absent from the environment (check with the `Skill` tool / `/help`) — if so, say that explicitly to the user and do the equivalent review by hand instead of silently moving on. Everyday feature PRs use `skills/pr/SKILL.md` (`/pr`) for the same gate on the branch diff.
3. **Dependencies.** The release tool already checks PyPI/npm and prints available updates as `[auto|manual] [safe|major]`; `auto`-flagged ones (e.g. the Claude Agent SDK) are bumped on `--apply`, the rest are only reported. Run a plan-only pass first (command below, no `--apply`) to see the list, then decide whether to adopt any `manual` updates in a separate commit before releasing. Don't blanket-upgrade majors as part of a release.
4. **Docs.** Update anything the change touched: `README.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `PWA_API.md` (any new/changed state-changing route **must** be documented here).
5. **The capabilities skill.** For any new user-facing feature, update `ciao/stock/skills/ciao-capabilities/SKILL.md` — add the feature to the right section and add trigger keywords to its frontmatter `description`. Skim the CHANGELOG since the last release tag to catch features that shipped without a catalog entry.
6. **This skill.** If the release flow, flags, or traps changed, update `skills/ciao-release/SKILL.md` too.
7. **CHANGELOG sanity.** The tool generates the entry from commits since the last tag. If commits landed on the release branch after `release: prepare`, append them to the entry before merging.

Once the release PR is open, give its diff one more fresh read before merging — the `release: prepare` commit adds version/CHANGELOG/dependency changes that weren't in your pre-cut review. Prefer `/code-review --comment` on the open PR so findings land as inline comments; still act on anything that should block the merge.

### Review the fixes, and anything that lands after the cut

Two things the v0.6.0 release learned the hard way:

- **A review pass does not cover the code it caused you to write.** The first pass found four unauthenticated-endpoint bugs; the fixes for them introduced two new ones (a 404 branch that masked real JSON errors, and an auth gate that made headless hosts unprotectable). Re-run the gate on the delta — `/code-review <the-commit-the-last-review-saw>..HEAD` — not just on the original range. Repeat until a pass comes back with nothing that blocks.
- **Findings are capped.** The workflow-backed review reports its top ~10 and drops the rest; its own summary says how many were dropped. If the release is large, a clean-looking report may just be a full one. Read the `stats` block.

Scope re-reviews to the delta rather than the whole release range, or you re-litigate findings you already fixed and burn the cap on them.

## Environment prerequisites

The release tool runs `pytest`, `npm run test`/`npm run build` (in `web/`), and a package smoke test **with the same interpreter that launched it**. System Python (3.9) can't even import `release.py`.

- Use the repo `.venv` (Python 3.12+, `ciaobot` editable-installed) or a dedicated `python3.13 -m venv .venv-rel && .venv-rel/bin/pip install -e ".[test]"`.
- `cd web && npm ci` at least once so `vitest` exists.
- `gh` authenticated (for `--create-pr`).
- Start from a **clean** tree on `develop` (see the dirty-tree trap below).

## The command

Plan-only first (writes nothing — inspect the version, CHANGELOG, and dependency report):

```bash
env -u PYTHONPATH .venv/bin/python -m ciao.release "$(pwd)" --bump <patch|minor|major>
```

Then apply, commit, push, and open a ready-for-review PR into `main`:

```bash
env -u PYTHONPATH .venv/bin/python -m ciao.release "$(pwd)" \
  --bump <patch|minor|major> --apply --commit --push --create-pr --ready
```

The `scripts/prepare-release` wrapper is equivalent (`CIAO_PYTHON=.venv/bin/python scripts/prepare-release --bump … --apply --create-pr --ready`) but does **not** unset `PYTHONPATH` — see the trap below. Use `--version X.Y.Z` for an explicit version. Defaults: `--source develop` (cuts `release/vX.Y.Z` from `origin/develop`), `--base main`.

What `--apply` does, in order: bumps `pyproject.toml`, `ciao/__init__.py`, `web/package.json`, `web/package-lock.json`, and the service-worker cache names in **both** `web/public/sw.js` and `ciao/web/static/sw.js`; refreshes `CHANGELOG.md`; auto-bumps `auto` dependencies; regenerates the packaged `gws-*` skills if the installed `gws` CLI differs from the pin; runs the full check suite; commits `release: prepare vX.Y.Z`; pushes the branch; opens the PR.

## Rebuild the PWA last

`ciao/web/static/` is **tracked** (the packaged wheel serves from there), so the built `index.html` in the release commit must reference the asset hashes of the code you are actually shipping. Run `cd web && npm run build` *after* the final source change and commit the result. If you rebuild early and then fix something, the packaged frontend is a mixed state — it will boot, so nothing fails loudly.

Corollary: if a concurrent session is editing the tree, its unfinished work gets baked into your build output. Check `git status` before building.

## Merging is the trigger

1. CI (`test`) on the PR must be green (`mergeStateStatus` CLEAN) before merging.
2. Merge the PR into `main`. This runs `.github/workflows/release-on-main.yml`, which creates the `vX.Y.Z` tag + GitHub release using `RELEASE_PAT` (a plain `GITHUB_TOKEN` release would **not** fire `release: published`).
3. That fires `publish.yml`: builds PWA + wheel + sdist, publishes to PyPI (trusted publishing, env `pypi`), attaches artifacts to the release, and its `update-homebrew-tap` job bumps the `raffaelefarinaro/homebrew-ciaobot` formula.
4. A follow-up job merges `main` back into `develop`.

No manual tag / `gh release create` / tap push. `pip install ciaobot` and `brew install raffaelefarinaro/ciaobot/ciaobot` both ship every release.

**Merging the release PR:** the auto-mode classifier blocks `gh pr merge` on the agent-authored release PR unless the user explicitly authorized merging (e.g. "merge #NNN" / "finish then release"). Attempt once; on denial, ask the user to click merge or reply with explicit authorization.

## Known traps

- **Dirty tree → silent downgrade.** `--apply` silently becomes plan-only (exit 0!) if the tree is dirty — even one untracked file. If version files stay unbumped after a "successful" run, re-run with `--allow-dirty`. Always verify `__version__` and the `release: prepare` commit afterward.
- **Double-bump on failed check.** The tool bumps files *before* running checks. If a check fails, `git checkout -- CHANGELOG.md ciao/__init__.py pyproject.toml web/package.json web/package-lock.json` before re-running, or it double-bumps.
- **`PYTHONPATH` / stray egg-info.** Never export `PYTHONPATH=.` before running the release/smoke tools — a leftover `ciao.egg-info/` or `ciaobot.egg-info/` at repo root leaks into the "isolated" smoke venv and the top-level wheel gets skipped, failing the probe with `ModuleNotFoundError: No module named 'ciao'` (tell: a bogus pip conflict naming an ancient pre-rename version). Use `env -u PYTHONPATH …`; `rm -rf ciao.egg-info` (gitignored, regenerates) if you see it.
- **Never `pip install -U ciao`** — that's an unrelated PyPI package. The distribution is `ciaobot`; self-update uses GitHub release wheels.
- **Post-merge watch timing.** Don't grab the latest `publish` run right after merging — it only spawns after `Release on main` finishes creating the tag, so you'd watch the *previous* release's run. Wait for a `Release on main` run on the merge commit, then take the `publish` run newer than it.
- **Verification lag.** PyPI's `/pypi/<pkg>/json` `info.version` lags a few minutes — verify via the `/simple/ciaobot/` index. `raw.githubusercontent.com` caches the tap formula ~5 min — verify the bump via `gh api repos/raffaelefarinaro/homebrew-ciaobot/contents/Formula/ciaobot.rb`.
- **vitest flake.** vitest can flake with fork-worker timeouts right after the pytest run; re-running `npm run test` cleanly passes.
- **`npm test` has pre-existing failures — establish the baseline before you blame yourself.** As of v0.6.0, 13 tests across `productTour.test.ts` and two other files fail with `localStorage` undefined (a jsdom-environment problem, not a flake — it reproduces every run, and CI does not run `npm test`, so it never surfaced there). Get the baseline count with `git stash && npm test && git stash pop` before assuming a change broke something. Do not "fix" these as part of a release.
- **Confirm which branch you are on before committing.** A long release session can drift: work intended for `develop` lands on `release/vX.Y.Z`, or vice versa. During v0.6.0 the checkout moved to `develop` mid-flight and four commits landed there instead of the release branch, which then needed `git merge develop` to pull them into the release. That is the right shape anyway (features on `develop`, release branch cut from it), so the fix is: check `git branch --show-current`, land features on `develop`, and merge `develop` into the release branch for anything that arrives after the cut. Renaming a local branch does **not** move an open PR's head — you need a new PR.
- **Never `git add -A`.** Another session may be editing the same checkout. Stage explicit file lists, and diff-check what you staged. If a conflicted file's mtime is moving, stop and coordinate rather than resolving it underneath someone.
- **An explicit file list is still not enough — `git add <file>` stages the *whole* file.** In a shared checkout that sweeps in whoever else's half-finished work is sitting in it, and the result is worse than muddied authorship: during v0.6.0 it produced a commit that failed its own tests, because the backend half of someone's feature went in while the test update for it was still uncommitted. CI went red on a commit whose message had nothing to do with the failure, and `git bisect` no longer works across it. Before committing, `git diff --cached` and check every hunk is yours. When a file genuinely holds both, isolate your hunks: copy the file aside, `git checkout HEAD -- <file>`, re-apply only your change, stage, then restore the copy unstaged.
- **Absolute repo_root.** Pass an absolute path — shell cwd persistence between tool calls is unreliable.

## After it ships

- Confirm the `vX.Y.Z` tag + GitHub release exist and artifacts are attached.
- Confirm the new version on the PyPI `/simple/` index and the tap formula bump (via `gh api`, not raw).
- On ≥0.4.28 the local server self-restarts after a `brew upgrade` (InstallWatcher); no manual `launchctl kickstart` needed.
