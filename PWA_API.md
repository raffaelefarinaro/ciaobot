# PWA API

Local HTTP API for the Ciaobot PWA. Default base URL is `http://localhost:$PWA_PORT` with `PWA_PORT=8443`.

The route source of truth is `ciao/web/app.py`. This file is kept in sync by `tests/test_pwa_api_docs.py`.

## Auth And Browser Security

- Password protection is on by default. `PWA_AUTH_TOKEN` is the dashboard password: the first-run wizard asks for it, `POST /api/auth/settings` changes it, and only `PWA_AUTH_REQUIRED=false` in the workspace `.env` turns protection off.
- `POST /api/auth` accepts `{"token": "<PWA_AUTH_TOKEN>"}` and returns an HttpOnly `ciao_session` cookie.
- `GET /?setup=<token>` is the local first-launch shortcut path. It is accepted only on `localhost`, `127.0.0.1`, or `::1`; when the token matches `.runtime/setup-token`, the server sets the same signed `ciao_session` cookie, deletes the token file, and redirects to `/`.
- Production cookies are `Secure`, `SameSite=Lax`, and host-only (scoped to the exact host that served them).
- `POST /api/auth/logout` clears the same host-only cookie.
- All `/api/*` routes except `POST /api/auth`, `GET /api/startup-status`, `GET /api/active-chats`, `GET /api/setup-status`, `POST /api/setup/finish`, `GET /api/setup/list-dirs`, and `POST /api/setup/mkdir` require the signed session cookie. (`GET /api/setup/inspect-folder` is not middleware-exempt, but it only answers in bootstrap mode, where protection is off anyway.) All `/ws/*` routes require the signed session cookie.
- `POST /api/setup/finish` is only accepted in bootstrap mode from localhost with a matching browser origin/referer (off-localhost requests get a 403 pointing at `http://localhost:<port>`). Body: `workspace` (required — the root folder holding the vault plus app data), `vault_root` (optional, default `<workspace>/memory-vault`; absolute or `~` paths are honored for an existing notes folder elsewhere), `password` (required — the dashboard password, at least 4 characters; setup always enables protection), plus optional `vault_mode`, `workspace_name`, `push_contact`, `port`, `python`, `launch_agents_dir`, `app_dir`, and `restart`. It writes the real workspace config, ensures workspace and vault are (in) git repos, creates local launch artifacts, and asks the supervisor to restart into the configured workspace. When the chosen folder already contains nested workspace directories (`memory-vault/<name>/` with a `MEMORY.md` inside), those are adopted as the workspace registry and `workspace_name` is ignored.
- `GET /api/setup/list-dirs`, `POST /api/setup/mkdir`, and `GET /api/setup/inspect-folder` back the setup wizard. They are only accepted in bootstrap mode from localhost with a matching browser origin/referer (404 outside bootstrap mode, 403 off-localhost). The folder picker (`list-dirs`, `mkdir`) lists directories only and never reads file contents. `inspect-folder?path=<dir>` returns `{mode: "scratch"|"existing", vault_root, existing_workspaces, has_env}` so the wizard can hide the "First Workspace" text field when nested workspaces are already present.
- State-changing `/api/*` requests with an `Origin` or `Referer` header must match the request host. Missing headers are accepted for non-browser clients.
- HTTP responses include baseline security headers, including CSP, `X-Content-Type-Options`, `Referrer-Policy`, and frame denial.
- The agent-facing `/mcp/` mount uses a separate scoped bearer capability issued to Ciaobot-managed provider processes; it does not accept the browser session cookie. `GET /api/mcp/status` exposes only readiness and catalog metadata, never a token.

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
| POST | `/api/desktop-drop` | Consume a native app's single-use Finder-drop grant (local node only) |
| GET | `/api/chats` | List all chats |
| GET | `/api/menubar-chats` | Compact chat list for the `Ciaobot.app` tray |
| GET | `/api/menubar-notifications` | Notification feed for the `Ciaobot.app` tray (`?after=<epoch>`, inclusive; includes read-clear controls and is proxied to the host in client mode) |
| POST | `/api/chats/read-all` | Mark all chats read |
| PATCH, DELETE | `/api/chats/{chat_id}` | Update or delete chat |
| POST | `/api/chats/{chat_id}/new` | Start a new provider session |
| POST | `/api/chats/{chat_id}/handover` | Continue chat on a fresh provider session |
| POST | `/api/chats/{chat_id}/fork` | Fork chat continuing from a completed turn |
| POST | `/api/chats/{chat_id}/archive` | Archive chat |
| POST | `/api/chats/{chat_id}/continue` | Create a new active chat continuing from this archived one |
| POST | `/api/chats/{chat_id}/read` | Mark chat read |
| POST | `/api/chats/{chat_id}/retry` | Set, stop, or run deferred chat retry |
| POST | `/api/chats/{chat_id}/stop` | Stop an in-flight turn; HTTP fallback for the websocket `stop` message, for when that chat's socket is disconnected or mid-reconnect |
| POST | `/api/chats/{chat_id}/retry-insights` | Re-run session-insights extraction for an archived chat (text-mode, on demand) |
| POST | `/api/chats/{chat_id}/prompt` | Send a prompt to start a background turn in the chat. Returns 409 `{error:"chat is archived", archived:true}` if the chat was archived; start a new chat (or `continue`) instead of retrying |
| GET | `/api/open-chat/{chat_id}` | Focus an existing chat in the PWA and report whether a live event subscriber received the navigation |
| GET | `/api/chats/{chat_id}/messages` | Load persisted chat messages |
| GET | `/api/chats/{chat_id}/messages/part` | Fetch one full history row by absolute index (lazy expansion) |
| GET | `/api/native/sessions` | List locally-running Claude Code CLI sessions for a workspace (handover warning) |
| POST | `/api/chats/{chat_id}/reentry-summary` | Return an ephemeral Apple Intelligence orientation summary for a reopened chat |
| GET | `/api/chats/{chat_id}/subagents` | Load subagent transcripts |
| POST | `/api/chats/{chat_id}/voice` | Upload voice for transcription |
| POST | `/api/chats/{chat_id}/speak` | Synthesize speech for a message; returns audio bytes |
| POST | `/api/chats/{chat_id}/images` | Upload chat images |
| GET | `/api/images/{ref}` | Read uploaded image blob |
| GET | `/api/workspace-file` | Read allowed text file |
| POST | `/api/workspace-file` | Write user-edited text file (allowlist + snapshot) |
| GET | `/api/workspace-html` | Render an `.html` artifact as `text/html` in a sandboxing CSP (panel Preview) |
| GET | `/api/workspace-image` | Read allowed image file |
| GET | `/api/workspace-binary` | Read allowed binary file |
| GET | `/api/libreoffice-status` | Whether LibreOffice (`soffice`) is available to render `.pptx` previews |
| POST | `/api/libreoffice-install` | Install LibreOffice via Homebrew Cask (macOS); no restart needed |
| POST | `/api/workspace-open` | Open a workspace file with the OS default app on the machine running Ciao |
| GET | `/api/file-history` | List snapshots for a `(chat_id, file_path)` |
| GET | `/api/file-content` | Read one snapshot's content |
| GET | `/api/vault-markdown-paths` | List workspace-relative markdown paths (file viewer resolves Obsidian wikilinks) |
| GET | `/api/vault/backlinks` | List notes whose wikilinks resolve to a given markdown path |
| GET | `/api/vault/graph` | Vault-wide note graph (frontmatter `related:` + `[[wikilinks]]`) for the Memory Map page; optional `?workspace=` scopes to one logical workspace |
| DELETE | `/api/vault/note` | Permanently delete one vault note (`?path=`, the `Entry.path` string form); strips dangling `related:`/`relatedTo:` and `[[wikilink]]` references from every note that linked to it first |
| POST | `/api/file-restore` | Restore a snapshot to disk |
| GET, POST | `/api/schedules` | List or create schedules |
| POST | `/api/schedule-run/{schedule_id}` | Run schedule now |
| PATCH, DELETE | `/api/schedules/{schedule_id}` | Update or delete schedule |
| GET, POST | `/api/loops` | List or create in-chat loops (re-dispatch a prompt into a fixed chat every N minutes) |
| POST | `/api/loop-run/{loop_id}` | Fire one loop iteration now (409 when the chat has a turn in flight) |
| PATCH, DELETE | `/api/loops/{loop_id}` | Update, start/stop (`{"running": bool}`), or delete a loop |
| GET | `/api/automation` | Background-job status (Settings → Automations): per job its trigger, last run, duration, model, errors, and bulk `sub_jobs`. Omits retired jobs and schedule-only jobs whose schedule is not installed |
| POST | `/api/automation/backfill-insights` | Run Session insights over every archived chat missing them. Optional `{"model": "<model-id>"}` runs this pass with a different model without changing the stored setting |
| GET | `/api/debug/issues` | Runtime issue report (server error log tail + failed job runs) for the dev-mode "Fix issues in chat" flow; 404 unless `CIAO_DEV_MODE` is set |
| GET | `/api/commands` | List slash commands |
| GET | `/api/agent-assets` | List subagents, slash commands, and workspace health for Settings |
| GET | `/api/agent-assets/audit` | Full AI OS audit report; `status` is `healthy`, `needs_attention`, or `error` |
| GET | `/api/workspace-health` | Scan workspace/vault/discovery-file health |
| POST | `/api/workspace-health/fix` | Apply the automatic remedies (create missing scaffold files, re-link skills); returns the fresh report |
| POST | `/api/agent-assets/subagents` | Create a workspace-owned subagent and vault mirror |
| PATCH, DELETE | `/api/agent-assets/subagents/{name}` | Update or delete a custom workspace-owned subagent |
| POST | `/api/agent-assets/commands` | Create a workspace-owned slash command and vault mirror |
| PATCH, DELETE | `/api/agent-assets/commands/{name}` | Update or delete a custom workspace-owned slash command |
| GET | `/api/rate-limits` | Read Claude rate-limit snapshots |
| GET | `/api/housekeeping` | List the home-screen operator actions (detector pass; each carries `run_label`, `chat_label`, `chat_prompt`) |
| POST | `/api/housekeeping/{action_id}/run` | Perform one action's mechanical work, re-run detection, and return the fresh action list; unknown id is 404 |
| GET | `/api/models` | List configured models, plus `providers[]` (id, labels, capabilities) from the runtime-provider registry. `?refresh=1` bypasses the provider catalog caches |
| GET, PATCH | `/api/status` | Read or update status |
| GET | `/api/mcp/status` | Embedded Ciaobot MCP readiness, tool catalog, project MCP servers (env-key status + observed tools), and active-session counts (no credentials) |
| GET | `/api/mcp/usage` | Embedded Ciaobot MCP per-tool call/error counters (no credentials) |
| POST | `/api/mcp/env-keys` | Save project-MCP env secrets into the workspace `.env` (optionally bind new keys into a server via `server`); values never returned |
| POST | `/api/mcp/servers` | Create a project MCP server in `.mcp.json` |
| PATCH | `/api/mcp/servers/{name}` | Update a project MCP server connection (and optional env keys) |
| DELETE | `/api/mcp/servers/{name}` | Remove a project MCP server from `.mcp.json` |
| GET | `/api/mcp/servers/{name}/tools` | Lazy tool discovery for one project MCP server (HTTP `tools/list` probe, or observed telemetry for stdio) |
| GET | `/api/startup-status` | Read startup phase progress |
| GET | `/api/active-chats` | List chat IDs with in-flight work (streaming or background subagents); drives the macOS menu bar spinner |
| GET | `/api/setup-status` | Read first-run setup checks and provider readiness |
| GET | `/api/package/status` | Read installed package version and best-effort latest GitHub release version |
| GET | `/api/package/changelog` | List commits between the installed and latest release for the update prompt |
| POST | `/api/package/update` | Return app-owned update guidance; production updates replace the signed Ciaobot.app bundle and restart |
| GET | `/api/device/package-status` | Same as `/api/package/status`, but never proxied: in client mode this reports *this* machine's install while `/api/package/status` reports the host's |
| GET | `/api/device/changelog` | Commits between this machine's installed version and the latest release (never proxied) |
| POST | `/api/device/update` | Return app-owned update guidance for *this* machine, not the host it mirrors (never proxied) |
| POST | `/api/setup/finish` | Finish first-run setup from bootstrap mode |
| GET | `/api/setup/list-dirs` | List local subdirectories for the setup wizard folder picker (bootstrap mode, localhost only) |
| GET | `/api/setup/inspect-folder` | Probe a candidate workspace folder for vault mode and any nested workspaces (bootstrap mode, localhost only) |
| POST | `/api/setup/mkdir` | Create a folder from the setup wizard folder picker (bootstrap mode, localhost only) |
| GET | `/api/stats` | Read CLI stats |
| GET | `/api/workspaces` | List configured logical workspaces |
| POST | `/api/workspaces/{name}` | Add or update a logical workspace config |
| DELETE | `/api/workspaces/{name}` | Delete a logical workspace config |
| GET, PATCH | `/api/settings/providers` | Read or update provider/service key status and the GitHub-skill refresh setting; credentials are redacted |
| POST | `/api/settings/providers/{provider}/{action}` | Connect, verify, or log out through the Claude Code or Codex CLI |
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
| POST | `/api/handover/merge` | Open an interactive chat that resolves sync conflicts on a branch |
| GET | `/api/node/addresses` | URLs this engine is reachable at (localhost, Bonjour `.local`, each LAN/VPN IPv4), each flagged `loopback` so the PWA can mark the ones a phone cannot use. Session-protected, unlike the loopback-public tray endpoints, because it enumerates LAN interfaces |
| GET | `/api/node/status` | Read multi-device node failover status and role |
| GET | `/api/node/connected-clients` | Live remote WebSocket clients connected to this host (excludes loopback) |
| POST | `/api/node/connect` | Connect this node as a client tunnel to a remote host |
| POST | `/api/node/demote` | Demote active node to standby |
| POST | `/api/node/handover` | Handover active role to another node |
| POST | `/api/node/peers` | Register or update node peer links |
| POST | `/api/admin/snapshot` | Git add, commit, and push snapshot |
| POST | `/api/admin/deploy` | Reinstall deps, rebuild frontend (plus the desktop app in dev mode), and restart with latest code |
| GET | `/api/admin/status` | Read admin/deploy status |
| GET | `/api/admin/skills` | List skills labelled as custom or GitHub/package |
| POST | `/api/admin/skills/add` | Add an upstream skill from GitHub and synchronize it |
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
# Inspect subagents, commands, and workspace health.
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

