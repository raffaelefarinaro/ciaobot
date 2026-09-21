---
name: ciao-cli
description: The `ciao` command-line surface for Ciaobot's own state — memory, vault, files, chats, projects, schedules, and background runs. Use whenever a chat needs to read or change Ciaobot state itself, not the user's workspace files: checking or editing bounded memory, searching or reviewing the vault, surfacing a file in the pinned panel, listing/creating/sending to/archiving chats, managing projects, creating or running schedules, or starting/checking/cancelling a tracked background command.
---

# ciao CLI

One `ciao <noun> <verb>` command per Ciaobot operation, callable from the shell. Every command prints one JSON envelope.

## When to use / when not to

Use it for:
- Reading or changing Ciaobot's own state: bounded memory, the vault, chats, projects, schedules, background runs.
- Surfacing a file you produced in the user's pinned preview panel.

Not for:
- Editing ordinary workspace files — use the provider's native Read/Write/Edit/Glob, same as today.
- Third-party MCP connectors (Google Workspace beyond `gws status`, or any other integration) — those keep their own tool surface.
- Git — use the shell's `git` directly; `ciao` has no source-control verbs.
- Anything not listed in the command table below.

## Calling convention

- Every command prints exactly one JSON object on stdout: `{"ok": true, "data": ...}` on success, `{"ok": false, "error": {"code": "...", "message": "...", "retryable": bool}}` on failure. Nothing else goes to stdout. `--json` is accepted for compatibility but output is always JSON — there is no human-readable mode.
- Exit code `0` when `ok` is true, `1` when `ok` is false, `2` for a usage error (bad flags, missing required argument) caught before any operation runs.
- Identity is never a flag. `CIAO_AGENT_TOKEN` and `CIAO_AGENT_URL` in the calling chat's environment say who is calling and against which server; there is no `--workspace` flag and no `--token` flag.
- `--chat ID` is optional on every chat-scoped command and defaults to the calling chat. Pointing it at another chat in the same workspace is allowed; pointing it at a chat in a different workspace returns `workspace_forbidden`.
- A plan-mode chat gets `plan_mode_read_only` back from any mutating command — plan mode is read-only by design, not a permission you can flag past.
- Destructive commands (`chat delete`, `chat stop`, `project complete`, `project delete`, `schedule pause|resume|run|delete`, `run start`, `run cancel`, and every `vault review` mutation) can return `approval_required` outside auto/bypass mode. That is not a failure to retry — it means a human has to approve the action first.
- Structured input is the exception, not the rule: only `chat handover --messages @file.json` and `run start -- <cmd> [args...]` take it. Everywhere else, arguments are scalar flags or a constrained enum value. For `run start`, everything after `--` is the literal argv passed to the subprocess — no shell, so `run start -- bash -lc "a && b"` is how you get shell features, and you own that choice.

## Commands

### Memory

| Command | Purpose | Guard |
|---|---|---|
| `memory status` | Report bounded native-guide (CLAUDE.md) memory usage and diagnostics. | — |
| `memory update --region {memory,profile} --action {add,replace,remove} [--entry TEXT] [--match TEXT]` | Add, replace, or remove one entry in native `memory`/`profile` memory. | `--match` targets replace/remove, `--entry` is the new text for add/replace. The region cap is advisory: the write always lands, and the reply carries `over_cap` with `used_chars`/`char_limit` when it exceeds the configured limit — consolidation, not refusal, is what bounds a region. |

### Vault

| Command | Purpose | Guard |
|---|---|---|
| `vault search QUERY [--limit N]` | Full-text search the active workspace vault. | — |
| `vault review list` | List scoped vault-note review candidates (stale, orphaned, duplicate, weakly linked). | Read-only; an unattended schedule may only list, never decide. |
| `vault review show PATH` | Inspect one candidate's evidence (why it was flagged, backlinks, duplicates). | Read-only. |
| `vault review keep --candidate ID` | Record a "keep" decision: clears the row and stamps the note's `updated:` date to today. | Mutating; needs an attended turn — `unattended_forbidden` when run from a schedule or other automation. |
| `vault review trash --candidate ID` | Move a note to trash (reversible). | Same attended-turn guard as `keep`. |
| `vault review restore --candidate ID` | Restore a trashed note. | Same attended-turn guard. |
| `vault review delete --candidate ID --confirm ID` | Permanently delete a trashed note. | Same attended-turn guard, plus `--confirm` must repeat the candidate id; irreversible. |

### Files

| Command | Purpose | Guard |
|---|---|---|
| `file surface PATH` | Deliberately open a workspace file — one you wrote, read, or a subagent produced — in the user's pinned preview panel. | The path must already exist under the workspace (`file_not_found` otherwise). The call only validates the path and requests the pin; `viewers` and `stream_state` in the reply don't prove the panel opened or stayed closed. |

