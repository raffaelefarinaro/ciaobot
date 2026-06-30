# PWA API

Local HTTP API for the Ciao PWA. Default base URL is `http://localhost:$PWA_PORT` with `PWA_PORT=8443`.

The route source of truth is `ciao/web/app.py`. This file is kept in sync by `tests/test_pwa_api_docs.py`.

## Auth And Browser Security

- `POST /api/auth` accepts `{"token": "<PWA_AUTH_TOKEN>"}` and returns an HttpOnly `ciao_session` cookie.
- `GET /?setup=<token>` is the local first-launch shortcut path. It is accepted only on `localhost`, `127.0.0.1`, or `::1`; when the token matches `.runtime/setup-token`, the server sets the same signed `ciao_session` cookie, deletes the token file, and redirects to `/`.
- Production cookies are `Secure`, `SameSite=Lax`, and host-only (scoped to the exact host that served them).
- `POST /api/auth/logout` clears the same host-only cookie.
- All `/api/*` routes except `POST /api/auth`, `GET /api/startup-status`, `GET /api/setup-status`, and `POST /api/setup/finish` require the signed session cookie. All `/ws/*` routes require the signed session cookie.
- `POST /api/setup/finish` is only accepted in bootstrap mode from localhost with a matching browser origin/referer. It writes the real workspace config, creates local launch artifacts, and asks the supervisor to restart into the configured workspace.
- State-changing `/api/*` requests with an `Origin` or `Referer` header must match the request host. Missing headers are accepted for non-browser clients.
- HTTP responses include baseline security headers, including CSP, `X-Content-Type-Options`, `Referrer-Policy`, and frame denial.

