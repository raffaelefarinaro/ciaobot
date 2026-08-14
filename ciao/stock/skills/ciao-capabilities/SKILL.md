---
name: ciao-capabilities
description: Authoritative catalog of what Ciaobot can do, for capability questions and feature tours. Use whenever the user asks what Ciaobot is, what it can do, what features are available, whether it can do something specific, or how one of its features works (memory, vault, archiving, schedules, loops, routines, workspaces, projects, forks, delegates, skills, voice, models, providers, custom providers, plan mode, notifications, desktop app, updates, menu bar, files, chat comments, pinned files, document previews, CSV tables and cell comments, HTML artifacts, backlinks) — and when onboarding or giving a tour or walkthrough to a new user. Trigger on phrasings like "what can you do", "what can ciaobot do", "help me get started", "give me a tour", "can you remind me / remember / schedule", "can you fork this chat", "can you ask Codex / another provider", even when the word "Ciaobot" is not mentioned.
---

# Ciaobot Capabilities

You are running inside Ciaobot. The app's feature surface is not otherwise visible from a chat session — the system prompt covers behavior, not features — so answer capability questions from this catalog instead of guessing from generic Claude knowledge. If the running app visibly disagrees with something here (features evolve), trust the app and say so.

## How to answer

- **Specific question** ("can you schedule things?", "where do my archived chats go?") → answer from the relevant section only. Don't recite the whole catalog.
- **Broad question** ("what can you do?") → give the one-paragraph pitch plus the capability areas below in a few lines each, then offer to go deeper on any of them.
- **New user / onboarding** → offer the guided tour below.
- Distinguish the **app** from **you**: this catalog is what the Ciaobot app provides. On top of it you have your normal agent abilities plus whatever skills, subagents, and slash commands are installed in this workspace (`skills/`, `subagents/`, `commands/`, and their `.claude/` / `.agents/skills/` mirrors).

## The one-paragraph pitch

Ciaobot is a local-first UI and UX layer for using Claude Code (and other backends) as a personal assistant and second brain. Chats, projects, files, schedules, memory, and archived knowledge live in one web app instead of being scattered across terminal sessions — and everything durable is plain markdown that works with any other tool even when Ciaobot is not running.

## Capability catalog

### 1. Chats, projects, and workspaces

- Hierarchy: **workspace → project → chat**. A workspace is a life area (personal, work, a client); each workspace holds projects; each project holds chats plus durable context (files, notes, decisions) that Ciaobot injects into every chat inside it — the agent doesn't rediscover what you're working on each time.
- Workspaces can have their own vault root, default model, integration profile, and tool deny-list.
- Each chat can override the workspace's model/provider from the picker.
- **Starting a chat from the home screen**: each workspace lane has a **+ new** button that starts a chat in that workspace's *General* project in one click. When the workspace has more than one project, a caret sits beside it that opens a project picker, so a new chat can go straight into the right project without creating it in General and moving it.
- Voice transcription for chat input; push notifications; the PWA is installable on desktop and mobile.
- Chats can be spawned programmatically from within a chat via the `chat_create` MCP tool.
- **Forks**: any completed agent answer has a *Fork conversation from here* action. It creates an independent new chat in the same project, copies the visible history through that answer, and inherits the source's provider/model/mode/thinking level — but starts a fresh provider session and never syncs back to the source. Forks are titled `Original · Fork 1`, `Original · Fork 2`. Use it to explore a branch without disturbing the original.
- **Delegates**: a chat's agent can spawn delegate chats to do real work in parallel — each a normal, resumable chat with full tool access (edit, bash, git) on a model the supervisor picks. They do not block: the supervising agent's turn ends, and when a delegate finishes, Ciaobot wakes the supervisor with a fresh turn summarizing the result (batched, so four finishing together produce one report). Open a delegate to watch it, or stop it, like any chat. A delegate cannot spawn delegates, and six run at once per chat. Use for "fix these four issues in parallel". Details: the `delegate_spawn` / `delegates_list` MCP tools.
- **Background runs**: when the work is one long script rather than a whole agent task, a chat's agent can start it as a tracked background command instead of a delegate — no model in the loop, no tool access, just the command. It does not block: the turn ends, the output streams into a log file, and when the process exits Ciaobot wakes the chat with the status, exit code, last lines, and the log path (batched the same way delegate reports are). The run can be checked or stopped mid-flight, belongs only to the chat that started it, and is confined to the workspace. Use for "fetch, verify, enrich — one script each". Details: the `background_run_start` / `background_run_status` / `background_run_cancel` MCP tools.
- **Plan mode**: `/plan` toggles the current chat between normal and read-only plan mode, so the agent researches and proposes an approach without editing anything. Toggling back restores whichever mode the chat was in before, per chat.
- **Turn resilience**: if a turn hits a provider connection error, Ciaobot auto-retries it with backoff instead of dropping the message; you can stop the retry or trigger one immediately. The active chat's live socket also auto-reconnects so a brief network blip doesn't lose the streaming result.

### 2. Memory and the vault (second brain)

