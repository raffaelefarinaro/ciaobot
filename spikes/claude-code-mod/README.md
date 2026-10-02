# Spike: Ciaobot as a Claude Code mod

Exploratory, not shipped. Tests whether Ciaobot's chat experience can live
inside Claude Code (mods, Claude Code 2.1.287+) instead of the PWA and engine.

## What it does

`/ciaobot` toggles a docked pane:

- Sidebar: workspace switch, Home, projects and their chats. Projects come
  read-only from `<workspace>/.runtime/web_projects.json` via `bin/catalog.py`.
- A chat is a subagent of the host Claude Code session, spawned with the
  project's context capsule (same fields as `ciao/context/capsule.py`).
- While the pane is open, Enter in Claude's own prompt box goes to the open
  chat (or starts a new one from Home). A band above the prompt says where text
  goes and toggles it back to the main session; `/` commands always pass.
- Finished-chat task notifications are dropped so they do not wake the main
  session.
- Archive removes a chat and queues it for memory extraction (queue only; no
  worker yet).

## Findings (2026-10-02)

- Subagents survive `--resume` of the host session: transcript readable via
  `$.session.messages({ agentId })`, and a message resumes the agent with its
  context. `$.agent.list()` does not list them after resume, so the mod keeps
  its own chat index.
- `session.end` hooks share a ~1.5 s budget; extraction has to be a detached
  process plus a start-up sweep for sessions that ended without the hook.
- No API swaps the main transcript; `/resume` via `$.prompt.fill` is the only
  way to jump between full sessions.
- Panes are a constrained element set (no DOM/webview). `Image` is
  terminal-only; Desktop and mobile get `Svg`. Mobile has no `Input`/`Select`.
- Real Ciao logic (memory pass, search, vault writes) should stay in the
  `ciao` package, run on demand (e.g. `uvx --from <release wheel> ciao ...`),
  which needs server-independent CLI commands; `agent_cli.py` currently talks
  to the running engine over HTTP.

## Known shortcuts

- `CIAO_ROOT` in `hooks/catalog.ts` is hardcoded to the author's workspace.
- No tests; tool calls render as a count; long chats show only the tail.

## Try it

```
claude --plugin-dir spikes/claude-code-mod
```

then `/ciaobot`.
