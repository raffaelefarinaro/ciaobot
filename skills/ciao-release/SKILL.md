---
name: ciao-release
description: How to cut a Ciaobot release — the patch/minor/major convention, the pre-release checklist (dependencies, docs, capabilities skill, /code-review --fix gate via /pr), the prepare-release command, what merging into main triggers, and the known traps. Trigger on "release", "cut a release", "publish", "bump the version", "ship a new version", "prepare-release", or any question about how Ciaobot versioning and publishing work.
---

# Ciaobot Release

> Contributor/project skill — lives in the repo's workspace `skills/` folder, **not** `ciao/stock/skills/`. It is for people working *on* Ciaobot and is deliberately not packaged or shipped to end-user installs. `ciao sync-skills` mirrors it into the runtime-discovered `.claude/` and `.agents/` catalogs. Don't move it into `ciao/stock/`.

Authoritative procedure for cutting a Ciaobot release. `develop` is the source line; `main` is publish-only — **merging a release PR into `main` is the trigger** for everything downstream (tag → GitHub release → bundled app assets). You never build artifacts or tag by hand.

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
3. **Dependencies.** The release tool checks the Python/npm dependencies used to build the app and prints available updates as `[auto|manual] [safe|major]`; `auto`-flagged ones are bumped on `--apply`, the rest are only reported. These registries are build inputs, not end-user installation channels. Run a plan-only pass first, then decide whether to adopt any `manual` updates in a separate commit before releasing. Don't blanket-upgrade majors as part of a release.
4. **Docs — sync, then prove it.** The docs must describe the product as it is about to ship, not as it was at the last tag. The sync is partly mechanical and partly a judgment call:
   - **The mechanical gate.** `tests/test_architecture_doc.py`, `tests/test_env_vars_documented.py`, and `tests/test_pwa_api_docs.py` fail when a `ciao/` module is missing from `docs/ARCHITECTURE.md`, a `CIAO_*` env var is missing from `INTEGRATIONS.md`, or a route is missing from `PWA_API.md` (state-changing routes also need an Agent recipe). They run inside `pytest tests/` (so `_run_checks` catches them), but run them explicitly **before** the cut — a doc failing after `--apply` has already bumped and committed means a revert-and-rerun:
     ```bash
     env -u PYTHONPATH .venv/bin/python -m pytest tests/test_architecture_doc.py tests/test_env_vars_documented.py tests/test_pwa_api_docs.py
     ```
   - **The stale-claims sweep (the gate cannot do this).** Those tests prove structure, not truth — a paragraph describing a removed engine, a renamed env var, a deleted page, or a renamed CLI flag passes them. For every feature the release removed or renamed (`git diff <last-tag>..develop --stat`, then the removed identifiers), `git grep -n <env var | route | provider id | command>` across `README.md`, `INTEGRATIONS.md`, `PWA_API.md`, `docs/`, and `DESIGN.md`, and delete or update every hit. v0.8.0 shipped exactly this kind of rot: `PWA_API.md` still documented the cloud transcription engine four releases after its removal, and `INTEGRATIONS.md` still claimed n8n was denied by default two releases after the policy was dropped — both caught by a release-time sweep, not by the sync tests.
   - **What to touch.** `README.md` (features/Providers), `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `PWA_API.md` (any new/changed state-changing route **must** be documented here), `docs/MCP.md` (the tool catalog must match `@tool(name=…)` in `ciao/mcp_server.py`), and — when the release touches the UI — `DESIGN.md` / `docs/DESIGN_SYSTEM.md` and the home-lanes plan's status. Commit doc fixes on `develop` before the cut; do not let them ride in the `release: prepare` commit.
5. **The capabilities skill.** For any new user-facing feature, update `ciao/stock/skills/ciao-capabilities/SKILL.md` — add the feature to the right section and add trigger keywords to its frontmatter `description`. Skim the CHANGELOG since the last release tag to catch features that shipped without a catalog entry.
6. **The desktop gate — run it if anything under `desktop/` changed.** `pytest tests/` and the `npm run build`s do not compile Rust, build the Swift voice sidecar, or assemble `Ciaobot.app`, so a desktop change can be green locally and still fail CI's `build-desktop` job — after the tag exists. `./scripts/check-desktop.sh` runs the same commands CI does (sidecar build, `cargo fmt --check`, `cargo clippy -D warnings`, `cargo test`, then a universal `tauri build`) and asserts the sidecar ends up bundled, universal, signed, and runnable inside the built app. `prepare-release` runs it as part of its checks, but run it yourself first: finding a clippy failure before the release branch exists is much cheaper. Needs Rust 1.90.0 and `swiftc`; `--fast` skips the bundle build.
7. **This skill.** If the release flow, flags, or traps changed, update `skills/ciao-release/SKILL.md` too.
8. **CHANGELOG sanity.** The tool generates the entry from commits since the last tag. If commits landed on the release branch after `release: prepare`, append them to the entry before merging.

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
- **A Rust toolchain, or plan to use `--skip-frontend`.** `_run_checks` puts `cd desktop && npm run test` (which is `cargo test`) and `cd desktop && npm run build` in the *same* group as the web checks, so there is no way to run the web checks locally without also needing `cargo`. Without Rust installed the run dies with `ReleaseError: command failed (127): npm run test` — a 127 from the desktop step, not a broken web suite. **`--skip-frontend` is not covered by the PR's CI on the desktop half.** `ci.yml` defines exactly one job, `test`; `build-desktop` lives in `publish.yml` behind `if: github.event_name == 'release'`, so nothing compiles Rust or Swift until *after* the tag exists, and a break there costs a version bump. Before merging with `--skip-frontend`, run `npm run test`, `npm run build`, and `npm run lint` in `web/` by hand **and** `./scripts/check-desktop.sh` yourself — that script is the only pre-merge desktop gate there is. (v0.7.2 was cut believing CI's `build-desktop` gated the PR; it does not, and the release's 500-line Swift sidecar had never been compiled anywhere. It passed, but that was luck, not a gate.)
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

What `--apply` does, in order: bumps `pyproject.toml`, `ciao/__init__.py`, `web/package.json`, `web/package-lock.json`, the five desktop version files (`desktop/package.json`, `desktop/package-lock.json`, `desktop/src-tauri/Cargo.toml`, `desktop/src-tauri/Cargo.lock`, `desktop/src-tauri/tauri.conf.json`), and the service-worker cache names in **both** `web/public/sw.js` and `ciao/web/static/sw.js`; refreshes `CHANGELOG.md`; auto-bumps `auto` dependencies; regenerates the packaged `gws-*` skills if the installed `gws` CLI differs from the pin; runs the full check suite; commits `release: prepare vX.Y.Z`; pushes the branch; opens the PR.

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
3. That fires `publish.yml`, which ships **the engine and desktop app from the same tag**:
   - `build-desktop` (macos) — builds the PWA and two architecture-specific embedded Python runtimes, assembles the universal `Ciaobot.app`, then attaches the versioned app archive, its signature, the native installer verifier, the generated installer, and `latest.json`.
   - The app uses an ad-hoc signature and is **not** notarized. The installer verifies the release archive with the embedded public key before extraction, so users do not need Apple Developer credentials.
   - `release-smoke` (macos) — installs from the release's one-line installer with a restricted PATH, verifies the bundled runtime and LaunchAgent, starts the app, checks the startup API, then reruns the installer as an update/recovery test.
4. A follow-up job merges `main` back into `develop`.

No manual tag / `gh release create`, no tap push, and no separate desktop release — one merge ships the engine and the app together. End users install with the release URL:

```bash
curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh
```

There is deliberately **no** independent app version: `desktop/package.json` and `desktop/src-tauri/tauri.conf.json` are bumped by `--apply` alongside the Python version, so the engine and app always report the same `X.Y.Z`. Never ship one without the other — the desktop build and embedded runtime are assembled by the same workflow, and `service.rs` resolves the engine from the app bundle.

**Merging the release PR:** the auto-mode classifier blocks `gh pr merge` on the agent-authored release PR unless the user explicitly authorized merging (e.g. "merge #NNN" / "finish then release"). Attempt once; on denial, ask the user to click merge or reply with explicit authorization.

## Known traps

- **Dirty tree → silent downgrade.** `--apply` silently becomes plan-only (exit 0!) if the tree is dirty — even one untracked file. If version files stay unbumped after a "successful" run, re-run with `--allow-dirty`. Always verify `__version__` and the `release: prepare` commit afterward.
- **Double-bump on failed check.** The tool bumps files *before* running checks. If a check fails, `git checkout -- CHANGELOG.md ciao/__init__.py pyproject.toml web/package.json web/package-lock.json` before re-running, or it double-bumps. Two things a failed run leaves behind that are easy to miss: a **second `## vX.Y.Z` section** stacked on the changelog entry that was already there (`grep -c '^## vX\.Y\.Z' CHANGELOG.md` must be 1), and a **`pyproject.toml` auto-dependency bump with no matching `uv.lock`** — the lock is synced later in the run, so an early failure leaves the two disagreeing and every `uv --frozen` step, including `build-bundled-runtime.sh`, then fails. Revert *before* switching branches: `git checkout <branch>` carries uncommitted changes with you onto the branch you were trying to keep clean.
- **`PYTHONPATH` / stray egg-info.** Never export `PYTHONPATH=.` before running the release/smoke tools — a leftover `ciao.egg-info/` or `ciaobot.egg-info/` at repo root leaks into the "isolated" smoke venv and the top-level wheel gets skipped, failing the probe with `ModuleNotFoundError: No module named 'ciao'` (tell: a bogus pip conflict naming an ancient pre-rename version). Use `env -u PYTHONPATH …`; `rm -rf ciao.egg-info` (gitignored, regenerates) if you see it.
- **A stale `build/` resurrects deleted files into the runtime.** Packaging tools do not always prune old build trees, so a removed module or old frontend asset can be copied into a later artifact. Clean generated build directories before release builds and inspect the staged app/runtime contents.
- **Post-merge watch timing.** Don't grab the latest `publish` run right after merging — it only spawns after `Release on main` finishes creating the tag, so you'd watch the *previous* release's run. Wait for a `Release on main` run on the merge commit, then take the `publish` run newer than it.
- **Never pipe `gh run watch --exit-status` into `tail`/`head`.** The pipeline's exit code is the *last* command's, so `tail` returns 0 and swallows the failure signal `--exit-status` exists to provide. During v0.6.4 that turned a failed `publish` run into a reported-green one. Run it unpiped, or re-check with `gh run view <id> --json conclusion` afterwards.
- **`pgrep` proves the process exists, not that the app runs.** The release smoke test must check the startup API and bundled runtime, not just process presence. When it fails, inspect the app's tray log, LaunchAgent state, and workspace runtime logs before theorising.
- **The installer must fail closed.** The native verifier checks both the verifier binary hash and the signed app archive before extraction. Never turn a verification error into a warning or add an unsigned fallback.
- **Ad-hoc signatures reset TCC grants on every update.** An ad-hoc bundle has no Team ID, so macOS pins Microphone / Full Disk Access / Accessibility records to its cdhash, which changes every build. Users re-granting permissions after an update is expected behaviour on the current signing setup, not a regression — only a Developer ID would fix it (`tauri.conf.json` `signingIdentity`, currently `"-"`).
- **The app owns engine cold start — do not "fix" `release-smoke` by loading launchd.** `desktop/src-tauri/src/lib.rs` starts the engine two ways, both keyed on whether the LaunchAgent plist exists: `spawn_bootstrap` (`ciao run`) when it does not, `start_engine_if_needed` (`desktop-service start`) when it does. Both paths log failures to `.runtime/desktop-tray.log`; `desktop-service start` itself is known-good (it starts the engine cleanly when invoked directly). Adding `--load-launchd` to the workflow would hide a genuine cold-start defect if one ever does appear.
- **Release propagation lag.** Verify that the GitHub release contains the installer, verifier, app archive/signature, and `latest.json` before diagnosing an installer failure.
- **Put Node 22 *and* cargo on PATH in the shell that runs the release tool.** `_run_checks` shells out to `npm run test` twice: in `web/`, where `scripts/check-node.mjs` hard-fails below `^20.19 || ^22.13 || >=24`, and in `desktop/`, where the script is `cargo test`. Each failure kills the run *after* it has already bumped the version files and regenerated the changelog, and each looks unrelated to its real cause (`ReleaseError: command failed (1): npm run test` for the Node floor; `(127)` with `sh: cargo: command not found` for the missing toolchain). Both fixes are per-shell and are not inherited from another terminal, so set them in the same command:

  ```bash
  . "$NVM_DIR/nvm.sh" && nvm use 22
  export PATH="/opt/homebrew/opt/rustup/bin:$PATH"   # brew rustup shims; NOT ~/.cargo/bin
  env -u PYTHONPATH .venv/bin/python -m ciao.release "$(pwd)" …
  ```

  Verify with `node --version && cargo --version` before starting, and clean up per the double-bump trap below after any failed attempt.