### Chats

| Command | Purpose | Guard |
|---|---|---|
| `chat list [--project ID]` | List active and archived chats in the workspace or one project. | — |
| `chat get [--chat ID]` | Get one chat. | Omit `--chat` for the calling chat. |
| `chat create [--project ID\|NAME] [--title T] [--provider P] [--model M] [--mode MODE] [--prompt TEXT]` | Create a chat, optionally sending its first prompt in the same call. | Omit `--project` to reuse the calling chat's own project — no need to list projects first for a sub-topic. |
| `chat update [--chat ID] [--title] [--provider] [--model] [--mode] [--thinking-level] [--project]` | Update chat metadata and same-backend model settings. | Omit `--chat` for the calling chat. |
| `chat send --chat ID --prompt TEXT` | Start or queue a user turn in another chat. | `--chat` is required here — there is no "send to self" case. |
| `chat continue --chat ID` | Continue an archived chat as a new active chat. | — |
| `chat retry [--chat ID] [--action set\|stop\|try_now] [--prompt TEXT]` | Manage a deferred provider-limit retry. | Default action is `try_now`. |
| `chat handover [--chat ID] --provider P [--model M] [--messages @file.json]` | Move a chat to a fresh provider session, optionally carrying visible history. | With both `--provider` and `--model` empty, it only clears the current session in place. |
| `chat archive [--chat ID]` | Archive a chat to the vault and trigger normal post-archive memory extraction. | — |
| `chat delete [--chat ID]` | Delete a chat. | Destructive; deleting the calling chat itself is deferred until the turn finishes. |
| `chat stop --chat ID` | Stop another chat's active provider turn. | Destructive; a chat cannot stop its own turn. |

### Projects

| Command | Purpose | Guard |
|---|---|---|
| `project list [--include-completed]` | List projects in the active workspace. | — |
| `project get [ID]` | Get one project by id or name. | Omit for the active project. |
| `project create --name N [--context TEXT]` | Create a project. | `--vault-folder` is not a create-time flag; set it with `project update` after creating. |
| `project update ID [--name] [--context] [--vault-folder]` | Update project metadata or its safe vault-folder binding. | Omit `ID` for the active project; an omitted flag keeps its current value. |
| `project restore STEM` | Restore a completed vault project into the active workspace. | Takes the completed project's *stem*, not a project id. |
| `project complete ID` | Move a vault-backed project to completed and archive its active record. | Destructive. |
| `project delete ID` | Delete a non-vault-backed project and its chats. | Destructive. |

### Schedules

| Command | Purpose | Guard |
|---|---|---|
| `schedule list` | List schedules in the active workspace with their next run. | — |
| `schedule preview ...flags` | Validate fields and compute `next_run` without saving. | Run before `create` for a new recurring schedule; a missing or invalid `next_run` means the fields don't validate yet. |
| `schedule create ...flags` | Create a validated schedule (recurring, one-off, or manual-only). | Show the user a draft — `next_run`, workspace, project — and get confirmation unless they already asked to apply it. |
| `schedule update ID ...flags` | Update an existing schedule; only fields you pass change. | A system schedule (`scope=system`) only accepts `--enabled`/workspace changes — anything else raises `system_schedule_read_only`. |
| `schedule pause ID` | Pause a schedule without deleting it. | Destructive. |
| `schedule resume ID` | Resume a paused schedule. | Destructive. |
| `schedule run ID` | Dispatch a schedule immediately through the normal chat pipeline. | Destructive. |
| `schedule delete ID` | Delete a removable user schedule. | Destructive; a system schedule cannot be deleted (`schedule_not_removable`). |

Shared `...flags` for `schedule create/update/preview`: `--prompt --daily-time --timezone --frequency {daily,weekly,monthly,manual,once,interval} --interval-minutes --days-of-week mon,tue --day-of-month --run-at-date --project --title --description --provider --model --archive-policy {manual,auto}`. There is no `--chat` binding flag — schedules bind to a project and workspace, never to one chat.

### Background runs

| Command | Purpose | Guard |
|---|---|---|
| `run start [--cwd DIR] [--env K=V]... [--timeout-s N] [--label L] -- CMD [ARGS...]` | Run one command in a tracked background subprocess; the chat is woken with status, exit code, log tail, and log path when it exits. | Destructive (approval-gated). Does not block — end the turn after starting it, don't poll `run status` for the common case. `--cwd` must stay under the chat's workspace root; loader-hook env vars are rejected. |
| `run status RUN_ID [--lines N]` | Status, exit code, and log tail for a run this chat started. | A run started by another chat reports as not found. |
| `run cancel RUN_ID` | SIGTERM the run's process group, then SIGKILL after a grace period. | Destructive; idempotent — cancelling an already-finished run returns its final state unchanged. |

