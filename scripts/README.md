# scripts/

Operator scripts. Run from the repo root; most expect the project venv at `.venv/` and an `.env` file with at least `PWA_AUTH_TOKEN`.

| File | Purpose |
|---|---|
| `run-ciao.sh` | Production launcher: activates the venv, sources `.env`, execs the Ciaobot server. |
| `dev.sh` | Compatibility wrapper for `ciao dev` after running `ensure-deps.sh`. |
| `prepare-release` | Prepare a release branch: bump package/PWA versions, update `CHANGELOG.md`, run release checks, commit, push, and optionally open a draft GitHub PR. Dry-run by default. Also reports available dependency updates (PyPI/npm) so you can decide whether to adopt them, and auto-bumps the Claude SDK (`claude-agent-sdk`) on `--apply`. Pass `--skip-dep-check` to disable. On `--apply` it also regenerates the packaged `gws-*` stock skills from the installed `gws` CLI (reports the pinned-vs-installed version in the plan); pass `--skip-gws-skills` to disable. Cuts the branch from `develop` by default and opens the PR into `main`. |
| `configure-github-branches.sh` | One-time GitHub setup for the release model: set `develop` as the default branch and require pull requests + CI on `develop` and `main`. Requires `gh` with admin access. |
| `gws-profile.sh` | Dual-account wrapper for the `gws` CLI: routes `personal` to `secrets/gws-personal/`, `work` to `secrets/gws/`. |
| `install-custom-skills.sh` | Compatibility wrapper for `ciao sync-skills`, which syncs canonical `skills/`, `subagents/`, and `commands/` into Claude catalogs and refreshes upstream skills from `skills-lock.json` when possible. |
| `skills_add.py` | Add an upstream skill from GitHub by pasting a URL (`scripts/skills_add.py <github-url>`). Wraps `npx skills add`, inferring `--skill` from a `/skills/<name>` URL segment. The added skill auto-updates on startup via `ciao sync-skills` + `ciao skills-sync`. |
| `skills_sync.py` | Compatibility wrapper for `ciao skills-sync`, the upstream-skill change detector used by `ciao sync-skills`. |
| `vault_index.py` | Compatibility wrapper for `ciao vault-index`. Default prints TSV; `--write` regenerates `memory-vault/INDEX.md`. Supports `--type`, `--tag`, `--related-to`, `--neighbors`. |
| `backfill_insights.py` | Backfill `## Session insights` sections into archived chats that predate the feature. One-shot; live archives are handled by `ciao/insights.py`. |
| `cleanup_sdk_blobs.py` | Compatibility wrapper for `ciao cleanup-sdk-blobs`. Reclaims `~/.claude/projects/.../*.jsonl` blobs for chats already archived to the vault. Dry-run by default; `--apply` to delete. |
