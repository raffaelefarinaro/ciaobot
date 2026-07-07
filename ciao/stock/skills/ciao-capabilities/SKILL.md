---
name: ciao-capabilities
description: Authoritative catalog of what Ciaobot can do, for capability questions and feature tours. Use whenever the user asks what Ciaobot is, what it can do, what features are available, whether it can do something specific, or how one of its features works (memory, vault, archiving, schedules, routines, workspaces, projects, skills, voice, models, providers, notifications, menu bar, files) — and when onboarding or giving a tour or walkthrough to a new user. Trigger on phrasings like "what can you do", "what can ciaobot do", "help me get started", "give me a tour", "can you remind me / remember / schedule", even when the word "Ciaobot" is not mentioned.
---

# Ciaobot Capabilities

You are running inside Ciaobot. The app's feature surface is not otherwise visible from a chat session — the system prompt covers behavior, not features — so answer capability questions from this catalog instead of guessing from generic Claude knowledge. If the running app visibly disagrees with something here (features evolve), trust the app and say so.

## How to answer

- **Specific question** ("can you schedule things?", "where do my archived chats go?") → answer from the relevant section only. Don't recite the whole catalog.
- **Broad question** ("what can you do?") → give the one-paragraph pitch plus the six pillars in a few lines each, then offer to go deeper on any of them.
- **New user / onboarding** → offer the guided tour below.
- Distinguish the **app** from **you**: this catalog is what the Ciaobot app provides. On top of it you have your normal agent abilities plus whatever skills, subagents, and slash commands are installed in this workspace (`.claude/skills/`, `.claude/agents/`, `.claude/commands/`).

## The one-paragraph pitch

Ciaobot is a local-first UI and UX layer for using Claude Code (and other backends) as a personal assistant and second brain. Chats, projects, files, schedules, memory, and archived knowledge live in one web app instead of being scattered across terminal sessions — and everything durable is plain markdown that works with any other tool even when Ciaobot is not running.

## Capability catalog

### 1. Chats, projects, and workspaces

- Hierarchy: **workspace → project → chat**. A workspace is a life area (personal, work, a client); each workspace holds projects; each project holds chats plus durable context (files, notes, decisions) that Ciaobot injects into every chat inside it — the agent doesn't rediscover what you're working on each time.
- Workspaces can have their own vault root, default model, integration profile, and tool deny-list.
- Each chat can override the workspace's model/provider from the picker.
- Voice transcription for chat input; push notifications; the PWA is installable on desktop and mobile.
- Chats can be spawned programmatically from within a chat (see the `create-chat` skill).

### 2. Memory and the vault (second brain)

- Chats are **archived into a markdown vault** (e.g. `memory-vault/Logs/Chats/`). From archived sessions Ciaobot extracts insights and drafts **memory proposals** — the user reviews and approves them before anything is promoted into durable memory (`MEMORY.md`). Nothing is memorized silently.
- The vault is standard, open markdown: notes, project folders, `CLAUDE.md`, `MEMORY.md`, a generated `INDEX.md` from frontmatter and wikilinks. It is agent-agnostic and remains useful without Ciaobot.
- Vault tooling: `ciao vault-search` (search before adding duplicate facts), `ciao vault-index` (rebuild the index after larger edits). Reading conventions live in the `vault-read` skill.

### 3. Schedules and automations

- A native scheduler dispatches recurring or one-off prompts as fresh chat turns into a target project or chat — daily/weekly/monthly/once, timezone-aware. Configure from the **Schedules page** or directly in chat (the `ciao-schedules` skill has the full recipe).
- System maintenance schedules ship with the app; the Automation page shows background job runs.

### 4. Files

- Create, preview, edit, and **restore** workspace and vault files from the PWA, with history — no terminal needed.

### 5. Skills, subagents, and commands (extensibility)

- **Stock skills** ship with the app and are synced into `.claude/skills/` (`ciao sync-skills`, runs at startup). A same-named skill in the workspace's `skills/` folder overrides the packaged copy.
- **Custom** skills, subagents, and slash commands are authored in the workspace (`skills/`, `subagents/`, `commands/`) and mirrored automatically.
- **GitHub-sourced skills** can be installed and are refreshed automatically on restart when upstream changes.
- **Skill evolution**: a background loop analyzes usage and proposes skill improvements — as reviewable proposals, never silent edits.

### 6. Models and providers

- Backends: **Claude Code** (Claude subscription or Anthropic API key), **Ollama** (cloud or local daemon), **OpenRouter**. No provider lock-in — chats and schedules can route through any configured backend.
- Per-workspace default model and model bucket (which controls how aliases like `opus`/`sonnet` resolve), per-chat override in the picker.

### 7. Google Workspace (`gws`)

- Ciaobot integrates with Gmail, Calendar, Drive, Docs, Sheets, Slides, and Tasks through the [`gws` CLI](https://github.com/googleworkspace/cli).
- **Settings → Integrations**: install `gws`, upload a GCP OAuth `client_secret.json` per profile, and connect Google accounts from the browser (no terminal required).
- Separate **personal** and **work** profiles; each workspace picks which profile to use (Settings → Workspaces).
- Stock **`gws-*` skills** ship with the app (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, Forms). Setup details: `gws-shared` skill and the ⓘ panel on the Integrations page.

### App and system surface

- **Settings page**: provider keys, model lists, skill/agent inventory, the injected system prompt (read-only), and local package updates from the UI.
- **macOS extras**: a menu bar companion (`ciao menubar`) with server status and open/restart/logs actions (the Ciaobot face turns scared when the server is down), a `Ciaobot.app` shortcut, and LaunchAgents so everything starts on login.
- **Local HTTP API**: the app exposes an API an in-chat agent can drive (create chats, subagents, commands) — recipes are in `PWA_API.md` in the Ciaobot GitHub repo (`raffaelefarinaro/ciaobot`); fetch it when you need the raw API surface. For the common cases, the shipped `create-chat` and `ciao-schedules` skills already contain the working recipes.

### Privacy and trust posture

Local-first: the server, vault, and runtime state live on the user's machine; traffic leaves only toward the configured model providers. Memory is opt-in via reviewed proposals, an existing notes folder is never discarded or rewritten during onboarding, and the vault stays portable plain markdown.

## Guided tour (new users)

When onboarding someone, walk these in order, hands-on rather than as a lecture — one short step at a time, checking in between:

1. **Orient**: workspaces in the sidebar, projects inside them, chats inside projects. Create or rename a project for something they're actually working on.
2. **Chat**: show the model picker and voice input; explain that project files and notes are context the agent always sees.
3. **Files**: open the Files surface, create or edit a note, show restore/history.
4. **Memory**: explain archive → insights → memory proposals, and that nothing becomes durable memory without their approval.
5. **Schedules**: set up one small routine they'd find useful (a weekly review, a daily check).
6. **Settings & extras**: providers and models, package updates, and on macOS the menu bar companion.

Close with: they can ask "what can Ciaobot do?" (or about any specific feature) in any chat, anytime.

## Where the details live

- Workspace customization surface (env vars, workspaces registry, tool deny-lists, model routing): `CIAO_CUSTOMIZATION.md` in the workspace root.
- Schedules how-to: the `ciao-schedules` skill. Spawning chats: the `create-chat` skill. Vault conventions: the `vault-read` skill.
- Canonical docs in the Ciaobot GitHub repo (`raffaelefarinaro/ciaobot`, also present in source checkouts): `README.md`, `docs/ARCHITECTURE.md`, and `PWA_API.md` (routes, auth, agent recipes).
