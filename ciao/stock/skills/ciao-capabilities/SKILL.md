---
name: ciao-capabilities
description: Use for questions about Ciaobot's features, tours and onboarding, or troubleshooting when Ciaobot itself is failing. Covers memory, the vault, schedules, webhooks, workspaces, projects, chats, tasks, files, terminal, skills, MCP, models, providers, settings, updates and conversation imports. For Ciaobot failures or explicit Ciaobot bug reports, read `references/support.md`; errors in the user's own project do not trigger Ciaobot support. Not for performing actions such as scheduling or adding tasks. Trigger on "what can you do", "what can ciaobot do", "help me get started", "give me a tour", "where are my agents", "is the subagent still running", "can ciaobot / can you / can I ...", "how does ... work in ciaobot", or reports that Ciaobot stopped responding, crashed, or is broken, even without its name.
---

# Ciaobot Capabilities

You are running inside Ciaobot. The app's feature surface is not otherwise visible from a chat session — the system prompt covers behavior, not features — so answer capability questions from this catalog instead of guessing from generic Claude knowledge. If the running app visibly disagrees with something here (features evolve), trust the app and say so.

## How to answer

- **Ciaobot-specific trouble** (for example, "Ciaobot stopped responding") or an explicit request such as "file a Ciaobot bug" → read `references/support.md` and follow its evidence, release-check, privacy, and consent steps. A generic error in the user's project (for example, "my Python script crashed") stays with that project; do not select Ciaobot support.

- **Specific question** ("can you schedule things?", "where do my archived chats go?") → answer from the relevant reference file only. Don't recite the whole catalog.
- **Broad question** ("what can you do?") → give the one-paragraph pitch plus the capability areas below in a few lines each, then offer to go deeper on any of them.
- **New user / onboarding** → offer the guided tour below.
- Distinguish the **app** from **you**: this catalog is what the Ciaobot app provides. On top of it you have your normal agent abilities plus whatever skills, subagents, and slash commands are installed in this workspace (`skills/`, `subagents/`, `commands/`, and their `.claude/` mirrors).

## The one-paragraph pitch

Ciaobot is a local-first personal assistant and second brain: a persistent personal workspace for Claude Code, Codex and OpenCode. Your agents do the work; Ciaobot keeps project context, chats, files, schedules, and reviewable memory together. Notes and remembered knowledge stay in plain Markdown you own and can use without Ciaobot. Technically, it is a meta-harness around existing agent execution loops, not a replacement for them. Changing models or harnesses keeps the workspace, but does not make provider sessions, tools, or permissions interchangeable.

## Connecting providers