**Housekeeping (operator-action strip)**

```bash
# List the home-screen operator actions. Each carries run_label, chat_label,
# and chat_prompt; the browser renders the buttons and seeds the chat itself.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/housekeeping"

# Run one action's mechanical work. The response re-runs detection and returns
# the fresh action list so the client cannot render a stale strip.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/housekeeping/vault-vocabulary/run"
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

Project file uploads are limited to 50 MB per file. File-list responses use
workspace-relative viewer paths when the vault is nested under the workspace
and absolute viewer paths when `CIAO_VAULT_ROOT` points elsewhere. Successful
upload entries also include `absolute_path`, which the chat composer uses after
a client uploads a dropped file to the host's active project folder. Saved-page
`.mht`/`.mhtml` files are accepted as binary project attachments and served as
downloads rather than executable inline content.

**Chats**

```bash
# Create — title/model/mode/provider all optional.
# provider is any id from the registry (`claude`, `codex`, `opencode`); see
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
# control_surface (legacy|mcp|auto|'') is still accepted here as an escape
# hatch, but it is engine-controlled now (MCP by default, legacy fallback);
# the PWA no longer exposes a selector for it.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/chats/$CID" \
  -H 'content-type: application/json' -d '{"thinking_level":"high"}'

# Handover — switch model/backend inside the same visible chat.
# Body keys: provider = claude|codex|opencode, model, messages
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