- **`prepare-release`'s checks are weaker than CI's — run `mypy ciao` yourself.** `_run_checks` runs pytest, the web suite and the desktop suite, but not the type check, while `ci.yml`'s `test` job does. So a type error passes every local gate and first appears as a red release PR. v0.8.0 hit exactly this: two `no-any-return` errors in `ciao/transcripts.py` from a commit pushed straight to `develop` (no PR, so CI had never seen it) surfaced only after the release branch was cut and pushed. Run `.venv/bin/mypy ciao` as part of the pre-cut checklist, and treat any commit that reached `develop` without a PR as unreviewed by CI.
- **vitest flake.** vitest can flake with fork-worker timeouts right after the pytest run; re-running `npm run test` cleanly passes.
- **Frontend/runtime build mismatch.** Rebuild the PWA after the final source change, then assemble the embedded runtime and app from that same checkout. Keep the release smoke test as the final gate.
- **Confirm which branch you are on before committing.** A long release session can drift: work intended for `develop` lands on `release/vX.Y.Z`, or vice versa. During v0.6.0 the checkout moved to `develop` mid-flight and four commits landed there instead of the release branch, which then needed `git merge develop` to pull them into the release. That is the right shape anyway (features on `develop`, release branch cut from it), so the fix is: check `git branch --show-current`, land features on `develop`, and merge `develop` into the release branch for anything that arrives after the cut. Renaming a local branch does **not** move an open PR's head — you need a new PR.
- **Never `git add -A`.** Another session may be editing the same checkout. Stage explicit file lists, and diff-check what you staged. If a conflicted file's mtime is moving, stop and coordinate rather than resolving it underneath someone.
- **An explicit file list is still not enough — `git add <file>` stages the *whole* file.** In a shared checkout that sweeps in whoever else's half-finished work is sitting in it, and the result is worse than muddied authorship: during v0.6.0 it produced a commit that failed its own tests, because the backend half of someone's feature went in while the test update for it was still uncommitted. CI went red on a commit whose message had nothing to do with the failure, and `git bisect` no longer works across it. Before committing, `git diff --cached` and check every hunk is yours. When a file genuinely holds both, isolate your hunks: copy the file aside, `git checkout HEAD -- <file>`, re-apply only your change, stage, then restore the copy unstaged.
- **Absolute repo_root.** Pass an absolute path — shell cwd persistence between tool calls is unreliable.

## After it ships

- Confirm the `vX.Y.Z` tag + GitHub release exist and artifacts are attached.
- Confirm the release assets and `latest.json` are present, then run the one-line installer smoke test. The app updater should restart the bundled engine without a separate package-manager action.
