---
name: ciao-capabilities
description: Authoritative catalog of what Ciaobot can do, for capability questions and feature tours. Use whenever the user asks what Ciaobot is, what it can do, what features are available, whether it can do something specific, or how one of its features works (memory, vault, archiving, schedules, interval automations, routines, workspaces, projects, forks, subagents, skills, voice, models, providers, opencode, plan mode, permission modes, notifications, desktop app, updates, menu bar, files, chat comments, pinned files, document previews, CSV tables and cell comments, HTML artifacts, backlinks, memory map, vault graph, note graph) — and when onboarding or giving a tour or walkthrough to a new user. Trigger on phrasings like "what can you do", "what can ciaobot do", "help me get started", "give me a tour", "can you remind me / remember / schedule", "can you fork this chat", "can you ask another provider", "can you stop asking permission", even when the word "Ciaobot" is not mentioned.
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
- **Subagents**: a chat's agent can dispatch subagents to work in parallel. While a background one runs it appears as a row under its chat in the sidebar, with the agent's description and a live working dot; clicking the row opens a read-only view of that agent's own conversation (you can read it, not steer it — a subagent is a transcript, not a chat you can send into). The row disappears when the agent finishes, and its full transcript stays in the chat's Activity trace, reachable from the same read-only view. Use for "investigate these four things in parallel".
- **Background runs**: when the work is one long script rather than a whole agent task, a chat's agent can start it as a tracked background command instead of a subagent — no model in the loop, no tool access, just the command. It does not block: the turn ends, the output streams into a log file, and when the process exits Ciaobot wakes the chat with the status, exit code, last lines, and the log path (batched, so a group finishing together produces one report). The run can be checked or stopped mid-flight, belongs only to the chat that started it, and is confined to the workspace. Use for "fetch, verify, enrich — one script each". Details: the `background_run_start` / `background_run_status` / `background_run_cancel` MCP tools.
- **Plan mode**: pick **Plan** in the Mode row of the model picker to put the current chat in read-only plan mode, so the agent researches and proposes an approach without editing anything. A `plan` chip appears in the chat header; clicking it leaves plan mode and restores whichever mode the chat was in before, per chat.
- **Turn resilience**: if a turn hits a provider connection error, Ciaobot auto-retries it with backoff instead of dropping the message; you can stop the retry or trigger one immediately. The active chat's live socket also auto-reconnects so a brief network blip doesn't lose the streaming result.

### 2. Memory and the vault (second brain)

- **How memory works, in plain terms.** When a chat is archived, Ciaobot re-reads it and pulls out only what will matter later — a preference, a lesson, a person, a decision — and files each fact where it belongs: things true everywhere go into a small always-loaded memory (the `ciao:memory` / `ciao:profile` regions in `CLAUDE.md`), project facts into that project's doc, people into `People/<Name>.md`, reusable how-to lessons into `Workspace/Learnings.md`. Every remembered fact is dated, and when a fact changes, the new version replaces the old one (the old text is kept in an undo log, never silently lost). A nightly "Memory curation" routine tidies all of this: it merges duplicates, retires expired facts, re-checks old notes, and rotates logs.
- **What is automatic vs. what is yours.** Automatic: extracting facts from archived chats, filing confident ones, deduplicating, dating, nightly tidying, and search-index upkeep. Yours (by design): any NEW fact bound for the always-loaded memory waits in a review queue — `Workspace/Memory-Proposals.md` — until you approve it, because that memory shapes every future conversation and no unattended run gets to rewrite it. Review from the app's proposals panel or the CLI (`ciao memory-proposals`, `ciao memory-proposal-dismiss <text>`); you can also just tell any chat "remember this" or "forget that". Nothing is ever deleted automatically — aging or unused facts are only *reported* for you (or an attended chat) to decide.
- The vault is standard, open markdown: notes, project folders, `CLAUDE.md` (which also holds the bounded memory regions), a vault `MEMORY.md` (curator notes — separate from the bounded regions), a generated `INDEX.md` and `VOCABULARY.md` from frontmatter and markdown links. It is agent-agnostic and remains useful without Ciaobot.
- Vault tooling: search (`vault_search`) before adding duplicate facts, and refresh the index (`ciao index`) after larger edits. Read-only recall uses the matched `vault_search` snippets as private evidence; do not open full vault notes with generic file-read tools for pure recall, and admit when nothing is found. This is inline system-prompt policy, not a separate skill. For the live list of typed Ciaobot tools (projects, chats, subagents, automations), use the MCP `tools_search` (no query for the full catalog, or a group name to narrow it) rather than `tools/list` — with `mcp_lazy_tools` enabled the latter returns only core tools; the bounded memory regions are edited directly with `Edit` on `CLAUDE.md` — there is no memory CLI or command. Vault-maintenance edits are the `ciao` CLI (`ciao index`, `ciao lint`).
- **Memory Map** (`/memory`): an interactive graph and list view of the whole vault for one workspace — nodes are notes (colored/typed from frontmatter), edges come from `related:`/`relatedTo:` frontmatter and relative markdown links in note bodies. Search, filter by category, jump to the most-connected notes, and shift-click two notes to trace the shortest path between them. Read-only — it visualizes the vault, it does not edit it.

