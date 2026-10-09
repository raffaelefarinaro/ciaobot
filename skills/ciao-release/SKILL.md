---
name: ciao-release
description: How to cut a Ciaobot release — the patch/minor/major convention, verifying develop, the prepare-release command, reviewing the release PR, installing and walking the candidate in the browser, what merging into main triggers, and the known traps. Trigger on "release", "cut a release", "publish", "bump the version", "ship a new version", "prepare-release", or any question about how Ciaobot versioning and publishing work.
---

# Ciaobot Release

> Contributor/project skill — lives in the repo's workspace `skills/` folder, **not** `ciao/stock/skills/`. It is for people working *on* Ciaobot and is deliberately not packaged or shipped to end-user installs. `ciao sync-skills` mirrors it into the runtime-discovered `.claude/skills/` catalog. Don't move it into `ciao/stock/`.

Authoritative procedure for cutting a Ciaobot release. `develop` is the source
line; `main` is publish-only — **merging a release PR into `main` is the
trigger** for everything downstream (tag → GitHub release → engine assets). You
never build artifacts or tag by hand.

**This skill does not fix anything.** It verifies, cuts, reviews, installs, and
ships. A defect found along the way becomes a GitHub issue or a follow-up task
on `develop` — it is not fixed inside the release session. Fixing happens
separately, on `develop`, before the next cut. This is deliberate: a release
that also patches what it is releasing has no fixed point, and every fix
introduced mid-release is unreviewed code riding into a tag.

Canonical companions: `docs/DEVELOPMENT.md` (§ "Branching and releases") and
`ciao/release.py`. When this skill and the code disagree, the code wins — say
so and update this skill.

Sibling skills this one calls into, rather than restating:
**`/ciao-dev-install`** to build and run the candidate on this machine, and
**`/ciao-upkeep`** for the standing checks that keep dependencies, the stock
catalog and `docs/UPKEEP.md` current — run those on their own cadence, not
here. Read each at the point it is needed.

Everything runs in **this checkout, in one session**. There is no worktree, no
external tree, and no separate reviewer process. If you find yourself wanting a
worktree, you are about to make a change, and changes do not belong here.

## Versioning convention (SemVer-by-impact)

Pick the bump from user-facing impact, not diff size:

- **patch** (`--bump patch`, default) — bug fixes, internal refactors, doc/test/CI changes, dependency bumps with no behavior change. Nothing new the user can do.
- **minor** (`--bump minor`) — any new user-facing capability or notable behavior change (e.g. a new provider/backend, a new page, a removed setting). Backward-compatible.
- **major** (`--bump major`) — breaking changes to what users or their data depend on: vault layout/format, workspace layout, the CLI surface/flags, the PWA API contract, or config that requires manual migration.

When unsure between patch and minor, ask: "could a user notice something new or
different?" If yes → minor.

**Check the bump against the actual diff, not the request.** A user asking for
"a patch release" is naming the ritual, not auditing the scope — and scope grows
after the branch is cut. Read the changelog you just generated: if it has an
`### Added` section with real features, it is a minor. Say so and let the user
decide. Renumbering costs a rewrite of every version-bearing file, the changelog
heading, the branch name and the PR — cheap, but cheaper still before the PR is
open.

A release that *removes* a user-facing thing is not automatically a major. The
distinction is whether a user has to do anything: a removed setting with an
automatic migration, or a bundled skill going away, is a minor. A layout or
format a user depends on, or config they must migrate by hand, is a major.

## Step 1 — Verify `develop`

The release ships exactly what is on `develop`. Establish that first, because
every later step assumes it.

```bash
rtk proxy git switch develop
rtk proxy git fetch origin
rtk proxy git rev-list --count origin/develop..HEAD   # must be 0
rtk proxy git status --short                          # must be empty
```

A non-zero count means `develop` is ahead of the remote: push it, or stop. A
dirty tree stops the cut (`--apply` refuses — see traps).

