# Integrations Setup

One-time setup for external tools and CLI dependencies.

SDK-level wiring notes (fallback_model, hooks, setting_sources) live in the module docstring of `ciao/providers/claude.py`.

## CLI Tools

## opencode

```bash
npm install -g opencode-ai@latest   # or: brew install sst/tap/opencode
ciao auth opencode                  # opens `opencode auth login`
```

opencode is bring-your-own-provider: it authenticates against whichever model
backends you connect (`opencode auth login`), and Ciaobot lists the models of
the connected ones. Ciaobot runs one `opencode serve` process per active chat
on an ephemeral loopback port protected by a per-process
`OPENCODE_SERVER_PASSWORD`, and drives it over HTTP plus the `/event` SSE
stream. Readiness verifies the operations Ciaobot needs against the server's
own OpenAPI document at `/doc`, so a logged-in but incompatible build is
reported as needing an update rather than half-working.

Workspace assets need almost no projection: opencode discovers
`.claude/skills/`, `.agents/skills/`, `AGENTS.md`, and `CLAUDE.md` natively, so
`ciao sync-skills` only generates `.opencode/agents/` (canonical subagents),
`.opencode/commands/` (canonical commands), and the `mcp` object in
`opencode.json`. Generated files carry a Ciaobot marker and only marked files
are pruned; `opencode.json` tracks Ciaobot-owned server names in
`.opencode/.ciao-managed-mcps.json` because JSON has no comment syntax.

opencode has no API for injecting a message into a running turn, so Ciaobot
keeps a mid-turn message in the next-turn queue instead of interrupting. This
is the same behavior every provider uses: Ciaobot buffers mid-turn messages and
flushes them as a fresh turn when the active one finishes. Fork, abort, tool
approvals, structured questions, and
background subagents (real child sessions) are all native. When a chat is
archived, deleted, or reset, Ciaobot disconnects its server and then calls
`DELETE /session/{id}` to reclaim the persisted opencode session; cleanup is
fail-open if the provider is unavailable.

### Auto mode (automatic permission review)

Ciaobot's opencode chats run in auto mode: the session permission ruleset is
`allow` for routine tools, with `bash` (every shell command) and Ciaobot's
destructive control-plane tools routed to `ask`. Each `ask` surfaces an
approval card in the chat that the operator approves or denies.