### Workspace and status

| Command | Purpose | Guard |
|---|---|---|
| `context get` | Return the active workspace, project, chat, provider, and control surface, plus local server/startup status. | — |
| `gws status` | Report whether the active workspace's Google Workspace account is connected and its token is valid. | Read-only; never triggers a live re-auth check — use it to warn the user their login expired, not to force one. |
| `workspace list` | List all configured logical workspaces — names, vault roots, defaults — not just the active one. | — |

## Worked examples

Memory:
```bash
ciao memory update --region memory --action add --entry "Prefers terse commit messages"
```
```json
{"ok": true, "data": {"region": "memory", "action": "add", "over_cap": false}}
```

Vault:
```bash
ciao vault review trash --candidate cand_8f2a
```
```json
{"ok": true, "data": {"candidate_id": "cand_8f2a", "path": "People/Old-Contact.md", "state": "trashed"}}
```

Files:
```bash
ciao file surface Workspace/deploy-report.html
```
```json
{"ok": true, "data": {"path": "Workspace/deploy-report.html", "viewers": 1, "stream_state": "active"}}
```

Chats:
```bash
ciao chat send --chat chat_91ab --prompt "Re-run the nightly check now"
```
```json
{"ok": true, "data": {"chat_id": "chat_91ab", "status": "queued"}}
```

Projects:
```bash
ciao project create --name "Q4 Launch" --context "Coordinates the Q4 rollout"
```
```json
{"ok": true, "data": {"project_id": "proj_q4launch", "name": "Q4 Launch"}}
```

Schedules:
```bash
ciao schedule create --prompt "Check open PRs" --frequency daily --daily-time 09:00 \
  --timezone Europe/Rome --project "Engineering"
```
```json
{"ok": true, "data": {"schedule_id": "sch_4471", "next_run": "2026-09-22T09:00:00+02:00"}}
```

Background runs:
```bash
ciao run start --label "adoption report" --timeout-s 900 -- ./scripts/report.sh --full
```
```json
{"ok": true, "data": {"run_id": "run_20b7", "label": "adoption report", "status": "started"}}
```

Workspace and status:
```bash
ciao context get
```
```json
{"ok": true, "data": {"workspace": "personal", "project": "General", "chat_id": "chat_91ab", "system": {"server": "up"}}}
```

## Common mistakes

- **`file_not_found`** — `file surface` only opens a path that already exists under the workspace root. Check the file was actually written (or that the path is relative to the workspace, not absolute or outside it) before surfacing it.
- **`memory_update_invalid` / `invalid_action`** — every `action`-style flag across this CLI is a closed enum, not free text (`memory update` takes `add`/`replace`/`remove`, never `append`). Run `ciao memory update --help` (or the equivalent `--help` on any command) to see the exact accepted values before guessing.
- **`unattended_forbidden`** — `vault review keep|trash|restore|delete` only resolve during an attended turn. Running as a schedule or other unattended automation, don't attempt the mutation: report the candidate and its evidence instead, and let an attended turn decide.

## Operation names for telemetry

```json
{
  "memory status": "memory_status",
  "memory update": "memory_update",
  "vault search": "vault_search",
  "vault review list": "vault_review",
  "vault review show": "vault_review",
  "vault review keep": "vault_review",
  "vault review trash": "vault_review",
  "vault review restore": "vault_review",
  "vault review delete": "vault_review",
  "file surface": "file_surface",
  "chat list": "chats_list",
  "chat get": "chat_get",
  "chat create": "chat_create",
  "chat update": "chat_update",
  "chat send": "chat_send",
  "chat continue": "chat_continue",
  "chat retry": "chat_retry",
  "chat handover": "chat_handover",
  "chat archive": "chat_archive",
  "chat delete": "chat_delete",
  "chat stop": "chat_stop",
  "project list": "projects_list",
  "project get": "project_get",
  "project create": "project",
  "project update": "project",
  "project restore": "project",
  "project complete": "project_action",
  "project delete": "project_action",
  "schedule list": "schedules_list",
  "schedule preview": "schedule",
  "schedule create": "schedule",
  "schedule update": "schedule",
  "schedule pause": "schedule_action",
  "schedule resume": "schedule_action",
  "schedule run": "schedule_action",
  "schedule delete": "schedule_action",
  "run start": "background_run_start",
  "run status": "background_run_status",
  "run cancel": "background_run_cancel",
  "context get": "context_get",
  "gws status": "gws_status",
  "workspace list": "workspaces_list"
}
```
