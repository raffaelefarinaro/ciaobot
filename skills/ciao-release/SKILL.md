---
name: ciao-release
description: How to cut a Ciaobot release — the patch/minor/major convention, the pre-release checklist (dependencies, docs, capabilities skill, /code-review --fix gate via /pr, and a local install of the candidate via /ciao-dev-install), the prepare-release command, what merging into main triggers, and the known traps. Trigger on "release", "cut a release", "publish", "bump the version", "ship a new version", "prepare-release", or any question about how Ciaobot versioning and publishing work.
---

# Ciaobot Release

> Contributor/project skill — lives in the repo's workspace `skills/` folder, **not** `ciao/stock/skills/`. It is for people working *on* Ciaobot and is deliberately not packaged or shipped to end-user installs. `ciao sync-skills` mirrors it into the generated `.claude/` catalog, which opencode discovers natively — skills get no separate opencode projection, unlike subagents, commands and MCPs. Don't move it into `ciao/stock/`.

Authoritative procedure for cutting a Ciaobot release. `develop` is the source line; `main` is publish-only — **merging a release PR into `main` is the trigger** for everything downstream (tag → GitHub release → engine assets). You never build artifacts or tag by hand.

Canonical companions: `docs/DEVELOPMENT.md` (§ "Branching and releases") and `ciao/release.py`. When this skill and the code disagree, the code wins — say so and update this skill.

Sibling skills this one calls into, rather than restating: **`/pr`** for the review gate's everyday form, **`/code-review`** and **`/simplify`** for the release-range gate, and **`/ciao-dev-install`** to build and run the candidate on this machine before cutting (checklist step 7). Read each at the point it is needed; the summaries here are prompts to do that, not replacements.

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
   - **Replay every merge in the range — `./scripts/check-merges.sh`.** `/code-review` reads the *diff*, and a merge that dropped a feature leaves no suspicious diff: the code is simply absent, exactly as if it had never been written. Nothing in the normal gate looks for that. This script replays the automatic 3-way merge of each merge commit's parents and reports where the committed tree differs — every delta is a hand edit made during conflict resolution, which is the only place this failure can occur.

     ```bash
     ./scripts/check-merges.sh                 # <last tag>..develop
     ./scripts/check-merges.sh v0.11.0..HEAD   # explicit range
     ```

     A delta is not automatically a bug — real conflicts must be resolved by hand — but each one needs a human look, because **no test will do it for you**. v0.12.0 shipped this: merge `ed04d1e9` resolved by taking the pre-feature side, reverting the entire frontend half of the subagent-subchat feature (sidebar rows, the read-only transcript route, the store polling) and committing a 3533-line `ProjectSidebar.vue.tmp` scratch file, while the backend half stayed live and served `/api/subagents/running` to nothing. The PR was green — the same resolution deleted `tests/test_delegates.py` along with the code it covered, so the suite had nothing left to fail on. The tells to look for in a delta: **a deleted test file**, a **stray `.tmp`/`.orig`/`.rej`** artifact, and any file where one side's changes vanished wholesale. Cross-check the survivors with `git grep` for an endpoint or component the release notes claim to add.
