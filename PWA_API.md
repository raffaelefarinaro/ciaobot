# PWA API

Local HTTP API for the Ciaobot PWA. Default base URL is `http://localhost:$PWA_PORT` with `PWA_PORT=8443`.

The route source of truth is `ciao/web/app.py`. This file is kept in sync by `tests/test_pwa_api_docs.py`.

## Auth And Browser Security

- `POST /api/auth` accepts `{"token": "<PWA_AUTH_TOKEN>"}` and returns an HttpOnly `ciao_session` cookie.
- `GET /?setup=<token>` is the local first-launch shortcut path. It is accepted only on `localhost`, `127.0.0.1`, or `::1`; when the token matches `.runtime/setup-token`, the server sets the same signed `ciao_session` cookie, deletes the token file, and redirects to `/`.
- Production cookies are `Secure`, `SameSite=Lax`, and host-only (scoped to the exact host that served them).
- `POST /api/auth/logout` clears the same host-only cookie.
- All `/api/*` routes except `POST /api/auth`, `GET /api/startup-status`, `GET /api/active-chats`, `GET /api/setup-status`, `POST /api/setup/finish`, `GET /api/setup/list-dirs`, and `POST /api/setup/mkdir` require the signed session cookie. All `/ws/*` routes require the signed session cookie.
- `POST /api/setup/finish` is only accepted in bootstrap mode from localhost with a matching browser origin/referer (off-localhost requests get a 403 pointing at `http://localhost:<port>`). Body: `workspace` (required — the root folder holding the vault plus app data), `vault_root` (optional, default `<workspace>/memory-vault`; absolute or `~` paths are honored for an existing notes folder elsewhere), plus optional `vault_mode`, `push_contact`, `port`, `python`, `auth_required`, `launch_agents_dir`, `app_dir`, and `restart`. It writes the real workspace config, ensures workspace and vault are (in) git repos, creates local launch artifacts, and asks the supervisor to restart into the configured workspace.
- `GET /api/setup/list-dirs` and `POST /api/setup/mkdir` back the setup wizard's folder picker. They are only accepted in bootstrap mode from localhost with a matching browser origin/referer (404 outside bootstrap mode, 403 off-localhost), list directories only, and never read file contents.
- State-changing `/api/*` requests with an `Origin` or `Referer` header must match the request host. Missing headers are accepted for non-browser clients.
- HTTP responses include baseline security headers, including CSP, `X-Content-Type-Options`, `Referrer-Policy`, and frame denial.
- The agent-facing `/mcp/` mount uses a separate scoped bearer capability issued to Ciaobot-managed provider processes; it does not accept the browser session cookie. `GET /api/mcp/status` exposes only readiness and catalog metadata, never a token.

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
| POST | `/api/chats/{chat_id}/fork` | Fork chat continuing from a completed turn |
| GET | `/api/chats/{chat_id}/provider-subchats` | List provider sub-chats |
| POST | `/api/chats/{chat_id}/provider-subchats` | Create provider sub-chat |
| GET | `/api/provider-subchats/{subchat_id}/events` | Read provider sub-chat events |
| POST | `/api/provider-subchats/{subchat_id}/messages` | Send message to provider sub-chat |
| POST | `/api/provider-subchats/{subchat_id}/close` | Close provider sub-chat |
| POST | `/api/provider-subchats/{subchat_id}/cancel` | Cancel active provider sub-chat work |
| POST | `/api/provider-subchats/{subchat_id}/extend` | Extend provider sub-chat limits |
| POST | `/api/provider-subchats/{subchat_id}/permission-response` | Resolve permission request in provider sub-chat |
| POST | `/api/provider-subchats/{subchat_id}/question-response` | Resolve structured question in provider sub-chat |
| POST | `/api/chats/{chat_id}/archive` | Archive chat |
| POST | `/api/chats/{chat_id}/continue` | Create a new active chat continuing from this archived one |
| POST | `/api/chats/{chat_id}/read` | Mark chat read |
| POST | `/api/chats/{chat_id}/retry` | Set, stop, or run deferred chat retry |
| POST | `/api/chats/{chat_id}/prompt` | Send a prompt to start a background turn in the chat |
| GET | `/api/open-chat/{chat_id}` | Focus an existing chat in the PWA and emit the local open-chat event |
| GET | `/api/chats/{chat_id}/messages` | Load persisted chat messages |
| GET | `/api/chats/{chat_id}/subagents` | Load subagent transcripts |
| POST | `/api/chats/{chat_id}/voice` | Upload voice for transcription |
| POST | `/api/chats/{chat_id}/speak` | Synthesize speech for a message; returns audio bytes |
| POST | `/api/chats/{chat_id}/images` | Upload chat images |
| GET | `/api/images/{ref}` | Read uploaded image blob |
| GET | `/api/workspace-file` | Read allowed text file |
| POST | `/api/workspace-file` | Write user-edited text file (allowlist + snapshot) |
| GET | `/api/workspace-image` | Read allowed image file |
| GET | `/api/workspace-binary` | Read allowed binary file |
| GET | `/api/libreoffice-status` | Whether LibreOffice (`soffice`) is available to render `.pptx` previews |
| POST | `/api/libreoffice-install` | Install LibreOffice via Homebrew Cask (macOS); no restart needed |
| POST | `/api/apfel/install` | Install apfel (Apple Intelligence CLI) via Homebrew (macOS); no restart needed |
| POST | `/api/workspace-open` | Open a workspace file with the OS default app on the machine running Ciao |
| GET | `/api/file-history` | List snapshots for a `(chat_id, file_path)` |
| GET | `/api/file-content` | Read one snapshot's content |
| GET | `/api/vault-markdown-paths` | List workspace-relative markdown paths (file viewer resolves Obsidian wikilinks) |
| POST | `/api/file-restore` | Restore a snapshot to disk |
| GET, POST | `/api/schedules` | List or create schedules |
| POST | `/api/schedule-run/{schedule_id}` | Run schedule now |
| PATCH, DELETE | `/api/schedules/{schedule_id}` | Update or delete schedule |
| GET, POST | `/api/loops` | List or create in-chat loops (re-dispatch a prompt into a fixed chat every N minutes) |
| POST | `/api/loop-run/{loop_id}` | Fire one loop iteration now (409 when the chat has a turn in flight) |
| PATCH, DELETE | `/api/loops/{loop_id}` | Update, start/stop (`{"running": bool}`), or delete a loop |
| GET | `/api/automation` | Background-job status (Settings → Automation): last run, duration, model, errors per process |
| POST | `/api/automation/backfill-insights` | Trigger background backfill of Session Insights into old archived chats |
| GET | `/api/debug/issues` | Runtime issue report (server error log tail + failed job runs) for the dev-mode "Fix issues in chat" flow; 404 unless `CIAO_DEV_MODE` is set |
| GET | `/api/commands` | List slash commands |
| GET | `/api/agent-assets` | List instruction sources, subagents, slash commands, and workspace health for Settings |
| GET | `/api/workspace-health` | Scan workspace/vault/discovery-file health |
| POST | `/api/workspace-health/fix` | Apply the automatic remedies (create missing scaffold files, re-link skills); returns the fresh report |
| POST | `/api/agent-assets/subagents` | Create a workspace-owned subagent and vault mirror |
| PATCH, DELETE | `/api/agent-assets/subagents/{name}` | Update or delete a custom workspace-owned subagent |
| POST | `/api/agent-assets/commands` | Create a workspace-owned slash command and vault mirror |
| PATCH, DELETE | `/api/agent-assets/commands/{name}` | Update or delete a custom workspace-owned slash command |
| GET | `/api/rate-limits` | Read Claude rate-limit snapshots |
| GET | `/api/models` | List configured models |
| GET, PATCH | `/api/status` | Read or update status |
| GET | `/api/mcp/status` | Embedded Ciaobot MCP readiness, tool catalog, and active-session counts (no credentials) |
| GET | `/api/mcp/usage` | Embedded Ciaobot MCP per-tool call/error counters (no credentials) |
| GET | `/api/startup-status` | Read startup phase progress |
| GET | `/api/active-chats` | List chat IDs with in-flight work (streaming or background subagents); drives the macOS menu bar spinner |
| GET | `/api/setup-status` | Read first-run setup checks and provider readiness |
| GET | `/api/package/status` | Read installed package version and best-effort latest GitHub release version |
| GET | `/api/package/changelog` | List commits between the installed and latest release for the update prompt |
| POST | `/api/package/update` | Upgrade ciaobot (`brew upgrade ciaobot` or latest release wheel) and restart |
| POST | `/api/voice/install-local` | Install local voice transcription dependencies and restart |
| POST | `/api/tts/install-local` | Install local speech synthesis dependencies (kokoro-onnx) and restart |
| POST | `/api/setup/finish` | Finish first-run setup from bootstrap mode |
| GET | `/api/setup/list-dirs` | List local subdirectories for the setup wizard folder picker (bootstrap mode, localhost only) |
| POST | `/api/setup/mkdir` | Create a folder from the setup wizard folder picker (bootstrap mode, localhost only) |
| GET | `/api/stats` | Read CLI stats |
| GET | `/api/workspaces` | List configured logical workspaces |
| POST | `/api/workspaces/{name}` | Add or update a logical workspace config |
| DELETE | `/api/workspaces/{name}` | Delete a logical workspace config |
| GET, PATCH | `/api/settings/providers` | Read or update keys used directly by Ciaobot |
| POST | `/api/settings/providers/{provider}/{action}` | Connect, verify, or log out through the Claude Code or Codex CLI |
| GET | `/api/integrations/gws` | Read Google Workspace CLI install, profile auth, and workspace usage status |
| POST | `/api/integrations/gws/install` | Install the `@googleworkspace/cli` (`gws`) binary globally via npm |
| POST | `/api/integrations/gws/client-secret` | Upload GCP client_secret.json for a profile |
| POST | `/api/integrations/gws/auth-url` | Generate Google OAuth authorization URL for a profile |
| POST | `/api/integrations/gws/exchange` | Complete Google OAuth flow and exchange code for credentials |
| POST | `/api/integrations/gws/disconnect` | Disconnect Google profile and clean up local credentials/client_secret |
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
| POST | `/api/handover/merge` | Open an interactive chat that resolves sync conflicts on a branch |
| POST | `/api/admin/snapshot` | Git add, commit, and push snapshot |
| POST | `/api/admin/deploy` | Reinstall deps, rebuild frontend, and restart with latest code |
| GET | `/api/admin/status` | Read admin/deploy status |
| GET | `/api/admin/skills` | List skills labelled as custom or GitHub/package |
| POST | `/api/admin/skills/add` | Add an upstream skill from GitHub and synchronize it |
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

