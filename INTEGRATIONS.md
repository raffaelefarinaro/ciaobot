# Integrations Setup

One-time setup for external tools and CLI dependencies.

The chat's Work details window displays project context and a link to its canonical document; this is a presentation of existing context, with no new integration, configuration, or change to what providers receive.

SDK-level wiring notes (fallback_model, hooks, setting_sources) live in the module docstring of `ciao/providers/claude.py`.

## Install

### Windows 11 (preview)

```powershell
irm https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.ps1 | iex
```

The installer is per user and needs no administrator rights. It installs the engine into a `uv tool` environment, puts `ciao.exe` on your user `PATH`, creates the workspace (`%USERPROFILE%\Ciaobot`, or `-Workspace DIR`), registers the per-user logon task `\Ciaobot\Engine` and starts it. Options are `-Workspace`, `-NoStart` and `-Uninstall`; because `iex` cannot take arguments, pass them through a script block. The full guide, including logs and troubleshooting, is [docs/WINDOWS.md](docs/WINDOWS.md). For engine updates and rollback, see [the Windows update guide](docs/WINDOWS.md#update-and-rollback).

### Upgrading from the macOS app

v1.0.0 retires the macOS `Ciaobot.app`; the PWA is now served by the engine.
There is no data migration and no vault change. If you still run the app, run
the installer with `--migrate`: it hands the engine the app currently manages
over to the terminal installer, keeps your workspace, password, chats,
schedules and push subscriptions, and removes the old `Ciaobot.app` bundle.

```bash
curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh -s -- --migrate
```

If the installer cannot tell whether your Mac is the host or a client, it says
so and asks for `--as-host` or `--as-client <url>`.

Should the automatic removal not be able to run — a `/Applications` install the
account cannot write, for instance — the installer says so and leaves the bundle
alone, and `ciao desktop uninstall` removes it by hand. That command takes the
bundle (`~/Applications`, or `/Applications` for older installs; `--app-dir`
points elsewhere) with its LaunchAgents and any `~/.local/bin/ciao` shim that
pointed inside it. Your workspace folder and its notes are kept. Updates after
that are the same one-liner again, or **Settings → Home** in the PWA, which
stages and applies the engine package update in the background; there is no
in-app app updater.

## CLI Tools

### The `ciao` command

The installer puts the engine in its own `uv tool` environment and writes a shim
at `~/.local/bin/ciao` that forwards to it — that is what makes the `ciao ...`
commands in this document work in a terminal. Two cases need a manual step, and
the installer says which one applies:

- `~/.local/bin` is not on your `PATH`: add it. The setup wizard shows the
  exact copyable line for your OS and shell; the variants are:
  - zsh (default): `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc`
  - bash (login shells read `~/.bash_profile`, not `~/.bashrc`):
    `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bash_profile && source ~/.bash_profile`
  - fish: `fish_add_path $HOME/.local/bin`.
  - Windows (PowerShell): the wizard substitutes your real directory for the
    `C:\Users\you` below —
    `$k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true); $v = $k.GetValue('Path', '', 'DoNotExpandEnvironmentNames'); $k.SetValue('Path', 'C:\Users\you\.local\bin' + $(if ($v) { ';' + $v }), [Microsoft.Win32.RegistryValueKind]::ExpandString)`
    — then open a new terminal so the change takes effect. It reads the old
    value unexpanded and writes it back as `REG_EXPAND_SZ`, so a PATH that
    holds `%USERPROFILE%`-style entries keeps both those entries and its
    registry type; `[Environment]::SetEnvironmentVariable` would freeze them
    to today's expanded paths and change the type to `REG_SZ`.
- Another `ciao` already exists (the name collides with Ciao Prolog, among
  others): the installer never overwrites it, so call Ciaobot's engine by its
  full path instead.

On Windows the installer does not write a shim: `ciao.exe` lives in the `uv tool` bin directory (`uv tool dir --bin`), which the installer adds to the user `PATH`.

Provider logins do not depend on the shim: the setup wizard hands out each
provider's own login command (`claude auth login`, `opencode auth login`).

## OpenCode

Ciaobot requires **OpenCode 2.0.16 or newer in the 2.x line**. OpenCode 1 is
not supported because its server API was replaced in V2. Settings reports older
or unidentifiable installs with an explicit upgrade message, and chat startup
fails closed rather than probing retired V1 routes.

```bash
npm install -g @opencode/cli        # works on Windows too
# or: brew install anomalyco/tap/opencode-v2
# or: curl -fsSL https://opencode.ai/v2/install | bash
ciao auth opencode                  # opens `opencode auth login`
```

The npm package `opencode-ai` and the `sst/tap` Homebrew tap are OpenCode 1 and do not work with Ciaobot, and `opencode upgrade` does not move a 1.x install to 2.x. If you installed OpenCode 1 that way, run `npm uninstall -g opencode-ai` first, because it also provides an `opencode` command.

OpenCode is bring-your-own-provider: it authenticates against whichever model
backends you connect, and Ciaobot lists enabled models from active providers
via the V2 `/api/provider` and `/api/model` endpoints. Ciaobot runs one
`opencode serve` process per active chat on an ephemeral loopback port
protected by a per-process `OPENCODE_SERVER_PASSWORD`. Startup reads the server
identity from `/api/info`, requires the tested 2.0.16+ API, and verifies the
operations Ciaobot uses against `/openapi.json`. Chat output arrives on the
`/api/event` SSE stream.

Workspace assets need almost no projection: OpenCode discovers
`.claude/skills/`, `.agents/skills/`, and `AGENTS.md` natively, so
`ciao sync-skills` only generates `.opencode/agents/` (canonical subagents),
`.opencode/commands/` (canonical commands), and the `mcp` object in
`opencode.json`. Generated files carry a Ciaobot marker and only marked files
are pruned; `opencode.json` tracks Ciaobot-owned server names in
`.opencode/.ciao-managed-mcps.json` because JSON has no comment syntax.

Ciaobot sends V2 text/file prompts with queued delivery, so a message produced
during an active turn remains a later turn instead of steering the current one.
V2 has no stable per-prompt system field, so compact core/runtime context rides
in the text behind markers that transcript replay and session handovers strip.
The provider normalizes V2's flat message records once at the HTTP boundary;
transcript rendering and child-session reads continue to use a small internal
part representation. Fork, interrupt, tool approvals, forms (rendered as
AskUserQuestion cards), and background subagents are native. When a chat is
archived, deleted, or reset, Ciaobot disconnects its server and then calls
`DELETE /api/session/{id}` to reclaim the persisted OpenCode session; cleanup
is fail-open if the provider is unavailable.

### Auto mode approvals

OpenCode chats follow the chat's permission mode (the per-provider default
from Settings → Models & providers, Auto unless changed). In Auto the V2
session permission ruleset allows routine tools while routing every `shell`
action to `ask`; Manual routes every tool to `ask` and Bypass suppresses
routine cards. V2 `glob` and `grep` resources are search patterns/regexes
rather than file paths, so those search actions require an explicit approval
card in every mode (including `bypass`) to prevent silent enumeration of
credential-bearing paths. In every mode, including Bypass, credential-path
denies (`**/.env`, `**/.runtime/**`, `**/secrets/**`, plus the resolved
`CIAO_RUNTIME_ROOT`) are appended last for OpenCode's V2 native file/search
actions. When the runtime root is inside the session location, the rules also
include its workspace-relative spelling because V2 internal resources are
relative. They do not cover `shell`, because a shell command cannot be
path-scoped by a glob. In Manual and Auto a shell command such as `cat .env`
still raises an approval card; in Bypass it runs without one, so Bypass does
not protect those files from shell access. Each `ask` surfaces an approval card
in the chat that the operator approves or denies. Native form cards validate
required, scalar, length, item-count, format, and pattern constraints before
submission; cards remain mounted until the server acknowledges delivery and
queue a response across a dropped chat socket.

