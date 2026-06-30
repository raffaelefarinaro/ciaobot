# scripts/

Operator scripts. Run from the repo root; most expect the project venv at `.venv/` and an `.env` file with at least `PWA_AUTH_TOKEN`.

| File | Purpose |
|---|---|
| `run-ciao.sh` | Production launcher: activates the venv, sources `.env`, execs the Ciao server. |
| `dev.sh` | Compatibility wrapper for `ciao dev` after running `ensure-deps.sh`. |
| `gws-profile.sh` | Dual-account wrapper for the `gws` CLI: routes `personal` to `secrets/gws-personal/`, `work` to `secrets/gws/`. Restores credentials from `.env` if missing. |
| `gws-personal.sh` | Wrapper to run `gws` commands for the `personal` GWS profile. |
| `gws-work.sh` | Wrapper to run `gws` commands for the `work` GWS profile. |
| `gws-secrets.py` | Backup and restore GWS credentials to/from `.env` in base64. |
| `install-custom-skills.sh` | Compatibility wrapper for `ciao sync-skills`, which syncs canonical `skills/`, `subagents/`, and `commands/` into Claude and Pi catalogs and refreshes upstream skills from `skills-lock.json` when possible. |
| `skills_add.py` | Add an upstream skill from GitHub by pasting a URL (`scripts/skills_add.py <github-url>`). Wraps `npx skills add`, inferring `--skill` from a `/skills/<name>` URL segment. The added skill auto-updates on startup via `ciao sync-skills` + `ciao skills-sync`. |
| `skills_sync.py` | Compatibility wrapper for `ciao skills-sync`, the upstream-skill change detector used by `ciao sync-skills`. |
| `sync-claude-agents-to-pi.py` | Compatibility wrapper for `ciao sync-agents-to-pi`, which converts `subagents/*.md` into Pi-flavored `~/.pi/agent/agents/*.md` files. |
| `vault_index.py` | Compatibility wrapper for `ciao vault-index`. Default prints TSV; `--write` regenerates `memory-vault/INDEX.md`. Supports `--type`, `--tag`, `--related-to`, `--neighbors`. |
| `backfill_insights.py` | Backfill `## Session insights` sections into archived chats that predate the feature. One-shot; live archives are handled by `ciao/insights.py`. |
| `cleanup_sdk_blobs.py` | Compatibility wrapper for `ciao cleanup-sdk-blobs`. Reclaims `~/.claude/projects/.../*.jsonl` blobs for chats already archived to the vault. Dry-run by default; `--apply` to delete. |
| `morning-briefing.py` | Fetches Zurich weather + today's calendar events. Used by the morning briefing schedule. |
| `work_chat_transcripts.py` | Lists archived chat transcripts for a given date, filtered by workspace. Used by `sched-workdaily` so the CHATS subagent only sees work chats. |
