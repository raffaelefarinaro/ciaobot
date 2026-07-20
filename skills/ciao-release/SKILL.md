---
name: ciao-release
description: How to cut a Ciaobot release — the patch/minor/major convention, the pre-release checklist (dependencies, docs, capabilities skill), the prepare-release command, what merging into main triggers, and the known traps. Trigger on "release", "cut a release", "publish", "bump the version", "ship a new version", "prepare-release", or any question about how Ciaobot versioning and publishing work.
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

## Before you cut — the pre-release checklist

Do these on `develop` (or a short prep branch merged into develop) **before** running prepare-release:

1. **Survey open PRs and issues.** Before cutting, list what's outstanding — `gh pr list --state open` and `gh issue list --state open` on `raffaelefarinaro/ciaobot`. Surface them to the user and **ask** whether any open PRs should land in this release (merge into `develop` first) and whether any reported issues should be fixed before cutting. Never auto-merge PRs or auto-close issues — the user decides what's in scope for the release. Once decisions are made, merge the chosen PRs / land the fixes on `develop` before continuing.
2. **Fresh review of the release surface — mandatory, blocking step.** Take a clean look at everything shipping since the last release tag — `git log --oneline <last-tag>..develop` and `git diff <last-tag>..develop`. Read it as a reviewer, not the author. Then, **before running `prepare-release`**, invoke both quality skills on that diff and act on their findings — this is not conditional on convenience, it's a required gate like step 1:
   - `/simplify` — reuse/simplification/efficiency cleanups (quality only).
   - `/code-review` (`security-review` instead if the diff touches auth, secrets, or external input) — correctness/bug hunt; pass a higher effort for a release.
   Apply the fixes each skill surfaces (or explicitly tell the user why a finding is being deferred) before moving on to step 3. A release is the checkpoint where small messes get paid down, not deferred further. Only skip a skill if it is genuinely absent from the environment (check with the `Skill` tool / `/help`) — if so, say that explicitly to the user and do the equivalent review by hand instead of silently moving on.
3. **Dependencies.** The release tool already checks PyPI/npm and prints available updates as `[auto|manual] [safe|major]`; `auto`-flagged ones (e.g. the Claude Agent SDK) are bumped on `--apply`, the rest are only reported. Run a plan-only pass first (command below, no `--apply`) to see the list, then decide whether to adopt any `manual` updates in a separate commit before releasing. Don't blanket-upgrade majors as part of a release.
4. **Docs.** Update anything the change touched: `README.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `PWA_API.md` (any new/changed state-changing route **must** be documented here).
5. **The capabilities skill.** For any new user-facing feature, update `ciao/stock/skills/ciao-capabilities/SKILL.md` — add the feature to the right section and add trigger keywords to its frontmatter `description`. Skim the CHANGELOG since the last release tag to catch features that shipped without a catalog entry.
6. **This skill.** If the release flow, flags, or traps changed, update `skills/ciao-release/SKILL.md` too.
7. **CHANGELOG sanity.** The tool generates the entry from commits since the last tag. If commits landed on the release branch after `release: prepare`, append them to the entry before merging.

Once the release PR is open, give its diff one more fresh read before merging — the `release: prepare` commit adds version/CHANGELOG/dependency changes that weren't in your pre-cut review.

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

What `--apply` does, in order: bumps `pyproject.toml`, `ciao/__init__.py`, `web/package.json`, `web/package-lock.json`; refreshes `CHANGELOG.md`; auto-bumps `auto` dependencies; regenerates the packaged `gws-*` skills if the installed `gws` CLI differs from the pin; runs the full check suite; commits `release: prepare vX.Y.Z`; pushes the branch; opens the PR.

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
- **Absolute repo_root.** Pass an absolute path — shell cwd persistence between tool calls is unreliable.

## After it ships

- Confirm the `vX.Y.Z` tag + GitHub release exist and artifacts are attached.
- Confirm the new version on the PyPI `/simple/` index and the tap formula bump (via `gh api`, not raw).
- On ≥0.4.28 the local server self-restarts after a `brew upgrade` (InstallWatcher); no manual `launchctl kickstart` needed.
