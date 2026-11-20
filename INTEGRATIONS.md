# Integrations Setup

One-time setup for external tools and CLI dependencies.

The Chat details window displays project context and a link to its canonical document; this is a presentation of existing context, with no new integration, configuration, or change to what providers receive.

Claude's disable options reach the claude-agent-acp adapter as `_meta.claudeCode.options`; `ciao/providers/acp/agents.py` owns that mapping.

## Install

### Upgrading from the macOS app

v1.0.0 retires the macOS `Ciaobot.app` as a separate updater; the PWA is served
by the engine. The `--migrate` hand-over that moved an app-managed engine to the
terminal installer is no longer shipped, so an app user does not re-run an
installer to switch. Your workspace folder, its notes and chats are kept.

To remove the old bundle, quit Ciaobot.app and drag it to the Trash (it lives in
`~/Applications`, or `/Applications` for older installs). If
`~/Library/LaunchAgents/com.ciao.menubar.plist` is still there, unload it with
`launchctl bootout gui/$(id -u)/com.ciao.menubar` and delete the file. If
`~/.local/bin/ciao` points inside the bundle, re-run the installer to replace
it. Updates after that come from Ciaobot for Mac, which updates itself
(Sparkle) and the engine with it.

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
- Another `ciao` already exists (the name collides with Ciao Prolog, among
  others): the installer never overwrites it, so call Ciaobot's engine by its
  full path instead.

Provider logins do not depend on the shim: the setup wizard hands out each
provider's own login command (`claude auth login`, `opencode auth login`).

## Claude

Claude runs through `claude-agent-acp`, the Agent Client Protocol adapter for Claude Code. The adapter is pinned at `@agentclientprotocol/claude-agent-acp@0.108.1` in `ciao/adapter_install.py`, and it drives the `claude` CLI that Ciaobot resolves (`CLAUDE_CODE_EXECUTABLE`). Node 22 or newer is required.