**Decide scope with the user.** Run `gh pr list --state open` and
`gh issue list --state open` on `raffaelefarinaro/ciaobot`, show the result, and
ask which open PRs should land in this release. Merge the chosen ones into
`develop` first, then re-run the count above. Issues are context, not work:
report them, do not fix them here.

## Step 2 — Run the gates

`_run_checks` in `ciao/release.py` runs these for you during `--apply`, and
`_run_checks` failing is a stop, not something to work around:

```
mypy ciao · pytest tests/ · cd web && npm run test · cd web && npm run build
ciao package-smoke --skip-frontend
```

There is no Rust, no `cargo`, no app bundle: the release *is* the engine
(`#655`, `#656`).

Before that, run the two checks the release tool does not do for you:

**The doc gates, explicitly.** They run inside `pytest tests/` too, but running
them here means a doc failure surfaces *before* `--apply` has bumped and
committed anything:

```bash
env -u PYTHONPATH .venv/bin/python -m pytest \
  tests/test_architecture_doc.py tests/test_env_vars_documented.py tests/test_pwa_api_docs.py
```

**Every merge in the range.** `/code-review` reads the *diff*, and a merge that
dropped a feature leaves no suspicious diff — the code is simply absent, exactly
as if it had never been written. Nothing in the normal gate looks for that:

```bash
./scripts/check-merges.sh <last-tag>..develop
```

This replays the automatic 3-way merge of each merge commit's parents and
reports every delta — each one is a hand edit made during conflict resolution,
which is the only place this failure can occur. v0.12.0 shipped one: merge
`ed04d1e9` resolved by taking the pre-feature side, reverting the entire
frontend half of a feature while the backend half stayed live, and deleting the
test that covered it — so the suite had nothing left to fail on. The tells in a
delta: **a deleted test file**, a **stray `.tmp`/`.orig`/`.rej`**, any file where
one side's changes vanished wholesale. A delta is not automatically a bug — real
conflicts must be resolved by hand — but no test will do it for you. Report
what you find; do not resolve it here.

## Step 3 — Review the release surface

Read the range as a reviewer, not the author:

```bash
rtk proxy git log --oneline <last-tag>..develop
rtk proxy git diff <last-tag>..develop
```

Then run the review **for its report only**:

- `/code-review high <last-tag>..develop` — no `--fix`. If the diff touches
  auth, secrets or external input, run `security-review` as well.
- **Read the `stats` block.** Findings are capped (~10) and the rest are
  dropped; a clean-looking report on a large diff may just be a full one. A
  release wants `high` or above — `/code-review` reuses the last level you typed
  when it cannot parse one, and says so only in its own summary line. Read that
  line too.
- **Verify each finding against the code before you report it.** Findings can be
  wrong, and shipping a "fix" for one is a regression. v0.15.0 was told
  `pwa_host`'s default was moving `127.0.0.1` → `0.0.0.0`; `from_env`'s fallback
  was *already* `0.0.0.0`, so every real install already bound all interfaces.

**Write down what you found, and file it.** A review that produces no durable
record is a review that gets re-litigated next release. Before cutting, put the
outcome somewhere permanent — the release PR body is the natural place, or
`gh issue create` per finding:

```bash
gh issue create --repo raffaelefarinaro/ciaobot \
  --title "[Bug] <one line>" --label bug --body "<file:line, the problem, the repro>"
```

Anything that must block the merge is the user's call, not yours. Say so
plainly and stop.

## Step 4 — Cut it

Plan-only first. It writes nothing, so it is safe against a dirty tree:

```bash
env -u PYTHONPATH .venv/bin/python -m ciao.release "$(pwd)" --bump <patch|minor|major>
```

Read the plan. It is the last chance to catch a wrong bump.

**Write the release notes.** The plan's changelog is the raw commit list; users
read the notes. Run **`/ciao-release-notes`** for the range
`<last-tag>..origin/develop`, write the body to
`$TMPDIR/release-notes-vX.Y.Z.md`, and show it to the operator before cutting.
The notes become the `CHANGELOG.md` section and the GitHub release body. Then:

