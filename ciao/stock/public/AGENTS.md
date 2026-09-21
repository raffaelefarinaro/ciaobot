# Ciaobot Contributor Guide

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

Use plain, factual engineering notes in commits and pull requests.