3. **Dependencies.** The release tool checks the Python/npm dependencies used to build the engine and prints available updates as `[auto|manual] [safe|major]`; `auto`-flagged ones are bumped on `--apply`, the rest are only reported. These registries are build inputs, not end-user installation channels. Run a plan-only pass first, then decide whether to adopt any `manual` updates in a separate commit before releasing. Don't blanket-upgrade majors as part of a release.
4. **Docs — sync, then prove it.** The docs must describe the product as it is about to ship, not as it was at the last tag. The sync is partly mechanical and partly a judgment call:
   - **The mechanical gate.** `tests/test_architecture_doc.py`, `tests/test_env_vars_documented.py`, and `tests/test_pwa_api_docs.py` fail when a `ciao/` module is missing from `docs/ARCHITECTURE.md`, a `CIAO_*` env var is missing from `INTEGRATIONS.md`, or a route is missing from `PWA_API.md` (state-changing routes also need an Agent recipe). They run inside `pytest tests/` (so `_run_checks` catches them), but run them explicitly **before** the cut — a doc failing after `--apply` has already bumped and committed means a revert-and-rerun:
     ```bash
     env -u PYTHONPATH .venv/bin/python -m pytest tests/test_architecture_doc.py tests/test_env_vars_documented.py tests/test_pwa_api_docs.py
     ```
   - **The stale-claims sweep (the gate cannot do this).** Those tests prove structure, not truth — a paragraph describing a removed engine, a renamed env var, a deleted page, or a renamed CLI flag passes them. For every feature the release removed or renamed (`git diff <last-tag>..develop --stat`, then the removed identifiers), `git grep -n <env var | route | provider id | command>` across `README.md`, `INTEGRATIONS.md`, `PWA_API.md`, `docs/`, and `DESIGN.md`, and delete or update every hit. v0.8.0 shipped exactly this kind of rot: `PWA_API.md` still documented the cloud transcription engine four releases after its removal, and `INTEGRATIONS.md` still claimed n8n was denied by default two releases after the policy was dropped — both caught by a release-time sweep, not by the sync tests.
    - **What to touch.** `README.md` (features/Providers), `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `PWA_API.md` (any new/changed state-changing route **must** be documented here), `docs/AGENT_CLI.md` (the operation catalog must match `ciao/stock/skills/ciao-cli/commands.json` and the dispatcher's `OPERATIONS` table in `ciao/mcp_server.py`), and — when the release touches the UI — `DESIGN.md` / `docs/DESIGN_SYSTEM.md` and the home-lanes plan's status. Commit doc fixes on `develop` before the cut; do not let them ride in the `release: prepare` commit.
5. **The capabilities skill.** For any new user-facing feature, update `ciao/stock/skills/ciao-capabilities/SKILL.md` — add the feature to the right section and add trigger keywords to its frontmatter `description`. Skim the CHANGELOG since the last release tag to catch features that shipped without a catalog entry.
6. **The engine gate — nothing under a native shell exists to check.** CI and `prepare-release` both gate the whole release on `mypy ciao`, `pytest tests/`, `cd web && npm test`, `cd web && npm run build` and `ciao package-smoke --skip-frontend`, which `_run_checks` runs for you. There is no Rust, no `cargo`, no app bundle: the release *is* the engine (`#655`, `#656`).
7. **Install the release candidate on this machine and run it — `/ciao-dev-install`.** Nothing above this line ever *starts* the thing you are about to ship. `pytest` and the `npm` suites all prove the parts compile and their tests pass; none of them boots an engine against a real workspace with real chats, schedules, MCP servers and credentials in it. The `ciao-dev-install` skill builds the PWA, pip-installs the current checkout over the live install preserving the workspace and password, restarts the service, then watches the engine logs. Do it **after** the final source change and **before** `prepare-release`, so what you smoke-test is what gets cut.

   Read that skill and follow it; do not improvise. Three of its rules matter enough to repeat:
   - **Check `service status --json` for `active_chat_ids` first, and again right before the restart.** The restart happens underneath whatever is running. Non-empty means ask the user; never pass `--force` silently.
   - **Build the PWA before the install.** `ciao/web/static/` ships as package data inside the wheel, so an install that ran before `npm run build` bakes in the previous build's assets.
   - **A running process is not a working engine.** Check the engine log for *this* boot only, count `Uvicorn running on` to rule out a crash loop, and confirm the PWA answers. An auth-required install returns `unauthorized` from `/api/startup` — that is a pass, not a failure.

   Then verify the running code really is the code you think it is, because a `pip install` that ran before a source change (or an import resolving to another `ciao` on the machine) silently runs older code:

   ```bash
   .venv/bin/python -c "
   import ciao, pathlib
   print(ciao.__version__, pathlib.Path(ciao.__file__).resolve())"   # must be this checkout
   ```

   This step is also what closes out any issue the release claims to fix: v0.15.0 shipped a fix for a transient startup error, and the dev install is where "it did not recur" was actually established, by grepping the log from this boot's `Uvicorn running on` line onward rather than trusting an older occurrence higher up the file.