```bash
env -u PYTHONPATH .venv/bin/python -m ciao.release "$(pwd)" \
  --bump <patch|minor|major> --notes-file "$TMPDIR/release-notes-vX.Y.Z.md" --apply \
  --commit --push --create-pr --ready > /tmp/release.log 2>&1
echo "EXIT=$?"
grep -nE "ReleaseError|command failed|/pull/" /tmp/release.log
```

Node 22 must be on PATH **in that same shell** (`_run_checks` shells out to npm;
see traps). Defaults: `--source develop` (cuts `release/vX.Y.Z` from
`origin/develop`), `--base main`.

What `--apply` does, in order: bumps `pyproject.toml`, `ciao/__init__.py`,
`web/package.json`, `web/package-lock.json` and the service-worker cache names
in both `web/public/sw.js` and `ciao/web/static/sw.js`; writes the notes into
`CHANGELOG.md` (or, without `--notes-file`, the commit list);
auto-bumps `auto` dependencies; regenerates the packaged `gws-*` skills if the
installed `gws` CLI differs from the pin; runs the full check suite; commits
`release: prepare vX.Y.Z`; pushes; opens the PR into `main`.

## Step 5 — Install the candidate and walk it

Nothing above this line ever *starts* the thing you are about to ship. `pytest`
and the `npm` suites prove the parts compile and their tests pass; none of them
boots an engine against a real workspace with real chats, schedules, MCP servers
and credentials in it.

**`/ciao-dev-install`** builds the PWA, installs the current checkout over the
live install preserving the workspace and password, restarts the service and
watches the logs. Read that skill and follow it; do not improvise. Do it **after
the final source change** so what you smoke-test is what gets cut.

Then **drive the running app like a user**, with `browser-use`, following the
task files in `skills/ciao-release/tasks/`:

```
skills/ciao-release/tasks/01-boot-and-home.md        first paint, workspace, what-needs-you
skills/ciao-release/tasks/02-chat-and-turns.md       start a chat, real turns, streaming, stop
skills/ciao-release/tasks/03-archive-and-memory.md   archive, reopen, the archived footer
skills/ciao-release/tasks/04-projects-and-schedule.md  a plain app project, an isolated vault note completion, a schedule row
skills/ciao-release/tasks/05-settings-and-assets.md  every Settings route loads, the scoped asset lists
```

The operator types the dashboard password once, into the visible browser, at the
start of `tasks/01`. After that the session is unlocked and the rest run
unattended. **Never read `PWA_AUTH_TOKEN` out of the workspace `.env` and type
it yourself** — it puts the password in the transcript and in this task's
context for no benefit.

### Driving the app, and the walkthrough report

The task files describe **what to do and what to look at**, not tool calls. Drive
the app with the `browser-use` skill: reach the app in a tab, find controls by
their accessibility role and visible name before falling back to coordinates, and
wait for an observable state rather than a fixed delay.

**Use the operator's own signed-in browser profile for the app, not a throwaway
one.** If the app must open in a specific Chrome profile, start Chrome with that
profile (`open -na "Google Chrome" --args --profile-directory="<dir>" <url>`;
`~/Library/Application Support/Google/Chrome/Local State` lists the directories),
then attach with `browser-use` and pick the tab by URL. `browser-use` cannot open
a tab in a chosen profile itself.

**Capture a screenshot at every checkpoint** a task names (and whenever something
looks off), into a per-run folder such as `$TMPDIR/ciao-release-walk/<version>/`,
named `NN-task-step.png`. Open each one and look at it. A passing assertion with a
collapsed layout, clipped text, an overlapping popover, a stray toast or an empty
state is still a finding.

**Finish with a report the operator reviews**, built from those screenshots: one
section per task, each with its checkpoints as embedded images, a one-line
verdict (pass / finding / blocked), and the findings spelled out beside the image
they came from. Build it as a self-contained HTML page with the `html-artifact`
skill so it renders in the pinned panel, and keep the PNGs next to it. The report
carries no secrets, no workspace data beyond what a screenshot shows of the
operator's own app, and no names of people or profiles.

