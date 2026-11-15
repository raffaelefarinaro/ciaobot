## Tasks

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
