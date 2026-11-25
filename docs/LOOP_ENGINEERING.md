# Loop engineering notes
Ciaobot facts the user-level `loop-engineering` skill (`~/.claude/skills`)
reads, via the pointer in `AGENTS.md`; the gates are the ones under
Verification in `AGENTS.md`.
- Finish the authorized graph without routine continuation questions. Keep one
  coordinator and a verified wake source while work is pending; a schedule is an
  observer that wakes the original authorized chat, not an unattended replacement
  executor. Respect real access, permission and model-budget boundaries.
- Workers run focused affected tests and static checks. The primary owns one
  final full local gate after review fixes settle, before pushing. Do not repeat
  a primary-owned pass on unchanged inputs: record the tree, gate commands,
  toolchain versions and dependency-install fingerprint. Changed inputs invalidate
  affected gates; uncertain dependencies require rerunning. Markdown/comment
  corrections still need applicable backend/doc checks, not an automatic repeat
  of unchanged frontend/browser gates. Focused small checks run directly; heavy
  suites/browser runs share the lock. Lock exit 75 is deferred, not red.
- Merge reviewed, locally gated and PR-green changes into `develop` without
  waiting for full-platform CI between them. A known red on a required develop
  check stops the queue; fix it before merging more. The Python 3.14 backend gates and
  the macOS job run on the release PR into `main` and full manual/nightly runs.
  Installer, migration, service,
  provider lifecycle, auth/security and CI/release work still gets the ship
  fan-out and a PR-scoped security review before merge. Preserve disjoint
  clean PR heads rather than syncing and restarting CI merely because
  develop moved.
- PR target is `develop`. `Closes #N` does not auto-close on a develop merge;
  close issues explicitly: "Merged into develop via #<PR> (<sha>). Ships in the
  next release."
- Branch prefix: `raffaelefarinaro/issue-<N>-<slug>`.
- Gates run from a worktree with the main checkout's venv, which is an
  editable install of `~/repos/ciaobot`: always set `PYTHONPATH=$PWD`
  (`PYTHONPATH=$PWD ~/repos/ciaobot/.venv/bin/python -m pytest -n auto tests/`).
- Symlink `web/node_modules` (and `desktop/runtime` when `desktop/` changes)
  from the main checkout; delete both before every push.
- Before planning, read `docs/DEVELOPMENT.md`, plus `web/README.md` and
  `DESIGN.md` for anything under `web/`.
- New issues: `[Bug]` → label `bug`, `[Feature]` → `enhancement` (see the
  support reference in `ciao-capabilities`). `report`-labelled issues are auto-opened and may be
  noise: flag, don't drop. `not_ready` issues are skipped by the backlog loop.
- No CI runs on develop pushes. The full suites run on the release PR into
  `main`, and `/ciao-release` merges
  only when that PR's `linux-server-latest-python`, `test` and `mac-app` checks are green. If a required
  develop check is already red, open a `bug` issue naming the failing tests
  and the merge that broke them, and fix it before merging more.
- Hands-on device testing (a real macOS install) is for diffs that
  touch the installers (`scripts/install*.sh`), the
  service code, engine update/migration, or `desktop/`.
  Other issues rely on CI.
- Security-sensitive diffs (ship fan-out plus a PR-scoped security review):
  auth, secrets, `/api` or `/ws` routes, `docs/REMOTE_BOUNDARY.md`, `desktop/`
  (also run the `desktop/src-tauri` commands in `docs/DEVELOPMENT.md`),
  release plumbing.
- There is no `/code-review` command for reviewer workers; review from
  `git diff origin/develop...HEAD`.