These tasks are the undeterministic half of the check. The 8 Playwright specs in
`web/e2e/specs/` pin down routing, sockets and named regressions; the model
tasks catch what a spec cannot express — a layout that collapsed at this width,
a toast that fired for a quiet action, a tab that hangs, a flow that reads as
broken. Run both. Report what the tasks found; do not fix it here.

`AGENTS.md` requires visual browser inspection before *pushing*. A release is
the one moment where all of it is true at once, which is why these live here.

### Windows smoke (preview, #696 C10/C11)

The macOS walk above does not touch the Windows engine, and there is no Windows job in `release-smoke` yet, so a release candidate gets this by hand while Windows is a preview. Use the clean-install Windows 11 VM snapshot (or a fresh local Windows account) and run it over SSH or in the console. It needs the release candidate's files, so run it after `publish` has attached them, or build them locally the way the CI end-to-end step does and use `-ReleaseDir <folder> -Version <x.y.z>`:

1. Restore the clean snapshot (or create a new account that has never had Ciaobot).
2. Install: `irm https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.ps1 | iex` (or the `-ReleaseDir`/`-Version` form for a candidate that is not published). Expect a sign-in URL and no error.
3. In a **new** terminal: `ciao --version` prints the candidate version, and `ciao service status` reports the engine as running.
4. Open the sign-in URL, sign in, and run one real chat turn with each installed provider.
5. Settings -> Restart: the engine comes back (this is the exit-code-75 path under `ciao supervise`).
6. Sign out and back in (or reboot and sign in): the engine starts by itself.
7. Uninstall: run the installer with `-Uninstall`. Expect `Ciaobot is uninstalled.`, `schtasks /Query /TN \Ciaobot\Engine` failing, the PATH entry gone, and the workspace folder still present.
8. Record the VM snapshot, the commit, the date and pass/fail per step in the #696 parity matrix. Anything broken becomes an issue; this skill does not fix it.

Engine update and rollback are not part of this smoke until #857 lands; add them then.

**Then ask the user to test.** Say plainly what is installed, what version, and
that the engine is live on their real workspace. They are the only reviewer
whose verdict covers "is this good", as opposed to "does this work".

## Step 6 — Merge, and watch it land

Once the release PR is open, give its diff one more read before merging — the
`release: prepare` commit adds version/CHANGELOG/dependency changes that were
not in the Step 3 review. Prefer `/code-review --comment` on the open PR so
findings land as inline comments.

1. CI (`test`) must be green (`mergeStateStatus` CLEAN).
2. Merge into `main`. This runs `.github/workflows/release-on-main.yml`, which
   creates the `vX.Y.Z` tag and GitHub release using `RELEASE_PAT` (a plain
   `GITHUB_TOKEN` release would **not** fire `release: published`).
