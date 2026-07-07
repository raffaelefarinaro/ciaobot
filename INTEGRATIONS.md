# Integrations Setup

One-time setup for external tools and CLI dependencies.

SDK-level wiring notes (fallback_model, hooks, setting_sources) live in the module docstring of `ciao/providers/claude.py`.

## CLI Tools

### `gws`: Google Workspace CLI

Required by `gws-*` and `recipe-*` skills (Gmail, Drive, Docs, Sheets, Slides, Calendar, Tasks, Forms).

```bash
# Install: https://github.com/googleworkspace/cli
# Authenticate (two profiles):
GOOGLE_WORKSPACE_CLI_CONFIG_DIR=~/.config/gws-personal gws auth login   # personal
gws auth login                                                           # work
```

Use `scripts/gws-profile.sh <personal|work> <gws-args>` to switch between accounts. Never source the wrapper: it ends with `exec`.

**PWA-native OAuth (no terminal required).** The Settings → Home → Integrations panel can upload a GCP `client_secret.json` and drive the full OAuth code-exchange from the browser. The server generates the Google authorization URL, opens it in a new tab, and exchanges the returned authorization code for a refresh token. Credentials are written to `<workspace>/secrets/gws-personal/` (personal) or `<workspace>/secrets/gws/` (work). Scopes granted: personal = Gmail + Calendar + Tasks; work also adds Drive, Docs, Sheets, and Slides. Use `Disconnect` to delete the stored credential files from the same panel. Note: these paths (`<workspace>/secrets/gws-*/`) are separate from `~/.config/gws-*/`, which the `gws` CLI uses by default when invoked via `scripts/gws-profile.sh`.

**Auth + scope gotchas.** Use `gws auth login --full` for complete scopes. `gws auth login --services calendar` has produced tokens that still lack the calendar scope. The wrapper already execs `gws`, so `scripts/gws-profile.sh personal gws calendar ...` doubles the command and fails with `Unknown service 'gws'`; pass the subcommand directly: `scripts/gws-profile.sh personal calendar ...`. If auth fails despite a fresh login, `credentials.enc` (AES-256-GCM) may hold the valid refresh token while `credentials.json` carries a stale one. The work gcloud account only has `openid`/`cloud-platform` scopes and returns 403 on Drive uploads, so do not substitute `gcloud auth print-access-token` for `gws` auth in Drive flows.

**Headless re-auth.** When `gws auth login` fails on a headless server, run `python3 scripts/gws-auth-helper.py <personal|work>`. It prints the auth URL, waits for the redirect URL to be pasted back, and saves fresh credentials.

**Token expiry.** Symptom: `"token_error": "Token has been expired or revoked."`. Fix: `scripts/gws-profile.sh <profile> auth login` (or the headless helper above). If `GOOGLE_WORKSPACE_CLI_CLIENT_ID` is set in `.env`, it can override the OAuth client to a wrong project; comment it out, or run `env -u GOOGLE_WORKSPACE_CLI_CLIENT_ID gws auth login --profile <profile>`.

**Credentials persistence.** GWS configs (`secrets/gws/` and `secrets/gws-personal/`) are gitignored and can be lost during git cleans or device migrations. Public installs should treat those directories as local secrets and back them up with an external secret manager if needed.


**Output parsing.** Strip the leading `Using keyring backend: file` banner from `gws` stdout before passing it to `jq`.

**PWA-only auth recovery.** When a token expires while the user is on the PWA with no shell, use the localhost-callback-relay flow: start the auth listener in the background, capture the sign-in URL, have the user open it on their phone, paste the redirect URL back, then `curl` it to the localhost listener.

### `notebooklm`: Google NotebookLM CLI

```bash
pip install notebooklm-py
notebooklm login   # browser-based, saves to ~/.notebooklm/storage_state.json
cp ~/.notebooklm/storage_state.json .notebooklm-auth.json
```

### `opencli`: Website CLI

CLI with 50+ website adapters (YouTube, LinkedIn, GitHub, etc.).