## Routes

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth` | Login with `PWA_AUTH_TOKEN` |
| POST | `/api/auth/logout` | Clear session cookie |
| GET | `/api/auth/check` | Verify current session |
| GET | `/api/projects` | List projects |
| POST | `/api/projects` | Create project |
| PATCH, DELETE | `/api/projects/{project_id}` | Update or delete project |
| POST | `/api/projects/{project_id}/complete` | Complete a vault-backed project |
| GET | `/api/projects/completed` | List completed projects (vault `completed/` scan) |
| POST | `/api/projects/completed/restore` | Restore a completed project to active |
| GET, POST | `/api/projects/{project_id}/chats` | List or create project chats |
| GET, POST | `/api/projects/{project_id}/files` | List or upload project files |
| GET | `/api/chats` | List all chats |
| POST | `/api/chats/read-all` | Mark all chats read |
| PATCH, DELETE | `/api/chats/{chat_id}` | Update or delete chat |
| POST | `/api/chats/{chat_id}/new` | Start a new provider session |
| POST | `/api/chats/{chat_id}/handover` | Continue chat on a fresh provider session |
| POST | `/api/chats/{chat_id}/archive` | Archive chat |
| POST | `/api/chats/{chat_id}/continue` | Create a new active chat continuing from this archived one |
| POST | `/api/chats/{chat_id}/read` | Mark chat read |
| POST | `/api/chats/{chat_id}/retry` | Set, stop, or run deferred chat retry |
| POST | `/api/chats/{chat_id}/prompt` | Send a prompt to start a background turn in the chat |
| GET | `/api/chats/{chat_id}/messages` | Load persisted chat messages |
| GET | `/api/chats/{chat_id}/subagents` | Load subagent transcripts |
| POST | `/api/chats/{chat_id}/voice` | Upload voice for transcription |
| POST | `/api/chats/{chat_id}/images` | Upload chat images |
| GET | `/api/images/{ref}` | Read uploaded image blob |
| GET | `/api/workspace-file` | Read allowed text file |
| POST | `/api/workspace-file` | Write user-edited text file (sandbox + snapshot) |
| GET | `/api/workspace-image` | Read allowed image file |
| GET | `/api/workspace-binary` | Read allowed binary file |
| GET | `/api/file-history` | List snapshots for a `(chat_id, file_path)` |
| GET | `/api/file-content` | Read one snapshot's content |
| POST | `/api/file-restore` | Restore a snapshot to disk |
| GET, POST | `/api/schedules` | List or create schedules |
| POST | `/api/schedule-run/{schedule_id}` | Run schedule now |
| PATCH, DELETE | `/api/schedules/{schedule_id}` | Update or delete schedule |
| GET | `/api/automation` | Background-job status (Settings → Automation): last run, duration, model, errors per process |
| GET | `/api/commands` | List slash commands |
| GET | `/api/rate-limits` | Read Claude rate-limit snapshots |
| GET | `/api/models` | List configured models |
| GET, PATCH | `/api/status` | Read or update status |
| GET | `/api/startup-status` | Read startup phase progress |
| GET | `/api/setup-status` | Read first-run setup checks and provider readiness |
| GET | `/api/package/status` | Read installed package version and best-effort latest-version status |
| POST | `/api/package/update` | Upgrade ciao package and restart |
| POST | `/api/setup/finish` | Finish first-run setup from bootstrap mode |
| GET | `/api/stats` | Read CLI stats |
| GET | `/api/workspaces` | List configured logical workspaces |
| GET | `/api/push/public-key` | Read VAPID public key |
| POST | `/api/push/subscribe` | Store push subscription |
| POST | `/api/push/unsubscribe` | Remove push subscription |
| GET | `/api/push/status` | Read push setup status |
| GET | `/api/push/subscription` | Check one subscription |
| GET | `/api/local/status` | Device-session state: device name, `dev/<device>` branch, dirty |
| GET | `/api/local/preflight` | Git preflight check for dirty files, categories, blockers/warnings |
| POST | `/api/local/handback` | Commit + push the device branch and try to merge it into `main` |
| POST | `/api/local/resync` | Re-point the device branch at the latest `main` |
| POST | `/api/handover/merge` | Open an interactive chat that merges a branch into `main` |
| POST | `/api/admin/snapshot` | Git add, commit, and push snapshot |
| POST | `/api/admin/deploy` | Reinstall deps, rebuild frontend, and restart with latest code |
| GET | `/api/admin/status` | Read admin/deploy status |
| GET | `/api/admin/skills` | List skills labelled as custom or GitHub/package |
| WS | `/ws/chat/{chat_id}` | Per-chat streaming socket |
| WS | `/ws/events` | Global event socket |

## Agent recipes

Concrete curl examples for the in-session agent acting on the local API. Auth once, reuse the cookie jar.

**Auth**

```bash
source .env
curl -sS -c /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/auth" \
  -H 'content-type: application/json' \
  -d "{\"token\":\"$PWA_AUTH_TOKEN\"}"
```

Reuse the jar with `-b /tmp/ciao.jar` on every other call. The Origin/Referer host-match check is skipped when those headers are absent, so plain curl works.

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

**Chats**

```bash
# Create — title/model/mode/provider/model_bucket all optional.
# provider ∈ {claude, pi}. model_bucket is optional and Claude-only:
# '' = auto from the project's configured workspace bucket. Legacy
# work/personal buckets still work; anthropic/ollama are the clearer
# configured names. Unknown buckets are rejected unless a workspace config
# defines them.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/projects/$PID/chats" \
  -H 'content-type: application/json' \
  -d '{"title":"Tile layout"}'

# Update — title, model, provider, model_bucket, mode, project_id (to move
# between projects), thinking_level. thinking_level is provider-native
# ('' = provider default, allowed values per provider in GET /api/models →
# thinking_levels) and is safe to change mid-chat; it resets to '' on
# handover. Changing model/provider/model_bucket across a routing boundary
# on a started chat returns 400; use handover instead.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/chats/$CID" \
  -H 'content-type: application/json' -d '{"thinking_level":"high"}'

# Handover — switch provider/model inside the same visible chat.
# Body keys: provider ∈ {claude, pi}, model, model_bucket (optional,
# Claude only), messages (visible rows).
# Starts the next provider turn as a fresh session seeded with those messages.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/handover" \
  -H 'content-type: application/json' \
  -d '{"provider":"pi","model":"openai-codex/gpt-5.5","messages":[{"role":"user","content":"continue this task"},{"role":"assistant","content":"current state"}]}'

# Archive — finalises the chat and writes a Markdown transcript. Returns {ok, archived_to}.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/archive"

# Mark read — returns {ok, last_read_at}.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/read"

# Mark all read.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/read-all"

# Deferred retry after provider/session quota errors. action ∈ {set, try_now, stop}.
# `set` needs the user prompt to replay; automatic quota handling fills this itself.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/retry" \
  -H 'content-type: application/json' \
  -d '{"action":"set","prompt":"retry this turn"}'
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/retry" \
  -H 'content-type: application/json' -d '{"action":"try_now"}'
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/retry" \
  -H 'content-type: application/json' -d '{"action":"stop"}'

# Start a new provider session inside an existing chat (resets context).
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/new"

# Delete.
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/chats/$CID"
```

**Schedules and ops**

```bash
# Create a routine with archive behavior. archive_policy ∈ manual|auto.
# `auto` runs a post-run classifier and archives only when Raffa does not need to see it.
# GET /api/schedules enriches each entry with `next_run` (next fire, ISO or null),
# `last_expected_run` (most recent due fire, ISO or null), and `missed` (true when a
# due fire was never recorded — surfaced in the Schedules overview).
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/schedules" \
  -H 'content-type: application/json' \
  -d '{"time":"01:00","timezone":"Europe/Zurich","frequency":"daily","prompt":"Memory curation","web_project_id":"proj-...","workspace":"personal","archive_policy":"auto"}'

# Update archive behavior.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/schedules/$SID" \
  -H 'content-type: application/json' \
  -d '{"archive_policy":"auto"}'

# Run a schedule on demand. Auto-archived routines can return archived_to.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/schedule-run/$SID"

# Deploy: snapshot, pull, build, restart. Don't call from inside the live PWA session
# (CLAUDE.md "Never restart the ciao service yourself"); ask Raffa to hit Deploy.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/admin/deploy"
```

**Routine settings (Settings → Models tab)**

```bash
# Read internal-routine settings: title, insights, and critique model overrides
# (plus the effective models after defaults), transcription engine, and grouped
# model options (anthropic / ollama_cloud / ollama_local).
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/settings/routines"

# Update any subset. Persisted in .runtime/app_settings.json, applied to the
# live config immediately (no restart). Empty string clears an override back
# to the env default. transcription_engine ∈ {cloud, local}; "local" uses
# mlx-whisper on-device (free) and falls back to cloud when unavailable.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/settings/routines" \
  -H 'content-type: application/json' \
  -d '{"title_model":"gemma4:12b-it-qat","critique_models":"openrouter/anthropic/claude-3.7-sonnet","transcription_engine":"local"}'
```

**Commit-to-main / handover flow** (`CIAO_DEVICE_NAME`)

Every instance works on its own `dev/<device_name>` branch cut from `origin/main`. Handback
commits + pushes the branch, then tries to merge it into `main`: a clean merge is pushed
directly (response: `{merged:true, deploy_needed:false, pushed}`); a conflict aborts the merge and
opens an interactive chat (`{merged:false, conflict:true, merge:{chat_id,...}}`) that resolves
it, asking you (push-notified) when ambiguous. After that chat lands `main`, resync re-points
the device branch at it. Workspace handback never deploys app code; app updates happen through
the package install/upgrade path.

```bash
# Current device-session state (device name, dev/<device> branch, dirty).
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/local/status"

# Commit to main — commit + push the device branch and try to merge into main.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/local/handback"

# After a conflict chat merged main, re-point the device branch at it.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/local/resync"

# Open an interactive merge chat for a branch by hand (also used on conflict).
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/handover/merge" \
  -H 'content-type: application/json' -d '{"branch":"dev/laptop"}'
```

When adding a new state-changing route (`POST/PATCH/DELETE /api/...`), add an entry here or add the path to `BROWSER_OR_INTERNAL_ROUTES` in `tests/test_pwa_api_docs.py` with a one-line reason. The doc-sync test enforces this.

**Message timings**

Each user turn carries timing metadata, computed in `ciao/web/project_chats.py` (provider-agnostic) and persisted under `ChatInfo.user_turn_timings` as `{ "<turn_index>": {sent_at, completed_at, duration_ms} }`.

- `GET /api/chats/{chat_id}/messages`: user entries include `sent_at`; the last assistant entry per turn includes `sent_at` (= `completed_at`) and `duration_ms`. Overlay is applied for Claude (SDK) and Pi sessions via `_overlay_assistant_timings` in `ciao/web/routes_api.py`. Pre-feature chats with no recorded timings get no extra fields.
- WS `/ws/chat/{chat_id}` `user_echo` event: adds optional `sent_at`.
- WS `/ws/chat/{chat_id}` `result` event: adds optional `sent_at`, `completed_at`, `duration_ms`.

**File-touch cards**

Write/Edit/MultiEdit/NotebookEdit tool calls flow through both transports tagged with `file_touch` so the PWA can render a clickable inline preview card next to the agent's reasoning trace.

- WS `/ws/chat/{chat_id}` `tool_use` event: adds optional `file_touch: {file_path, action}` when the tool mutates a file on disk. Detection lives in `extract_file_touch` (`ciao/web/chat_broker.py`); `action` is `written | edited`.
- `GET /api/chats/{chat_id}/messages` and `GET /api/chats/{chat_id}/subagents`: file-mutating tool calls become standalone `{role: "system", tool_name: "_filecard", file_path, action, tool, content: file_path}` entries instead of folding into `_activity`. Claude and Pi readers honour this.
- Card click opens `/api/workspace-file` (text/code) or `/api/workspace-image` (images by extension). The classification is advisory only; the workspace endpoints stay the security boundary and 403 anything outside `workspace_root` + `extra_workspace_roots`.

**File snapshots, history, diff, edit-in-place**

Every file-touch tool call also triggers a debounced (1.5s) content snapshot via `SnapshotStore` in `ciao/web/file_snapshots.py`. Snapshots are append-only files under `.runtime/snapshots/<chat_id>/<urlencoded_path>/NNNN.snap` with a sibling `meta.json`. Dedup hashes consecutive captures so re-firing the hook on identical content does not pollute history.

- `GET /api/file-history?chat_id=&file_path=` returns `{snapshots: [{seq, ts, action, tool, size, truncated?}]}`. Most recent last.
- `GET /api/file-content?chat_id=&file_path=&seq=` returns `{content: str, meta}`. 413 if the snapshot was over `MAX_SNAPSHOT_BYTES` at capture time, 415 if the snapshot was binary.
- `POST /api/file-restore` body `{chat_id, file_path, seq}` writes the snapshot back to disk (sandboxed against allowed roots) and captures a new snapshot with `action="restored"` so the timeline stays linear. Returns `{ok, restored_seq, new_seq}`.
- `POST /api/workspace-file` body `{chat_id?, path, content}` writes user-edited content back (FileViewerModal edit mode). Same sandbox as the GET. When `chat_id` is supplied, the write is captured as a snapshot with `tool="PWAEdit"` so PWA edits show up in the history alongside agent edits.

## State

- Project and chat state: `.runtime/web_projects.json`. On-disk shape mirrors the `ProjectInfo` and `ChatInfo` dataclasses in `ciao/web/project_chats.py` (`class ProjectInfo:` around line 339, `class ChatInfo:` around line 373); `to_dict()` on each defines the JSON fields. `ChatInfo.user_turn_timings` holds per-turn `{sent_at, completed_at, duration_ms}` keyed by user-turn index (as str); the matching `_turn_perf_started` map on `ProjectChatManager` is in-memory only.
- `ChatInfo.pending_question` (string, in `to_dict()` so it rides every chat list / chat object): raw AskUserQuestion JSON (`{"questions": [...]}`) set when the model paused the chat on a question. When the headless CLI fires AskUserQuestion the server interrupts the live turn so the CLI cannot auto-answer it, persists this field, and clears it on the next user send. The PWA reads it on chat open to rebuild the interactive question picker after a reload. Empty string when no question is pending.
- Schedule state: `.runtime/schedules.json`. Shape and field semantics in `ciao/schedules.py` (`ScheduleEntry`); the `ciao-schedules` skill packs the create/edit recipes.
- Uploaded media: under the configured runtime/media directory

## Naming

See `README.md` "Project naming convention" for folder layout, frontmatter, and the auto-created `General` project.
