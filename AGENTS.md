# Ciao Contributor Guide

You are working on the Ciaobot app repository.

Before changing code:
- Read `docs/ARCHITECTURE.md` for the system design and `docs/DEVELOPMENT.md` for the dev workflow.
- Read `web/README.md` before changing the PWA.
- Read [`DESIGN.md`](DESIGN.md) before changing the PWA, and keep its tokens and interaction principles aligned with the implementation.
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

Project shape:
- App code lives in `ciao/`.
- PWA code lives in `web/`.
- Generic package assets live in `ciao/stock/`.
- User vaults and runtime data belong in a separate workspace, not in the public app repo.
- There is no client/host split any more: one engine, one origin, one session. Keep `/api/*` behind the signed session cookie, keep every `/ws/*` handshake same-origin-gated, and keep the loopback-only set (`_LOOPBACK_ONLY_API` in `ciao/web/auth.py`) gated on the TCP peer. The boundary audit is in `docs/REMOTE_BOUNDARY.md`.

Verification:
- Run focused tests for the changed behavior.
- Run every gate CI blocks on before pushing, not just the tests. CI's
  blocking steps are, in order:
  1. `mypy ciao` — easy to forget and it fails the whole job. Watch for
     `no-any-return`: several attributes (`ProjectChatManager._background_runner`,
     for one) are typed `Any` because they are wired after construction, so
     returning a call on one straight out of a typed function is an error.
     Annotate the local instead.
  2. `pytest -n auto tests/` — the full suite (parallel, about a minute),
     before claiming backend work is complete. A fake object in an unrelated test can break on a new
     attribute (adding a field to the `/ws/events` snapshot broke
     `tests/test_ws_auth.py`, whose `SimpleNamespace` stub had no such
     attribute), so a green focused run proves nothing about the suite.
  3. `cd web && npm test` — the full frontend suite. Needs Node >= 22.22.2;
     `npx vitest` on an older Node silently skips component files while
     printing green. The floor is set by jsdom and lives in three places that
     must agree: `web/package.json` `engines`, `SUPPORTED_RANGE` in
     `web/scripts/check-node.mjs`, and `docs/DEVELOPMENT.md`.
  4. `cd web && npm run build` after frontend changes.
  PRs into `develop` only run these on Linux; the macOS job (browser tests and
  an engine cold-start) runs after merge, so a PR going green is not proof the
  macOS job will.
  `pip-audit`, `npm audit` and `npm run lint` are advisory in CI (`|| true`).
  Lint is still worth running — it just will not fail the build for you.
- For UI changes, verify keyboard focus, browser zoom, and mobile touch targets.
- Workspace shortcuts map unmodified `1`–`9` to the visible sidebar order and
  must remain inert while a text field is focused.
- OpenCode provider changes must preserve the V2-only 2.0.16+ contract. Replay
  the V2 fixtures and run one tiny real turn against the installed OpenCode 2.x
  server; do not restore V1 route or response-shape fallbacks.
- Every new feature must be visually inspected in the browser before pushing.

Branching and releases:
- All pull requests target `develop`.
- Only the admin creates releases from `develop`.

Upkeep between releases:
- [`docs/UPKEEP.md`](docs/UPKEEP.md) is the watchlist: topics we keep an eye on,
  with the last-checked date and the decision. **Edit it during ordinary work** —
  add a row when you notice something worth watching, in whatever PR you are
  already in. It is a working document, not a periodic report, and a stale date
  on an unchecked row is worse than no date.
- The checks themselves live in the `/ciao-upkeep` skill: every dependency and
  pin we install (the `gws` CLI, the GitHub Actions pins and the paired Node
  floor), the stock skill catalog against `ciao-capabilities` and
  the marketing site, doc rot, and the advisory CI steps nobody reads.
- `/ciao-release` deliberately does none of this. It cuts, verifies, installs,
  walks the UI and ships; it does not fix. A defect found during a release
  becomes an issue, and the fix happens on `develop` before the next cut.

Use plain, factual engineering notes in commits and pull requests.

## Reporting Issues & Continuous Improvement
- As an open-source project, if you (the agent) discover bugs, unexpected behavior, test failures, or potential enhancements in `ciaobot` (either during development or when trying to run/use the project), you can and should create a GitHub issue in the repository: `https://github.com/raffaelefarinaro/ciaobot`.
- To do this, use the local GitHub CLI (`gh`) if available and authenticated.
- Always explain the issue clearly to the user and suggest creating a GitHub issue. You can run the following command to file the issue:
  ```bash
  gh issue create --repo raffaelefarinaro/ciaobot --title "[Agent] Brief summary of the issue" --body "Detailed description of the problem, reproducing steps, relevant code locations, and logs."
  ```
- This helps maintain a continuous loop of improvements for the open-source repository.