Ciaobot installs the adapter itself, once, into a private folder outside the app (`~/Library/Application Support/Ciaobot/adapters` on macOS; the engine's state folder beside the install receipt on Linux). It uses the machine's own `npm` with `--omit=optional --ignore-scripts` and no shell, and it runs the adapter under the machine's own `node`. The native Claude package the adapter can pull in optionally (about 340 MB) is left out, because the user's own `claude` does the work. The install starts at engine startup only when `claude` is present and the adapter is missing or not the pin. A failed install is shown on the Settings card and is not retried by the engine; the card's Install button runs the same line in a terminal.

```bash
npm install --no-audit --no-fund --omit=optional --ignore-scripts --prefix "<adapters folder>" @agentclientprotocol/claude-agent-acp@0.108.1
claude auth login                   # the Settings sign-in action runs this in a terminal
```

The adapter reads the `claude` executable from `CLAUDE_CODE_EXECUTABLE`, so with that variable set it passes MCP servers to the CLI as an argument (`--mcp-config`) rather than in the SDK's initialize request. An MCP server's `env` and `headers` therefore appear on the adapter's command line, visible to other users on the machine through the process list. Ciaobot does not write them to a file.

Sign-in is handled by the Claude CLI. Ciaobot never reads or stores a Claude credential.

## Codex

Codex runs through `codex-acp`, the Agent Client Protocol adapter for OpenAI's Codex CLI. The adapter is pinned at `@agentclientprotocol/codex-acp@2.7.0` in `ciao/adapter_install.py`, installed the same way as the Claude adapter (see above). It does not install `@openai/codex` itself: Ciaobot runs the user's own `codex` by setting `CODEX_PATH`. When no `codex` resolves, the Settings install line also installs the CLI globally, at the version the adapter is built against (`@openai/codex@0.166.0`). Node 22 or newer is required.

```bash
npm install -g @openai/codex@0.166.0                  # only when no codex resolves
npm install --no-audit --no-fund --omit=optional --ignore-scripts --prefix "<adapters folder>" @agentclientprotocol/codex-acp@2.7.0
codex login                         # the Settings sign-in action runs this in a terminal
```

Sign-in is handled entirely by the Codex CLI. Ciaobot does not implement, prompt for, or store any Codex credential, and it has no API-key field. Settings only checks `codex login status`, which reads the exit code and nothing else, so a token that command prints never reaches the PWA or the log. The adapter starts only when the codex CLI and node resolve; the `codex` it runs is the one on the terminal PATH, which the adapter receives in `CODEX_PATH`.

## OpenCode

Ciaobot requires **OpenCode 2.3.2 or newer in the 2.x line**. OpenCode 1 is
not supported. The 2.3.2 floor is the release that ships `opencode acp` child
session updates and the background-shell fix.

```bash
npm install -g @opencode/cli
# or: brew install anomalyco/tap/opencode-v2
# or: curl -fsSL https://opencode.ai/v2/install | bash
ciao auth opencode                  # opens `opencode auth login`
```

The npm package `opencode-ai` and the `sst/tap` Homebrew tap are OpenCode 1 and do not work with Ciaobot, and `opencode upgrade` does not move a 1.x install to 2.x. If you installed OpenCode 1 that way, run `npm uninstall -g opencode-ai` first, because it also provides an `opencode` command.

OpenCode is bring-your-own-provider: it authenticates against whichever model
backends you connect, and Ciaobot lists the models its ACP session reports.
Ciaobot runs one `opencode acp` process per active chat over stdio, the Agent
Client Protocol. Chat output, permission cards and subagent activity arrive as
ACP session updates.

Workspace assets need almost no projection: OpenCode discovers
`.claude/skills/`, `.agents/skills/`, and `AGENTS.md` natively, so
`ciao sync-skills` only generates `.opencode/agents/` (canonical subagents) and
`.opencode/commands/` (canonical commands). MCP servers are owned by OpenCode;
Ciaobot reads provider configuration and live chat status without editing
`opencode.json` or launching a server from Settings. Keep existing MCP entries
in the provider configuration. See the [OpenCode V2 MCP documentation](https://opencode.ai/v2/docs/mcp-servers/).

Ciaobot sends prompts over ACP with queued delivery (one prompt at a time per
session). The unified core context rides in the text of the first prompt of each
session, after a compaction and after a handover. Fork, interrupt, tool approvals, and background
subagents are native. Forms (AskUserQuestion cards) are not wired under ACP: the ACP provider has no
`respond_question` override and emits no form events, so a form reply gets the base refusal
"Native question responses are not supported by this provider" and an ACP session shows no form card. When a chat is
archived, deleted, or reset, Ciaobot closes its ACP session and asks the agent to
delete the persisted session (`session/delete`) when the agent advertises it;
cleanup is fail-open if the agent is unavailable.

### Auto mode approvals

OpenCode chats follow the chat's permission mode (the per-provider default
from Settings → Providers, Auto unless changed). The V2 session permission
ruleset is a mode wildcard, last-match-wins. Plan asks for every action and
then allows `read`, `glob` and `grep`. Normal (Manual) asks for every action.
Auto allows routine tools and routes every `shell` action to `ask`. Bypass
allows every action. `glob` and `grep` follow those wildcards: allowed in
plan and auto, ask in normal. There are no credential-path denies. File tools
can read the workspace `.env` and `.runtime`. Bypass and shell were never
covered by a path rule: a shell command such as `cat .env` still raises an
approval card in normal and auto, and in bypass it runs without one. Each
`ask` surfaces an approval card
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
# npm 12 blocks install scripts by default and the postinstall is what downloads
# the gws binary, so allow it for this package:
npm install -g --allow-scripts=@googleworkspace/cli @googleworkspace/cli
# Authenticate (two profiles):
GOOGLE_WORKSPACE_CLI_CONFIG_DIR=~/.config/gws-personal gws auth login   # personal
gws auth login                                                           # work
```

Use `ciao gws <profile> <gws-args>` to switch between accounts (`$GWS_PROFILE` holds the chat's own account). Never source the wrapper: it ends with `exec`. (`scripts/gws-profile.sh` remains as a thin back-compat shim that forwards to `ciao gws`.) The retired top-level `GWS_PROFILE` setting is no longer an operator default: on the first upgraded server start, a value naming an account this install knows is copied once into every workspace whose stored `gws_profile` is blank. Explicit workspace links are preserved, and later blank links are not backfilled; remove `GWS_PROFILE` from `.env` after the migration marker appears in `.runtime/`.

**PWA-native OAuth (no terminal required).** The Settings → Overview → Google account row (Connect Google account opens a dialog) can upload a GCP `client_secret.json` and drive the full OAuth code-exchange from the browser. The server generates the Google authorization URL, opens it in a new tab, and exchanges the returned authorization code for a refresh token. Accounts are created per workspace from that dialog and named from the workspace (there are no built-in ones); credentials are written to `<workspace>/secrets/gws-<account>/`, except the two pre-registry names, which keep `<workspace>/secrets/gws-personal/` (personal) and `<workspace>/secrets/gws/` (work). Scopes granted: every account requests the same set — Gmail, Calendar, Drive, Sheets, Docs, Slides, Tasks, Contacts, Forms, plus `openid` and the user's email/profile (`ciao/gws_auth.py`). Disconnect and remove in the row's ··· menu deletes the account's stored credentials and OAuth client and unlinks the workspace. Note: these paths (`<workspace>/secrets/gws-*/`) are separate from `~/.config/gws-*/`, which the `gws` CLI uses by default when invoked directly via `ciao gws`. An account that no workspace links to is not shown in Settings. It stays under `<workspace>/secrets/gws-<name>/` and in `.runtime/gws_profiles.json`, and `ciao gws <name> ...` still works with it (the CLI does not list accounts). Opening Connect on a workspace whose name matches such an account reuses it; to delete one by hand, remove its `secrets/gws-<name>/` folder and its entry in `.runtime/gws_profiles.json`. The health monitor keeps checking every account with credentials, but only an account that at least one active workspace links raises a login warning; an unlinked account (including one linked only by an archived workspace) never does.

**Getting `client_secret.json`.** In [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials): create or pick a project, enable the APIs you need (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, People for Contacts, Forms), then create an OAuth 2.0 client ID of type **Desktop app**. The one-click Google sign-in requires a Desktop app client; Web application clients are not supported for that flow. Download the JSON credentials file and upload it in Settings → Overview → Google account → Connect Google account. The Connect dialog repeats these steps for end users. Stock `gws-*` skills ship with the app once `gws` is installed and authenticated.

**Auth + scope gotchas.** Use `gws auth login --full` for complete scopes. `gws auth login --services calendar` has produced tokens that still lack the calendar scope. `ciao gws` already execs `gws`, so `ciao gws personal gws calendar ...` doubles the command and fails with `Unknown service 'gws'`; pass the subcommand directly: `ciao gws personal calendar ...`. If auth fails despite a fresh login, `credentials.enc` (AES-256-GCM) may hold the valid refresh token while `credentials.json` carries a stale one. The work gcloud account only has `openid`/`cloud-platform` scopes and returns 403 on Drive uploads, so do not substitute `gcloud auth print-access-token` for `gws` auth in Drive flows.

**Headless re-auth.** When `gws auth login` fails on a headless server, run `ciao gws-auth-helper <profile>`. It prints the auth URL, waits for the redirect URL to be pasted back, and saves fresh credentials.

**Token expiry.** Symptom: `"token_error": "Token has been expired or revoked."`. Fix: `ciao gws <profile> auth login` (or the headless helper above). If `GOOGLE_WORKSPACE_CLI_CLIENT_ID` is set in `.env`, it can override the OAuth client to a wrong project; comment it out, or run `env -u GOOGLE_WORKSPACE_CLI_CLIENT_ID gws auth login --profile <profile>`.

**Token-health monitoring (automatic).** A background loop checks each configured profile's token validity every 900 seconds. On a transition to invalid it sends one PWA notification (only linked profiles raise the notification) and publishes an in-app `gws_health` status event naming the profile, so a revoked login surfaces instead of GWS-dependent schedules failing silently. The alert is debounced (one notification per breakage) and re-arms after the token recovers. The result also appears per profile in `GET /api/integrations/gws` as `token_valid` / `token_error` / `needs_relogin`, which Settings → Overview → Google account renders as Sign-in expired.

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

The Settings → General → Keyboard shortcuts card lets you disable or rebind the
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

These MCP servers reach the session through the claude.ai connector bridge. They are enabled per-workspace on claude.ai; no local install required. Availability is session-scoped: new connectors only surface after a fresh Claude Code session. Claude context trimming's **claude.ai connectors** option (Settings → Overview, trim key `setting:claude_ai_connectors`, default on) turns them off for that workspace's Claude chats by setting `ENABLE_CLAUDEAI_MCP_SERVERS=false` on the spawned Claude Code process. A registry row that still has `claude_ai_connectors: false` and no trim entry for that key is read once as trimmed; the next save drops the old field. Memory passes always run with them off.

| Connector | Scope | Used by |
|---|---|---|
| Airtable | read/write | `airtable-opportunities`, `airtable-feedback`, `airtable-projects`, work daily, weekly review |
| Atlassian (Jira + Confluence) | read/write | `jira-tickets`, work daily, sprint review |
| Slack | read/write | work daily (authored Slack messages), content sourcing |
| Zoom for Claude | read-only on meetings (summaries, transcripts, recordings, My Notes, Zoom Docs); write for new Zoom Docs | memory curation (meeting ingestion), work daily (ZOOM subagent), ad-hoc meeting recall |

**Zoom capability map:** `search_meetings`, `recordings_list`, `get_meeting_assets` (AI summary + transcript + participants), `get_recording_resource`, `get_file_content`, `search_zoom` (chat + Zoom Docs), `create_new_file_with_markdown` (Zoom Docs only). No meeting creation; scheduling remains via Google Calendar.

If a connector's tools don't show up, the fix is on the claude.ai side: toggle the connector for the workspace, then start a new session.

### Provider-owned MCP servers

Claude Code and OpenCode own their MCP configuration, on/off state and authentication. Ciaobot keeps no MCP inventory or status and never reads or writes that configuration or any credential. Settings → MCP shows one button per installed agent that types the agent's own MCP command (`claude mcp`, `opencode mcp`) into the in-app terminal for the workspace; Chat details has the same button for the chat's agent. Sign in, add, remove and turn servers on or off in the CLI. A change applies to new chats; a running chat loads it after `ciao chat reload`.

To add a Claude server to just this workspace, run Claude Code from that workspace root and use `claude mcp add --scope local <name> -- <command> [args...]` for stdio, or `claude mcp add --scope local --transport http <name> <url>` for HTTP. Authenticate with `/mcp` in Claude Code or `claude mcp login <name>`. The standalone `/mcp` in a Ciaobot chat instead opens Chat details → MCP for that chat's provider and workspace; it is not a provider CLI command. Claude's `local` scope is user-specific to the current project; `project` scope writes `.mcp.json` and requires project approval. Every server declared in the workspace's own `.mcp.json` or local scope loads in its Claude chats; to block one, add `mcp__<name>` to the workspace's `disallowed_tools`. See [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).

To add an OpenCode server, run `opencode mcp add <name> --url <url>` from the workspace for a remote server, or `opencode mcp add <name> -- <command> [args...]` for a local stdio server. Authenticate with `opencode mcp auth <name>`; use `/mcps` in OpenCode to inspect or connect servers. Project scope is the default; `--global` applies to all projects. See [OpenCode V2 MCP documentation](https://opencode.ai/v2/docs/mcp-servers/).

Keep secrets in provider-supported credential stores or environment variables referenced by provider configuration. Do not put bearer tokens directly in `.mcp.json` or `opencode.json`.

The one-line macOS installer preserves a configured workspace discovered from
the current LaunchAgent. On a clean installation it starts the packaged
engine without creating a workspace or random dashboard password; the PWA then
presents bootstrap onboarding, which asks the user to create or adopt the
workspace and choose the password.

## Environment Variables

### Anthropic credentials for Claude chats

The CLIs and ACP adapters inherit the engine's process environment, so provider credentials set there (for example `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) are handled by the CLI itself; only Ciaobot's own `CIAO_*` internals and the PWA token are withheld. The in-app terminal filters its environment on purpose differently: it strips secrets (`*_TOKEN`, `*_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, and similar), while agent processes inherit the engine environment minus `CIAO_*` and `PWA_AUTH_TOKEN`.

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

