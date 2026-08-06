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
  --bump <patch|minor|major> --apply --commit --push --create-pr --ready
```

The `scripts/prepare-release` wrapper is equivalent (`CIAO_PYTHON=.venv/bin/python scripts/prepare-release --bump … --apply --create-pr --ready`) but does **not** unset `PYTHONPATH` — see the trap below. Use `--version X.Y.Z` for an explicit version. Defaults: `--source develop` (cuts `release/vX.Y.Z` from `origin/develop`), `--base main`.

What `--apply` does, in order: bumps `pyproject.toml`, `ciao/__init__.py`, `web/package.json`, `web/package-lock.json`, the five desktop version files (`desktop/package.json`, `desktop/package-lock.json`, `desktop/src-tauri/Cargo.toml`, `desktop/src-tauri/Cargo.lock`, `desktop/src-tauri/tauri.conf.json`), and the service-worker cache names in **both** `web/public/sw.js` and `ciao/web/static/sw.js`; refreshes `CHANGELOG.md`; auto-bumps `auto` dependencies; regenerates the packaged `gws-*` skills if the installed `gws` CLI differs from the pin; runs the full check suite; commits `release: prepare vX.Y.Z`; pushes the branch; opens the PR.

## Rebuild the PWA last

`ciao/web/static/` is **tracked** (the packaged wheel serves from there), so the built `index.html` in the release commit must reference the asset hashes of the code you are actually shipping. One wrinkle: `ciao/web/static/assets/` itself is *gitignored* (`.gitignore:22`) — only `index.html` and the non-hashed assets are tracked, and the hashed bundles are regenerated by the publish workflow's own build. So a stale committed `index.html` is a local-run hazard rather than a shipped one, but commit the rebuild anyway to keep the tree honest. Run `cd web && npm run build` *after* the final source change and commit the result. If you rebuild early and then fix something, the packaged frontend is a mixed state — it will boot, so nothing fails loudly.

Corollary: if a concurrent session is editing the tree, its unfinished work gets baked into your build output. Check `git status` before building.

## Merging is the trigger

1. CI (`test`) on the PR must be green (`mergeStateStatus` CLEAN) before merging.
2. Merge the PR into `main`. This runs `.github/workflows/release-on-main.yml`, which creates the `vX.Y.Z` tag + GitHub release using `RELEASE_PAT` (a plain `GITHUB_TOKEN` release would **not** fire `release: published`).
3. That fires `publish.yml`, which ships **both the engine and the desktop app from the same tag**:
   - `build-and-publish` (ubuntu) — PWA + wheel + sdist, publishes to PyPI (trusted publishing, env `pypi`), attaches artifacts to the release.
   - `build-desktop` (macos) — `npm run tauri build -- --target universal-apple-darwin`, then attaches `Ciaobot_<version>_universal.dmg`, `…app.tar.gz`, `…app.tar.gz.sig`, and a generated `latest.json` (the updater manifest the app polls). Needs the `TAURI_SIGNING_PRIVATE_KEY` / `…_PASSWORD` secrets; the app is ad-hoc signed and **not** notarized.
   - `update-homebrew-tap` — bumps **both** `Formula/ciaobot.rb` (wheel sha256) and `Casks/ciaobot-desktop.rb` (DMG sha256) in `raffaelefarinaro/homebrew-ciaobot`.
   - `release-smoke` (macos) — installs the released formula *and* cask from the tap, verifies the updater metadata, and checks cask uninstall. It lives in its own `release-smoke.yml` (called by `publish.yml`, and `workflow_dispatch`-able with a `version` input) so a fix to it can be tested against an already-published version instead of costing a version bump. On failure a `Diagnostics` step dumps processes, listening sockets, launchd state, an explicit `desktop-service start`, and the workspace's `.runtime` logs — read that before theorising.
4. A follow-up job merges `main` back into `develop`.

No manual tag / `gh release create` / tap push, and no separate desktop release — one merge ships the engine and the app together. `pip install ciaobot`, `brew install raffaelefarinaro/ciaobot/ciaobot`, and `brew install --cask raffaelefarinaro/ciaobot/ciaobot-desktop` all ship every release.

There is deliberately **no** independent app version: `desktop/package.json` and `desktop/src-tauri/tauri.conf.json` are bumped by `--apply` alongside the Python version, so the engine and app always report the same `X.Y.Z`. Never ship one without the other — a desktop build expecting a newer engine surfaces as an opaque `Invalid desktop-service response`, because `service.rs` resolves the CLI from hardcoded Homebrew paths before `PATH`.

**Merging the release PR:** the auto-mode classifier blocks `gh pr merge` on the agent-authored release PR unless the user explicitly authorized merging (e.g. "merge #NNN" / "finish then release"). Attempt once; on denial, ask the user to click merge or reply with explicit authorization.

## Known traps

- **Dirty tree → silent downgrade.** `--apply` silently becomes plan-only (exit 0!) if the tree is dirty — even one untracked file. If version files stay unbumped after a "successful" run, re-run with `--allow-dirty`. Always verify `__version__` and the `release: prepare` commit afterward.
- **Double-bump on failed check.** The tool bumps files *before* running checks. If a check fails, `git checkout -- CHANGELOG.md ciao/__init__.py pyproject.toml web/package.json web/package-lock.json` before re-running, or it double-bumps.
- **`PYTHONPATH` / stray egg-info.** Never export `PYTHONPATH=.` before running the release/smoke tools — a leftover `ciao.egg-info/` or `ciaobot.egg-info/` at repo root leaks into the "isolated" smoke venv and the top-level wheel gets skipped, failing the probe with `ModuleNotFoundError: No module named 'ciao'` (tell: a bogus pip conflict naming an ancient pre-rename version). Use `env -u PYTHONPATH …`; `rm -rf ciao.egg-info` (gitignored, regenerates) if you see it.
- **Never `pip install -U ciao`** — that's an unrelated PyPI package. The distribution is `ciaobot`; self-update uses GitHub release wheels.
- **A stale `build/` resurrects deleted files into the wheel.** setuptools does not prune `build/lib/`, so a module you deleted from `ciao/` is still copied into the next wheel from the previous build's tree — the package ships a file that no longer exists in git, and `ciao/web/static/` accumulates every old hashed asset from every prior build (632 files packaged where the source tree had 392). It fails silently: the wheel installs and boots fine. `rm -rf build/` before building whenever the release deletes a module or renames assets, and sanity-check with `unzip -l dist/*.whl | grep -c 'ciao/web/static/'` against `find ciao/web/static -type f | wc -l`.
- **`pip install --ignore-installed` leaves orphans.** Homebrew's install has no `RECORD` file, so pip refuses to uninstall it and `--ignore-installed` is the only way to overwrite in place — but it only *adds and replaces* files. Modules dropped in the new version stay on disk in `site-packages/ciao/`, dead but importable. After overwriting a Homebrew libexec by hand, diff the installed module list against the wheel and delete the leftovers.
- **Post-merge watch timing.** Don't grab the latest `publish` run right after merging — it only spawns after `Release on main` finishes creating the tag, so you'd watch the *previous* release's run. Wait for a `Release on main` run on the merge commit, then take the `publish` run newer than it.
- **Never pipe `gh run watch --exit-status` into `tail`/`head`.** The pipeline's exit code is the *last* command's, so `tail` returns 0 and swallows the failure signal `--exit-status` exists to provide. During v0.6.4 that turned a failed `publish` run into a reported-green one. Run it unpiped, or re-check with `gh run view <id> --json conclusion` afterwards.
- **A red `publish` does not mean nothing shipped.** `update-homebrew-tap` needs `[build-and-publish, build-desktop]` and `release-smoke` needs `update-homebrew-tap`, so if `release-smoke` produced any log output at all, everything upstream of it succeeded and the version *is* on PyPI and in the tap. Read the job list before considering a recovery release: v0.6.3 and v0.6.4 both show `publish` failed while both shipped fine.
- **`pgrep` proves the process exists, not that the app runs.** The v0.6.3/v0.6.4 `release-smoke` failures looked like a broken engine cold start; a stack sample proved the app was pinned in `_dyld_start` with a 96K footprint 35s after launch, having never reached `main()`. Cause: the app is ad-hoc signed and **not notarized**, and `brew install --cask` sets `com.apple.quarantine` (`0181;…;Homebrew\x20Cask;…`), so the first launch waits on a Gatekeeper assessment nothing is there to approve. CI clears it with `xattr -dr com.apple.quarantine` before launching — note `brew install --cask --no-quarantine` is **not** an option any more, Homebrew rejects the flag. When the probe fails, read the stack sample first: `launchctl`/engine theories are worthless if no Rust code ever ran.
- **Notarization is deliberately not done, and the install path routes around it rather than stripping quarantine.** There is no Apple Developer certificate for this project. The documented install is `ciao desktop install` (`ciao/desktop_install.py`), which downloads the release's `.app.tar.gz` with `urllib` — a command-line download never gets a `com.apple.quarantine` flag, so Gatekeeper never assesses and there is no first-launch gesture. Because Apple's notary check is therefore not the guard, that command verifies the release's minisign signature against the same key `tauri.conf.json` gives the updater and **must** keep failing closed; a change that downgrades a verification failure to a warning turns it into a remote code execution path. The cask remains as a fallback and still hits the Gatekeeper block, so its `caveats` still describe the Open Anyway dance. Don't "fix" a cask first-launch report with a quarantine-stripping `postflight` — that silently removes a check for users who chose the cask; point them at `ciao desktop install` instead.
- **Ad-hoc signatures reset TCC grants on every update.** An ad-hoc bundle has no Team ID, so macOS pins Microphone / Full Disk Access / Accessibility records to its cdhash, which changes every build. Users re-granting permissions after an update is expected behaviour on the current signing setup, not a regression — only a Developer ID would fix it (`tauri.conf.json` `signingIdentity`, currently `"-"`).
- **The app owns engine cold start — do not "fix" `release-smoke` by loading launchd.** `desktop/src-tauri/src/lib.rs` starts the engine two ways, both keyed on whether the LaunchAgent plist exists: `spawn_bootstrap` (`ciao run`) when it does not, `start_engine_if_needed` (`desktop-service start`) when it does. Both paths log failures to `.runtime/desktop-tray.log`; `desktop-service start` itself is known-good (it starts the engine cleanly when invoked directly). Adding `--load-launchd` to the workflow would hide a genuine cold-start defect if one ever does appear.
- **Verification lag.** PyPI's `/pypi/<pkg>/json` `info.version` lags a few minutes — verify via the `/simple/ciaobot/` index. `raw.githubusercontent.com` caches the tap formula ~5 min — verify the bump via `gh api repos/raffaelefarinaro/homebrew-ciaobot/contents/Formula/ciaobot.rb`.
- **vitest flake.** vitest can flake with fork-worker timeouts right after the pytest run; re-running `npm run test` cleanly passes.
- **`npm run test` on the wrong Node is a version collision, not broken tests — but the Node 26 half of this is fixed as of v0.7.0.** The release tool's check suite runs `npm run test`. The 13 Node 26 failures this note used to describe lived in `productTour.test.ts`, `gettingStarted.test.ts`, and `stores/productTour.test.ts`, which were **deleted** when the product tour was removed (`420c987`); the suite now passes clean on Node 26 (39 files, 346 tests as of v0.7.2), so `--skip-frontend` is no longer needed there. The underlying collision is still real if such a test returns: Node ships a native `localStorage` global that is disabled without `--localstorage-file` and shadows the one jsdom provides, despite those files declaring `// @vitest-environment jsdom`. On **Node 20** it fails differently — 15 files never load, `ERR_REQUIRE_ESM`. CI pins **Node 22** (`ci.yml:24`) and the suite passes there, so this never appears in CI. (Earlier versions of this note claimed CI does not run `npm test`; it does, at `ci.yml:62-65`.) Do not "fix" these tests as part of a release. Without a Node 22 locally, cut with **`--skip-frontend`** — it skips only the frontend test/build and still runs pytest and the package smoke — then let the release PR's CI be the gate for the *web* half, which it does run on the exact release commit. It does **not** gate the desktop half — see the Rust bullet above, and run `./scripts/check-desktop.sh` by hand. Prefer `--skip-frontend` over `--skip-checks`, which would also drop the wheel probe.
- **Confirm which branch you are on before committing.** A long release session can drift: work intended for `develop` lands on `release/vX.Y.Z`, or vice versa. During v0.6.0 the checkout moved to `develop` mid-flight and four commits landed there instead of the release branch, which then needed `git merge develop` to pull them into the release. That is the right shape anyway (features on `develop`, release branch cut from it), so the fix is: check `git branch --show-current`, land features on `develop`, and merge `develop` into the release branch for anything that arrives after the cut. Renaming a local branch does **not** move an open PR's head — you need a new PR.
- **Never `git add -A`.** Another session may be editing the same checkout. Stage explicit file lists, and diff-check what you staged. If a conflicted file's mtime is moving, stop and coordinate rather than resolving it underneath someone.
- **An explicit file list is still not enough — `git add <file>` stages the *whole* file.** In a shared checkout that sweeps in whoever else's half-finished work is sitting in it, and the result is worse than muddied authorship: during v0.6.0 it produced a commit that failed its own tests, because the backend half of someone's feature went in while the test update for it was still uncommitted. CI went red on a commit whose message had nothing to do with the failure, and `git bisect` no longer works across it. Before committing, `git diff --cached` and check every hunk is yours. When a file genuinely holds both, isolate your hunks: copy the file aside, `git checkout HEAD -- <file>`, re-apply only your change, stage, then restore the copy unstaged.
- **Absolute repo_root.** Pass an absolute path — shell cwd persistence between tool calls is unreliable.

## After it ships

- Confirm the `vX.Y.Z` tag + GitHub release exist and artifacts are attached.
- Confirm the new version on the PyPI `/simple/` index and the tap formula bump (via `gh api`, not raw).
- On ≥0.4.28 the local server self-restarts after a `brew upgrade` (InstallWatcher); no manual `launchctl kickstart` needed.