**Agent assets**

```bash
# Inspect Claude Code context sources, generated Ciaobot prompt blocks, subagents, and commands.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/agent-assets"

# Inspect workspace/vault health only.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/workspace-health"

# Create a workspace-owned subagent.
# Writes subagents/<name>.md, mirrors a vault note under memory-vault/Workspace/Subagents/,
# then syncs the subagent into .claude/agents/.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/agent-assets/subagents" \
  -H 'content-type: application/json' \
  -d '{"name":"pr-reviewer","description":"Review pull-request diffs for regressions.","prompt":"Inspect the changed files, identify concrete risks, and report findings first."}'

# Update or delete a custom subagent. Installed/system subagents are read-only.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/agent-assets/subagents/pr-reviewer" \
  -H 'content-type: application/json' \
  -d '{"description":"Review pull-request diffs for regressions.","content":"# Pr Reviewer\n\nInspect changed files, identify concrete risks, and report findings first."}'
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/agent-assets/subagents/pr-reviewer"

# Create a workspace-owned slash command.
# Writes commands/<name>.md, mirrors a vault note under memory-vault/Workspace/Commands/,
# then syncs it into .claude/commands/ plus a Codex .agents/skills/ wrapper.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/agent-assets/commands" \
  -H 'content-type: application/json' \
  -d '{"name":"decision-record","description":"Turn notes into a decision record.","argument_hint":"<notes>","prompt":"Convert $ARGUMENTS into a concise decision record with context, decision, and consequences."}'

# Update or delete a custom slash command. Installed/system commands are read-only.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/agent-assets/commands/decision-record" \
  -H 'content-type: application/json' \
  -d '{"description":"Turn notes into a decision record.","argument_hint":"<notes>","content":"# Decision Record: $ARGUMENTS\n\nConvert $ARGUMENTS into a concise decision record with context, decision, and consequences."}'
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/agent-assets/commands/decision-record"
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

Project file uploads are limited to 50 MB per file. File-list responses use
workspace-relative viewer paths when the vault is nested under the workspace
and absolute viewer paths when `CIAO_VAULT_ROOT` points elsewhere.

**Chats**

```bash
# Create — title/model/mode/provider/model_bucket all optional.
# provider is `claude` or `codex`. model_bucket only controls Claude backends:
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
# control_surface (legacy|mcp|auto|'') is still accepted here as an escape
# hatch, but it is engine-controlled now (MCP by default, legacy fallback);
# the PWA no longer exposes a selector for it.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/chats/$CID" \
  -H 'content-type: application/json' -d '{"thinking_level":"high"}'