8. **This skill.** If the release flow, flags, or traps changed, update `skills/ciao-release/SKILL.md` too.
9. **CHANGELOG sanity.** The tool generates the entry from commits since the last tag. If commits landed on the release branch after `release: prepare`, append them to the entry before merging.

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
- **No Rust toolchain needed, and none is checked.** `_run_checks` runs `mypy ciao`, `pytest tests/`, the `web/` npm test/build, and `ciao package-smoke`; `#655` dropped the shell's npm commands and `#656` deleted the Rust tree, so the release gate never touches `cargo` and there is nothing under it left to gate.
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
  --bump <patch|minor|major> --apply \
  --commit --push --create-pr --ready
```

The `scripts/prepare-release` wrapper is equivalent (`CIAO_PYTHON=.venv/bin/python scripts/prepare-release --bump … --apply --create-pr --ready`) but does **not** unset `PYTHONPATH` — see the trap below. Use `--version X.Y.Z` for an explicit version. Defaults: `--source develop` (cuts `release/vX.Y.Z` from `origin/develop`), `--base main`.

What `--apply` does, in order: bumps `pyproject.toml`, `ciao/__init__.py`, `web/package.json`, `web/package-lock.json` and the service-worker cache names in **both** `web/public/sw.js` and `ciao/web/static/sw.js`; refreshes `CHANGELOG.md`; auto-bumps `auto` dependencies; regenerates the packaged `gws-*` skills if the installed `gws` CLI differs from the pin; runs the full check suite; commits `release: prepare vX.Y.Z`; pushes the branch; opens the PR.

## Rebuild the PWA last

`ciao/web/static/` holds the packaged frontend the wheel serves, but the two
generated parts of it are **not tracked**: `assets/` (hashed bundles) and
`index.html` (the shell that names them). Tracking the shell bought nothing —
the bundles it points at were ignored, so a fresh clone could never serve it —
while its hash line changed on every build, which made every pair of frontend
branches conflict on it. Packaging globs the filesystem, not git, so what ships
is whatever `npm run build` last produced.

That makes the build order load-bearing rather than cosmetic: run `cd web && npm
run build` *after* the final source change. `prepare-release` verifies the
result — `_check_built_pwa` fails the release if the shell is missing or names a
bundle that is not on disk, which is what a stale build looks like after a
branch switch. Without a build you would otherwise get a wheel that installs and
then serves a 404 where the UI should be.

Corollary: if a concurrent session is editing the tree, its unfinished work gets baked into your build output. Check `git status` before building.

## Merging is the trigger

1. CI (`test`) on the PR must be green (`mergeStateStatus` CLEAN) before merging.
2. Merge the PR into `main`. This runs `.github/workflows/release-on-main.yml`, which creates the `vX.Y.Z` tag + GitHub release using `RELEASE_PAT` (a plain `GITHUB_TOKEN` release would **not** fire `release: published`).
3. That fires `publish.yml`, which ships **the engine from the same tag**:
   - `build-engine` (macos) — builds the PWA and the engine wheel, verifies the wheel in a clean environment, signs the engine manifest with the release minisign key, and attaches the wheel, the manifest and its signature, plus the engine installer as both `install.sh` and `install-engine.sh` — the same bytes under both names, because the public one-liner keeps `install.sh` while the hand-over path fetched `install-engine.sh` (#651). Since #653 there is no app archive, no updater feed, no native verifier and no bundled runtime.
   - `release-smoke` (macos) — installs the engine from the release's one-line installer with a restricted PATH, verifies the `ciao` entry point, the install receipt and the LaunchAgent, checks the startup API, then reruns the installer as an update/recovery test.
4. A follow-up job merges `main` back into `develop`.

No manual tag / `gh release create`, no tap push, and no separate desktop release — one merge ships the engine. End users install it with the release URL, which is unchanged from the last release — the same one-liner now installs the engine:

```bash
curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh
```

There is only one version to ship: the Python and PWA versions are bumped together by `--apply`, and they are the release. Nothing else is versioned (`#656` deleted the app's version files).

**Merging the release PR:** the auto-mode classifier blocks `gh pr merge` on the agent-authored release PR unless the user explicitly authorized merging (e.g. "merge #NNN" / "finish then release"). Attempt once; on denial, ask the user to click merge or reply with explicit authorization.

## Known traps