# Archive — finalises the chat and writes a Markdown transcript. If this chat
# supervises delegate subchats, their active chats are archived too: a subchat
# that is mid-turn is stopped first, so its unfinished output is discarded
# rather than written past the archive. Returns
# {ok, archived_to, archived_chat_ids, stopped_chat_ids, failed_chat_ids,
# subchats}; one `chat_archived` event is emitted for each chat.
# The cascade can partly fail, and `ok` only covers the requested chat, so
# treat `archived_chat_ids` as the authority on what is actually archived —
# anything absent from it is still live. `stopped_chat_ids` are the subchats
# whose running turn was cut short (tell the user); `failed_chat_ids` are the
# ones left active and needing a direct archive.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/archive"

# Mark read — returns {ok, last_read_at}.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/$CID/read"

# Mark all read.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/chats/read-all"

# Read mutations also cancel the delayed push and emit a cross-device clear
# control. Connected PWAs close the matching service-worker notification tag;
# the macOS tray removes delivered Ciaobot banners for the chat.

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
  -d '{"parent_turn_index":0,"owner":{"provider":"claude","model":"opus","label":"Claude"},"participant":{"provider":"codex","model":"gpt-4","label":"Codex"},"task_prompt":"Analyze this issue"}'

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
# List — returns {workspaces, active, provider_options}.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/workspaces"

