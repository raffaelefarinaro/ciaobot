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
- Run `pytest tests/` before claiming backend work is complete.
- Run `cd web && npm run build` after frontend changes.
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