# Handover — switch model/backend inside the same visible chat.
# Body keys: provider = claude|codex, model, model_bucket (Claude only), messages
# (visible rows). Starts the next provider turn as a fresh session seeded
# with those messages.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/handover" \
  -H 'content-type: application/json' \
  -d '{"provider":"claude","model":"sonnet","model_bucket":"anthropic","messages":[{"role":"user","content":"continue this task"},{"role":"assistant","content":"current state"}]}'

# Fork — create a new independent chat in the same project continuing from a completed turn.
# Body keys: messages (visible rows up to and including the target assistant answer),
# turn_index (zero-based count of user messages). Allocates a root-relative title number.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/fork" \
  -H 'content-type: application/json' \
  -d '{"turn_index":0,"messages":[{"role":"user","content":"Question"},{"role":"assistant","content":"Answer"}]}'

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

# Provider Sub-chats — list all sub-chats for a parent chat.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/provider-subchats"

# Create Provider Sub-chat.
# Body keys: parent_turn_index, owner (object with provider, model, label), participant (object), task_prompt (optional), user_authorized (optional).
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/provider-subchats" \
  -H 'content-type: application/json' \
  -d '{"parent_turn_index":0,"owner":{"provider":"claude","model":"opus","label":"Claude"},"participant":{"provider":"codex","model":"gpt-4","label":"Codex"},"task_prompt":"Analyze this issue"}'