The `opencode-auto-permissions` plugin was written for OpenCode 1 and does not
load under V2, so it cannot provide automatic review here. Use a
V2-compatible plugin only after verifying that it preserves Ciaobot's
server-side permission floor.

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

Use `ciao gws <profile> <gws-args>` to switch between accounts (`$GWS_PROFILE` holds the chat's own account). Never source the wrapper: it ends with `exec`. (`scripts/gws-profile.sh` remains as a thin back-compat shim that forwards to `ciao gws`.) The retired top-level `GWS_PROFILE` setting is no longer an operator default: on the first upgraded server start, a value naming an account this install knows is copied once into every workspace whose stored `gws_profile` is blank. Explicit workspace links are preserved, and later blank links are not backfilled; remove `GWS_PROFILE` from `.env` after the migration marker appears in `.runtime/`.

**PWA-native OAuth (no terminal required).** The Settings → Workspaces Google Workspace card can upload a GCP `client_secret.json` and drive the full OAuth code-exchange from the browser. The server generates the Google authorization URL, opens it in a new tab, and exchanges the returned authorization code for a refresh token. Accounts are added in that card (there are no built-in ones); credentials are written to `<workspace>/secrets/gws-<account>/`, except the two pre-registry names, which keep `<workspace>/secrets/gws-personal/` (personal) and `<workspace>/secrets/gws/` (work). Scopes granted: every account requests the same set — Gmail, Calendar, Drive, Sheets, Docs, Slides, Tasks, Contacts, Forms, plus `openid` and the user's email/profile (`ciao/gws_auth.py`). Use `Disconnect` to delete the stored credential files from the same panel. Note: these paths (`<workspace>/secrets/gws-*/`) are separate from `~/.config/gws-*/`, which the `gws` CLI uses by default when invoked directly via `ciao gws`.

**Getting `client_secret.json`.** In [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials): create or pick a project, enable the APIs you need (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, People for Contacts, Forms), then create an OAuth 2.0 client ID (Desktop app, or Web application with redirect URI `http://localhost`). Download the JSON credentials file and upload it in Settings → Workspaces (Google Workspace card). The PWA ⓘ panel on that card repeats these steps for end users. Stock `gws-*` skills ship with the app once `gws` is installed and authenticated.

**Auth + scope gotchas.** Use `gws auth login --full` for complete scopes. `gws auth login --services calendar` has produced tokens that still lack the calendar scope. `ciao gws` already execs `gws`, so `ciao gws personal gws calendar ...` doubles the command and fails with `Unknown service 'gws'`; pass the subcommand directly: `ciao gws personal calendar ...`. If auth fails despite a fresh login, `credentials.enc` (AES-256-GCM) may hold the valid refresh token while `credentials.json` carries a stale one. The work gcloud account only has `openid`/`cloud-platform` scopes and returns 403 on Drive uploads, so do not substitute `gcloud auth print-access-token` for `gws` auth in Drive flows.

**Headless re-auth.** When `gws auth login` fails on a headless server, run `ciao gws-auth-helper <profile>`. It prints the auth URL, waits for the redirect URL to be pasted back, and saves fresh credentials.

**Token expiry.** Symptom: `"token_error": "Token has been expired or revoked."`. Fix: `ciao gws <profile> auth login` (or the headless helper above). If `GOOGLE_WORKSPACE_CLI_CLIENT_ID` is set in `.env`, it can override the OAuth client to a wrong project; comment it out, or run `env -u GOOGLE_WORKSPACE_CLI_CLIENT_ID gws auth login --profile <profile>`.

**Token-health monitoring (automatic).** A background loop checks each configured profile's token validity every 900 seconds. On a transition to invalid it sends one PWA notification and publishes an in-app `gws_health` status event naming the profile, so a revoked login surfaces instead of GWS-dependent schedules failing silently. The alert is debounced (one notification per breakage) and re-arms after the token recovers. The result also appears per profile in `GET /api/integrations/gws` as `token_valid` / `token_error` / `needs_relogin`, which Settings → Workspaces renders on the Google Workspace card.

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

CLI with 50+ website adapters (YouTube, LinkedIn, GitHub, etc.). Optional manual install for workspace-specific workflows. Not required: URL reading uses the provider's own web-fetch tool, and GitHub URLs go through `gh`.

```bash
npm install -g @jackwener/opencli
opencli list   # see available adapters
```

Note: many adapters require the Browser Bridge Chrome extension and a logged-in Chrome session.

### PWA workspace keyboard navigation

The Settings → Home → Keyboard shortcuts card lets you disable or rebind the
PWA's global shortcuts, including the workspace number slots, and choose
Cmd/Ctrl+Enter or Enter to send. These preferences live in the engine's
`.runtime/app_settings.json` and are shared with connected devices. No
environment variable or integration setup is required. Enter-to-send stays off
for touch keyboards so Enter continues to insert a newline.

By default, unmodified `1`–`9` keys select the first through ninth workspace in
the sidebar's displayed order; the sidebar shows the assigned number on each
workspace button. Number keys remain available for normal typing inside text
fields.

The home screen shows the selected workspace's chats only; switching workspaces
swaps the home content. Arrow-key navigation follows the visible lane layout:
the selected workspace's lane and any stacked rescue lanes (stale or unknown
workspaces) use up/down to move between lanes and left/right to move between
chats within a lane.