```bash
npm install -g @jackwener/opencli
opencli list   # see available adapters
```

### `apfel`: Apple Intelligence CLI

Provides local-first chat title generation using macOS on-device models.

```bash
# Install: https://github.com/Arthur-Ficial/apfel
brew install apfel
```
No API key or network setup is needed; it queries the macOS on-device Apple Foundation Models via the Neural Engine.


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

## Environment Variables

Copy `.env.example` to `.env` and fill in the app-level settings first:

**Required for a configured workspace:** `PWA_AUTH_TOKEN`. `CIAO_PUSH_CONTACT` is optional: leave it empty to run without Web Push notifications until you set a contact in Settings.

`ciao setup` writes the initial `.env` into the selected workspace, seeds stock agents, commands, schedules, agent-readable workspace docs (`CLAUDE.md`, `CIAO_CUSTOMIZATION.md`), and the default vault, renders `~/Library/LaunchAgents/com.ciao.server.plist`, and creates `~/Applications/Ciaobot.app`. The app shortcut opens `http://localhost:<port>/?setup=<token>`; the server redeems `.runtime/setup-token` once on localhost, sets the signed session cookie, then deletes the token. By default setup prints the launchd load command without starting the service; use `--load-launchd` to run `launchctl`. `ciao auth <claude|ollama>` runs the provider login command in Terminal; `--print-only` shows the command for the setup wizard. `GET /api/setup-status` reports required local config plus Claude Code, Ollama, and OpenRouter readiness so the wizard can poll after terminal OAuth commands or `.env` edits. In bootstrap mode, `POST /api/setup/finish` accepts the wizard's final local choices (`vault_root` is required; `workspace` defaults to `~/.ciaobot`), writes the real workspace `.env`, scaffolds and git-inits the configured `CIAO_VAULT_ROOT`, refreshes the LaunchAgent and `Ciaobot.app` shortcut, and requests the restart exit for supervisor relaunch (a foreground `ciao run` re-execs itself on that exit code).

**Runtime:** `CIAO_WORKSPACE`, `CIAO_PORT`

**Optional provider keys:** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CIAO_OLLAMA_API_KEY`

Workspace-specific integrations can still be set in `.env`, but the public `.env.example` does not ship private/work examples. Use user-owned credentials for each integration:

**GWS:** `GWS_PROFILE`, `GOOGLE_WORKSPACE_CLI_CLIENT_ID`, `GOOGLE_WORKSPACE_CLI_CLIENT_SECRET`

**Airtable:** `AIRTABLE_API_KEY` (get from https://airtable.com/create/tokens, scopes: data.records:read/write, schema.bases:read)

**Zendesk:** `ZENDESK_EMAIL`, `ZENDESK_API_TOKEN` (Admin Center > APIs > Zendesk API), `ZENDESK_SUBDOMAIN`

**BigQuery:** `GOOGLE_APPLICATION_CREDENTIALS`

**OpenAI:** `OPENAI_API_KEY` (used by voice transcription and other OpenAI-integrated features).

**n8n MCP:** `N8N_MCP_TOKEN` (bearer token for the self-hosted `n8n_mcp` HTTP server in `.mcp.json`). Lives in `.env` only, value redacted.

**OpenRouter:** `OPENROUTER_API_KEY` (optional). When set, OpenRouter is available as a model backend: the Anthropic-compatible endpoint (`https://openrouter.ai/api`) is reached via `ANTHROPIC_BASE_URL` env injection, so chats and one-shot automations can route `owner/model` ids (e.g. `anthropic/claude-haiku-4.5`) through OpenRouter. The picker exposes the per-tier alias defaults plus dynamically discovered anthropic-family models. The adversarial-review skill (`ciao.critique`) defaults to an OpenRouter panel when this key is set.

