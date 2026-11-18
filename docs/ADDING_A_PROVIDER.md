# Adding a provider

A runtime provider is a CLI or SDK that runs a whole agentic turn. Today
that set is Claude Code and OpenCode. Codex, Cursor, the Antigravity CLI,
and anything else in that class is the same job: one adapter that speaks
the contract below, plus the call sites that still name the two existing
ids and otherwise skip the work.

A model backend behind OpenCode (Ollama, OpenRouter, an OpenAI-compatible
endpoint) is not a provider. The user configures it in OpenCode, and
Ciaobot lists whatever OpenCode reports.

Ciaobot used to project skills and MCP config into `.codex/`. That
projection is retired. `sync_skills._cleanup_legacy_codex_projections`
still deletes the marked artifacts on upgrade. Adding Codex as a runtime
provider is a new adapter. Leave the cleanup in place.

## What the registry already does

`ciao/provider_registry.py` is the enumeration. A `ProviderDescriptor`
carries the id, the three label forms, and lazy import paths for the
factory, login command, system-skill list, and install/auth probe, plus
thinking levels and the default-model setting. Paths resolve on first use,
so listing providers never imports an agent SDK.

These read the registry and pick up a new id with no further edit:

- Workspace default provider, chat and schedule validation, `ciao` flags
  that take `--provider`.
- Settings → Providers connection cards.
- Startup connection checks for every descriptor that has a status probe,
  except Claude, which is probed on its own path (`ciao/main.py`).
- `/api/models`, which ships each provider's `ProviderCapabilities`.

`ciao/provider_service.py` constructs the class and calls
`run_streaming`. It does not know how the CLI works.

## The turn contract

Implement `ciao.providers.base.BaseProvider`. The drain and connection
operations the service dispatches — `can_drain`, `cli_connected`,
`drain_events` — are concrete optional defaults on the base, so a minimal
adapter implementing only `run_streaming` satisfies that contract. A
missing override for any other row below still degrades to that row's
base default, an unsupported-feature answer or a silent skip — check each
row's last column before assuming one fits. Provider-specific manager
access that remains duck-typed or id-branched is in the skip list below
and is separate work, not something this table has finished.

### Inbound: `AgentRequest`

| Field | What it carries |
|---|---|
| `prompt`, `display_prompt` | Model-facing text, and the human-visible form when they differ. |
| `model`, `thinking_level`, `mode` | Empty model means the provider picks. `mode` is `normal` / `auto` / `plan` / `bypass`. |
| `resume_session`, `fork_session` | Continue the native session, or branch it when it is busy. |
| `images` | Absolute paths. The shared prompt also lists them in an `[INCOMING IMAGES]` block (`build_prompt`). |
| `extra_env` | Includes `CIAO_AGENT_URL` and `CIAO_AGENT_TOKEN`. The foreground shell must see both. Background children strip the token. |
| `disallowed_tools`, `memory_pass` | Claude applies the tool list directly. OpenCode selects its own stricter ruleset when `memory_pass` is set. |
| `stable_context_prefix`, `context_digest` | Re-send the stable capsule when the provider replaces a session. |

The control plane is already provider-neutral. The agent reaches Ciaobot
by running `ciao <noun> <verb>` with that token. See `docs/AGENT_CLI.md`.
A harness that cannot run a shell command cannot use it.

### Outbound: `StreamEvent`

Yield these from `run_streaming`. The PWA and the transcript render this
set and nothing else.

