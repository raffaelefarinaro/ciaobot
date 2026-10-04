# PWA API

Local HTTP API for the Ciaobot PWA. Default base URL is `http://localhost:$PWA_PORT` with `PWA_PORT=8443`.

The route source of truth is `ciao/web/app.py`. This file is kept in sync by `tests/test_pwa_api_docs.py`.

## Auth And Browser Security

- Password protection is on by default. `PWA_AUTH_TOKEN` is the dashboard password: the first-run wizard asks for it, `POST /api/auth/settings` changes it, and only `PWA_AUTH_REQUIRED=false` in the workspace `.env` turns protection off.
- `POST /api/auth` accepts `{"token": "<PWA_AUTH_TOKEN>"}` and returns an HttpOnly `ciao_session` cookie.
- `GET /?setup=<token>` is the local first-launch shortcut path. It is accepted only on `localhost`, `127.0.0.1`, or `::1`; when the token matches `.runtime/setup-token`, the server sets the same signed `ciao_session` cookie, deletes the token file, and redirects to `/`.
- Production cookies are `Secure`, `SameSite=Lax`, and host-only (scoped to the exact host that served them).
- `POST /api/auth/logout` clears the same host-only cookie.
- All `/api/*` routes except `POST /api/auth`, `GET /api/auth/check`, `GET /api/startup-status`, `GET /api/active-chats`, `GET /api/setup-status`, `POST /api/setup/finish`, `GET /api/setup/list-dirs`, and `POST /api/setup/mkdir` require the signed session cookie. (`GET /api/setup/inspect-folder` is not middleware-exempt, but it only answers in bootstrap mode, where protection is off anyway.) All `/ws/*` routes require the signed session cookie.
- Node mode is gone: there is one engine per install and a browser either talks to it directly or not at all. There is no second origin, no local-control capability header, and no session bridge between origins. `docs/REMOTE_BOUNDARY.md` records what replaced the old model.
- `POST /api/setup/finish` is only accepted in bootstrap mode from localhost with a matching browser origin/referer (off-localhost requests get a 403 pointing at `http://localhost:<port>`). Body: `workspace` (required — the root folder holding the vault plus app data), `vault_root` (optional, default `<workspace>/memory-vault`; absolute or `~` paths are honored for an existing notes folder elsewhere), `password` (required — the dashboard password, at least 4 characters; setup always enables protection), plus optional `vault_mode`, `workspace_name`, `push_contact`, `port`, `python`, `launch_agents_dir`, `app_dir`, and `restart`. It writes the real workspace config, ensures workspace and vault are (in) git repos, creates local launch artifacts, and asks the supervisor to restart into the configured workspace. When the chosen folder already contains nested workspace directories (`memory-vault/<name>/` with a `MEMORY.md` inside), those are adopted as the workspace registry and `workspace_name` is ignored.
- `GET /api/setup/list-dirs`, `POST /api/setup/mkdir`, and `GET /api/setup/inspect-folder` back the setup wizard. They are only accepted in bootstrap mode from localhost with a matching browser origin/referer (404 outside bootstrap mode, 403 off-localhost). The folder picker (`list-dirs`, `mkdir`) lists directories only and never reads file contents. `inspect-folder?path=<dir>` returns `{mode: "scratch"|"existing", vault_root, existing_workspaces, has_env}` so the wizard can hide the "First Workspace" text field when nested workspaces are already present.
- State-changing `/api/*` requests with an `Origin` or `Referer` header must match the request host. Missing headers are accepted for non-browser clients.
- HTTP responses include baseline security headers, including CSP, `X-Content-Type-Options`, `Referrer-Policy`, and frame denial.
- `POST /agent/v1/{op}` is the agent CLI's loopback transport (`ciao <noun> <verb>` inside a managed provider shell, see `docs/ARCHITECTURE.md` → `agent_surface.py` and `docs/AGENT_CLI.md`). It takes a scoped bearer capability (`CIAO_AGENT_TOKEN`) in an `Authorization: Bearer` header, runs the registered control-plane operation, and returns the same JSON envelope; it is not a browser or curl API and does not accept the session cookie.
- `GET /api/import/sources` and `POST /api/import/preview` are the import consent surface (#1029). Both take the ordinary signed session cookie and are in neither the public nor the loopback-only allowlist, so a signed-in browser — including a second device — can ask the engine what past conversations its own workspace holds and what a chosen selection would process. Discovery is metadata only and resolves its roots from configuration alone, and the preview takes `{provider, source_id}` pairs rather than paths, so a request cannot name a file outside the workspace's known sources; an OpenCode id additionally has to be one that workspace's own `session list` named, because the CLI resolves ids across projects, and that check runs before anything is exported. Neither route reads a Claude account export, a cache or a cookie store, neither sends an absolute session path to the browser, and neither sends transcript text to the browser.
- `POST /hooks/v1/{trigger_id}` is the **webhook receiver**, and it is a machine surface: not `/api/*`, so `AuthMiddleware` does not guard it and it does not accept the session cookie (a valid `ciao_session` authorizes nothing here). It takes one webhook trigger's own secret in an `Authorization: Bearer` header — the secret `POST /api/webhooks` shows once — an `Idempotency-Key` header, and a body of exactly `{"text": "..."}` (the trigger's `event_text` input policy; any other key, including one naming a workspace, project, model, mode or path, is a 400). `POST` only: any other method is a 405 with `Allow: POST`, refused ahead of the bearer check rather than answered by the SPA catch-all. A foreign or `null` `Origin` on this state-changing request is a 403. Success is `202` with `{receipt_id, trigger_id, status}`, `status` being `accepted`: **the event is recorded, not dispatched** — no chat is created and no model turn runs, so no recipe, skill or capability may claim that one does. Refusals use `{"ok": false, "error": {code, message, retryable}}`: 401 without a valid secret (missing, disabled, revoked and wrong are one answer), 400 for a bad body or key, 409 for the same key with a different body, 413 over 65536 bytes, 429 over 10 attempts a minute per trigger (with `Retry-After`), 503 when the trigger already has 20 receipts still awaiting launch, and 500 when the trigger store itself cannot be read. See `docs/WEBHOOK_TRIGGER_STORE.md`.