- The CLI-first agent surface is the only control path and is always on; there is no enable/disable switch and no alternative surface. The former embedded MCP endpoint was removed in the S6 break. External MCP servers are owned and configured by Claude Code or OpenCode; Settings → MCP reads inventory and live status without starting sessions; its explicit on/off controls write provider configuration.
- `CIAO_AGENT_TOKEN` / `CIAO_AGENT_URL`: internal. Ciaobot hands the scoped short-lived token and the `POST /agent/v1/{op}` base URL to the managed provider's foreground shell so `ciao <noun> <verb>` commands can call the control plane (D-01). Background runs strip the token. Operators must not configure or persist either.

The endpoint is mounted at `http://127.0.0.1:<PWA_PORT>/agent/v1/{op}`. Ciaobot generates a scoped short-lived token and configures its managed provider process. See [docs/AGENT_CLI.md](docs/AGENT_CLI.md).

The agent control plane runs inside `ciao/mcp_server.py` (the shared operation table, bearer-token registry, and the envelope/plan-mode gate/telemetry) served by the `POST /agent/v1/{op}` route in `ciao/web/routes_agent.py`; it is not an MCP server, so the MCP SDK pin and the `2026-07-28` spec wire-format notes no longer apply.

**Internal command markers:** `CIAO_COMMAND_BEGIN`, `CIAO_COMMAND_INSTRUCTIONS`, and `CIAO_COMMAND_END` are reserved transcript markers used when Ciaobot expands a Claude-style slash command for a managed provider. They are not environment variables and should not be configured.