# Upsert — body keys: name, default_provider, default_model,
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
# Steps run against CIAO_APP_REPO when set, else the directory holding the running
# ciao package; a non-checkout returns 400 with a "locate checkout" step. With
# CIAO_DEV_MODE and changed desktop/ sources it also rebuilds the Tauri app, then
# quits and relaunches it just before the engine restart.
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
# Read internal-routine settings: insights and critique model overrides, the
# per-provider default model / thinking / routine-model maps, and the effective
# models after defaults.
#
# insights_model_effective is the PRIMARY workspace's answer only. With no
# override the insights routine resolves from the chat's own workspace, so
# insights_model_by_workspace carries the full {workspace: model} map. The map
# is empty when an override is set, because then that one model applies
# everywhere.
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/settings/routines"

# Update any subset. Persisted in .runtime/app_settings.json, applied to the
# live config immediately (no restart). Empty string clears an override back
# to the env default. "apple" routes a routine to the on-device Foundation
# Model (insights_model). Per-provider defaults use the nested maps:
# provider_default_models, provider_default_thinking, provider_insights_models.
curl -sS -b /tmp/ciao.jar -X PATCH "http://localhost:${PWA_PORT:-8443}/api/settings/routines" \
  -H 'content-type: application/json' \
  -d '{"insights_model":"gemma4:12b-it-qat","critique_models":"anthropic/claude-sonnet-4.5","provider_default_models":{"codex":"gpt-5.6-terra"}}'
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
curl -sS -b /tmp/ciao.jar -X DELETE "http://localhost:${PWA_PORT:-8443}/api/mcp/servers/linear"

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

