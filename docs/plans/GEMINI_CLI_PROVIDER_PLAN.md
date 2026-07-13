# Google Gemini CLI provider plan

Status: implementation plan with strict feasibility gate  
Research date: 2026-07-10  
Target repository: `raffaelefarinaro/ciaobot`

## Verdict

Gemini CLI can be added as a first-class Ciaobot provider and users can authenticate with the Google account attached to a Google AI Pro or Ultra subscription. Its official ACP mode provides durable sessions, streamed messages and thoughts, tool state, images, approval requests, cancellation, model/mode controls, and session usage.

**Exact parity with the current Claude integration is not possible through Gemini CLI's supported interfaces today.** Two current Ciaobot behaviors have no equivalent in Gemini ACP:

1. **Non-destructive same-turn steering.** ACP exposes `session/prompt` and `session/cancel`, but no steer/append-to-active-turn method. Gemini CLI's current ACP implementation aborts a pending prompt when another prompt is started. Ciaobot can queue a follow-up for the next turn, but that is not the same behavior as Claude's live `client.query()` or Codex `turn/steer`.
2. **Claude-style background subagent lifecycle and transcript access.** Gemini subagents run as synchronous tools with independent context and return a result to the parent. ACP exposes their tool activity, but not a supported background-agent API with continued completion after the parent turn, per-agent message retrieval, and Ciaobot's post-turn drain semantics.

A third item must be proven before release: Gemini's `ask_user` tool supports structured choice, text, and yes/no input in the interactive CLI, but the current ACP documentation does not promise a structured question request/response contract equivalent to Ciaobot's question card. Treat this as an acceptance spike, not an assumption.

Therefore this document defines two outcomes:

- **Strict outcome:** stop after Phase 0 unless the installed Gemini CLI has gained the missing ACP capabilities. Do not label the provider exact-parity.
- **Practical outcome:** with explicit product acceptance, ship core functional parity using queued next-turn follow-ups and inline subagent tool activity, with the unavailable capabilities declared in the UI/provider metadata.

This limitation does not prevent subscription login or a useful Gemini provider. It prevents an honest claim of exact Claude feature parity.

## Supported scope

The practical implementation can provide:

- Google subscription login and cached local credentials.
- Install, version, readiness, update, and logout/setup diagnostics.
- New, persisted, resumed, handed-over, archived, and reset chats.
- Streamed assistant text, thought summaries, tool calls/results, errors, and usage.
- Image input.
- PWA approve/deny handling for gated tools.
- Stop/cancel.
- Dynamic model selection and the provider's available execution modes.
- Ciaobot transcript replay, file snapshots, schedules, quota-error retry, and provider handover.
- Static/dynamic Ciaobot instructions, memory, and runtime context.
- Workspace skills, native Gemini slash commands, and native Gemini subagent definitions.
- Inline subagent status as ordinary tool activity.

It cannot currently provide exact same-turn steer or Claude-equivalent background subagent panels/message histories.

## Parity matrix

| Ciaobot behavior | Gemini surface | Verdict / decision |
| --- | --- | --- |
| Subscription login | Sign in with Google using the subscription account | Supported for individual Google AI Pro/Ultra accounts; organization accounts may require a Cloud project. |
| Persistent chat | ACP `newSession` and `loadSession`; automatic local session recording | Supported. Persist the Gemini session ID immediately. |
| Fork | No first-class ACP fork method | Implement Ciaobot handover/new session from its visible transcript; mark provider-native fork unsupported. If current UI exposes fork separately, this is a parity gap. |
| Text streaming | ACP `agent_message_chunk` | Supported. |
| Reasoning streaming | ACP `agent_thought_chunk` | Supported for display-safe thoughts/summaries supplied by Gemini. |
| Tool lifecycle | ACP `tool_call` and `tool_call_update` | Supported. |
| Images | ACP prompt image capability | Supported after existing Ciaobot path/size validation. |
| Stop | ACP `cancel` | Supported. |
| Mid-turn follow-up | No ACP steer; a new prompt aborts the pending prompt | **Not supported.** Queue for the next turn only in practical-parity mode. |
| Tool approval | ACP client `requestPermission` callback | Supported. Translate selected provider options to Ciaobot approve/deny. |
| Structured question | Interactive `ask_user`; ACP contract not established | Acceptance spike required. Do not claim parity until choice and free-text round trips pass. |
| Modes | ACP `setSessionMode` with modes advertised by session | Supported. Map by advertised IDs, not assumed order. |
| Models | ACP session model metadata and `unstable_setSessionModel` | Supported but the setter is explicitly unstable; capability-test each version. |
| Usage/context | prompt response metadata and saved session statistics | Supported for values exposed by the installed build. |
| Quota/reset time | provider errors and per-model usage; no guaranteed ACP rate-limit snapshot | Partial. Retry classified 429/quota errors; show reset time only if supplied. |
| Transcript replay | ACP `loadSession` plus Ciaobot transcript | Supported. |
| File snapshots | tool-call updates/content and Gemini session data | Supported when changed paths can be identified; otherwise compare the existing pre/post snapshot set. |
| Skills | `.gemini/skills/` or shared `.agents/skills/` | Supported. Activation consent must pass through permissions. |
| Commands | project `.gemini/commands/*.toml` | Supported via deterministic conversion from canonical Ciaobot Markdown. |
| Subagent execution | project `.gemini/agents/*.md`, exposed as tools | Supported synchronously. |
| Background subagent tracking/messages | no supported ACP background-agent lifecycle/transcript API | **Not supported.** Inline tool status only. |
| Schedules | ACP session in an unattended mode | Supported with interaction-safe failure handling. |