## Routes

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth` | Login with `PWA_AUTH_TOKEN` |
| POST | `/api/auth/logout` | Clear session cookie |
| GET | `/api/auth/check` | Verify current session |
| GET, POST | `/api/auth/settings` | Read protection state, or set/change the PWA password (cannot disable protection) |
| GET | `/api/projects` | List projects |
| POST | `/api/projects` | Create project |
| PATCH, DELETE | `/api/projects/{project_id}` | Update or delete project |
| POST | `/api/projects/reorder` | Reorder a workspace's projects (drag-to-reorder) |
| POST | `/api/projects/{project_id}/complete` | Complete a vault-backed project |
| GET | `/api/projects/completed` | List completed projects (vault `completed/` scan) |
| POST | `/api/projects/completed/restore` | Restore a completed project to active |
| GET, POST | `/api/projects/{project_id}/chats` | List or create project chats |
| GET, POST | `/api/projects/{project_id}/files` | List or upload project files |
| POST | `/api/desktop-drop` | Consume a native app's single-use Finder-drop grant; returns bounded opaque file references |
| GET | `/api/chats` | List all chats |
| GET | `/api/menubar-chats` | Compact chat list for the loopback-only local feed (legacy route name, no native client) |
| POST | `/api/chats/read-all` | Mark all chats read |
| PATCH, DELETE | `/api/chats/{chat_id}` | Update or delete chat |
| POST | `/api/chats/{chat_id}/new` | Start a new provider session |
| POST | `/api/chats/{chat_id}/handover` | Continue chat on a fresh provider session |
| POST | `/api/chats/{chat_id}/fork` | Fork chat continuing from a completed turn |
| POST | `/api/chats/{chat_id}/archive` | Archive chat |
| POST | `/api/chats/{chat_id}/continue` | Create a new active chat continuing from this archived one |
| POST | `/api/chats/{chat_id}/read` | Mark chat read |
| POST | `/api/chats/{chat_id}/unread` | Mark chat unread on purpose ("come back to this"); clears the read stamp and emits a cross-device `chat_unread` event |
| POST | `/api/chats/{chat_id}/retry` | Set, stop, or run deferred chat retry |
| POST | `/api/chats/{chat_id}/stop` | Stop an in-flight turn; HTTP fallback for the websocket `stop` message, for when that chat's socket is disconnected or mid-reconnect |
| POST | `/api/chats/{chat_id}/prompt` | Send a prompt to start a background turn in the chat. Returns 409 `{error:"chat is archived", archived:true}` if the chat was archived; start a new chat (or `continue`) instead of retrying |
| GET | `/api/open-chat/{chat_id}` | Focus an existing chat in the PWA and report whether a live event subscriber received the navigation |
| GET | `/api/chats/{chat_id}/messages` | Load persisted chat messages |
| GET | `/api/chats/{chat_id}/messages/part` | Fetch one full history row by absolute index (lazy expansion) |
| GET | `/api/chats/{chat_id}/subagents` | Load subagent transcripts. `?agent_id=` narrows to one agent (bare or `agent-`-prefixed) and skips reading the siblings — what the read-only subagent view polls |
| GET | `/api/subagents/running` | Live subagents per working chat (metadata only), for the sidebar's subagent rows |
| POST | `/api/chats/{chat_id}/images` | Upload chat images |
| POST | `/api/chats/{chat_id}/attachments` | Upload chat files; supported documents become Markdown in the active project folder; the browser receives bounded `file_refs`, not server paths |
| GET | `/api/images/{ref}` | Read uploaded image blob |
| GET | `/api/workspace-file` | Read allowed text file |
| POST | `/api/workspace-file` | Write user-edited text file (allowlist + snapshot) |
| GET | `/api/workspace-html` | Render an `.html` artifact as `text/html` in a sandboxing CSP (panel Preview) |
| GET | `/api/workspace-image` | Read allowed image file |
| GET | `/api/workspace-binary` | Read allowed binary file |
| GET | `/api/libreoffice-status` | Whether LibreOffice (`soffice`) is available to render `.pptx` previews |
| POST | `/api/libreoffice-install` | Install LibreOffice via Homebrew Cask (macOS); no restart needed |
| POST | `/api/workspace-open` | Open a workspace file with the OS default app on the machine running Ciao; loopback clients only (403 otherwise) |
| GET | `/api/file-history` | List snapshots for a `(chat_id, file_path)` |
| GET | `/api/file-content` | Read one snapshot's content |
| GET | `/api/vault-markdown-paths` | List workspace-relative markdown paths (file viewer resolves Obsidian wikilinks) |
| GET | `/api/vault/backlinks` | List notes whose wikilinks resolve to a given markdown path |
| GET | `/api/vault/graph` | Vault-wide note graph (frontmatter `related:` + `[[wikilinks]]`) for the Memory Map page; optional `?workspace=` scopes to one logical workspace | Each node also carries `check`: the managed verification's state for its current revision, and a check that already answers it **clears** the node's `stale` flag (a note checked inside its 30-day cooldown, or one a proposal is waiting on, is not unchecked however old its own `updated:` reads), so the map does not send the operator to a queue that has deliberately stopped asking. `check.settled` says the revision is answered, `check.pending` that a proposal is waiting for a person, and `check.conflicted` that the pinned proposal can no longer be applied and the note is due again; `check: null` means nobody has checked it.
| GET, POST | `/api/vault/review` | List explainable note-review candidates (`?include=trashed,cleared` also lists the reversible trash inventory and the kept notes still in the vault) or record an explicit keep/restore decision; trash, permanent deletion, `complete` and `reopen` are separate actions, with permanent deletion requiring a trashed candidate and exact confirmation. `reopen` undoes a keep by appending to the ledger, putting the note back in the queue. `complete` closes a **project** candidate out: it moves the note to `projects/completed/` — the whole folder for a `projects/active/<slug>/` project, whatever note in that folder was the candidate — rewrites `status: active` to `status: completed`, and repoints every `related:`/wikilink/markdown reference to any note in the moved project, in one transaction that rolls the move and every link back if the ledger append fails; `restore_completed` reverses one such row, and a project already restored cannot be restored again. Every candidate in the payload carries `completable`, the backend's own answer to "will `complete` be honoured for this row" (true only for a project that still has somewhere to complete into): a client should offer Complete in place of Retire when it is true rather than re-deriving project-ness from `evidence.type`, or it will offer a button the engine refuses. Retire (`trash`) stays available for every type, including a project that is wrong or abandoned. A successful POST answers `{ok, result, candidates, trashed, cleared}` — the queue it had to rebuild anyway, so a client never needs a follow-up GET (candidate generation reads every note in the vault three times) | A candidate the managed verification pass has already looked at carries the check state for its **current** revision (`evidence.verification`: the outcome, when the check ran, its coverage, the citations it rested on, and the cooldown), and a check that already answers the revision clears the `unverified` signal — so the queue, the Memory Map and the nightly curation pass cannot disagree about which notes are still due. When a `note_edit` proposal is waiting on a person for that exact revision, the row also carries `pending_verification` (the queue row and the sidecar id, with the outcome, coverage, citations and reason) and `retirement_offered`: a client should link the proposal rather than offer a second `Still true` / `Retire` on the same revision, and must keep `Retire` when another signal (unlinked, duplicate, superseded wording) is an independent finding. A proposal pinned to a revision the note has left comes back as `pending_verification: null` with`evidence.verification.conflicted: true` — its accept would refuse as a conflict, so the row's own actions are the only route left. `checked_at` is when the check RAN and is not the note's own `updated:`; a verdict that came back `unverified` writes nothing, so a client must not collapse the two into one date.
| DELETE | `/api/vault/note` | Permanently delete one vault note (`?path=`, the `Entry.path` string form); strips dangling `related:`/`relatedTo:` and `[[wikilink]]` references from every note that linked to it first |
| POST | `/api/file-restore` | Restore a snapshot to disk |
| GET, POST | `/api/schedules` | List or create automations of any cadence, including `frequency: "interval"` |
| POST | `/api/schedule-run/{schedule_id}` | Run now. 409 for an interval entry whose target chat has a turn in flight (refused, not queued) |
| PATCH, DELETE | `/api/schedules/{schedule_id}` | Update, pause/resume (`{"enabled": bool}`), or delete |
| GET | `/api/tasks?workspace=` | One workspace's board rows (`?workspace=` is required, 400 otherwise), valid tasks in board order followed by one row per file that could not be read as a task — a row carrying `code` and no task fields, so a malformed file never reads as an empty board. A row carries `id`, `title`, `status`, `project_id`, `due`, `assignee`, `review_state`, `chat_id`, `attempt_id`, `created_at`, `updated_at`, `revision` and `relative_path` |
| GET | `/api/tasks/{task_id}?workspace=` | One task as the store reads it now, `body` included — the list above deliberately carries no description, so this is the read an editor needs before it writes one back. `?workspace=` is required (400 otherwise), an unknown id in this workspace is a 404 |
| POST | `/api/tasks` | File one task: `{"workspace", "title", "body", "project_id", "due"}`. `project_id` takes an id or a name and must be a project of this workspace (400 otherwise). Answers 201 with the record as stored, `body` and `revision` included |
| PATCH | `/api/tasks/{task_id}` | Edit one task at `expected_revision` (required, 400 without it): only the fields sent change — `title`, `status`, `project_id`, `due`, `assignee`, `review_state`, and `body` replacing the description wholesale. Any other key is a 400. A stale revision is a **409** and writes nothing |
| DELETE | `/api/tasks/{task_id}` | Remove one task record at `expected_revision` (required, 400 without it). The record is the user's own Markdown file and this unlinks it — there is no trash. A stale revision is a 409 and the file stays |
| POST | `/api/tasks/{task_id}/complete` | Mark one task `done` at `expected_revision` (required). This is the signed-in user's own session, so completion is theirs to make here; the same operation through the agent CLI is refused `completion_requires_user`. A task linked to a live chat or attempt is refused too — the delegation service owns that |
| GET | `/api/webhooks?workspace=` | List a workspace's webhook triggers (public records only, never secrets) |
| POST | `/api/webhooks` | Create a webhook trigger; returns the trigger plus its one-time secret |
| PATCH | `/api/webhooks/{trigger_id}` | Update a trigger's name, instructions, or enabled flag (revision-checked) |
| POST | `/api/webhooks/{trigger_id}/rotate` | Rotate a trigger's secret; returns the trigger plus the new one-time secret |
| DELETE | `/api/webhooks/{trigger_id}` | Delete a trigger and its verifier (`?expected_revision=`, revision-checked) |
| GET | `/api/import/sources?workspace=` | List the past conversations this workspace's known sources hold, **as metadata only**: offered refs (an id and its weakest locator, no absolute path), excluded rows with their reasons (Ciaobot's own / unreadable / over cap), sources that could not be listed (an OpenCode below the V2 floor), and per-source `truncated` when a listing cap was reached. Opens no conversation and returns no text |
| POST | `/api/import/preview` | The pre-extraction confirmation for the **selected** sources only: per-conversation counts, the source's own first date when it has one, the reader's omission record, the effective provider and model, an input-volume estimate, `batch_cap` and the destination workspace. Body is `{"workspace", "sources": [{"provider", "source_id"}]}` — ids, never paths; an OpenCode id must be one this workspace's own listing named, or the row comes back `unreadable` with no export run. Reads the selected files and returns no transcript text; it extracts nothing and stores no batch — `POST /api/import/batches/{batch_id}/run` is the route that runs the extraction |
| POST | `/api/import/batches` | File an import batch over a selection (#1032, C6): body is `{"workspace", "sources": [{"provider", "source_id"}]}` (ids, never paths; at most the `batch_cap` the preview states) with an optional `destination` workspace. Refuses a Ciaobot-own session (400), a second batch while one is still open for the workspace (409), and a conversation a live batch already covers (409). Answers 201 with the batch as stored — selection, per-source digests (empty until extraction reads them), progress and provenance. No provider call, no model call, no transcript text |
| GET | `/api/import/batches?workspace=` | That workspace's import batches, oldest first, each with its selection, per-source digests, progress (`queued → running → done \| failed \| cancelled \| partial`), cancellation and per-fact provenance. Digests only, never transcript text |
| POST | `/api/import/batches/{batch_id}/run` | Extract a filed batch's selected conversations into the **review queue** (#1038, C7): body is `{"workspace"}` and a batch filed for another workspace is a 404. Answers 202 with the batch already moved to `running` — **no model turn runs inside the request**; the tool-less extraction happens afterwards in the runner. A body naming a `model` or `provider` is ignored: the run resolves the per-provider insights model for the *source's* provider (`resolve_insights_model` → `_resolve_insights_call`), which is not necessarily the `model` the preview screen showed — that one is the workspace default for the workspace's own provider. A batch already `running` is answered 200 with its current state rather than starting a second run; one already settled as done, failed, cancelled or partial is a 409, since re-running it is a new batch. Filed rows land in `Workspace/Memory-Proposals.md` with the **source** message's `[as-of:]` date and a `_(from: provider:session_id:anchor)_` tag; nothing else in the vault is written |
| POST | `/api/import/batches/{batch_id}/cancel` | Stop a batch, keeping its recorded progress and provenance; body is `{"workspace"}` and a batch filed for another workspace is a 404. Idempotent — cancelling a cancelled batch answers it unchanged — while a batch already settled as done, failed or partial is a 409. Cancellation cannot unsend provider input |
| DELETE | `/api/import/batches/{batch_id}` | Drop a batch record (`?workspace=` is required; a batch filed for another workspace is a 404). Queue and vault are untouched: filed proposals stay queued and accepted facts stay in the vault |
| POST | `/hooks/v1/{trigger_id}` | Webhook receiver (machine surface, bearer + `Idempotency-Key`, not the session cookie): records a durable receipt for one event and answers `202 accepted`. No dispatch — see `docs/WEBHOOK_TRIGGER_STORE.md` |
| GET | `/api/debug/issues` | Runtime issue report (server error log tail + failed job runs) for the dev-mode "Fix issues in chat" flow; 404 unless `CIAO_DEV_MODE` is set |
| GET | `/api/commands` | List slash commands; `?workspace=<name>` scopes them to that workspace's agent root |
| GET | `/api/agent-assets` | List subagents, slash commands, and workspace health for Settings; `?workspace=<name>` scopes the subagent and command lists to that workspace's agent root |
| GET | `/api/agent-assets/audit` | Full AI OS audit report; `status` is `healthy`, `needs_attention`, or `error` |
| GET | `/api/workspace-health` | Scan workspace/vault/discovery-file health |
| POST | `/api/workspace-health/fix` | Apply the automatic remedies (create missing scaffold files, re-link skills); returns the fresh report |
| POST | `/api/agent-assets/subagents` | Create a workspace-owned subagent and vault mirror; the body must carry `workspace` |
| PATCH, DELETE | `/api/agent-assets/subagents/{name}` | Update or delete a custom workspace-owned subagent; `workspace` in the PATCH body, `?workspace=` on the DELETE |
| POST | `/api/agent-assets/commands` | Create a workspace-owned slash command and vault mirror; the body must carry `workspace` |
| PATCH, DELETE | `/api/agent-assets/commands/{name}` | Update or delete a custom workspace-owned slash command; `workspace` in the PATCH body, `?workspace=` on the DELETE |
| GET | `/api/rate-limits` | Read Claude rate-limit snapshots |
| GET | `/api/housekeeping` | List the home-screen operator actions (detector pass; each carries `run_label`, `chat_label`, `chat_prompt`) |
| POST | `/api/housekeeping/{action_id}/run` | Perform one action's mechanical work, re-run detection, and return the fresh action list; unknown id is 404 |
| POST | `/api/housekeeping/{action_id}/dismiss` | Record a "not now" for an ask-style action (e.g. the GitHub star nudge), re-run detection, and return the fresh action list; unknown id is 404 |
| GET | `/api/update-tasks` | The "After this update" tasks this engine version supports for `?workspace=`, one row per task: `id`, `revision`, `scope`, `title`, `why`, `since_version`, `status` (the recorded lifecycle — `offered` when there is no record yet — through `dismissed`), `applicability` (`applicable`/`not_applicable`/`unknown`), `offered`, `suppressed`, and the `chat_id`/`prompt_digest`/`attempted_fingerprint`/`updated_at` of its last attempt. `applicability_checked_at` is when that row's *answer* was computed (ISO-8601 UTC), and is a different clock from `updated_at`, which is when the *record* was written — a dismissal is a decision, not a re-check. Inside the freshness window a row keeps the stamp of the call that computed the answer, so a repeated read does not re-date it. `coverage_gap` is present only when nothing is being offered, and says which of `no_eligible_task`/`not_substantiated`/`nothing_offered` applies. `?workspace=` is required (400 otherwise): applicability and state are per workspace. No `change_token`, so applicability falls back to the freshness window; the state file is still read on every call, so a dismissal or a launch shows up at once. The one write it performs is a **settlement**: a task whose record is `in_progress`/`waiting_review`/`failed` is checked against its registered completion check when this call computes its detector answer, and becomes `completed` if the check's postcondition holds — so a task the operator finished leaves Home without a separate endpoint, and its `status`/`suppressed`/`offered` in the very response that settled it. Nothing else is written and nothing is launched: no chat, no prompt, no model turn. `applicability` stays the detector's own answer even when `status` is `completed`, because the rows it counts may still be there (a review that retained them is a finished task). The settlement rides the same freshness window as the detector, so it lands within `APPLICABILITY_TTL_S` (300s) of the evidence appearing; nothing is written when the check is unregistered, raises, or says the postcondition does not hold yet. The lifecycle is revalidated under the write lock, so a `dismissed` (or reopened) decision that lands while the check is running wins over the settlement and is what this response reports; and a settlement whose write cannot land is skipped rather than raised, leaving the task in its previous lifecycle for a later listing |
| POST | `/api/update-tasks/{task_id}/start` | Start this task's chat with the **packaged** prompt for its revision, or hand back the chat the last start created. `{ok, task_id, chat_id, resumed, result, tasks}`; `resumed: false` means this call created the chat and dispatched the prompt, `true` means nothing was created (double click, second tab, retry, restart — all the same call twice). A live chat is not the same as a dispatched prompt, so the record decides what a resume does: a `failed` attempt re-sends the prompt into that same chat and still reports `resumed: true` (one chat throughout, the prompt dispatched exactly once across the failure and the retry), and a `dismissed` or reopened-`offered` record is a reopen — same chat, record written `in_progress`, nothing re-sent. 409 for a refused task (unknown id, one this engine version cannot support, no `?workspace=`, no chat manager, a state record that cannot be written before the turn starts, or a record that already says `completed` at this revision — a finished task is never re-run, and that holds whether or not its chat is still there), 500 with the `chat_id` when the chat exists but the turn could not be dispatched — which is recoverable: the next start sends the prompt into that same chat. Nothing after the turn started is a refusal: a record that will not take the `in_progress` write is logged and still answered with the chat (the record keeps saying `failed`, which the next start retries from), and a detector pass that cannot list the tasks leaves the reply without its `tasks` key |
| POST | `/api/update-tasks/{task_id}/dismiss` | Record "not this one" for this task at this revision (optional `{"reason": "..."}` body, kept as the record's evidence), suppressing the offer at this revision only and keeping the chat it was in — except a `failed` record's chat, which is live and empty because the prompt never reached it, so that one is dropped and the next start creates a fresh chat and dispatches into it. `{ok, task_id, result, tasks}`; 409 for a refused task — an unknown or unsupported id, and a record that already says `completed` at this revision, which is the same refusal a start answers and for the same reason: overwriting a verdict would put a later start back in reach of a finished task. |
| POST | `/api/update-tasks/{task_id}/reopen` | Undo that dismissal at this revision and re-offer the task. `{ok, task_id, result, tasks}`; only `dismissed` is reopened, so an offered or in-flight task succeeds and writes nothing. 409 for a refused task |
| GET | `/api/models` | List configured models, plus `providers[]` (id, labels, capabilities) from the runtime-provider registry. `?refresh=1` bypasses the provider catalog caches |
| GET, PATCH | `/api/memory/entity-types` | The vault's category list (`?workspace=` required) as `{workspace, vault, types}`, where each row is `{id, label, kind, folder, description, aliases, stale_after_days, enabled, builtin, note_count}`. `GET` returns every effective entry, disabled ones included; `note_count` is the notes carrying that `type:` (an alias counts for its category, drift under its own spelling). A `PATCH` sends the whole desired list as `{"types": [...]}` — a row whose `id` is a shipped one is a partial override of that category's default (an omitted field falls back to the shipped default; a custom row's omitted fields take the built-in defaults instead, so send the whole list), only rows that differ from the shipped default are written to `<agent vault root>/entity-types.yaml`, and the write regenerates `VOCABULARY.md` with a `## Categories` section. 400 for a malformed body, a duplicate id, two enabled categories sharing a folder, an alias that is another entry's id, a missing label, a negative `stale_after_days`, or a delete of a custom category whose notes still name it; 500 when the file cannot be written (the old file is left in place) |
| GET, PATCH | `/api/status` | Read or update status |
| GET | `/api/mcp/status` | Project MCP server inventory (env-key status + observed tools) and active-session counts (no credentials); `?workspace=<name>` scopes it to that workspace's own `.mcp.json`. Ciaobot's own surface is reported by `/api/agent/status` |
| GET | `/api/mcp/usage` | Agent surface per-operation call/error counters, plus a `window` object naming the aggregation window (lifetime totals vs. the retained detail records behind them) (no credentials) |
| POST | `/api/mcp/env-keys` | Save project-MCP env secrets into the workspace `.env` (optionally bind new keys into a server via `server`); values never returned |
| POST | `/api/mcp/servers` | Create a project MCP server in `.mcp.json`; `?workspace=<name>` writes that workspace's own file |
| PATCH | `/api/mcp/servers/{name}` | Update a project MCP server connection (and optional env keys); `?workspace=<name>` scopes the file |
| DELETE | `/api/mcp/servers/{name}` | Remove a project MCP server from `.mcp.json`; `?workspace=<name>` scopes the file |
| GET | `/api/mcp/servers/{name}/tools` | Lazy tool discovery for one project MCP server (HTTP `tools/list` probe, or observed telemetry for stdio); `?workspace=<name>` scopes the lookup |
| GET | `/api/startup-status` | Read startup phase progress |
| GET | `/api/active-chats` | List chat IDs with in-flight work (streaming or background subagents); guards a drain before an engine restart |
| GET | `/api/setup-status` | Read first-run setup checks and provider readiness |
| GET | `/api/package/status` | Read installed package version and best-effort latest GitHub release version |
| GET | `/api/package/changelog` | List commits between the installed and latest release for the update prompt |
| POST | `/api/package/update` | Return update guidance for the installed package; production engine updates go through the signed one-line installer or Settings → Home |
| GET | `/api/update/status` | Installed-engine update job: install mode, whether it can update, and the persisted operation record; `error` carries a stage/apply that was refused before it could record one (unreachable latest release, already on that version), or a record that could not be read |
| POST | `/api/update/stage` | Start staging a release for an installer-managed engine (background; poll status) |
| POST | `/api/update/apply` | Apply the staged release: drain, detached swap, restart, rollback (background; poll status) |
| POST | `/api/setup/finish` | Finish first-run setup from bootstrap mode |
| GET | `/api/setup/list-dirs` | List local subdirectories for the setup wizard folder picker (bootstrap mode, localhost only) |
| GET | `/api/setup/inspect-folder` | Probe a candidate workspace folder for vault mode and any nested workspaces (bootstrap mode, localhost only) |
| POST | `/api/setup/mkdir` | Create a folder from the setup wizard folder picker (bootstrap mode, localhost only) |
| GET | `/api/stats` | Read CLI stats |
| GET | `/api/agent/status` | Agent CLI surface status: `{ready, operations, telemetry_path, version}` for the Settings → Agent CLI panel |
| GET | `/api/workspaces` | List configured logical workspaces |
| POST | `/api/workspaces/{name}` | Add or update a logical workspace config |
| POST | `/api/workspaces/{name}/archive` | Archive a workspace: unregister it, archive its chats, take its user schedules, and move its folder intact into `<install>/.archived-workspaces/<name>-<YYYYMMDD-HHMMSS>/`. Refuses the primary or last workspace, a workspace with a running chat, and layouts it cannot move safely (409) |
| DELETE | `/api/workspaces/{name}` | Alias of `POST /api/workspaces/{name}/archive`, kept for existing scripts; it no longer deletes anything |
| GET | `/api/workspaces/archived` | List archived workspaces, newest first: `{archived: [{id, name, archived_at, path, layout, color, default_provider, gws_profile, disallowed_tools, allowed_mcp_servers, schedules, schedules_dropped, restorable, blocked_reason}]}`. The settings and `schedules` count are what a restore would apply after validation (`archive.json` syncs through git, so it is treated as untrusted); the PWA shows them in the restore confirmation |
| POST | `/api/workspaces/archived/restore` | Restore an archived workspace (`{id}`): move its folder back, re-register it, and put its schedules back **paused**. Every registry field and schedule row is rebuilt from validated metadata: invalid schedules and system rows are dropped, restored ones are pinned to the workspace, and an unreadable MCP allowlist becomes `[]`. Refuses when the name or the folder is taken, or when the archive's vault location is not inside the restored folder (409). Response `restored: {id, name, path, schedules, schedules_paused, schedules_dropped}` |
| GET | `/api/settings/providers` | Read each provider CLI's connection status |
| POST | `/api/settings/providers/{provider}/{action}` | Connect, verify, or log out through the Claude Code or opencode CLI |
| GET | `/api/integrations/gws` | Read Google Workspace CLI install, profile auth, and workspace usage status |
| POST | `/api/integrations/gws/install` | Install the `@googleworkspace/cli` (`gws`) binary globally via npm |
| POST | `/api/integrations/gws/client-secret` | Upload GCP client_secret.json for a profile |
| POST | `/api/integrations/gws/auth-url` | Generate Google OAuth authorization URL for a profile |
| POST | `/api/integrations/gws/exchange` | Complete Google OAuth flow and exchange code for credentials |
| POST | `/api/integrations/gws/disconnect` | Disconnect Google profile and clean up local credentials/client_secret |
| POST | `/api/integrations/gws/profiles/add` | Register a Google account (`name`, optional `label`) so workspaces can link to it |
| POST | `/api/integrations/gws/profiles/remove` | Forget a Google account: delete its credential directory and unlink workspaces |
| POST | `/api/integrations/gws/relogin/start` | Start a server-managed OAuth re-login; returns the consent URL and keeps a loopback callback listener alive in-process |
| GET | `/api/integrations/gws/relogin/status` | Poll a pending re-login (pending/completed/error/none) |
| POST | `/api/integrations/gws/relogin/cancel` | Cancel a pending re-login and tear down its loopback listener |
| GET | `/api/push/public-key` | Read VAPID public key |
| POST | `/api/push/subscribe` | Store push subscription |
| POST | `/api/push/unsubscribe` | Remove push subscription |
| GET | `/api/push/status` | Read push setup status |
| GET | `/api/push/subscription` | Check one subscription |
| GET | `/api/local/status` | Workspace git state: `git_repo`, current `branch` (nullable), dirty |
| GET | `/api/local/preflight` | Git preflight check for dirty files, categories, blockers/warnings |
| POST | `/api/local/handback` | Commit pending work, pull from origin, push the current branch |
| POST | `/api/local/resync` | Merge `origin/<branch>` back into the checkout |
| GET | `/api/local/backup` | Memory-backup status: `state`, `scope`, `branch`, sanitized `remote` and `last_remote`, `enabled`, `interval_s`, last attempt/success, `pending_changes`, `pending_commits`, `reason` (read-only) |
| PATCH | `/api/local/backup` | Turn the memory backup off/on (`enabled`) or pause/resume it (`paused`); persists across a restart |
| POST | `/api/local/backup/run` | Back up now, through the same serialized path the five-minute loop uses |
| GET | `/api/local/backup/setup-prompt` | The canonical setup prompt plus the trusted `context` it was rendered from (read-only, entirely local) |
| POST | `/api/local/backup/setup-chat` | Open (or re-enter) a setup chat and **send** the prompt into it; idempotent |
| POST | `/api/handover/merge` | Open an interactive chat that resolves sync conflicts on a branch |
| GET | `/api/addresses` | Where other devices can open this engine: the Tailscale Serve HTTPS URL first when one exists (`kind: trusted`, `secure: true`), then LAN/Bonjour HTTP URLs (`kind: lan`), then localhost (`kind: loopback`). Session-protected; URLs never carry a password or token |
| POST | `/api/admin/snapshot` | Git add, commit, and push snapshot |
| POST | `/api/admin/deploy` | Reinstall deps, rebuild frontend, and restart with latest code (source checkout in dev mode only) |
| POST | `/api/admin/restart` | Drain active chat work and restart the installed engine without pulling or rebuilding code (authenticated) |
| POST | `/api/admin/drain` | Close admission for new turns ahead of an engine update; returns `{draining, active_chat_ids}` (loopback-only, no session; used by `ciao update apply`) |
| POST | `/api/admin/drain/cancel` | Reopen admission after an update's drain timed out; returns `{draining: false}` (loopback-only, no session; used by `ciao update apply`) |
| GET | `/api/admin/status` | Read admin/deploy status |
| GET | `/api/service/login` | Whether the engine service starts at the next sign-in, as the machine reports it |
| PATCH | `/api/service/login` | Turn that start-at-sign-in state on or off for this engine's own service (`{"enabled": bool}`) |
| GET | `/api/admin/skills` | List skills labelled as custom or stock; merged across every agent root, or one workspace's root with `?workspace=<name>` |
| POST | `/api/admin/skills/add` | Deprecated: returns 410, replaced by `/api/skills/import` |
| POST | `/api/skills/import` | Import a skill from a validated zip (multipart `file`; validates zip-slip, one SKILL.md, frontmatter). A SKILL.md over the 15KB context budget imports with a note on `warnings`/`message` |
| WS | `/ws/chat/{chat_id}` | Per-chat streaming socket |
| WS | `/ws/events` | Global event socket |