**Proposal queue**

Routes: `GET /api/proposals`, `POST /api/proposals/{id}/{action}` (action is
`accept` or `dismiss`), `POST /api/proposals/batch`,
`POST /api/proposals/dismiss-older-than`.

`accept` PERFORMS the promotion for a `memory`/`profile` row: the entry is written
into that workspace's bounded region (resolved through `agent_root`, so the right
guide in either layout), and only then is the bullet dropped. Write-then-dismiss,
never the reverse — a failed write returns **409** with the bullet still queued,
so an over-cap region cannot silently swallow the fact. A `rehome` row is not
moved here: relocating a note and rewriting every reference to it is
`vault_rehome`'s job, reversible through its own receipt, and doing half of it
from a queue row would leave links pointing at a path that moved. `dismiss` writes
nothing. Batch accept applies the same rule per row and reports `promoted` and
`dismissed` for each, keeping the bullets it could not write.

```bash
# List every queued proposal across all workspaces, plus skill-proposal files.
# Each row: {id, kind, text, source, workspace, path, line}. `id` is a stable,
# content-derived hash (survives other rows being dismissed). Rehome rows carry
# `rehome: {destination, candidates[], justified, reason}` so a UI never
# pre-accepts a destination no tag backs; region rows carry `region` and
# `leak_warning` (true when accepting would write a foreign workspace's fact
# into the primary workspace's injected region).
curl -sS -b /tmp/ciao.jar "http://localhost:${PWA_PORT:-8443}/api/proposals"

# Accept one row. Dispatches through the kind's own accept descriptor: memory/
# profile/user are region edits (returns {action: edit_region, region,
# leak_warning}), rehome is a file move (returns {action: move_file,
# destination, justified}). The row is dismissed from the queue; promotion is
# a separate explicit step, matching the MCP resolve path.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/proposals/$ID/accept"

# Dismiss one row from the queue. No region/file is touched.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/proposals/$ID/dismiss"

# Accept or dismiss a set atomically. Body: {"action":"accept|dismiss","ids":[...]}.
# Every id must resolve or the whole batch is rejected (404) with no file change.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/proposals/batch" \
  -H 'content-type: application/json' \
  -d '{"action":"accept","ids":["<id1>","<id2>"]}'

# Dismiss every row dated strictly before a cutoff (YYYY-MM-DD), atomically.
# A July proposal about a forgotten chat is not worth promoting.
curl -sS -b /tmp/ciao.jar -X POST "http://localhost:${PWA_PORT:-8443}/api/proposals/dismiss-older-than?date=2026-08-01"
```