**Provider-owned credentials:** Claude Code and opencode credentials are owned by their CLIs (`ciao auth <provider>` or Settings → Providers). Ciaobot reads no provider API key and exposes no API-key editor.

Workspace-specific integrations can still be set in `.env`, but the public `.env.example` does not ship private/work examples. Use user-owned credentials for each integration:

**GWS:** `GOOGLE_WORKSPACE_CLI_CLIENT_ID`, `GOOGLE_WORKSPACE_CLI_CLIENT_SECRET`

**Airtable:** `AIRTABLE_API_KEY` (get from https://airtable.com/create/tokens, scopes: data.records:read/write, schema.bases:read)

**Zendesk:** `ZENDESK_EMAIL`, `ZENDESK_API_TOKEN` (Admin Center > APIs > Zendesk API), `ZENDESK_SUBDOMAIN`

**BigQuery:** `GOOGLE_APPLICATION_CREDENTIALS`

**OpenAI:** Ciaobot does not use an OpenAI API key. OpenAI-compatible models are reached through opencode.

**MCP credentials:** MCP credentials belong to the provider that owns each connection. Configure them through the provider's supported environment or credential-store mechanism; Ciaobot Settings does not read or write MCP secret values. Claude workspace reachability is scoped by where a server is declared (each workspace's agent root) and narrowed by `mcp__<name>` entries in `disallowed_tools`.