- Other sessions merge into `develop` all day, so use the skill's serial merge
  queue. The `gates.sh` for this repo runs, from the worktree root: `PYTHONPATH=$PWD
  ~/repos/ciaobot/.venv/bin/python -m mypy ciao`, `PYTHONPATH=$PWD
  ~/repos/ciaobot/.venv/bin/python -m pytest -n auto tests/` (re-run only `--lf`
  before calling it red; latency tests flake under load), and, after symlinking
  `web/node_modules`, `npm test` and `npm run build`
  in `web/` (remove the symlink afterwards). When the diff touches `web/`, it
  also runs `npm run test:e2e`; npm's prehook builds the PWA and terminal
  harness before the browser suite. Develop PRs changing `web/` run the
  conditional Linux Chromium gate in parallel with unit/build work before merge.
  The macOS job runs on release PRs into `main`. When the diff touches `mac/`,
  it also runs, from `mac/` and alone (no other suite in parallel),
  `DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer swift test`,
  `swift build`, `scripts/build-app.sh`, `swift test --filter AppAssetsTests`
  and `plutil -lint .build/Ciaobot.app/Contents/Info.plist` (see `mac/AGENTS.md`
  traps). The terminal-renderer specs need
  `npm run build:harness` first.
- `PWA_API.md`, `DESIGN.md` (the Home paragraph), `web/README.md` and
  `docs/ARCHITECTURE.md` are appended to by most PRs and conflict when two land
  together. Resolve by keeping both sides.
- An engine booted inside a worktree can push memory-backup commits
  (`backup <ts>Z`, `<branch> session commit <ts>`) to the branch. Compare
  `git ls-remote origin <branch>` with the reported sha and drop strays with
  `--force-with-lease`.
- Live `ciao eval tasks` runs (`docs/TASK_EVAL.md`) import the checkout under
  test through an editable venv. Run them from a detached worktree with its own
  venv, never from an issue worktree: the merge queue removes that worktree and
  every later run in the batch errors.

## Plan traps

Repo-specific traps for loop plans. Planners and the goldfish `codebase_details`
check read them alongside the generic list in the skill's `planning.md`.

- **Vitest environment.** Many web test files run in `node`, not `jsdom`. A plan
  that sets a `window` global in one with no `// @vitest-environment jsdom`
  header (and none in the config) makes every case throw. Check the header and
  config; use `globalThis` or name the environment change.
- **Guard tests assemble banned names.** A guard test builds a removed global
  from fragments (`'__CIAOBOT' + '_DESKTOP__'`), so `git grep` misses it. A plan
  that brings back a removed name runs the frontend suite on a scratch edit.
- **Hidden UI is pinned by e2e hooks, not labels.** Hiding a phone header
  control and making a strip non-scrollable broke four specs that asserted
  through `scroll-snap-type`, native touch events, `.work-inspector-trigger`
  and focus on the hidden control. Grep `web/e2e` for every such hook.
- **Class prefixes are taken.** A new header named `chat-rail` collided with
  another panel's scoped `chat-rail-*` rules and broke two e2e specs. Check
  with `git grep -ohE "<prefix>[a-z-]*" -- web/src web/e2e | sort -u`.
- **Shared `<style scoped src>` sheets need a stated cascade.** Shared block
  last, legacy rules scoped to the variant that still uses them, overrides as
  compound selectors (`.cd-detail.my-warning`); a lone class loses to
  `.cd-detail`. Rows bleeding with negative margins break section-level
  `scrollWidth <= clientWidth` checks: measure the padded container. See
  `web/README.md`.
- **Leftover greps vs absence tests.** A Verify step
  `git grep -n "<removed hook>" -- web/src web/e2e  # expect no output` can
  never pass when the plan's own tests assert `.exists()).toBe(false)`. Scope it
  with `':!**/__tests__/**'` or list the allowed hits.
- **macOS-only coordinators fail on the Linux runner.** A module gated on
  `sys.platform != "darwin"` passed 115 tests on a Mac and failed every
  lifecycle test on the Linux PR runner. Its tests force the gate with an
  autouse fixture, as the existing suites for that area do.
- **The real classifier and lock in one test.** A repair coordinator's tests
  injected `classify`; the real classifier, run under the coordinator's own
  lock, read that lock as "another install in progress" and refused every real
  run. Keep one end-to-end test with the real classifier and lock, faking only
  launchctl, network and codesign.
- **Stock skill size caps.** A test asserts `len(skill.encode()) <= 13200` on a
  stock skill; a plan that adds text to one checks the cap first.
- **Every `last_error` writer.** A plan changed one `last_error` writer and
  missed the second in the wake-turn path. `git grep -n "last_error ="`.
- **e2e runs through `npm run test:e2e`.** Its `pretest:e2e` rebuilds the
  bundle; a bare `npx playwright test` serves the last build.
