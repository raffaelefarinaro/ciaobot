---
name: ciao-cli
description: "Exact syntax, flags, guards and error codes for the `ciao <noun> <verb>` commands that read or change Ciaobot's own state: chats, projects, the task board, schedules, webhook triggers, surfacing a file in the pinned panel, and filing into the review queues. Use when a chat needs the exact form of one of those commands. What to write to memory or the vault, and when, is the `ciao-memory` skill's policy, not this one's."
---

# ciao CLI

One `ciao <noun> <verb>` command per Ciaobot operation, callable from the shell. Every command prints one JSON envelope.

## When to use / when not to

Use it for:
- Reading or changing Ciaobot's own state: chats, projects, the task board, schedules, webhook triggers, and the memory, vault and review-queue commands (syntax only; their write policy is `ciao-memory`).
- Surfacing a file you produced in the user's pinned preview panel.
- Suggesting a terminal command the user can review and run themselves.

Not for:
- Editing ordinary workspace files — use the provider's native Read/Write/Edit/Glob, same as today.
- Third-party MCP connectors (Google Workspace beyond `gws status`, or any other integration) — those keep their own tool surface.
- Git — use the shell's `git` directly; `ciao` has no source-control verbs.
- Deciding what is worth remembering or where it belongs: follow `ciao-memory`.

## Calling convention

- Every command prints exactly one JSON object on stdout: `{"ok": true, "data": ...}` on success, `{"ok": false, "error": {"code": "...", "message": "...", "retryable": bool}}` on failure. Nothing else goes to stdout. `--json` is accepted for compatibility but output is always JSON — there is no human-readable mode.
- Exit code `0` when `ok` is true, `1` when `ok` is false, `2` for a usage error (bad flags, missing required argument) caught before any operation runs.
- Identity is never a flag. `CIAO_AGENT_TOKEN` and `CIAO_AGENT_URL` in the calling chat's environment say who is calling and against which server; there is no `--workspace` flag and no `--token` flag.
- `--chat ID` is optional on every chat-scoped command and defaults to the calling chat. Pointing it at another chat in the same workspace is allowed; pointing it at a chat in a different workspace returns `workspace_forbidden`. The one exception is `chat terminal-offer`, which has no `--chat` at all: the suggestion belongs to the chat that asked for it.
- Ids are not the only accepted form. `--project` (and a positional project argument) takes a project **name**, and `--chat` takes a chat **title**, matched case-insensitively and exactly, inside the calling chat's workspace only. A title has to name exactly one non-archived chat: two matches, none, or a match in another workspace all return `chat_not_found`, an unknown project returns `project_not_found`, and a name two projects in the workspace share returns `project_ambiguous` (pass the id). Resolution never widens scope, so a name can only ever reach something the id would have reached.
- A plan-mode chat gets `plan_mode_read_only` back from any mutating command — plan mode is read-only by design, not a permission you can flag past.
- Destructive commands (`chat delete`, `chat stop`, `project complete`, `project delete`, `schedule pause|resume|run|delete`, `webhook delete`, and every `vault review` mutation) can return `approval_required` outside auto/bypass mode. That is not a failure to retry — it means a human has to approve the action first.
- `schedule create|update --command` sets a shell command that later runs with no permission prompt. Outside bypass mode it returns `command_requires_operator`: create the schedule without it and ask the user to add the command in the schedule's Prompt card. Do not retry.
- Structured input is the exception, not the rule: only `chat handover --messages @file.json` and `chat terminal-offer --command-file FILE` take it. Everywhere else, arguments are scalar flags or a constrained enum value.

### Reloading tools

`/mcp`, `/reload-plugins` and the other Claude Code / opencode terminal slash commands do not exist in Ciaobot. When an MCP server, plugin or skill was added or changed, run `ciao chat reload`.

### Long-running commands

For a long command use the provider's native background shell (Bash `run_in_background`, `Monitor`, OpenCode's background shell): it wakes the chat when it finishes and Ciaobot keeps the provider alive meanwhile. On Claude, a background Bash stops at its `timeout` (30 minutes by default): pass a `timeout` that covers the run, up to 24 hours.

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

### Notes