- **Dirty tree → hard failure.** `--apply` refuses on a dirty tree — even one untracked file — with `ReleaseError: working tree is dirty; commit/stash changes or pass --allow-dirty` (`_ensure_clean`, `ciao/release.py:416-422`). It is *not* the silent downgrade to plan-only that earlier versions of this skill described; re-verified 2026-08-24. The check sits *after* the `if not args.apply: return 0` early exit, so a **plan-only pass is safe to run against a dirty tree** — useful for previewing the version and changelog while someone else is mid-edit. Reach for `--allow-dirty` only when you know exactly what is uncommitted: it bakes the tree into the release, and `npm run build` globs the filesystem rather than git, so a concurrent session’s half-finished work ends up inside the shipped wheel. Always verify `__version__` and the `release: prepare` commit afterward.
- **`--source` prefers `origin/<branch>` over your local branch — deliberately.** `_resolve_source_ref` (`ciao/release.py:424-441`) verifies `origin/<source>` first and falls back to a local ref only when no remote matches, so a local branch that *lags* origin cannot silently cut a stale release. The inverse is the trap: when local `develop` is **ahead** of `origin/develop`, `--source develop` cuts from the remote and silently drops every unpushed commit — the release then ships a fraction of what you reviewed, and the changelog looks plausible because it is generated from whatever did get cut. Either push `develop` first, or pass the **commit SHA** as `--source` (`origin/<sha>` fails to verify, so it falls through to the local ref). Check before cutting:

  ```bash
  git rev-list --count origin/develop..HEAD   # 0, or pass an explicit SHA
  ```
