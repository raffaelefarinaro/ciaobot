# Ciao Contributor Guide

You are working on the Ciaobot app repository. This is the root agent rules
file; Claude Code and opencode both load it. The Swift Mac app in `mac/` adds
`mac/AGENTS.md`: read it before changing anything under `mac/`.

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
- `ciao/providers/oneshot.py` runs one-shot model calls. There is no Messages API
  and no provider-enforced structured output, so validate model JSON locally.
- `ciao/stock/` ships in the wheel. User vaults and runtime data belong in a
  separate workspace, never in this repo.
- One engine, one origin, one session. Keep `/api/*` behind the signed session
  cookie, every `/ws/*` handshake same-origin-gated, and the loopback-only set
  (`_LOOPBACK_ONLY_API` in `ciao/web/auth.py`) gated on the TCP peer. See
  `docs/REMOTE_BOUNDARY.md`.
- `mac/` is never part of the wheel; `mac/docs/reference.md` is its reference.

## Commands
- Use the venv, not pytest on PATH: `.venv/bin/pytest tests/test_x.py -q`,
  `.venv/bin/pytest -n auto tests/`, `.venv/bin/mypy ciao`.
- Frontend: `cd web && npm test`, never `npx vitest` (see the Node floor below).
- `scripts/dev-commands.sh all` runs typecheck, coverage, lint and audit.

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
  PRs into `develop` run `linux-server` and `linux-web`; `linux-e2e` and
  `mac-app` run only when the diff touches `web/` or `mac/`, `pyproject.toml`
  or `uv.lock`. Python 3.14 and macOS (`test`) run on release PRs and manual
  runs, so a green develop PR does not prove the release PR's macOS job.
  `npm run test:e2e` builds the PWA and terminal harness first. `pip-audit`,
  `npm audit` and `npm run lint` are advisory in CI.
- Run one full backend suite and one frontend run at a time; concurrent suites
  time out the 20 s component tests. Re-run a single failure (`--lf`) before
  calling it red. Timing tests assert relative behaviour, not fixed budgets.
- Re-run a red CI run whole (`gh run rerun <id>`, not `--failed` or "Re-run
  failed jobs"). A failed-jobs re-run leaves the PR's status rollup with only
  that attempt's jobs, so required checks such as `linux-server`, `linux-web`,
  the conditional `linux-e2e` and `mac-app` go missing and the PR shows BLOCKED
  while everything is green.
- Assert a guard where it acts: for bounded, redacted or filtered untrusted text,
  the test observes the value at that seam, with more inputs than the limit.
- Some docs are checked by tests, so new code often needs a doc edit:
  - a new route in `app.py` goes in `PWA_API.md`. A new POST/PATCH/DELETE
    `/api/*` route also needs an entry under `## Agent recipes` there, or a
    place in `BROWSER_OR_INTERNAL_ROUTES` in `tests/test_pwa_api_docs.py`;
  - a new `CIAO_*` env var goes in `INTEGRATIONS.md` (`tests/test_env_vars_documented.py`);
  - a new module under `ciao/` goes in `docs/ARCHITECTURE.md`'s repo layout
    (`tests/test_architecture_doc.py`);
- For UI changes, verify keyboard focus, browser zoom, and 44px mobile touch
  targets. Every new feature must be visually inspected in the browser before
  pushing.
- Keyboard shortcuts and Enter-to-send are specified in
  `web/README.md` and `DESIGN.md`; read them before touching those paths.
- OpenCode provider changes run over `opencode acp` at OpenCode 2.3.2 or newer.
  Replay the ACP fixtures and run one tiny real turn against the installed
  OpenCode 2.x; do not restore the retired HTTP server or its V1 route fallbacks.

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
- Add a row to [`docs/UPKEEP.md`](docs/UPKEEP.md) (watchlist, last-checked date,
  decision) when you notice something worth watching. A defect found during a
  release becomes an issue, fixed on `develop` before the next cut.

## Repository-specific loop facts
- A required `develop` check that fails reproducibly stops merges until it is
  fixed. A failure that passes on a whole-workflow rerun is a flake: log it and
  keep merging; a green PR may merge while develop reruns.
  Reviewed PRs with passing applicable checks may merge serially as a batch;
  use full-platform CI on the final combined SHA for integration acceptance.
- `Closes #N` does not close an issue when merging into `develop`. Close an
  issue explicitly only when its acceptance criteria are complete; partial
  PRs leave the issue open.
- The main checkout's venv is an editable install. When using it from another
  worktree, set `PYTHONPATH=$PWD` so tests import that worktree's code.
- Record the tree, commands, toolchain and dependency fingerprint for full
  gates. Reuse a valid pass only while its inputs remain unchanged; rerun the
  affected checks when they change. A suite-lock deferral is not a test failure.
- Installer, migration, service, provider lifecycle, authentication and release
  changes require a PR-scoped security review. Real macOS install testing is
  required for installer, service and engine update or migration changes.
- Shared documentation often conflicts across parallel PRs; preserve both
  changes when resolving `PWA_API.md`, `DESIGN.md`, `web/README.md` and
  `docs/ARCHITECTURE.md`.
- An engine started in a worktree can add backup or session commits to its
  branch. Compare the remote head with the intended head before pushing.
  Run live `python -m evals` only in a detached worktree with its own venv.

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
