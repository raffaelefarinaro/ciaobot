# Ciao Contributor Guide

You are working on the Ciaobot app repository. This is the only agent rules
file; Claude Code and opencode both load it.

Before changing code:
- Read the relevant sections of `docs/ARCHITECTURE.md` (system design) and
  `docs/DEVELOPMENT.md` (dev workflow). Both are 150–320 KB: search them, do not
  load them whole.
- Before changing the PWA, read `web/README.md` and [`DESIGN.md`](DESIGN.md),
  and keep DESIGN.md's tokens and interaction principles aligned with the code.
- Keep changes scoped and covered by tests.
- Do not add fallbacks or compatibility shims. Delete dead or superseded code
  outright rather than leaving a code path "just in case". When removing a
  fallback would break an install that people already have, stop and ask
  the maintainer before removing it.
- Avoid new environment variables. Hardcode a sensible default as a
  constant; if a value truly must vary per user, make it a Settings option
  instead. Add an env var only when nothing else can work (secrets,
  install paths, test isolation), and document it in `INTEGRATIONS.md`.
- Do not commit secrets, private workspace data, or operator credentials.

## Project shape
- Backend: Python 3.12, Starlette + uvicorn. Frontend: Vue 3 + Vite + Pinia +
  TypeScript in `web/`.
- `ciao/cli.py` is the `ciao` CLI; `ciao/main.py` is the server entry.
- `ciao/web/app.py` lists every route as an explicit `Route(...)`; handlers
  live in `ciao/web/routes_*.py`.
- `ciao/providers/` holds `claude.py` (Claude Agent SDK), `opencode.py` and the
  shared `base.py`. One-shot model calls go through `providers/oneshot.py`;
  there is no Messages API and no provider-enforced structured output, so
  validate model JSON locally.
- `ciao/stock/` holds generic assets shipped in the wheel (skills, commands,
  schedules, workspace templates). User vaults and runtime data belong in a
  separate workspace, never in this repo.
- `web/src/`: `components/` (one SFC per pane, Settings panels in `settings/`),
  `stores/` (Pinia), `composables/`, `lib/` (pure helpers, no Vue imports).
  Tests sit in `__tests__/` beside the code, or as `foo.test.ts` next to `lib/foo.ts`.
- `npm run build` writes into `ciao/web/static/`, which is gitignored.
- There is no client/host split any more: one engine, one origin, one session.
  Keep `/api/*` behind the signed session cookie, keep every `/ws/*` handshake
  same-origin-gated, and keep the loopback-only set (`_LOOPBACK_ONLY_API` in
  `ciao/web/auth.py`) gated on the TCP peer. The boundary audit is in
  `docs/REMOTE_BOUNDARY.md`.

## Commands
```bash
.venv/bin/pytest tests/test_x.py -q      # focused; use the venv, not pytest on PATH
.venv/bin/pytest -n auto tests/          # full backend suite, about a minute
.venv/bin/mypy ciao
cd web && npm test                       # vitest
cd web && npm run build                  # vue-tsc + vite build
cd web && npm run test:e2e               # Playwright
ciao dev                                 # backend :8543 + Vite :5173
scripts/dev-commands.sh all              # typecheck, coverage, lint, audit
```

