# Ciaobot Workspace Guide

You are running inside a local Ciaobot workspace.

Baseline operating policies (apply low-risk fixes directly instead of proposing them; never restart the Ciaobot service or replace its running frontend assets from inside a chat) are injected into every chat's system prompt by the app — this file only needs workspace-specific additions.

Before changing configuration or workspace files:
- Read `CIAO_CUSTOMIZATION.md` for the local customization surface.
- Treat `.env`, `.runtime/`, OAuth tokens, and provider keys as private.
- Do not reveal secrets. You may say whether a key appears configured, but do not print its value.
- Prefer workspace-local configuration over edits to the installed package.
- When a setting requires restart, tell the operator exactly what changed and that Ciaobot must be restarted or updated from Settings.

Useful local files:
- `.env`: server, provider, workspace, and integration config.
- `.runtime/workspaces.json`: logical workspace registry when `CIAO_WORKSPACES` is not set.
- `.runtime/server_errors.log`: rotating server error log for Ciaobot runtime failures.
- `.runtime/job_runs.jsonl`: recent background automation runs, including failed jobs.
- `.runtime/ciao.stderr.log` and `.runtime/ciao.stdout.log`: macOS LaunchAgent service logs when present.
- `.claude/agents/`, `.claude/commands/`, `.claude/skills/`: installed agent-facing assets.
- `.agents/skills/`: Codex skill mirror and Ciaobot command/agent wrappers.
- `memory-vault/`: durable markdown memory, projects, logs, and references.

When helping diagnose Ciaobot or prepare a GitHub issue, use sanitized excerpts from the runtime logs above plus reproduction steps, platform, install method/version, and expected vs actual behavior. Ask before creating or posting a public issue.

If the user asks what Ciaobot can customize, start with `CIAO_CUSTOMIZATION.md`.
