# Ciaobot Workspace Guide

You are running inside a local Ciaobot workspace.

Baseline operating policies are injected into every Ciaobot chat. Never restart
the Ciaobot service or replace its running frontend assets from inside a chat.

Before changing configuration or workspace files:

- Read `CIAO_CUSTOMIZATION.md` for the supported customization surface.
- Treat `.env`, `.runtime/`, OAuth tokens, and provider keys as private.
- Never reveal secrets. You may report whether a credential appears configured.
- Prefer workspace-local configuration over edits to the installed package.
- When a setting requires restart, explain what changed and tell the operator to
  restart or update Ciaobot from Settings.

Useful local files:

- `.env`: server, provider, workspace, and integration configuration.
- `.runtime/workspaces.json`: logical workspace registry.
- `.runtime/server_errors.log`: rotating runtime error log.
- `.runtime/job_runs.jsonl`: recent background automation runs.
- `.agents/skills/`: shared workspace skills discovered by the supported
  runtimes.
- `.claude/agents/`, `.claude/commands/`, `.claude/skills/`: Claude Code assets.
- `skills/`, `commands/`, and `subagents/`: canonical workspace-authored assets.
- `memory-vault/`: durable markdown memory, projects, logs, and references.

File and project routing:

- Use the active workspace and vault path supplied in the Ciaobot context; do not guess another workspace's folder.
- For project work, inspect the active project's canonical document and relevant live files before editing.
- Use workspace-relative paths for files. Use `vault_search` for recall, native file tools for reading and writing, and `file_surface` when a deliverable should open in the user's file panel.

When diagnosing Ciaobot or preparing a GitHub issue, use sanitized log excerpts,
reproduction steps, platform, install method/version, and expected versus actual
behavior. Ask before creating or posting a public issue.