## Architecture

### 1. Land the provider-neutral foundation once

If the Codex plan is implemented first, reuse its Phase 1 rather than duplicating it. Otherwise this Gemini implementation must land the same foundation:

1. Replace the hard-coded `ClaudeProvider` construction in `ciao/provider_service.py` with a provider registry/factory.
2. Add `ProviderCapabilities` in `ciao/providers/base.py`, including explicit flags for steer, fork, structured questions, background subagents, subagent messages, quota snapshots, and schedule safety.
3. Make chat, history, subagent, new-session, schedule, and setup routes call provider methods instead of Claude SDK helpers.
4. Add `ciao/providers/stdio_rpc.py`, an asynchronous JSON-lines peer supporting request/response, notifications, server-initiated permission requests, timeouts, process failure, cancellation, and bounded logs. Gemini's envelope is JSON-RPC 2.0; keep its codec separate from Codex's variant.
5. Migrate `ciao/sessions.py` to per-provider session/usage/quota/cost state while preserving current v3 Claude data.
6. Make provider/model/mode metadata dynamic through the API and frontend.
7. Add capability-aware UI behavior: do not render a “steer now” affordance or background-subagent promise for a provider that does not support it. Queued messages remain available.
8. Extract the current Claude `UserPromptSubmit` runtime/entity injection into a provider-neutral builder, preserving date, active workspace/project, GWS profile, cwd, and vault entity tags for every provider.

Keep `ChatInfo.provider` authoritative and preserve Ciaobot's visible transcript across provider handovers.

### 2. Implement `GeminiProvider` over ACP

Create `ciao/providers/gemini.py`. Use `gemini --acp` over stdio. Do not scrape the Ink terminal UI and do not use headless `-p --output-format stream-json` for interactive chats: headless mode turns `ask_user` policy into deny and cannot route approvals back to the PWA.

Use one Gemini ACP subprocess per active Ciaobot chat. This isolates failures and works around the current absence of ACP `session/close`/`session/delete`: disconnecting the chat process releases its in-memory session resources, while a later process can `loadSession` from persisted history.

Lifecycle:

1. Resolve `CIAO_GEMINI_BIN`, then the recorded managed install, then `PATH` through `ciao.tool_path`.
2. Spawn `gemini --acp` with the chat workspace as `cwd` and complete ACP `initialize`.
3. Advertise only Ciaobot client capabilities actually implemented. In particular, omit ACP filesystem proxy capabilities so Gemini uses its local tools behind its normal policy/permission engine; do not accidentally promise a filesystem service Ciaobot does not enforce.
4. Authenticate using the CLI's selected cached auth type. On a new chat call `newSession`; on a known ID call `loadSession` and replay its updates only into provider restoration, not as duplicate visible user turns.
5. Persist the session ID before starting the first model prompt.
6. Send visible text as ACP text content and validated images as ACP image content.
7. Send Ciaobot's generated memory and per-turn runtime block as ACP `embeddedContext` if the negotiated prompt capability supports it. If it does not, prepend a clearly delimited application-context text block without changing the visible Ciaobot transcript.
8. Apply the selected session mode and model before the prompt. Reject unsupported values instead of letting the CLI silently select something else.
9. Normalize `session/update`, permission requests, and prompt completion into existing Ciaobot events.
10. `ActiveHandle.stop()` calls ACP cancel. In practical-parity mode, `steer()` returns unsupported and `ProviderService` keeps the user message in the existing next-turn queue; it must never send a second ACP prompt that aborts the active work.
11. On shutdown, cancel pending prompts, resolve permission requests as deny, terminate the process within a bounded timeout, and preserve the session ID for resume.

