# Ciaobot

<p align="center">
  <img src="docs/hero.png" alt="Ciaobot mascot saying Ciao!" width="100%">
</p>

Ciaobot is your **second brain and personal assistant**. It is built on top of the CLIs you already use, such as [Claude Code](https://github.com/anthropics/claude-code) and [opencode](https://opencode.ai), and gives you one interface for working with them.

You are not tied to one CLI, model, or provider. Use Claude, opencode with a cloud or local model, or switch between them as needed. Ciaobot keeps the interface and the context consistent while your work, chats, and memory remain in a folder you own and can reuse with any CLI in the future.

## Install

**macOS 13+ on Apple Silicon (M1 or newer):**

```bash
curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh
```

Open `http://localhost:8443` and follow the setup wizard. It will help you choose or create your workspace, set a dashboard password, and connect the provider you want to use.

The workspace is your own folder. It contains your plain-Markdown vault, projects, chats, and configuration. You can keep it under version control or sync it with the storage workflow you prefer.

Contributors running from a git checkout can follow [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

![Ciaobot PWA overview](docs/screenshots/pwa-overview.png)

## How it works

The basic model is **workspace → project → chat**:

- A **workspace** is a separate area such as personal, work, or a client. Each workspace has its own projects and memory, so a "personal" workspace never pollutes a "work" workspace.
- A **project** groups related work. Its frontmatter `description:` is included as project context in every turn, so you do not need to explain to the model which project you are working in every time.
- A **chat** is where the work happens. When you finish, archive it and Ciaobot extracts useful insights and routes them into your memory or project documents.

You can do everything from a chat. Ask Ciaobot to create a project, move a chat into a project, archive a conversation, find a file, update your memory, or set up a recurring prompt. The interface is there to make those CLI-powered workflows easy to use and keep their context together.

## Your second brain

Memory is stored as ordinary Markdown in your workspace. You can read it in Ciaobot, Obsidian, a text editor, Claude Code, opencode, or any other tool that works with files.

When you archive a conversation, Ciaobot extracts decisions, useful learnings, and other durable facts. Confident facts are filed automatically; uncertain ones become proposals for you to review. This lets your second brain grow from the work you actually do without making the vault a proprietary database.

## Features

- **Workspaces and projects:** Keep personal, work, and client contexts separate; group chats around projects; switch context from the sidebar.
- **Chats and agents:** Continue conversations, fork them, run prompts on a schedule, and follow the subagents working in the background.
- **Files and documents:** Browse and edit workspace files, inspect outputs and history, pin documents, and comment on selected passages before sending your next prompt.
- **Memory and archiving:** Archive completed chats, extract insights, review proposals, and maintain a searchable Markdown vault.
- **Automations:** Run recurring or one-off prompts and see their status in Settings.
- **Providers:** Use Claude Code or opencode with the model access you already have, including local models exposed through opencode.
- **Remote access:** Use the PWA from another device over a private network such as [Tailscale](https://tailscale.com/).

## Documentation

| Doc | What's in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, workspace layout, chat pipeline, memory, schedules, and providers. |
| [docs/MCP.md](docs/MCP.md) | MCP architecture, security, tool catalog, and provider configuration. |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Git checkout, development workflow, testing, and change guidelines. |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Environment variables, OAuth, and integration configuration. |
| [PWA_API.md](PWA_API.md) | API endpoints, authentication, state paths, and agent recipes. |
| [web/README.md](web/README.md) | PWA development workflow and frontend details. |
| [SECURITY.md](SECURITY.md) | Security policy. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guidelines. |
| [docs/CREDITS.md](docs/CREDITS.md) | Open tools Ciaobot is built on. |

The user-facing product is **Ciaobot**. The CLI is installed as both `ciaobot` and `ciao`; the Python package and many environment variables still use `ciao`/`CIAO_*` for compatibility.

## Built on

Ciaobot is glue around excellent open tools: Claude Code, the Claude Agent SDK, opencode, Starlette, Vue, and more. See [docs/CREDITS.md](docs/CREDITS.md) for the full list.