### AI OS audit response

`GET /api/agent-assets/audit` returns HTTP 200 with the full audit report:

```json
{
  "status": "healthy",
  "total_issues": 0,
  "total_errors": 0,
  "timestamp": "2026-07-26T10:00:00+00:00",
  "setup_audit": {},
  "vault_hygiene": {},
  "skill_audit": {},
  "rule_audit": {},
  "memory_hygiene": {},
  "job_runs_audit": {},
  "scan_errors": []
}
```

`healthy` means a reliable scan found no actionable items. `needs_attention` means a reliable scan found findings. `error` means one or more required inputs could not be inspected reliably; `total_issues` includes those scan errors, while `total_errors` counts them separately. Each section object contains its detailed counts, findings, and local errors. An unexpected handler failure returns HTTP 500 with `{"error":"failed to run AI OS audit"}`.

## Agent recipes

### Import batches

`POST /api/import/batches` files one import batch over a selection (#1032,
C6) — the private per-workspace record of what was selected, what was read,
progress, dedupe across attempts, and per-fact provenance. One batch at a
time per workspace. `POST /api/import/batches/{id}/run` (#1038, C7) is what
drives it: it answers 202 with the batch already `running` and the
extraction happens after the response, so poll the list route for progress.
Nothing is applied — the extracted facts arrive as review proposals with the
*source* message's date, and a person accepts them.

```bash
# File a batch over two selected conversations (ids, never paths).
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/import/batches" \
  -H 'content-type: application/json' \
  -d '{"workspace":"default","sources":[{"provider":"claude_code","source_id":"<session-a>"},{"provider":"opencode","source_id":"<session-b>"}]}'

# That workspace's batches, oldest first, each with its selection,
# per-source digests, progress and provenance.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/import/batches?workspace=default"

# Run the batch. Answers 202 with the batch now `running`; no model turn
# happens in this request. Polling `GET .../batches?workspace=` shows
# progress move to `done` / `partial` / `failed`, and the proposals land in
# the review queue for a person to accept. A batch already `running` is
# answered 200 with its current state (never a second run); a settled batch
# is a 409.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/import/batches/$BATCH/run" \
  -H 'content-type: application/json' \
  -d '{"workspace":"default"}'

# Stop a batch, keeping its recorded progress, its provenance and every
# proposal it already filed. Idempotent: cancelling a cancelled batch answers
# it unchanged. Cannot unsend provider input, and a batch already settled as
# done, failed or partial is a 409.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/import/batches/$BATCH/cancel" \
  -H 'content-type: application/json' \
  -d '{"workspace":"default"}'

# Drop a batch record. Queue and vault are untouched: filed proposals stay
# queued and accepted facts stay in the vault.
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/import/batches/$BATCH?workspace=default"
```

### Restart an installed server

`POST /api/admin/restart` uses the backend's existing chat-drain lifecycle and
does not run git, pip, npm, or frontend builds. Settings chooses this action when
`/api/local/status` reports `restart_only: true`: Linux production, a legacy
bundled-app install (whatever the dev mode), and any install that is not a
deployable source checkout. Development deploys from a checkout retain
`/api/admin/deploy`, which returns 400 up front on an installed (non-checkout)
engine.

```sh
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/admin/restart"
```

Concrete curl examples for the in-session agent acting on the local API. Auth once, reuse the cookie jar.

**Auth**

```bash
source .env
curl -sS -c /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/auth" \
  -H 'content-type: application/json' \
  -d "{\"token\":\"$PWA_AUTH_TOKEN\"}"
```

Reuse the jar with `-b /tmp/ciao.jar` on every other call. The Origin/Referer host-match check is skipped when those headers are absent, so plain curl works.

**Vault note review**

```bash
# Detection is read-only and scoped to one logical workspace.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/vault/review?workspace=default"

# The reversible trash inventory (what the Review → Retirement tab renders),
# and the kept notes still in the vault that "Recently cleared" offers to re-queue.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/vault/review?workspace=default&include=trashed,cleared"

# Record a reversible decision. Permanent deletion is only available after trash.
# The response carries the refreshed `candidates` and `trashed` lists, so render
# from those rather than issuing another GET.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/vault/review?workspace=default" \
  -H 'content-type: application/json' \
  -d '{"action":"decide","candidate_id":"<candidate-id>","disposition":"keep"}'

# Retire into the reversible trash, then restore from it. Nothing is purged on
# a timer: a trashed note stays restorable until it is explicitly deleted.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/vault/review?workspace=default" \
  -H 'content-type: application/json' \
  -d '{"action":"trash","candidate_id":"<candidate-id>"}'
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/vault/review?workspace=default" \
  -H 'content-type: application/json' \
  -d '{"action":"restore","candidate_id":"<candidate-id>"}'

# Undo a keep. The candidate id comes from the `cleared` list above; a keep is
# otherwise suppressed until the note's own bytes change.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/vault/review?workspace=default" \
  -H 'content-type: application/json' \
  -d '{"action":"reopen","candidate_id":"<candidate-id>"}'

# Close a finished PROJECT out, then put it back. Only a `type: project` (or a
# note under `projects/`) is accepted — anything else is a 409, and `trash` is
# the action for it. The response `result` carries `previous_path`, `new_path`,
# `status_rewritten` and `edited_backlinks`, so a client can say what moved and
# which notes were repointed without re-reading the vault.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/vault/review?workspace=default" \
  -H 'content-type: application/json' \
  -d '{"action":"complete","candidate_id":"<candidate-id>"}'
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/vault/review?workspace=default" \
  -H 'content-type: application/json' \
  -d '{"action":"restore_completed","candidate_id":"<candidate-id>"}'
```

**Agent assets**

Subagents, commands and skills belong to ONE workspace, named by the `workspace`
field (or `?workspace=` on a bodyless request). A write that omits it, or names a
workspace that is not registered, is a 400 — it is never redirected to the
install root, so an asset cannot land somewhere you did not ask for. A read with
no workspace (or an unknown one) falls back to the whole install.

```bash
# Inspect one workspace's subagents, commands, and the install-wide health block.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/agent-assets?workspace=personal"

# Inspect workspace/vault health only.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/workspace-health"

# Create a workspace-owned subagent.
# Writes subagents/<name>.md, mirrors a vault note under memory-vault/Workspace/Subagents/,
# then syncs the subagent into .claude/agents/.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/agent-assets/subagents" \
  -H 'content-type: application/json' \
  -d '{"workspace":"personal","name":"pr-reviewer","description":"Review pull-request diffs for regressions.","prompt":"Inspect the changed files, identify concrete risks, and report findings first."}'

# Update or delete a custom subagent. Installed/system subagents are read-only.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/agent-assets/subagents/pr-reviewer" \
  -H 'content-type: application/json' \
  -d '{"workspace":"personal","description":"Review pull-request diffs for regressions.","content":"# Pr Reviewer\n\nInspect changed files, identify concrete risks, and report findings first."}'
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/agent-assets/subagents/pr-reviewer?workspace=personal"

# Create a workspace-owned slash command.
# Writes commands/<name>.md, mirrors a vault note under memory-vault/Workspace/Commands/,
# then syncs it into the provider-native command locations.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/agent-assets/commands" \
  -H 'content-type: application/json' \
  -d '{"workspace":"personal","name":"decision-record","description":"Turn notes into a decision record.","argument_hint":"<notes>","prompt":"Convert $ARGUMENTS into a concise decision record with context, decision, and consequences."}'

# Update or delete a custom slash command. Installed/system commands are read-only.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/agent-assets/commands/decision-record" \
  -H 'content-type: application/json' \
  -d '{"workspace":"personal","description":"Turn notes into a decision record.","argument_hint":"<notes>","content":"# Decision Record: $ARGUMENTS\n\nConvert $ARGUMENTS into a concise decision record with context, decision, and consequences."}'
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/agent-assets/commands/decision-record?workspace=personal"
```

**Housekeeping (operator-action strip)**

```bash
# List the home-screen operator actions. Each carries run_label, chat_label,
# and chat_prompt; the browser renders the buttons and seeds the chat itself.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/housekeeping"

# Run one action's mechanical work. The response re-runs detection and returns
# the fresh action list so the client cannot render a stale strip.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/housekeeping/vault-vocabulary/run"

# Record a "not now" for an ask-style action (e.g. the GitHub star nudge). The
# response re-runs detection and returns the fresh action list.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/housekeeping/github-star/dismiss"
```

**Update tasks (the "After this update" catalog)**

```bash
# List the tasks this engine supports for a workspace, with their state. Each row
# carries its lifecycle (`status`), the applicability answer behind it, and the
# chat a previous start created — so a caller never has to remember a chat id.
# `?workspace=` is required.
#
# Read `applicability_checked_at` as "when did anyone last look" and `updated_at`
# as "when did the operator decide". They are separate clocks on purpose: a
# dismissed task has a decision time and a stale check time, and a surface that
# shows only the second cannot tell an unexamined condition from a settled one.
# Answers are cached for `update_tasks.APPLICABILITY_TTL_S`, so a repeat read
# inside that window reports the same check time rather than re-dating it; the
# state file, by contrast, is read on every call.
#
# This is also how a task finishes. When it computes a task's answer, it asks
# that task's registered completion check — so a task the operator has finished
# turns to `status: "completed"` here, and drops off Home, with no separate call.
# `applicability` is unaffected: a review that deliberately kept some rows leaves
# those rows countable, and the task is done anyway.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/update-tasks?workspace=personal"

# Start a task. Idempotent per (task, revision): press it twice and you get the
# SAME chat back with `"resumed": true`, and the packaged prompt runs once. There
# is no prompt field — the instructions are read from the packaged catalog on the
# server, and the response's `prompt_digest` is what they hash to. If the last
# dispatch failed (the 500 below, or a crash mid-launch), the next start sends
# the prompt into that same chat rather than reporting a resume that never ran.
# Starting a task you had dismissed reopens it in place. 409 means the task was
# refused (unknown id, or one this engine version cannot support).
curl -sS -b /tmp/ciao.jar -X POST \
  "http://localhost:${PWA_PORT:-8443}/api/update-tasks/review-legacy-rows/start?workspace=personal"

# Decline this task at this revision. Suppresses the offer at this revision only;
# a later revision is new work and is offered again. `reason` is optional.
#
# This hides the task in this workspace. It does NOT cancel a chat that is
# already open and it does NOT mark the work done: the record goes to
# `dismissed`, the chat carries forward, and a client that tells the operator
# otherwise is lying about the state of the machine. Reopen below puts it back.
# The chat is carried forward unless the last attempt never dispatched
# (`status: "failed"`): that chat is live and empty, so the next start creates a
# fresh one and dispatches into it rather than reporting the empty one as
# running.
curl -sS -b /tmp/ciao.jar -X POST \
  "http://localhost:${PWA_PORT:-8443}/api/update-tasks/review-legacy-rows/dismiss?workspace=personal" \
  -H 'content-type: application/json' \
  -d '{"reason":"reviewed them by hand"}'

# Undo that dismissal and put the task back on offer. Re-evaluates current
# applicability rather than replaying old instructions: the answer comes from the
# same detector pass as the list.
curl -sS -b /tmp/ciao.jar -X POST \
  "http://localhost:${PWA_PORT:-8443}/api/update-tasks/review-legacy-rows/reopen?workspace=personal"
```

**Projects**

```bash
# Create — returns the project dict with `project_id`. `workspace` is any configured workspace name.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/projects" \
  -H 'content-type: application/json' \
  -d '{"name":"Home reno","workspace":"personal","context":""}'

# Update — any subset of: name, context, vault_folder. The running server owns
# `.runtime/web_projects.json`; for renames, use PATCH here (not a hand-edit of
# the file), or the next request will race with the server and create a duplicate.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/projects/$PID" \
  -H 'content-type: application/json' \
  -d '{"context":"Track the kitchen rebuild"}'

# Reorder — pass the full top-to-bottom sequence of project ids for one
# workspace. Omitted ids keep their relative order after the listed ones;
# `General` is always pinned first regardless of where it appears.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/projects/reorder" \
  -H 'content-type: application/json' \
  -d '{"workspace":"personal","order":["proj-b","proj-a"]}'

# Complete (vault-backed only) — moves the vault folder to projects/completed/ and deletes the PWA project.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/projects/$PID/complete"

# List completed projects (read-only scan of projects/completed/). Optional ?workspace=<name>.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/projects/completed?workspace=work"

# Restore a completed project — moves its folder back to active/, flips status to active.
# Body keys: workspace (configured name) and stem (the completed folder name).
# Auto-discovery recreates the PWA project; the original chats stay archived.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/projects/completed/restore" \
  -H 'Content-Type: application/json' -d '{"workspace":"work","stem":"maf-onsite"}'

# Delete — returns {"ok": true|false}.
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/projects/$PID"
```

**Task board**

Every call names a *workspace*, never a directory: the server resolves the name
to that workspace's own vault. `workspace` is required on every route, and an
unknown name is a 400 rather than a fallback to the install root. Every task is
one Markdown file at `<workspace vault>/Workspace/Tasks/<32-hex id>.md`, and
every write presents the `revision` it read — the SHA-256 of that file's exact
bytes, returned by the list and create calls and by any successful edit. A stale
revision is a **409** and writes nothing, so re-read rather than resending it.
Completion is the user's own decision and works over this session cookie; the
same operation from the agent CLI (`ciao task complete`) is refused with
`task_completion_requires_user`.

```bash
# List one workspace's board. Every row carries the `revision` a later edit has
# to pass back; a file that is not a readable task comes back as a row carrying
# `code` instead of task fields, so a malformed file never reads as an empty
# board.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/tasks?workspace=personal"

# Read one task, description included. The list rows above carry no `body`,
# so an editor that shows the prose and writes it back reads it from here.
curl -sS -b /tmp/ciao.jar \
  "http://localhost:${PWA_PORT:-8443}/api/tasks/9f2c4a1b7e3d4f6a8b5c2d1e0f3a4b6c?workspace=personal"

# File a task. `project_id` accepts an id or a name and must belong to this
# workspace; `due` is a calendar date (YYYY-MM-DD). Answers 201 with the record
# as stored — take its `id` and `revision` from there.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/tasks" \
  -H 'content-type: application/json' \
  -d '{"workspace":"personal","title":"Draft the migration runbook","body":"Steps, links, acceptance criteria.","project_id":"Home","due":"2026-10-20"}'

# Edit. Only the fields sent change; `body` replaces the description wholesale.
TID=9f2c4a1b7e3d4f6a8b5c2d1e0f3a4b6c
TREV=$(curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/tasks?workspace=personal" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["tasks"][0]["revision"])')
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/tasks/$TID" \
  -H 'content-type: application/json' \
  -d "{\"workspace\":\"personal\",\"expected_revision\":\"$TREV\",\"title\":\"Draft the rollback runbook\"}"

# Complete — the user's own session, at the revision it read.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/tasks/$TID/complete" \
  -H 'content-type: application/json' \
  -d "{\"workspace\":\"personal\",\"expected_revision\":\"$TREV\"}"

# Remove the record. The file is the user's own Markdown and this unlinks it:
# there is no trash, so pass the revision you read.
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/tasks/$TID" \
  -H 'content-type: application/json' \
  -d "{\"workspace\":\"personal\",\"expected_revision\":\"$TREV\"}"
```

Project and chat uploads are limited to 50 MB per file, 100 files per request,
and 512 MB total. Project-file list responses use workspace-relative viewer
paths when the vault is nested under the workspace and absolute viewer paths
when `CIAO_VAULT_ROOT` points elsewhere. The project upload response exposes
only a bounded relative `path` plus metadata; chat attachment and native-drop
responses expose bounded `file_refs` (`ciao-drop:drop_<32 hex>`), never an
absolute path. The server expands a reference only while building the provider
prompt, so the browser never needs the host path. Saved-page `.mht`/`.mhtml`
files are accepted as binary project attachments and served as downloads rather
than executable inline content.

**Chats**

```bash
# Create — title/model/mode/provider all optional. The proposal Review UI also
# sends validated `helper` lifecycle metadata; ordinary clients should omit it.
# provider is any id from the registry (`claude`, `opencode`); see
# GET /api/models -> providers[] for the live list and per-provider
# '' = auto from the project's configured workspace bucket. Legacy
# configured names. Unknown buckets are rejected unless a workspace config
# defines them.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/projects/$PID/chats" \
  -H 'content-type: application/json' \
  -d '{"title":"Tile layout"}'

# Update — title, model, provider, mode, project_id (to move
# between projects), thinking_level. thinking_level is provider-native
# ('' = provider default, allowed values per provider in GET /api/models →
# thinking_levels) and is safe to change mid-chat; it resets to '' on
# handover. Changing provider
# on a started chat returns 400; use handover instead.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/chats/$CID" \
  -H 'content-type: application/json' -d '{"thinking_level":"high"}'

# Handover — switch model/backend inside the same visible chat.
# Body keys: provider = claude|opencode, model, messages
# (visible rows). Starts the next provider turn as a fresh session seeded
# with those messages.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/handover" \
  -H 'content-type: application/json' \
  -d '{"provider":"claude","model":"sonnet","messages":[{"role":"user","content":"continue this task"},{"role":"assistant","content":"current state"}]}'

# Fork — create a new independent chat in the same project continuing from a completed turn.
# Body keys: messages (visible rows up to and including the target assistant answer),
# turn_index (zero-based count of user messages). Allocates a root-relative title number.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/fork" \
  -H 'content-type: application/json' \
  -d '{"turn_index":0,"messages":[{"role":"user","content":"Question"},{"role":"assistant","content":"Answer"}]}'

# Archive — finalises the chat and writes a Markdown transcript. Returns
# {ok, archived_to, postprocess}; a `chat_archived` event is emitted too.
# `postprocess` is the post-archive pipeline's opening state, returned in the
# response as well as published, so the client that archived cannot miss it.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/archive"

# Subagent transcripts — one entry per subagent this chat's session ever
# spawned: {agent_id, messages, description, subagent_type, status, turn_index}.
# `status` is running, completed, failed, or the neutral terminal stopped state.
# `messages` shares the /messages shape; both provider renderers omit
# `timestamp` on these, so a reader must tolerate it being absent. The PWA's
# read-only subagent view (/chat/{chat_id}/subagent/{agent_id}) reads this.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/subagents"

# Subagents working right now, across every chat the server considers active:
# {"chats": {chat_id: [{agent_id, description, subagent_type, is_async,
# status, turn_index}]}}. Metadata only — no transcripts — so it is cheap
# enough to poll for the sidebar. Chats with nothing running are omitted
# entirely, which is how a finished agent's row disappears; poll it only while
# something is working, and replace your whole map with each response rather
# than merging. For Claude chats this is what the parent session can name:
# background `Agent` dispatches. A foreground Task is recorded there by its
# own completion, so it is never listed as running; that work is visible in
# the chat's live trace while its turn streams.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/subagents/running"

# Mark read — returns {ok, last_read_at}.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/read"

# Mark unread on purpose ("come back to this") — clears the read stamp so the
# chat is unread again on every device; returns {ok, last_read_at}.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/unread"

# Mark all read.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/read-all"

# Read mutations also cancel the delayed push and emit a cross-device clear
# control. Connected PWAs close the matching service-worker notification tag.

# Deferred retry after provider/session quota errors. action ∈ {set, try_now, stop}.
# `set` needs the user prompt to replay; automatic quota handling fills this itself.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/retry" \
  -H 'content-type: application/json' \
  -d '{"action":"set","prompt":"retry this turn"}'
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/retry" \
  -H 'content-type: application/json' -d '{"action":"try_now"}'
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/retry" \
  -H 'content-type: application/json' -d '{"action":"stop"}'

# Stop an in-flight turn — {stopped: bool}. Same effect as the websocket
# `stop` message; use this when driving a chat over plain HTTP or when a
# socket may not be connected.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/stop"

# Start a new provider session inside an existing chat (resets context).
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/new"

# Delete.
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/chats/$CID"

# Create Provider Sub-chat.
# Body keys: parent_turn_index, owner (object with provider, model, label), participant (object), task_prompt (optional), user_authorized (optional).
  -H 'content-type: application/json' \
  -d '{"parent_turn_index":0,"owner":{"provider":"claude","model":"opus","label":"Claude"},"participant":{"provider":"opencode","model":"provider/model","label":"opencode"},"task_prompt":"Analyze this issue"}'

# Read Sub-chat Events.

# Send Message/Prompt to Sub-chat.
# Body keys: message, user_authorized (optional).
  -H 'content-type: application/json' \
  -d '{"message":"Next instruction"}'

# Close Sub-chat.

# Cancel Sub-chat.

# Extend Sub-chat limits.
# Body keys: user_authorized (required).
  -H 'content-type: application/json' \
  -d '{"user_authorized":true}'

# Resolve Permission Request in Sub-chat.
# Body keys: request_id, approved, reason (optional).
  -H 'content-type: application/json' \
  -d '{"request_id":"req-1","approved":true}'

# Resolve Structured Question in Sub-chat.
# Body keys: request_id, answers (dict).
  -H 'content-type: application/json' \
  -d '{"request_id":"req-2","answers":{"choice":["option-a"]}}'
```

**Workspaces**

```bash
# List — returns {workspaces, active, primary, provider_options}.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/workspaces"

# Upsert — body keys: name, default_provider,
# gws_profile, color (pink|cyan|amber|emerald|violet; default
# pink — PWA accent only), disallowed_tools (extra tools, CSV or list,
# null = defaults). claude.ai connector MCPs are always allowed. POST
# creates `<CIAO_VAULT_ROOT>/<name>` and PATCH /api/workspaces/{name} updates
# metadata in place. `vault_root` in a request body is ignored: locations are
# read-only here so a routine settings save cannot relocate a workspace.
# Setup and migration may still persist an external/legacy root in the registry.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/workspaces" \
  -H 'content-type: application/json' \
  -d '{"name":"client-a","disallowed_tools":"mcp__n8n_mcp"}'

# Update metadata on an existing workspace.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/workspaces/personal" \
  -H 'content-type: application/json' \
  -d '{"disallowed_tools":"mcp__n8n_mcp"}'

# Archive. Nothing is deleted or merged into another workspace: the folder
# moves intact to <install>/.archived-workspaces/<name>-<YYYYMMDD-HHMMSS>/
# beside an archive.json, its chats are archived, its user schedules leave with
# it, and search and INDEX.md stop seeing its notes. The primary and the last
# workspace are refused. DELETE /api/workspaces/{name} is an alias.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/workspaces/client-a/archive"

# List archived workspaces, then restore one by id (refused if the name is taken).
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/workspaces/archived"
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/workspaces/archived/restore" \
  -H 'content-type: application/json' \
  -d '{"id":"client-a-20260923-101500"}'
```

**Schedules and ops**

```bash
# Create a routine with archive behavior. archive_policy ∈ manual|auto.
# `auto` runs a post-run classifier and archives only when the user does not need to see it.
# GET /api/schedules enriches each entry with its resolved `workspace`,
# `effective_provider`, `effective_model`, `next_run` (next fire, ISO or null),
# `last_expected_run` (most recent due fire, ISO or null), and `missed` (true when a
# due fire was never recorded — surfaced in the Schedules overview). Empty persisted
# model/provider values inherit the selected workspace on every dispatch. At server
# startup, only the latest missed occurrence is dispatched; no backlog is replayed.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/schedules" \
  -H 'content-type: application/json' \
  -d '{"time":"01:00","timezone":"Europe/Zurich","frequency":"daily","prompt":"Memory curation","web_project_id":"proj-...","workspace":"personal","archive_policy":"auto"}'

# Update archive behavior.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/schedules/$SID" \
  -H 'content-type: application/json' \
  -d '{"archive_policy":"auto"}'

# Run a schedule on demand. Auto-archived routines can return archived_to.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/schedule-run/$SID"

# Create an interval automation bound to one existing chat: re-sends the prompt
# every N minutes, keeping that conversation going. Pass no model — each run
# inherits the chat's own model and mode. `time` and `timezone` are ignored:
# cadence is measured from the last dispatch, not a wall-clock slot.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/schedules" \
  -H 'content-type: application/json' \
  -d '{"prompt":"Check my PRs for review changes; reply \"no changes\" if nothing new.","frequency":"interval","interval_minutes":10,"web_chat_id":"chat-..."}'

# Same cadence, fresh chat per run: pass web_project_id instead of web_chat_id
# (and a model/provider if you want an override rather than the workspace default).
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/schedules" \
  -H 'content-type: application/json' \
  -d '{"prompt":"Sweep the inbox and report anything urgent.","frequency":"interval","interval_minutes":30,"web_project_id":"proj-..."}'

# Pause / resume it. One flag: a paused entry neither runs now nor resumes on
# the next boot. "Run now" still works while paused.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/schedules/$SID" \
  -H 'content-type: application/json' -d '{"enabled":false}'

# Deploy: snapshot, pull, build, restart. Don't call from inside the live PWA session
# (AGENTS.md "Never restart the ciao service yourself"); ask the operator to hit Deploy.
# Steps run against CIAO_APP_REPO when set, else the directory holding the running
# ciao package; a non-checkout returns 400 with a "locate checkout" step. An
# installer-managed engine is refused up front: re-run install.sh to update it.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/admin/deploy"
```

**Google Workspace re-login (recover a revoked/expired token)**

```bash
# Check which profiles report a dead login (token_valid=false / needs_relogin=true).
# The values come from the periodic health monitor's cache — no live probe here.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/integrations/gws"

# Start a server-managed re-login. Returns { auth_url, state, port, expires_in }.
# The loopback callback listener lives IN the engine process, so — unlike
# `gws auth login` in a background bash task — it survives across chat turns and
# actually captures the redirect. Open auth_url in a browser and sign in.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/integrations/gws/relogin/start" \
  -H 'content-type: application/json' -d '{"profile":"personal"}'

# Poll until status is "completed" (or "error"). Never returns tokens.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/integrations/gws/relogin/status?profile=personal"

# Abort a pending attempt and free the loopback port.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/integrations/gws/relogin/cancel" \
  -H 'content-type: application/json' -d '{"profile":"personal"}'
```

**Routine settings (Settings → General / Models)**

```bash
# Read internal-routine settings: the automatic-session-insights switch,
# insights and critique model overrides, the per-provider default model /
# thinking / routine-model maps, and the effective models after defaults.
# insights_enabled=false stops the memory pass.
#
# insights_model_effective is the PRIMARY workspace's answer only. With no
# override the insights routine resolves from the chat's own workspace, so
# insights_model_by_workspace carries the full {workspace: model} map. The map
# is empty when an override is set, because then that one model applies
# everywhere.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/settings/routines"

# Update any subset. Persisted in .runtime/app_settings.json, applied to the
# live config immediately (no restart). Empty string clears an override back
# to the env default. A stored "apple"/"apfel" insights_model (from the retired
# on-device option) reads as Automatic rather than reaching a provider as a
# literal model id. Per-provider defaults use the nested maps:
# provider_default_models, provider_default_thinking, provider_insights_models.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/settings/routines" \
  -H 'content-type: application/json' \
  -d '{"insights_enabled":false,"insights_model":"gemma4:12b-it-qat","critique_models":"anthropic/claude-sonnet-4.5","provider_default_models":{"opencode":"provider/model"}}'
```

**Project MCP servers (Settings → MCP tab)**

```bash
# Create a project MCP server in .mcp.json. Pass url for an HTTP server, or
# command (+ optional args) for a stdio one; one of the two is required.
# env_keys binds placeholder names to .env keys the server needs.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/mcp/servers" \
  -H 'content-type: application/json' \
  -d '{"name":"linear","url":"https://mcp.linear.app/sse","env_keys":{"LINEAR_API_KEY":""}}'

# Update one server. Omitted transport fields (url, command, args) keep their
# current values, so a PATCH can change env bindings alone.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/mcp/servers/linear" \
  -H 'content-type: application/json' \
  -d '{"env_keys":{"LINEAR_API_KEY":"LINEAR_TOKEN"}}'

# Delete one server. 404 when the name is not in .mcp.json.
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/mcp/servers/linear?workspace=personal"

# Discover a server's tools on demand (HTTP probe, or previously observed names).
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/mcp/servers/linear/tools"

# Save MCP secrets into the workspace .env. Values are write-only: they are
# never returned by any endpoint. Keys already known from a discovered server
# are accepted bare; an unknown key needs "server" so it can be bound into that
# server's .mcp.json env map.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/mcp/env-keys" \
  -H 'content-type: application/json' \
  -d '{"server":"linear","keys":{"LINEAR_API_KEY":"lin_api_..."}}'
```

**Workspace git sync**

Ciaobot never creates or switches local branches: it works on whatever branch the workspace
checkout is currently on. Handback commits pending work, pulls from origin (merge-based), and
pushes the branch: a clean pull is pushed directly (response: `{merged:true,
deploy_needed:false, pushed}`); a conflicting pull is left in the tree and opens an interactive
chat (`{merged:false, conflict:true, merge:{chat_id,...}}`) that resolves it, asking you
(push-notified) when ambiguous. After that chat lands the branch, resync merges
`origin/<branch>` back into the checkout. A failing step returns `{ok:false, step, error}` with
status 400, where `step` names the stage that failed: `branch` (no branch / detached HEAD),
`preflight` (a git operation you started outside Ciaobot still holds this repository — a
preexisting `.git/index.lock` or an in-progress merge/rebase, which is left untouched for you
to finish or abort), `add`, `status`, `commit`, `fetch` (nothing is pulled or pushed after a
failed commit or fetch), or `push`. Resync reports the same failures as `{ok:false, detail}`.
One sync is one serialized mutation, so a concurrent Ciaobot mutation of the same repository
waits rather than interleaving. Non-git workspaces (or detached HEAD) get
`{ok:false, error}` with status 400. Workspace sync never deploys app code; app updates happen
through the package install/upgrade path.

```bash
# Current workspace git state: {git_repo, branch (null when not a repo / detached), dirty, dev_mode}.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/local/status"

# Sync with remote — commit pending work, pull from origin, push the current branch.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/local/handback"

# After a conflict chat pushed the branch, merge origin/<branch> back into the checkout.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/local/resync"

# Open an interactive conflict-resolution chat for a branch by hand (also used on conflict).
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/handover/merge" \
  -H 'content-type: application/json' -d '{"branch":"main"}'
```

**Unattended memory backup**

Routes: `GET /api/local/backup`, `PATCH /api/local/backup`, `POST /api/local/backup/run`,
`GET /api/local/backup/setup-prompt`, `POST /api/local/backup/setup-chat`.

The engine commits the durable-data scope (`memory-vault/`, `skills/`, `subagents/`,
`commands/`, the `AGENTS.md` guide and archived workspaces — never credentials, runtime
state, caches, the transcript archive or application source) and pushes it every five
minutes. This is a different path from "Sync with Remote" above: it is unattended, it
commits only what the scope allows, and it never creates a commit or a push when there is
nothing pending.

`GET` is read-only and always 200 — a repository with no remote is a state it reports, not
a failure of the endpoint. `state` is one of:

| `state` | meaning |
| --- | --- |
| `ready` | committed and pushed; `reason` says what the last run did |
| `pending` | a scoped change to commit, or a commit that never reached origin |
| `running` | a run is in flight right now |
| `paused` | this boot will not run one: `enabled:false`, `paused:true`, or not the host |
| `offline` | the commit is safe locally and origin did not answer; retried on the slow cadence |
| `needs_attention` | only the owner can fix it: a credential in the scope, a repository another git operation holds, a branch that diverged from origin (the commit is on a per-commit backup ref) |
| `not_configured` | the data root is not a repository, is on a detached HEAD, or has no `origin` — re-checked every tick, so adding a remote needs no restart |

`last_success_commit` is a commit known to exist on the remote: it is written only by a push
that landed, never by a local commit. A failed push keeps the local commit untouched — no
reset, no force-push — and the remote URL is always reported with any credential removed.
`remote` is read fresh on every call and is empty whenever this boot is paused or the
repository is unconfigured; `last_remote` is the record of where the last run pushed, so it
is still the answer when `remote` is not. `coverage_gap` is the number of tracked paths git
already has that the backup scope refuses to commit — the whole count, not a sample — so a
repository that is also a checkout is reported as a coverage fact beside whatever `state` is.
It is not a state: a run that committed and pushed through a gap is `ready` and is recorded
as a success. `reason` is prose for a human and may be reworded; read `coverage_gap` for the
number. `POST /api/local/backup/run` is the manual trigger;
it takes the same lock as the scheduled tick, so the two can never interleave. 200 when the
run left the repository in a state that needs nothing from you, 400 when it could not do its
job (no repository, no remote, refused credentials, unreachable remote) — the body is the
same status object either way.

```bash
# What the backup service knows: {state, scope, branch, remote, last_remote, enabled,
# interval_s, last_attempt_at, last_success_at, last_success_commit, pending_changes,
# pending_commits, coverage_gap, reason}.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/local/backup"

# Pause backups, or turn them off entirely. Both survive a restart.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/local/backup" \
  -H 'content-type: application/json' -d '{"paused":true}'
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/local/backup" \
  -H 'content-type: application/json' -d '{"enabled":false}'

# Back up now, through the same serialized path the five-minute loop uses.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/local/backup/run"

# The one canonical setup prompt, and the trusted context behind it. Copy this
# text to hand to an agent on another machine...
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/local/backup/setup-prompt"

# ...or have a chat here do it. The prompt is SENT, not drafted, and a second
# click re-enters the same chat instead of starting a second agent.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/local/backup/setup-chat"
```

**Engine start at sign-in**

`GET /api/service/login` and `PATCH /api/service/login` report and change one
thing: whether the *engine service that serves this request* starts when this
user signs in. They are the next-sign-in state, not the running state — an
engine stopped right now can still be set to start at the next sign-in, and a
running engine can be set not to.

Both are ordinary `/api/*` routes: signed session cookie required, and the PATCH
also needs a same-origin `Origin`/`Referer`. Neither is in the public or
loopback-only allowlist, so a local process without a session is refused.

The body is exactly one key: `{"enabled": true}` or `{"enabled": false}`. A
string, a number, `null`, an extra key, an empty object, or unparseable JSON is
a 400, and the request never reaches the service layer.

```bash
# Read the verified state: {platform, supported, installed, enabled, can_change,
# reason, setup_command}.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/service/login"

# Turn it on / off. The response is the re-read state, not the request echoed.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/service/login" \
  -H 'content-type: application/json' -d '{"enabled":true}'
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/service/login" \
  -H 'content-type: application/json' -d '{"enabled":false}'
```

`installed` and `enabled` are tri-state, and `null` means *the machine did not
say*. A definition that does not parse, a `launchctl print-disabled` listing
that cannot be read, a Task Scheduler identity mangled by a lossy code page, a
definition that names no workspace, a definition that serves a different
workspace, a hand-written LaunchAgent with neither `RunAtLoad` nor `KeepAlive`,
a task with no enabled logon trigger: each reports `null` with the reason in
`reason` and `can_change: false`. Do not render a switch position out of a
`null`; render the reason. `can_change` is the only field that licenses a
write, and it is true only where the enabled bit is proven to be what decides.

`platform` is `macos`, `windows`, `linux`, or `other`. On Linux, and on a
platform with no service backend, `supported` is false and `reason` points at
`docs/LINUX.md` (a systemd unit an administrator enables by hand).

The engine identity is fixed and never taken from a request: the
`com.ciao.server` LaunchAgent in the real per-user LaunchAgents directory on
macOS, the registered `\Ciaobot\Engine` Task Scheduler task on Windows.
`installed: false` (macOS, no plist) carries a `setup_command` to run instead;
`setup_command` is always a fixed, non-executing hint. **These routes never
create, register, start, stop, restart, bootstrap, or repoint a service, and
never write a plist or task XML.** A service belonging to another workspace is
reported and left alone.

Status codes on PATCH: 200 with the re-read status; 409 with `error` plus the
same status fields when the change is not this engine's to make (no service,
another workspace's service, a definition that does not start at sign-in, an
unanswerable query); 503 when the OS refused the change or the re-read did not
confirm it. A change only counts as done when the machine's re-read says what
was asked for — the 503 is the honest answer when it does not, and it is never
papered over with the requested value.

