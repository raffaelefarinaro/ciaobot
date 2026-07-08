# Ciaobot

Ciaobot is an opinionated UI and UX layer for using Claude Code as a personal assistant and second brain. It is a local web app around agentic work: chats, projects, files, schedules, memory, and archived knowledge all live in one interface instead of being scattered across terminal sessions.

## Who it's for

Ciaobot is built for **knowledge work, not software development**. It's where you brainstorm, research, draft, plan, and work through documents with an agent that already knows your context — the day-to-day thinking and writing that normally ends up scattered across chat windows, notes apps, and browser tabs.

- **Built for**: brainstorming, research, writing and editing, planning, and document work — typically drafted as plain markdown in a local vault, then published to Google (Docs, Drive, Sheets) once it's ready to leave your machine.
- **Not built for**: day-to-day coding. There is no code editor, terminal, or repo tooling in the UI — keep using your IDE for that. Ciaobot *runs on* Claude Code, but it points that engine at your knowledge and documents, not your codebase.
- **Native Google Workspace**: Gmail, Calendar, Drive, Docs, Sheets, Slides, and Tasks through Google's official [`gws` CLI](https://github.com/googleworkspace/cli), connected with browser-based OAuth from Settings — no terminal required.

## A personal project, shared

Ciaobot is my personal idea of how an AI assistant should work day to day. I built it for my own use, run it on my own machines, and the defaults reflect that: project-first navigation, a plain-markdown vault as memory, explicit model routing, and self-improvement loops that propose changes instead of applying them blindly.

I'm sharing it because the patterns may be useful to you, and because I'm happy to receive contributions: ideas, bug reports, disagreements with my defaults, and pull requests are all welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## What it does

- Runs Claude Code-backed chats in a PWA with project and workspace navigation.
- Lets you create, preview, edit, and restore workspace files from the UI.
- Lets you schedule project or workspace routines to run when you choose.
- Archives chats into a markdown vault, then extracts session insights and drafts memory proposals for review.
- Keeps durable project context separate from short-lived chat state.
- Connects to Google Workspace — Gmail, Calendar, Drive, Docs, Sheets, Slides, and Tasks — through Google's [`gws` CLI](https://github.com/googleworkspace/cli), with browser-based OAuth from Settings (no terminal required).
- Supports voice transcription, push notifications, model/provider settings, and local package updates from the UI.
- Shows a macOS menu bar icon (`ciao menubar`) with server status and open/restart/logs actions — the Ciaobot face turns scared when the server is down.

### Working in chat

- **Comment on text** — select any passage in a message, add a sidebar comment, and send it with your next prompt so the agent knows exactly what you mean.
- **Inline file previews** — when the agent reads or edits a file, a card appears in the thread; click to open a viewer with history, diff, and restore.
- **Pin documents** — keep a file open beside the chat; add line-level comments in the preview (attached to your next message, like chat comments).
- **Rich previews** — images inline; PDFs in a built-in viewer; PowerPoint (`.pptx`) converted to PDF for display (requires LibreOffice on the machine running Ciaobot).

Select text in any message to drop a comment, which collects in a side panel:

![Selecting text in a chat message shows a Comment action](docs/images/chat-comment-select.png)

![A pending comment in the Comments side panel](docs/images/chat-comment-sidebar.png)

The comment travels with your next message, so the agent has the exact context:

![The comment is attached to the follow-up message](docs/images/chat-comment-attached.png)

Pin a document beside the chat and annotate it the same way:

![A document pinned in a split view next to the chat](docs/images/pinned-file.png)

On first launch, an in-app product tour walks through these flows. Replay it anytime from **Settings → Home → Product tour**.

What it does **not** do automatically: it never promotes memory proposals into your long-term memory files without review, never discards or rewrites an existing notes folder during onboarding, and never locks you into one provider; chats and routines can route through any configured backend.

**Agent-agnostic, standalone vault.** The vault structure, project folders, markdown notes, and configuration files (such as `CLAUDE.md` and `MEMORY.md`) created in your workspace are standard, open files. They work with any other AI agent, IDE assistant, or bare terminal tool even if Ciaobot is not running. Ciaobot is the opinionated local web interface on top.

## Providers

Use the access you already have:

- Claude Code through your Claude subscription or Anthropic API key.
- Ollama Cloud, a local Ollama daemon, or compatible Ollama model routing.
- OpenRouter through an `OPENROUTER_API_KEY`.

The model is project-first: a workspace represents a life area (personal, work, a client), each workspace contains projects, and each project carries files, notes, decisions, and context that Ciaobot injects when you work inside it, so the agent does not rediscover what you are talking about every time.

## Install