To get a Claude-Code/Codex-style **automatic** approval classifier instead of
manual cards, install the [`opencode-auto-permissions`](https://github.com/hueyexe/opencode-auto-permissions)
plugin into opencode's global config:

```bash
opencode plugin -g opencode-auto-permissions
```

The plugin answers each `permission.asked` with a reviewer model (your
session's model by default) that auto-approves routine work and denies
destructive or out-of-scope calls, so `rm`/`sudo`/`git push`/shell pipelines
are reviewed rather than prompting you. It is designed for opencode's Auto
mode; keep chats in Auto and opt into the plugin deliberately. See the
plugin's README and opencode's
[permissions docs](https://opencode.ai/docs/permissions/#auto-mode) for
configuration.

### Live eval provider access

`ciao eval` uses the selected provider's existing CLI authentication and the
same managed chat path as a normal turn. Claude runs require a working Claude
Code login. Live evals may call external tools and spend provider tokens, so
run the opt-in fixtures in `tests/fixtures/evals/`
individually and write reports to a disposable directory. Normal pytest and CI
coverage for the eval framework mocks provider execution and requires no
provider credentials.

### `gws`: Google Workspace CLI

Required by `gws-*` and `recipe-*` skills (Gmail, Drive, Docs, Sheets, Slides, Calendar, Tasks, Forms).

```bash
# Install: https://github.com/googleworkspace/cli
# Authenticate (two profiles):
GOOGLE_WORKSPACE_CLI_CONFIG_DIR=~/.config/gws-personal gws auth login   # personal
gws auth login                                                           # work
```

Use `ciao gws <profile> <gws-args>` to switch between accounts (`$GWS_PROFILE` holds the chat's own account). Never source the wrapper: it ends with `exec`. (`scripts/gws-profile.sh` remains as a thin back-compat shim that forwards to `ciao gws`.)

**PWA-native OAuth (no terminal required).** The Settings → Workspaces Google Workspace card can upload a GCP `client_secret.json` and drive the full OAuth code-exchange from the browser. The server generates the Google authorization URL, opens it in a new tab, and exchanges the returned authorization code for a refresh token. Accounts are added in that card (there are no built-in ones); credentials are written to `<workspace>/secrets/gws-<account>/`, except the two pre-registry names, which keep `<workspace>/secrets/gws-personal/` (personal) and `<workspace>/secrets/gws/` (work). Scopes granted: personal = Gmail + Calendar + Tasks; work also adds Drive, Docs, Sheets, and Slides. Use `Disconnect` to delete the stored credential files from the same panel. Note: these paths (`<workspace>/secrets/gws-*/`) are separate from `~/.config/gws-*/`, which the `gws` CLI uses by default when invoked directly via `ciao gws`.

**Getting `client_secret.json`.** In [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials): create or pick a project, enable the APIs you need (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks), then create an OAuth 2.0 client ID (Desktop app, or Web application with redirect URI `http://localhost`). Download the JSON credentials file and upload it in Settings → Workspaces (Google Workspace card). The PWA ⓘ panel on that card repeats these steps for end users. Stock `gws-*` skills ship with the app once `gws` is installed and authenticated.

**Auth + scope gotchas.** Use `gws auth login --full` for complete scopes. `gws auth login --services calendar` has produced tokens that still lack the calendar scope. `ciao gws` already execs `gws`, so `ciao gws personal gws calendar ...` doubles the command and fails with `Unknown service 'gws'`; pass the subcommand directly: `ciao gws personal calendar ...`. If auth fails despite a fresh login, `credentials.enc` (AES-256-GCM) may hold the valid refresh token while `credentials.json` carries a stale one. The work gcloud account only has `openid`/`cloud-platform` scopes and returns 403 on Drive uploads, so do not substitute `gcloud auth print-access-token` for `gws` auth in Drive flows.

**Headless re-auth.** When `gws auth login` fails on a headless server, run `ciao gws-auth-helper <profile>`. It prints the auth URL, waits for the redirect URL to be pasted back, and saves fresh credentials.

**Token expiry.** Symptom: `"token_error": "Token has been expired or revoked."`. Fix: `ciao gws <profile> auth login` (or the headless helper above). If `GOOGLE_WORKSPACE_CLI_CLIENT_ID` is set in `.env`, it can override the OAuth client to a wrong project; comment it out, or run `env -u GOOGLE_WORKSPACE_CLI_CLIENT_ID gws auth login --profile <profile>`.

**Token-health monitoring (automatic).** A background loop checks each configured profile's token validity on a fixed interval (`CIAO_GWS_HEALTH_INTERVAL`, default 900s). On a transition to invalid it sends one PWA notification and publishes an in-app `gws_health` status event naming the profile, so a revoked login surfaces instead of GWS-dependent schedules failing silently. The alert is debounced (one notification per breakage) and re-arms after the token recovers. The result also appears per profile in `GET /api/integrations/gws` as `token_valid` / `token_error` / `needs_relogin`, which Settings → Workspaces renders on the Google Workspace card.

**Server-managed re-login (reliable from chat).** `POST /api/integrations/gws/relogin/start` (body `{"profile":"personal"|"work"}`) runs the OAuth flow **inside the long-lived engine process**: it binds a loopback callback listener on `127.0.0.1:<ephemeral-port>`, returns the Google consent URL, captures the redirect, and exchanges the code server-side into the profile's `credentials.json`. Because the listener lives in the server (not a background bash task that dies between chat turns), the redirect is always captured. Poll `GET /api/integrations/gws/relogin/status?profile=<profile>` for `pending`/`completed`/`error`; `POST /api/integrations/gws/relogin/cancel` aborts. This reuses the same server-side scope, token-exchange, and credential-write code as the PWA-native upload flow. Tokens, client secrets, and authorization codes are never logged or returned in any response. Requires a Desktop-type OAuth client (loopback redirect on an arbitrary port).

**Credentials persistence.** GWS configs (`secrets/gws/` and `secrets/gws-personal/`) are gitignored and can be lost during git cleans or device migrations. Public installs should treat those directories as local secrets and back them up with an external secret manager if needed.


**Output parsing.** Strip the leading `Using keyring backend: file` banner from `gws` stdout before passing it to `jq`.

**PWA-only auth recovery.** When a token expires while the user is on the PWA with no shell, use the localhost-callback-relay flow: start the auth listener in the background, capture the sign-in URL, have the user open it on their phone, paste the redirect URL back, then `curl` it to the localhost listener.

### `notebooklm`: Google NotebookLM CLI

This is an optional, workspace-specific integration. It is not installed by
the Ciaobot package; install it only when a workspace uses the NotebookLM
skill.

```bash
pip install notebooklm-py
notebooklm login   # browser-based, saves to ~/.notebooklm/storage_state.json
cp ~/.notebooklm/storage_state.json .notebooklm-auth.json
```

### `opencli`: Website CLI

CLI with 50+ website adapters (YouTube, LinkedIn, GitHub, etc.). Optional manual install for workspace-specific workflows — not used by the stock `web-research` skill (that uses defuddle).

```bash
npm install -g @jackwener/opencli
opencli list   # see available adapters
```

Note: many adapters require the Browser Bridge Chrome extension and an logged-in Chrome session.

### Apple Intelligence (on-device session insights)

Nothing to install. Selecting **Apple** as the Session insights model in
Settings → Models uses Apple's on-device Foundation Model through the
`ciaobot-native` sidecar bundled in `Ciaobot.app` — no API key, no network, and
no per-task cost.

This replaced the `apfel` Homebrew CLI, which did the same job through the same
Apple models but had to be installed and kept up to date separately. Ciaobot
calls `FoundationModels` directly rather than Apple's `fm` CLI, because `fm`
only ships with macOS 27 while the framework behind it has been present since
macOS 26.

Requires macOS 26+, the desktop app, and Apple Intelligence switched on in
System Settings → Apple Intelligence & Siri. When any of those is missing,
Settings says which one and titles fall back to the configured cloud model.

### PWA workspace keyboard navigation

The PWA has no integration or environment setting for workspace shortcuts. In the
chat and automations views, unmodified `1`–`9` keys select the first through
ninth workspace in the sidebar's displayed order; the sidebar shows the assigned
number on each workspace button. Number keys remain available for normal typing
inside text fields.

The home screen shows the selected workspace's chats only; switching workspaces
swaps the home content. Arrow-key navigation follows the visible lane layout:
the selected workspace's lane and any stacked rescue lanes (stale or unknown
workspaces) use up/down to move between lanes and left/right to move between
chats within a lane.

The sidebar's project and subagent disclosure state is local UI state;
it has no integration, environment variable, or cross-device synchronization
setting.


### Python 3 + `google-cloud-bigquery`

Required by `bigquery-data` skill (`memory-vault/work/automations/bigquery/runner.py`).

```bash
pip install google-cloud-bigquery
```

Auth: set `GOOGLE_APPLICATION_CREDENTIALS` in `.env` (base64-encoded service account JSON or file path).

### `curl` + `jq`

Required by `zendesk-assistant` (direct REST API calls). Usually pre-installed.

## MCP Connectors (via claude.ai)

These MCP servers reach the session through the claude.ai connector bridge. They are enabled per-workspace on claude.ai; no local install required. Availability is session-scoped: new connectors only surface after a fresh Claude Code session.

| Connector | Scope | Used by |
|---|---|---|
| Airtable | read/write | `airtable-opportunities`, `airtable-feedback`, `airtable-projects`, work daily, weekly review |
| Atlassian (Jira + Confluence) | read/write | `jira-tickets`, work daily, sprint review |
| Slack | read/write | work daily (authored Slack messages), content sourcing |
| Zoom for Claude | read-only on meetings (summaries, transcripts, recordings, My Notes, Zoom Docs); write for new Zoom Docs | memory curation (meeting ingestion), work daily (ZOOM subagent), ad-hoc meeting recall |

**Zoom capability map:** `search_meetings`, `recordings_list`, `get_meeting_assets` (AI summary + transcript + participants), `get_recording_resource`, `get_file_content`, `search_zoom` (chat + Zoom Docs), `create_new_file_with_markdown` (Zoom Docs only). No meeting creation; scheduling remains via Google Calendar.

If a connector's tools don't show up, the fix is on the claude.ai side: toggle the connector for the workspace, then start a new session.

### Self-hosted MCP (project-scoped)

`n8n_mcp` is not a claude.ai connector. It can be registered in `.mcp.json` as an HTTP MCP server and authenticated with a bearer token read from the `N8N_MCP_TOKEN` env var (never inline the token in `.mcp.json`). `scripts/run-ciao.sh` sources `.env` so ciao-spawned `claude` subprocesses inherit the token. Like the claude.ai connectors, n8n is usually workspace-scoped: add it to the denylist for workspaces where it should not be available.

Project MCP servers in `.mcp.json` are consumed by the provider runtimes from the shared project source. Ciaobot preserves user-owned provider configuration and keeps credentials as `${ENV_VAR}` references.

The one-line macOS installer preserves a configured workspace discovered from
the current LaunchAgent. On a clean installation it starts the packaged
engine without creating a workspace or random dashboard password; the desktop
app then presents bootstrap onboarding, which asks the user to create or adopt
the workspace and choose the password.

## Environment Variables

Copy `.env.example` to `.env` and fill in the app-level settings first:

**Required for a configured workspace:** `PWA_AUTH_TOKEN` — the dashboard password. Password protection is on by default; see `PWA_AUTH_REQUIRED` below for the opt-out. `CIAO_PUSH_CONTACT` is optional: leave it empty to run without Web Push notifications until you set a contact in Settings.

`ciao setup` writes the initial `.env` into the selected workspace, seeds stock agents, commands, schedules, agent-readable workspace docs (`CLAUDE.md`, `AGENTS.md`, `CIAO_CUSTOMIZATION.md`), and the default vault, renders `~/Library/LaunchAgents/com.ciao.server.plist`, and creates `~/Applications/Ciaobot.app`. The app shortcut opens `http://localhost:<port>/?setup=<token>`; the server redeems `.runtime/setup-token` once on localhost, sets the signed session cookie, then deletes the token. By default setup prints the launchd load command without starting the service; use `--load-launchd` to run `launchctl`. `ciao auth <claude|opencode>` runs the provider login command in Terminal; `--print-only` shows the command for the setup wizard. `GET /api/setup-status` reports required local config plus Claude Code and opencode readiness so the wizard can poll after terminal OAuth commands or `.env` edits. In bootstrap mode, `POST /api/setup/finish` accepts the wizard's final local choices (`workspace` and `password` are required; `provider` becomes the first logical workspace default; `vault_root` defaults to `memory-vault` inside it), writes the real workspace `.env`, scaffolds the configured `CIAO_VAULT_ROOT`, refreshes the LaunchAgent and `Ciaobot.app` shortcut, and requests the restart exit for supervisor relaunch (a foreground `ciao run` re-execs itself on that exit code).

**Runtime:** `CIAO_WORKSPACE`, `CIAO_PORT`

**Ciaobot agent control plane:**

- The embedded authenticated MCP endpoint and managed-process integration are mandatory and always on; there is no enable/disable switch and no alternative control surface. `CIAO_MCP_ENABLED` and `CIAO_CONTROL_SURFACE` were removed.
- `CIAO_MCP_LAZY_TOOLS`: lists only the core and destructive Ciaobot tools in `tools/list` and defers the rest to the `tools_search` / `tools_call` pair, so the full catalog (~9k tokens of JSON schema) is not injected into every chat. Set to `0` to list the whole catalog eagerly. Default `true`.
- `CIAO_MCP_SESSION_TOKEN`: internal, short-lived bearer capability injected only into a Ciaobot-managed provider process. Ciaobot sets it automatically and excludes it from model-created shell commands; operators must not configure or persist it.

The endpoint is mounted at `http://127.0.0.1:<PWA_PORT>/mcp/`. Do not place a static token in `.mcp.json`: Ciaobot generates a scoped short-lived token and configures its managed provider process. See [docs/MCP.md](docs/MCP.md).

The embedded server pins the Python MCP SDK at `mcp>=1.29.0,<2.0`, bumped for the MCP spec release `2026-07-28`. v1.29.0 is a compatibility release that speaks both the prior wire format and `2026-07-28`; Ciaobot stays on the v1 SDK line and does not migrate to the `2.0.0` rewrite. The `2026-07-28` spec opens a 12-month deprecation window for Roots, Sampling, Logging, and the legacy HTTP+SSE transport. `ciao/mcp_server.py` already runs `stateless_http=True`, `json_response=True`, with no session id and no Roots/Sampling/Elicitation usage, so none of that deprecated surface is in play here.

**Internal command markers:** `CIAO_COMMAND_BEGIN`, `CIAO_COMMAND_INSTRUCTIONS`, and `CIAO_COMMAND_END` are reserved transcript markers used when Ciaobot expands a Claude-style slash command for a managed provider. They are not environment variables and should not be configured.

**Optional direct-service keys:** none. Every provider owns its own authentication through its own CLI (`ciao auth <provider>`), so Settings → Providers has no API-key fields; Ciaobot consumes no model API key of its own.

Workspace-specific integrations can still be set in `.env`, but the public `.env.example` does not ship private/work examples. Use user-owned credentials for each integration:

**GWS:** `GWS_PROFILE`, `GOOGLE_WORKSPACE_CLI_CLIENT_ID`, `GOOGLE_WORKSPACE_CLI_CLIENT_SECRET`

**Airtable:** `AIRTABLE_API_KEY` (get from https://airtable.com/create/tokens, scopes: data.records:read/write, schema.bases:read)

**Zendesk:** `ZENDESK_EMAIL`, `ZENDESK_API_TOKEN` (Admin Center > APIs > Zendesk API), `ZENDESK_SUBDOMAIN`

**BigQuery:** `GOOGLE_APPLICATION_CREDENTIALS`

**OpenAI:** Ciaobot does not use an OpenAI API key. OpenAI-compatible models are reached through opencode, and voice is on-device.

**n8n MCP:** `N8N_MCP_TOKEN` (bearer token for the self-hosted `n8n_mcp` HTTP server in `.mcp.json`). Lives in `.env` only, value redacted. Settings → Assets → MCP servers shows the key status and can write it into `.env`.

**Notion MCP:** `NOTION_TOKEN` (internal integration secret from https://www.notion.so/profile/integrations, used by the official `@notionhq/notion-mcp-server` stdio MCP registered in `.mcp.json`). Lives in `.env` only, value redacted. Settings → Assets → MCP servers shows the key status and can write it into `.env`. Workspace-scoped: add `mcp__notion` to a workspace's `disallowed_tools` to keep it out of that workspace — for example, to make Notion personal-only, set the **work** workspace's denylist to the harness defaults plus `mcp__notion`. Tools surface as `mcp__notion__*`.

**Provider connections.** Settings → Providers launches, verifies, and logs out
Claude Code and opencode through their own CLIs; Ciaobot stores none of
their credentials, and there are no API-key fields to fill in. Voice and Apple
Intelligence are on-device and need no provider key.

**Reaching any other model.** Ciaobot talks to exactly two providers: Claude
Code (Anthropic) and opencode (everything else). opencode is
bring-your-own-provider, so Ollama, OpenRouter, LM Studio, or any
OpenAI-compatible endpoint is configured *in opencode* — see its own docs — and
Ciaobot picks it up automatically: `GET /api/models` lists whatever opencode
reports as connected, so its models appear in the chat picker, workspace
defaults, title generation, insights, schedules, and the critique
panel with no Ciaobot configuration at all.

This replaces three earlier mechanisms — `CIAO_OLLAMA_*` routing, an
`OPENROUTER_API_KEY` backend, and user-registered "custom compatible providers"
in `.ciao/custom_providers.json`. All three worked by pointing the Claude Code or
provider CLI at a third-party upstream through `ANTHROPIC_BASE_URL` /
`OPENAI_BASE_URL` env injection. Those variables and that file are no longer
read; leftover values are ignored rather than erroring.

## Google Tasks Reference

| List | ID |
|---|---|
| Backlog | `MTM3MDY3ODI0NjY2ODE2Mzg1ODk6MDow` |
| In Progress | `VWVEMHZCMkhuaGVpcG0yUQ` |
| On Hold | `OUVaZExIdDA3b3JBeUpMVA` |
| Automate | `Z2pjblpMd2NrNTlYRlVjQQ` |

## Skill-owned reference data

The Jira project table, Airtable base/table IDs, and similar skill-specific reference data live next to the skill that uses them, not here. See `skills/jira-tickets/SKILL.md`, `skills/airtable-feedback/SKILL.md`, `skills/airtable-opportunities/SKILL.md`, `skills/airtable-projects/SKILL.md`.

## Ciaobot Server Operation

Runtime config for the Ciaobot server itself (PWA, schedules, deploy).

### Required env vars

- `PWA_AUTH_TOKEN` (required): the dashboard password, doubling as the pre-shared
  token for PWA auth and the session-signing secret. The first-run wizard asks for
  it and Settings → PWA password changes it. On a node in client
  mode this local token is not what you log in with: the login screen
  authenticates against the *host's* token, and Settings → PWA password edits the
  host's too (that card is proxied). The local token still guards this machine's
  own never-proxied routes, `/api/node/*` and `/api/device/*`.
- `CIAO_PUSH_CONTACT` (optional): push notification contact string for the Web Push VAPID subject, for example `mailto:you@example.com`. Empty disables Web Push delivery until set (in `.env` or Settings), but the macOS menu-bar companion still posts local alerts from the runtime notification log. Read mutations emit a clear control for matching delivered PWA and macOS notifications.
- `PWA_PORT` (default `8443`), `PWA_HOST` (default `0.0.0.0`).
- `CIAO_RUNTIME_ROOT` (optional): runtime-state directory. `Ciaobot.app` reads
  this from the configured workspace `.env` and resolves a relative value
  against the workspace.
- `CIAO_ENGINE_PATH` (internal): bundled engine path inherited by onboarding
  and desktop helpers; it is normally supplied by `Ciaobot.app`, not set by
  operators.
- `CIAO_DESKTOP_SERVER_URL` (development only): overrides desktop runtime
  discovery for a local Tauri development server target.
- Session cookies are HttpOnly. Production/domain-scoped cookies are also Secure, and state-changing browser requests must come from the same host via `Origin` or `Referer`.
- Ciaobot sends baseline security headers from the Starlette app, including CSP, `X-Content-Type-Options`, `Referrer-Policy`, and frame denial.
- Workspace HTML artifact previews use a stricter sandbox CSP: inline scripts/styles and `data:` images/fonts/audio/video are allowed, while network connections, `blob:` sources, and same-origin session access are blocked.

### Optional env vars

- `CLAUDE_EXECUTION_MODE` / `CLAUDE_PERMISSION_MODE`: **removed 2026-08-21 and no longer read.** Execution mode is fixed at `auto` for every provider (Claude Code and opencode). Auto lets safe reads and edits run silently and asks before destructive operations. An install that still sets one gets a `legacy-env-ignored` operator tile, because a setting that is silently ignored reads as a setting that is in effect.
- `PWA_AUTH_REQUIRED`: password protection for the PWA dashboard. **Enabled by default** — an unset value protects the dashboard whenever `PWA_AUTH_TOKEN` is present (without a token there is no password a human could type, so protection stays off until one is set in Settings). Set it to `false` to run unprotected on a machine nobody else can reach; that is the only way to turn protection off, since Settings can only change the password. `ciao setup` writes the value explicitly (`--no-auth` writes `false`).
- `CIAO_ALLOWED_ORIGINS`: comma-separated extra hostnames/origins accepted for state-changing HTTP and WebSocket handshakes when the app is reached under a host it doesn't bind to (reverse proxy, tunnel, or host alias). Without it, such setups get their `/ws/*` upgrades rejected (403) and live updates stall. A proxy-supplied `X-Forwarded-Host` is honored automatically. Example: `app.example.com,ciao.tailnet.ts.net`.
- `CIAO_DEV_MODE`: set to `true` to enable developer mode controls in the PWA dashboard (like the Deploy button), the `/api/debug/issues` report, and the desktop-app rebuild step in Settings → Restart.
- `CIAO_APP_REPO`: absolute path to the Ciaobot source checkout for developer-mode Deploy/Restart actions. Packaged apps update through the signed Tauri updater and do not resolve a checkout, Homebrew, or PyPI installation.
- `CIAO_VAULT_MODE`: onboarding mode for vault folders. Either `scratch` (initialize the current vault layout) or `existing` (preserve the selected notes folder and start an initial inventory/curation chat; clear material may be reorganized, ambiguous material is left in place).
- `CIAO_BOOTSTRAP_WORKSPACE`: temp workspace root used when `PWA_AUTH_TOKEN` is absent. Defaults to `~/.ciao/bootstrap`; Ciaobot persists the generated bootstrap auth token under its `.runtime/` so first-run setup survives a restart.
- `CIAO_NO_BROWSER`: set to any value to stop a first-run `ciao run` from auto-opening the setup wizard in the default browser (the wizard URL is still printed). Auto-open already only happens on interactive terminals, never under launchd or CI.
- `CIAO_WORKSPACE`: filesystem workspace root for operational state, `.runtime/`, `.env`, `.claude/`, `.agents/skills/`, `CLAUDE.md`, and `AGENTS.md`. Default `.`.
- `CIAO_OPENCODE_BIN`: optional absolute path to the opencode CLI. Normally unnecessary because Ciaobot checks the login-shell PATH.
- `CIAO_VAULT_ROOT`: durable memory/vault root. Default `<CIAO_WORKSPACE>/memory-vault`. Set this to an external notes folder when operational state should stay out of synced notes.
- `CIAO_WORKSPACES`: JSON workspace registry. Preferred shape is a list of objects with `name`, `vault_root`, `default_provider`, `disallowed_tools`, and `gws_profile`. `vault_root` is relative to `CIAO_WORKSPACE` unless absolute. It is an internal/setup migration field: fresh setup and ordinary PWA workspace creation derive `<CIAO_VAULT_ROOT>/<name>`, while existing-folder setup preserves the selected root until a model-guided migration updates the registry. Later Settings updates preserve the stored path. If unset, Ciaobot reads `.runtime/workspaces.json`; if that is also missing, Ciaobot bootstraps one registry entry per directory in the vault that looks like a workspace (a folder containing `People/`, `Projects/`, `journal/` or a `MEMORY.md`), falling back to a single `personal` workspace when none do. It used to manufacture `personal` and `work` unconditionally, which left an install unable to re-root: a registered workspace with no vault directory refuses the plan. Schedules assigned to a workspace inherit its current `default_provider` on every run unless an explicit override is stored; the model comes from that provider's operator default (Settings → Models) — a workspace no longer pins one, and a `default_model` key in the registry is ignored. Example: `[{"name":"default","vault_root":"memory-vault/default","default_provider":"claude","gws_profile":"personal"}]`.
- `CIAO_AUTO_SYNC_ON_START=false` disables the automatic `git pull --rebase` on server startup (enabled by default).
- `CIAO_RESTART_EXIT_CODE=75`: the exit code `ciao.main` returns to signal `scripts/run-ciao.sh` to restart in place (used by the Deploy button). The restart loop picks up a new `.env` on every iteration, so Deploy doesn't need a full launchd reload.
- `CIAO_LOG_LEVEL`: root log level for the server (default `info`). Accepts standard names (`debug`, `info`, `warning`, `error`) or numeric values. Setting it to `debug` also attaches a rotating `.runtime/server_debug.log` (10 MB × 2 backups) capturing every DEBUG+ record — provider stderr noise, lifecycle events, uvicorn request logs — which the dev-mode `/api/debug/issues` report and the `{{ISSUE_REPORT}}` placeholder surface alongside the error tail so failures can be traced beyond their final error line.
- `CIAO_CLAUDE_MAX_BUFFER_BYTES`: max bytes the Claude Agent SDK buffers for a single JSON message from the `claude` CLI subprocess stdout. Raises the SDK's 1 MiB default (Ciaobot default 32 MiB) so one large tool result or content block doesn't abort the turn. If even this is exceeded the turn ends with a recoverable error instead of a crash. Set higher only for unusually large payloads.
- File viewer path policy: the file/binary/image viewers and the in-PWA editor have **no workspace sandbox**. They read (and, for the editor and snapshot-restore, write) any path on disk. Relative paths still anchor to the workspace root. The extension allowlist (no `.env`, no key files) and the size caps are the only remaining guards, so secrets in allowlisted files elsewhere on the machine are reachable from an authenticated PWA session.
- `CIAO_GWS_HEALTH_INTERVAL`: seconds between Google Workspace token-health checks (default `900`). Each cycle runs a cheap `gws auth status` per configured profile; when a refresh token is revoked/expired it fires one PWA notification plus an in-app status event (debounced until the token recovers). Set to `0` to disable the periodic check.
- `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND`: optional override for `gws`; the server defaults it to `file` at startup for headless auth.
- `ciao gws-auth-helper <profile>`: interactive headless OAuth re-authentication when `gws auth login` cannot open a browser.
- `CIAO_INSIGHTS_DISABLED`: set to `true`/`yes`/`on` to disable post-archive session insights extraction. Default is enabled (false). When enabled, after a chat is archived, raw JSONL is filtered and run through a model to extract errors, dead ends, new entities, decisions, and reusable code, then appended as a `## Session insights` section to the archive markdown.
- `CIAO_INSIGHTS_MODEL`: model ID for insights extraction. Default `sonnet` — a tier alias, resolved by whichever provider runs the routine. Set it to `apple` (or choose Apple in Settings) to use the on-device Foundation Model; if Apple Intelligence is unavailable, Ciaobot falls back to the configured automatic model.
- `CIAO_INSIGHTS_BACKFILL_ON_STARTUP`: set to `true`/`yes`/`on` to asynchronously scan for and backfill missing session insights on server startup. Default is disabled (false). Helps regenerate missing insights if the model call failed during chat archive due to budget or network issues.
- `CIAO_INSIGHTS_TIMEOUT_S`: per-call timeout (seconds) for the insights and text-fallback model calls. Default `600`. The insights model is operator-selectable, and a slow local or cloud GGUF can take 214–253s end to end; the previous flat 120s budget turned tail latency into a `TimeoutError` and failed the job. Lower it if you route insights at a fast model and want to fail sooner.
- `CIAO_INSIGHTS_MAX_INPUT_CHARS`: ceiling (characters) on the filtered transcript sent to the insights model. Default `320000`, roughly 90k tokens, which leaves headroom for the system prompt inside a 128k-token context window. Oldest transcript lines are dropped first so the newest turns and their `[idx=N]` citations survive; the trim is logged. Raise it for a large-context model, lower it if you still see `400 Message too long`. An oversized-input rejection is not retried, since the identical payload would fail again.
- `CIAO_INSIGHTS_BACKFILL_MAX`: most archives a single backfill run will process when the caller supplies no explicit limit — which is what both the startup job and the Settings → Automations button do. Default `200`. One archive is one model call, so on an aged vault an uncapped run from a single click is hours of work and a large bill; when the cap trims a run, the job record carries `capped_at` and `remaining_after_cap` rather than implying it finished everything. `scripts/backfill_insights.py --limit 0` still opts into a genuinely unbounded pass.
- `CIAO_TRAJECTORIES_DISABLED`: set to `true`/`yes`/`on` to disable structured trajectory capture after a chat is archived (skills loaded, tools used, errors, decisions). Default enabled. Trajectories are written to `~/.ciao/trajectories/YYYY-MM/<session-id>.json` and mined by the weekly skill-evolution pass.
- `CIAO_REVIEW_MODELS`: comma-separated list of model IDs for the `adversarial_review` MCP tool (`ciao.critique`). Overrides the default panel. An entry may name the provider that runs it: `opencode:<tier>` routes to that provider's app-server, and a bare tier alias runs through Claude Code. The default panel lists one voice per signed-in vendor. Runtime-overridable from the PWA (Settings → Models, persisted in `.runtime/app_settings.json` under `critique_models`).
- `CIAO_ADVERSARIAL_MODELS`: legacy alias for `CIAO_REVIEW_MODELS`.
- `CIAO_MEMORY_CHAR_LIMIT`: advisory cap (chars) on the `ciao:memory` region in the workspace `CLAUDE.md`. Default `3000`. Nothing refuses a write at edit time — `memory_update` writes over the cap and reports `over_cap` with `used_chars` — and `os_audit` plus nightly memory curation report and consolidate when over cap. It was enforced as a hard refusal until 2026-08-20, which made accepting a queued proposal impossible once the region filled up, without shrinking the region.
- `CIAO_USER_CHAR_LIMIT`: advisory cap (chars) on the `ciao:profile` region. Default `1375`. Same advisory flow as `CIAO_MEMORY_CHAR_LIMIT`.
- `VOCAB_PROMOTION_THRESHOLD`: the usage threshold (a minimum of 2) above which a non-canonical `type:` or a tag becomes a candidate for the canonical/established set in the workspace-hygiene vocabulary proposals. Default `5`, matching the established-tag tier boundary. Raising it delays promotions and merges; it is shared by the proposal audit (`ciao/vocabulary_proposals.py`) and the generated `VOCABULARY.md` tiers (`ciao/vault_index.py`). Not a `CIAO_*` variable, so it is not covered by the env-var documentation test; it is documented here for operator discoverability.
- `CLAUDE_DEFAULT_MODEL_PERSONAL` / `CLAUDE_DEFAULT_MODEL_WORK` / `CIAO_DISALLOWED_TOOLS_PERSONAL` / `CIAO_DISALLOWED_TOOLS_WORK`: **removed 2026-08-20 and no longer read.** They configured the two hardcoded `personal`/`work` entries of the bootstrap registry, which now derives its workspaces from the vault instead, so they could not describe a workspace named anything else. Put `disallowed_tools` on the workspace in `.runtime/workspaces.json` (or `CIAO_WORKSPACES`), which works for any name; the default model is now a per-provider operator setting (Settings → Models), not a per-workspace one. An install that still sets one gets a `legacy-env-ignored` operator tile, because a setting that is silently ignored reads as a setting that is in effect.
- `CIAO_MEMORY_DIR`: legacy override for the old `~/.ciao/memory.md` + `user.md` directory during the one-release migration window. Default `~/.ciao`. Not used for new writes; safe to unset after migration.
- `CIAO_AUTO_VAULT_INDEX`: set to `false` to disable automatic vault index regeneration on server startup. Default `true`.
- `CIAO_GITHUB_REPO`: `owner/name` of the GitHub repository used to fetch the changelog (commits between the installed and latest release tags) shown in the Settings update prompt. Default `raffaelefarinaro/ciaobot`.
- `CIAO_GITHUB_TOKEN` (also honors `GITHUB_TOKEN` / `GH_TOKEN`): personal access token used to authenticate on-demand GitHub REST API calls when fetching the changelog for an available update. Optional; when set it raises GitHub's API rate limit from 60 to 5000 requests/hour. The recurring update check does not use the API at all (it follows the public `releases/latest` redirect), so a token is not needed just to check for updates. No scopes are required (public read only).
- **Image-capability pre-flight**: before dispatch, a turn that carries images checks whether the selected model can see them. Anthropic's and OpenAI's current models all accept images, so only opencode is consulted — it is bring-your-own-provider, and its catalog states each model's `capabilities.input.image`. An unstated answer counts as capable, so a cold catalog or an older opencode build never blocks a turn. A non-vision model pauses the turn on a `model_capability_question` (30s window): the PWA offers the models opencode states accept images, an "Open picker" escape hatch, and Cancel. Switch re-dispatches on the picked model; cancel/timeout close the turn with a `status` bubble; the images are never silently dropped. Text-only and unattended (loop/schedule) turns skip the question. Implemented in `ciao/providers/opencode.py::model_accepts_images` and the pre-flight in `ciao/web/project_chats.py::ProjectChatManager.stream_chat`.
- `CIAO_PUSH_CONTACT`: push notification contact string. Optional, no default; empty disables Web Push delivery. Used for VAPID subject.
- `CIAO_PUSH_DELAY_SECONDS`: delay before sending push notifications after a completed turn (default `30`). Rapid replies to the same chat cancel the previous timer and start a new one (coalesce into a single push). Permission requests and model questions push immediately (no delay). Unanswered permission requests re-fire every 30 seconds, up to 3 times, until the user approves/denies or the turn ends. Marking a chat read sends a separate clear control to all registered Web Push subscriptions and the macOS notification log.
- `CIAO_PYTHON`: path to a specific Python binary for `scripts/dev.sh` (e.g. when Homebrew breaks `ensurepip`).
- `CIAO_PATH`: baked into the launchd plist's `EnvironmentVariables` at setup time so developer-mode subprocesses (npm, node, git) are found despite launchd's minimal default PATH. Not an operator env var; it's a `com.ciao.server.plist.tmpl` placeholder rendered from the user's shell PATH.
- `CLAUDE_MODELS`: comma-separated list of Anthropic models in the picker. Default `opus,sonnet,haiku,fable`.
- `CIAO_TITLE_MODEL`: model used to auto-title Anthropic chats. Default `haiku`.
- `CIAO_TITLE_MODEL_OVERRIDE`: env-level default for the title-model override normally set from the PWA (Settings → Models tab, persisted in `.runtime/app_settings.json`). When set (either way), it wins over `CIAO_TITLE_MODEL`. An `opencode:` prefix routes titles through that provider. Empty = automatic.
- `CIAO_NATIVE_SIDECAR`: absolute path to the `ciaobot-native` binary that backs both on-device voice engines. Normally unset — the engine finds it inside the installed `Ciaobot.app`. Point it at `desktop/src-tauri/binaries/ciaobot-native-aarch64-apple-darwin` to test a locally built sidecar (`npm run build:native` in `desktop/`) without installing the app.
- Installer and release-build variables: `CIAO_APP_DIR` overrides the per-user app directory, `CIAO_ARCHIVE_NAME` and `CIAO_VERIFIER_NAME` override release asset names, and `CIAO_RELEASE_BASE_URL` selects a private release mirror for an explicit `--version` (the embedded archive signature is still mandatory). `CIAO_BUNDLED_APP` marks the embedded runtime for internal mode detection. `CIAO_PYTHON_ARM64_URL`, `CIAO_PYTHON_ARM64_SHA256`, `CIAO_PYTHON_X86_64_URL`, and `CIAO_PYTHON_X86_64_SHA256` are required only by the release workflow when assembling the embedded runtimes. `CIAO_EXECUTABLE` is a LaunchAgent template token, not an operator environment variable.
- `CIAO_TRANSCRIPTION_LOCALE`: BCP-47 language for both on-device engines — dictation matches it against the installed dictation languages, and the synthesizer picks a voice for it. Default `en-US`.
- `CIAO_TTS_LOCAL_VOICE`: macOS voice identifier or name for the local engine (e.g. `com.apple.voice.compact.en-US.Samantha`). Empty by default, which means the highest-quality installed voice for `CIAO_TRANSCRIPTION_LOCALE` — preferring premium, then enhanced, then default. The stock voices are all the basic tier; voices marked **Premium** (then Enhanced) sound markedly better and are a free download under System Settings → Accessibility → Read & Speak → System voice → Manage Voices ([Apple's guide](https://support.apple.com/guide/mac-help/mchlp2290/mac)). Ciaobot picks the best installed voice automatically, so downloading one is enough. Siri's voices are not available to third-party apps.
- `CIAO_MAX_IMAGE_BYTES` / `CIAO_MAX_VOICE_BYTES`: upload size caps. Defaults 10 MB / 25 MB.
- `CIAO_PUBLIC_PRIVATE_PATTERNS`: comma-separated private string patterns used by `ciao public-preflight scan` when a `--private-patterns` file is not supplied. Intended for public extraction checks, not normal runtime.

**Note:** `ciao gws-auth-helper` is the helper for headless `gws` auth when the keyring backend fails.

### Injected CLI context variables

The Ciaobot server injects the following environment variables into every spawned agent CLI subprocess (`claude` or `opencode serve`):

- `CIAO_WORKSPACE`: the filesystem workspace root path. This is operator config forwarded for compatibility; it is not the logical chat workspace.
- `CIAO_ACTIVE_WORKSPACE`: the logical workspace name derived per turn from `chat -> project -> project.workspace`.
- `CIAO_ACTIVE_PROJECT`: the active project ID.
- `CIAO_MODEL`: the model ID configured for the chat.
- `CIAO_PROVIDER`: the provider name (`claude` or `opencode`).
- `CIAO_CHAT_ID`: the ID of the active chat.
- `GWS_PROFILE`: resolved from the active workspace's `gws_profile`, falling back to `GWS_PROFILE` / the default profile.
- `CLAUDE_CODE_DISABLE_AUTO_MEMORY`: set to `1` to disable Claude Code's native auto-memory system since Ciaobot implements its own memory layer.

These variables let package commands (like `ciao create-chat`) or custom skills auto-detect the current chat's context and preferences.

### Google OAuth client files

- Do not commit Google OAuth client JSON files. `client_secret_gws.json` is ignored and should live only as a local/operator file when needed.
- Prefer `gws` profile config directories for active tokens: `~/.config/gws-personal/` and `~/.config/gws/`.
- If a client secret was ever committed, rotate that OAuth client in Google Cloud. Removing the file from the repo does not remove it from git history.

### Deploy

Ciaobot runs on macOS under launchd.

- `ciao setup --workspace <path> --load-launchd` renders and loads the LaunchAgent.
- The packaged launchd template is `ciao/stock/deploy/com.ciao.server.plist.tmpl`.
- The `Ciaobot.app` menu bar shows `Start at Login: On/Off` and toggles `com.ciao.server` with `launchctl enable/disable`. Its status section also offers `Start Server` when the local server is unreachable and `Restart Server` when it is live. The unread badge counts every unread chat even though the quick-open list remains limited to the ten most recent chats.
- Selecting `Update` from the menu-bar tray opens the bundled update window immediately. It reports engine/app milestones with a percentage and expandable terminal details, then restarts the app only after both halves are ready. The PWA's non-bundled package-update actions use the same visual progress surface while their restart is pending.
- Stop: `launchctl unload ~/Library/LaunchAgents/com.ciao.server.plist`.
- Remote access is not configured by the public app. Use localhost by default, or put Tailscale or another user-owned network layer in front of the local server.
- In client mode, non-image files dropped into chat are uploaded through the
  authenticated host tunnel into the active project folder; the composer uses
  the returned absolute host path, never the client's local filesystem path.

### Server startup behaviors

Auto-skills update, auto-CLI update, and similar behaviors belong in server startup code (`ciao/main.py`), not in Claude Code's `settings.json` hooks.

Enabled schedules also receive one startup catch-up check. If the latest expected occurrence was missed while the server was unavailable, it runs immediately once; older skipped intervals are not replayed, and the scheduled prompt receives the current run context rather than a backdated occurrence date.

The Settings → Automations view receives per-job capability metadata from
`GET /api/automation`: `uses_model` identifies model-backed work and
`produces_outcome` identifies durable or user-visible results. These flags are
static registry metadata and remain available before a job has run.