**Connecting a data folder to a private remote**

`GET /api/local/backup/setup-prompt` is the setup path for a user who has no Git
knowledge: it renders one prompt from trusted configuration and returns it beside
the `context` it was built from, so a Settings surface can show the folder and
scope without parsing prose.

`context` is `{folder, scope, scope_paths, excluded, tracked_excluded, branch,
has_repo, repo_root, parent_repo, has_remote, remote, interval_s}`. `folder` is
the absolute data root with its spaces intact; `parent_repo` is true when that
folder sits inside a larger checkout, which is the case where a remote added
here would carry more than the notes. The `remote` is credential-free on the way
out, and the prompt carries no token, key, or password.

The same text serves both actions — a copy button and `POST
/api/local/backup/setup-chat` — because both call one `render_setup_prompt`. That
route opens a chat titled "Set up memory backup" in the host workspace and sends
the prompt; a repeated click, a retry, or a reload re-enters that chat
(`reused: true`) rather than putting a second agent on the same repository, and
the prompt is never re-sent into a chat that already has it. Only a chat the
owner archived is replaced. 500 when no General project exists in any workspace
to host it.

Readiness is not decided by either route. The chat does the work, the service
re-reads the repository's real state on every tick and on every status call, and
`GET /api/local/backup` reports what it finds — which is also how an external
agent's setup is detected, with no restart. A guided setup that verifies turns
the backup on, unless the owner has paused it: the pause is a hold they lift
themselves, and a pause taken *during* a setup is the one that counts.