### 3. Automations

- One primitive covers every cadence. An automation dispatches its prompt as a chat turn at a time of day (daily/weekly/monthly/once, timezone-aware), every N minutes, or only when the user clicks Run. Configure from the **Automations page** or directly in chat (the `schedule` MCP tool with `action="preview"`/`"create"`/`"update"` carries the full field semantics in its docstring; `schedule_action` handles pause/resume/run/delete).
- **Two targets, and the choice matters.** Point an automation at a **project** and each run opens a fresh chat there with its own model and provider — right for briefings, reports, and maintenance. Point it at an existing **chat** and every run continues that conversation, inheriting its model and mode — right when continuity between runs is the point ("check my PRs every 10 minutes and tell me what changed").
- **Every N minutes** is `frequency="interval"` with `interval_minutes` (minimum 1). Combined with a chat it is what used to be called a loop, and behaves the same way: a run that comes due while the chat is still working is skipped and retried shortly after, never queued, and the cadence resumes after a restart rather than replaying what it missed. Combined with a project it opens a fresh chat each time.
- **When to create one**: the agent creates automations conversationally when the user asks — this is an agent action, not a PWA-only button. Show a concise draft (cadence, next_run, target workspace, target project/chat) and get confirmation before creating, unless the user already explicitly asked to apply it. Call `schedule` with `action="preview"` first to validate. Ask which workspace/project when it isn't obvious from the request. An automation always belongs to one logical workspace and shows under Automations for that workspace.
- Time-of-day runs that were due while the app was off are caught up on the next launch; each workspace shows how many it missed. Interval runs are not replayed — their cadence just resumes.
- System maintenance schedules ship with the app. **Settings → Automations** lists the background work Ciaobot does on its own — what each automation does, when it runs, and how its last run went — leading with anything that needs attention. Failing automations can be re-run from there; Session insights can be run over every archived chat that is missing them, optionally with a different model when the configured one keeps failing.

### 4. Files

- Create, preview, edit, and **restore** workspace and vault files from the PWA, with history — no terminal needed.
- **In chat**: agent file touches surface as inline cards; open the viewer, pin beside the chat, and add line comments on selections — including while the agent is still working, in which case the comment rides along on your next message. Freshly written `.md`/`.csv` files auto-surface in the pinned panel so you see them without hunting.
- **Drag to attach**: drag a file into the composer to insert an agent-accessible absolute path. On the host, Ciaobot uses the desktop path when the webview exposes it; from a client (or a sandboxed browser), it uploads the file into the active project folder on the host first. Images dropped this way upload as visual attachments.
- **Per-chat drafts**: unsent composer text is cached locally per chat and restored after switching chats or reloading. Sending clears only the active chat's draft.
- **Chat annotations**: select text in any message and attach a comment that rides on your next send. With a selection live you can skip the Comment pill: typing opens the note seeded with that keystroke, pasting opens it with the clipboard text (and attaches pasted images), and Cmd/Ctrl+D opens it and starts dictating. The same shortcuts work on document and CSV-cell selections in the viewer and the pinned panel.
- **Rich previews**: images inline; PDFs in the viewer; `.pptx` slides rendered as PDF (LibreOffice on the server).
- **Interactive HTML artifacts**: ask for a dashboard, chart, annotated diff, timeline, or interactive comparison and Ciaobot writes one self-contained `.html` page that renders live in the panel, with a Preview/Code toggle and version history. The page runs sandboxed with no network access, so it works offline and cannot phone home. Prose stays markdown and tables stay CSV, since those support comments.
- **CSV tables**: `.csv` files render as an editable table in the viewer, and you can attach comments to individual cells (anchored by row and column) the same way you annotate document lines.
- **Backlinks**: the markdown viewer has a Backlinks tab listing other vault notes that link to the open note (`[Note](./Note.md)`) — the incoming half of the link graph.
- **Keyboard shortcuts** work in the browser as well as the desktop app, on whichever modifier is actually free: new chat, dictation, and archive are `Cmd+T` / `Cmd+D` / `Cmd+A` in the app, and `Option+N` / `Option+D` / `Option+A` in the PWA, where the browser has already claimed the Cmd versions for new-tab, bookmark, and select-all. Arrow keys roam the home screen's recent chats and Esc closes the open chat in both. Unmodified `1`–`9` switch to the workspace in that position in the sidebar (inert while you are typing in a field), and `Cmd+S` / `Option+S` shows and hides the sidebar. **Settings → Shortcuts** lists the set with the labels for how you are running it.

### 5. Skills, subagents, and commands (extensibility)