**Provider connections.** The chat providers card in Settings → Providers launches, verifies, and logs out Claude Code and opencode through
their own CLIs; Ciaobot stores none of their credentials, and there are no
API-key fields to fill in.

**Reaching any other model.** Ciaobot talks to exactly two providers: Claude
Code (Anthropic) and opencode (everything else). opencode is
bring-your-own-provider, so Ollama, OpenRouter, LM Studio, or any
OpenAI-compatible endpoint is configured *in opencode* — see its own docs — and
Ciaobot picks it up automatically: `GET /api/models` lists whatever opencode
reports as connected, so its models appear in the chat picker, workspace
defaults, title generation, insights, and schedules with no Ciaobot
configuration at all.

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
  it and Settings → Access & devices → PWA password changes it. There is one engine per install, so
  this is the only password that install has: a second device runs its own engine
  and its own token.
- `PWA_PORT` (default `8443`). The bind address is no longer an env var: the
  server binds all interfaces by default so the PWA is reachable over LAN and
  Tailscale, and Settings → Access & devices → Network access → "This computer only"
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
- `CIAO_GWS_BIN` (test isolation only): absolute path to an executable that `ciao gws` and the Google token health check run instead of `gws`. Set by `ciao eval tasks` for tasks requiring `gws_fake`, so evals never reach Google. When set it is used as-is, with no lookup and no fallback. It is read from the process environment only, unlike `CIAO_OPENCODE_BIN`, which is read from the workspace configuration. A value that is not an absolute path is an error: `ciao gws` exits 2 with `CIAO_GWS_BIN must be an absolute path`, and the token health check reports the token as invalid with that message. Whitespace-only counts as unset. POSIX only.
- `CIAO_ENGINE_PATH` (internal): legacy override for the engine executable the
  CLI runs. It is still read by `ciao/cli.py`, but nothing sets it any more
  (the bundled launcher that used to is gone), so an operator never needs to.
- Session cookies are HttpOnly. Production/domain-scoped cookies are also Secure, and state-changing browser requests must come from the same origin (host, scheme and effective port) via `Origin` or `Referer`, or from a host a proxy declared in `X-Forwarded-Host`.
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