| Command | Purpose | Guard |
|---|---|---|
| `note create --type TYPE --title TITLE [--description-file F] [--tags a,b] [--body-file F] [--path DIR]` | Create a vault note with canonical frontmatter (`type`, `title`, `description`, `tags`, `created`, `updated`). | `TYPE` is a category id or alias from `VOCABULARY.md`; an unknown type lists the valid ones. The folder is the category's, and a category with no folder needs `--path`, a vault-relative folder. Each `FILE` is inside this workspace; prose never goes as an argument. It never overwrites an existing note. For a project, use `project create`. |
| `note related PATH [--limit N]` | List the vault notes connected to one note: the ones it links to, the ones linking to it and the ones whose body names its title or an alias. Read it before a verdict on a stale note. | Read-only, scoped to this workspace's vault, `PATH` vault-relative. Each related note prints its own `revision` and `due`: `revision` is the `expected_revision` a `note verify` payload for that note needs, and `due: false` means a check already settles it. |
| `note verify --payload-file FILE` | Record one stale note's verification verdict, or one stale **entry's** with `entry` + `entry_fingerprint`: `still_valid`, `update`, `retire` or `unverified`, with its evidence. | `FILE` is JSON inside this workspace (note prose never travels as a shell argument). Fields, the `expected_revision` rule, the entry-level steps and an example are in the `ciao-memory` skill, `references/note-verify.md`. |

The reply's `status` is the whole decision: `applied` wrote the note through a durable receipt; `needs_review` means one typed `note_edit` proposal is queued for a person (report it, do not route around it); `unverified` and `conflict` wrote nothing; `already_checked` means a check for this exact revision is in its cooldown or waiting on a proposal; `failed` means the note could not be used at all. Retirement always comes back `needs_review`. Do not hand-edit a stale note's Markdown instead of calling this.

### Review queues

Top-level commands, outside the `<noun> <verb>` table: plain text output (a `--json` flag gives structured output where noted), not the JSON envelope. They file into, list and settle the queues the app's review page shows. Every free-text field travels in a file, never as a shell argument: it is conversation prose, and `$()`, backticks or quotes would run or mangle.