**Proposal queue**

Routes: `GET /api/proposals`, `GET /api/proposals/history`,
`GET /api/proposals/{id}/preview`,
`POST /api/proposals/{id}/implement`,
`POST /api/proposals/{id}/{action}` (action is `accept` or `dismiss`),
`POST /api/proposals/batch`, `POST /api/proposals/dismiss-older-than`.

Memory mutations also expose `GET /api/memory/receipts`,
`GET /api/memory/receipts/{id}` and `POST /api/memory/receipts/{id}/undo`
(see below).

`accept` PERFORMS the promotion for a `memory`/`profile` row: the entry is written
into that workspace's bounded region (resolved through `agent_root`, so the right
guide in either layout), and only then is the bullet dropped. Write-then-dismiss,
never the reverse — a failed write returns **409** with the bullet still queued,
so an over-cap region cannot silently swallow the fact. A `rehome` row is not
moved here: relocating a note and rewriting every reference to it is
`vault_rehome`'s job, reversible through its own receipt, and doing half of it
from a queue row would leave links pointing at a path that moved. A `note_edit`
row IS performed here, and it is the one accept that rewrites a whole vault note:
through `ciao.note_receipts.commit_note_change`, so it is revision-checked,
journaled and undoable from History like any other note write, and a note that
moved since the proposal was filed is a **409** with `conflict: true` and the
bullet still queued — never an overwrite. A `retire` in the same row is
attended-only and reversible: it calls `vault_review.trash_note` and moves the
note into `Workspace/.vault-trash/`, from where the review panel's restore puts
it back. `dismiss` writes nothing, but on a `note_edit` row it SETTLES the
proposal, clearing the note-check's `proposal_id` — deliberately not a permanent
"refused" marker, so the note is not asked about again until its cooldown
expires and an edited note is proposed again straight away. A write that lands
but cannot be recorded as decided — the sidecar unreadable, the lock held — is a
**409**, not a success: the error names the note and its receipt, the bullet
stays queued, and that dismissal is what settles the record. Reporting it as an
accept would take away the only control the owner has over a note whose proposal
is no longer in the queue. Batch accept applies
the same rules per row and reports `promoted` and `dismissed` for each, keeping
the bullets it could not write.