As with Claude, retry only before any visible/model/tool event. A process failure after a possible side effect must not replay the turn.

### 3. Mode and policy mapping

Gemini sessions advertise their available modes. Prefer IDs reported by `newSession`/`loadSession`, with this intended mapping when those standard Gemini modes are present:

| Ciaobot mode | Gemini mode | Meaning |
| --- | --- | --- |
| `normal` | `default` | Ask according to Gemini's normal policy. |
| `plan` | `plan` | Read-only/planning behavior. |
| `auto` | `autoEdit` | Automatically apply edits while retaining other safety prompts. |
| `bypass` | `yolo` | Broad automatic execution. |

If an advertised ID differs, map by explicit metadata/config, not by array position. If a mode is missing, disable that option for Gemini and explain why.

Do not rely on project `.gemini/policies` until an installed-build conformance test proves workspace-tier policy rules are active; the official policy documentation currently warns that this tier is non-functional. Use the ACP session mode plus user/admin policy behavior. Add provider-specific workspace deny policy rather than translating Claude `disallowed_tools` names blindly.

For schedules, a permission or `ask_user` request with no live operator must produce a visible `needs_user` job state. Never auto-approve merely because the request came from a schedule.

### 4. Event and permission normalization

Implement a fixture-tested translator:

- `agent_message_chunk` -> `AssistantTextDelta`.
- `agent_thought_chunk` -> `ThinkingEvent`.
- `tool_call` -> `ToolUseEvent` start with a stable tool-call ID.
- `tool_call_update` -> running/completed/failed `ToolUseEvent`; retain sanitized diff/path/output content needed by file snapshots.
- prompt stop reason -> `ResultEvent` error/success/interrupted state.
- response `_meta` token/model usage -> `TokenUsageEvent` and final usage metadata.
- auth, model, or quota errors -> classified `ResultEvent` with an actionable setup/retry category.

ACP permission callback handling:

1. Map Gemini's permission request ID to `PermissionRequestEvent.request_id`.
2. Present a sanitized tool name and input summary. Never publish environment variables, OAuth payloads, or full command output in push notifications.
3. On approve/deny, choose the matching provider option returned in the request; do not invent a global “always allow” decision when the PWA only asked for one approval.
4. Resolve every pending request exactly once. Stop, disconnect, prompt completion, and process exit deny unresolved requests and remove replay cards.

Structured `ask_user` handling is a release gate. The adapter must demonstrate access to the raw question schema and return the user's selected/free-text answers without converting the question into a generic allow/deny card. If the installed ACP build exposes only a permission prompt and not the question/answer payload, mark `structured_questions=false` and fail strict parity.

### 5. Models, usage, and quota

- Populate Gemini model options from ACP session `availableModels`/configuration metadata. Do not hard-code current Gemini model IDs.
- Apply model changes through the negotiated method. Because `unstable_setSessionModel` is unstable, keep its wire shape isolated in `GeminiProvider` and cover it with version fixtures.
- Store the provider-confirmed current/effective model.
- Preserve token input/output/cached/thought/tool usage when returned.
- Calculate context percentage only when both used tokens and a trustworthy context limit are supplied; otherwise omit it rather than guessing.
- Classify documented quota/rate-limit errors for the existing hourly retry path. Do not fabricate reset timestamps.
- Update the frontend's Claude-only `ProviderKey`, bucket definitions, and thinking maps to consume provider sections and capabilities from `GET /api/models`.

### 6. Authentication, installation, and update handling

Official Gemini CLI requires Node.js 20+ and supports npm and Homebrew installation. Ciaobot should support both while recording the install mechanism.

1. Settings states: `not installed`, `runtime missing`, `installed / login required`, `ready`, `incompatible`, `organization setup required`, and `error`.
2. Installation is an explicit operator action:
   - npm: `npm install -g @google/gemini-cli@latest`;
   - Homebrew: `brew install gemini-cli`.