Install from [PyPI](https://pypi.org/project/ciaobot/) — the wheel ships with the pre-built PWA (the same wheel is attached to each [GitHub release](https://github.com/raffaelefarinaro/ciaobot/releases/latest)):

**macOS (Homebrew)** — includes the menu bar companion:

```bash
brew install raffaelefarinaro/ciaobot
ciao run
```

**Any platform (pip)** — requires Python 3.12 or newer (use whichever `python3.X` you have, e.g. `brew install python@3.13`):

```bash
python3.13 -m venv ~/.ciaobot-venv
~/.ciaobot-venv/bin/pip install ciaobot
~/.ciaobot-venv/bin/ciao run
```

Then open `http://localhost:8443` and follow the setup wizard. It asks for two things before creating anything:

- **Workspace folder** (default `~/ciaobot`) — one root folder holding your second brain (`memory-vault/`) plus app config and runtime state. Start from scratch and it scaffolds the vault, or point it at an existing notes folder and it adapts it.
- **Model provider** — which of the supported providers to use.

The wizard then writes the config, initializes the workspace as a git repository (with a `.gitignore` that keeps `.env` and runtime state out of commits, so snapshots and sync work from day one), and on macOS installs the LaunchAgents and `Ciaobot.app`.

For scripted or headless setups, `ciao setup --workspace <dir>` skips the wizard and prints the login URL to open. If a setup link ever returns `invalid setup token` (tokens are one-time-use), print a fresh one with `~/.ciaobot-venv/bin/ciao setup-url --workspace <dir>`.

## Quickstart (from source)

A git checkout does not include the built PWA bundle, so build it once before running:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
(cd web && npm ci && npm run build)
ciao setup --workspace ~/ciao-workspace
ciao run
```

`ciao setup` is idempotent: it writes the initial `.env`, seeds the workspace docs and vault, and (on macOS) renders LaunchAgents for the server and the menu bar companion plus a `Ciaobot.app` shortcut that opens the local PWA. The menu bar companion is included automatically on macOS installs (no extra needed). Full setup details and optional Node tooling: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

Optional capabilities (Google Workspace, Apple Intelligence titles, MCP connectors) each have their own setup in [INTEGRATIONS.md](INTEGRATIONS.md).

## Documentation

| Doc | What's in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design: repo and workspace layout, chat pipeline, memory and insights, schedules, providers, frontend, device branches. |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Setup, dev workflow, testing, change guidelines. |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Operator config: env vars, OAuth, MCP connectors, server runtime knobs. |
| [PWA_API.md](PWA_API.md) | API endpoints, auth flow, state paths, agent recipes. |
| [web/README.md](web/README.md) | PWA frontend workflow, iOS Safari gotchas, design tokens. |
| [SECURITY.md](SECURITY.md) | Security policy. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute. |

Naming note: the user-facing product is **Ciaobot**. The CLI is installed as both `ciaobot` and `ciao` (same command); the Python package, import path, and many environment variables are still named `ciao`/`CIAO_*` for compatibility.

## For coding agents

`CLAUDE.md` (loaded every prompt) is the contributor guide; `docs/ARCHITECTURE.md` is the canonical orientation doc, read on demand. After changes that affect layout, capabilities, env vars, endpoints, or commands, dispatch the `doc-updater` agent to keep the docs truthful.

## Built on

Ciaobot is glue around a lot of excellent open tools. It wouldn't exist without them:

**Agent engine & models**

- [Claude Code](https://github.com/anthropics/claude-code) and the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) — the agent runtime every chat and routine runs on.
- [Ollama](https://ollama.com) — cloud and local model routing.
- [OpenRouter](https://openrouter.ai) — additional model backends via an Anthropic-compatible endpoint.
- [OpenAI](https://openai.com) — cloud voice transcription.
- [`mlx-whisper`](https://pypi.org/project/mlx-whisper/) — on-device voice transcription on Apple Silicon (free, offline).

**Integrations & CLIs**

- [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli) — Gmail, Calendar, Drive, Docs, Sheets, Slides, and Tasks.
- [NotebookLM CLI (`notebooklm-py`)](https://pypi.org/project/notebooklm-py/) — Google NotebookLM automation.
- [`opencli`](https://www.npmjs.com/package/@jackwener/opencli) — 50+ website adapters (YouTube, LinkedIn, GitHub, …).
- [`apfel`](https://github.com/Arthur-Ficial/apfel) — local-first chat titles via macOS on-device Apple Intelligence.
- [LibreOffice](https://www.libreoffice.org) — `.pptx` slide rendering.

**Frameworks & libraries**

- [Starlette](https://www.starlette.io) + [Uvicorn](https://www.uvicorn.org) — the server.
- [Vue](https://vuejs.org), [Vite](https://vite.dev), and [Pinia](https://pinia.vuejs.org) — the PWA.
- [Excalidraw](https://excalidraw.com) — in-app diagram previews.
- [Playwright](https://playwright.dev) — headless browser automation.
