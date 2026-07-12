# Ciao Contributor Guide

You are working on the Ciaobot app repository.

Before changing code:
- Read `docs/ARCHITECTURE.md` for the system design and `docs/DEVELOPMENT.md` for the dev workflow.
- Read `web/README.md` before changing the PWA.
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
- For UI changes, verify keyboard focus, browser zoom, and mobile touch targets.

Use plain, factual engineering notes in commits and pull requests.

## Reporting Issues & Continuous Improvement
- As an open-source project, if you (the agent) discover bugs, unexpected behavior, test failures, or potential enhancements in `ciaobot` (either during development or when trying to run/use the project), you can and should create a GitHub issue in the repository: `https://github.com/raffaelefarinaro/ciaobot`.
- To do this, use the local GitHub CLI (`gh`) if available and authenticated.
- Always explain the issue clearly to the user and suggest creating a GitHub issue. You can run the following command to file the issue:
  ```bash
  gh issue create --repo raffaelefarinaro/ciaobot --title "[Agent] Brief summary of the issue" --body "Detailed description of the problem, reproducing steps, relevant code locations, and logs."
  ```
- This helps maintain a continuous loop of improvements for the open-source repository.