**Provider key PWA editor.** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CIAO_OLLAMA_API_KEY`, and `OPENROUTER_API_KEY` can all be written or updated directly from Settings → Home → API Keys. The server patches the `.env` file in place and triggers a restart automatically — no terminal required. This is the recommended path during initial setup or when rotating keys.

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

- `PWA_AUTH_TOKEN` (required): pre-shared token for PWA auth.
- `CIAO_PUSH_CONTACT` (optional): push notification contact string for the Web Push VAPID subject, for example `mailto:you@example.com`. Empty = Web Push disabled until set (in `.env` or Settings); nothing else breaks.
- `PWA_PORT` (default `8443`), `PWA_HOST` (default `0.0.0.0`).
- Session cookies are HttpOnly. Production/domain-scoped cookies are also Secure, and state-changing browser requests must come from the same host via `Origin` or `Referer`.
- Ciaobot sends baseline security headers from the Starlette app, including CSP, `X-Content-Type-Options`, `Referrer-Policy`, and frame denial.

### Optional env vars

- `CLAUDE_EXECUTION_MODE`: `normal`, `plan`, `auto`, `bypass`. Legacy `CLAUDE_PERMISSION_MODE` still accepted.
- `PWA_AUTH_REQUIRED`: set to `false` to disable password protection for the PWA dashboard entirely.
- `CIAO_DEV_MODE`: set to `true` to enable developer mode controls in the PWA dashboard (like the Deploy button).
- `CIAO_VAULT_MODE`: onboarding mode for memory-vault folders. Either `scratch` (create folders and documentation from scratch) or `existing` (connect and adapt existing markdown folders).
- `CIAO_BOOTSTRAP_WORKSPACE`: temp workspace root used when `PWA_AUTH_TOKEN` is absent. Defaults to `~/.ciao/bootstrap`; Ciaobot persists the generated bootstrap auth token under its `.runtime/` so first-run setup survives a restart.
- `CIAO_WORKSPACE`: filesystem workspace root for operational state, `.runtime/`, `.env`, `.claude/`, and `CLAUDE.md`. Default `.`.
- `CIAO_VAULT_ROOT`: durable memory/vault root. Default `<CIAO_WORKSPACE>/memory-vault`. Set this to an external notes folder when operational state should stay out of synced notes.
- `CIAO_WORKSPACES`: JSON workspace registry. Preferred shape is a list of objects with `name`, `vault_root`, `default_model`, `disallowed_tools`, `claude_ai_mcps`, `gws_profile`, and `model_bucket`. `vault_root` is relative to `CIAO_WORKSPACE` unless absolute. If unset, Ciaobot reads `.runtime/workspaces.json`; if that is also missing, it falls back to the legacy `personal` and `work` workspace definitions. `model_bucket` is a routing label, not a workspace name: `work` and `anthropic` keep Anthropic aliases, `personal` and `ollama` route Claude aliases through the configured Ollama tier models, and any other configured bucket is accepted as an Anthropic-style bucket until a provider mapping is added. Example: `[{"name":"default","vault_root":"memory-vault","default_model":"opus","gws_profile":"personal","model_bucket":"anthropic"}]`.
- `claude_ai_mcps` (workspace field, also settable from the PWA Workspaces tab): tri-state toggle for the claude.ai account-OAuth connector MCPs (Airtable, Atlassian, Slack, Asana, BigQuery, incident.io, Salesforce, Sentry). `null` = per-workspace default (personal off, else on). When off, the connector set is added to the effective denylist for that workspace. `disallowed_tools` covers extra non-connector tools (e.g. `mcp__n8n_mcp`); the effective denylist is the union of the two.
- `CIAO_CLAUDE_AI_MCPS_PERSONAL` / `CIAO_CLAUDE_AI_MCPS_WORK`: env override for the claude.ai MCPs toggle on the legacy `personal`/`work` workspaces. Accepts `true`/`false` (or `1`/`0`, `on`/`off`); `default`/unset keeps the per-workspace default (personal off, work on). The PWA Workspaces tab writes the per-workspace `claude_ai_mcps` field instead; these env vars are the headless equivalent.
- `CIAO_DISALLOWED_TOOLS_PERSONAL` / `CIAO_DISALLOWED_TOOLS_WORK`: CSV of extra (non-connector) tools to deny in the legacy `personal`/`work` workspace, on top of the connector set controlled by the toggle. Literal `none` clears the extras. Default extras for personal: `mcp__n8n_mcp` (self-hosted n8n, work-only); work defaults to none.
- `CIAO_AUTO_SYNC_ON_START=false` disables the automatic `git pull --rebase` on server startup (enabled by default).
- `CIAO_RESTART_EXIT_CODE=75`: the exit code `ciao.main` returns to signal `scripts/run-ciao.sh` to restart in place (used by the Deploy button). The restart loop picks up a new `.env` on every iteration, so Deploy doesn't need a full launchd reload. Legacy `TELEGRAM_BRIDGE_RESTART_EXIT_CODE` is also accepted.
- `CIAO_WORKSPACE_EXTRA_ROOTS`: comma-separated extra absolute paths the file/binary/image viewer may serve, on top of the workspace and vault roots. Same extension allowlist (no `.env`), same size caps. Set to `~/.claude` to inspect Claude Code config from the PWA.
- `CIAO_WORKSPACE_UNRESTRICTED_FILE_VIEWER=1`: opt-in escape hatch that drops the root sandbox entirely. When set, the read-only file/binary/image viewer serves any allowlisted-extension file on the system, regardless of path. Relative paths still anchor to the workspace. The extension allowlist (no `.env`, no key files) and size caps still apply, so secrets stay blocked. Writes from the in-PWA editor are NOT loosened: `workspace-file-write` keeps the real-root sandbox regardless of this flag, so a loosened read policy can't be turned into arbitrary file creation. Use this when you want "open any file" without enumerating roots via `CIAO_WORKSPACE_EXTRA_ROOTS`.
- `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND`: optional override for `gws`; the server defaults it to `file` at startup for headless auth.
- `scripts/gws-auth-helper.py`: interactive headless OAuth re-authentication when `gws auth login` cannot open a browser.
- `CIAO_OLLAMA_MODELS`: optional comma-separated list of Ollama model IDs to surface in the picker (e.g. `kimi-k2.7-code:cloud,deepseek-v4-pro:cloud`). Routing is now dynamic: any `:tag`/`:cloud`-shaped id routes through Ollama Cloud when `CIAO_OLLAMA_API_KEY` is set, and local-daemon models route through the daemon — no allowlist required for routing. This list is picker display only. When the picker selects an Ollama model, Ciaobot injects `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY=""`, `ANTHROPIC_BASE_URL`, and Claude Code internal model remaps so main turns, Task subagents, and auto-mode classifier calls all hit Ollama. The explicit empty `ANTHROPIC_API_KEY` is required (it forces the CLI to pick `ANTHROPIC_AUTH_TOKEN` as the auth source), so do not set `ANTHROPIC_API_KEY` in `.env`.
- `CIAO_OLLAMA_API_KEY`: Ollama Cloud API key (https://ollama.com/settings/keys). Setting this flips the wiring to "direct cloud" mode: `ANTHROPIC_AUTH_TOKEN` becomes the key (Bearer auth), `ANTHROPIC_BASE_URL` defaults to `https://ollama.com`, and the local daemon is bypassed. Cloud models (`*:cloud` IDs) require an active Ollama Cloud subscription tier that covers them. A bare API key is not enough.
- `CIAO_OLLAMA_COOKIE`: browser session cookie for Ollama Cloud usage scraping. Copy the full `Cookie` header value from your browser's devtools Network tab while logged into `ollama.com/settings`, then paste it here. When set, the Settings page shows your Ollama Cloud session and weekly usage bars. The cookie expires when the browser session ends; refresh it by signing in again.
- `CIAO_OLLAMA_URL`: base URL override. Default depends on whether `CIAO_OLLAMA_API_KEY` is set: with key → `https://ollama.com`, without → `http://localhost:11434` (device-linked daemon flow via `ollama signin`). Set explicitly to point at a self-hosted relay.
- `CIAO_OLLAMA_LOCAL_URL`: base URL of the *local* Ollama daemon used for locally-installed models, independent of `CIAO_OLLAMA_URL` (which may point at ollama.com when a cloud key is set). Default `http://localhost:11434`. Local models route here with the literal `ollama` token; cloud models keep going to `CIAO_OLLAMA_URL`. For local routes, Ciaobot maps Claude Code's internal haiku/sonnet/opus/subagent/classifier slots to the selected local model so those background calls do not fall back to unsupported `claude-*` IDs.
- `CIAO_OLLAMA_LOCAL_MODELS`: comma-separated list of local-daemon model IDs to pin into the pickers (e.g. `gemma4:12b-it-qat`). Usually unnecessary: at startup Ciaobot auto-discovers installed models via `GET <local_url>/api/tags` and merges them in (models already in `CIAO_OLLAMA_MODELS` keep their cloud routing). Local models appear in the personal Claude bucket, badged as local.
- `CIAO_OLLAMA_LOCAL_DISCOVERY`: set to `false`/`0` to disable the startup auto-discovery of local daemon models. Default enabled; discovery is best-effort with a 2s timeout, so a missing daemon just means no local models.
- `CIAO_OLLAMA_TITLE_MODEL`: cheap model used to auto-title Ollama-routed chats (the chat's own `:cloud` model is usually subscription-gated and overkill for 50-token titles). Default `gemma4:e2b-it-qat`. Other free-tier-friendly options: `gemma3:4b`, `qwen3:8b`, `gpt-oss:20b`, `nemotron-3-nano:30b`. Anthropic chats keep titling via `CIAO_TITLE_MODEL` (default `haiku`) and ignore this knob. When the `apfel` CLI is on PATH, the title call uses it first. If `apfel` is unavailable or fails, Ciaobot falls back to the Claude Agent SDK with the same Ollama env injection used for chats.
- `CIAO_OLLAMA_WEBSEARCH_HOOK`: kill switch for the PostToolUse hook that backfills WebSearch on Ollama-cloud-routed chats. Ollama's Anthropic-compat layer doesn't execute the server-side `web_search` tool, so Claude Code's built-in WebSearch returns an empty boilerplate; the hook reruns the query against Ollama's standalone `POST /api/web_search` and injects the real results as `additionalContext`. Default `1` (enabled). Set `0` to disable. No-op on the Anthropic path (where WebSearch works natively) and on local-daemon routes. See `ciao/observability/hooks.py`.
- `CIAO_OLLAMA_AUTO_CLASSIFIER`: **removed**. Auto mode is now always live for Ollama-routed chats because the bundled `claude` CLI's permission classifier resolves to an Ollama-served model. Previously this was an opt-in flag because the classifier targeted Anthropic's server-side gate, which ollama.com and local daemons do not expose; the tier-remap env now injected on Ollama routes (`ANTHROPIC_DEFAULT_{HAIKU,SONNET,OPUS,FABLE}_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_AUTO_MODE_MODEL`, `CLAUDE_CODE_BG_CLASSIFIER_MODEL`) fixes that. Delete `CIAO_OLLAMA_AUTO_CLASSIFIER` from your `.env` if it is still there.
- `CIAO_INSIGHTS_DISABLED`: set to `true`/`yes`/`on` to disable post-archive session insights extraction. Default is enabled (false). When enabled, after a chat is archived, raw JSONL is filtered and run through a model to extract errors, dead ends, new entities, decisions, and reusable code, then appended as a `## Session insights` section to the archive markdown.
- `CIAO_INSIGHTS_MIN_TURNS`: minimum number of turns in a session before insights extraction runs. Default `5`. Override with any positive integer. Short sessions are skipped.
- `CIAO_INSIGHTS_MODEL`: model ID for insights extraction, routed through the Ollama Anthropic-compatible API. Default `deepseek-v4-flash:cloud`. This model is fixed at the server level and bypasses the Ollama models allowlist. Uses existing `CIAO_OLLAMA_*` config (base URL, API key) through the Claude Agent SDK one-shot path.
- `CIAO_TRAJECTORIES_DISABLED`: set to `true`/`yes`/`on` to disable structured trajectory capture after a chat is archived (skills loaded, tools used, errors, decisions). Default enabled. Trajectories are written to `~/.ciao/trajectories/YYYY-MM/<session-id>.json` and mined by the weekly skill-evolution pass.
- `CIAO_SKILL_EVOLUTION_DISABLED`: set to `true`/`yes`/`on` to hard-disable the weekly skill-evolution pass even if the schedule entry remains. Default enabled. The schedule entry itself is the primary on/off switch.
- `CIAO_REVIEW_MODELS`: comma-separated list of model IDs for the critique / adversarial-review skill (`ciao.critique`). Overrides the default panel. IDs use the native shape for their backend: `owner/model` for OpenRouter (e.g. `anthropic/claude-sonnet-4.5`), `:tag`/`:cloud` for Ollama, bare aliases for Anthropic. Runtime-overridable from the PWA (Settings → Models, persisted in `.runtime/app_settings.json` under `critique_models`).
- `CIAO_ADVERSARIAL_MODELS`: legacy alias for `CIAO_REVIEW_MODELS`.
- `CIAO_TRAJECTORY_RETENTION_MONTHS`: number of months of trajectory JSON files to keep under `~/.ciao/trajectories/`. Older `YYYY-MM/` directories are pruned by the skill-evolution pass. Default `6`.
- `CIAO_MEMORY_ENABLED`: set to `false`/`0`/`no`/`off` to skip injecting `~/.ciao/memory.md` and `~/.ciao/user.md` into the system prompt and registering the `memory` MCP tool. Default enabled. Useful when debugging prompt-cache behavior or for one-off chats that should run with no persisted memory.
- `CIAO_MEMORY_CHAR_LIMIT`: soft cap (chars) on `~/.ciao/memory.md`. Default `2200`. The `memory` tool refuses `add` actions that would push the file past this limit and asks the agent to consolidate (merge or remove entries) first.
- `CIAO_USER_CHAR_LIMIT`: soft cap (chars) on `~/.ciao/user.md`. Default `1375`. Same consolidation flow as `CIAO_MEMORY_CHAR_LIMIT`.
- `CIAO_MEMORY_DIR`: override for the directory holding `memory.md` and `user.md`. Default `~/.ciao`. Used in tests to point at a tmp_path; not normally set in production.
- `CLAUDE_DEFAULT_MODEL_PERSONAL` / `CLAUDE_DEFAULT_MODEL_WORK`: legacy per-workspace default model knobs for new chats and schedules. They seed the default `personal`/`work` entries when `CIAO_WORKSPACES` and `.runtime/workspaces.json` are absent. Empty value falls back to `claude_default_model` (first entry of `CLAUDE_MODELS`). Explicit picker selection in the PWA always wins.
- `CIAO_DISALLOWED_TOOLS_PERSONAL` / `CIAO_DISALLOWED_TOOLS_WORK`: legacy per-workspace tool denylist knobs. They seed the default `personal`/`work` entries when `CIAO_WORKSPACES` and `.runtime/workspaces.json` are absent. **Personal defaults to denying the 8 claude.ai connector MCPs** (Airtable, Asana, Atlassian, Google_Cloud_BigQuery, Salesforce, Sentry, Slack, incident_io) **plus the self-hosted `n8n_mcp`** since those are work tools. Work defaults to no denylist. Three-state knob: unset / blank → use defaults; CSV → custom denylist; literal `none` → opt out (zero denylist on the workspace, used to restore claude.ai MCPs on personal). Tool names follow the SDK convention: `mcp__servername` blocks the whole server, `mcp__servername__toolname` blocks one tool, `Bash` blocks the Bash builtin, etc.
- `CIAO_AUTO_VAULT_INDEX`: set to `false` to disable automatic vault index regeneration on server startup. Default `true`.
- `CIAO_AUTO_UPDATE_GITHUB_SKILLS`: set to `false` to disable checking/updating locked package skills from GitHub on boot. Default `true`.
- `CIAO_GITHUB_REPO`: `owner/name` of the GitHub repository used to fetch the changelog (commits between the installed and latest release tags) shown in the Settings update prompt. Default `raffaelefarinaro/ciaobot`.
- `CIAO_OLLAMA_HAIKU_MODEL` / `CIAO_OLLAMA_SONNET_MODEL` / `CIAO_OLLAMA_OPUS_MODEL`: per-tier Ollama model overrides for Claude chats whose effective `model_bucket` is `personal` and that select the aliases `haiku`, `sonnet`, or `opus`. The alias resolves at runtime when Ollama is available (cloud key or local daemon); `work` bucket chats keep Anthropic aliases. Defaults: `deepseek-v4-flash:cloud` (haiku), `kimi-k2.7-code:cloud` (sonnet), `glm-5.2:cloud` (opus).
- `CIAO_OPENROUTER_BASE_URL`: base URL for OpenRouter's Anthropic-compatible endpoint. Default `https://openrouter.ai/api` (the SDK appends `/v1/messages`). Override only for a self-hosted relay.
- `CIAO_OPENROUTER_HAIKU_MODEL` / `CIAO_OPENROUTER_SONNET_MODEL` / `CIAO_OPENROUTER_OPUS_MODEL`: per-tier OpenRouter model overrides (owner/model ids) for chats/automations that select the `haiku`/`sonnet`/`opus` aliases and route through OpenRouter. Defaults: `anthropic/claude-haiku-4.5`, `anthropic/claude-sonnet-4.5`, `anthropic/claude-opus-4.8`.
- `CIAO_OPENROUTER_MODELS`: comma-separated allowlist of extra OpenRouter model IDs (owner/model) to surface in the picker on top of the tier defaults and the discovered anthropic-family catalogue. Leave empty to rely on dynamic discovery.
- `CIAO_OPENROUTER_WEBSEARCH_HOOK`: kill switch for the PostToolUse hook that backfills WebSearch on OpenRouter-routed chats, mirroring `CIAO_OLLAMA_WEBSEARCH_HOOK`. OpenRouter's Anthropic-compat endpoint doesn't execute the server-side `web_search` tool, so Claude Code's built-in WebSearch returns an empty boilerplate; the hook reruns the query as a one-shot chat-completions call with OpenRouter's `web` plugin (on the configured haiku-tier model) and injects the `url_citation` sources as `additionalContext`. Default `1` (enabled). Set `0` to disable. See `ciao/observability/hooks.py`.
- `CIAO_DISPATCH_SCHEDULES`: `1`/`true`/`on` to make this instance dispatch scheduled automations. Off by default (opt-in), so an occasional dev box never double-fires schedules. Set it on only for the always-on "main" device.
- `CIAO_PUSH_CONTACT`: push notification contact string. Optional, no default; empty disables Web Push delivery. Used for VAPID subject.
- `CIAO_PUSH_DELAY_SECONDS`: delay before sending push notifications after a completed turn (default `30`). Rapid replies to the same chat cancel the previous timer and start a new one (coalesce into a single push). Permission requests and model questions push immediately (no delay). Unanswered permission requests re-fire every 30 seconds, up to 3 times, until the user approves/denies or the turn ends.
- `CIAO_PYTHON`: path to a specific Python binary for `scripts/dev.sh` (e.g. when Homebrew breaks `ensurepip`).
- `CIAO_PATH`: baked into the launchd plist's `EnvironmentVariables` at setup time so the server's subprocesses (npm, node, Homebrew git/pip) are found despite launchd's minimal default PATH. Not an operator env var; it's a `com.ciao.server.plist.tmpl` placeholder rendered from the user's shell PATH.
- `CLAUDE_MODELS`: comma-separated list of Anthropic models in the picker. Default `opus,sonnet,haiku`.
- `CIAO_TITLE_MODEL`: model used to auto-title Anthropic chats. Default `haiku`.
- `CIAO_TITLE_MODEL_OVERRIDE`: env-level default for the title-model override normally set from the PWA (Settings → Models tab, persisted in `.runtime/app_settings.json`). When set (either way), it wins over both `CIAO_OLLAMA_TITLE_MODEL` and `CIAO_TITLE_MODEL` and is routed per model: local daemon, Ollama cloud, or Anthropic alias. Empty = automatic routing.
- `CIAO_TRANSCRIPTION_ENGINE`: voice dictation engine, `cloud` (default; OpenAI API, needs `OPENAI_API_KEY`) or `local` (mlx-whisper on-device, free, Apple Silicon only; bundled in the default bootstrap, this repo now targets macOS only). Runtime-overridable from Settings → Models.
- `CIAO_TRANSCRIPTION_LOCAL_MODEL`: Hugging Face repo of the mlx-whisper checkpoint for the local engine. Default `mlx-community/whisper-large-v3-turbo` (downloaded on first use).
- `CIAO_MAX_IMAGE_BYTES` / `CIAO_MAX_VOICE_BYTES`: upload size caps. Defaults 10 MB / 25 MB.
- `CIAO_MEDIA_TTL_HOURS`: auto-cleanup age for uploaded media. Default `72`.
- `CIAO_PUBLIC_PRIVATE_PATTERNS`: comma-separated private string patterns used by `ciao public-preflight scan` when a `--private-patterns` file is not supplied. Intended for public extraction checks, not normal runtime.

**Note:** `scripts/gws-auth-helper.py` is the helper for headless `gws` auth when the keyring backend fails.

### Injected CLI context variables

The Ciaobot server injects the following environment variables into every spawned agent CLI subprocess (`claude`):

- `CIAO_WORKSPACE`: the filesystem workspace root path. This is operator config forwarded for compatibility; it is not the logical chat workspace.
- `CIAO_ACTIVE_WORKSPACE`: the logical workspace name derived per turn from `chat -> project -> project.workspace`.
- `CIAO_ACTIVE_PROJECT`: the active project ID.
- `CIAO_MODEL`: the model ID configured for the chat.
- `CIAO_PROVIDER`: the provider name (`claude`).
- `CIAO_MODEL_BUCKET`: the model bucket configured for the chat (e.g. `personal`, `work`, or empty).
- `CIAO_CHAT_ID`: the ID of the active chat.
- `GWS_PROFILE`: resolved from the active workspace's `gws_profile`, falling back to `GWS_PROFILE` / the default profile.

These variables let package commands (like `ciao create-chat`) or custom skills auto-detect the current chat's context and preferences.

### Adding or changing an Ollama model

See `docs/runbooks/add-ollama-model.md` for the full recipe (env-var wiring, alias and one-shot model overrides, restart, verification).

### Google OAuth client files

- Do not commit Google OAuth client JSON files. `client_secret_gws.json` is ignored and should live only as a local/operator file when needed.
- Prefer `gws` profile config directories for active tokens: `~/.config/gws-personal/` and `~/.config/gws/`.
- If a client secret was ever committed, rotate that OAuth client in Google Cloud. Removing the file from the repo does not remove it from git history.

### Deploy

Ciaobot runs on macOS under launchd.

- `ciao setup --workspace <path> --load-launchd` renders and loads the LaunchAgent.
- The packaged launchd template is `ciao/stock/deploy/com.ciao.server.plist.tmpl`.
- Stop: `launchctl unload ~/Library/LaunchAgents/com.ciao.server.plist`.
- Remote access is not configured by the public app. Use localhost by default, or put Tailscale or another user-owned network layer in front of the local server.

### Server startup behaviors

Auto-skills update, auto-CLI update, and similar behaviors belong in server startup code (`ciao/main.py`), not in Claude Code's `settings.json` hooks.