| Command | Purpose | Guard |
|---|---|---|
| `memory-proposals [--limit N] [--offset N] [--json]` | List the pending proposals in this workspace's review queue, one page at a time (default 20, max 200). | Read-only. When the page has `next_offset` (or the text output says `more with --offset N`), pass `--offset` with it for the next page. `--json` gives `proposals`, `total`, `offset`, `limit`, and `truncated`/`next_offset` only when more remain. |
| `memory-proposal-add [TEXT] [--text-file FILE] [--kind K] [--payload P] [--payload-file FILE] [--source S] [--request ID] [--decide nightly|you] [--decide-reason-file FILE] [--allow-dismissed] [--json]` | Queue one durable fact in `Workspace/Memory-Proposals.md` for review. | `--kind` is `memory` (default), `profile`, `project`, `people`, `learnings`, `review` or `repo`; `project`, `people` and `repo` also need `--payload` (a doc path, a person's name, an absolute repository path). Prefer `--text-file` over `TEXT`. Re-filing identical text is a no-op; `--allow-dismissed` re-files a dismissed fact, a promoted one stays deduped. `--request` is for `learnings` only. `--decide-reason-file` is required with `--decide`: `nightly` means more context would settle it, `you` means a judgement about the operator. |
| `memory-proposal-dismiss --text-file FILE [--promoted] [--reason-file FILE] [--handover]` | Remove one queued proposal, matched by a unique substring of its text. | Write the text to a file. File the fact first, then dismiss with `--promoted`; a plain dismissal records a decision against the fact. Optionally `--reason-file` records why. `--handover` settles a `[memory]`/`[profile]` row the run predicted dismiss; refused unless the operator's handover allows it. Never delete the bullet from the queue file by hand. |
| `decisions-digest [--json]` | Group the operator's recent memory and task decisions (last 180 days) for the weekly rules pass. | Read-only. Operator decisions only; agent-made ones are counted, never listed. |
| `task-rules` | Print the operator's distilled task preferences from `Workspace/Task-Rules.md`, fenced; prints nothing when absent. | Read-only. Preferences, never orders. Run it before filing a task. |
| `decision-rules-write memory|task --body-file FILE [--json]` | Write the managed section of `Workspace/Memory-Review-Rules.md` or `Workspace/Task-Rules.md`. | Weekly pass only. Writes only inside the fenced managed section; refuses over 2500 chars, a malformed fence, or an unapproved flip. |
| `memory-proposal-escalate --text-file FILE --reason-file FILE [--json]` | Re-tag one queued proposal `decide: you` with the reason it could not be settled. | Matched like dismiss. Not a decision: the bullet stays queued and no History row is written. Never edit the queue by hand. |
| `memory-proposal-predict --text-file FILE --verdict accept|dismiss --reason-file FILE [--json]` | Record the verdict the run would give a queued `[memory]`/`[profile]` row, without acting. | Not a decision: the bullet stays queued and no History row is written until the operator decides. Re-recording an identical verdict is a no-op. |
| `memory-proposal-accept --text-file FILE [--json]` | Accept one queued `[memory]`/`[profile]` row into its region. | Refused unless the operator's handover is `all` and a stored accept prediction exists. Writes through the guarded path with an undo receipt. |
| `observation-add --about user\|NAME [--about-file FILE] --source CHAT_ID --text-file FILE [--json]` | Count this chat's sighting of one behavioural pattern in `Workspace/Observations.md`. | `--source` is required. A chat counts at most once per observation; at three distinct chats the Workspace care run proposes it. The file holds the one-sentence pattern. |
| `skill-proposal-add NAME --input-file FILE` | File one supported improvement proposal for a skill in this workspace's `Workspace/Skill-Proposals/`, merging it into that skill's existing record. | `FILE` is JSON: `title`, `problem`, `change`, `rationale`, and either a non-empty `sources` list (entries carry `chat_id`, `archive`, `turn` and a short verbatim `excerpt`) or, for a routed learning, a non-empty `origins` list (entries carry `learning_id` and `finding`, optionally `source_revision` and `summary`; `workspace` may only name this one; `state` and `verification` are refused). A payload with neither is refused; `origins` without `sources` is the lesson-routing form. Every field is text from a conversation, so it never travels as a shell argument. The target is resolved, not trusted: only a source this workspace owns under its own `skills/` directory is accepted, so a packaged skill, a provider mirror, a shared source and an unknown name are refused. Never write into the queue folder by hand, and never edit the skill here. |
| `skill-proposal-remove NAME` | Settle a proposal once the decision is made (implemented, or decided against); `NAME` is the skill or proposal id exactly, or else a unique substring of the skill. | Records the decision and takes the row out of the queue. The record stays on disk, keeps accumulating evidence, and stays settled — re-filing the same finding does not reopen it. Only settle a proposal after its change is actually in place or decided against. `--applied` on a proposal that links learning findings also needs a verification: `--verification-file FILE` for a readback (never paste it as an argument), or `--verification ID` for a managed write receipt. |
| `skill-draft-add --input-file FILE [--json]` | File one `[review]` draft for a lesson this workspace cannot edit in place: `target` `upstream_issue` (a packaged, mirrored or shared skill) or `new_skill` (a workflow no skill covers). | `FILE` is JSON: `target`, `skill`, `title`, `change`, plus a sanitized `body` for an issue, and optionally `repository` (`owner/name`), `version`, `private_evidence`, `origins`, `same_as` (fold this sighting into an existing draft of the same skill) or `distinct_from` (list every existing draft id of that skill when this is a different defect); a filing for a skill that already has drafts is refused until one of them is given. Title, skill, version and body reach `gh`, so any carrying a transcript excerpt, path, name or credential is refused. Files and nothing more: no issue is opened and no skill created. |
| `skill-drafts [--all] [--json]` | List open skill drafts (`--all` adds settled ones) with the ids `skill-draft-approve` takes. | Read-only. |
| `skill-draft-approve DRAFT_ID [--content-file FILE] [--reason R] [--json]` | Act on one draft: search for a matching GitHub issue and link or file it, or create a `new_skill` under this workspace's `skills/`. | Attended only: exits 4 and changes nothing while a curation run holds the lease, so a schedule reports the draft instead. `--content-file` (the SKILL.md text) is required for `new_skill`. A failed or ambiguous request leaves the draft pending with the reason. |
| `skill-draft-reject DRAFT_ID [--reason R] [--json]` | Turn a draft down so it is not offered again. | Same attended-only guard as `approve`. |

### Files

| Command | Purpose | Guard |
|---|---|---|
| `file surface PATH` | Deliberately open a workspace file — one you wrote, read, or a subagent produced — in the user's pinned preview panel. | The path must already exist under the workspace (`file_not_found` otherwise). The call only validates the path and requests the pin; `viewers` and `stream_state` in the reply don't prove the panel opened or stayed closed. `pinned: false` with `dismissed: true` means the user closed this file earlier, so it was not reopened: say so and tell them to pin it from the file viewer's ⋯ menu. |

### Chats

| Command | Purpose | Guard |
|---|---|---|
| `chat list [--project ID] [--limit N] [--offset N] [--full]` | List active and archived chats in the workspace or one project, newest activity first. Default is 20 compact rows (`chat_id`, `title`, `project`, `provider`, `archived`, `last_activity_at`, and `last_response` cut to 200 characters); `--limit` is 1-200. | The reply carries `total`; when more rows remain it also carries `truncated: true` and `next_offset`, so pass `--offset` with that value for the next page. `--full` returns full rows (still paginated). |
| `chat get [--chat ID] [--full]` | Get one chat's metadata plus its last 10 messages, each capped at 2,000 characters (`truncated: true` and `content_chars` mark a cut message; `messages_total` counts all of them). | Omit `--chat` for the calling chat. `--full` returns every message untruncated. |
| `chat create [--project ID\|NAME] [--title T] [--provider P] [--model M] [--mode MODE] [--prompt TEXT]` | Create a chat, optionally sending its first prompt in the same call. | Omit `--project` to reuse the calling chat's own project — no need to list projects first for a sub-topic. |
| `chat update [--chat ID] [--title] [--provider] [--model] [--mode] [--thinking-level] [--project] [--code on\|off]` | Update chat metadata and same-backend model settings. | Omit `--chat` for the calling chat. `--code on` needs a project linked to a repository; the reply then carries `repo_instructions` (follow them now) and a `note`. Your own turn continues automatically once it ends, with the repository's skills and write access loaded; do not ask the user to send another message. `--code off` is refused once the chat has messages (code mode is one-way). Refused in plan mode. |
| `chat send [--chat ID] --prompt TEXT` | Start or queue a user turn in a chat. | Omit `--chat` for the calling chat; pass it to reach another chat in this workspace. |
| `chat continue --chat ID` | Continue an archived chat as a new active chat. | — |
| `chat retry [--chat ID] [--action set\|stop\|try_now] [--prompt TEXT] [--kind network]` | Manage a deferred retry. | Default action is `try_now`. `--action set --kind network` (unattended runs whose job could not reach an external service): re-runs this chat's prompt after 10, then 20, then 40 minutes, at most 3 times; a refusal `retry_exhausted` means report the failure. |
| `chat handover [--chat ID] [--provider P] [--model M] [--messages @file.json]` | Move a chat to a fresh provider session, optionally carrying visible history. | With both `--provider` and `--model` empty, it only clears the current session in place. |
| `chat reload [--chat ID]` | Restart the provider so MCP servers, plugins and skills added or changed since the chat started are loaded. | Same session, nothing is lost. On the calling chat it runs when the turn ends and the conversation continues automatically: end your turn, never ask the user to reconnect. Refused with `chat_busy` while background agents or a question form hold the provider. Use it after `claude mcp add`/`opencode mcp add`, a redeployed MCP server whose new tools you cannot see, an installed plugin, or a new/edited skill. |
| `chat archive [--chat ID]` | Archive a chat to the vault and trigger normal post-archive memory extraction. | — |
| `chat delete [--chat ID]` | Delete a chat. | Destructive; deleting the calling chat itself is deferred until the turn finishes. |
| `chat stop --chat ID` | Stop another chat's active provider turn. | Destructive; a chat cannot stop its own turn. |
| `chat terminal-offer --command-file FILE` | Suggest a terminal command for this chat: the user reads it, copies it and runs it themselves. The user's terminal may open in the linked repository, so a command whose effect depends on the folder it runs in (a project-scoped MCP add) starts with a `cd` into the workspace folder. | `FILE` holds the command text (`@file.txt` or a plain path). There is no `--chat` and no inline command: the suggestion belongs to the calling chat. The text is untrusted, model-authored guidance — Ciaobot does not verify it and does not claim it is safe, so put a placeholder in it, never a real token or password. Nothing is executed, no terminal grant is obtained and no output is collected. For a provider Ciaobot already supports, prefer the Settings flow for its own login or install step. |

### Projects

| Command | Purpose | Guard |
|---|---|---|
| `project list [--include-completed]` | List projects in the active workspace. | — |
| `project get [ID\|NAME]` | Get one project by id or name. | Omit for the active project. |
| `project create --name N [--context TEXT] [--repo PATH]` | Create a project. | Also creates `projects/active/<slug>/<slug>.md` with valid frontmatter and binds it; edit that note, do not write a new one. Skipped when the folder name is taken. `--vault-folder` is not a create-time flag; set it with `project update` after creating. `--repo` links a local git checkout and must be its top level (relative paths resolve against your shell's directory). |
| `project update [ID\|NAME] [--name] [--context] [--vault-folder] [--repo PATH]` | Update project metadata or its safe vault-folder binding. | Omit `ID` for the active project; an omitted flag keeps its current value. `--repo ""` unlinks. `project get`/`list` report `repo: {path, branch, dirty, missing, instructions}`. |
| `project restore STEM` | Restore a completed vault project into the active workspace. | Takes the completed project's *stem*, not a project id. |
| `project complete ID\|NAME` | Move a vault-backed project to completed and archive its active record. | Destructive. |
| `project delete ID\|NAME` | Delete a non-vault-backed project and its chats. | Destructive. |

### Tasks

| Command | Purpose | Guard |
|---|---|---|
| `task list [--completed-since ISO] [--completed-before ISO]` | List this workspace's board: valid tasks in board order (dated first), then one row per file that is not a readable task, carrying `code` instead of task fields. The two bounds keep tasks with a completion in `[since, before)`, by when they were completed, not last edited; a task never completed is left out of a filtered list. | Read-only. A malformed file is always reported when no bound is given, never dropped, so an empty-looking board is never the truth. |
| `task get TASK_ID` | Get one task, description/links body included. | Read-only. |
| `task create --title TITLE [--body-file FILE.md] [--project P] [--due YYYY-MM-DD]` | File a task. `--project` takes an id or a name and must belong to this workspace; `--due` is a calendar day, never an instant. | The body is Markdown prose, so it travels as a file — never as a shell argument. A project in another workspace is `project_not_found`. |
| `task update TASK_ID --revision REV [--title T] [--body-file FILE.md] [--due D] [--assignee user\|agent] [--status STATUS]` | Edit one task; only the fields passed change. `REV` is the `revision` you read. | `task_revision_conflict` (retryable) means the file moved: re-read and re-plan, never resend the same revision. An unknown field is refused. |
| `task delete TASK_ID --revision REV --reason-file FILE.txt` | Permanently remove one task, at the revision you read. Only that record goes, including the description and any updates, delegation log and completion history stored inside its file. The reason is required: why the user asked, in their words. An empty one is refused. The engine logs the deletion and the reason in `Workspace/Task-Decisions.jsonl`, and the weekly rules pass learns from it. | Destructive, with no trash: the file cannot be restored. Only on a turn the user typed: a scheduled, webhook, delegated-task or other automatic turn is refused (`unattended_forbidden`); ask the user to request it in the chat themselves. Only when the user clearly asked you to delete that task; say what will be lost first, and tell them the removal is permanent. Chats, running turns and attempt history are left alone — this is neither `chat delete` nor task completion. Delete only by the task's 32-hex id: never by title or chat id; run `task list`/`task get` first, and ask if the target is ambiguous. A stale `REV` removes nothing (`task_revision_conflict`). |
| `task move TASK_ID --to STATUS --revision REV` | Set the column: `backlog` (To do), `in_progress`, `in_review`, `done`. A delegated task reaches `in_review` through your `task report --outcome done`. | Same revision guard. `--to done` is refused exactly like `complete` — see below. |
| `task complete TASK_ID --revision REV --reason-file FILE.txt` | Mark a task done because the user told you it is. The reason is required: why it is done, in their words; it is stored as the completion's resolution, logged in `Workspace/Task-Decisions.jsonl`, and the engine learns from it. | Only when the user clearly said the task is done. Only on a turn the user typed (`unattended_forbidden` otherwise). The only way for you to close a task: `task move --to done` and `task update --status done` are refused (`task_completion_requires_user`). A task linked to a chat or a delegated run, including one ready for review, is refused too: the user approves those on the board. |
| `task delegate TASK_ID --revision REV [--project P]` | Hand a task to the agent as one ordinary chat. The chat runs in `--project` (or the task's own project, else General) and the prompt is the task's own description — there is no prompt argument. | The turn is **attended**: an approval card it raises is an ordinary Needs-you card in that chat, answered the ordinary way. One live attempt per task: a second call returns the attempt already running and starts nothing. Delegating is not completing — a finished turn puts the task in front of the user for review. |
| `task add-update TASK_ID --revision REV --text-file FILE.md` | Append one progress note: a milestone, a blocker, or a finished step. The note is not a copy of the chat, and it does not complete the task. | Only while the task is in progress or in review. If another chat holds the live attempt, or the chat is not linked to the task, this is refused (`task_report_not_holder`). A chat linked with `task link-chat` may also write. A stale `REV` writes nothing. |
| `task link-chat TASK_ID --revision REV [--chat CHAT] [--remove]` | Record that a chat (default: this one) worked on the task, outside delegation. The board lists it under the task; a linked chat may add progress notes. | Not a delegation: it never moves, assigns or completes the task. At most 20 chats. A stale `REV` writes nothing. |
| `task report TASK_ID --outcome done\|blocked\|needs_input --summary-file FILE.md` | **When you were delegated a task**, say how far you got, once, before your turn ends. `done`: finished, for the user to review. `blocked`: you cannot go on. `needs_input`: you end the turn still waiting on the user's answer (ask it with your question tool first, and carry on in this turn if they reply). The summary (Markdown: what you did, where the results are, what is left) is saved on the task and handed to any later attempt. | Only the chat holding the task's live attempt may report (`task_report_not_holder` otherwise). It never moves or completes the task. A turn that ends **without** a report is shown to the user as *Unfinished*, not as a result to review. |
| `task attempt ATTEMPT_ID ACTION [--reason-file FILE.txt]` | `stop` (ends the running turn; irreversible), `resume` (continues the **same** chat under the **same** attempt), `retry` (starts a **new** attempt in a new chat, leaving the old one as history) or `detach` (stops the turn and clears the linkage, which is what makes the task completable again). | `stop` is destructive and is asked for like one. `resume` only works on an attempt that did not finish — a `ready_for_review` result is waiting for the user's decision, so continuing it is their call. None of these completes a task. `--reason-file` applies to `detach` of a `ready_for_review` result only; any other use is refused (`invalid_action`). |

The user's tasks are this board, not your provider's todo tool. Asked to delegate
a task, run `task delegate` rather than doing the work in this turn. When you work
a task yourself, `task link-chat` it to this chat, `task add-update` a milestone
(not a chat dump), then `task move` it to `in_review`.

Deleting a task is a user decision you may carry out on their request, in an
attended turn. A scheduled turn is refused (`unattended_forbidden`): report
that the user must ask for the deletion themselves rather than looking for
another route. Read the task first with `task get` and pass that exact
`revision`; say plainly that the record is permanently removed. A chat id or
title is not a task id — resolve it through `task list`/`task get`, and ask the
user which task they mean if more than one could match. Deletion removes only
the task record: it does not delete or archive linked chats, stop a running
turn, discard attempt history, or complete the task.

To point the user at a task — in a reply, a daily log, any vault note — link it
as `[Reply to Ivo](/tasks/<id>)` with the full 32-hex `id` from `task list`.
The PWA opens that task's detail on the board, in place. A short prefix is not an
id and renders as a dead link, and a link to the task's `Workspace/Tasks/` file
only shows its raw Markdown.

Two things about the board you cannot see from a single `task get`, all of
which are the user's decision rather than yours to resolve:

- **A finished turn is not a finished task.** Only a turn you ended with
  `task report … --outcome done` becomes `ready_for_review`, which means the
  result is waiting for the user. Report it, name the task, and stop — the
  `Approve Done` in the board is the completion of a delegated task, and
  `task complete` is refused to you for a task linked to one.
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
| `schedule update ID ...flags` | Update an existing schedule; only fields you pass change. | A system schedule (`scope=system`) only accepts workspace, model, provider and archive-policy changes — anything else raises `system_schedule_read_only`. Pause or resume one with `schedule pause|resume`; there is no `--enabled` flag. |
| `schedule pause ID` | Pause a schedule without deleting it. | Destructive. |
| `schedule resume ID` | Resume a paused schedule. | Destructive. |
| `schedule run ID` | Dispatch a schedule immediately through the normal chat pipeline. | Destructive. |
| `schedule delete ID` | Delete a removable user schedule. | Destructive; a system schedule cannot be deleted (`schedule_not_removable`). |

Shared `...flags` for `schedule create/update/preview`: `--prompt --daily-time --timezone --frequency {daily,weekly,monthly,manual,once,interval} --interval-minutes --days-of-week mon,tue --day-of-month --run-at-date --project --title --description --provider --model --archive-policy {manual,auto} --command`. `--command` is operator-gated, see Calling convention. There is no `--chat` binding flag — schedules bind to a project and workspace, never to one chat.

### Webhook triggers

A trigger is a URL an external sender POSTs to. Ciaobot answers the event with a
durable receipt and launches an ordinary chat in the trigger's own project — the
sender never chooses a chat, a model, a mode or a workspace, and the turn is not
unattended, so an approval it raises is an ordinary approval card.

| Command | Purpose | Guard |
|---|---|---|
| `webhook list` | List this workspace's triggers as public records. | Read-only. Never a secret: only a hash of it is stored. |
| `webhook create --name NAME [--instructions-file FILE] [--project P]` | Configure a trigger and mint its secret. | Returns the trigger **and its secret, shown once**: hand it to the sender now, because only its hash is kept and it cannot be read back. Created **disabled** — nothing authorizes until `webhook update --enable`. |
| `webhook update ID --revision REV [--name N] [--instructions-file FILE] [--enable\|--disable]` | Edit one trigger; only the fields you pass change. `--enable`/`--disable` is the off switch, and it reaches an event accepted a moment earlier. | `webhook_revision_conflict` (retryable) means the trigger moved: re-read and re-plan. The target and the mode are not editable — delete and recreate instead. |
| `webhook rotate ID --revision REV` | Replace the trigger's secret and return the new one, shown once. | **Revokes on rotate**: the old secret stops authorizing immediately. Keeps `enabled` as it was; rotating is not a way to enable a disabled trigger. |
| `webhook delete ID --revision REV` | Delete the trigger and destroy its verifier. | Destructive and irreversible: the sender needs a new trigger. Recorded receipts for past events are kept. |

The sender side is not this CLI: an external system calls
`POST /hooks/v1/<trigger_id>` with `Authorization: Bearer <that secret>` and an
`Idempotency-Key`, and a body of exactly `{"text": "..."}`. `202` means the event
is **recorded**, not that a turn finished. Both recipes are in `PWA_API.md`.

A trigger is created disabled, so ask the user before `webhook update --enable`,
then tell them the sender needs the secret as a bearer token.

Instructions are operator prose that the launched turn reads as its own, so
they travel as a file (`--instructions-file`) rather than as a shell argument.

### Workspace and status

| Command | Purpose | Guard |
|---|---|---|
| `context get` | Return the active workspace, project, chat, provider, and control surface, plus local server/startup status. | — |
| `context logs [--lines N]` | Installed version plus recent failed background jobs whose recorded workspace is exactly the active one, already redacted, capped and fenced as untrusted data. Engine-wide error/debug/service logs are omitted (`scope`/`omitted` say why) because they have no immutable workspace provenance; `lines` (1–1000) is retained but no longer caps a log tail. | Read-only; works under workspace-only file access. Never assume the omission means there were no engine errors. |
| `gws status` | Report whether the active workspace's Google Workspace account is connected and its token is valid. | Read-only; never triggers a live re-auth check — use it to warn the user their login expired, not to force one. |
| `workspace list` | List all configured logical workspaces — names, vault roots, defaults — not just the active one. | — |
| `repo candidates` | List git checkouts one level under ~/repos, ~/code, ~/src, ~/dev, ~/Developer, ~/projects, to offer for `project update --repo`. | Read-only; a checkout elsewhere can still be linked by path. |

## Examples and errors

Worked examples per command family and the error-code catalogue (`unattended_forbidden`, `task_revision_conflict`, `webhook_*` and the rest) are in `reference.md` beside this file; `ciao help` prints it after this document.
