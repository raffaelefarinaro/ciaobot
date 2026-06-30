# Ciao Customization Guide

This document is for agents and operators working inside an installed Ciao workspace. It explains where customization lives and what can be changed safely.

## Where to Look First

- `CIAO_CUSTOMIZATION.md`: this guide.
- `.env`: server config, provider keys, model lists, OAuth-related paths, push contact, and runtime paths.
- `.runtime/workspaces.json`: logical workspaces when `CIAO_WORKSPACES` is not set in `.env`.
- `.claude/agents/`: project agents available to Claude-backed chats.
- `.claude/commands/`: slash commands available to Claude-backed chats.
- `.claude/skills/`: skills available to Claude-backed chats.
- `memory-vault/`: durable workspace memory, projects, references, and chat logs.

Do not edit package files under the Python installation for normal customization. Prefer workspace files.

## Workspaces

Ciao has two workspace concepts:

- `CIAO_WORKSPACE`: the filesystem root for this local Ciao instance. It contains `.env`, `.runtime/`, `.claude/`, and usually `memory-vault/`.
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
    "model_bucket": "anthropic",
    "disallowed_tools": []
  },
  {
    "name": "client-a",
    "vault_root": "vaults/client-a",
    "default_model": "sonnet",
    "gws_profile": "work",
    "model_bucket": "anthropic",
    "disallowed_tools": ["mcp__claude_ai_Slack", "mcp__claude_ai_Salesforce"]
  }
]
```

`vault_root` is relative to `CIAO_WORKSPACE` unless it is absolute.

## Providers and Models

Ciao routes each chat through a provider:

- `claude`: Claude Code / Claude Agent SDK. It can use Anthropic models directly or route selected models through Ollama-compatible settings.
- `pi`: Pi coding agent subprocess. It has its own model/provider routing.

Useful `.env` settings:

- `CLAUDE_MODELS`: Anthropic model aliases shown in the picker. Default: `opus,sonnet,haiku`.
- `CIAO_OLLAMA_MODELS`: Ollama cloud or compatible model IDs shown in the picker.
- `CIAO_OLLAMA_LOCAL_MODELS`: local Ollama daemon model IDs to pin into the picker.
- `CIAO_PI_MODELS`: Pi-native add-on model IDs.
- `CIAO_PI_DEFAULT_MODEL`: default model for new Pi chats.
- `CLAUDE_DEFAULT_MODEL_PERSONAL` and `CLAUDE_DEFAULT_MODEL_WORK`: legacy defaults for the built-in personal/work workspaces.
- `CIAO_WORKSPACES`: preferred multi-workspace registry. Use `default_model` and `model_bucket` per workspace.

`model_bucket` controls how Claude aliases route:

- `anthropic` or `work`: keep aliases such as `opus`, `sonnet`, and `haiku` on Anthropic.
- `ollama` or `personal`: allow aliases to resolve to configured Ollama tier models.

The chat picker can still override the workspace default for a specific chat.

## API Keys and Secrets

Provider keys live in `.env` or the provider's own OAuth store. Do not put keys in vault pages, docs, prompts, or git commits.

Common keys:

- `ANTHROPIC_API_KEY`: Anthropic API fallback when not using Claude OAuth.
- `OPENAI_API_KEY`: OpenAI features such as cloud voice transcription.
- `CIAO_OLLAMA_API_KEY`: Ollama cloud API key.
- `OPENROUTER_API_KEY`: optional critique/review model routing.

Agents may check whether a key is set, but must not print the value.

## MCPs, Tools, Skills, and Agents

Workspaces can limit tool access with `disallowed_tools`.

Use this when a workspace should not see certain MCPs, connectors, or high-risk tools. Examples:

```json
{
  "name": "client-a",
  "vault_root": "vaults/client-a",
  "model_bucket": "anthropic",
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

Skills and agents are installed into `.claude/skills/` and `.claude/agents/`. Ciao also mirrors supported assets to Pi with `ciao sync-skills`.

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
- `model`, `provider`, `model_bucket`: optional overrides. Empty means inherit/default.
- `archive_policy`: `manual` or `auto`.

## Safe Change Rules

Safe workspace-level changes:

- Add or edit `.runtime/workspaces.json`.
- Add project docs, vault references, and memory pages.
- Add or update `.claude/skills/`, `.claude/agents/`, and `.claude/commands/`.
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
