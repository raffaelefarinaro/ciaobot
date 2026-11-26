---
name: ciao-cli
description: "Exact syntax, flags, guards and error codes for the `ciao <noun> <verb>` commands that read or change Ciaobot's own state: chats, projects, the task board, schedules, webhook triggers, surfacing a file in the pinned panel, and filing into the review queues. Use when a chat needs the exact form of one of those commands. What to write to memory or the vault, and when, is the durable-memory guide's policy, not this one's."
---

# ciao CLI

Use `ciao` for Ciaobot's managed operations. Most use `<noun> <verb>` and return a JSON envelope; the top-level review-queue commands below have their own output conventions. This reference is available through `ciao help`, even when `ciao-cli` is not installed as a skill.

## When to use / when not to

Use it for:
- Reading or changing Ciaobot's own state: chats, projects, the task board, schedules, webhook triggers, and the memory, vault and review-queue commands (write policy: system memory guide).
- Surfacing a file you produced in the user's pinned preview panel.
- Suggesting a terminal command the user can review and run themselves.

Not for:
- Editing ordinary workspace files — use the provider's native Read/Write/Edit/Glob, same as today.
- Third-party MCP connectors (any integration) — those keep their own tool surface.
- Git — use the shell's `git` directly; `ciao` has no source-control verbs.
- Deciding what is worth remembering or where it belongs: follow the engine-packaged durable-memory guide path named in system instructions.

## Calling convention

- Agent-surface commands print exactly one JSON object on stdout: `{"ok": true, "data": ...}` on success, `{"ok": false, "error": {"code": "...", "message": "...", "retryable": bool}}` on failure. Nothing else goes to stdout. `--json` is accepted for compatibility but output is always JSON. Top-level review-queue commands are the exception (see that section).
- Exit code `0` when `ok` is true, `1` when `ok` is false, `2` for a usage error (bad flags, missing required argument) caught before any operation runs.
- Identity is never a flag. `CIAO_AGENT_TOKEN` and `CIAO_AGENT_URL` in the calling chat's environment say who is calling and against which server; there is no `--workspace` flag and no `--token` flag.
- `--chat ID` is optional on every chat-scoped command and defaults to the calling chat. Pointing it at another chat in the same workspace is allowed; pointing it at a chat in a different workspace returns `workspace_forbidden`. The one exception is `chat needs-action`, which has no `--chat` at all: the request belongs to the chat that asked for it.
- Ids are not the only accepted form. `--project` (and a positional project argument) takes a project **name**, and `--chat` takes a chat **title**, matched case-insensitively and exactly, inside the calling chat's workspace only. A title has to name exactly one non-archived chat: two matches, none, or a match in another workspace all return `chat_not_found`, an unknown project returns `project_not_found`, and a name two projects in the workspace share returns `project_ambiguous` (pass the id). Resolution never widens scope, so a name can only ever reach something the id would have reached.
- A plan-mode chat gets `plan_mode_read_only` back from any mutating command — plan mode is read-only by design, not a permission you can flag past.
- Destructive commands (`chat delete`, `chat stop`, `project complete`, `project delete`, `schedule pause|resume|run|delete` and `webhook delete`) can return `approval_required` outside auto/bypass mode. That is not a failure to retry — it means a human has to approve the action first.
- `schedule create|update --command` sets a shell command that later runs with no permission prompt. Outside bypass mode it returns `command_requires_operator`: create the schedule without it and ask the user to add the command in the schedule's Prompt card. Do not retry.
- Structured input is the exception, not the rule: `chat handover --messages @file.json`, and `chat needs-action command --command-file FILE` take it. Everywhere else, arguments are scalar flags or a constrained enum value.

## Topics

The command tables, guards and examples live in five references beside this file. `ciao help <topic>` prints this entry plus that topic's reference; bare `ciao help` prints all five, in this order.

- `memory`, `vault`, `queues` — [memory-and-review.md](references/memory-and-review.md): memory, vault search, note review and review queues.
- `note`, `file`, `chat`, `project` — [workspace-operations.md](references/workspace-operations.md): notes, files, chats and projects, with the MCP/tool reload procedure.
- `task` — [tasks.md](references/tasks.md): the board, delegation, reports and deletion.
- `schedule`, `loop`, `webhook`, `context`, `stats`, `workspace`, `repo` — [automation-and-status.md](references/automation-and-status.md): automations, triggers, context and stats.
- `examples` — [reference.md](reference.md): worked examples, errors and long-running commands.