# Read Sub-chat Events.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/provider-subchats/$SUBID/events"

# Send Message/Prompt to Sub-chat.
# Body keys: message, user_authorized (optional).
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/provider-subchats/$SUBID/messages" \
  -H 'content-type: application/json' \
  -d '{"message":"Next instruction"}'

# Close Sub-chat.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/provider-subchats/$SUBID/close"

# Cancel Sub-chat.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/provider-subchats/$SUBID/cancel"

# Extend Sub-chat limits.
# Body keys: user_authorized (required).
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/provider-subchats/$SUBID/extend" \
  -H 'content-type: application/json' \
  -d '{"user_authorized":true}'

# Resolve Permission Request in Sub-chat.
# Body keys: request_id, approved, reason (optional).
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/provider-subchats/$SUBID/permission-response" \
  -H 'content-type: application/json' \
  -d '{"request_id":"req-1","approved":true}'

# Resolve Structured Question in Sub-chat.
# Body keys: request_id, answers (dict).
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/provider-subchats/$SUBID/question-response" \
  -H 'content-type: application/json' \
  -d '{"request_id":"req-2","answers":{"choice":["option-a"]}}'
```

**Workspaces**

```bash
# List — returns {workspaces, active, provider_options, claude_ai_connectors}.
# claude_ai_connectors is the claude.ai connector MCP set the per-workspace
# toggle controls (for UI labels).
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/workspaces"

# Upsert — body keys: name, vault_root, default_provider, default_model,
# gws_profile, model_bucket, disallowed_tools (extra non-connector tools,
# CSV or list, null = defaults), claude_ai_mcps (true|false|null where null
# = per-workspace default: personal off, else on). The effective denylist is
# the union of the claude.ai connector set (when the toggle is off) and the
# extras. POST creates, PATCH /api/workspaces/{name} updates in place.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/workspaces" \
  -H 'content-type: application/json' \
  -d '{"name":"client-a","vault_root":"vaults/client-a","claude_ai_mcps":true,"disallowed_tools":"mcp__n8n_mcp"}'

# Flip just the toggle on an existing workspace.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/workspaces/personal" \
  -H 'content-type: application/json' \
  -d '{"claude_ai_mcps":true}'

# Delete.
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/workspaces/client-a"
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

# Create a loop: re-sends the prompt into one existing chat every N minutes.
# No model field — iterations run with the chat's own model/mode. autostart=true
# starts it with the server; start=true starts it right now. GET /api/loops
# enriches each entry with `running`, `context_label`, and `next_run`.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/loops" \
  -H 'content-type: application/json' \
  -d '{"prompt":"Check my PRs for review changes; reply \"no changes\" if nothing new.","web_chat_id":"chat-...","interval_minutes":10,"autostart":false,"start":true}'

# Start / stop a loop (runtime state; survives only via autostart across restarts).
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/loops/$LID" \
  -H 'content-type: application/json' -d '{"running":false}'

# Fire one iteration now (works while stopped). 409 if the chat is mid-turn.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/loop-run/$LID"

