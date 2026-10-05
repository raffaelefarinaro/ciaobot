---
name: ciao-cli
description: The `ciao` command-line surface for Ciaobot's own state — memory, vault, files, chats, projects, tasks, schedules, webhook triggers, and background runs. Use whenever a chat needs to read or change Ciaobot state itself, not the user's workspace files: checking or editing bounded memory, searching or reviewing the vault, surfacing a file in the pinned panel, listing/creating/sending to/archiving chats, managing projects, filing and moving the workspace task board, creating or running schedules, configuring webhook triggers an external sender can call, or starting/checking/cancelling a tracked background command.
---

# ciao CLI

One `ciao <noun> <verb>` command per Ciaobot operation, callable from the shell. Every command prints one JSON envelope.

## When to use / when not to

Use it for:
- Reading or changing Ciaobot's own state: bounded memory, the vault, chats, projects, the task board, schedules, webhook triggers, background runs.
- Surfacing a file you produced in the user's pinned preview panel.

Not for:
- Editing ordinary workspace files — use the provider's native Read/Write/Edit/Glob, same as today.
- Third-party MCP connectors (Google Workspace beyond `gws status`, or any other integration) — those keep their own tool surface.
- Git — use the shell's `git` directly; `ciao` has no source-control verbs.
- Anything not listed in the command tables below.

## Calling convention

- Every command prints exactly one JSON object on stdout: `{"ok": true, "data": ...}` on success, `{"ok": false, "error": {"code": "...", "message": "...", "retryable": bool}}` on failure. Nothing else goes to stdout. `--json` is accepted for compatibility but output is always JSON — there is no human-readable mode.
- Exit code `0` when `ok` is true, `1` when `ok` is false, `2` for a usage error (bad flags, missing required argument) caught before any operation runs.
- Identity is never a flag. `CIAO_AGENT_TOKEN` and `CIAO_AGENT_URL` in the calling chat's environment say who is calling and against which server; there is no `--workspace` flag and no `--token` flag.
- `--chat ID` is optional on every chat-scoped command and defaults to the calling chat. Pointing it at another chat in the same workspace is allowed; pointing it at a chat in a different workspace returns `workspace_forbidden`.
- Ids are not the only accepted form. `--project` (and a positional project argument) takes a project **name**, and `--chat` takes a chat **title**, matched case-insensitively and exactly, inside the calling chat's workspace only. A title has to name exactly one non-archived chat: two matches, none, or a match in another workspace all return `chat_not_found`, an unknown project returns `project_not_found`, and a name two projects in the workspace share returns `project_ambiguous` (pass the id). Resolution never widens scope, so a name can only ever reach something the id would have reached.
- A plan-mode chat gets `plan_mode_read_only` back from any mutating command — plan mode is read-only by design, not a permission you can flag past.
- Destructive commands (`chat delete`, `chat stop`, `project complete`, `project delete`, `schedule pause|resume|run|delete`, `webhook delete`, `run start`, `run cancel`, and every `vault review` mutation) can return `approval_required` outside auto/bypass mode. That is not a failure to retry — it means a human has to approve the action first.
- Structured input is the exception, not the rule: only `chat handover --messages @file.json` and `run start -- <cmd> [args...]` take it. Everywhere else, arguments are scalar flags or a constrained enum value. For `run start`, everything after `--` is the literal argv passed to the subprocess — no shell, so `run start -- bash -lc "a && b"` is how you get shell features, and you own that choice.

## Commands

### Memory

| Command | Purpose | Guard |
|---|---|---|
| `memory status` | Report bounded native-guide (AGENTS.md) memory usage and diagnostics. | — |
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
| `vault review complete --candidate ID` | Close a project out: move it to `projects/completed/` (the whole folder for a folder project), rewrite `status: active` to `status: completed`, and repoint every note that links to it. Refuses a note that is not a project — retire that one instead. | Same attended-turn guard; a folder project's siblings and backlinks move with it. |
| `vault review restore-completed --candidate ID` | Undo a completion: put the project, its `status:` line and every rewritten link back. Refuses when the original path is occupied or a note the completion rewrote has been edited since. | Same attended-turn guard. |
| `vault review delete --candidate ID --confirm ID` | Permanently delete a trashed note. | Same attended-turn guard, plus `--confirm` must repeat the candidate id; irreversible. |

### Note verification

