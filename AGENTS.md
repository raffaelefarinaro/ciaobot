# Ciao Contributor Guide

You are working on the Ciaobot app repository.

Before changing code:
- Read `docs/ARCHITECTURE.md` for the system design and `docs/DEVELOPMENT.md` for the dev workflow.
- Read `web/README.md` before changing the PWA.
- Read [`DESIGN.md`](DESIGN.md) before changing the PWA or tray UI, and keep its tokens and interaction principles aligned with the implementation.
- Keep changes scoped and covered by tests.
- Do not commit secrets, private workspace data, or operator credentials.

Project shape:
- App code lives in `ciao/`.
- PWA code lives in `web/`.
- Generic package assets live in `ciao/stock/`.
- User vaults and runtime data belong in a separate workspace, not in the public app repo.

Verification:
- Run focused tests for the changed behavior.
- Run every gate CI blocks on before pushing, not just the tests. CI's
  blocking steps are, in order:
  1. `mypy ciao` — easy to forget and it fails the whole job. Watch for
     `no-any-return`: several attributes (`ProjectChatManager._background_runner`,
     for one) are typed `Any` because they are wired after construction, so
     returning a call on one straight out of a typed function is an error.
     Annotate the local instead.
  2. `pytest tests/` — the full suite, before claiming backend work is
     complete. A fake object in an unrelated test can break on a new
     attribute (adding a field to the `/ws/events` snapshot broke
     `tests/test_ws_auth.py`, whose `SimpleNamespace` stub had no such
     attribute), so a green focused run proves nothing about the suite.
  3. `cd web && npm test` — the full frontend suite. Needs Node >= 20.19;
     `npx vitest` on an older Node silently skips component files while
     printing green.
  4. `cd web && npm run build` after frontend changes.
  `pip-audit`, `npm audit` and `npm run lint` are advisory in CI (`|| true`).
  Lint is still worth running — it just will not fail the build for you.
- Run `./scripts/check-desktop.sh` after changes under `desktop/` — nothing else
  compiles the Rust shell, the Swift native sidecar, or assembles `Ciaobot.app`,
  so those break in CI rather than locally. Use `--fast` to skip the bundle step
  when you have not touched `desktop/native/` or `tauri.conf.json`. It needs
  Rust (`brew install rustup && rustup default 1.90.0`) and `swiftc`
  (`xcode-select --install`).
- For UI changes, verify keyboard focus, browser zoom, and mobile touch targets.
- Workspace shortcuts map unmodified `1`–`9` to the visible sidebar order and
  must remain inert while a text field is focused.
- Every new feature must be visually inspected in the browser before pushing.

Branching and releases:
- All pull requests target `develop`.
- Only the admin creates releases from `develop`.

Use plain, factual engineering notes in commits and pull requests.

## Reporting Issues & Continuous Improvement
- As an open-source project, if you (the agent) discover bugs, unexpected behavior, test failures, or potential enhancements in `ciaobot` (either during development or when trying to run/use the project), you can and should create a GitHub issue in the repository: `https://github.com/raffaelefarinaro/ciaobot`.
- To do this, use the local GitHub CLI (`gh`) if available and authenticated.
- Always explain the issue clearly to the user and suggest creating a GitHub issue. You can run the following command to file the issue:
  ```bash
  gh issue create --repo raffaelefarinaro/ciaobot --title "[Agent] Brief summary of the issue" --body "Detailed description of the problem, reproducing steps, relevant code locations, and logs."
  ```
- This helps maintain a continuous loop of improvements for the open-source repository.
