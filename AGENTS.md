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
     `no-any-return`: several attributes (`ProjectChatManager._background_runner`,
     for one) are typed `Any` because they are wired after construction, so
     returning a call on one straight out of a typed function is an error.
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
  PRs into `develop` run these on Linux, and the first three on Windows too:
  the `windows` job (`windows-latest`) blocks on every module importing,
  `python -m mypy ciao` (natively win32, so a POSIX-only call such as
  `os.getuid` or `signal.SIGHUP` outside a platform branch fails it), the
  full `pytest` suite and `npm test`. The macOS job (browser tests and an
  engine cold-start) runs after merge, so a PR going green is not proof the
  macOS job will.
  `pip-audit`, `npm audit` and `npm run lint` are advisory in CI (`|| true`).
  Lint is still worth running; it just will not fail the build for you.
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
- Use plain, factual engineering notes in commits and pull requests, with a
  `Co-Authored-By:` trailer naming the tool.
- [`docs/UPKEEP.md`](docs/UPKEEP.md) is the watchlist of topics we keep an eye on,
  with the last-checked date and the decision. Add a row in whatever PR you are
  already in when you notice something worth watching; a stale date on an
  unchecked row is worse than no date. The checks themselves are in the
  `/ciao-upkeep` skill. `/ciao-release` does not fix things: a defect found
  during a release becomes an issue, fixed on `develop` before the next cut.

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
