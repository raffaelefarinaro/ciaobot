# Integrations and environment variables

What Ciaobot needs from the machine it runs on: the agent CLIs it drives, and
the environment variables the engine reads or sets. Install steps for the app
are in the [README](README.md); Linux hosting is in [docs/LINUX.md](docs/LINUX.md).

## Agent CLIs

Ciaobot has no model account of its own. It runs the Claude Code, Codex or
OpenCode CLI already installed on the machine, over the Agent Client Protocol
(ACP). Each CLI owns its sign-in, its credentials and its MCP servers; Ciaobot
stores none of them and has no API-key fields. Settings → Providers runs each
CLI's install and login commands in a terminal; `ciao auth <provider>` does the
same from a shell (`--print-only` prints the command).

### Claude

Claude runs through the `claude-agent-acp` adapter, pinned at
`@agentclientprotocol/claude-agent-acp@0.108.1` in `ciao/adapter_install.py`.
It drives the user's own `claude`, passed as an absolute path in
`CLAUDE_CODE_EXECUTABLE`. Node 22 or newer is required.

If `claude-agent-acp` on the PATH is exactly the pinned version (and not inside
a world-writable folder), Ciaobot uses it. Otherwise it installs the adapter once
into a private folder (`~/Library/Application Support/Ciaobot/adapters` on
macOS, the engine's state folder on Linux) with the machine's `npm` and a
lockfile shipped in `ciao/stock/adapters/<name>/` (`npm ci --omit=optional
--ignore-scripts`). The install starts in the background at engine startup when
`claude` is present; a failure shows on the Settings card, whose Install button
runs the same command:

```bash
<engine python> -m ciao.adapter_install claude-agent-acp
claude auth login
```

MCP servers reach the adapter as a `--mcp-config` argument, so a server's
`env` and `headers` are visible in the process list to other users on the
machine.

### Codex

Codex runs through `codex-acp`, pinned at `@agentclientprotocol/codex-acp@2.7.0`
and found or installed the same way as the Claude adapter. It runs the user's
own `codex`, passed in `CODEX_PATH`. When no `codex` resolves, the Settings
install line also installs the CLI at the version the adapter is built against.

```bash
npm install -g @openai/codex@0.166.0                # only when no codex resolves
<engine python> -m ciao.adapter_install codex-acp
codex login
```

Settings reads only the exit code of `codex login status`.

### OpenCode

Ciaobot requires OpenCode 2.3.2 or newer in the 2.x line and runs one
`opencode acp` process per active chat. OpenCode reaches every other model
backend (OpenAI-compatible endpoints, OpenRouter, Ollama, LM Studio): configure
it in OpenCode and its connected models appear in Ciaobot's model picker.

```bash
npm install -g @opencode/cli
# or: brew install anomalyco/tap/opencode-v2
# or: curl -fsSL https://opencode.ai/v2/install | bash
ciao auth opencode                  # opens `opencode auth login`
```

The npm package `opencode-ai` and the `sst/tap` Homebrew tap are OpenCode 1,
which Ciaobot does not support, and `opencode upgrade` does not move 1.x to 2.x.
Run `npm uninstall -g opencode-ai` first, since it also provides an `opencode`
command. Set `CIAO_OPENCODE_BIN` if OpenCode is installed outside the PATH.

### MCP servers

Each CLI owns its MCP configuration, sign-in and on/off state. Settings → MCP
opens the CLI's own MCP command in the in-app terminal; a change applies to new
chats, and a running chat picks it up after `ciao chat reload`. For Claude
chats, the workspace's **claude.ai connectors** trim option (on by default)
can hide the Claude account's remote connectors by setting
`ENABLE_CLAUDEAI_MCP_SERVERS=false`.

## Environment variables

The engine reads `.env` in the workspace folder and its own process
environment. `tests/test_env_vars_documented.py` requires every `CIAO_*` name
the code references to appear here or in that test's internal allowlist.

### Read by the engine

| Variable | Meaning |
|---|---|
| `PWA_AUTH_TOKEN` | Dashboard password and session-signing secret. Setup writes it; Settings → General changes it. A workspace without one signs sessions with a generated `.runtime/session-secret` and stays protected. |
| `PWA_PORT` | Port the server binds. Default `8443`. The bind address is a Setting, not a variable. |
| `CIAO_WORKSPACE` | Workspace folder (`.env`, `.runtime/`, skills, the registry). Set by the service definition; move it with Settings → Advanced → Ciaobot folder or `ciao workspace-move`. |
| `CIAO_RUNTIME_ROOT` | Runtime-state folder, default `<workspace>/.runtime`. Read from the process environment only (the LaunchAgent sets it); a value in `.env` is ignored. |
| `CIAO_PORT` | Set by the LaunchAgent. CLI commands and the macOS service manager use it to find the running server when `.env` has no `PWA_PORT`. |
| `CIAO_VAULT_ROOT` | Notes (vault) root, default `<workspace>/memory-vault`. Also set per chat for the agent (see below). |
| `CIAO_MEMORY_DIR` | Overrides the folder of the search index (default: the runtime root). Test harnesses outside pytest must set it. |
| `CIAO_BOOTSTRAP_WORKSPACE` | Temporary workspace for first-run mode, used while no workspace is set up. Default `~/.ciao/bootstrap`. |
| `CIAO_NO_BROWSER` | Any value stops a first-run `ciao run` from opening the setup page in a browser. The systemd unit sets it. |
| `CIAO_OPENCODE_BIN` | Absolute path to an OpenCode CLI outside the PATH. |
| `CIAO_ENGINE_PATH` | Overrides the engine executable the CLI runs. Nothing sets it. |
| `CIAO_ALLOW_LAUNCH_AGENT_REPOINT` | `1`/`true`/`yes` lets `ciao setup` repoint the LaunchAgent to another workspace without `--yes`. |
| `CIAO_PYTHON` | Python binary for `scripts/dev.sh` (via `scripts/ensure-deps.sh`). |

Retired variables the engine still recognises only to import or warn:
`CIAO_WORKSPACES` (imported once into `.runtime/workspaces.json`),
`CIAO_LOG_LEVEL` and `CIAO_APP_REPO` (imported once into Settings),
`CIAO_DEV_MODE` (ignored), and `CIAO_VAULT_DIR` (recognised in an old service
definition during a Mac takeover). The `legacy-env-ignored` tile asks you to
remove the first four from `.env`.

### Set by Ciaobot for agent processes

Agent CLIs inherit the engine's environment minus `CIAO_*` and
`PWA_AUTH_TOKEN`, so credentials such as `ANTHROPIC_API_KEY` set there are
handled by the CLI itself. Ciaobot then adds, per chat:

- `CIAO_WORKSPACE`, `CIAO_VAULT_ROOT` (the chat's vault),
  `CIAO_ACTIVE_WORKSPACE`, `CIAO_ACTIVE_PROJECT`, `CIAO_MODEL`,
  `CIAO_PROVIDER`, `CIAO_CHAT_ID`.
- `CIAO_AGENT_TOKEN` and `CIAO_AGENT_URL`: a scoped short-lived token and the
  `/agent/v1/{op}` base URL for `ciao <noun> <verb>` commands in the
  foreground shell (see [docs/AGENT_CLI.md](docs/AGENT_CLI.md)). Never
  configure them.
- `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_CODE_DISABLE_ARTIFACT=1`,
  `GIT_TERMINAL_PROMPT=0`, `GH_PROMPT_DISABLED=1`, and the engine's own
  executable folder first on `PATH`.
