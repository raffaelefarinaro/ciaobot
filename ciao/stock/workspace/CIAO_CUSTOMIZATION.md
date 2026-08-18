# Ciaobot Customization Guide

This document is for agents and operators working inside an installed Ciaobot workspace. It explains where customization lives and what can be changed safely.

## Where to Look First

- `CIAO_CUSTOMIZATION.md`: this guide.
- `.env`: server config, provider keys, model lists, OAuth-related paths, push contact, and runtime paths.
- `.runtime/workspaces.json`: logical workspaces when `CIAO_WORKSPACES` is not set in `.env`.
- `.claude/agents/`: project agents available to Claude-backed chats.
- `.claude/commands/`: slash commands available to Claude-backed chats.
- `.claude/skills/`: skills available to Claude-backed chats.
- `.agents/skills/`: Codex workspace skills plus generated command/agent wrappers.
- `.codex/agents/` and `.codex/config.toml`: generated native Codex agent definitions and registrations.
- `memory-vault/`: durable workspace memory, projects, references, and chat logs.

Do not edit package files under the Python installation for normal customization. Prefer workspace files.

## Workspaces

Ciaobot has two workspace concepts:

- `CIAO_WORKSPACE`: the filesystem root for this local Ciaobot instance. It contains `.env`, `.runtime/`, `.claude/`, and usually `memory-vault/`.
- Logical workspaces: named chat spaces such as `default`, `personal`, `work`, or `client-a`. These appear in the PWA sidebar and route projects, chats, vault roots, model defaults, and integration profiles.

Logical workspaces are configured with `CIAO_WORKSPACES` in `.env` or `.runtime/workspaces.json`.

Example `.runtime/workspaces.json`:

```json
[
  {
    "name": "default",
    "vault_root": "memory-vault",
    "default_model": "opus",
    "gws_profile": "personal",
    "color": "pink",
    "disallowed_tools": []
  },
  {
    "name": "client-a",
    "vault_root": "vaults/client-a",
    "default_model": "sonnet",
    "gws_profile": "work",
    "color": "cyan",
    "disallowed_tools": ["mcp__claude_ai_Slack", "mcp__claude_ai_Salesforce"]
  }
]
```

`vault_root` is relative to `CIAO_WORKSPACE` unless it is absolute.

`color` is an optional PWA accent preset (`pink`, `cyan`, `amber`, `emerald`, `violet`). Missing values default to Ciao pink. Only accent tokens change; canvas colors stay fixed.

## Providers and Models

Ciaobot supports three chat providers, each authenticating through its own CLI:

- `claude`: Claude Code / Claude Agent SDK, against Anthropic.
- `codex`: OpenAI Codex CLI app-server. It uses the account authenticated by `codex login` (including ChatGPT subscription login) and discovers that account's models and reasoning levels dynamically.
- `opencode`: the open-source agent CLI, bring-your-own model provider. This is how you reach anything else — Ollama, OpenRouter, or any OpenAI-compatible endpoint. Configure it in opencode and its models appear in Ciaobot's pickers automatically; Ciaobot lists whatever opencode reports as connected.

Useful `.env` settings:

- `CLAUDE_MODELS`: Anthropic model aliases shown in the picker. Default: `opus,sonnet,haiku`.
- `CLAUDE_DEFAULT_MODEL_PERSONAL` and `CLAUDE_DEFAULT_MODEL_WORK`: legacy defaults for the built-in personal/work workspaces.
- `CIAO_WORKSPACES`: preferred multi-workspace registry. Use `default_provider` and `default_model` per workspace.
- `CIAO_CODEX_BIN`: optional absolute override when `codex` is not discoverable on the service PATH.

Each provider has its own default model and thinking level for new chats, set
in Settings → Models → defaults per provider. A Claude model alias
(`haiku`, `sonnet`, `opus`) is a real Claude model id; Codex and opencode
resolve their own defaults from the signed-in account's catalog.

The chat picker can still override the workspace default for a specific chat.

## API Keys and Secrets

Provider keys live in `.env` or the provider's own OAuth store. Do not put keys in vault pages, docs, prompts, or git commits.

Common keys:

- Claude Code authentication is owned by the Claude CLI; use Settings → Providers to connect or verify it.
- Voice transcription and read-aloud use the host Mac's on-device Apple frameworks; no voice API key is required.
- Codex and opencode authentication is owned by their own CLIs; use `ciao auth <provider>` or Settings → Providers. There are no model API keys to set.

Agents may check whether a key is set, but must not print the value.

## MCPs, Tools, Skills, and Agents

Workspaces can limit tool access with `disallowed_tools`.

Use this when a workspace should not see certain MCPs, connectors, or high-risk tools. Examples:

```json
{
  "name": "client-a",
  "vault_root": "vaults/client-a",
  "disallowed_tools": [
    "mcp__claude_ai_Slack",
    "mcp__claude_ai_Airtable",
    "Bash"
  ]
}
```

Tool names follow Claude SDK naming:

- `mcp__server_name`: block an entire MCP server.
- `mcp__server_name__tool_name`: block one MCP tool.
- `Bash`: block the Bash tool.

Canonical assets live in `skills/`, `commands/`, and `subagents/`. Project MCPs live in `.mcp.json`. `ciao sync-skills` mirrors canonical assets into Claude's `.claude/` catalogs, Codex's `.agents/skills/` catalog, native Codex agent definitions under `.codex/`, and projects `.mcp.json` servers into `.codex/config.toml`. Ciaobot commands remain available through the same `/command` syntax in chats; Codex also receives skill wrappers for commands and named agent roles.

Use workspace-level tool deny lists for access control. Use skills and agents for behavior and workflow guidance.

## Memory and Vault Layout

The vault is markdown-first.

Common paths:

- `memory-vault/MEMORY.md`: durable workspace memory.
- `memory-vault/INDEX.md`: generated index from frontmatter and wikilinks.
- `memory-vault/projects/active/`: active projects.
- `memory-vault/projects/completed/`: completed projects.
- `memory-vault/Logs/Chats/`: archived chat transcripts.

Use `ciao vault-index` after larger vault edits. Use `ciao vault-search` to search existing memory before adding duplicate facts.

## Schedules and Automations

Runtime schedules live in `.runtime/schedules.json`.

System schedules are seeded by the package and are normally read-only in the UI. User schedules can run normal chat prompts against a target workspace/project/chat and can inherit that target's provider and model.

Important fields:

- `workspace`: logical workspace name.
- `web_project_id`: target PWA project.
- `model`, `provider`: optional overrides. Empty means inherit/default.
- `archive_policy`: `manual` or `auto`.

## Safe Change Rules

Safe workspace-level changes:

- Add or edit `.runtime/workspaces.json`.
- Add project docs, vault references, and memory pages.
- Add or update canonical `skills/`, `subagents/`, and `commands/` assets, then run `ciao sync-skills`.
- Change model lists and provider keys in `.env` without printing secrets.

Changes that usually need restart:

- Provider keys.
- `CIAO_WORKSPACES`.
- Model list env vars.
- `CIAO_WORKSPACE`, `CIAO_VAULT_ROOT`, and runtime path changes.

Changes that should be made through the app or package update flow:

- Installed package code.
- PWA static assets.
- LaunchAgent configuration.

If unsure, explain the file you would change, the reason, and whether restart is required.