- Chats are **archived into a markdown vault** (e.g. `memory-vault/Logs/Chats/`). From archived sessions Ciaobot extracts insights and drafts **memory proposals** — the user reviews and approves them before anything is promoted into the bounded `ciao:memory` (env/conventions/lessons) and `ciao:profile` (identity, preferences) regions fenced inside the workspace `CLAUDE.md`. Accept/reject via the `memory_proposal_resolve` MCP tool. Nothing is memorized silently.
- The vault is standard, open markdown: notes, project folders, `CLAUDE.md` (which also holds the bounded memory regions), a vault `MEMORY.md` (curator notes — separate from the bounded regions), a generated `INDEX.md` from frontmatter and wikilinks. It is agent-agnostic and remains useful without Ciaobot.
- Vault tooling: search (`vault_search`) before adding duplicate facts, and refresh the index (`ciao index`) after larger edits. Read-only recall (search, read matches, admit when nothing is found) is inline system-prompt policy, not a separate skill. For the live list of typed Ciaobot tools (projects, chats, delegates, schedules, loops), read the MCP `tools/list` rather than reciting a static tool list; the bounded memory regions are edited directly with `Edit` on `CLAUDE.md` — there is no memory CLI or command. Vault-maintenance edits are the `ciao` CLI (`ciao index`, `ciao lint`).

### 3. Schedules, loops, and automations

- A native scheduler dispatches recurring or one-off prompts as fresh chat turns into a target project or chat — daily/weekly/monthly/once, timezone-aware. Configure from the **Automations page** or directly in chat (the `schedule_*`/`loop_*` MCP tools carry the full field semantics in their own docstrings).
- **Loops** are the sub-day sibling of schedules: bound to one existing chat, they re-send the same prompt every N minutes (e.g. "check my PRs every 10 minutes"), keeping the conversation's context between iterations. A loop runs with the chat's own model; loops set to start with the server resume on boot, the rest are started manually. Managed from the same Automations page.
- Schedules that were due while the app was off are caught up on the next launch; each workspace shows how many runs it missed.
- System maintenance schedules ship with the app. **Settings → Automations** lists the background work Ciaobot does on its own — what each automation does, when it runs, and how its last run went — leading with anything that needs attention. Failing automations can be re-run from there; Session insights can be run over every archived chat that is missing them, optionally with a different model when the configured one keeps failing.

### 4. Files

- Create, preview, edit, and **restore** workspace and vault files from the PWA, with history — no terminal needed.
- **In chat**: agent file touches surface as inline cards; open the viewer, pin beside the chat, and add line comments on selections — including while the agent is still working, in which case the comment rides along on your next message. Freshly written `.md`/`.csv` files auto-surface in the pinned panel so you see them without hunting.
- **Drag to attach**: drag a file into the composer to insert an agent-accessible absolute path. On the host, Ciaobot uses the desktop path when the webview exposes it; from a client (or a sandboxed browser), it uploads the file into the active project folder on the host first. Images dropped this way upload as visual attachments.
- **Per-chat drafts**: unsent composer text is cached locally per chat and restored after switching chats or reloading. Sending clears only the active chat's draft.
- **Chat annotations**: select text in any message and attach a comment that rides on your next send.
- **Rich previews**: images inline; PDFs in the viewer; `.pptx` slides rendered as PDF (LibreOffice on the server).
- **Interactive HTML artifacts**: ask for a dashboard, chart, annotated diff, timeline, or interactive comparison and Ciaobot writes one self-contained `.html` page that renders live in the panel, with a Preview/Code toggle and version history. The page runs sandboxed with no network access, so it works offline and cannot phone home. Prose stays markdown and tables stay CSV, since those support comments.
- **CSV tables**: `.csv` files render as an editable table in the viewer, and you can attach comments to individual cells (anchored by row and column) the same way you annotate document lines.
- **Backlinks**: the markdown viewer has a Backlinks tab listing other vault notes that link to the open note via wikilinks (`[[Note]]`) — the incoming half of the wikilink graph.
- **Keyboard shortcuts** work in the browser as well as the desktop app, on whichever modifier is actually free: new chat, dictation, and archive are `Cmd+T` / `Cmd+D` / `Cmd+A` in the app, and `Option+N` / `Option+D` / `Option+A` in the PWA, where the browser has already claimed the Cmd versions for new-tab, bookmark, and select-all. Arrow keys roam the home screen's recent chats and Esc closes the open chat in both. Unmodified `1`–`9` switch to the workspace in that position in the sidebar (inert while you are typing in a field), and `Cmd+S` / `Option+S` shows and hides the sidebar. **Settings → Shortcuts** lists the set with the labels for how you are running it.

### 5. Skills, subagents, and commands (extensibility)

- **Stock skills** ship with the app and are synced into both `.claude/skills/` and `.agents/skills/` (`ciao sync-skills`, runs at startup). A same-named skill in the workspace's `skills/` folder overrides the packaged copy.
- **Custom** skills, subagents, and slash commands are authored in the workspace (`skills/`, `subagents/`, `commands/`) and mirrored automatically.
- **GitHub-sourced skills** can be installed and are refreshed automatically on restart when upstream changes.
- **Skill evolution**: a background loop analyzes usage and proposes skill improvements — as reviewable proposals, never silent edits.