- **Stock skills** ship with the app and are synced into both `.claude/skills/` and `.agents/skills/` (`ciao sync-skills`, runs at startup). A same-named skill in the workspace's `skills/` folder overrides the packaged copy.
- **Visual plans**: ask for a plan, design direction, architecture review, UI flow, or approval artifact and Ciaobot writes a local Markdown plan with an optional self-contained HTML companion, including diagrams drawn as inline SVG. Markdown is the canonical, commentable, editable, restorable plan; HTML is an optional companion that answers a specific review question. Only one file is pinned at a time. Plan mode cannot produce a plan file — the skill explains that and offers an in-chat proposal instead. Routine working docs (notes, analyses) stay with the `workspace-authoring` skill.
- **Custom** skills, subagents, and slash commands are authored in the workspace (`skills/`, `subagents/`, `commands/`) and mirrored automatically.
- **Adding a skill**: place a folder `skills/<name>/SKILL.md` (or validated zip containing one top-level folder with `SKILL.md`) then run `ciao sync-skills`. Workspace git sync carries it to other operators. No GitHub fetch.
- **Skill evolution**: a background routine analyzes usage and proposes skill improvements — as reviewable proposals, never silent edits.

### 6. Models and providers

- Backends: **Claude Code** (Claude subscription or Anthropic API key) and **opencode** (the open-source agent CLI, bring-your-own model provider — this is how you reach anything else, including Ollama, OpenRouter, or a local OpenAI-compatible server: configure it in opencode and its models appear here automatically). A message sent while a turn is still running is buffered and flushed as the next turn when the active one finishes — this works identically across providers.
- **Other model backends**: Ollama, OpenRouter, LM Studio, and other OpenAI-compatible endpoints are reached through **opencode**. Configure the backend in opencode; Ciaobot discovers its connected models automatically and exposes them in the chat, workspace, and routine pickers. Ciaobot does not store or probe those backend credentials itself.
- Per-provider default model and thinking level for new chats (**Settings → Models**) — no cross-provider tier mapping; each provider resolves `opus`/`sonnet`/`haiku`/`fable`-style aliases against its own catalog. Per-chat override in the picker, with Automatic resolving to the chat's own model.
- Per-provider default **permission mode** for new chats (**Settings → Providers**): *manual* asks before every action, *auto* (the default) runs safe reads and edits silently and asks before destructive ones, *bypass* allows everything. Any chat can still be switched individually, and plan mode stays a per-chat choice rather than a default.
- Beyond per-chat routing, one chat can **reach another model without leaving the conversation**: the `/critique` command gives an inline multi-model second opinion, and a handover moves the whole chat to another provider.
- **Voice** is on-device and free — one engine each, with no API key, no per-minute billing, and no engine picker: dictation uses Apple's dictation models and speech uses `AVSpeechSynthesizer`, both through the `ciaobot-native` sidecar bundled in `Ciaobot.app`. It needs a **macOS 26+ host with the desktop app installed**; on Linux, Windows, older macOS, or a package-only install there is no voice, and Settings says so instead of failing when you press record. The constraint is on the *host* only — a phone or iPad talking to a Mac host gets voice, because the PWA uploads the audio to the host to transcribe.
- **Session insights** can use Apple Intelligence as an explicit on-device model, offered whenever the machine supports it (macOS 26+, the app bundle, Apple Intelligence on, model downloaded) — no separate beta opt-in.

### 7. Google Workspace (`gws`)

- Ciaobot integrates with Gmail, Calendar, Drive, Docs, Sheets, Slides, and Tasks through the [`gws` CLI](https://github.com/googleworkspace/cli).
- **Settings → Workspaces**: install `gws`, upload a GCP OAuth `client_secret.json` per profile, and connect Google accounts from the browser (no terminal required). The Google Workspace card (and its ⓘ panel) lives on that tab.
- Separate **personal** and **work** profiles; each workspace picks which profile to use on the same Workspaces tab.
- Stock **`gws-*` skills** ship with the app (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, Forms). Setup details: `gws-shared` skill and the ⓘ panel on the Google Workspace card.

### App and system surface

- **Settings page**: Home (deploy, notifications, appearance, **host & client** multi-device role, workspace health), Providers, Workspaces (including Google Workspace), Models, Context (injected prompt layers), Assets (skill/agent/command inventory plus editable **project MCP servers** and secrets), and Automations.
- **macOS extras**: `Ciaobot.app` provides the main window plus a menu bar with engine status, notification and Start at Login toggles, and a single **Update…** action that updates the engine and the app together and restarts. The Python engine remains a separate LaunchAgent.
- **Local HTTP API**: the app exposes an API an in-chat agent can drive (create chats, subagents, commands) — recipes are in `PWA_API.md` in the Ciaobot GitHub repo (`raffaelefarinaro/ciaobot`); fetch it when you need the raw API surface. For the common cases, the `chat_create` and `schedule_*` MCP tools already carry the working recipes in their own docstrings.

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
- Automations how-to: the `schedule` and `schedule_action` MCP tool docstrings. Spawning chats: the `chat_create` MCP tool. Reaching another model: the `chat_handover` MCP tool and the `/critique` command. Vault read conventions are inline system-prompt policy.
- Canonical docs in the Ciaobot GitHub repo (`raffaelefarinaro/ciaobot`, also present in source checkouts): `README.md`, `docs/ARCHITECTURE.md`, and `PWA_API.md` (routes, auth, agent recipes).