3. Do not use `npx` per chat. It adds network startup, mutable versions, and an unauditable execution path.
4. Record only install mechanism, resolved executable, Node executable, and version in `.runtime/provider-installs.json`; never credentials.
5. `ciao auth gemini` launches the resolved interactive `gemini` command in a real terminal so the user can select **Sign in with Google** and complete browser OAuth. For a Google AI Pro/Ultra subscriber, the user must choose the subscription account.
6. Where reliable, a Settings login flow may use ACP `initialize`/`authenticate`; otherwise show/launch the official interactive flow. Do not parse or copy cached OAuth token files to determine identity.
7. Readiness uses a bounded ACP initialize/new-session probe that does not submit a model prompt or consume a user turn. Return organization/Cloud-project errors verbatim after secret/path sanitization.
8. Update through the recorded mechanism (`npm install -g ...@latest` or `brew upgrade gemini-cli`) only when no Gemini process is active.
9. Pin to the stable release channel. Preview/nightly builds are opt-in and unsupported for parity claims.

Capability-test the installed CLI after install/update. Required practical capabilities are initialize/authenticate/newSession/loadSession/prompt/cancel, permission callbacks, image and embedded-context prompt capability, session modes, model selection, message/thought/tool updates, and usage. Strict parity additionally requires steer, structured questions, fork if exposed in Ciaobot, and background-subagent inspection. Disable the provider with an update message when required practical capabilities are missing.

Update `ciao/setup_status.py`, `ciao/cli.py`, `ciao/main.py`, `ciao/upgrade.py`, setup/settings APIs, and `LoginView.vue`, `SettingsView.vue`, and `StartupView.vue`.

### 7. Workspace instructions and asset synchronization

Canonical, editable Ciaobot sources stay provider-neutral:

- `skills/<name>/SKILL.md`
- `commands/<name>.md`
- `subagents/<name>.md`

Extend `ciao/sync_skills.py` into a provider-aware projection while keeping the existing command compatible.

#### Instructions

- Keep root `CLAUDE.md` for Claude.
- Create/update root `GEMINI.md` with the Ciaobot workspace guide or a safe import of a provider-neutral Ciaobot instruction file.
- Preserve user-authored content. A generated block/file must carry a Ciaobot marker; conflict must produce a workspace-health warning, not overwrite unrelated instructions.
- Do not use `GEMINI_SYSTEM_MD` to replace Gemini's built-in system prompt: official docs warn that a custom system prompt removes the original core instructions unless reconstructed. Use `GEMINI.md` plus ACP embedded application context.

#### Skills

- Mirror canonical skills to `.agents/skills/<name>` using relative links. Gemini and Codex both recognize this workspace alias; Claude continues using `.claude/skills`.
- Build a Ciaobot-managed staging inventory from workspace `skills/`, packaged stock skills, and the entries in `skills-lock.json`, then project that inventory to `.claude/skills` and `.agents/skills`. Do not leave locked upstream skills visible only to Claude.
- Retain precedence as workspace > packaged stock > locked upstream, marker-based safe pruning, and exclusion of unrelated user-global/provider-global skills.
- Skill activation requires user consent in Gemini. Ensure its ACP permission request reaches the PWA.
- Validate discovery using the installed Gemini skill listing where possible and report invalid/missing projections in workspace health.

#### Commands

Generate `.gemini/commands/<name>.toml` from each canonical Markdown command:

- frontmatter `description` -> TOML `description`;
- Markdown body -> TOML multiline `prompt`;
- `$ARGUMENTS` -> `{{args}}`;
- `argument-hint` remains Ciaobot UI metadata because Gemini's command file does not need the same field.

Escape TOML safely, reject ambiguous positional placeholders, preserve the exact command name, and remove only marked generated files. Also add the common Ciaobot pre-dispatch command resolver so `/remember` behaves consistently even if a provider's native parser changes; keep the original command text visible in the transcript.

#### Subagents

The existing canonical files already use YAML frontmatter plus a Markdown system prompt, which matches Gemini's project agent format closely.

- Generate `.gemini/agents/<name>.md` with `name`, `description`, `kind: local`, and the existing body.
- Omit `tools` and `model` unless canonical provider-specific metadata is explicitly set; omission inherits the parent safely and avoids invalid Claude-to-Gemini tool translation.
- Add optional `providers.gemini` metadata later for Gemini tool/model/turn/timeout restrictions.
- Mark and safely prune only generated copies.
- Verify the agent appears through Gemini's agent discovery/listing.

#### MCP servers and connectors