- `CLAUDE_EXECUTION_MODE` / `CLAUDE_PERMISSION_MODE`: **removed 2026-08-21 and no longer read.** The permission mode is now set per provider in Settings → Providers (Manual asks before every action, Auto lets safe reads and edits run silently and asks before destructive operations, Bypass allows everything); a provider with no pin uses Auto.
- `PWA_AUTH_REQUIRED`: **removed and no longer read.** Password protection is always on; `ciao setup --no-auth` is gone with it. An install that still sets it to `false` gets a `legacy-env-ignored` operator tile; the `true` that setup used to write is ignored silently. A workspace `.env` with no `PWA_AUTH_TOKEN` signs sessions with a generated `.runtime/session-secret` and is still protected: put a password in `PWA_AUTH_TOKEN` and restart, or sign in once with `ciao setup-url`.
- `PWA_HOST`, `CIAO_LOG_LEVEL`, `CIAO_DEV_MODE`, `CIAO_APP_REPO`: **moved to Settings and no longer read.** They are Settings → Access & devices → Network access (the bind address, default `0.0.0.0`; read at startup) and Settings → Advanced → Developer (developer mode — the Debug card, `/api/debug/issues` and a source-checkout deploy from Settings → Restart; the source checkout path for that deploy; and the log level, read at startup, where `debug` also attaches a rotating `.runtime/server_debug.log` of 10 MB × 2 backups that the dev-mode `/api/debug/issues` report and the `{{ISSUE_REPORT}}` placeholder surface alongside the error tail). All four are stored in `.runtime/app_settings.json` and exposed on `/api/settings/routines`. On the first server start after upgrading, any of them still set in `.env` is imported into Settings once (an unusable host or log level is skipped with a warning); the `legacy_env_imported` flag in that file makes the import one-shot, so a value later changed in Settings stays changed. After that the variables are inert: the `legacy-env-ignored` operator tile asks you to remove them from `.env`.
- `CIAO_VAULT_MODE`: **no longer written or read.** Setup records the vault mode (`scratch` initializes the current vault layout; `existing` preserves the selected notes folder and starts an initial inventory/curation chat, where clear material may be reorganized and ambiguous material is left in place) on the second line of `.runtime/setup-completed-at`, which only the first-run onboarding chat reads. A value left in `.env` is ignored silently: setup wrote it, so it is not a setting the operator chose.
- `CIAO_BOOTSTRAP_WORKSPACE`: temp workspace root used when `PWA_AUTH_TOKEN` is absent. Defaults to `~/.ciao/bootstrap`; Ciaobot persists the generated bootstrap auth token under its `.runtime/` so first-run setup survives a restart.
- `CIAO_NO_BROWSER`: set to any value to stop a first-run `ciao run` from auto-opening the setup wizard in the default browser (the wizard URL is still printed). Auto-open already only happens on interactive terminals, never under launchd or CI.
- `CIAO_WORKSPACE`: set by the service definition (and by `ciao run` started inside a set-up workspace, which is found through its `.runtime/workspaces.json` or, for a pre-1.0 install not yet started on this release, a `.env` with a non-empty `CIAO_WORKSPACES`; setup no longer writes `CIAO_WORKSPACE=.` into `.env`). The filesystem workspace root for operational state, `.runtime/`, `.env`, canonical `skills/`, `subagents/`, and `commands/`, the generated `.claude/` catalog, and the active agent root's `AGENTS.md` (the install root on the shared layout, or a per-workspace root after re-rooting). Default `.`. `.agents/skills/` remains a legacy native-discovery path and is not generated by sync. Setting it inside that `.env` cannot move the install, because the engine finds `.env` inside the folder the service definition names; move it with Settings → Advanced → Ciaobot folder → Move… (on the computer running Ciaobot) or `ciao workspace-move <new folder> [--apply]`; on Linux the administrator runs it with `sudo` (docs/LINUX.md).
- `CIAO_VAULT_ROOT`: durable memory/vault root. Default `<CIAO_WORKSPACE>/memory-vault`. Set this to an external notes folder when operational state should stay out of synced notes.
- `CIAO_VAULT_DIR`: legacy vault-directory key that the Mac takeover plan still recognises in an old service definition; do not inherit it when starting a genuinely fresh install.
- `CIAO_WORKSPACES`: **retired and no longer read as a workspace source.** It used to override `.runtime/workspaces.json` on every start, so a workspace created, edited or archived in Settings could silently revert on the next restart. The workspace list is now runtime state owned by the app: Settings → Overview → All workspaces writes `.runtime/workspaces.json`, and that file is the only source. On the first server start after upgrading, an install that still sets the variable has its workspaces imported into the registry once (every one when no registry file exists, otherwise only names the registry does not already define; existing entries are never overwritten, a workspace with an archive in `.archived-workspaces/` is skipped rather than re-registered). A `.runtime/workspaces-env-imported.json` marker makes that import one-shot, so a workspace later removed in Settings stays removed. After that the variable is inert: startup logs a warning and the `legacy-env-ignored` operator tile asks you to remove it from `.env`; Ciaobot does not edit `.env` for you.
- `.runtime/workspaces.json` (not an environment variable): the workspace registry, a JSON list of objects with `name`, `vault_root`, `default_provider`, `disallowed_tools`, `claude_trim` (sparse map of trim-option key → bool, see `ciao/claude_trim.py`; `setting:claude_ai_connectors` hides the Claude account's connectors), `gws_profile` and `color`. `vault_root` is relative to `CIAO_WORKSPACE` unless absolute. Fresh setup and ordinary PWA workspace creation derive `<CIAO_VAULT_ROOT>/<name>`, while existing-folder setup preserves the selected root until a model-guided migration updates the registry. Later Settings updates preserve the stored path. If the file is missing, Ciaobot bootstraps one registry entry per directory in the vault that looks like a workspace (a folder containing `People/`, `Projects/`, `journal/` or a `MEMORY.md`), falling back to a single `personal` workspace when none do. It used to manufacture `personal` and `work` unconditionally, which left an install unable to re-root: a registered workspace with no vault directory refuses the plan. Schedules assigned to a workspace inherit its current `default_provider` on every run unless an explicit override is stored; the model comes from that provider's operator default (Settings → Providers) — a workspace no longer pins one, and a `default_model` key in the registry is ignored. Example: `[{"name":"default","vault_root":"memory-vault/default","default_provider":"claude","gws_profile":"personal"}]`.
- File viewer path policy: the file/binary/image viewers and the in-PWA editor have **no workspace sandbox**. They read (and, for the editor and snapshot-restore, write) any path on disk. Relative paths still anchor to the workspace root. The extension allowlist (no `.env`, no key files) and the size caps are the only remaining guards, so secrets in allowlisted files elsewhere on the machine are reachable from an authenticated PWA session.
- `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND`: optional override for `gws`; the server defaults it to `file` at startup for headless auth.

Session insights models are chosen per provider in Settings → Providers.
Automatic uses the source chat's model for archive memory, chat titles and
schedule attention. Choose a specific model there to override it. History
imports without a recorded source model use the workspace/provider default.

Chat file links use the chat's agent root for relative-path resolution. A
resolved absolute identity is shared by previews and pins; this is anchoring,
not a new filesystem sandbox. Existing viewer extension and size guards remain.
- `ciao gws-auth-helper <profile>`: interactive headless OAuth re-authentication when `gws auth login` cannot open a browser.
- `CLAUDE_DEFAULT_MODEL_PERSONAL` / `CLAUDE_DEFAULT_MODEL_WORK` / `CIAO_DISALLOWED_TOOLS_PERSONAL` / `CIAO_DISALLOWED_TOOLS_WORK`: **removed 2026-08-20 and no longer read.** They configured the two hardcoded `personal`/`work` entries of the bootstrap registry, which now derives its workspaces from the vault instead, so they could not describe a workspace named anything else. Put `disallowed_tools` on the workspace in `.runtime/workspaces.json`, which works for any name; the default model is now a per-provider operator setting (Settings → Providers), not a per-workspace one.
- `CIAO_MEMORY_DIR`: legacy override for the old `~/.ciao/memory.md` + `user.md` directory during the one-release migration window. Default `~/.ciao`. Not used for new writes; safe to unset after migration.
- **Image-capability pre-flight**: before dispatch, a turn that carries images checks whether the chat's agent accepts images. Gating is agent-level under ACP: `promptCapabilities.image` from the agent's `initialize` response. Only an explicit `false` refuses, so an agent that has not stated it, or has no connection yet, never blocks a turn. A non-vision model pauses the turn on a `model_capability_question` (30s window): the PWA offers the candidates `_capability_candidates` returns, an "Open picker" escape hatch, and Cancel. Switch re-dispatches on the picked model; cancel/timeout close the turn with a `status` bubble; the images are never silently dropped. Text-only and unattended (loop/schedule) turns skip the question. Implemented in `ciao/providers/acp/provider.py::agent_accepts_images`, `ProjectChatManager._agent_accepts_images`, and the pre-flight in `ciao/web/chat_streaming.py`.
- **Web Push timing** (not configurable): a completed turn pushes 30 seconds after it ends. Rapid replies to the same chat cancel the previous timer and start a new one (coalesce into a single push). Permission requests and model questions push immediately (no delay). Unanswered permission requests re-fire every 30 seconds, up to 3 times, until the user approves/denies or the turn ends. Marking a chat read sends a separate clear control to all registered Web Push subscriptions and the macOS notification log.
- `CIAO_PYTHON`: path to a specific Python binary for `scripts/dev.sh` (e.g. when Homebrew breaks `ensurepip`).
- `CIAO_PATH`: baked into the launchd plist's `EnvironmentVariables` at setup time so developer-mode subprocesses (npm, node, git) are found despite launchd's minimal default PATH. Not an operator env var; it's a `com.ciao.server.plist.tmpl` placeholder rendered from the user's shell PATH.
- Installer and release-build variables: `CIAO_ARCHIVE_NAME` and `CIAO_VERIFIER_NAME` override release asset names, and `CIAO_RELEASE_BASE_URL` selects a private release mirror for an explicit `--version` (the embedded archive signature is still mandatory). `CIAO_PYTHON_ARM64_URL`, `CIAO_PYTHON_ARM64_SHA256`, `CIAO_PYTHON_X86_64_URL`, and `CIAO_PYTHON_X86_64_SHA256` are retired: the release workflow used to require them when assembling the embedded runtimes, and nothing reads them now. `CIAO_EXECUTABLE` is a LaunchAgent template token, not an operator environment variable.
- `CIAO_ALLOW_LAUNCH_AGENT_REPOINT`: set to `1`/`true`/`yes` (case-insensitive) to allow `setup_workspace`/`_write_launchd_plist` and `ciao setup` to repoint the live `~/Library/LaunchAgents/com.ciao.server.plist` to a different workspace without `confirm_repoint=True`/`--yes`. For automation that intentionally moves the install; leave unset for the safe default (refuse). `CIAO_LAUNCH_AGENTS_DIR` (test-only) already isolates the suite by redirecting the plist location.
- `CIAO_EVAL_MAX_CALLS`: hard call ceiling for one `ciao eval run` (the bounded model-backed behavioral probe). Default `40`. The runner stops claiming new probes at the ceiling and records the remainder as `budget_exhausted` rather than continuing to spend. The deterministic `ciao eval contracts` half never calls a model and ignores it.
- `CIAO_EVAL_MAX_COST_USD`: hard estimated-cost ceiling (USD) for one `ciao eval run`. Default `2.0`. `run_oneshot` returns text rather than a cost, so cost is enforced as a declared per-call upper bound (`calls × CIAO_EVAL_COST_PER_CALL_USD`); the report records the estimate used.
- `CIAO_EVAL_COST_PER_CALL_USD`: the declared per-call upper-bound cost used to enforce `CIAO_EVAL_MAX_COST_USD`. Default `0.05`. Raise it when the evaluated model is materially more expensive per call so the ceiling stays honest.

**Note:** `ciao gws-auth-helper` is the helper for headless `gws` auth when the keyring backend fails.

### Injected CLI context variables

The Ciaobot server injects the following environment variables into every spawned agent CLI subprocess (`claude`, `codex` or `opencode acp`):

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
- Updates: macOS: the app updates itself (Sparkle). Linux: see docs/LINUX.md.
- Remote access is not configured by the public app. Use localhost by default, or put Tailscale or another user-owned network layer in front of the local server.
- A dropped non-image chat file is converted locally: supported documents are
  converted from the drop's own source and only Markdown is kept in the active
  project; the composer never receives a filesystem path. Generic ProjectView
  uploads are unchanged.

### Server startup behaviors

Auto-skills update, auto-CLI update, and similar behaviors belong in server startup code (`ciao/main.py`), not in Claude Code's `settings.json` hooks.

Enabled schedules also receive one startup catch-up check. If the latest expected occurrence was missed while the server was unavailable, it runs immediately once; older skipped intervals are not replayed, and the scheduled prompt receives the current run context rather than a backdated occurrence date.