### 6. Models and providers

- Backends: **Claude Code** (Claude subscription or Anthropic API key), **Codex** (OpenAI ChatGPT subscription via the Codex CLI), **Ollama** (cloud or local daemon, routed through Claude Code), and **OpenRouter** (routed through Claude Code). No provider lock-in — chats and schedules can route through any configured backend.
- **Custom providers**: any OpenAI- or Anthropic-compatible endpoint can be added by the user in **Settings → Providers** — give it a name, a URL, a token, and whether it runs through the Claude or Codex runner. Ciaobot can probe the endpoint to discover its model ids, and those models then appear in the normal chat, workspace, and routine model pickers alongside the built-in backends. Tokens are stored separately in the gitignored runtime directory and are never returned by the API; only whether a token is set is exposed.
- Per-workspace default model and model bucket (which controls how aliases like `opus`/`sonnet` resolve), per-chat override in the picker.
- Beyond per-chat routing, one chat can **reach another model without leaving the conversation**: `adversarial_review` for an inline multi-model second opinion, or a delegate (see §1) for writable work on a different model.
- **Voice** is on-device and free — one engine each, with no API key, no per-minute billing, and no engine picker: dictation uses Apple's dictation models and speech uses `AVSpeechSynthesizer`, both through the `ciaobot-native` sidecar bundled in `Ciaobot.app`. It needs a **macOS 26+ host with the desktop app installed**; on Linux, Windows, older macOS, or a package-only install there is no voice, and Settings says so instead of failing when you press record. The constraint is on the *host* only — a phone or iPad talking to a Mac host gets voice, because the PWA uploads the audio to the host to transcribe.
- **Session insights** can use Apple Intelligence as an explicit on-device model. Settings can re-run the text-only extraction against a small sample of already-processed archives for a read-only comparison; archives are not modified by that test.

### 7. Google Workspace (`gws`)

- Ciaobot integrates with Gmail, Calendar, Drive, Docs, Sheets, Slides, and Tasks through the [`gws` CLI](https://github.com/googleworkspace/cli).
- **Settings → Workspaces**: install `gws`, upload a GCP OAuth `client_secret.json` per profile, and connect Google accounts from the browser (no terminal required). The Google Workspace card (and its ⓘ panel) lives on that tab.
- Separate **personal** and **work** profiles; each workspace picks which profile to use on the same Workspaces tab.
- Stock **`gws-*` skills** ship with the app (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, Forms). Setup details: `gws-shared` skill and the ⓘ panel on the Google Workspace card.

### App and system surface

- **Settings page**: Home (deploy, notifications, appearance, **host & client** multi-device role, workspace health), Providers, Workspaces (including Google Workspace), Models, Context (injected prompt layers), Assets (skill/agent/command inventory plus editable **project MCP servers** and secrets), and Automations.
- **macOS extras**: `Ciaobot.app` provides the main window plus a menu bar with engine status, notification and Start at Login toggles, and a single **Update…** action that updates the engine and the app together and restarts. The Python engine remains a separate LaunchAgent.
- **Local HTTP API**: the app exposes an API an in-chat agent can drive (create chats, subagents, commands) — recipes are in `PWA_API.md` in the Ciaobot GitHub repo (`raffaelefarinaro/ciaobot`); fetch it when you need the raw API surface. For the common cases, the `chat_create` and `schedule_*`/`loop_*` MCP tools already carry the working recipes in their own docstrings.

### Privacy and trust posture

Local-first: the server, vault, and runtime state live on the user's machine; traffic leaves only toward the configured model providers. Memory is opt-in via reviewed proposals, an existing notes folder is never discarded or rewritten during onboarding, and the vault stays portable plain markdown.

## Guided tour (new users)

When onboarding someone, walk through these hands-on:

1. **Orient** — workspaces → projects → chats; create or rename a project for something they're working on.
2. **Chat** — model picker, voice input, and project context the agent always sees.
3. **Annotate & files** — message comments, inline file cards, pin, line comments, and rich previews.
4. **Memory** — archive → insights → memory proposals; nothing becomes durable without approval.
5. **Schedules** — set up one small routine they'd actually use.
6. **Settings** — providers/models, package updates, and on macOS the menu bar companion.

Close with: they can ask "what can Ciaobot do?" (or about any specific feature) in any chat, anytime.

## Where the details live

- Workspace customization surface (env vars, workspaces registry, tool deny-lists, model routing): `CIAO_CUSTOMIZATION.md` in the workspace root.
- Schedules and loops how-to: the `schedule_*`/`loop_*` MCP tool docstrings. Spawning chats: the `chat_create` MCP tool. Reaching another model: the `delegate_spawn` and `adversarial_review` MCP tools. Vault read conventions are inline system-prompt policy.
- Canonical docs in the Ciaobot GitHub repo (`raffaelefarinaro/ciaobot`, also present in source checkouts): `README.md`, `docs/ARCHITECTURE.md`, and `PWA_API.md` (routes, auth, agent recipes).