The sidebar's project and subagent disclosure state is local UI state;
it has no integration, environment variable, or cross-device synchronization
setting.


### Python 3 + `google-cloud-bigquery`

Optional dependency for workspace-owned BigQuery workflows. No BigQuery skill is
packaged in this repository.

```bash
pip install google-cloud-bigquery
```

Auth: set `GOOGLE_APPLICATION_CREDENTIALS` in `.env` (base64-encoded service account JSON or file path).

### `curl` + `jq`

Optional dependency for workspace-owned Zendesk workflows (direct REST API
calls). Usually pre-installed.

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
engine without creating a workspace or random dashboard password; the PWA then
presents bootstrap onboarding, which asks the user to create or adopt the
workspace and choose the password.

## Environment Variables

Copy `.env.example` to `.env` and fill in the engine-level settings first:

**Required for a configured workspace:** `PWA_AUTH_TOKEN` — the dashboard password. Password protection is always on and cannot be turned off.

`ciao setup` writes the initial `.env` into the selected workspace, seeds skills (including `ciao-memory`), commands, schedules, agent-readable workspace docs (`AGENTS.md`, `CIAO_CUSTOMIZATION.md`), and the default vault, and renders `~/Library/LaunchAgents/com.ciao.server.plist`. No stock subagents ship; the main agent can delegate independent work or use custom subagents. Open `http://localhost:<port>` and follow the setup wizard; the first-run link is printed with the `?setup=<token>` one-time token, which the server redeems once on localhost, sets the signed session cookie, then deletes. By default setup prints the launchd load command without starting the service; use `--load-launchd` on macOS to run `launchctl`. `ciao auth <claude|opencode>` runs the provider login command in Terminal; `--print-only` shows the command for the setup wizard. `GET /api/setup-status` reports required local config plus Claude Code and opencode readiness so the wizard can poll after terminal OAuth commands or `.env` edits. In bootstrap mode, `POST /api/setup/finish` accepts the wizard's final local choices (`workspace` and `password` are required; `provider` becomes the first logical workspace default; `vault_root` defaults to `memory-vault` inside it), writes the real workspace `.env`, scaffolds the configured `CIAO_VAULT_ROOT`, refreshes the LaunchAgent, and requests the restart exit for supervisor relaunch (a foreground `ciao run` re-execs itself on that exit code).