| Event | Required when |
|---|---|
| `AssistantTextDelta` | The model writes. Set `parent_tool_use_id` for text inside a subagent. `phase` distinguishes progress commentary from the final answer when the CLI exposes it. |
| `ThinkingEvent` | The CLI streams reasoning. |
| `ToolUseEvent` | A tool runs. `file_touches` is how Write/Edit become file cards. `request_id` marks a native question that must be answered inside the turn. |
| `PermissionRequestEvent` | The CLI is waiting on an approval card. |
| `TokenUsageEvent` | Live counts. The terminal `ResultEvent.usage` is still the authoritative total. |
| `ModelChangedEvent` | The effective model changed mid-turn. |
| `ResultEvent` | Every turn ends on one. Carry `session_id`, `effective_model`, `is_error`, `stopped` (a user Stop is not an error), `usage`, and `quota` when the CLI has it. `recovered_with_pending` means a permission or question card is still unanswered. Set `origin` to a non-empty kind (Claude: `task-notification`) on a result the provider produced without a prompt from Ciaobot, so the host drain can tell a follow-up turn apart. |

### Methods the chat manager already calls

| Method | Who calls it | If it is absent |
|---|---|---|
| `disconnect()` | Session reset, delete, idle reap, stop escalation. | The process is leaked. On `BaseProvider` the default is a no-op. |
| `ActiveHandle.stop()` | The Stop button. | Registered from `run_streaming`. |
| `steer_turn(request)` | A user steer during a live turn: returns a delivery id and emits `SteerDeliveredEvent` once the model consumed it. | Falls back to the next-turn queue. On `BaseProvider` the default returns `None`. |
| `move_to_background()` | The PWA's *Move to background* action (OpenCode's Ctrl+B). | On `BaseProvider` the default returns `"unsupported"` and the route answers 409. A provider that implements it sets `ToolUseEvent.backgroundable` on the calls it can move; the PWA shows the action only for those. |
| `response_session_id` | Unheld permission and question replies, to reject replies for an old session. | Defaults to `""` when the provider has no session-scoped response fence. |
| `current_session_id` | Turn delivery session rotation in `_drive_stream` and the host's live session probes; the service getter forwards it. | Defaults to `None`: the adapter tracks no session. A service with no provider instance reports `None` without constructing one. |
| `has_permission_request`, `permission_request_session`, `tool_use_id_for_request` | Permission ownership, child-session selection, and capturing the tool card id before acknowledgement. | Defaults to false/empty values. |
| `has_question_request`, `question_request_session` | Native-form ownership and child-session selection. | Defaults to false/empty values. |
| `respond_permission` | The approval card; return `QuestionResponseResult` after acknowledgement. | Explicit nonretryable unsupported result. Claude adapts its local `PermissionGate` boolean at this boundary. |
| `respond_question` | A native question card; return `QuestionResponseResult` after acknowledgement. | Explicit nonretryable unsupported result. |
| `can_drain` | The between-turns drain scheduler in `ciao/web/chat_streaming.py`: true only when the adapter permits a between-turn reader right now. | Defaults to `False`, and eligibility is dynamic, not a static capability: OpenCode reports true only with a live server, a session and outstanding background work, Claude additionally requires a connected client. The service forwards `drain_events` without re-checking this. |
| `cli_connected` | `_cli_owner_alive` in the chat manager, for the orphan-CLI-task wake sweep. | Defaults to `True`: an adapter that keeps no long-lived subprocess reports no owner loss. A service holding no provider instance reports `False`. OpenCode inherits this default — inherited `True` says nothing about whether a server is running. |
| `drain_events` | Claude and OpenCode, between turns, so a background agent or shell can wake the parent. | Defaults to an empty async generator with no side effects. The service forwards events unchanged — identity, order, exceptions and cancellation. Before starting another stream reader or a foreground turn, the caller cancels the running drain and awaits its full unwind; not every current host path is audited to enforce that obligation. |
| `read_thread` | OpenCode transcript history and replay (the host branch is still gated on the OpenCode id). The host reaches it through `provider_registry.require(id).factory()` for every session in the chat's lineage and renders the `{"info", "messages"}` payload. Claude history uses its own SDK/JSONL helpers instead. Session reclaim no longer routes through this row. | Returns `{}`; the host then falls back to the local transcript or archived markdown. |
| `is_connected` | Read paths that may query a live provider connection without starting one (MCP status, the live collab tree), and `live_for_session`. | Defaults to `False`. Claude returns its `_connected` flag; OpenCode returns whether its own server is running. Not `cli_connected`, which defaults to `True` and says nothing about a connection. |
| `read_mcp_status()` | MCP status rows, needs-auth detection and the Claude sign-in flow. | Defaults to `{}`. Claude and OpenCode override it and return `{}` without a live connection. |
| `reconnect_mcp_server(server_name)` | The Claude sign-in flow, after `claude mcp login` exits, so the live CLI picks up the token. | Defaults to `False` (nothing reconnected). Only Claude overrides it; OpenCode inherits the default and its flow never calls it. |
| `mcp_sign_in_exempt(server)` | `detect_mcp_auth`, which skips raising a sign-in card for an exempt server. | Defaults to `False`. Claude returns true for claude.ai connectors, which are connected on claude.ai. |
| `live_for_session(provider, session_id)` (module function) | Host guard for "this chat's provider is connected and bound to this session". | `False` for anything that is not a `BaseProvider`, is not `is_connected`, or has a different `current_session_id`. |
| `read_collab_tree(workspace_root, session_id)` (classmethod), `read_live_collab_tree(*, include_messages=False, only_child_id=None)` | Subagent panels and schedule waits: child sessions with history, from storage or from the live connection. | Defaults return `[]`. |
| `wait_background_change(timeout)` | The subagent watcher's poll wait between background-work reads. | Defaults to `asyncio.sleep(timeout)`. OpenCode returns early on a shell exit or child end. |
| `refresh_background_shells()`, `background_count`, `background_shell_rows()`, `background_child_ids()` | The background-process list and the Stop/restart gates. Rows are adapter-shaped, not normalised, like `model_catalog`. | Defaults return `0`, `0`, `[]`, `[]`. |
| `stop_background_task(task_id)`, `abort_session(session_id)` | Stopping one background task or aborting a provider-side session. | `False` and a no-op. |
| `interrupted_background_work()` | Shutdown record of work a restart loses: `{"children": [...], "shells": [{"id", "command"}]}`. The host checks `background_work_outstanding` first. | Defaults to `None`: nothing to record. |
| `native_title_candidates(workspace_root, session_id)` (classmethod) | `ProjectChatManager._native_chat_title`, tier 1 of the auto-title poll. | On `BaseProvider` the default returns `()` — no native title for that provider. |
| `delete_session(workspace_root, session_id)` (classmethod) | `ProjectChatManager._reclaim_provider_sessions_async`, the delete/archive/reset cleanup. | On `BaseProvider` the default returns `None` — an adapter inheriting it intentionally does no reclaim, and the host stays quiet. |
| `model_catalog(workspace_root, *, force=False)` (classmethod) | `ProjectChatManager._catalog_image_support` (image preflight, via `_model_capable`), `_capability_candidates`, and the `/api/models` assembly, all through `provider_registry`. | On `BaseProvider` the default returns `[]`: no dynamic catalog supplied by this operation. An overriding adapter may start a short-lived server. |

