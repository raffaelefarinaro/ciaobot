# Ciaobot

Ciaobot is an opinionated UI and UX layer for using Claude Code as a personal assistant and second brain. It is a local web app around agentic work: chats, projects, files, schedules, memory, and archived knowledge all live in one interface instead of being scattered across terminal sessions.

## A personal project, shared

Ciaobot is my personal idea of how an AI assistant should work day to day. I built it for my own use, run it on my own machines, and the defaults reflect that: project-first navigation, a plain-markdown vault as memory, explicit model routing, and self-improvement loops that propose changes instead of applying them blindly.

I'm sharing it because the patterns may be useful to you, and because I'm happy to receive contributions: ideas, bug reports, disagreements with my defaults, and pull requests are all welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## What it does

- Runs Claude Code-backed chats in a PWA with project and workspace navigation.
- Lets you create, preview, edit, and restore workspace files from the UI.
- Lets you schedule project or workspace routines to run when you choose.
- Archives chats into a markdown vault, then extracts session insights and drafts memory proposals for review.
- Keeps durable project context separate from short-lived chat state.
- Supports voice transcription, push notifications, model/provider settings, and local package updates from the UI.
- Shows a macOS menu bar icon (`ciao menubar`) with server status and open/restart/logs actions — the Ciaobot face turns scared when the server is down.

What it does **not** do automatically: it never promotes memory proposals into your long-term memory files without review, never discards or rewrites an existing notes folder during onboarding, and never locks you into one provider; chats and routines can route through any configured backend.

**Agent-agnostic, standalone vault.** The vault structure, project folders, markdown notes, and configuration files (such as `CLAUDE.md` and `MEMORY.md`) created in your workspace are standard, open files. They work with any other AI agent, IDE assistant, or bare terminal tool even if Ciaobot is not running. Ciaobot is the opinionated local web interface on top.

## Providers

Use the access you already have:

- Claude Code through your Claude subscription or Anthropic API key.
- Ollama Cloud, a local Ollama daemon, or compatible Ollama model routing.
- OpenRouter through an `OPENROUTER_API_KEY`.

The model is project-first: a workspace represents a life area (personal, work, a client), each workspace contains projects, and each project carries files, notes, decisions, and context that Ciaobot injects when you work inside it, so the agent does not rediscover what you are talking about every time.

## Install

Install from the [latest release](https://github.com/raffaelefarinaro/ciaobot/releases/latest) — the wheel ships with the pre-built PWA:

Requires Python 3.12 or newer (use whichever `python3.X` you have, e.g. `brew install python@3.13`):

```bash
python3.13 -m venv ~/.ciaobot-venv
~/.ciaobot-venv/bin/pip install https://github.com/raffaelefarinaro/ciaobot/releases/download/v0.2.1/ciao-0.2.1-py3-none-any.whl
~/.ciaobot-venv/bin/ciao setup --workspace ~/ciao-workspace
~/.ciaobot-venv/bin/ciao run
```

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

`ciao setup` is idempotent: it writes the initial `.env`, seeds the workspace docs and vault, and (on macOS) renders LaunchAgents for the server and the menu bar companion plus a `Ciaobot.app` shortcut that opens the local PWA. The menu bar icon needs the optional extra (`pip install 'ciao[menubar]'`). Full setup details and optional Node tooling: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

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

Naming note: the user-facing product is **Ciaobot**. The Python package, import path, CLI command, and many environment variables are still named `ciao`/`CIAO_*` for compatibility.

## For coding agents

`CLAUDE.md` (loaded every prompt) is the contributor guide; `docs/ARCHITECTURE.md` is the canonical orientation doc, read on demand. After changes that affect layout, capabilities, env vars, endpoints, or commands, dispatch the `doc-updater` agent to keep the docs truthful.
