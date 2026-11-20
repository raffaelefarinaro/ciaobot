# Adding a provider

A runtime provider is an agent CLI that speaks the Agent Client Protocol (ACP).
Three ship today: `claude` (Claude Code through `claude-agent-acp`), `opencode`
(`opencode acp`) and `codex`. Each is one `AgentSpec`, one `AcpProvider`
subclass and one descriptor. No turn loop is written by hand.

A model backend behind OpenCode (Ollama, OpenRouter, an OpenAI-compatible
endpoint) is not a provider. The user configures it in OpenCode, and Ciaobot
lists whatever OpenCode reports.

## What to write

1. **An `AgentSpec`** in `ciao/providers/acp/agents.py` (the three existing
   specs are `CLAUDE`, `CODEX` and `OPENCODE`; the class is in
   `ciao/providers/acp/spec.py`). It names the binary and argv, the fixed
   environment, `client_meta` opt-ins, the config option each Ciaobot mode maps
   to (`modes`), `model_config_id` and `thought_config_id`, the quiet period and
   the initialize timeout. The spec is data. Set `slash_capsule_as_block` only
   when the agent's command parser keeps just the first text block: the context
   capsule then goes in a second block, so a native slash command still works.

2. **A subclass of `AcpProvider`** (`ciao/providers/acp/provider.py`) that sets
   `spec`. Override `session_meta(request)` and the classmethod `oneshot_meta()`
   only when the agent needs `_meta` on `session/new`, `session/load` and
   `session/resume`, or on its one-shot calls. Both default to `{}`. Put only
   the agent's own options in `_meta`. `ClaudeAcpProvider` is the example: its
   trim denies, settings object and connectors env come from
   `claude_session_options` (`ciao/providers/acp/agents.py`), built from
   `TRIM_OPTIONS` in `ciao/claude_trim.py`.

3. **A descriptor** in `ciao/provider_registry.py`. Fields that matter here:
   `id`, `label`, `short_label`, `cli_label`, `factory_path`
   (`ciao.providers.acp.agents:<Subclass>`), `acp_spec_path`
   (`ciao.providers.acp.agents:<SPEC>`), `auth_command_path`,
   `status_probe_path`, `system_skills_path`, `thinking_levels`,
   `default_model` and `default_model_config_key`. Imports resolve on first use,
   so listing providers never imports an agent.
   The Claude-only behaviours are flags that default to off: `session_blob_on_disk`,
   `workspace_cli_settings`, `tier_model_aliases`, `claude_ai_connectors_toggle`,
   `title_from_session_events` and `session_jsonl_history`. A new provider leaves them
   off unless its CLI really has the same behaviour.

4. **Login and install actions.** Add `<id>-install` and `<id>-login` to
   `ACTION_IDS` in `ciao/host_terminal.py` and to `action_command`. The install
   command is a constant, never built from request data. The login command comes
   from `auth_command_path`. `test_every_provider_has_a_login_action` in
   `tests/test_provider_connect_route.py` fails until the login action exists.

5. **A status probe.** Write the function `status_probe_path` names, in
   `ciao/setup_status.py`. Its signature is `(env, *, config, config_path,
   workspace_root, **unused) -> dict`. It must not echo credentials. An empty
   `status_probe_path` means no Settings connection row.

6. **Fixtures and tests.** Replay captured ACP updates with no network:
   `tests/fixtures/acp/updates_<id>.jsonl` with a `.golden.json` beside it, and
   drive a scripted agent (`tests/fixtures/acp/fake_agent.py`) through
   `ProviderService`, as `tests/test_acp_provider.py` does. A fixture carries no
   credentials.

7. **One real turn** against the installed CLI before the descriptor ships. The
   turn has to exercise what fixtures cannot: login state, a permission card
   answered from the PWA, a question card, Stop, and a reload that still shows
   the transcript. OpenCode changes also follow the version floor in
   `docs/UPKEEP.md`; do not relax it while adding something else.

## What the host does without you

These read the registry or the provider object and need no edit:

