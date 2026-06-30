# Ciao Workspace Guide

You are running inside a local Ciao workspace.

Before changing configuration or workspace files:
- Read `CIAO_CUSTOMIZATION.md` for the local customization surface.
- Treat `.env`, `.runtime/`, OAuth tokens, and provider keys as private.
- Do not reveal secrets. You may say whether a key appears configured, but do not print its value.
- Prefer workspace-local configuration over edits to the installed package.
- When a setting requires restart, tell the operator exactly what changed and that Ciao must be restarted or updated from Settings.

Useful local files:
- `.env`: server, provider, workspace, and integration config.
- `.runtime/workspaces.json`: logical workspace registry when `CIAO_WORKSPACES` is not set.
- `.claude/agents/`, `.claude/commands/`, `.claude/skills/`: installed agent-facing assets.
- `memory-vault/`: durable markdown memory, projects, logs, and references.

If the user asks what Ciao can customize, start with `CIAO_CUSTOMIZATION.md`.
