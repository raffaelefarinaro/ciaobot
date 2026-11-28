# Ciaobot

<p align="center">
  <img src="site/assets/img/hero.png" alt="Ciaobot" width="100%">
</p>

Ciaobot is your **second brain and personal assistant**: a persistent personal workspace for the agents you already use, [Claude Code](https://github.com/anthropics/claude-code), [Codex](https://github.com/openai/codex) and [OpenCode](https://opencode.ai).

**Your agents do the work. Ciaobot remembers.** Claude Code, Codex and OpenCode handle models and tools; Ciaobot adds projects, durable memory, files, and recurring tasks so each conversation can build on what came before.

**Change your AI, not your second brain.** Use Claude, Codex or OpenCode with a cloud or local model. Your notes, project context, and remembered preferences stay in a folder you own. Durable Markdown knowledge remains useful outside Ciaobot; provider sessions, tools, and permissions are not interchangeable.

## Install

**macOS 14+ on Apple Silicon (M1 or newer):** download the [Ciaobot app](https://github.com/raffaelefarinaro/ciaobot/releases/latest) (DMG). The app includes the engine: open it and follow its first-use steps, and on a machine updating from an older install Home shows where to get it. Your workspace defaults to `~/Ciaobot`, and Ciaobot uses Claude Code as the AI tool unless you change it.

**Linux (Ubuntu 24.04 or similar):** run this as the account that will own Ciaobot. It installs the engine in your account with uv and asks once for `sudo` to install and start the systemd service, then prints the one-time login link for opening Ciaobot in a browser.

```bash
curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh
```

The first-run setup in your browser lets you use a notes folder you already have: tick **Use a separate existing notes folder** and enter it in **Notes folder on the Ciaobot computer**. Ciaobot's own data stays in the workspace folder.

Your notes stay yours, in plain Markdown, usable with any other tool. Keep the notes and workspace folders out of iCloud Drive, Dropbox, Google Drive and OneDrive: sync conflicts damage files and Git history. Use Ciaobot's [private Git backup](https://www.raffaelefarinaro.com/ciaobot/memory.html#backup) instead.

The first time you open Ciaobot, Home shows your workspace folder and the password on the computer running it (the password is also `PWA_AUTH_TOKEN` in the folder's `.env`). Change the AI tool in **Settings → Overview** and the password in **Settings → General**.

Ciaobot does not run a model of its own. It drives the Claude Code, OpenCode or Codex CLI already installed on the computer running Ciaobot, signed in with your own account, so your login and the models you can pick come from that CLI. MCP servers, plugins and skills you set up in a CLI are picked up by its Ciaobot chats, with project-level ones read from the workspace folder; Ciaobot can still hide some of Claude Code's built-in tools per workspace.

### Existing installs

- **Moving from 1.x to 2.0:** see [Moving from 1.x to 2.0](https://www.raffaelefarinaro.com/ciaobot/migrate.html) for the Mac, Linux and Windows paths and what changed.
- **Upgrading from the retired macOS app:** follow the [migration guide](INTEGRATIONS.md#upgrading-from-the-macos-app) to keep your workspace, password, chats, and schedules.
- **Updates:** on macOS, Ciaobot for Mac updates itself (Sparkle). On Linux, see [Linux hosting](docs/LINUX.md).
- **Stop or start the engine:** run `ciao service stop` or `ciao service start`. Your workspace is kept.

## How it works

Work is organised as **workspace → project → chat**. A workspace is a big area of your life (personal, work, a client) with its own memory; a project holds the background for a topic; a chat is one task. When you finish a chat, archive it and Ciaobot files what is worth keeping into your notes, as plain Markdown in the folder you chose.

A project can also be linked to a git repository on your machine. Chats in a project with a linked repository work in **code mode** automatically: the chat follows the repository's `AGENTS.md`, loads its skills and can write in it, while notes and decisions stay in Ciaobot. Put a chat in a project without a repository to keep it out.

Everything you can click, you can also ask for in a chat.

**Full guide, features, memory, models and remote access: [raffaelefarinaro.com/ciaobot](https://www.raffaelefarinaro.com/ciaobot/).**

## Documentation

Start with the [documentation hub](docs/README.md), organised by what you want to do:

- **Using Ciaobot:** first task, projects, memory, files, and automations.
- **Connecting and extending:** providers, Google Workspace, skills, and MCP.
- **Operating Ciaobot:** updates, backups, remote access, and troubleshooting.
- **Building Ciaobot:** architecture, development, API, and agent CLI.

## Why not just use Claude Code or OpenCode?

They are excellent agents. Ciaobot gives them a persistent personal workspace: separate areas of your life, project context carried into each conversation, reviewable memory, scheduled work, and documents you can discuss beside the chat.

| Without a persistent workspace | With Ciaobot |
|---|---|
| Explain the same background in every session | Project context travels with the conversation |
| Useful decisions disappear into chat history | Archived chats can become durable Markdown knowledge |
| Copy outputs between a terminal and documents | Review and annotate files beside the chat |
| Remember to repeat the same prompt every week | Automations run it and leave a result to inspect |

### See what it remembered. Decide what stays.

Archiving a chat can file confident facts automatically; uncertain ones go on the Unsure list, one Markdown file you can read in Memory → Unsure. Every change to a note is in the workspace's Git history, so it can be reverted. Daily curation consolidates existing core memory. Old or unused notes are never deleted automatically.

The user-facing product is **Ciaobot**. The CLI is installed as both `ciaobot` and `ciao`; the Python package and many environment variables still use `ciao`/`CIAO_*` for compatibility.

## Built on

Ciaobot builds a persistent personal workspace on excellent open tools: Claude Code (through claude-agent-acp), OpenCode, Starlette, Vue, and more. See [docs/CREDITS.md](docs/CREDITS.md) for the full list.