`native_title_candidates` is the shared native-title read. It is a
stateless classmethod on the adapter class — the manager resolves it through
the provider registry and never constructs a provider — scoped to a
workspace root (the chat's own agent root) and a persisted session id.
It returns ordered candidate titles in provider preference order, or an
empty tuple when the provider has none; there is no new capability flag,
availability is the empty default. The adapter owns its native storage and
payload interpretation and must not catch SDK failures (they land on the
manager's failure log); the host owns filtering, and rejects empty,
placeholder and context-contaminated candidates through
`chat_service._real_title` before a candidate can reach the sidebar.

`delete_session` is the shared session-reclaim seam, used by the same
stateless registry lookup. It returns `True` for affirmative confirmation, a
literal `False` to request the host's cleanup-warning log, or `None` for no
confirmation signal — an unsupported adapter inheriting the default
intentionally does no reclaim, and a best-effort cleanup may report nothing
without implying anything was deleted. The host never infers UI or retry
policy from these values: it warns only on `False` and otherwise continues,
and the manager swallows adapter failures with its fail-open per-session
boundary.

`model_catalog(workspace_root, *, force=False)` is the shared optional catalog
classmethod (#3178): call it on the adapter class, without constructing an
adapter instance or a `ProviderService`. The chat manager dispatches it
through `provider_registry` for image preflight and capability candidates
(keyed by the chat's provider id), and `/api/models` calls it on the
`opencode` descriptor. The inherited default returns a fresh `[]` and does no
work; `[]` means *no dynamic catalog is exposed by this operation*, not that
the provider cannot run a model and not that discovery completed.
`workspace_root` scopes adapter-native discovery and keyword-only
`force=True` asks the adapter to bypass its own cache; the base introduces no
cache and no framework-wide refresh policy. Rows are the adapter's internal
representation passed through unchanged — OpenCode's projection uses `model`
and `label` strings, `variants` as variant ids, and optional `images`
(absent means unknown); there is no shared wire schema, no sorting, filtering
or cross-provider id normalization. Override exceptions and cancellation
propagate unwrapped to the caller, which keeps its own policy (image
preflight treats any exception as unknown and therefore capable). Only the
inherited default is guaranteed no-start: an explicit override can launch a
short-lived server — OpenCode's catalog call owns an ephemeral
`opencode serve` process — so status polling, startup, drain eligibility,
Stop and live-session getters must not call it. Because the base default is
empty, an adapter that does not override it is never questioned about image
input.

`current_session_id` is a synchronized, read-only observation of the
session id the live adapter instance currently knows. It may be learned
during turn setup or mid-stream, so reads are dynamic and immediate —
`None → id → None` follows the adapter's answer with nothing cached — and
an adapter returning `""` keeps that exact value at the service boundary;
callers apply their own truthiness policy. A service without a provider
instance returns `None` without constructing one. The observation is
distinct from the stateless `native_title_candidates` and `delete_session`
classmethods, which read persisted sessions without an instance, and from
the permission-fence `response_session_id`. Because the base defines it as
a read-only property, an adapter owning mutable session state overrides it
with a getter over its own backing field, as both built-ins do. Two
deliberate compatibility edges are pinned in `tests/test_provider_service.py`:
a subclass that assigns `self.current_session_id = ...` without overriding
the inherited property now raises `AttributeError` (including during
construction), and a getter that raises `AttributeError` propagates to the
caller instead of being masked by the former duck-typed service probe.
These preservation claims cover the inspected built-ins only. The
remaining history, MCP, models, background and Stop paths and the other
session probes stay provider-specific; this seam does not make the
session lifecycle provider-neutral.

Permission and native-question replies use the shared `BaseProvider` response
contract, so any adapter can participate without a provider-specific manager
branch. The shared `ciao.providers.base.QuestionResponseResult` requires `ok`;
`error` defaults to `""`, `retryable` to `True`, and `delivered_as_message`,
`expired`, and `already_resolved` to `False`. Claude normalizes its local gate's
boolean result in its adapter; the gate itself retains its boolean API.

`structured_questions` covers intercepted `AskUserQuestion` events; it does
not promise native-form response support. Native forms are a separate optional
operation, and unsupported providers return the explicit default failure.
Existing capability flags remain unchanged. This typed manager seam does not
make unrelated session/history, drain/models, background, MCP/version or final
audit paths provider-neutral; those are separate changes.

Declare `capabilities = ProviderCapabilities(...)` honestly.
`capabilities_for` serves it on `/api/models`. The PWA type says the UI
gates on these flags. The Vue app does not read them yet, so a flag alone
does not hide a button or skip a backend branch.

## Call sites that still skip an unknown id

These are the reason a descriptor is not enough. Each one names `claude`
or `opencode`. An id that matches neither takes the skip. Walk the list
when adding a provider. Where a third copy of the same branch would
appear, put the behavior on the provider object instead of adding another
arm.

| If you do nothing | What the user sees | Where |
|---|---|---|
| Subagent routes return `[]`. | No child transcripts, and the sidebar shows no running agents. | The `chat.provider == "opencode"` gate in `ciao/web/routes_api.py` chat subagents and `_running_subagent_rows`. Inside it the reads already go through `live_for_session` and `read_collab_tree`. |
| The schedule archive wait returns "settled" immediately. | A schedule can archive while its background agents are still running. | The `chat.provider` gate in `ciao/web/schedule_dispatch.py` `_await_schedule_subagents`. Inside the opencode branch the reads go through `live_for_session` and `read_collab_tree`. |
| The subagent watcher falls through to the Claude JSONL reader. | Only if `background_subagents` is true. The watcher then looks for a Claude session file and finds nothing. | The `chat.provider` gate in `ciao/web/subagent_watchers.py` `watch_inner`. Inside the opencode branch the reads go through `live_for_session` and `read_collab_tree`. The CLI-task orphan sweep is Claude-only and skips everyone else, which is safe. |
| `disallowed_tools_for_chat` returns `[]`. | The harness denylist is not applied. Credential denies are also not applied unless the adapter sends them itself. | `ProjectChatManager.disallowed_tools_for_chat`. OpenCode applies the shared patterns from `execution_modes.opencode_credential_deny_rules` inside its own permission rules. |
| Image preflight does work only for providers that report a catalog with `images`. | A model that cannot see images receives them anyway. Claude and current Anthropic/OpenAI models all accept images, and the base `model_catalog` default is empty, so only OpenCode ever asks. | `ProjectChatManager._catalog_image_support`. |
| `native_title_candidates` is not overridden. | No native title: the chat keeps the deterministic title, then the one-shot titler. The read seam itself is id-neutral now — the remaining limitation is polling policy: `auto_title_if_default` still gives Claude one immediate native read and all other ids the existing delayed poll window. Polling neutrality is a separate follow-up. | `ProjectChatManager.auto_title_if_default`. |
| Transcript reload uses the local markdown only. | The chat bubble history still renders from Ciaobot's own transcript. Provider-side history that never landed there does not. | `ciao/web/transcript_service.py`. |
| Stop escalation disconnects only Claude. | A wedged CLI is not torn down on Stop unless `ActiveHandle.stop` already did it. | `ProjectChatManager` force-stop. |
| One-shot raises `Unknown one-shot provider`. | Titles, doc folding, and routine-model calls fail. | `ciao/providers/oneshot.py`. This is a separate no-tools call, not `run_streaming`. |
| Connect has no `<id>-login` action. | Settings → Connect answers 404. | `ACTION_IDS` and `action_command` in `ciao/host_terminal.py`; `tests/test_provider_connect_route.py::test_every_provider_has_a_login_action` fails until it is added. |
| Logout assumes `auth_command` is `[binary, auth, login]`. | Settings → Logout runs `[binary, auth, logout]`. A CLI with a different shape signs nobody out and can still exit 0. | `provider_connection_action` in `ciao/web/routes_api.py`. |
| `ciao auth --print-only` has a missing-binary fallback only for OpenCode. | The printed command errors when the binary is not installed. | `ciao/cli.py`. |

Two more branches are harmless for a new id and should stay that way.
Claude tier aliases (`haiku` / `sonnet` / `opus` / `fable`) are rewritten
to the provider's own default on every non-Claude id
(`_model_for_provider`). An empty model on a `ResultEvent` is written
back onto the chat only for OpenCode; other providers keep the model the
chat already stored.

## Assets

Both current CLIs read `AGENTS.md`. OpenCode also reads `.claude/skills`
and `.agents/skills` itself, so skill sync generates nothing for it.
What it does generate is `.opencode/agents/`, `.opencode/commands/`, and
the `mcp` object in `opencode.json` (`ciao/sync_skills.py`).

Before writing a projection, check whether the new CLI already loads
`AGENTS.md` and one of those skill directories. If it does, follow
OpenCode and project only the surfaces it does not discover (commands,
subagents, MCP). `ciao/web/commands.py` `_provider_command_dir` is the
slash-command half of that. System skills shown in Settings come from
the descriptor's `system_skills_path`. The function also feeds the slash
picker's provider skills and must be cheap or cached, since `/api/commands`
calls it (opencode caches a private-server read for 300 s).

Claude Code hides harness surfaces the PWA already owns (plan mode, its
own scheduler, notifications). The trim catalog is `TRIM_OPTIONS` in
`ciao/claude_trim.py`: tool denies, bundled-skill overrides and `disable*`
settings, each with its default. A
new CLI that ships the same tools needs the equivalent, in its own
permission language. Deny rules are not a sandbox: the shell can still
read a path the file tool refuses. See `docs/AGENT_CLI.md`.

## Optional, ship only if the CLI has the feature

- **Conversation import.** `ciao/import_sources/` turns one CLI's stored
  sessions into a `NormalizedSession`. Everything after that is
  provider-blind. A new source is a new module registered beside
  `claude_code` and `opencode`, plus a `KNOWN_PROVIDERS` entry. Chat
  works without it.
- **Quota.** Claude reports a rate-limit snapshot. OpenCode reports none
  (`quota=False`) because there is no single account to measure.
- **Dynamic model catalog.** `model_catalog(workspace_root, *, force=False)`
  (classmethod, optional) is the shared operation for the models a provider
  can currently reach; the base default answers `[]`. OpenCode overrides it:
  its `_fetch_model_catalog` reads the V2 `/api/model` snapshot only — already
  filtered to enabled models, so it deliberately does not join a separately
  timed `/api/provider` snapshot — and addresses models as
  `providerID/modelID`, with its own cache, keyword-only `force` refresh and a
  short-lived `opencode serve` process while it reads. The `/api/models`
  response assembly calls it through `provider_registry.require("opencode")`,
  and image preflight dispatches it by the chat's provider id. A fixed catalog can
  live on the descriptor's `default_model` instead.

## Docs, site, and the weekly probe

A working adapter is not the whole change. These name the two providers
in user-facing text:

- `INTEGRATIONS.md` install and login.
- `docs/LINUX.md` and the provider cards in
  `site/guide.html`.
- The stock `ciao-capabilities` skill.
- `.github/workflows/discover-agents.yml`, which installs both CLIs and
  runs one real turn on macOS. A third CLI belongs there
  once it is supported, not before.

`docs/UPKEEP.md` has the OpenCode 2.x contract. A new provider with a
version floor gets its own row. Do not relax the OpenCode floor while
adding something else.

## Tests

Replay captured native events into `StreamEvent`s with no network. That
is `tests/test_opencode_provider.py` and the Claude provider tests.
Include a fixture that carries no credentials.

Then one real turn against the installed CLI, the same bar `AGENTS.md`
sets for OpenCode changes. The turn has to exercise the pieces the
fixtures cannot: login state, a permission card answered from the PWA,
a question card, Stop, and a reload that still shows the transcript.

Before pushing, run the gates in `AGENTS.md`: `mypy ciao`,
`pytest -n auto tests/`, and, if the picker or Settings changed,
`cd web && npm test` and `npm run build`.

## Order of work

1. Confirm the CLI can resume a session, stop a turn, and run a shell
   command. If it cannot resume, `capabilities.resume` stays false and
   every chat is one-shot. If it cannot run a shell command, stop.
2. Add the descriptor and an adapter that streams one text turn to a
   `ResultEvent` with a session id.
3. Add permission replies, question replies, and Stop. Wire the skip-list
   rows whose feature you claim to support, and leave the others
   explicitly unsupported in `ProviderCapabilities`.
4. Add the one-shot path so titles and doc folds work.
5. Decide asset discovery versus projection.
6. Replay fixtures, then run one real turn.
