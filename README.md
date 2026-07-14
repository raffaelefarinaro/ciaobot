# Ciaobot

Ciaobot is a local web app for knowledge work with subscription-backed agents (Claude Code, OpenAI Codex, and others). Chats, projects, files, schedules, memory, and archived knowledge live in one interface instead of being scattered across terminal sessions — with a plain-markdown vault you own.

## Who it's for

Ciaobot is built for **knowledge work, not software development**: brainstorming, research, writing and editing, planning, and document work — typically drafted as markdown in a local vault, then published to Google (Docs, Drive, Sheets) when ready.

- **Not built for** day-to-day coding. There is no code editor or repo tooling in the UI — keep using your IDE for that.
- **Google Workspace** — Gmail, Calendar, Drive, Docs, Sheets, Slides, and Tasks through Google's [`gws` CLI](https://github.com/googleworkspace/cli), connected with browser-based OAuth from Settings.

## The idea

Ciaobot does not reinvent how you talk to agents. It runs [Claude Code](https://github.com/anthropics/claude-code) or [OpenAI Codex](https://developers.openai.com/codex/cli/) in the background, on the bet that the vendors' own CLIs are the best-maintained agent harnesses available — they keep the model communication, tool use, and agentic loop optimized so this project doesn't have to. Ciaobot stays in control of the three things that matter to me:

1. **The context** — deciding exactly what memory, notes, and project state the agent is fed each turn.
2. **One interface** — the same UI regardless of which project or provider you're talking to.
3. **Incremental capabilities** — features are added only when I need them or discover a pattern worth adopting, not speculatively.

What that looks like in practice:

- **Workspaces and projects** — split life areas (personal, work, a client, …) into sidebar workspaces, then organize work inside projects. Ciaobot injects project notes and context into every turn.
- **A vault you own** — durable knowledge as plain markdown with wikilinks and an `INDEX.md`, inspired by [Andrej Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). Browse in [Obsidian](https://obsidian.md/) or any editor; sync via GitHub, Drive, or iCloud.
- **Skills, subagents, and commands** — packaged defaults, extensible from Settings or workspace files (see [What ships by default](#what-ships-by-default)).
- **Files and automations** — create, preview, edit, and restore vault files from the UI; run recurring routines on a cron you choose (schedules) or re-run a prompt inside one chat every N minutes (loops).
- **Voice, notifications, and updates** — transcription, push alerts, model settings, and in-app package updates. On macOS: menu bar companion, `Ciaobot Server.app`, and background service after setup.
- **Provider choice** — Claude Code or Codex with your existing login; Ollama, OpenRouter, and on-device models for lighter tasks (see [Providers](#providers)).

Pick a workspace folder, choose a provider, and work — Ciaobot is the interface on top; the vault is yours to keep.

## Memory and the vault

Ciaobot keeps memory in layers so the agent can recall what matters without stuffing every prompt. **Settings → Context** shows what the agent actually loads.

- **Short agent memory** (`~/.ciao/memory.md` and `user.md`) — a small, capped scratchpad the model maintains for you: preferences, conventions, lessons. Updated during conversation or via `/remember`; a snapshot is injected at the start of each chat.
- **Your vault** (`memory-vault/`, or a separate vault root per sidebar workspace) — durable markdown you own: people, projects, ideas. Browse it in Obsidian or any editor; it stays useful even without Ciaobot.
- **One behavior file for the install** — `<workspace>/CLAUDE.md` (and `AGENTS.md` for Codex) applies to every chat.

When your message mentions a name that appears in the vault index, the agent gets a quiet hint — “this probably means `People/Emma`” — so it opens the right note without you repeating context. And when a chat is archived, a pipeline turns it into durable knowledge: session insights are extracted, memory proposals are drafted, and daily/weekly curation runs update vault pages — but nothing is promoted into long-term memory without review, and Ciaobot never discards or rewrites an existing notes folder during onboarding. Track the background steps under **Settings → Automation**, and see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full pipeline.

## Features

**Chats, projects, and workspaces**

- Sidebar workspaces per life area (personal, work, a client) — each with its own vault, projects, and default model.
- Projects group related chats and inject durable notes and context into every turn.
- Comment on any passage of a reply — select text, attach a note, and it rides along with your next prompt; queue follow-ups while the agent is still working.
- Per-chat model picker with provider thinking levels on top of per-workspace defaults.

**Voice — dictation and read-aloud**

- Speech-to-text dictation in any chat: cloud transcription or free on-device via [mlx-whisper](https://pypi.org/project/mlx-whisper/) (Apple Silicon).
- Text-to-speech read-aloud of replies: cloud voices or free on-device via [Kokoro](https://pypi.org/project/kokoro-onnx/); local models download on first use and are re-installed automatically after app upgrades.

**Files and documents**

- Agent file touches surface as inline cards in the thread; click one for a viewer with history, diff, and restore.
- Pin a document beside the chat and add line-level comments on the preview (attached to your next message, like chat comments).
- Rich previews: images inline, PDFs in a built-in viewer, PowerPoint (`.pptx`) converted to PDF for display (requires LibreOffice on the machine running Ciaobot).
- Create, edit, and restore vault files from the UI, with snapshots behind every agent edit.

**Memory, vault, and insight extraction**

- Layered memory: a capped agent memory and user profile injected at chat start, plus a plain-markdown vault you own (Obsidian-compatible, syncable via GitHub/Drive/iCloud).
- Archiving a chat runs an extraction pipeline: session insights and trajectories are captured, memory proposals are drafted, and project canonical docs are updated — nothing is promoted into long-term memory without review.
- Daily and weekly curation routines keep vault pages and `Workspace/Learnings.md` current.
- Vault-index hints: mention a name the index knows and the agent is quietly pointed at the right note.

**Automations**

- Schedules: recurring or one-off cron routines that dispatch fresh prompts into a project or chat.
- Loops: re-run a prompt inside one chat every N minutes, keeping the conversation's context between iterations.
- System routines ship enabled (memory curation, skill evolution, weekly self-improvement review); every background run is visible under **Settings → Automation**.

**Extensibility — skills, subagents, commands**

- Stock skills, subagents, and slash commands ship with the app; same-named workspace versions override them.
- Install skills from GitHub repositories; they refresh automatically on restart.
- A weekly skill-evolution routine proposes improvements from real usage — reviewable proposals, never silent edits.

**Providers and models**

- Claude Code or OpenAI Codex with the subscription login you already have; Ollama (cloud or local) and OpenRouter as API backends.
- haiku/sonnet/opus tier routing mapped across providers; background tasks (titles, insights) routable to cheaper or on-device models ([apfel](https://github.com/Arthur-Ficial/apfel)).

**Google Workspace**

- Gmail, Calendar, Drive, Docs, Sheets, Slides, and Tasks through Google's `gws` CLI, connected with browser OAuth from Settings — no terminal required.

**App surface**

- Installable PWA with web-push notifications and in-app package updates.
- macOS extras: menu bar companion, `Ciaobot Server.app`, and a background service that starts on login.
- First-run product tour plus a getting-started checklist whose steps deep-link into the real pages.
- A local HTTP API an in-chat agent can drive (create chats, subagents, commands — see [PWA_API.md](PWA_API.md)).

On first launch, an in-app product tour walks through the core flows. Replay it anytime from **Settings → Home → Product tour**.

## What ships by default

Every install seeds a set of subagents, slash commands, and system routines from the package (`ciao/stock/`); your own workspace versions with the same name take precedence.

### Subagents

Specialized roles the main agent can delegate to ([ciao/stock/agents/](ciao/stock/agents/)):

| Subagent | What it does |
|---|---|
| [memory](ciao/stock/agents/memory.md) | Vault curation, durable note updates, and memory-proposal processing. |
| [researcher](ciao/stock/agents/researcher.md) | Researches current external information and summarizes it with sources. |
| [secretary](ciao/stock/agents/secretary.md) | Calendar, email, reminders, and lightweight admin via the Google Workspace skills; asks before sending anything. |

### Slash commands

Type these in any chat ([ciao/stock/commands/](ciao/stock/commands/)):

| Command | What it does |
|---|---|
| [/remember](ciao/stock/commands/remember.md) | Saves a durable fact or learning to the right memory layer (agent memory, user profile, or a vault page). |
| [/interrogation](ciao/stock/commands/interrogation.md) | Asks a few targeted questions to turn a vague project, person, or idea into a useful canonical vault note. |
| [/critique](ciao/stock/commands/critique.md) | Quick single-model review of a plan or draft (the multi-model `adversarial-review` skill is the heavier option). |

### System routines

Recurring schedules that ship enabled ([ciao/stock/schedules.json](ciao/stock/schedules.json)); they run through the same provider pipeline as a chat turn, and their runs are visible under **Settings → Automation**:

| Routine | Cadence | What it does |
|---|---|---|
| Memory curation | Daily | Reviews recent archived chats, memory proposals, and learnings; updates vault pages and `Workspace/Learnings.md`. |
| Skill evolution | Weekly (Sun) | Drafts skill-improvement proposals from recent usage; never applies them automatically. |
| Weekly self-improvement review | Weekly (Sun) | Runs the [weekly review checklist](ciao/stock/schedules/weekly-review-template.md): promote recurring learnings, lint the vault, reconcile contradictions. |

Your own schedules live alongside these in the workspace (`.runtime/schedules.json`), with in-chat loops in `.runtime/loops.json`; both are managed from the UI's Automations page. Packaged **skills** (vault search, Google Workspace, web research, and more) are browsable under **Settings → Skills** and live in [ciao/stock/skills/](ciao/stock/skills/).

## Install

**macOS ([Homebrew](https://brew.sh))** — recommended; includes `Ciaobot Server.app` and the background service:

```bash
brew install raffaelefarinaro/ciaobot/ciaobot
ciao run
```

**Any platform ([PyPI](https://pypi.org/project/ciaobot/))** — or macOS without Homebrew; requires Python 3.12 or newer:

```bash
python3.13 -m venv ~/.ciaobot-venv
~/.ciaobot-venv/bin/pip install ciaobot
~/.ciaobot-venv/bin/ciao run
```

Then open `http://localhost:8443` and follow the setup wizard:

- **Workspace folder** (default `~/ciaobot`) — your second brain (`memory-vault/`) plus app config and runtime state. Sync this folder (GitHub, Drive, iCloud, …) so your vault follows you across machines.
- **Model provider** — Claude Code, Codex, or another configured backend.

The wizard writes config, initializes the workspace as a git repo (with a `.gitignore` for secrets and runtime state), and on macOS installs LaunchAgents and `Ciaobot Server.app`.

For scripted setups: `ciao setup --workspace <dir>`. If a setup link returns `invalid setup token`, mint a fresh one with `ciao setup-url --workspace <dir>`.

Contributors running from a git checkout: see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Providers

Use the access you already have:

- **Claude Code** — CLI-managed Claude subscription or Anthropic Console authentication.
- **OpenAI Codex** — `codex login`, including eligible ChatGPT subscription accounts.
- **Ollama** — cloud or local daemon.
- **OpenRouter** — `OPENROUTER_API_KEY`.
- **On-device models** — for lightweight tasks where available: titles via [apfel](https://github.com/Arthur-Ficial/apfel), speech via [mlx-whisper](https://pypi.org/project/mlx-whisper/), and similar.

See [INTEGRATIONS.md](INTEGRATIONS.md) for env vars, OAuth, and per-task model routing (titles, insights, voice).

## A personal project, shared

Ciaobot is my personal idea of how an AI assistant should work day to day. I built it for my own use, run it on my own machines, and the defaults reflect that: project-first navigation, a plain-markdown vault as memory, explicit model routing, and self-improvement loops that propose changes instead of applying them blindly.

I'm sharing it because the patterns may be useful to you. Ideas, bug reports, disagreements with my defaults, and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

| Doc | What's in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design: repo and workspace layout, chat pipeline, memory, schedules, providers. |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Git checkout, dev workflow, testing, change guidelines. |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Env vars, OAuth, MCP connectors, server runtime knobs. |
| [PWA_API.md](PWA_API.md) | API endpoints, auth flow, state paths, agent recipes. |
| [web/README.md](web/README.md) | PWA frontend workflow, iOS Safari gotchas, design tokens. |
| [SECURITY.md](SECURITY.md) | Security policy. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute. |
| [docs/CREDITS.md](docs/CREDITS.md) | Open tools Ciaobot is built on. |

Naming note: the user-facing product is **Ciaobot**. The CLI is installed as both `ciaobot` and `ciao` (same command); the Python package, import path, and many environment variables are still named `ciao`/`CIAO_*` for compatibility.

## Why "Ciao"?

*Ciao* isn't just Italian for "hi" and "bye" — it comes from the Venetian phrase *s-ciào vostro* ("[I am] your slave"), a servile greeting that shed its literal meaning over the centuries and became the everyday word Italians use today. Fitting for an assistant: yours to command. See the [etymology on Wikipedia](https://en.wikipedia.org/wiki/Ciao#Etymology).

## Built on

Ciaobot is glue around a lot of excellent open tools — Claude Code, the Claude Agent SDK, Codex CLI, Starlette, Vue, and more. See [docs/CREDITS.md](docs/CREDITS.md) for the full list.
