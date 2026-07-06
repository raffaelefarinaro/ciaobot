# Contributing to Ciaobot

Ciaobot started as a personal project: it is one person's opinionated idea of how Claude Code works best as a daily personal assistant and second brain. It is shared in the open because the patterns may be useful to others, and because outside ideas make it better.

Contributions of every size are welcome:

- **Ideas and feedback.** Open a GitHub issue describing your use case, what felt wrong, or what you wish existed. Disagreement with the built-in opinions is useful input.
- **Bug reports.** Include reproduction steps, relevant log output (`.runtime/server_errors.log`), and your platform.
- **Pull requests.** Small, focused changes are easiest to review. For larger ideas, open an issue first so we can agree on direction before you invest time.

## Ground rules

- Read `docs/ARCHITECTURE.md` and `docs/DEVELOPMENT.md` before changing code.
- Keep changes scoped and covered by tests: `pytest tests/` for the backend, `cd web && npm run build` (and `npm test`) for the PWA.
- Documentation is test-enforced in places: new API routes must appear in `PWA_API.md`, and new `CIAO_*` env vars in `INTEGRATIONS.md`.
- Never include secrets, personal workspace data, or operator credentials in commits.
- Use plain, factual engineering notes in commits and pull requests.

## Design philosophy

Defaults are opinionated on purpose (project-first navigation, vault-first memory, explicit provider routing, review-gated memory promotion). If a behavior gets in your way, prefer adding a configuration surface over changing the default, and explain the use case in the PR.