3. That fires `publish.yml`:
   - `build-engine` (macos) — builds the PWA and the engine wheel, verifies the
     wheel in a clean environment, signs the engine manifest with the release
     minisign key, and attaches the wheel, the manifest and its signature, plus
     the engine installer as both `install.sh` and `install-engine.sh` (the same
     bytes under both names, #651) and the Windows installer `install.ps1`.
     Since #653 there is no app archive, no updater feed, no native verifier and
     no bundled runtime.
   - `release-smoke` (macos) — installs the engine from the release's one-line
     installer with a restricted PATH, verifies the `ciao` entry point, the
     install receipt and the LaunchAgent, checks the startup API, then reruns
     the installer as an update/recovery test.
4. A follow-up job merges `main` back into `develop`.

No manual tag, no `gh release create`, no tap push, no separate desktop release —
one merge ships the engine.

End users install it with the release URL, which does not change between
releases — the same one-liner now installs the engine:

```bash
curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh
```

`tests/test_ci_workflow.py::test_first_party_install_command_stays_install_sh`
pins that exact command into this skill, `README.md`, `site/index.html`,
`site/guide.html` and `docs/DEVELOPMENT.md`, and fails if any of them starts
pointing a first-time user at `install-engine.sh` instead. The build attaches
the installer under both names because the public one-liner kept `install.sh`
while the hand-over path fetched `install-engine.sh` (#651) — but `install.sh` is
the one a user runs.

**Merging the release PR** is the one irreversible act in this skill. The
auto-mode classifier blocks `gh pr merge` on the agent-authored release PR
unless the user explicitly authorized merging. Attempt once; on denial, ask the
user to click merge or reply with explicit authorization. Do not work around it.

**Watch timing:** do not grab the latest `publish` run right after merging — it
only spawns after `Release on main` finishes creating the tag, so you would
watch the *previous* release's run. Wait for a `Release on main` run on the
merge commit, then take the `publish` run newer than it.

After it ships, confirm the tag, the GitHub release, the wheel, the signed
manifest, both installer names and `install.ps1`. `pgrep` proves a process
exists, not that the engine runs — the smoke test checks the startup API and the
installed `ciao`.

## Environment prerequisites

The release tool runs `pytest`, `npm run test`/`npm run build` and the package
smoke test **with the interpreter that launched it**. System Python (3.9) can't
even import `release.py`.

- Repo `.venv` (Python 3.12+, `ciaobot` editable-installed), or
  `python3.13 -m venv .venv-rel && .venv-rel/bin/pip install -e ".[test]"`.
- `cd web && npm ci` at least once so `vitest` exists.
- Node on PATH per `.nvmrc`, **in the same shell** that runs the tool.
- `gh` authenticated (for `--create-pr`).
- `browser-use` on PATH for Step 5, attached to Chrome with remote debugging
  on (`browser-use --doctor`).
- A clean tree on `develop`.

## Known traps

Release-cutting traps. Bugs in the code are not here — file them.

- **Dirty tree → hard failure.** `--apply` refuses on a dirty tree, even one
  untracked file (`ReleaseError: working tree is dirty`, `_ensure_clean`). The
  check sits *after* the `if not args.apply: return 0` early exit, so a
  **plan-only pass is safe against a dirty tree** — useful for previewing while
  someone else is mid-edit. `--allow-dirty` bakes the tree into the release, and
  `npm run build` globs the filesystem rather than git, so a concurrent
  session's half-finished work ends up inside the shipped wheel. Don't.
- **`--source` prefers `origin/<branch>` — deliberately.** `_resolve_source_ref`
  verifies `origin/<source>` first so a local branch lagging origin cannot cut a
  stale release. The inverse is the trap: when local `develop` is **ahead** of
  `origin/develop`, `--source develop` cuts from the remote and silently drops
  every unpushed commit. The changelog still looks plausible, because it is
  generated from whatever did get cut. Step 1's `rev-list --count` is the check.
- **Double-bump on a failed run.** The tool bumps files *before* running checks.
  If a check fails, revert before re-running:
  `git checkout -- CHANGELOG.md ciao/__init__.py pyproject.toml web/package.json web/package-lock.json uv.lock`.
  Two things a failed run leaves behind: a **second `## vX.Y.Z` section**
  (`grep -c '^## vX\.Y\.Z' CHANGELOG.md` must be 1), and a `pyproject.toml` bump
  with no matching `uv.lock` — every `uv --frozen` step fails on that pair,
  including the engine wheel build in `publish.yml`. Revert *before* switching
  branches; `git checkout` carries uncommitted changes with you.
- **A failed run leaves the release branch behind, and the retry cannot recreate
  it.** `_checkout_release_branch` uses `git switch -c`, which refuses when the
  branch exists, so the second attempt dies with a git error that says nothing
  about the real problem. Revert the bumps first, then:
  ```bash
  rtk proxy git log --oneline develop..release/vX.Y.Z    # must be empty
  rtk proxy git ls-remote --heads origin release/vX.Y.Z  # must be empty
  rtk proxy git switch develop && rtk proxy git branch -D release/vX.Y.Z
  ```
- **`PYTHONPATH` / stray egg-info.** Never export `PYTHONPATH=.` before running
  the release or smoke tools — a leftover `ciao.egg-info/` leaks into the
  "isolated" smoke venv and the top-level wheel gets skipped, failing the probe
  with `ModuleNotFoundError: No module named 'ciao'`. Use `env -u PYTHONPATH …`.
- **Never pipe a release-critical command into `tail`/`head` — including the
  release tool itself.** The pipeline's exit code is the *last* command's, so
  `tail` returns 0 and swallows the failure. v0.6.4 reported a failed `publish`
  as green this way. Redirect to a file and grep it (Step 4 shows the shape).
  For workflow runs, `gh run watch --exit-status` unpiped, or re-check with
  `gh run view <id> --json conclusion`.
- **Put Node 22 on PATH in the shell that runs the tool.** `_run_checks` shells
  out to `npm run test` in `web/`, where `scripts/check-node.mjs` hard-fails
  below `^22.22.2 || ^24.15.0 || >=26`. The failure lands *after* the version files
  are bumped and looks unrelated (`ReleaseError: command failed (1): npm run
  test`). The fix is per-shell and not inherited from another terminal:
  `. "$NVM_DIR/nvm.sh" && nvm use 22` in the same command.
- **Rebuild the PWA after the last source change.** `ciao/web/static/` ships as
  package data inside the wheel, and both generated parts — `assets/` and
  `index.html` — are **untracked** (`.gitignore`); packaging globs the
  filesystem, not git. `_check_built_pwa` fails the release if the shell is
  missing or names a bundle that is not on disk, which is what a stale build
  looks like after a branch switch.
- **A stale `build/` resurrects deleted files into the runtime.** Packaging tools
  do not always prune old build trees. Clean generated build directories before
  release builds and inspect the staged runtime contents.
- **The memory-backup service can dirty the tree under you.** Step 5 boots the
  engine against this checkout, and since the automatic backup service (#689)
  that engine commits the durable scope and can push a `backup <ts>Z` commit to
  the branch's upstream. Re-check `git status --short` after the walkthroughs and
  before `--apply`; if a stray commit landed, do not ship it — reset the branch
  to the reviewed sha.
- **`mypy ciao` cannot see a deleted module.** With `ignore_missing_imports`, a
  leftover `from ciao.providers.<deleted> import …` stays green under both mypy
  *and* pytest. After a range that removes a `ciao/` module:
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
- **Treat any commit that reached `develop` without a PR as unreviewed.** That is
  how v0.8.0's two `no-any-return` errors in `ciao/transcripts.py` first
  surfaced — as a red release PR after the branch was cut and pushed.
- **vitest flake.** vitest can flake with fork-worker timeouts right after the
  pytest run; a clean re-run passes. That is not a licence to re-run twice before
  reporting a failure.
- **Verify release-critical git state with `rtk proxy git …`, not bare `git`.**
  The RTK hook rewrites `git` and filters its output, not always faithfully.
  During v0.12.0 `git status --short` reported a **clean tree while seven files
  were modified**, and `git ls-files` omitted a file `git ls-tree HEAD` showed
  as committed. A tell that you are reading filtered output: the same fact
  answered differently by two git commands.
- **Never `git add -A`, and an explicit file list is still not enough.**
  `git add <file>` stages the *whole* file, so in a shared checkout it sweeps in
  whoever else's half-finished work. Stage, then `git diff --cached` and check
  every hunk is yours. Isolate mixed files by hand: copy aside, `git checkout
  HEAD -- <file>`, re-apply only your change, stage, restore the copy unstaged.
- **Confirm which branch you are on before committing.** A long release session
  drifts. Check `git branch --show-current`. Renaming a local branch does **not**
  move an open PR's head — that needs a new PR.
- **The installer must fail closed.** The engine manifest signature and the
  wheel's digest are both verified before extraction. Never turn a verification
  error into a warning or add an unsigned fallback.
- **Engine cold start is the installer's job** — do not "fix" `release-smoke` by
  adding `--load-launchd` to the workflow. That would hide a genuine cold-start
  defect.
- **Release propagation lag.** Verify the GitHub release contains the wheel, its
  signed manifest, both installer names and `install.ps1` before diagnosing an
  installer failure.
- **Absolute repo_root.** Pass an absolute path — shell cwd persistence between
  tool calls is unreliable.