**Runtime:** `CIAO_WORKSPACE`, `PWA_PORT`. `CIAO_PORT` does not control the
port the server binds; it is a legacy fallback used to *locate* a running
server when the workspace `.env` does not define `PWA_PORT` — read by CLI
commands, by the bootstrap launcher and by the macOS service manager
(`ciao/macos_service.py`), each of which falls back to it from the LaunchAgent
or process environment. Leave it in place on a legacy install rather than
removing it.

**Ciaobot agent control plane:**

- The CLI-first agent surface is the only control path and is always on; there is no enable/disable switch and no alternative surface. The former embedded MCP endpoint was removed in the S6 break; third-party project MCP servers (the agent's external tools) are still managed through Settings → Assets.
- `CIAO_AGENT_TOKEN` / `CIAO_AGENT_URL`: internal. Ciaobot hands the scoped short-lived token and the `POST /agent/v1/{op}` base URL to the managed provider's foreground shell so `ciao <noun> <verb>` commands can call the control plane (D-01). Background runs strip the token. Operators must not configure or persist either.

The endpoint is mounted at `http://127.0.0.1:<PWA_PORT>/agent/v1/{op}`. Ciaobot generates a scoped short-lived token and configures its managed provider process. See [docs/AGENT_CLI.md](docs/AGENT_CLI.md).

The agent control plane runs inside `ciao/mcp_server.py` (the shared operation table, bearer-token registry, and the envelope/plan-mode gate/telemetry) served by the `POST /agent/v1/{op}` route in `ciao/web/routes_agent.py`; it is not an MCP server, so the MCP SDK pin and the `2026-07-28` spec wire-format notes no longer apply.

**Internal command markers:** `CIAO_COMMAND_BEGIN`, `CIAO_COMMAND_INSTRUCTIONS`, and `CIAO_COMMAND_END` are reserved transcript markers used when Ciaobot expands a Claude-style slash command for a managed provider. They are not environment variables and should not be configured.

**Provider-owned credentials:** Claude Code and opencode credentials are owned by their CLIs (`ciao auth <provider>` or Settings → Models & providers). Ciaobot reads no provider API key and exposes no API-key editor.

Workspace-specific integrations can still be set in `.env`, but the public `.env.example` does not ship private/work examples. Use user-owned credentials for each integration:

**GWS:** `GOOGLE_WORKSPACE_CLI_CLIENT_ID`, `GOOGLE_WORKSPACE_CLI_CLIENT_SECRET`

**Airtable:** `AIRTABLE_API_KEY` (get from https://airtable.com/create/tokens, scopes: data.records:read/write, schema.bases:read)

**Zendesk:** `ZENDESK_EMAIL`, `ZENDESK_API_TOKEN` (Admin Center > APIs > Zendesk API), `ZENDESK_SUBDOMAIN`

**BigQuery:** `GOOGLE_APPLICATION_CREDENTIALS`

**OpenAI:** Ciaobot does not use an OpenAI API key. OpenAI-compatible models are reached through opencode.

**n8n MCP:** `N8N_MCP_TOKEN` (bearer token for the self-hosted `n8n_mcp` HTTP server in `.mcp.json`). Lives in `.env` only, value redacted. Settings → Assets → MCP servers shows the key status and can write it into `.env`.

**Notion MCP:** `NOTION_TOKEN` (internal integration secret from https://www.notion.so/profile/integrations, used by the official `@notionhq/notion-mcp-server` stdio MCP registered in `.mcp.json`). Lives in `.env` only, value redacted. Settings → Assets → MCP servers shows the key status and can write it into `.env`. Workspace-scoped: add `mcp__notion` to a workspace's `disallowed_tools` to keep it out of that workspace — for example, to make Notion personal-only, set the **work** workspace's denylist to the harness defaults plus `mcp__notion`. Tools surface as `mcp__notion__*`.

**Provider connections.** The chat providers card in Settings → Models &
providers launches, verifies, and logs out Claude Code and opencode through
their own CLIs; Ciaobot stores none of their credentials, and there are no
API-key fields to fill in.

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

Jira project tables, Airtable base/table IDs, and similar skill-specific
reference data belong next to the workspace skill that uses them, not in this
repository-wide document.

## Ciaobot Server Operation

Runtime config for the Ciaobot server itself (PWA, schedules, deploy).

### Required env vars

- `PWA_AUTH_TOKEN` (required): the dashboard password, doubling as the pre-shared
  token for PWA auth and the session-signing secret. The first-run wizard asks for
  it and Settings → PWA password changes it. There is one engine per install, so
  this is the only password that install has: a second device runs its own engine
  and its own token.
- `PWA_PORT` (default `8443`). The bind address is no longer an env var: the
  server binds all interfaces by default so the PWA is reachable over LAN and
  Tailscale, and Settings → General → Network access → "This computer only"
  makes it loopback-only (takes effect after a restart).
- `CIAO_RUNTIME_ROOT` (service-provided, process environment only):
  runtime-state directory. The runtime root is `<workspace>/.runtime` unless the
  process environment names another one; the macOS LaunchAgent sets it to
  `<workspace>/.runtime`, and tests and isolated dev backends use it to move the
  runtime root. It is **not read from `.env`**: setup no longer writes it there,
  and every reader of the workspace `.env` drops the line. A leftover
  `CIAO_RUNTIME_ROOT=.runtime` (what setup used to write) is silent; any other
  value raises the `legacy-env-ignored` operator tile, because the runtime it
  names is no longer the one the engine uses.
- `CIAO_OPENCODE_BIN` (optional): absolute path to an OpenCode CLI installed
  outside the login-shell `PATH`. The path is used for provider startup and
  authentication; an invalid path is reported as unavailable.
- `CIAO_ENGINE_PATH` (internal): legacy override for the engine executable the
  CLI runs. It is still read by `ciao/cli.py`, and on Windows `ciao service start` uses it as the interpreter (`python.exe` or `pythonw.exe`) when it has to register the logon task, but nothing sets it any more
  (the bundled launcher that used to is gone), so an operator never needs to.
- Session cookies are HttpOnly. Production/domain-scoped cookies are also Secure, and state-changing browser requests must come from the same host via `Origin` or `Referer`.
- Ciaobot sends baseline security headers from the Starlette app, including CSP, `X-Content-Type-Options`, `Referrer-Policy`, and frame denial.
- There is one origin and one session: `/api/*` needs the signed `ciao_session` cookie (minus a small public allowlist), `/ws/*` handshakes are checked for same-origin and then the session, and state-changing requests must present a matching `Origin`/`Referer`. The loopback-only local feed (legacy route name, no native client) and the update drain stay gated on the TCP peer. There is no native capability of any kind on a remote browser; see `docs/REMOTE_BOUNDARY.md` for the remaining protocol and credential work.
- Workspace HTML artifact previews use a stricter sandbox CSP: inline scripts/styles and `data:` images/fonts/audio/video are allowed, while network connections, `blob:` sources, and same-origin session access are blocked.

### Optional env vars

Linux hosts can run the Python backend and PWA under systemd. `ciao setup`
initializes their workspace without launchd; `ciao linux-service --workspace
/srv/ciaobot --user ciaobot --home /var/lib/ciaobot --python
/opt/ciaobot/venv/bin/python` prints a unit for explicit administrator installation.
The service reads `.env` through Ciaobot's dotenv loader, not systemd's different
EnvironmentFile syntax. Keep provider credentials in the service account's home.
Linux production Settings uses `/api/admin/restart` for a draining restart,
without a git checkout, package reinstall, or desktop build.
See [Linux hosting](docs/LINUX.md) for provisioning, HTTPS, updates, and recovery.

- `CLAUDE_EXECUTION_MODE` / `CLAUDE_PERMISSION_MODE`: **removed 2026-08-21 and no longer read.** The permission mode is now set per provider in Settings → Models & providers (Manual asks before every action, Auto lets safe reads and edits run silently and asks before destructive operations, Bypass allows everything); a provider with no pin uses Auto.
- `PWA_AUTH_REQUIRED`: **removed and no longer read.** Password protection is always on; `ciao setup --no-auth` is gone with it. An install that still sets it to `false` gets a `legacy-env-ignored` operator tile; the `true` that setup used to write is ignored silently. A workspace `.env` with no `PWA_AUTH_TOKEN` signs sessions with a generated `.runtime/session-secret` and is still protected: put a password in `PWA_AUTH_TOKEN` and restart, or sign in once with `ciao setup-url`.
- `PWA_HOST`, `CIAO_LOG_LEVEL`, `CIAO_DEV_MODE`, `CIAO_APP_REPO`: **moved to Settings and no longer read.** They are Settings → General → Network access (the bind address, default `0.0.0.0`; read at startup) and Settings → General → Developer (developer mode — the Debug card, `/api/debug/issues` and a source-checkout deploy from Settings → Restart; the source checkout path for that deploy; and the log level, read at startup, where `debug` also attaches a rotating `.runtime/server_debug.log` of 10 MB × 2 backups that the dev-mode `/api/debug/issues` report and the `{{ISSUE_REPORT}}` placeholder surface alongside the error tail). All four are stored in `.runtime/app_settings.json` and exposed on `/api/settings/routines`. On the first server start after upgrading, any of them still set in `.env` is imported into Settings once (an unusable host or log level is skipped with a warning); the `legacy_env_imported` flag in that file makes the import one-shot, so a value later changed in Settings stays changed. After that the variables are inert: the `legacy-env-ignored` operator tile asks you to remove them from `.env`.
- `CIAO_VAULT_MODE`: **no longer written or read.** Setup records the vault mode (`scratch` initializes the current vault layout; `existing` preserves the selected notes folder and starts an initial inventory/curation chat, where clear material may be reorganized and ambiguous material is left in place) on the second line of `.runtime/setup-completed-at`, which only the first-run onboarding chat reads. A value left in `.env` is ignored silently: setup wrote it, so it is not a setting the operator chose.
- `CIAO_BOOTSTRAP_WORKSPACE`: temp workspace root used when `PWA_AUTH_TOKEN` is absent. Defaults to `~/.ciao/bootstrap`; Ciaobot persists the generated bootstrap auth token under its `.runtime/` so first-run setup survives a restart.
- `CIAO_NO_BROWSER`: set to any value to stop a first-run `ciao run` from auto-opening the setup wizard in the default browser (the wizard URL is still printed). Auto-open already only happens on interactive terminals, never under launchd or CI.
- `CIAO_WORKSPACE`: set by the service definition (and by `ciao run` started inside a set-up workspace, which is found through its `.runtime/workspaces.json` or, for a pre-1.0 install not yet started on this release, a `.env` with a non-empty `CIAO_WORKSPACES`; setup no longer writes `CIAO_WORKSPACE=.` into `.env`). The filesystem workspace root for operational state, `.runtime/`, `.env`, canonical `skills/`, `subagents/`, and `commands/`, the generated `.claude/` catalog, and the active agent root's `AGENTS.md` (the install root on the shared layout, or a per-workspace root after re-rooting). Default `.`. `.agents/skills/` remains a legacy native-discovery path and is not generated by sync. Setting it inside that `.env` cannot move the install, because the engine finds `.env` inside the folder the service definition names; move it with Settings → Main workspace → Move… (on the computer running Ciaobot) or `ciao workspace-move <new folder> [--apply]`; on Linux the administrator runs it with `sudo` (docs/LINUX.md).
- `CIAO_VAULT_ROOT`: durable memory/vault root. Default `<CIAO_WORKSPACE>/memory-vault`. Set this to an external notes folder when operational state should stay out of synced notes.
- `CIAO_WORKSPACES`: **retired and no longer read as a workspace source.** It used to override `.runtime/workspaces.json` on every start, so a workspace created, edited or archived in Settings could silently revert on the next restart. The workspace list is now runtime state owned by the app: Settings → Workspaces writes `.runtime/workspaces.json`, and that file is the only source. On the first server start after upgrading, an install that still sets the variable has its workspaces imported into the registry once (every one when no registry file exists, otherwise only names the registry does not already define; existing entries are never overwritten, a workspace with an archive in `.archived-workspaces/` is skipped rather than re-registered, and an imported workspace without `allowed_mcp_servers` gets an empty allowlist, which is what it resolved to before). A `.runtime/workspaces-env-imported.json` marker makes that import one-shot, so a workspace later removed in Settings stays removed. After that the variable is inert: startup logs a warning and the `legacy-env-ignored` operator tile asks you to remove it from `.env`; Ciaobot does not edit `.env` for you.
- `.runtime/workspaces.json` (not an environment variable): the workspace registry, a JSON list of objects with `name`, `vault_root`, `default_provider`, `disallowed_tools`, `allowed_mcp_servers`, `gws_profile` and `color`. `vault_root` is relative to `CIAO_WORKSPACE` unless absolute. Fresh setup and ordinary PWA workspace creation derive `<CIAO_VAULT_ROOT>/<name>`, while existing-folder setup preserves the selected root until a model-guided migration updates the registry. Later Settings updates preserve the stored path. If the file is missing, Ciaobot bootstraps one registry entry per directory in the vault that looks like a workspace (a folder containing `People/`, `Projects/`, `journal/` or a `MEMORY.md`), falling back to a single `personal` workspace when none do. It used to manufacture `personal` and `work` unconditionally, which left an install unable to re-root: a registered workspace with no vault directory refuses the plan. Schedules assigned to a workspace inherit its current `default_provider` on every run unless an explicit override is stored; the model comes from that provider's operator default (Settings → Models) — a workspace no longer pins one, and a `default_model` key in the registry is ignored. Example: `[{"name":"default","vault_root":"memory-vault/default","default_provider":"claude","gws_profile":"personal"}]`.
- File viewer path policy: the file/binary/image viewers and the in-PWA editor have **no workspace sandbox**. They read (and, for the editor and snapshot-restore, write) any path on disk. Relative paths still anchor to the workspace root. The extension allowlist (no `.env`, no key files) and the size caps are the only remaining guards, so secrets in allowlisted files elsewhere on the machine are reachable from an authenticated PWA session.
- `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND`: optional override for `gws`; the server defaults it to `file` at startup for headless auth.
- `CIAO_INSIGHTS_DISABLED`: **retired and no longer read after one-time migration.** On the first upgraded server start, its value is copied into the persisted **Session insights** switch in Settings → General; an existing Settings value wins. Remove it from `.env` after that migration. The retired startup-backfill opt-in remains inert.

Session insights models are chosen per provider in Settings → Models & providers.
Automatic uses the source chat's model for archive memory, chat titles and
schedule attention. Choose a specific model there to override it. History
imports without a recorded source model use the workspace/provider default.
- `ciao gws-auth-helper <profile>`: interactive headless OAuth re-authentication when `gws auth login` cannot open a browser.
- `CLAUDE_DEFAULT_MODEL_PERSONAL` / `CLAUDE_DEFAULT_MODEL_WORK` / `CIAO_DISALLOWED_TOOLS_PERSONAL` / `CIAO_DISALLOWED_TOOLS_WORK`: **removed 2026-08-20 and no longer read.** They configured the two hardcoded `personal`/`work` entries of the bootstrap registry, which now derives its workspaces from the vault instead, so they could not describe a workspace named anything else. Put `disallowed_tools` on the workspace in `.runtime/workspaces.json`, which works for any name; the default model is now a per-provider operator setting (Settings → Models), not a per-workspace one.
- `CIAO_MEMORY_DIR`: legacy override for the old `~/.ciao/memory.md` + `user.md` directory during the one-release migration window. Default `~/.ciao`. Not used for new writes; safe to unset after migration.
- **Image-capability pre-flight**: before dispatch, a turn that carries images checks whether the selected model can see them. Anthropic's and OpenAI's current models all accept images, so only OpenCode is consulted — it is bring-your-own-provider, and each V2 catalog entry lists supported modalities in `capabilities.input`. An unstated answer counts as capable, so a cold catalog never blocks a turn. A non-vision model pauses the turn on a `model_capability_question` (30s window): the PWA offers the models OpenCode states accept images, an "Open picker" escape hatch, and Cancel. Switch re-dispatches on the picked model; cancel/timeout close the turn with a `status` bubble; the images are never silently dropped. Text-only and unattended (loop/schedule) turns skip the question. Implemented in `ciao/providers/opencode.py::model_accepts_images` and the pre-flight in `ciao/web/project_chats.py::ProjectChatManager.stream_chat`.
- **Web Push timing** (not configurable): a completed turn pushes 30 seconds after it ends. Rapid replies to the same chat cancel the previous timer and start a new one (coalesce into a single push). Permission requests and model questions push immediately (no delay). Unanswered permission requests re-fire every 30 seconds, up to 3 times, until the user approves/denies or the turn ends. Marking a chat read sends a separate clear control to all registered Web Push subscriptions and the macOS notification log.
- `CIAO_PYTHON`: path to a specific Python binary for `scripts/dev.sh` (e.g. when Homebrew breaks `ensurepip`).
- `CIAO_PATH`: baked into the launchd plist's `EnvironmentVariables` at setup time so developer-mode subprocesses (npm, node, git) are found despite launchd's minimal default PATH. Not an operator env var; it's a `com.ciao.server.plist.tmpl` placeholder rendered from the user's shell PATH.
- Installer and release-build variables: `CIAO_APP_DIR` overrides the per-user app directory, `CIAO_ARCHIVE_NAME` and `CIAO_VERIFIER_NAME` override release asset names, and `CIAO_RELEASE_BASE_URL` selects a private release mirror for an explicit `--version` (the embedded archive signature is still mandatory). `CIAO_BUNDLED_APP` marks the embedded runtime for internal mode detection. `CIAO_PYTHON_ARM64_URL`, `CIAO_PYTHON_ARM64_SHA256`, `CIAO_PYTHON_X86_64_URL`, and `CIAO_PYTHON_X86_64_SHA256` are retired: the release workflow used to require them when assembling the embedded runtimes, and nothing reads them now. `CIAO_EXECUTABLE` is a LaunchAgent template token, not an operator environment variable.
- `CIAO_ALLOW_LAUNCH_AGENT_REPOINT`: set to `1`/`true`/`yes` (case-insensitive) to allow `setup_workspace`/`_write_launchd_plist` and `ciao setup` to repoint the live `~/Library/LaunchAgents/com.ciao.server.plist` to a different workspace without `confirm_repoint=True`/`--yes`. For automation that intentionally moves the install; leave unset for the safe default (refuse). `CIAO_LAUNCH_AGENTS_DIR` (test-only) already isolates the suite by redirecting the plist location.
- `CIAO_EVAL_MAX_CALLS`: hard call ceiling for one `ciao eval run` (the bounded model-backed behavioral probe). Default `40`. The runner stops claiming new probes at the ceiling and records the remainder as `budget_exhausted` rather than continuing to spend. The deterministic `ciao eval contracts` half never calls a model and ignores it.
- `CIAO_EVAL_MAX_COST_USD`: hard estimated-cost ceiling (USD) for one `ciao eval run`. Default `2.0`. `run_oneshot` returns text rather than a cost, so cost is enforced as a declared per-call upper bound (`calls × CIAO_EVAL_COST_PER_CALL_USD`); the report records the estimate used.
- `CIAO_EVAL_COST_PER_CALL_USD`: the declared per-call upper-bound cost used to enforce `CIAO_EVAL_MAX_COST_USD`. Default `0.05`. Raise it when the evaluated model is materially more expensive per call so the ceiling stays honest.

**Note:** `ciao gws-auth-helper` is the helper for headless `gws` auth when the keyring backend fails.

### Injected CLI context variables

The Ciaobot server injects the following environment variables into every spawned agent CLI subprocess (`claude` or `opencode serve`):

- `CIAO_WORKSPACE`: the filesystem workspace root path. This is operator config forwarded for compatibility; it is not the logical chat workspace.
- `CIAO_ACTIVE_WORKSPACE`: the logical workspace name derived per turn from `chat -> project -> project.workspace`.
- `CIAO_ACTIVE_PROJECT`: the active project ID.
- `CIAO_MODEL`: the model ID configured for the chat.
- `CIAO_PROVIDER`: the provider name (`claude` or `opencode`).
- `CIAO_CHAT_ID`: the ID of the active chat.
- `GWS_PROFILE`: resolved from the active workspace's `gws_profile`, falling back to the `personal` profile.
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
- Start and stop: `ciao service start` / `ciao service stop` (or `launchctl unload ~/Library/LaunchAgents/com.ciao.server.plist`). Both refuse while chats are actively running unless `--force` is passed.
- Updates: re-run the one-line installer, or use **Settings → Home** in the PWA, which stages and applies the engine package update in the background. There is no in-app app updater.
- Remote access is not configured by the public app. Use localhost by default, or put Tailscale or another user-owned network layer in front of the local server.
- A dropped non-image chat file is converted locally: supported documents are
  converted from the drop's own source and only Markdown is kept in the active
  project; the composer never receives a filesystem path. Generic ProjectView
  uploads are unchanged.

### Server startup behaviors

Auto-skills update, auto-CLI update, and similar behaviors belong in server startup code (`ciao/main.py`), not in Claude Code's `settings.json` hooks.

Enabled schedules also receive one startup catch-up check. If the latest expected occurrence was missed while the server was unavailable, it runs immediately once; older skipped intervals are not replayed, and the scheduled prompt receives the current run context rather than a backdated occurrence date.
