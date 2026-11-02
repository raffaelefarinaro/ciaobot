# ciao CLI reference

Worked examples and the error catalogue for the `ciao-cli` skill. `SKILL.md` has the command syntax; `ciao help` prints both.

## Worked examples

Memory:
```bash
cat > fact.txt <<'EOF'
<the fact, in the user's words>
EOF
ciao memory-proposal-add --kind memory --text-file fact.txt
```
The fact travels in a file, never as a shell argument. `memory update --entry` is only for text you wrote yourself, such as consolidating existing entries. This admin command does not return the JSON envelope; it prints:
```
Queued [memory] proposal in <workspace>/Workspace/Memory-Proposals.md.
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
# creates projects/active/q4-launch/q4-launch.md and binds it
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

Workspace and status:
```bash
ciao context get
```
```json
{"ok": true, "data": {"workspace": "personal", "project": "General", "chat_id": "chat_91ab", "system": {"server": "up"}}}
```

## Common mistakes

- **`code_mode_unavailable`**: `--code on` in a chat whose project has no linked repository, or whose repository folder is missing. Link one with `project update --repo PATH`, or ask the user.
- **`file_not_found`** — `file surface` only opens a path that already exists under the workspace root. Check the file was actually written (or that the path is relative to the workspace, not absolute or outside it) before surfacing it.
- **`memory_update_invalid` / `invalid_action`** — every `action`-style flag across this CLI is a closed enum, not free text (`memory update` takes `add`/`replace`/`remove`, never `append`). Run `ciao memory update --help` (or the equivalent `--help` on any command) to see the exact accepted values before guessing.
- **`unattended_forbidden`** — `vault review keep|trash|restore|complete|restore-completed|delete` only resolve during an attended turn. Running as a schedule or other unattended automation, don't attempt the mutation: report the candidate and its evidence instead, and let an attended turn decide.
- **`note_type_unknown` / `note_folder_required` / `note_exists` / `note_path_invalid` / `note_file_invalid`** — `note create` writes nothing on any of these. Pick `--type` from the valid list in the message, or queue a new-category question; a category with no folder needs `--path DIR`. `note_exists` means the note is already there: edit it instead. `note_path_invalid` is a `--path` that is not a plain vault-relative folder; `note_file_invalid` is a prose file that is unreadable, not UTF-8 or over the size cap.
- **`payload_required` / `payload_invalid` / `payload_too_large`** — `note verify` takes a path, and the server reads and bounds the document. An empty flag, a path outside the workspace, malformed JSON, an `outcome` outside `still_valid|update|retire|unverified`, or a payload over the cap are all refused before anything is written. Put long evidence in the vault and cite its path instead of pasting it.
- **`workspace_forbidden`** — a `note verify` payload may not name a workspace other than this chat's. The check state and the note-edit proposal are filed per workspace; pairing one workspace's name with another's vault would record a verdict about a vault nobody claimed. Drop the `workspace` field or make it match.
- **`task_revision_conflict`** — the task file changed since you read it. Nothing was written. Run `task get` (or `task list`) again, re-plan your edit against what is there now, and send the new `revision`; resending the old one is the same refusal.
- **`task_completion_requires_user`** — you may close a task only with `task complete --reason-file`, when the user clearly said it is done. This means you gave no reason, the task is linked to a chat or a delegated run, or you tried a status route (`task move --to done`, `task update --status done`). Do not retry another route and do not edit the file: say which task is finished and let the user close it.
- **`task_not_found`** — a task id that is well formed but absent *from this workspace*. Another workspace's task is answered exactly this way, so this is also the answer when you reached for an id from the wrong vault.
- **`webhook_revision_conflict`** — the trigger changed since you read it (`webhook list`). Nothing was written: re-read and re-plan instead of resending the same `--revision`.
- **`webhook_not_found`** — no trigger with that id **in this workspace**. A trigger belonging to another workspace answers exactly this way, so do not look for it in another workspace; report the id back and let the operator reconcile it.
- **`webhook_invalid`** — the store refused one field: a blank name or a `--revision` that is not a number. Enabling a revoked trigger is also refused this way: rotate its secret first.
- **`unauthorized` on the sender's POST** — the trigger's secret does not authorize right now. Missing, disabled, revoked and wrong are one answer; if the secret was rotated, the old one is dead by design and the sender needs the new one, which `webhook create`/`webhook rotate` showed once.