- Do not copy `~/.claude`, `~/.gemini`, OAuth caches, connector configuration, or MCP credentials between providers.
- Ciaobot currently has no provider-neutral MCP registry. Synchronize Ciaobot-owned instructions, skills, commands, and subagents only.
- A future workspace-owned MCP registry needs explicit Gemini/Codex/Claude compilers and secret references; provider tool names are not a safe translation layer.

Update `ciao/web/agent_assets.py`, Settings descriptions, and workspace health so they display canonical assets and per-provider projection state rather than only `.claude` paths.

### 8. History, reset, subagents, and schedules

History and reset:

- Ciaobot's transcript stays canonical for visible UI replay and cross-provider handover.
- ACP `loadSession` restores Gemini context; suppress replay duplicates when it streams stored history.
- Gemini records sessions under its user config area. Do not copy global session files wholesale or expose their project hashes/paths.
- Archive Ciaobot data under `Logs/Chats/<chat-id>/gemini/`.
- Reset archives the Ciaobot transcript, cancels/terminates the per-chat ACP process, clears the session ID, and starts a new session next turn. Provider-side deletion is optional; current ACP lacks a standard close/delete lifecycle, so do not make deletion a prerequisite.

Subagents in practical-parity mode:

- Render Gemini agent invocation as an ordinary tool row with the agent name, running/completed/failed state, and returned result.
- Do not increment the existing “background agents still running” counter after the parent prompt ends.
- Do not invent subagent messages from parent tool text or read Gemini's private session files as an undocumented API.
- Set `background_subagents=false` and `subagent_messages=false` in capabilities so schedules skip Claude's post-turn background-agent drain.

Schedules:

- Reuse the ACP provider instead of headless `stream-json` so behavior stays consistent.
- Apply the schedule's mapped mode. If permission/input is required with no live user, terminate as `needs_user`, retain the chat, and never hang the job.
- Preserve autoarchive and retry behavior. Retry only classified quota/rate-limit failures.

## Concrete change set

Expected existing files to modify:

- `ciao/models.py`
- `ciao/providers/base.py`, `ciao/providers/__init__.py`
- `ciao/provider_service.py`
- `ciao/web/project_chats.py`, `ciao/web/chat_broker.py`, chat/history/subagent routes
- `ciao/config.py`, `ciao/sessions.py`, `ciao/setup_status.py`
- `ciao/cli.py`, `ciao/main.py`, `ciao/upgrade.py`, `ciao/tool_path.py`
- `ciao/sync_skills.py`, `ciao/skills_inventory.py`, `ciao/web/agent_assets.py`
- the runtime/entity context helper currently implemented in `ciao/observability/hooks.py`
- stock workspace guide/customization assets
- `web/src/lib/types.ts`, `api.ts`, `modelSections.ts`
- `ChatPanel.vue`, `ModelSelector.vue`, `LoginView.vue`, `SettingsView.vue`, `StartupView.vue`, schedules, and `SubagentPanel.vue`

Expected new files:

- shared `ciao/providers/stdio_rpc.py` if the Codex work has not already added it
- `ciao/providers/gemini.py`
- Gemini ACP protocol fixtures and focused tests under `tests/`
- generated stock templates/converters for `GEMINI.md`, commands, and agents as needed

Do not add the Gemini CLI npm package to Ciaobot's frontend `web/package.json`; it is an external provider executable, not a browser dependency.

## Delivery sequence and hard gates

### Phase 0: authenticated conformance spike

Use a disposable workspace and an authenticated Google subscription account. Prove and save sanitized fixtures for:

- initialize/authenticate/new/load session;
- text/thought/tool streaming and prompt completion;
- image and embedded-context input;
- edit/shell/skill approval round trips;
- cancel;
- mode and model changes;
- usage and quota-error classification;
- native project command, skill, and subagent discovery;
- `ask_user` choice and free-text question/answer round trips.

Then explicitly test the known gaps:

- send a follow-up during an active prompt and verify whether a new supported steer method exists without cancelling the active prompt;
- start a background subagent, let the parent turn end, and verify whether ACP exposes continued lifecycle plus independent message retrieval;
- verify whether a provider-native session fork exists if Ciaobot intends to expose fork for Gemini.

If these fail, record the capability flags as false. **If the product requirement is still exact parity, stop here.** Continue only after the user/product owner accepts practical parity or upstream Gemini adds the missing APIs.

### Phase 1: provider-neutral core