# Delete a loop (also stops it).
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/loops/$LID"

# Deploy: snapshot, pull, build, restart. Don't call from inside the live PWA session
# (CLAUDE.md "Never restart the ciao service yourself"); ask the operator to hit Deploy.
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

**Routine settings (Settings → Models tab)**

```bash
# Read internal-routine settings: title, insights, and critique model overrides
# (plus the effective models after defaults), transcription engine, and grouped
# model options (anthropic / ollama_cloud / ollama_local).
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/settings/routines"

# Update any subset. Persisted in .runtime/app_settings.json, applied to the
# live config immediately (no restart). Empty string clears an override back
# to the env default. transcription_engine ∈ {cloud, local}; "local" uses
# mlx-whisper on-device (free).
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/settings/routines" \
  -H 'content-type: application/json' \
  -d '{"title_model":"gemma4:12b-it-qat","critique_models":"anthropic/claude-sonnet-4.5","transcription_engine":"local"}'
```

**Workspace git sync**

Ciaobot never creates or switches local branches: it works on whatever branch the workspace
checkout is currently on. Handback commits pending work, pulls from origin (merge-based), and
pushes the branch: a clean pull is pushed directly (response: `{merged:true,
deploy_needed:false, pushed}`); a conflicting pull is left in the tree and opens an interactive
chat (`{merged:false, conflict:true, merge:{chat_id,...}}`) that resolves it, asking you
(push-notified) when ambiguous. After that chat lands the branch, resync merges
`origin/<branch>` back into the checkout. Non-git workspaces (or detached HEAD) get
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

When adding a new state-changing route (`POST/PATCH/DELETE /api/...`), add an entry here or add the path to `BROWSER_OR_INTERNAL_ROUTES` in `tests/test_pwa_api_docs.py` with a one-line reason. The doc-sync test enforces this.

**WebSocket events**

Global `/ws/events` payloads the PWA reacts to:

- `chat_streaming_started` / `chat_streaming_done` / `chat_result_ready`: lifecycle of the main chat turn.
- `chat_subagents_ready`: emitted when a background `Agent` (run_in_background) finishes or its count drops. Fields: `{chat_id, project_id, remaining}`.
- `chat_read`: another client/device marked the chat read.
- `chat_title`: auto-title finished.
- `chat_moved` / `chat_deleted`: project changes.
- `server_restarting`: restart drain began (`{message}`). The connect `snapshot` also carries `restarting: true` when drain is already in progress so late clients show the overlay without waiting for a turn rejection.

Per-chat `/ws/chat/{chat_id}` events include text/thinking deltas, `tool_use` (with optional `file_touch` and provider-native `request_id`), `permission_request`, `result`, `user_echo`, `queued`, `queue_state`, `steered`, `status`, `error`, and `server_restarting` (sent instead of `error` when a new turn is rejected because restart drain is in progress). Client messages include normal `message`, `stop`, `permission_response`, and `question_response`; Codex structured questions use `question_response {request_id, answers: {question_id: string[]}}` so the answer resolves inside the still-running app-server turn.

**Queue management**: while the assistant is streaming, the client can queue follow-up messages (mode `queue`). Each queued item gets an `id` and is flushed as its own user turn once the prior turn finishes. The client can also send `queue_reorder {entry_id, before_id}` (move `entry_id` before `before_id`, or to the end when `before_id` is null), `queue_edit {entry_id, text, images?}`, and `queue_remove {entry_id}`. The server confirms with `queue_state {queue: [{id, text, images?}]}` so connected clients stay in sync.

**Auto tier-fallback status events**: when the primary model returns a capability error (image input, tool use, context length, etc.), the server emits a `status` event with a "retrying on &lt;model&gt;" message, then runs the retry and emits the normal `result` for the new model. The terminal `result.effective_model` is the retry target's id. Rate limits, auth errors, content filters, and 5xx do NOT trigger this path; only Claude, Ollama, and OpenRouter backends participate.

**Message timings**

Each user turn carries timing metadata, computed in `ciao/web/project_chats.py` (provider-agnostic) and persisted under `ChatInfo.user_turn_timings` as `{ "<turn_index>": {sent_at, completed_at, duration_ms} }`.

- `GET /api/chats/{chat_id}/messages`: user entries include `sent_at`; the last assistant entry per turn includes `sent_at` (= `completed_at`) and `duration_ms`. Overlay is applied to both Claude SDK and Codex app-server history. Pre-feature chats with no recorded timings get no extra fields.
- WS `/ws/chat/{chat_id}` `user_echo` event: adds optional `sent_at`.
- WS `/ws/chat/{chat_id}` `result` event: adds optional `sent_at`, `completed_at`, `duration_ms`.