When adding a new state-changing route (`POST/PATCH/DELETE /api/...`), add an entry here or add the path to `BROWSER_OR_INTERNAL_ROUTES` in `tests/test_pwa_api_docs.py` with a one-line reason. The doc-sync test enforces this.

**WebSocket events**

Global `/ws/events` payloads the PWA reacts to:

- `chat_streaming_started` / `chat_streaming_done` / `chat_result_ready`: lifecycle of the main chat turn.
- `chat_subagents_ready`: emitted when a background `Agent` (run_in_background) finishes or its count drops. Fields: `{chat_id, project_id, remaining}`.
- `chat_delegates_reported`: emitted on the *supervisor* chat once finished delegates (chats carrying `spawned_from_chat_id`) have been reported back to it. Fields: `{chat_id, project_id, count, delivery}`, where `delivery` is `"queued"` (supervisor was mid-turn, so the wake was appended as a follow-up) or `"started"` (supervisor was idle, so a new turn began). Completions inside a 5s window coalesce into one event.
- `chat_read`: another client/device marked the chat read.
- `chat_title`: auto-title finished.
- `chat_created`: a new chat was created (fresh or fork). Fields: `{chat: ChatInfo}`. The acting tab already pushes optimistically; this event is what makes other tabs/devices, or the acting tab after a racing `syncLatest` clobber, render the chat without waiting for the 15s poll. Without it a fork (which starts no streaming turn, so no `chat_result_ready` refetch) stayed invisible until a manual reload.
- `chat_moved` / `chat_archived` / `chat_deleted`: project changes.
- `chat_postprocess`: the post-archive pipeline reporting itself. Archiving a chat dispatches one task that extracts session insights, folds the project doc, writes a trajectory and files memory proposals (`ciao/insights.py:extract_and_append`); this event fires when the pipeline starts, as each step finishes, and when it settles. Fields: `{chat_id, project_id, postprocess}`, where `postprocess` is `{state: "running"|"done", step, expected: [job_id], steps: {job_id: {status, extra}}, started_at, updated_at, interrupted?}`. The same object is persisted on the chat and returned as `ChatInfo.postprocess`, so an archived chat can still report what was learned from it after a reload — the PWA renders it as a muted activity signal while `state` is `running` and as a settled one-line summary afterwards. `interrupted` marks a pipeline a restart killed mid-flight. The connect `snapshot` carries `postprocessing: [chat_id]` for pipelines already in flight, so a client that joins between the start and finish events still shows them.
- `loops_changed`: a loop was created, edited, started, stopped, or deleted (REST route, Schedules page, or the `loop_*` MCP tools mid-turn). No payload; the client refetches `GET /api/loops`, which is where the computed `running` / `next_run` fields are assembled. Without it a loop created by the model stayed invisible (no chat banner, no sidebar `↻` marker) until a manual reload.
- `server_restarting`: restart drain began (`{message}`). The connect `snapshot` also carries `restarting: true` when drain is already in progress so late clients show the overlay without waiting for a turn rejection.

