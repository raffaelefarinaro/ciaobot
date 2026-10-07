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
- Settings → Providers connection cards, including the macOS Terminal
  login launcher (`POST /api/providers/{id}/connect`).
- Startup connection checks for every descriptor that has a status probe,
  except Claude, which is probed on its own path (`ciao/main.py`).
- `/api/models`, which ships each provider's `ProviderCapabilities`.

`ciao/provider_service.py` constructs the class and calls
`run_streaming`. It does not know how the CLI works.

## The turn contract

Implement `ciao.providers.base.BaseProvider`. The chat manager also calls
the methods in the second table through `getattr`. A missing method is a
silent no-op for that feature, not a startup error.

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
| `ResultEvent` | Every turn ends on one. Carry `session_id`, `effective_model`, `is_error`, `stopped` (a user Stop is not an error), `usage`, and `quota` when the CLI has it. `recovered_with_pending` means a permission or question card is still unanswered. |

### Methods the chat manager already calls

| Method | Who calls it | If it is absent |
|---|---|---|
| `disconnect()` | Session reset, delete, idle reap, stop escalation. | The process is leaked. On `BaseProvider` the default is a no-op. |
| `ActiveHandle.stop()` | The Stop button. | Registered from `run_streaming`. |
| `current_session_id` | Permission and question replies. | The reply cannot find the live session. |
| `steer(request)` | A message sent while a turn is running. | The message waits for the next turn. |
| `send_permission_response` / `permission_gate` | The approval card. | The card cannot be answered. Claude uses `PermissionGate`; OpenCode posts to the session. |
| `send_question_response` | A native question card. | Same, for questions. |
| `can_drain` / `drain_events` | Claude, between turns, so a background agent can wake the parent. | No between-turns drain. OpenCode reports `can_drain` false. |
| `read_thread` / `delete_thread` | OpenCode titles and session reclaim. Claude uses its own JSONL helpers instead. | See the skip list below. |

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
| Session delete has no reclaim arm. | The native session file or server-side thread is left behind when the chat is deleted. | `ProjectChatManager` session cleanup: Claude deletes the SDK JSONL blob, OpenCode calls `OpencodeProvider.delete_thread`. |
| Subagent routes return `[]`. | No child transcripts, and the sidebar shows no running agents. | `ciao/web/routes_api.py` chat subagents and `_running_subagent_rows`. |
| The schedule archive wait returns "settled" immediately. | A schedule can archive while its background agents are still running. | `ciao/web/schedule_dispatch.py` `_await_schedule_subagents`. |
| The subagent watcher falls through to the Claude JSONL reader. | Only if `background_subagents` is true. The watcher then looks for a Claude session file and finds nothing. | `ciao/web/subagent_watchers.py` `watch_inner`. The CLI-task orphan sweep is Claude-only and skips everyone else, which is safe. |
| `disallowed_tools_for_chat` returns `[]`. | The harness denylist and the MCP allowlist are not applied. Credential denies are also not applied unless the adapter sends them itself. | `ProjectChatManager.disallowed_tools_for_chat`. OpenCode applies the shared patterns from `execution_modes.opencode_credential_deny_rules` inside its own permission rules. |
| Image preflight runs only for OpenCode. | A model that cannot see images receives them anyway. Claude and current Anthropic/OpenAI models all accept images, which is why the check is OpenCode-only. | `ProjectChatManager._opencode_image_support`. |
| Native title read returns `None`. | The chat keeps the deterministic title, then the one-shot titler. The poll window is Claude's instant poll only when the id is `claude`; every other id waits out the OpenCode delays first. | `ProjectChatManager._native_chat_title`. |
| Transcript reload uses the local markdown only. | The chat bubble history still renders from Ciaobot's own transcript. Provider-side history that never landed there does not. | `ciao/web/transcript_service.py`. |
| Stop escalation disconnects only Claude. | A wedged CLI is not torn down on Stop unless `ActiveHandle.stop` already did it. | `ProjectChatManager` force-stop. |
| One-shot raises `Unknown one-shot provider`. | Titles, critique, doc folding, and routine-model calls fail. | `ciao/providers/oneshot.py`. This is a separate no-tools call, not `run_streaming`. |
| Critique's availability map has no entry. | The review panel skips the provider. | `ciao/critique.py` `default_critique_panel`. |
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
the descriptor's `system_skills_path`.

Claude Code hides harness surfaces the PWA already owns (plan mode, its
own scheduler, notifications). The deny list is `HARNESS_DISABLED_SKILLS`
in `ciao/execution_modes.py` and the tool denies in `ciao/config.py`. A
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
- **Dynamic model catalog.** OpenCode joins its `/api/provider` and
  `/api/model` snapshots and addresses models as `providerID/modelID`.
  A fixed catalog can live on the descriptor's `default_model` instead.

## Docs, site, and the weekly probe

A working adapter is not the whole change. These name the two providers
in user-facing text:

- `INTEGRATIONS.md` install and login.
- `docs/WINDOWS.md`, `docs/LINUX.md`, and the provider cards in
  `site/guide.html`.
- The stock `ciao-capabilities` skill.
- `.github/workflows/discover-agents.yml`, which installs both CLIs and
  runs one real turn on macOS and Windows. A third CLI belongs there
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
4. Add the one-shot path so titles and critique work.
5. Decide asset discovery versus projection.
6. Replay fixtures, then run one real turn.