| Command | Purpose | Guard |
|---|---|---|
| `note verify --payload-file FILE` | Record one stale note's verification verdict — or one stale **entry's**, with `entry` + `entry_fingerprint`: `still_valid`, `update`, `retire` or `unverified`, with its evidence. | `FILE` is JSON: `relative_path`, `expected_revision`, `outcome`, `coverage`, `evidence[]`, `before`/`after`, `reason`, plus `entry`/`entry_fingerprint` for the one-fact case. Every field is note prose or a citation, so it never travels as a shell argument — pass the path and let the server read it. The file must be inside this workspace and under the size cap. `expected_revision` is the lowercase hex SHA-256 of the note's full text as UTF-8, unmodified; the `stale_note` item you were given prints it as `revision <hex>`, so pass that rather than hashing it yourself. With `entry` set, `before`/`after` are the **entry's** text — a whole-note replacement there would rewrite every other fact in the file — and the `stale_entry` item's `reason` prints the note's `revision <hex>`, the `entry identity <hex>`, the `entry fingerprint <hex>` and the `at characters <start>-<end>` span to fill the three fields with, all of them whole: a truncated fingerprint is a different string and comes back `conflict`, and an `entry` that is not all 64 characters is refused before the note is opened. The reply gains `scope: "entry"`. |

The reply's `status` is the whole decision, and the words are not
interchangeable: `applied` wrote the note through a durable receipt;
`needs_review` means the rule refused to write it unattended and **one typed
`note_edit` proposal is now queued for a person** — report it, do not route
around it; `unverified` means nothing this pass reached could settle the
verdict (no source, partial coverage, or an input too large to judge), with
nothing written; `conflict` means the note moved since you read it, so re-read
and judge the text that is there; `already_checked` means a check for this exact
revision is in its cooldown or waiting on a proposal; `failed` means the note
could not be used at all.