- **Double-bump on failed check.** The tool bumps files *before* running checks. If a check fails, `git checkout -- CHANGELOG.md ciao/__init__.py pyproject.toml web/package.json web/package-lock.json` before re-running, or it double-bumps. Two things a failed run leaves behind that are easy to miss: a **second `## vX.Y.Z` section** stacked on the changelog entry that was already there (`grep -c '^## vX\.Y\.Z' CHANGELOG.md` must be 1), and a **`pyproject.toml` auto-dependency bump with no matching `uv.lock`** — every `uv --frozen` step, including `publish.yml`'s engine wheel build, fails on that pair. Revert both files together.

  A **successful** run used to leave the same mismatch, which this trap previously mis-described as a failed-run artifact only. `_apply_auto_dependency_updates` returned no paths, so the commit step never staged `uv.lock`; `pyproject.toml`, `web/package.json` and `web/package-lock.json` were staged anyway because they are independently version-bearing, so only the Python lock fell through — and the tagged commit carried the new pin beside the old lock. Fixed (issue #420): the dependency step now returns `auto_update_paths()` into `touched`, and `_ensure_nothing_left_behind` fails the run if the release commit leaves any modified **tracked** file behind. Untracked files are deliberately ignored there — the generated PWA bundle is gitignored and a shared checkout may hold another session's work. If that guard ever fires, stage what it names into the release commit rather than pushing past it. Revert *before* switching branches: `git checkout <branch>` carries uncommitted changes with you onto the branch you were trying to keep clean.
- **`PYTHONPATH` / stray egg-info.** Never export `PYTHONPATH=.` before running the release/smoke tools — a leftover `ciao.egg-info/` or `ciaobot.egg-info/` at repo root leaks into the "isolated" smoke venv and the top-level wheel gets skipped, failing the probe with `ModuleNotFoundError: No module named 'ciao'` (tell: a bogus pip conflict naming an ancient pre-rename version). Use `env -u PYTHONPATH …`; `rm -rf ciao.egg-info` (gitignored, regenerates) if you see it.
- **A stale `build/` resurrects deleted files into the runtime.** Packaging tools do not always prune old build trees, so a removed module or old frontend asset can be copied into a later artifact. Clean generated build directories before release builds and inspect the staged app/runtime contents.
- **Post-merge watch timing.** Don't grab the latest `publish` run right after merging — it only spawns after `Release on main` finishes creating the tag, so you'd watch the *previous* release's run. Wait for a `Release on main` run on the merge commit, then take the `publish` run newer than it.
- **Never pipe a release-critical command into `tail`/`head` — including the release tool itself.** The pipeline's exit code is the *last* command's, so `tail` returns 0 and swallows the failure. During v0.6.4 that turned a failed `publish` run into a reported-green one; during v0.15.0 the same shape hid a `ReleaseError` from `ciao.release` behind a long dependency report, so the run looked like it had merely printed less than expected. Redirect to a file and grep it instead — that also survives the output being longer than any `tail -N` you would have guessed:

  ```bash
  env -u PYTHONPATH .venv/bin/python -m ciao.release "$(pwd)" … > /tmp/release.log 2>&1
  echo "EXIT=$?"
  grep -nE "ReleaseError|command failed|/pull/" /tmp/release.log
  ```

  For workflow runs, run `gh run watch --exit-status` unpiped, or re-check with `gh run view <id> --json conclusion` afterwards.
- **`pgrep` proves the process exists, not that the engine runs.** The release smoke test must check the startup API and the installed `ciao`, not just process presence. When it fails, inspect the LaunchAgent state and the workspace runtime logs before theorising.
- **The installer must fail closed.** The engine manifest signature and the wheel's digest are both verified before extraction. Never turn a verification error into a warning or add an unsigned fallback.
- **Engine cold start is the installer's job — do not "fix" `release-smoke` by loading launchd.** `install-engine.sh` starts the engine through the `com.ciao.server` LaunchAgent it installs, and a cold start exercises the real path. Adding `--load-launchd` to the workflow would hide a genuine cold-start defect if one ever does appear.
- **Release propagation lag.** Verify that the GitHub release contains the wheel, its signed manifest, and both installer names before diagnosing an installer failure.
- **Put Node 22 on PATH in the shell that runs the release tool.** `_run_checks` shells out to `npm run test` and `npm run build` in `web/`, where `scripts/check-node.mjs` hard-fails below `^22.22.2 || ^24.15.0 || >=26`. The failure kills the run *after* it has already bumped the version files and regenerated the changelog, and it looks unrelated to its real cause (`ReleaseError: command failed (1): npm run test`). The fix is per-shell and is not inherited from another terminal, so set it in the same command:

  ```bash
  . "$NVM_DIR/nvm.sh" && nvm use 22
  env -u PYTHONPATH .venv/bin/python -m ciao.release "$(pwd)" …
  ```

  Verify with `node --version` before starting, and clean up per the double-bump trap below after any failed attempt.
- **`mypy ciao` is in the check suite now — but it cannot see a deleted module.** `_run_checks` runs mypy *first and blocking* (`ciao/release.py:457-466`), deliberately mirroring `ci.yml`'s bare `mypy ciao` step — contrast the adjacent `pip-audit --desc || true`, which is why the suite does not gate on that one. Earlier versions of this skill said to run mypy by hand because `_run_checks` omitted it; that is no longer true, re-verified 2026-08-24. What mypy still will **not** catch: with `ignore_missing_imports`, a leftover `from ciao.providers.<deleted> import …` stays green under both mypy *and* pytest. After removing any `ciao/` module, sweep by importing every module in the package:

  ```bash
  env -u PYTHONPATH .venv/bin/python -c "
  import importlib, pkgutil, ciao
  bad = []
  for m in pkgutil.walk_packages(ciao.__path__, 'ciao.'):
      try: importlib.import_module(m.name)
      except Exception as e: bad.append((m.name, type(e).__name__, str(e)[:120]))
  print('import errors:', len(bad))
  for b in bad: print(' ', b)
  "
  ```

  Separately, treat any commit that reached `develop` without a PR as unreviewed by CI — that is how v0.8.0's two `no-any-return` errors in `ciao/transcripts.py` first surfaced, as a red release PR after the branch was already cut and pushed.
- **vitest flake.** vitest can flake with fork-worker timeouts right after the pytest run; re-running `npm run test` cleanly passes.
- **Frontend/build mismatch.** Rebuild the PWA after the final source change (`ciao/web/static/` ships as package data inside the wheel), then keep the release smoke test as the final gate.
- **Confirm which branch you are on before committing.** A long release session can drift: work intended for `develop` lands on `release/vX.Y.Z`, or vice versa. During v0.6.0 the checkout moved to `develop` mid-flight and four commits landed there instead of the release branch, which then needed `git merge develop` to pull them into the release. That is the right shape anyway (features on `develop`, release branch cut from it), so the fix is: check `git branch --show-current`, land features on `develop`, and merge `develop` into the release branch for anything that arrives after the cut. Renaming a local branch does **not** move an open PR's head — you need a new PR.
- **Verify release-critical git state with `rtk proxy git …`, not bare `git`.** The RTK hook rewrites `git` invocations and filters their output, and the filtering is not always faithful. During v0.12.0 `git status --short` reported a **clean tree while seven files were modified**, and `git ls-files` omitted a file that `git ls-tree HEAD` showed as a committed blob. Both would have been silent disasters at release time: `--apply` traps on a dirty tree, so a false "clean" reading means either a `ReleaseError` you cannot explain or, with `--allow-dirty`, someone else's half-finished work baked into the shipped wheel. `rtk proxy <cmd>` runs the raw command unfiltered. Use it for `status`, `ls-files`, `ls-tree`, and anything else you are about to make a cut/commit decision on. A tell that you are reading filtered output: the same fact answered differently by two git commands, or a loop/pipeline whose per-item results disagree with the same command run alone.
- **Never `git add -A`.** Another session may be editing the same checkout. Stage explicit file lists, and diff-check what you staged. If a conflicted file's mtime is moving, stop and coordinate rather than resolving it underneath someone.
- **An explicit file list is still not enough — `git add <file>` stages the *whole* file.** In a shared checkout that sweeps in whoever else's half-finished work is sitting in it, and the result is worse than muddied authorship: during v0.6.0 it produced a commit that failed its own tests, because the backend half of someone's feature went in while the test update for it was still uncommitted. CI went red on a commit whose message had nothing to do with the failure, and `git bisect` no longer works across it. Before committing, `git diff --cached` and check every hunk is yours. When a file genuinely holds both, isolate your hunks: copy the file aside, `git checkout HEAD -- <file>`, re-apply only your change, stage, then restore the copy unstaged.
- **A failed run leaves the release branch behind, and the retry cannot recreate it.** `_checkout_release_branch` uses `git switch -c release/vX.Y.Z origin/develop`, which refuses when the branch already exists — so the second attempt dies with `ReleaseError: command failed (128): git switch -c release/vX.Y.Z origin/develop`, an error about git that says nothing about the real problem. Before re-running, revert the bumps (see the double-bump trap), then delete the leftover branch — after checking it holds nothing:

  ```bash
  rtk proxy git log --oneline develop..release/vX.Y.Z   # must be empty
  rtk proxy git ls-remote --heads origin release/vX.Y.Z # must be empty (never pushed)
  rtk proxy git switch develop && rtk proxy git branch -D release/vX.Y.Z
  ```

  Order matters: revert *first*. The bumps are uncommitted, and `git switch` carries them onto `develop`.
- **Pass the review effort explicitly, and check the level it reports back.** `/code-review` reuses the last level you typed when it cannot parse one, so an argument in the wrong position is silently downgraded — on v0.15.0 a request for `ultra` over a 14k-line range ran at **medium** and said so only in its own summary line. Read that line. A release wants `high` or above; combined with the findings cap, a medium pass over a large diff is close to a spot check.
- **Verify a review finding against the code before acting on it, and write the test it did not.** Two failure modes seen on v0.15.0, both from an otherwise good pass:
  - **Findings can be wrong.** One reported `pwa_host`'s default moving `127.0.0.1` → `0.0.0.0` as an exposure change riding in the release. It was the opposite: `from_env`'s fallback was *already* `0.0.0.0`, so every real install already bound all interfaces, and the commit only aligned the dataclass default used by directly-constructed configs. Shipping a "fix" for that would have been a real regression. Another described a whitespace-only `CIAO_WORKSPACE` as resolving to the cwd; it actually resolved to `<cwd>/"   "`, a directory *named* three spaces — same bug, different fix.
  - **`--fix` applies edits and adds no tests.** Eleven fixes landed across nine files with zero test changes. Write regressions for the ones that are testable, and prove each is non-vacuous by reverting the fix and watching it fail — two of the tests written that way turned out to prove nothing (one never reached the changed line at all; another's fixture was not deep enough to trigger the bug) and were rewritten or deleted.
- **Absolute repo_root.** Pass an absolute path — shell cwd persistence between tool calls is unreliable.

## After it ships

- Confirm the `vX.Y.Z` tag + GitHub release exist and artifacts are attached.
- **Confirm the release assets and the one-line installer are present, then run the installer smoke test. A machine on the release reports its own version in Settings → Home, and the PWA's package-update card can apply the next engine release without a separate package-manager action.**