## Verification
- Run focused tests for the changed behavior.
- Run every gate CI blocks on before pushing, not just the tests. CI's
  blocking steps are, in order:
  1. `mypy ciao`, which is easy to forget and fails the whole job. Watch for
     `no-any-return`: an attribute typed `Any` (for instance one wired after construction)
     makes any call on it return `Any`, so returning that call straight out
     of a typed function is an error.
     Annotate the local instead.
  2. `pytest -n auto tests/`, the full suite, before claiming backend work is
     complete. A fake object in an unrelated test can break on a new
     attribute (adding a field to the `/ws/events` snapshot broke
     `tests/test_ws_auth.py`, whose `SimpleNamespace` stub had no such
     attribute), so a green focused run proves nothing about the suite.
  3. `cd web && npm test`, the full frontend suite. Needs Node >= 22.22.2;
     `npx vitest` on an older Node silently skips component files while
     printing green. The floor is set by jsdom and lives in three places that
     must agree: `web/package.json` `engines`, `SUPPORTED_RANGE` in
     `web/scripts/check-node.mjs`, and `docs/DEVELOPMENT.md`.
  4. `cd web && npm run build` after frontend changes.
  PRs into `develop` run these on Linux (`linux-server` for the backend,
  `linux-web` for vitest and the build, in parallel). On Windows a develop PR
  runs only the fast gates: every module importing and `python -m mypy ciao`
  (natively win32, so a POSIX-only call such as `os.getuid` or `signal.SIGHUP`
  outside a platform branch fails it). The Windows `pytest` and `npm test`
  suites and the macOS job (browser tests and an engine cold-start) run on
  the push to `develop` after merge and on release PRs into `main`, so a PR
  going green is not proof those will. Path handling is the usual Windows
  break (`ciao\x` vs `ciao/x`): compare paths with `Path`/`as_posix()`, not
  string literals.
  A test that hands a path to code that checks it is absolute builds it from
  `tmp_path`: `/nonexistent/x` has no drive and is relative on Windows.
  `pip-audit`, `npm audit` and `npm run lint` are advisory in CI (`|| true`).
  Lint is still worth running; it just will not fail the build for you.
- Run one full backend suite and one frontend run at a time, here and in any
  gate script. Two concurrent suites starve the machine and time out the 20 s
  component tests (`SettingsFsScope`), which reads as a red gate. A single
  failure is re-run (`--lf`) before it is called red. Timing tests assert
  relative behaviour with a small absolute allowance, not a fixed millisecond
  budget a loaded machine can miss.
- Assert a guard where it acts, not only in the finished output. When a change
  bounds, redacts, filters or fences untrusted text, the test should observe the
  value at that seam (what is handed to the fence, filter or builder), and an
  ordering-sensitive filter needs more inputs than its limit so that reversing
  "filter then limit" fails the test.
- Some docs are checked by tests, so new code often needs a doc edit:
  - a new route in `app.py` goes in `PWA_API.md`. A new POST/PATCH/DELETE
    `/api/*` route also needs an entry under `## Agent recipes` there, or a
    place in `BROWSER_OR_INTERNAL_ROUTES` in `tests/test_pwa_api_docs.py`;
  - a new `CIAO_*` env var goes in `INTEGRATIONS.md` (`tests/test_env_vars_documented.py`);
  - a new module under `ciao/` goes in `docs/ARCHITECTURE.md`'s repo layout
    (`tests/test_architecture_doc.py`);
  - Windows installer or service changes go in `docs/WINDOWS.md` (`tests/test_windows_docs.py`).
- For UI changes, verify keyboard focus, browser zoom, and 44px mobile touch
  targets. Every new feature must be visually inspected in the browser before
  pushing.
- Workspace shortcuts map unmodified `1`–`9` to the visible sidebar order and
  must remain inert while a text field is focused. The Settings keyboard
  shortcuts card can disable or rebind them; preferences are shared through the
  engine. Enter-to-send is also shared, but touch keyboards always insert a
  newline. Preserve picker precedence and IME composition handling.
- OpenCode provider changes must preserve the V2-only 2.0.16+ contract. Replay
  the V2 fixtures and run one tiny real turn against the installed OpenCode 2.x
  server; do not restore V1 route or response-shape fallbacks.

## Common mistakes
- **Restarting the service.** Never restart the running engine yourself; it cuts
  off the PWA session you may be talking through. The user deploys from Settings.
- **Treating this checkout as the live install.** An installed engine runs its
  own released copy. Never apply a develop-only migration or schema change to a
  live workspace.
- **Scripts run outside pytest.** `tests/conftest.py` points home,
  `CIAO_MEMORY_DIR` and LaunchAgents at `tmp_path`. A harness outside pytest
  must set `CIAO_MEMORY_DIR` and `CIAO_LAUNCH_AGENTS_DIR` itself, or it
  overwrites the real index and LaunchAgent.