Per-chat `/ws/chat/{chat_id}` events include text/thinking deltas, `tool_use` (with optional `file_touch` and provider-native `request_id`), `permission_request`, `model_capability_question`, `tool_denied`, `result`, `user_echo`, `queued`, `queue_state`, `steered`, `status`, `error`, `host_unreachable`, and `server_restarting`. A `message` frame that reaches the server always starts its turn: the stream is registered before any socket write, so a client that disconnects right after sending (mobile/webview suspension) still gets the turn, and the reconnecting socket replays the buffered `user_echo` from the broker. The client-mode proxy emits `host_unreachable` when it cannot open the remote host socket; the PWA treats it as one ephemeral reconnecting state with a force-become-host action, never as a chat error. `server_restarting` is likewise sent instead of `error` when a new turn is rejected because restart drain is in progress. Client messages include normal `message`, `stop`, `permission_response`, `question_response`, and `capability_response`; Codex structured questions use `question_response {request_id, answers: {question_id: string[]}}` so the answer resolves inside the still-running app-server turn.

**Image-capability pre-flight**: when a turn carries images and the selected model cannot see them, the server pauses before dispatch and emits `model_capability_question {request_id, missing: "image_input", current_model, candidates: [{id, label, supports_vision?, disabled?}], timeout_s: 30}`. `candidates` leads with the current model (disabled) followed by up to 3 same-backend vision models. The client answers with `capability_response {request_id, action, model_id?}`: `switch` re-dispatches the turn on `model_id` (the chat model is persisted and a `model_changed` event is emitted), `picker` closes the question so the PWA can open the model selector and the user re-sends, and `cancel` (or the 30s timeout) closes the turn with a `status` bubble telling the user the images were not sent. The question is skipped entirely for text-only turns and for unattended (loop/schedule) turns, which close with the bubble instead of waiting.

**Queue management**: while the assistant is streaming, the client can queue follow-up messages (mode `queue`). Each queued item gets an `id` and is flushed as its own user turn once the prior turn finishes. When that turn starts, its `user_echo` includes `entry_id` so the client removes only the flushed item and keeps later queue entries visible. The client can also send `queue_reorder {entry_id, before_id}` (move `entry_id` before `before_id`, or to the end when `before_id` is null), `queue_edit {entry_id, text, images?}`, and `queue_remove {entry_id}`. The server confirms with `queue_state {queue: [{id, text, images?}]}` so connected clients stay in sync.

**Auto tier-fallback status events**: when the primary model returns a capability error (image input, tool use, context length, etc.), the server emits a `status` event with a "retrying on &lt;model&gt;" message, then runs the retry and emits the normal `result` for the new model. The terminal `result.effective_model` is the retry target's id. Rate limits, auth errors, content filters, and 5xx do NOT trigger this path; only Claude chats pinned to a bare tier alias participate.

**Message timings**

Each user turn carries timing metadata, computed in `ciao/web/project_chats.py` (provider-agnostic) and persisted under `ChatInfo.user_turn_timings` as `{ "<turn_index>": {sent_at, completed_at, duration_ms} }`.

- `GET /api/chats/{chat_id}/messages`: user entries include `sent_at`; the last assistant entry per turn includes `sent_at` (= `completed_at`) and `duration_ms`. Overlay is applied to both Claude SDK and Codex app-server history. Pre-feature chats with no recorded timings get no extra fields. With an `offset` and/or `limit` query param the endpoint returns a `{items, total, offset, limit, hasMore, nextOffset}` envelope instead of a flat array; `offset=0` is the newest tail. Envelope rows carry `i` (absolute index) and long `_thinking` rows are truncated head+tail with `lazy: true`; the full row is fetchable from `GET /api/chats/{chat_id}/messages/part?i=<index>`. Without params the legacy flat array is returned unchanged.
- WS `/ws/chat/{chat_id}` `user_echo` event: adds optional `sent_at`.
- WS `/ws/chat/{chat_id}` `result` event: adds optional `sent_at`, `completed_at`, `duration_ms`.