`still_valid` re-stamps the note's frontmatter `updated:` and needs `coverage:
complete` plus a citation naming the note. An `update` is applied only when
*every* evidence row is a citation someone could re-open, and it must carry a
fresh `updated:` in its own `after` text. Retirement is never applied here: it
comes back `needs_review` and reaches a person as a proposal. Do not hand-edit
a stale note's Markdown instead of calling this — a hand edit leaves no receipt,
no check state and no record of who decided.

### Memory-proposal review queue

Two older top-level commands, outside the `<noun> <verb>` table and its JSON envelope (plain text unless `--json`). They are how a chat resolves a row from the app's review page.

| Command | Purpose | Guard |
|---|---|---|
| `memory-proposals [--json]` | List the pending proposals in this workspace's review queue. | Read-only. |
| `memory-proposal-dismiss --text-file FILE [--promoted]` | Remove one queued proposal, matched by a unique substring of its text. | Write the text to a file; never pass it as an argument, since proposal text is arbitrary prose. File the fact first, then dismiss with `--promoted`; a plain dismissal records a decision against the fact. Never delete the bullet from the queue file by hand. |

### Skill-proposal review queue

The other top-level pair, same envelope rules. They are how a supported skill improvement is filed and then decided — never how a skill is edited.

| Command | Purpose | Guard |
|---|---|---|
| `skill-proposal-add NAME --input-file FILE` | File one supported improvement proposal for a skill in this workspace's `Workspace/Skill-Proposals/`, merging it into that skill's existing record. | `FILE` is JSON: `title`, `problem`, `change`, `rationale`, and a non-empty `sources` list whose entries carry `chat_id`, `archive`, `turn` and a short verbatim `excerpt`. Every field is text from a conversation, so it never travels as a shell argument. The target is resolved, not trusted: only a source this workspace owns under its own `skills/` directory is accepted, so a packaged skill, a provider mirror, a shared source and an unknown name are refused. Never write into the queue folder by hand, and never edit the skill here. |
| `skill-proposal-remove NAME` | Settle a proposal once the decision is made (implemented, or decided against); `NAME` is the skill or proposal id exactly, or else a unique substring of the skill. | Records the decision and takes the row out of the queue. The record stays on disk, keeps accumulating evidence, and stays settled — re-filing the same finding does not reopen it. Only settle a proposal after its change is actually in place or decided against. `--applied` on a proposal that links learning findings also needs a verification: `--verification-file FILE` for a readback (never paste it as an argument), or `--verification ID` for a managed write receipt. |

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

### Tasks

| Command | Purpose | Guard |
|---|---|---|
| `task list` | List this workspace's board: valid tasks in board order (dated first), then one row per file that is not a readable task, carrying `code` instead of task fields. | Read-only. A malformed file is always reported, never dropped, so an empty-looking board is never the truth. |
| `task get TASK_ID` | Get one task, description/links body included. | Read-only. |
| `task create --title TITLE [--body-file FILE.md] [--project P] [--due YYYY-MM-DD]` | File a task. `--project` takes an id or a name and must belong to this workspace; `--due` is a calendar day, never an instant. | The body is Markdown prose, so it travels as a file — never as a shell argument. A project in another workspace is `project_not_found`. |
| `task update TASK_ID --revision REV [--title T] [--body-file FILE.md] [--due D] [--assignee user\|agent] [--status STATUS]` | Edit one task; only the fields passed change. `REV` is the `revision` you read. | `task_revision_conflict` (retryable) means the file moved: re-read and re-plan, never resend the same revision. An unknown field is refused. |
| `task move TASK_ID --to STATUS --revision REV` | Set the column: `backlog` (To do), `in_progress`, `in_review`, `done`. A delegated task reaches `in_review` through your `task report --outcome done`. | Same revision guard. `--to done` is refused exactly like `complete` — see below. |
| `task complete TASK_ID --revision REV` | Ask to mark a task done. | **Always refused to you** with `task_completion_requires_user`: the user closes their own tasks. Report the finished work and let them complete it; never look for another route to the same status. |
| `task delegate TASK_ID --revision REV [--project P]` | Hand a task to the agent as one ordinary chat. The chat runs in `--project` (or the task's own project, else General) and the prompt is the task's own description — there is no prompt argument. | The turn is **attended**: an approval card it raises is an ordinary Needs-you card in that chat, answered the ordinary way. One live attempt per task: a second call returns the attempt already running and starts nothing. Delegating is not completing — a finished turn puts the task in front of the user for review. |
| `task report TASK_ID --outcome done\|blocked\|needs_input --summary-file FILE.md` | **When you were delegated a task**, say how far you got, once, before your turn ends. `done`: finished, for the user to review. `blocked`: you cannot go on. `needs_input`: you end the turn still waiting on the user's answer (ask it with your question tool first, and carry on in this turn if they reply). The summary (Markdown: what you did, where the results are, what is left) is saved on the task and handed to any later attempt. | Only the chat holding the task's live attempt may report (`task_report_not_holder` otherwise). It never moves or completes the task. A turn that ends **without** a report is shown to the user as *Unfinished*, not as a result to review. |
| `task attempt ATTEMPT_ID ACTION` | `stop` (ends the running turn; irreversible), `resume` (continues the **same** chat under the **same** attempt), `retry` (starts a **new** attempt in a new chat, leaving the old one as history) or `detach` (stops the turn and clears the linkage, which is what makes the task completable again). | `stop` is destructive and is asked for like one. `resume` only works on an attempt that did not finish — a `ready_for_review` result is waiting for the user's decision, so continuing it is their call. None of these completes a task. |

There is no `task delete` on this surface: a task record is the user's own
Markdown file, and removing one is their decision, made in the PWA.

Three things about the board you cannot see from a single `task get`, all of
which are the user's decision rather than yours to resolve:

- **`changed_since_delegated`** in `task list` / `task get` is `true` when the
  record has been edited since the current attempt was handed it — the result you
  are reading was reached against an older description. It is `false` right after
  a clean settle, because the engine rebinds the attempt to the revision its own
  review flag leaves behind. When it is `true`, say so in your report and let the
  user judge the result; the PWA offers them **Send update**, which puts the
  current description into that same chat as one ordinary message. Do **not**
  re-delegate to "fix" it: a second `task delegate` mints a new attempt in a new
  chat and leaves the attempt under review looking abandoned.
- **A finished turn is not a finished task.** Only a turn you ended with
  `task report … --outcome done` becomes `ready_for_review`, which means the
  result is waiting for the user. Report it, name the task, and stop — the
  `Approve Done` in the board is the only completion, and `task complete` is
  refused to you whatever the reason.
- **`task attempt … resume`** continues the same chat under the same attempt and
  is refused for a `ready_for_review` one, and for an attempt whose chat was
  archived or deleted (`attempt_chat_archived`): `retry` then starts a new chat
  that is handed the earlier attempts' reports and transcript paths. Do not reach for `retry` to keep going
  on a review: retrying is the user's call, made next to the result they have not
  read yet.

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

### Webhook triggers

A trigger is a URL an external sender POSTs to. Ciaobot answers the event with a
durable receipt and launches an ordinary chat in the trigger's own project — the
sender never chooses a chat, a model, a mode or a workspace, and the turn is not
unattended, so an approval it raises is an ordinary approval card.

| Command | Purpose | Guard |
|---|---|---|
| `webhook list` | List this workspace's triggers as public records. | Read-only. Never a secret: only a hash of it is stored. |
| `webhook create --name NAME [--instructions-file FILE] [--project P] [--mode normal\|auto\|plan]` | Configure a trigger and mint its secret. | Returns the trigger **and its secret, shown once**: hand it to the sender now, because only its hash is kept and it cannot be read back. Created **disabled** — nothing authorizes until `webhook update --enable`. |
| `webhook update ID --revision REV [--name N] [--instructions-file FILE] [--enable\|--disable]` | Edit one trigger; only the fields you pass change. `--enable`/`--disable` is the off switch, and it reaches an event accepted a moment earlier. | `webhook_revision_conflict` (retryable) means the trigger moved: re-read and re-plan. The target and the mode are not editable — delete and recreate instead. |
| `webhook rotate ID --revision REV` | Replace the trigger's secret and return the new one, shown once. | **Revokes on rotate**: the old secret stops authorizing immediately. Keeps `enabled` as it was; rotating is not a way to enable a disabled trigger. |
| `webhook delete ID --revision REV` | Delete the trigger and destroy its verifier. | Destructive and irreversible: the sender needs a new trigger. Recorded receipts for past events are kept. |

The sender side is not this CLI: an external system calls
`POST /hooks/v1/<trigger_id>` with `Authorization: Bearer <that secret>` and an
`Idempotency-Key`, and a body of exactly `{"text": "..."}`. `202` means the event
is **recorded**, not that a turn finished. Both recipes are in `PWA_API.md`.

Instructions are operator prose that the launched turn reads as its own, so
they travel as a file (`--instructions-file`) rather than as a shell argument.

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

Webhook triggers:
```bash
ciao webhook create --name "CI failure" --instructions-file @webhook.md --project Engineering
```
```json
{"ok": true, "data": {"trigger": {"trigger_id": "5b1f…", "enabled": false, "revision": 1}, "secret": "kQ8…(shown once)"}}
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
- **`unattended_forbidden`** — `vault review keep|trash|restore|complete|restore-completed|delete` only resolve during an attended turn. Running as a schedule or other unattended automation, don't attempt the mutation: report the candidate and its evidence instead, and let an attended turn decide.
- **`payload_required` / `payload_invalid` / `payload_too_large`** — `note verify` takes a path, and the server reads and bounds the document. An empty flag, a path outside the workspace, malformed JSON, an `outcome` outside `still_valid|update|retire|unverified`, or a payload over the cap are all refused before anything is written. Put long evidence in the vault and cite its path instead of pasting it.
- **`workspace_forbidden`** — a `note verify` payload may not name a workspace other than this chat's. The check state and the note-edit proposal are filed per workspace; pairing one workspace's name with another's vault would record a verdict about a vault nobody claimed. Drop the `workspace` field or make it match.
- **`task_revision_conflict`** — the task file changed since you read it. Nothing was written. Run `task get` (or `task list`) again, re-plan your edit against what is there now, and send the new `revision`; resending the old one is the same refusal.
- **`task_completion_requires_user`** — the user, not you, marks a task done. Do not retry, do not reach for `task move --to done`, and do not edit the file: say which task is finished and let them close it.
- **`task_not_found`** — a task id that is well formed but absent *from this workspace*. Another workspace's task is answered exactly this way, so this is also the answer when you reached for an id from the wrong vault.
- **`webhook_revision_conflict`** — the trigger changed since you read it (`webhook list`). Nothing was written: re-read and re-plan instead of resending the same `--revision`.
- **`webhook_not_found`** — no trigger with that id **in this workspace**. A trigger belonging to another workspace answers exactly this way, so do not look for it in another workspace; report the id back and let the operator reconcile it.
- **`webhook_invalid`** — the store refused one field: a blank name, an unknown `--mode` (only `normal`, `auto`, `plan`), or a `--revision` that is not a number. Enabling a revoked trigger is also refused this way: rotate its secret first.
- **`unauthorized` on the sender's POST** — the trigger's secret does not authorize right now. Missing, disabled, revoked and wrong are one answer; if the secret was rotated, the old one is dead by design and the sender needs the new one, which `webhook create`/`webhook rotate` showed once.

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
  "vault review complete": "vault_review",
  "vault review restore-completed": "vault_review",
  "vault review delete": "vault_review",
  "note verify": "verify_note",
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
  "task list": "task_list",
  "task get": "task_get",
  "task create": "task_create",
  "task update": "task_update",
  "task move": "task_action",
  "task complete": "task_action",
  "schedule list": "schedules_list",
  "schedule preview": "schedule",
  "schedule create": "schedule",
  "schedule update": "schedule",
  "schedule pause": "schedule_action",
  "schedule resume": "schedule_action",
  "schedule run": "schedule_action",
  "schedule delete": "schedule_action",
  "webhook list": "webhook_list",
  "webhook create": "webhook_create",
  "webhook update": "webhook_update",
  "webhook rotate": "webhook_rotate",
  "webhook delete": "webhook_delete",
  "run start": "background_run_start",
  "run status": "background_run_status",
  "run cancel": "background_run_cancel",
  "context get": "context_get",
  "gws status": "gws_status",
  "workspace list": "workspaces_list"
}
```