**File-touch cards**

Write/Edit/MultiEdit/NotebookEdit tool calls flow through both transports tagged with `file_touch`. The PWA renders each card chronologically inside expanded `Activity`, plus a deduplicated `Outputs` chip below the final answer. If a turn is interrupted before producing a final answer, the chip remains inside `Activity` so the touched file is not hidden.

- WS `/ws/chat/{chat_id}` `tool_use` event: adds optional `file_touch: {file_path, action}` when the tool mutates a file on disk. Detection lives in `extract_file_touch` (`ciao/web/chat_broker.py`); `action` is `written | edited`.
- `GET /api/chats/{chat_id}/messages` and `GET /api/chats/{chat_id}/subagents`: file-mutating tool calls become standalone `{role: "system", tool_name: "_filecard", file_path, action, tool, content: file_path}` entries instead of folding into `_activity`. Both provider readers honour this.
- Card click opens `/api/workspace-file` (text/code) or `/api/workspace-image` (images by extension). The classification is advisory only. The viewer endpoints have no workspace sandbox: they serve any allowlisted-extension file on disk (relative paths anchor to `workspace_root`). The extension allowlist (no `.env`) and size caps are the only guards.

**File snapshots, history, diff, edit-in-place**

Every file-touch tool call also triggers a debounced (1.5s) content snapshot via `SnapshotStore` in `ciao/web/file_snapshots.py`. Snapshots are append-only files under `.runtime/snapshots/<chat_id>/<urlencoded_path>/NNNN.snap` with a sibling `meta.json`. Dedup hashes consecutive captures so re-firing the hook on identical content does not pollute history.

- `GET /api/file-history?chat_id=&file_path=` returns `{snapshots: [{seq, ts, action, tool, size, truncated?}]}`. Most recent last.
- `GET /api/file-content?chat_id=&file_path=&seq=` returns `{content: str, meta}`. 413 if the snapshot was over `MAX_SNAPSHOT_BYTES` at capture time, 415 if the snapshot was binary.
- `POST /api/file-restore` body `{chat_id, file_path, seq}` writes the snapshot back to its recorded host path (absolute paths are intentional) and captures a new snapshot with `action="restored"` so the timeline stays linear. Returns `{ok, restored_seq, new_seq}`.
- `POST /api/workspace-file` body `{chat_id?, path, content}` writes user-edited content back (FileViewerModal edit mode). It has the same intentional unrestricted host-path behavior and extension/size guards as the GET. When `chat_id` is supplied, the write is captured as a snapshot with `tool="PWAEdit"` so PWA edits show up in the history alongside agent edits.

## State

- Project and chat state: `.runtime/web_projects.json`. `.runtime/server.lock` prevents two backend processes from owning this registry, and `.runtime/web_projects.audit.jsonl` records append-only mutation IDs/revisions for repair without storing chat content. On-disk shape mirrors the `ProjectInfo` and `ChatInfo` dataclasses in `ciao/web/project_chats.py`; `to_dict()` on each defines the JSON fields. `ChatInfo.user_turn_timings` holds per-turn `{sent_at, completed_at, duration_ms}` keyed by user-turn index (as str); the matching `_turn_perf_started` map on `ProjectChatManager` is in-memory only.
- `ChatInfo.pending_question` (string, in `to_dict()` so it rides every chat list / chat object): raw AskUserQuestion JSON (`{"questions": [...]}`) set when the model paused the chat on a question. When the headless CLI fires AskUserQuestion the server interrupts the live turn so the CLI cannot auto-answer it, persists this field, and clears it on the next user send. The PWA reads it on chat open to rebuild the interactive question picker after a reload. Empty string when no question is pending.
- Schedule state: `.runtime/schedules.json`. Shape and field semantics in `ciao/schedules.py` (`ScheduleEntry`); the `ciao-automations` skill packs the create/edit recipes.
- Loop state: `.runtime/loops.json` (`ciao/loops.py`, `LoopEntry`). Running/stopped is runtime-only state in the `LoopManager`: `autostart` decides what runs after boot, so prefer the API over direct file writes for loops.
- Uploaded media: under the configured runtime/media directory

## Naming

See `README.md` "Project naming convention" for folder layout, frontmatter, and the auto-created `General` project.