**Unattended turns (loop / schedule ticks)**

A loop or schedule fires its prompt as an ordinary user turn, so without a marker it is indistinguishable from something the user typed — the model read its own loop prompt as a live message and replied "even though you're actively messaging me".

- WS `user_echo` gains `unattended: true` on such turns; `GET /api/chats/{chat_id}/messages` sets the same flag on the user entry, read back from `ChatInfo.user_turn_unattended` (keyed by turn index). The SDK session file records no sender, so the flag has to come from our own per-turn record. Absent = interactive, so old chats are unaffected.
- The PWA renders a `↻ auto` marker in the bubble footer.
- The model gets a matching line inside the injected-context block (stripped from rendered history) telling it the turn is unattended, that nobody is watching, and not to ask questions or wait for approvals.
- Permission mode for these turns is `bypass`: an escalation would be auto-denied ("Scheduled runs cannot wait for interactive approval"), which silently broke any automation needing network access or a first-time write. Deny rules still apply.

**Loop / schedule banner**

The PWA shows a banner at the top of a chat when an automation is bound to it. Loops are 1:1 with one fixed chat, so the banner is driven by filtering `GET /api/loops` where `loop.web_chat_id === chat.chat_id`. Schedules are 1:many (a `web_project_id` schedule spawns a new chat each run), so the chat carries a durable backlink instead: `ChatInfo.schedule_id` and `ChatInfo.schedule_title`, stamped in `prepare_schedule_chat` for both the project-schedule (new chat) and fixed-chat (`web_chat_id`) branches and included in every chat object via `to_dict()`. The banner filters `GET /api/schedules` where `s.schedule_id === chat.schedule_id` OR `s.web_chat_id === chat.chat_id` (the second arm covers fixed-chat schedules). Each row links to `/schedules/<id>` (Manage) and offers Run now (`POST /api/schedule-run/{schedule_id}`). Existing chats predating the field stay empty-string and render no banner.

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
- Client: `HtmlArtifactViewer.vue` (shared by `PinnedFilePanel` and `FileViewerModal`) frames it with `sandbox="allow-scripts"`, matching the CSP directive — the effective sandbox is the intersection of attribute and header. Source for Code view is a separate lazy fetch, because `error` blanks the viewer body and an oversized-source failure must not hide a page that renders. Artifacts are not commentable (comment anchors need markdown highlights or text lines).
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
- `ChatInfo.pending_question` (string, in `to_dict()` so it rides every chat list / chat object): raw AskUserQuestion JSON (`{"questions": [...]}`) set when the model paused the chat on a question. When the headless CLI fires AskUserQuestion the server interrupts the live turn so the CLI cannot auto-answer it, persists this field, and clears it on the next user send. The PWA reads it on chat open to rebuild the interactive question picker after a reload. Empty string when no question is pending.
- `ChatInfo.schedule_id` / `ChatInfo.schedule_title` (strings, in `to_dict()`): backlink to the schedule that created or drives the chat. Stamped in `prepare_schedule_chat` for both schedule branches (project-schedule new chat, fixed-chat reuse). Drives the schedule banner in ChatPanel. Empty for interactive chats and for chats created before the field existed. See "Loop / schedule banner" above.
- Schedule state: `.runtime/schedules.json`. Shape and field semantics in `ciao/schedules.py` (`ScheduleEntry`); the `schedule_create`/`schedule_update` MCP tools carry the field semantics in their own docstrings.
- Loop state: `.runtime/loops.json` (`ciao/loops.py`, `LoopEntry`). Running/stopped is runtime-only state in the `LoopManager`: `autostart` decides what runs after boot, so prefer the API over direct file writes for loops.
- Uploaded media: under the configured runtime/media directory

## Naming

See `README.md` "Project naming convention" for folder layout, frontmatter, and the auto-created `General` project.