- Default provider, chat and schedule validation, and the `--provider` flags
  of the `ciao` CLI.
- Settings connection cards and the startup connection checks for every
  descriptor with a status probe.
- `/api/models`, which takes each provider's `ProviderCapabilities` and its
  model catalog (`model_catalog`, classmethod, default `[]`; the ACP probe is
  cached per agent by `invalidate_acp_catalog`).
- Permission and question routing, the between-turn drain, the idle reaper and
  the warm pool for ACP agents, and transcript history, which renders from the
  session journal (`read_thread` returns `{"acp_journal": ...}`).
- Auto-titles: the agent's session title arrives as a `session_info_update`
  event; the one-shot titler is the fallback.
- One-shot calls (`ciao/providers/oneshot.py`) for titles, insights and doc
  folds, through the agent's `oneshot_meta`.

Image input is not read from the catalog. An ACP adapter states it at its
handshake (`promptCapabilities.image`), and `agent_accepts_images()` reports it
to the image preflight.

## Branches on the provider id

Hosts under `ciao/web` reach agents only through `BaseProvider` and the
registry. They never name `AcpProvider`, a concrete subclass or `ciao.providers.acp`
(`test_web_sources_name_no_acp_provider_class` fails if one appears).

A few host branches still compare the provider id. They are allowed only where
the difference is the agent's own on-disk or command layout (`commands.py`
command directories, the connection-action cache in `routes_api.py`). The guard
`test_web_sources_branch_on_no_acp_provider_id_outside_allowlist` in
`tests/test_provider_host_seam.py` lists every such branch. A new id that adds
one fails that test. Put the behaviour on the descriptor or the provider object
instead. The remaining Claude-only branches on that list are open work.

Claude tier aliases (`haiku`, `sonnet`, `opus`, `fable`) are rewritten to the
provider's own default on every non-Claude id. An empty model on a `ResultEvent`
is written back onto the chat only for an ACP provider (its descriptor sets
`acp_spec_path`).

## Assets and per-CLI files

Skill and MCP projection is `ciao/sync_skills.py`. Before you add a projection,
check whether the new CLI already reads `AGENTS.md` and a skills directory. If it
does, project only what it does not discover (commands, subagents, MCP), as
OpenCode does for `.opencode/agents/`, `.opencode/commands/` and `opencode.json`.
The slash-command directory per agent is `_provider_command_dir` in
`ciao/web/commands.py`. `system_skills_path` feeds Settings and the slash picker,
so it must be cheap or cached. `/api/commands` calls it.

The Claude-specific permission language (disable options, trimmed skills) is
described in `docs/AGENT_CLI.md`. A new CLI with the same tools needs the
equivalent in its own permission language.

## Optional, ship only if the CLI has the feature

- **Conversation import.** `ciao/import_sources/` turns one CLI's stored sessions
  into a `NormalizedSession`. A new source is a module beside `claude_code` and
  `opencode`, plus a `KNOWN_PROVIDERS` entry. Chat works without it.
- **Quota.** Claude reports a rate-limit snapshot. OpenCode reports none. A new
  provider reports none unless its CLI exposes one.

## Docs, site, and the weekly probe

These name the providers in user-facing text and need the new id:

- `INTEGRATIONS.md` (install and login).
- `docs/LINUX.md` (adapter installs) and the provider cards in `site/guide.html`.
- The stock `ciao-capabilities` skill (`ciao/stock/skills/ciao-capabilities`).
- `.github/workflows/discover-agents.yml`, the weekly probe. It installs and runs
  a real turn for `claude` and `opencode` only. `codex` is not in it yet.

## Order of work

1. Confirm the CLI can resume a session, stop a turn and run a shell command. If
   it cannot run a shell command, stop.
2. Write the `AgentSpec`, the subclass and the descriptor. Stream one text turn
   to a `ResultEvent` with a session id.
3. Add permission replies, question replies and Stop. Leave unsupported features
   explicitly off in `ProviderCapabilities`.
4. Add the login and install actions and the status probe.
5. Capture fixtures, replay them, then run one real turn.