`GET /api/proposals/history` resolves every `note_edit` decision back to the
record it was filed from and returns it as a `note_edit` block: the vault-relative
note, the operation (`replace` | `restamp` | `retire`), the exact `before`/`after`
text, the reason, the evidence the verdict rested on, its coverage, and whether
the decision is still `pending`. Without it a settled verification reads as the
bullet's one line with a destination — nothing a reader could judge it against
months later. An accepted `replace`/`restamp` carries a `note_apply` receipt and
so the ordinary undo; an accepted `retire` moved a file and journalled no memory
receipt, so the row is returned with `reversible_by: "restore"` (the review
trash) rather than as a change with no snapshot, which is a different claim. A
row whose record cannot be read simply has no `note_edit` key, the same contract
as a missing `change`.

```bash
# List every queued proposal across all workspaces, plus open skill-proposal
# records. Each row: {id, kind, text, source, workspace, path, line}. `id` is a
# stable, content-derived hash (survives other rows being dismissed). Rehome rows
# carry `rehome: {destination, candidates[], justified, reason}` so a UI never
# pre-accepts a destination no tag backs; region rows carry `region` and
# `leak_warning` (true when accepting would write a foreign workspace's fact
# into the primary workspace's injected region).
#
# A `kind: "skill"` row is one skill's improvement proposal, parsed by
# `ciao/skill_proposals.py` from `Workspace/Skill-Proposals/<skill>.md`. It
# carries the record rather than the filename: `skill`, `title`, `problem`,
# `change`, `rationale`, `lifecycle`, and `sources[]` ({chat_id, archive, turn,
# excerpt}) naming the sessions it came from. `text` stays the skill name, which
# is what two runs of the same skill share. `id` is derived from (workspace,
# skill), so a later pass that merges more evidence into the same skill keeps the
# same id. Rows are filed by `ciao skill-proposal-add NAME --input-file FILE`
# (the memory pass files a supported finding; a person can hand-author one
# through the same command) and never by hand: the target is resolved through
# `skills_inventory.resolve_owned_skill`, so a stock copy, a provider mirror, a
# shared source and an unknown name are refused there, and the record's
# `canonical_path`/`reviewed_revision` are the resolved source's own answers.
# The row also carries the record's `chat_id` and `lifecycle`: the server's own
# statement of which chat is implementing it, which is the source of truth for
# "Open chat". Dismissing one records the decision and flips its `lifecycle` to
# `dismissed`: the file stays on disk, readable and still accumulating evidence,
# and the row leaves the listing.
#
# A `kind: "note_edit"` row is one note whose verification the autonomy rule
# would not apply unattended (a retirement, an update the evidence cannot carry,
# a `still_valid` with no `updated:` to stamp) — see
# `ciao/note_edit_proposals.py`. It carries `target` (the vault-relative note the
# accept would rewrite) and `note_edit: {id, operation, outcome, settled,
# receipt_id, can_accept, reason}`. `operation` is `replace` | `restamp` |
# `retire`; `can_accept` is the server's own answer to whether the accept could
# do what a button saying so claims (false for a note that moved since the
# proposal was filed - a retirement included, which is refused as a conflict
# rather than trashing a note nobody judged - a re-stamp with no frontmatter to
# stamp, a record that has already been decided, and a record that is missing or
# unreadable), with the reason beside it. The queue bullet's own
# payload is the sidecar id — a digest that says nothing to a reviewer, so the
# row is resolved server-side — and the operation, the before/after images and
# the citations live at
# `<vault>/Workspace/Memory-Note-Edit-Proposals/<id>.json`. Exactly one row per
# (note, revision).
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/proposals"

# What accepting one row would write, WITHOUT writing it. Returns
# {ok, preview} where preview is {id, kind, text, action, operation
# (add|update|move|add_category|note_edit|retire_note|none), destination,
# destination_path, before, after, revision, exact, can_accept, reason,
# truncated}. `before`/`after` are the exact destination body the accept would
# replace, computed from the same
# functions the accept calls - so a stamped learned-at date, a duplicate that
# writes nothing, and a learning whose recurrence count is bumped instead of
# appended all show as what they are. `exact: false` marks a kind whose result
# cannot be known without writing (a `[project]` fold is decided by a model at
# accept time). `?text=` previews an edited wording against the same current
# destination. `revision` is the destination digest this preview was computed
# against; hand it back on the accept below.
#
# `note_edit` and `retire_note` are the note-verification operations and are
# their own values rather than `update`/`move`: a `note_edit` preview is the
# WHOLE note's before/after, byte-exact and computed against the note as it
# stands, so its `after` is the bytes the accept writes; a `retire_note` preview
# has no after body, because the accept moves the note to the review trash
# rather than rewriting it.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/proposals/$ID/preview"

# Accept a SKILL proposal into an implementation chat. A skill row is the one
# kind that cannot be accepted by writing: what it asks for is a change to a
# `skills/<name>/SKILL.md` that already exists, so accepting it means opening
# the chat that will do that work. Replies {ok, chat_id, project_id, created}.
#
# IDEMPOTENT, and that is the point: the association is read from the record, so
# a double tap, a retry after a dropped response, and a second device all get the
# SAME chat back (`created: false`) rather than each starting its own. The chat
# is opened in the proposal's OWN workspace, because a proposal filed in `work`
# is about work's `skills/` catalog.
#
# The prompt is the server's, not the client's: it names the existing
# `skills/<name>/SKILL.md`, the proposal id, the evidence and the reviewed
# revision, and instructs the chat to read the skill first, apply a focused
# change only if the finding still holds, verify, run `ciao sync-skills`, and
# record the resolution with `ciao skill-proposal-remove NAME --applied`
# (`--not-applicable` if the finding no longer holds, `--interrupted` if it stops
# part-way, which leaves the proposal queued).
#
# The row stays in the listing with `lifecycle: "implementing"` and the chat id
# on it. Completion is never inferred from the chat ending: the record only
# becomes `applied` when the work records that outcome.
#
# `created: false` in the reply means this accept was handed the chat that was
# already running, not that one was started. A 500 means the chat was opened and
# bound but its turn could not be dispatched; the reply carries that `chat_id`, and
# a retry returns the same chat.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/proposals/$ID/implement"

# Accept one row. Dispatches through the kind's own accept descriptor: memory/
# profile/user are region edits (returns {action: edit_region, region,
# leak_warning}), rehome is a file move (returns {action: move_file,
# destination, justified}). The row is dismissed from the queue; promotion is
# a separate explicit step, matching the MCP resolve path.
#
# Optional JSON body: {"expected_revision": "<preview revision>", "text":
# "<edited wording>"}. A destination that changed since that revision is
# refused with 409 {error, conflict: true, preview} carrying a REFRESHED
# preview, and nothing is written - an accept can never land on top of an edit
# nobody saw. `text` promotes an edited wording; the decision history still
# records the bullet's original text, because that is what the dedupe readers
# compare a re-extracted fact against. Both fields are optional, so a client
# that shows no preview behaves exactly as before.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/proposals/$ID/accept"

# Accept a region row, reconciling it against that region's CURRENT entries
# first, so a fact that supersedes one already there replaces it (undo-logged)
# instead of being appended beside it. This is the way out of a deferral. Opt-in because it is one model call per row — the plain accept above
# is a single synchronous write, and the batch endpoint deliberately never
# reconciles (one timeout per row). `reconcile` accepts 1/true/yes.
#
# When the fresh reconcile cannot decide either, nothing is written, the bullet
# stays queued, and the 409 is marked `deferred: true` with `reason` and the
# `competing` region entries (capped at 5) it was weighed against — the one
# refusal here that another retry can resolve on its own.
#
# It composes with the preview handshake above: the PWA sends the card's
# `expected_revision` on a reconciling accept too, so the check still cannot
# land on a destination nobody looked at.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/proposals/$ID/accept?reconcile=1"

# Dismiss one row from the queue. No region/file is touched.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/proposals/$ID/dismiss"

# Accept or dismiss a set atomically. Body: {"action":"accept|dismiss","ids":[...]}.
# Every id must resolve or the whole batch is rejected (404) with no file change.
# Optional "revisions": {"<id>": "<preview revision>"} applies the same
# conflict guard per row: a row whose destination moved fails on its own with
# {conflict: true} and stays queued, and the rest of the batch still runs.
# The reply carries `results` (one entry per row, unchanged) and `summary`:
# one entry per destination with {destination, action, total, ok, failed,
# conflicts, duplicates, failed_ids[], errors[]}, so a fifty-row accept reads
# as what changed and where without losing any per-row failure.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/proposals/batch" \
  -H 'content-type: application/json' \
  -d '{"action":"accept","ids":["<id1>","<id2>"]}'

# Dismiss every row dated strictly before a cutoff (YYYY-MM-DD), atomically.
# A July proposal about a forgotten chat is not worth promoting.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/proposals/dismiss-older-than?date=2026-08-01"

# Decision history across workspaces, newest first: what was accepted or
# dismissed, by whom (`via`: pwa | agent | auto), and where it landed
# (`destination`). Reads the same per-workspace sidecar the accept/dismiss
# routes above write to (Memory-Proposals.dismissed.jsonl), so it also shows
# what the memory pass recorded on its own ("outcome":
# "suppressed"/"duplicate"), and what the nightly curation agent resolved
# via the CLI ("via": "agent"). Optional query params: workspace,
# limit (default 200, max 1000), action (accepted|dismissed). The reply carries
# {rows, total, truncated, limit, at_max}: `limit` is the clamped page size
# actually served and `at_max` says the request asked for more than the cap, so
# a wider limit would return the same page - a client paging with "show more"
# must stop on `at_max` rather than on `truncated`.
#
# Each served row also carries `source_path` (the archive transcript it came
# from, when one still exists on disk) and, where the receipt protocol
# performed the decision, `change`: {receipt_id, kind, status, destination,
# undoable, changed, ts}. The decision ledger records the id of the receipt
# that performed the write, so the join is exact even when the operator edited
# the wording before accepting (the ledger keeps the ORIGINAL bullet as its
# text, because append-time dedupe compares a re-extracted fact against it).
# Rows written before the id was recorded are joined by matching the decision's
# text against the receipt's - accepts against destination receipts, dismissals
# against queue receipts, with no cross-fallback. A row with NO `change` key is
# one the protocol never recorded - every decision made before receipts landed,
# and every one made outside them - and must be rendered as "No change snapshot
# available" rather than given an undo it cannot honour.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/proposals/history"

# Managed memory mutations (receipts), newest first: every region write, queue
# resolution and prune with actor/source, destination, before/after revisions
# and status (prepared | applied | rolled_back | failed | conflict | undone).
# `undoable` is true only for an applied operation this protocol can reverse;
# unsupported legacy rows render without an Undo affordance. A multi-row batch
# (batch accept/dismiss, expiry sweep) is one atomic rewrite, so exactly one
# row carries the whole-file image and is undoable as a unit; the rest are
# history-only (`undoable: false`) facts that must not each restore the file.
# Optional query params: workspace, limit (default 200).
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/memory/receipts"

# One receipt with its before/after images and a line diff, which the list
# above deliberately strips. Returns {id, workspace, kind, status, ts, actor,
# source, destination, fact_text, undoable, has_snapshot, changed, error} and,
# when `has_snapshot`, {before, after, diff[{op, text}], truncated,
# diff_truncated}. `has_snapshot: false` carries a `reason` and is what the
# History row renders as "No change snapshot available"; a row that is not
# undoable carries a `reason` saying which case applies (part of a batch
# transaction, not applied, itself an undo, or unsupported).
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/memory/receipts/$RECEIPT_ID"

# Undo one receipt. Refuses with 409 when the destination changed since the
# operation (undo would otherwise delete an unrelated later fact), 400 when the
# receipt is unsupported/view-only, 404 when the id is unknown.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/memory/receipts/$RECEIPT_ID/undo"

# The vault's category list. `?workspace=` is required, and it resolves the AGENT
# vault root - the one that owns entity-types.yaml, INDEX.md and VOCABULARY.md -
# not the workspace's notes root, so a pre-re-rooting install (one shared vault)
# edits the same list from either workspace. Every effective row comes back,
# disabled ones included, each with `builtin` and a `note_count`.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/memory/entity-types?workspace=personal"

# Save the desired list. NOT a diff: the body is the whole list, and a row is a
# partial override of the shipped category with that id, so
#   {"types": [{"id": "person", "label": "Human"}]}
# relabels a person and leaves its folder, aliases and staleness alone, and a
# client that sends the GET's rows back unchanged writes nothing at all. Only
# rows that differ from the shipped default reach the file, so an upgrade to a
# default still lands; omitting a stock id is not deleting it (send
# "enabled": false to turn one off), while omitting a CUSTOM id deletes it and is
# refused with 400 while any note still carries that type. The reply is the same
# body as the GET, and the write regenerates VOCABULARY.md's Categories section.
curl -sS -b /tmp/ciao.jar -X PATCH \
  -H 'Content-Type: application/json' \
  -d "{\"types\": [{\"id\": \"person\", \"label\": \"Human\"}]}" \
  "http://localhost:${PWA_PORT:-8443}/api/memory/entity-types?workspace=personal"
```


When adding a new state-changing route (`POST/PATCH/DELETE /api/...`), add an entry here or add the path to `BROWSER_OR_INTERNAL_ROUTES` in `tests/test_pwa_api_docs.py` with a one-line reason. The doc-sync test enforces this.

**WebSocket events**

Global `/ws/events` payloads the PWA reacts to:

- `chat_streaming_started` / `chat_streaming_done` / `chat_result_ready`: lifecycle of the main chat turn.
- `chat_subagents_ready`: emitted when a background `Agent` (run_in_background) finishes or its count drops. Fields: `{chat_id, project_id, remaining}`.
- `chat_runs_reported`: emitted on a chat once finished background command runs have been reported back to it. Fields: `{chat_id, project_id, count, delivery}`, where `delivery` is `"queued"` (the chat was mid-turn, so the wake was appended as a follow-up) or `"started"` (the chat was idle, so a new turn began). Completions inside a 5s window coalesce into one event.
- `chat_read`: another client/device marked the chat read.
- `chat_unread`: another client/device marked the chat unread on purpose ("come back to this"). Fields: `{chat_id, last_read_at}` (empty string). The PWA clears the local read stamp so the dot and OS badge rise again.
- `chat_title`: auto-title finished.
- `chat_created`: a new chat was created (fresh or fork). Fields: `{chat: ChatInfo}`. The acting tab already pushes optimistically; this event is what makes other tabs/devices, or the acting tab after a racing `syncLatest` clobber, render the chat without waiting for the 15s poll. Without it a fork (which starts no streaming turn, so no `chat_result_ready` refetch) stayed invisible until a manual reload.
- `chat_moved` / `chat_archived` / `chat_deleted`: project changes.
- `chat_postprocess`: the memory pass queued by archiving a chat reporting on that chat. Fields: `{chat_id, project_id, postprocess}`, where `postprocess` is `{steps: {memory_pass: {status: "queued"|"running"|"ok"|"attention", extra: {chat_id}}}, updated_at}` and `extra.chat_id` is the pass's own chat. The same object is persisted on the archived chat and returned as `ChatInfo.postprocess`, so it can still link to its memory pass after a reload.
- `workspaces_changed`: a workspace was archived or restored (any tab or device). No payload; clients refetch `GET /api/workspaces` and `GET /api/projects`, so the sidebar and pickers stop offering an archived workspace without a reload.
- `schedules_changed`: an automation was created, edited, paused, resumed, or deleted (REST route, Automations page, or the `schedule_*` MCP tools mid-turn). No payload; the client refetches `GET /api/schedules`, which is where the computed `next_run` / `missed` / `context_available` fields are assembled. Without it an automation created by the model stayed invisible (no chat banner, no sidebar `↻` marker) until a manual reload. The deprecated `loops_changed` alias was removed with the loop MCP tools (#441); existing clients listen for `schedules_changed`.
- `server_restarting`: restart drain began (`{message}`). The connect `snapshot` also carries `restarting: true` when drain is already in progress so late clients show the overlay without waiting for a turn rejection.

Per-chat `/ws/chat/{chat_id}` events include text/thinking deltas, `tool_use` (with optional `file_touch` and provider-native `request_id`), `permission_request`, `model_capability_question`, `tool_denied`, `result`, `user_echo`, `queued`, `queue_state`, `steered`, `status`, `error`, and `server_restarting`. Permission and native-form cards stay visible until the provider acknowledges the reply: the server emits `permission_response_result` / `question_response_result` for the requesting client and ephemeral `permission_resolved` / `question_resolved` controls to other attached subscribers. Results are correlated by `request_id` and optional `session_id`; the PWA keeps the complete response frame in memory and retries it after a closed socket, while the persisted `pending_permission` / `pending_question` snapshot reconciles tabs that missed the live control. A `message` frame that reaches the server always starts its turn: the stream is registered before any socket write, so a client that disconnects right after sending (mobile/webview suspension) still gets the turn, and the reconnecting socket replays the buffered `user_echo` from the broker. `server_restarting` is likewise sent instead of `error` when a new turn is rejected because restart drain is in progress. Client messages include normal `message`, `stop`, `permission_response`, `question_response`, and `capability_response`; structured questions use `question_response {request_id, session_id?, action: "reply"|"cancel", answers: {question_id: string[]}}`. `permission_response.approved` must be a JSON boolean, never a truthy string.

**Image-capability pre-flight**: when a turn carries images and the selected model cannot see them, the server pauses before dispatch and emits `model_capability_question {request_id, missing: "image_input", current_model, candidates: [{id, label, supports_vision?, disabled?}], timeout_s: 30}`. `candidates` leads with the current model (disabled) followed by all same-backend vision models (for `opencode`, every catalog entry with `images is True`; other providers have no non-vision models today). The PWA renders the full provider-filtered `ModelSelector` inline — vision models only — with the current model shown disabled, so the user can pick any suitable model in one step. The client answers with `capability_response {request_id, action, model_id?}`: `switch` re-dispatches the turn on `model_id` (the chat model is persisted and a `model_changed` event is emitted) and `cancel` (or the 30s timeout) closes the turn with a `status` bubble telling the user the images were not sent. The `picker` action is retained only for old clients. The question is skipped entirely for text-only turns and for unattended (automation) turns, which close with the bubble instead of waiting.

**Queue management**: while the assistant is streaming, the client can queue follow-up messages (mode `queue`). Each queued item gets an `id` and is flushed as its own user turn once the prior turn finishes. When that turn starts, its `user_echo` includes `entry_id` so the client removes only the flushed item and keeps later queue entries visible. The client can also send `queue_reorder {entry_id, before_id}` (move `entry_id` before `before_id`, or to the end when `before_id` is null), `queue_edit {entry_id, text, images?}`, and `queue_remove {entry_id}`. The server confirms with `queue_state {queue: [{id, text, images?}]}` so connected clients stay in sync.

**Auto tier-fallback status events**: when the primary model returns a capability error (image input, tool use, context length, etc.), the server emits a `status` event with a "retrying on &lt;model&gt;" message, then runs the retry and emits the normal `result` for the new model. The terminal `result.effective_model` is the retry target's id. Rate limits, auth errors, content filters, and 5xx do NOT trigger this path; only Claude chats pinned to a bare tier alias participate.

**Message timings**

Each user turn carries timing metadata, computed in `ciao/web/project_chats.py` (provider-agnostic) and persisted under `ChatInfo.user_turn_timings` as `{ "<turn_index>": {sent_at, completed_at, duration_ms} }`.

- `GET /api/chats/{chat_id}/messages`: user entries include `sent_at`; the last assistant entry per turn includes `sent_at` (= `completed_at`) and `duration_ms`. Overlay is applied to provider history. Pre-feature chats with no recorded timings get no extra fields. With an `offset` and/or `limit` query param the endpoint returns a `{items, total, offset, limit, hasMore, nextOffset}` envelope instead of a flat array; `offset=0` is the newest tail. Envelope rows carry `i` (absolute index) and long `_thinking` rows are truncated head+tail with `lazy: true`; the full row is fetchable from `GET /api/chats/{chat_id}/messages/part?i=<index>`. Without params the legacy flat array is returned unchanged.
- WS `/ws/chat/{chat_id}` `user_echo` event: adds optional `sent_at`.
- WS `/ws/chat/{chat_id}` `result` event: adds optional `sent_at`, `completed_at`, `duration_ms`.

**Unattended turns (automation ticks)**

An automation fires its prompt as an ordinary user turn, so without a marker it is indistinguishable from something the user typed — the model read its own recurring prompt as a live message and replied "even though you're actively messaging me".

- WS `user_echo` gains `unattended: true` on such turns; `GET /api/chats/{chat_id}/messages` sets the same flag on the user entry, read back from `ChatInfo.user_turn_unattended` (keyed by turn index). The SDK session file records no sender, so the flag has to come from our own per-turn record. Absent = interactive, so old chats are unaffected.
- The PWA renders a `↻ auto` marker in the bubble footer.
- The model gets a matching line inside the injected-context block (stripped from rendered history) telling it the turn is unattended, that nobody is watching, and not to ask questions or wait for approvals.
- Permission mode for these turns is `bypass`: an escalation would be auto-denied ("Scheduled runs cannot wait for interactive approval"), which silently broke any automation needing network access or a first-time write. Deny rules still apply.

**Automation banner**

The PWA shows a banner at the top of a chat when an automation is bound to it. A project-bound automation is 1:many (it spawns a new chat each run), so the chat carries a durable backlink: `ChatInfo.schedule_id` and `ChatInfo.schedule_title`, stamped in `prepare_schedule_chat` for both the project (new chat) and fixed-chat (`web_chat_id`) branches and included in every chat object via `to_dict()`. The banner filters `GET /api/schedules` where `s.schedule_id === chat.schedule_id` OR `s.web_chat_id === chat.chat_id` (the second arm covers every fixed-chat automation, including the interval entries that replaced loops). Each row links to `/schedules/<id>` (Manage) and offers Pause/Resume (`PATCH {"enabled": bool}`) and Run now (`POST /api/schedule-run/{schedule_id}`). Existing chats predating the field stay empty-string and render no banner.

**File-touch cards**

Write/Edit/MultiEdit/NotebookEdit tool calls flow through both transports tagged with `file_touch`. The PWA renders each card chronologically inside expanded `Activity`, plus a deduplicated `Outputs` chip below the final answer. If a turn is interrupted before producing a final answer, the chip remains inside `Activity` so the touched file is not hidden.

- WS `/ws/chat/{chat_id}` `tool_use` event: adds optional `file_touch: {file_path, action}` when the tool mutates a file on disk. Detection lives in `extract_file_touch` (`ciao/web/chat_broker.py`); `action` is `written | edited`.
- `GET /api/chats/{chat_id}/messages` and `GET /api/chats/{chat_id}/subagents`: file-mutating tool calls become standalone `{role: "system", tool_name: "_filecard", file_path, action, tool, content: file_path}` entries instead of folding into `_activity`. Both provider readers honour this.
- Refused or failed calls get no card. `file_touch` is attached when a call is *requested*, so a denied `Write` used to paint an Outputs chip for a file that was never created. Live: the server publishes `tool_denied {tool_use_id}` on a deny and strips the touch from the replay buffer (the permission gate keys requests by `tool_use_id`, which is the same id the `tool_use` event carries). On reload: `/messages` and the subagent renderer skip the card when that call's `tool_result` came back `is_error`. The activity row stays either way, so the attempt is still visible.
- Card click opens `/api/workspace-file` (text/code) or `/api/workspace-image` (images by extension). The classification is advisory only. The viewer endpoints have no workspace sandbox: they serve any allowlisted-extension file on disk (relative paths anchor to `workspace_root`). The extension allowlist (no `.env`) and size caps are the only guards.

**HTML artifacts (`GET /api/workspace-html`)**

`.html` is in the `/api/workspace-file` text allowlist, so it already served as `text/plain` for the panel's Code view. `workspace-html` is the Preview side: the same file as `text/html`, under its own policy so the PWA can embed model-authored markup in a frame.

- Query `?path=` (workspace-relative or absolute, fuzzy-resolved like its siblings), `.html`/`.htm` only (415 otherwise), capped at 2 MB (413 over it). The cap deliberately matches the text viewer and `MAX_SNAPSHOT_BYTES`, so a file cannot be renderable but unreadable, or have history but refuse to render.
- Response headers: `_ARTIFACT_CSP` (`default-src 'none'`, `script-src 'unsafe-inline'`, `style-src 'unsafe-inline'`, `img-src data:`, `media-src data:`, `font-src data:`, `connect-src 'none'`, `form-action 'none'`, `base-uri 'none'`, `frame-ancestors 'self'`, `sandbox allow-scripts`), plus `X-Frame-Options: SAMEORIGIN` and `Cache-Control: no-cache`.
- Self-contained audio and video may use `data:` URLs. Network media and `blob:` URLs remain blocked, so artifacts do not need relative asset paths or access to workspace APIs.
- `script-src 'unsafe-inline'` is load-bearing: an artifact inlines its own script, so removing it breaks every artifact rather than hardening anything. Containment is `sandbox allow-scripts` without `allow-same-origin` (opaque origin: no session cookie, no `localStorage`, no parent access) plus `connect-src 'none'` and a `data:`-only `img-src`. An artifact cannot reach `/api/*` despite being same-host.
- `X-Frame-Options` must stay explicit: `SecurityHeadersMiddleware` sets `DENY` via `setdefault`, so an unconditional assignment there would break the frame. `tests/test_workspace_html.py` guards this.
- Client: `HtmlArtifactViewer.vue` (shared by `PinnedFilePanel` and `FileViewerModal`) frames it with `sandbox="allow-scripts"`, matching the CSP directive — the effective sandbox is the intersection of attribute and header. Source for Code view is a separate lazy fetch, because `error` blanks the viewer body and an oversized-source failure must not hide a page that renders.
- **Comments**: the response body carries an injected bridge script (`ciao/web/artifact_bridge.py`, inserted after `<head>` when present). Inside the frame it floats a Comment pill on text selection, supports Alt+Click to comment on a whole element, and sends the anchor — CSS selector plus character offsets into the element's text, plus the verbatim quote — to the panel over `postMessage`, the only channel an opaque-origin frame has. `HtmlArtifactViewer` relays those to `PinnedFilePanel`/`FileViewerModal`, which stage them as the same pending file comments as the markdown viewer (chips above the composer, `<user-comment-reference>` blocks with an `(element <tag>)` locator instead of line numbers). The panel also pushes the durable comment list back into the frame so past comments re-draw as `<mark>` highlights after each revision reload. Frame messages are untrusted (model-authored script could forge them); the worst case is a note in the user's own composer, reviewed before sending. The selector is not migration-proof across artifact revisions — the quote is the durable anchor the model reads.
- Manual checks live in `tests/fixtures/html_artifacts/` (inline script runs, external requests blocked, API/session unreachable). Header tests pass on a blank frame, so those fixtures are the real verification.

**File snapshots, history, diff, edit-in-place**

Every file-touch tool call also triggers a debounced (1.5s) content snapshot via `SnapshotStore` in `ciao/web/file_snapshots.py`. Snapshots are append-only files under `.runtime/snapshots/<chat_id>/<urlencoded_path>/NNNN.snap` with a sibling `meta.json`. Dedup hashes consecutive captures so re-firing the hook on identical content does not pollute history.

- `GET /api/file-history?chat_id=&file_path=` returns `{snapshots: [{seq, ts, action, tool, size, truncated?}]}`. Most recent last.
- `GET /api/file-content?chat_id=&file_path=&seq=` returns `{content: str, meta}`. 413 if the snapshot was over `MAX_SNAPSHOT_BYTES` at capture time, 415 if the snapshot was binary.
- `POST /api/file-restore` body `{chat_id, file_path, seq}` writes the snapshot back to its recorded host path (absolute paths are intentional) and captures a new snapshot with `action="restored"` so the timeline stays linear. Returns `{ok, restored_seq, new_seq}`.
- `POST /api/workspace-file` body `{chat_id?, path, content}` writes user-edited content back (FileViewerModal edit mode). It has the same intentional unrestricted host-path behavior and extension/size guards as the GET. When `chat_id` is supplied, the write is captured as a snapshot with `tool="PWAEdit"` so PWA edits show up in the history alongside agent edits.

**Vault**

```bash
# Permanently delete one vault note. Unlike workspace-file, this is scoped to
# config.vault_root — the path must be the Entry.path string form from
# /api/vault/graph, e.g. "memory-vault/work/People/Mo.md". Every other note
# that links to it (frontmatter related:/relatedTo: or a body [[wikilink]])
# is rewritten first so no dangling link is left behind. No undo.
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/vault/note?path=memory-vault/work/People/Mo.md"
```

## State

- Project and chat state: `.runtime/web_projects.json`. `.runtime/server.lock` prevents two backend processes from owning this registry, and `.runtime/web_projects.audit.jsonl` records append-only mutation IDs/revisions for repair without storing chat content. On-disk shape mirrors the `ProjectInfo` and `ChatInfo` dataclasses in `ciao/web/project_chats.py`; `to_dict()` on each defines the JSON fields. `ChatInfo.user_turn_timings` holds per-turn `{sent_at, completed_at, duration_ms}` keyed by user-turn index (as str); the matching `_turn_perf_started` map on `ProjectChatManager` is in-memory only.
- `ChatInfo.pending_question` (string, in `to_dict()` so it rides every chat list / chat object): raw AskUserQuestion JSON (`{"questions": [...], "request_id": ..., "session_id": ...}`) set when the model paused the chat on a question. Native V2 forms persist this field until the server acknowledges the matching `question_response` (including an explicit cancel); transport loss or a terminal stream frame is not an acknowledgement. The PWA reads it on chat open to rebuild the interactive question picker after a reload. Empty string when no question is pending.
- `ChatInfo.pending_permission` (string): the same durable acknowledgement boundary for a native permission, with `{request_id, session_id, tool_name, message, tool_input}`. It is cleared only by a successful/authoritatively stale response, not merely because the SSE turn ended or the browser disconnected.
- `ChatInfo.schedule_id` / `ChatInfo.schedule_title` (strings, in `to_dict()`): backlink to the schedule that created or drives the chat. Stamped in `prepare_schedule_chat` for both schedule branches (project-schedule new chat, fixed-chat reuse). Drives the automation banner in ChatPanel. Empty for interactive chats and for chats created before the field existed. See "Automation banner" above.
- Schedule state: `.runtime/schedules.json`. Shape and field semantics in `ciao/schedules.py` (`ScheduleEntry`); the `schedule_create`/`schedule_update` MCP tools carry the field semantics in their own docstrings.
- Automation state: `.runtime/schedules.json` (`ciao/schedules.py`, `ScheduleEntry`), every cadence in one store. A legacy `.runtime/loops.json` is imported once on startup as `frequency: "interval"` entries — the `loop-…` id is kept as the `schedule_id` so deep links still resolve — and renamed `loops.json.migrated`. Interval entries additionally carry `interval_minutes`; their `missed` is always false, since relative cadence has no expected slot to have missed. `last_status` (`""` | `running` | `ok` | `error` | `busy` | `missing-chat` | `skipped`) is carried by every cadence: it is stamped `running` at dispatch and replaced by the run's outcome, which is what lets a wall-clock slot tell a completed run apart from one whose turn died mid-flight. A wall-clock entry whose run for the expected slot ended in `error` reports `missed: true` so it can be re-run from the UI (issue #486). `last_status` describes one dispatch, not the entry's whole history: `last_dispatch_id` (`<slot-day>#<token>`) names the dispatch it belongs to, so a run superseded by an overlapping one — a manual "Run now" started just before the cron slot — no longer writes its result over the current run's, while `last_completed_on` records the occurrence a completed run served so an already-done slot is never replayed because a later run for it failed (issue #490).
- Uploaded media: under the configured runtime/media directory

## Naming

See `README.md` "Project naming convention" for folder layout, frontmatter, and the auto-created `General` project.
