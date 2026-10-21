---
name: ciao-capabilities
description: Use for questions about what Ciaobot can do or how one of its features works, and for tours and onboarding of new users. Covers memory and the vault, Memory Map, vault review, schedules and webhook triggers, workspaces, projects, forks, subagents, files and pinned panels, file access, slash commands, keyboard shortcuts, document conversion, HTML artifacts and CSV tables, the task board, the browser terminal, skills, MCP, models and providers, permission modes, notifications, updates, importing past conversations, and installing on macOS, Linux or Windows. Not for performing the action itself (scheduling, remembering, adding a task). Trigger on "what can you do", "what can ciaobot do", "help me get started", "give me a tour", "can ciaobot / can you / can I ...", "how does ... work in ciaobot", even when the word Ciaobot is not mentioned.
---

# Ciaobot Capabilities

You are running inside Ciaobot. The app's feature surface is not otherwise visible from a chat session — the system prompt covers behavior, not features — so answer capability questions from this catalog instead of guessing from generic Claude knowledge. If the running app visibly disagrees with something here (features evolve), trust the app and say so.

## How to answer

- **Specific question** ("can you schedule things?", "where do my archived chats go?") → answer from the relevant reference file only. Don't recite the whole catalog.
- **Broad question** ("what can you do?") → give the one-paragraph pitch plus the capability areas below in a few lines each, then offer to go deeper on any of them.
- **New user / onboarding** → offer the guided tour below.
- Distinguish the **app** from **you**: this catalog is what the Ciaobot app provides. On top of it you have your normal agent abilities plus whatever skills, subagents, and slash commands are installed in this workspace (`skills/`, `subagents/`, `commands/`, and their `.claude/` mirrors).

## The one-paragraph pitch

Ciaobot is a local-first personal assistant and second brain: a persistent personal workspace for Claude Code and OpenCode. Your agents do the work; Ciaobot keeps project context, chats, files, schedules, and reviewable memory together. Notes and remembered knowledge stay in plain Markdown you own and can use without Ciaobot. Technically, it is a meta-harness around existing agent execution loops, not a replacement for them. Changing models or harnesses keeps the workspace, but does not make provider sessions, tools, or permissions interchangeable.

## Connecting providers

Ciaobot uses the provider CLI the user already has, rather than creating a
second model account or storing a second provider credential. For Anthropic,
the user installs and signs into [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview),
then chooses Claude Code in Ciaobot; Ciaobot adds workspace/project context and
handles the end-of-conversation memory pass around that session. For
OpenAI access, OpenRouter, Ollama, or another cloud/local backend, the user
installs [OpenCode 2.0.16+](https://opencode.ai/v2/docs/), configures and
authenticates the provider there, then chooses OpenCode in Ciaobot. Its connected models appear
in the Ciaobot picker. Point users to the live
[`INTEGRATIONS.md`](https://github.com/raffaelefarinaro/ciaobot/blob/main/INTEGRATIONS.md)
for current commands; do not invent version-sensitive install or login syntax.

## Capability areas

Each area has a reference file next to this one. Open only the file the question needs.

| Area | What it covers | Read |
|---|---|---|
| Workspaces, projects, chats | hierarchy, file access, archiving a workspace, `ciao project`, linked repos, code mode, forks, subagents, background runs, plan mode, turn resilience | `references/workspaces-and-chats.md` |
| Memory and the vault | the memory pass, what is automatic vs yours, To decide, History and Undo, Memory Map, Vault Review, retirement candidates, conversation import, memory backup | `references/memory.md` |
| Automations | schedules, interval runs, project vs chat targets, catch-up, webhook triggers | `references/automations.md` |
| Files | viewer and pinned panel, annotations, drafts, previews, document conversion, HTML artifacts, CSV tables, backlinks, keyboard shortcuts | `references/files.md` |
| Skills, models, Google Workspace | skills, subagents, slash commands, skill proposals, providers, permission modes, queue and steer, `gws` | `references/models-and-extensions.md` |
| Task board | the Tasks page, Delegate to agent, Approve Done, "After this update" tasks | `references/tasks.md` |
| Terminal | the browser terminal panel: where it runs, the unlock, sessions, the socket protocol, and what an agent can and cannot do | `references/terminal.md` |
| App and system | Settings page, MCP, installing and updating, other devices, forgotten password, Linux, Windows, local API | `references/install-and-settings.md` |

## Privacy and trust posture

Local-first: the server, vault, and runtime state live on the user's machine; traffic leaves only toward the configured model providers. Memory is reviewable: confident state-shaped facts are filed by the memory pass, the unsure ones wait as proposals, and no unattended run promotes a new always-loaded fact (unless the operator set Memory handover to Dismiss and accept). An existing notes folder is never discarded or rewritten during onboarding, and the vault stays portable plain markdown. The agent's file access is set per workspace (File access, in `references/workspaces-and-chats.md`): under Workspace only it cannot read outside that workspace's folder, but the network is not restricted.

## Guided tour (new users)

When onboarding someone, walk through these hands-on:

1. **Orient** — workspaces → projects → chats; create or rename a project for something they're working on.
2. **Chat** — model picker and project context the agent always sees.
3. **Annotate & files** — message comments, inline file cards, pin, line comments, and rich previews.
4. **Memory** — archive → memory pass; confident facts are filed, unsure ones wait, and no unattended run promotes a new always-loaded fact (unless the operator set Memory handover to Dismiss and accept).
5. **Schedules** — set up one small routine they'd actually use.
6. **Task board** — file one task with a due date, open it and press **Delegate to agent**, watch the badge go running, open the chat it works in, then **Approve Done** when the result lands. The step worth stopping on is that the agent's turn finishing never marks the task done: that gesture is theirs.
7. **Webhooks** — point an external system at one trigger URL, if they have something that reports events.
8. **Settings** — models & providers, package updates, and the engine's own service controls.

Close with: they can ask "what can Ciaobot do?" (or about any specific feature) in any chat, anytime.

## Where the details live

- Workspace customization surface (env vars, workspaces registry, tool deny-lists, model routing): `CIAO_CUSTOMIZATION.md` in the workspace root.
- Automations how-to: `ciao schedule` commands in `ciao help`. Webhook triggers: `ciao webhook` commands in `ciao help`, and the manage + sender recipes in `PWA_API.md`. Spawning chats: `ciao chat create`. Reaching another model: `ciao chat handover`. Vault read conventions are inline system-prompt policy.
- Canonical docs in the Ciaobot GitHub repo (`raffaelefarinaro/ciaobot`, also present in source checkouts): `README.md`, `docs/ARCHITECTURE.md`, and `PWA_API.md` (routes, auth, agent recipes).