- **Deleting a module.** `mypy` (`ignore_missing_imports = true`) and pytest
  both stay green on leftover imports, function-local ones especially. Grep for
  the module name and import every `ciao` module before calling it done.
- **Removing a background job.** Add its id to `job_runs.RETIRED_JOBS`, or the
  debug report keeps listing its stale last run.
- **Worktrees.** `web/node_modules` does not come along; symlink it from the main
  checkout and delete the symlink before `git add`, since `.gitignore` does not
  match a symlink.
- **Shared checkout.** Other agents may be editing `develop` at the same time.
  Work on a branch off `develop`, preferably in a worktree, and check
  `git status` for files you did not touch before committing.

## Branching and releases
- All pull requests target `develop`. Only the admin creates releases from `develop`.
- Use plain, factual engineering notes in commits and pull requests.
- [`docs/UPKEEP.md`](docs/UPKEEP.md) is the watchlist of topics we keep an eye on,
  with the last-checked date and the decision. Add a row in whatever PR you are
  already in when you notice something worth watching; a stale date on an
  unchecked row is worse than no date. The checks themselves are in the
  `/ciao-upkeep` skill. `/ciao-release` does not fix things: a defect found
  during a release becomes an issue, fixed on `develop` before the next cut.

## Loop engineering notes
Facts the user-level `loop-engineering` skill (`~/.claude/skills`) reads from
this file; the gates are the ones under Verification.
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
  `ciao-support` skill). `report`-labelled issues are auto-opened and may be
  noise: flag, don't drop. `not_ready` issues are skipped by the backlog loop.
- After merging a batch into `develop`, watch the post-merge push run
  (`gh run list --workflow ci.yml --branch develop --event push --limit 1`).
  The Windows suites and the macOS job run only there. If it goes red, open a
  `bug` issue naming the failing tests and the merge that broke them, and fix
  it before merging more on top. `/ciao-release` will not cut over a red run.
  Before queuing a PR whose diff touches path handling, the filesystem,
  subprocesses or Windows code, add the `windows-full` label and push once, so
  the full Windows suites run on the PR instead of after merge.
- Hands-on device testing (a real Windows or macOS install) is for diffs that
  touch the installers (`scripts/install*.sh`, `scripts/install.ps1`), the
  service or scheduled-task code, engine update/migration, or `desktop/`.
  Other issues rely on CI.
- Security-sensitive diffs (ship fan-out plus a PR-scoped security review):
  auth, secrets, `/api` or `/ws` routes, `docs/REMOTE_BOUNDARY.md`, `desktop/`
  (also run the `desktop/src-tauri` commands in `docs/DEVELOPMENT.md`),
  release plumbing.
- There is no `/code-review` command for reviewer workers; review from
  `git diff origin/develop...HEAD`.
- Other sessions merge into `develop` all day, so use the skill's serial merge
  queue. The `gates.sh` for this repo runs, from the worktree root: `PYTHONPATH=$PWD
  ~/repos/ciaobot/.venv/bin/python -m mypy ciao`, the same with `--platform
  win32`, `PYTHONPATH=$PWD ~/repos/ciaobot/.venv/bin/python -m pytest -n auto
  tests/` (re-run only `--lf` before calling it red; latency tests flake under
  load), and, after symlinking `web/node_modules`, `npm test` and `npm run build`
  in `web/` (remove the symlink afterwards). When the diff touches `web/`, it
  also runs `npm run test:e2e` after the build: the Playwright suite runs in CI
  only on the macOS job after merge, so a renamed button a spec still looks for
  reaches `develop` red (#2279). The terminal-renderer specs need
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

## Reporting issues
When you find a bug, test failure or worthwhile enhancement, explain it to the
user and suggest filing it:
```bash
gh issue create --repo raffaelefarinaro/ciaobot --title "[Agent] Brief summary" --body "Problem, repro steps, code locations, logs."
```


<!-- ciao:memory:start cap=3000 -->
## Agent memory

<!-- ciao:memory:end -->

<!-- ciao:profile:start cap=1375 -->
## User profile

<!-- ciao:profile:end -->