Ciaobot uses the provider CLI the user already has, rather than creating a
second model account or storing a second provider credential. For Anthropic,
the user installs and signs into [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview),
then chooses Claude Code in Ciaobot; Ciaobot adds workspace/project context and
handles the end-of-conversation memory pass around that session. For Codex, the user installs the Codex CLI and signs in with `codex login`
(install and sign-in are in Settings → Providers), then chooses Codex in Ciaobot.
For OpenRouter, Ollama, or another cloud/local backend, the user
installs [OpenCode 2.3.2+](https://opencode.ai/v2/docs/), configures and
authenticates the provider there, then chooses OpenCode in Ciaobot. Its connected models appear
in the Ciaobot picker. Point users to the live
[`INTEGRATIONS.md`](https://github.com/raffaelefarinaro/ciaobot/blob/main/INTEGRATIONS.md)
for current commands; do not invent version-sensitive install or login syntax.

For provider login or installation prefer the existing Settings flow. For MCP
sign-in use **Chat details → MCP**, which runs the agent's own MCP command;
[MCP servers](/settings/mcp) opens the same command. Read `references/install-and-settings.md` for setup,
manual fallback and `ciao chat needs-action` details. For an operator-run command,
offer a reviewed `ciao chat needs-action command --command-file FILE` suggestion
or point to **Chat details → Open → Terminal**; the user runs it. See
`references/terminal.md` for the shell's boundaries.

## Capability areas

Each area has a reference file next to this one. Open only the file the question needs.

| Area | What it covers | Read |
|---|---|---|
| Workspaces, projects, chats | hierarchy, permission mode and file access, archiving a workspace, usage statistics (`ciao stats`) and the Usage page, `ciao project`, linked repos, code mode, forks, subagents, long scripts (a subagent, or a background shell: the chat is told when it finishes), plan mode, turn resilience | `references/workspaces-and-chats.md` |
| Memory and the vault | the memory pass, what is automatic vs yours, To decide, History and Undo, the optional reason on every decision, Memory Map, Vault Review, retirement candidates, conversation import, memory backup, shared brains | `references/memory.md` |
| Automations | schedules, interval runs, project vs chat targets, catch-up, webhook triggers | `references/automations.md` |
| Files | viewer and pinned panel, folder browser and folder pins, annotations, drafts, previews, document conversion, HTML artifacts, CSV tables, backlinks, keyboard shortcuts | `references/files.md` |
| Skills, models | skills, subagents, slash commands, skill proposals, providers, permission modes, message queue | `references/models-and-extensions.md` |
| Task board | the Tasks page, Delegate to agent, Approve Done, "After this update" tasks | `references/tasks.md` |
| Terminal | the browser terminal panel: where it runs, the grant and the session it rides on, sessions, the socket protocol, how to offer a suggested command, and what an agent can and cannot do | `references/terminal.md` |
| App and system | Settings page, MCP, installing and updating, other devices, forgotten password, Linux, local API | `references/install-and-settings.md` |

## Left to companion tools

Ciaobot deliberately does not build in dictation, keeping the host awake, an agent browser, or subscription usage tracking: those tools change fast, so the user picks their own. The maintainer's open-source picks, each installable by asking the agent: dictation, [FluidVoice](https://github.com/altic-dev/FluidVoice); keeping a Mac awake, [KeepingYouAwake](https://github.com/newmarcel/KeepingYouAwake); an agent browser, [ego lite](https://github.com/citrolabs/ego-lite); usage left, [OpenUsage](https://github.com/robinebers/openusage). The list lives at https://www.raffaelefarinaro.com/ciaobot/companions.html and is linked from Settings → What can Ciaobot do?.

## Privacy and trust posture

Local-first: the server, vault, and runtime state live on the user's machine; traffic leaves only toward the configured model providers. Memory is reviewable: confident state-shaped facts are filed by the memory pass, the unsure ones wait as proposals, every individual accept or dismiss in the queue asks for an optional reason before it writes (picking a re-home destination is the exception; the destination is the answer), and no unattended run promotes a new always-loaded fact (unless the operator set Memory handover to Dismiss and accept). An existing notes folder is never discarded or rewritten during onboarding, and the vault stays portable plain markdown. Ciaobot does not limit the agent's file access; each CLI's permission mode does, and bypass gives full access to what the user account can reach (`references/workspaces-and-chats.md`).

## Guided tour (new users)

When onboarding someone, walk through these hands-on:

1. **Orient** — workspaces → projects → chats; create or rename a project for something they're working on.
2. **Chat** — model picker and project context the agent always sees.
3. **Annotate & files** — message comments, inline file cards, pin, line comments, and rich previews.
4. **Memory** — archive → memory pass; confident facts are filed, unsure ones wait, and no unattended run promotes a new always-loaded fact (unless the operator let Workspace care accept that kind in Settings, or set Memory handover to Dismiss and accept).
5. **Schedules** — set up one small routine they'd actually use.
6. **Task board** — file one task with a due date, open it and press **Delegate to agent**, watch the badge go running, open the chat it works in, then **Approve Done** when the result lands. The step worth stopping on is that the agent's turn finishing never marks the task done: that gesture is theirs (an agent can close a manual task only when you tell it the task is done).
7. **Webhooks** — point an external system at one trigger URL, if they have something that reports events.
8. **Settings** — models & providers, package updates, and the engine's own service controls.

Close with: they can ask "what can Ciaobot do?" (or about any specific feature) in any chat, anytime.

## Where the details live

- Workspace customization surface (env vars, workspaces registry, tool deny-lists, model routing): `CIAO_CUSTOMIZATION.md` in the workspace root.
- Automations how-to: `ciao schedule` commands in `ciao help schedule`. Webhook triggers: `ciao webhook` commands in `ciao help webhook`, and the manage + sender recipes in `PWA_API.md`. Spawning chats: `ciao chat create`. Reaching another model: `ciao chat handover`. Vault read conventions are inline system-prompt policy.
- Canonical docs in the Ciaobot GitHub repo (`raffaelefarinaro/ciaobot`, also present in source checkouts): `README.md`, `docs/ARCHITECTURE.md`, and `PWA_API.md` (routes, auth, agent recipes).