Land registry, capabilities, state migration, provider-delegated routes, dynamic metadata, and RPC transport. Keep all Claude tests green.

### Phase 2: Gemini ACP adapter

Land session lifecycle, event mapping, images, approvals, cancel, model/mode, usage, history, and safe error handling behind a disabled-by-default feature flag.

### Phase 3: setup and workspace assets

Land install/auth/update/status plus GEMINI instructions, shared skills, TOML commands, and Gemini agent projection. Prove idempotent sync and safe conflicts/pruning.

### Phase 4: PWA and schedules

Land provider/model selection, handover, capability messaging, status cards, approvals/questions where supported, inline subagent tools, and schedules. Enable only the scope proven by Phase 0.

## Required tests

Backend:

- ACP JSON-RPC correlation, server callbacks, timeout, malformed line, stderr, process exit, and cancel.
- All update/event mappings using sanitized fixtures.
- No replay after first visible/tool event.
- New/load/reset/handover persistence and duplicate-history suppression.
- Images and path traversal rejection.
- Permission approve/deny/duplicate/disconnect/turn-end cleanup.
- `ask_user` choice/free-text round trip or an explicit unsupported-capability test.
- Active-turn follow-up queues without sending a destructive second prompt.
- Mode/model discovery and unsupported-value rejection.
- Usage and quota-error retry without invented reset time.
- Sync conversion for `GEMINI.md`, skills, command TOML, and agent Markdown; idempotence, conflicts, marker-only prune.
- Runtime/entity context equivalence with Claude, using ACP embedded context without duplicating it in the visible transcript.
- Synchronous subagent tool rendering and explicit absence of background drain.
- Schedule success, `needs_user`, stop, retry, and autoarchive.
- state migration and unchanged Claude behavior.
- install/status/auth command construction without real credential access.

Frontend:

- Gemini selection and handover confirmation.
- Dynamic model/mode controls.
- Provider status/install/auth states.
- Approval and structured question cards when supported.
- Queued-follow-up copy for no-steer capability.
- Inline Gemini subagent tool rendering without false background state.
- Capability disclosure in Settings/help.

Final verification:

```text
pytest tests/
cd web && npm run build
```

Run the authenticated Gemini smoke suite only when explicitly enabled; normal CI must skip it when no cached Google login exists.

## Definition of done

For practical parity:

- A fresh user can install Gemini CLI, sign in with the Google account attached to an eligible subscription, select Gemini in Ciaobot, and complete a tool-using chat without an API key.
- Restarting Ciaobot resumes the same Gemini session.
- Text/thought/tools, images, approvals, stop, models, modes, usage, transcript, file snapshots, handover, schedules, skills, commands, and synchronous subagents pass.
- Unsupported steer/background-subagent behavior is capability-gated and accurately described.
- No tokens, cached auth files, account identifiers, private paths, or private session contents enter the repository, fixtures, logs, or generated workspace assets.
- Claude behavior remains unchanged.

For exact parity, definition of done additionally requires upstream-supported, tested non-destructive same-turn steering, provider-native fork if exposed, structured question answers, and background subagent lifecycle/message retrieval. As of the research date, that definition cannot be met.

## Official sources

- [Gemini CLI installation and releases](https://geminicli.com/docs/get-started/installation/)
- [Gemini CLI authentication](https://geminicli.com/docs/get-started/authentication/)
- [Gemini CLI ACP mode](https://geminicli.com/docs/cli/acp-mode/)
- [Gemini CLI headless mode](https://geminicli.com/docs/cli/headless/)
- [Gemini CLI session management](https://geminicli.com/docs/cli/session-management/)
- [Gemini CLI custom commands](https://geminicli.com/docs/cli/custom-commands/)
- [Gemini CLI Agent Skills](https://geminicli.com/docs/cli/using-agent-skills/)
- [Gemini CLI subagents](https://geminicli.com/docs/core/subagents/)
- [Gemini CLI Ask User tool](https://geminicli.com/docs/tools/ask-user/)
- [Gemini CLI policy engine](https://geminicli.com/docs/reference/policy-engine/)
- [Gemini CLI ACP implementation](https://github.com/google-gemini/gemini-cli/blob/main/packages/cli/src/acp/acpClient.ts)
- [Agent Client Protocol](https://agentclientprotocol.com/)

Implementation must negotiate the installed CLI rather than assume that today's ACP surface is permanent. Re-run the Phase 0 gap tests on each minimum-version bump; an upstream addition could make strict parity possible later.
