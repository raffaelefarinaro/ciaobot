# OpenAI Codex CLI provider plan

Status: implemented and verified (2026-07-11)  
Research date: 2026-07-10  
Target repository: `raffaelefarinaro/ciaobot`

## Verdict

Adding Codex as a first-class Ciaobot provider is feasible. A user can authenticate with **Sign in with ChatGPT** and consume the Codex entitlement included in an eligible ChatGPT subscription; an API key remains an optional alternative. Ciaobot should integrate with `codex app-server`, not scrape the interactive terminal and not use one-shot `codex exec`.

The app-server protocol exposes the primitives Ciaobot needs: durable threads, resume and fork, streamed text and reasoning, tool lifecycle, images, approval requests, structured user questions, cancellation, same-turn steering, model and reasoning discovery, usage and rate limits, and multi-agent events. This is enough for user-visible parity with the current Claude provider.

Two qualifications must remain visible in release notes:

1. Feature parity does not mean identical provider behavior. Codex has its own models, sandbox, approval policies, reasoning levels, quota rules, and multi-agent implementation.
2. The installed Codex build used during this investigation labels `app-server` as experimental even though OpenAI documents it as the rich-client integration surface. Ciaobot must capability-test the installed protocol and fail closed on an incompatible build. Do not pin the implementation to the investigator's locally bundled ChatGPT-app binary.

## Non-negotiable scope

The implementation is complete only when a Codex chat can do everything below through the existing Ciaobot UI and persistence layer:

- Start, resume, fork, hand over, archive, and reset a durable chat.
- Stream assistant text, reasoning summaries, tool state, command output, and failures.
- Accept local image inputs.
- Stop a running turn and inject a follow-up into the active turn.
- Ask for PWA approval before gated commands or file changes, and receive structured answers to agent questions.
- Select a provider-native model, reasoning effort, and Ciaobot execution mode.
- Preserve transcript replay, file snapshots, token/context usage, quota state, and scheduled-run behavior.
- Load Ciaobot system/runtime/memory instructions.
- Discover the workspace's canonical skills, commands, and subagents.
- Show Codex subagent activity and messages in the existing subagent UI.
- Install, authenticate, diagnose, and update Codex without exposing credentials.

Do not count a wrapper around `codex exec` as complete. It cannot provide the live approval, steering, or durable rich-client behavior required here.

## Parity matrix

| Ciaobot behavior | Codex surface | Implementation decision |
| --- | --- | --- |
| Subscription login | Sign in with ChatGPT; app-server account login/read/logout | Supported. Make ChatGPT OAuth the default Codex setup path. |
| Persistent chat | `thread/start`, `thread/resume`, `thread/read` | Persist the Codex thread ID in `ChatInfo.session_id` as soon as it is known. |
| Fork | `thread/fork` | Map `AgentRequest.fork_session` directly. |
| Text streaming | agent-message item deltas | Normalize to `AssistantTextDelta`. |
| Reasoning streaming | reasoning summary/raw deltas | Normalize summaries to `ThinkingEvent`; do not expose provider-private hidden chain of thought. |
| Tool lifecycle | item started/updated/completed notifications | Normalize to `ToolUseEvent`, retaining item ID and parent/collaboration IDs. |
| Images | turn inputs of type `image` or `localImage`/path, depending on negotiated schema | Send the existing validated Ciaobot image paths; never let a client submit arbitrary filesystem paths. |
| Stop | `turn/interrupt` | Implement `ActiveHandle.stop()`. |
| Mid-turn follow-up | `turn/steer` with the active turn ID precondition | Implement `ProviderService.steer()` without falling back to queueing when the turn is steerable. |
| Tool approval | server requests for command execution, file change, permissions, and MCP elicitation | Correlate the app-server request ID with `PermissionRequestEvent`; answer approve/deny once. |
| Structured question | `item/tool/requestUserInput` | Map questions and answers to the existing question card instead of treating them as command approval. |
| Modes | collaboration mode + sandbox + approval policy/reviewer | Use the explicit mapping below. |
| Models and effort | `model/list` and per-turn model/effort | Discover dynamically. Never hard-code current model IDs. |
| Usage/context | thread token-usage updates | Normalize input/output/cached/context window values; derive context percentage. |
| Quota | account rate-limit read/update notifications and usage-limit errors | Merge sparse updates into the latest snapshot and feed the existing retry scheduler. |
| Transcript replay | `thread/read(includeTurns=true)` plus Ciaobot transcript | Ciaobot transcript remains canonical for UI replay; app-server is the provider-session source of truth. |
| File snapshots | file-change items/patch updates and command/file tool completion | Feed affected paths into the existing snapshot pipeline. |
| Skills | Codex skills plus `skills/list` | Project skills live under the shared `.agents/skills/` projection. |
| Commands | No documented project `.codex/commands` contract | Expand Ciaobot slash commands before dispatch; also generate Codex skill wrappers for direct CLI use. |
| Subagents | custom Codex agents and `collabAgentToolCall` items | Generate Codex agent config and map receiver thread IDs to the subagent panel. |
| Schedules | same app-server adapter with non-interactive approval policy | Run with a schedule-safe policy and fail visibly if interaction is required. |

## Architecture

### 1. Land the provider-neutral foundation first

This phase is shared with the Gemini plan and must be implemented once, not twice.

1. Replace the hard-coded `ClaudeProvider` construction in `ciao/provider_service.py` with a registry/factory keyed by `claude`, `codex`, and later `gemini`.
2. Add a `ProviderCapabilities` value to `ciao/providers/base.py`. At minimum it must declare:
   - `resume`, `fork`, `images`, `stop`, `steer`
   - `permissions`, `structured_questions`
   - `dynamic_models`, `thinking_levels`, `usage`, `quota`
   - `subagents`, `background_subagents`, `subagent_messages`
   - `session_history`, `schedule_unattended`
3. Keep `run_streaming()` as the normalized data path, but add provider methods for model discovery, history, subagent inspection, auth/status, and cleanup instead of calling Claude SDK helpers from routes.
4. Add a small asynchronous JSON-lines RPC peer in `ciao/providers/stdio_rpc.py`. It must support:
   - request IDs and pending futures;
   - notifications;
   - server-initiated requests and responses;
   - bounded stdout line size, stderr capture, process-exit propagation, timeouts, and cancellation;
   - protocol-specific envelopes, because Codex omits the JSON-RPC header while Gemini ACP uses JSON-RPC 2.0.
5. Make `ciao/web/project_chats.py`, schedule draining, `/messages`, `/subagents`, and new-session cleanup call provider capabilities rather than import Claude-only helpers.
6. Change provider validation in chat create/update/handover, schedules, the CLI, and workspace configuration from closed literals to the registry's enabled providers.
7. Migrate `ciao/sessions.py` to a new state version with per-provider usage, quota, cost, and session metadata. Preserve migration from the current Claude-only v3 state.
8. Keep `ChatInfo.provider` authoritative. A provider handover must archive the old provider session and start a fresh target-provider session while retaining the visible Ciaobot transcript.
9. Extract the current `UserPromptSubmit` behavior into a provider-neutral turn-context builder. It must preserve the date, active workspace/project, GWS profile, cwd, and vault entity tags currently produced by `ciao/observability/hooks.py`; the Claude hook becomes one consumer rather than the only implementation.

Do not change the meaning of the existing Claude/Ollama/OpenRouter model buckets in this refactor. They remain routes through the Claude SDK until separately redesigned.

### 2. Implement `CodexProvider`

Create `ciao/providers/codex.py` with one app-server subprocess per active Ciaobot chat. Per-chat isolation matches the current Claude client lifecycle and prevents one broken RPC process from taking down unrelated chats.

Lifecycle:

1. Resolve the executable from `CIAO_CODEX_BIN`, then the recorded managed install, then `PATH` through `ciao.tool_path`. Never depend on `/Applications/ChatGPT.app/.../codex` or another private application-bundle path.
2. Start `codex app-server` over stdio and complete its initialization handshake.
3. If `AgentRequest.session_id` is set, call `thread/resume`; if it is set with `fork_session`, call `thread/fork`; otherwise call `thread/start`.
4. For a new thread, pass:
   - absolute workspace `cwd`;
   - the selected model;
   - Ciaobot's generated system/memory instructions as `developerInstructions`;
   - the mapped sandbox and approval policy;
   - a Ciaobot client/source marker where the negotiated schema supports it.
5. Publish the thread ID immediately so `project_chats.py` persists it before the first model delta.
6. Start a turn with validated text/image inputs, model, effort, working directory, approval policy, and sandbox policy. Add the provider-neutral runtime/entity block as application context through the negotiated input schema; if that schema has no separate application-context item, prepend a clearly delimited hidden dispatch block while preserving only the user's original text in Ciaobot's visible transcript.
7. Track the active turn ID. `steer()` must use `turn/steer(expectedTurnId=...)`; `stop()` must use `turn/interrupt`.
8. End the normalized stream only on turn completion or an unrecoverable process/protocol error. Drain late subagent notifications before disconnect using the same bounded behavior Ciaobot already applies to Claude.
9. On WebSocket disconnect, leave the provider turn running under the broker as Ciaobot does now. On service shutdown, interrupt active turns, deny unresolved server requests, and terminate child processes within a bounded timeout.

Retry only before the first user-visible event. Never replay a turn after a command or file change may have executed.

### 3. Mode mapping

The UI keeps the four existing Ciaobot modes. Map their intent explicitly:

| Ciaobot mode | Codex collaboration mode | Sandbox | Approval behavior |
| --- | --- | --- | --- |
| `normal` | `default` | `workspace-write` | `on-request`, reviewed by the user |
| `plan` | `plan` | `read-only` | `on-request`, reviewed by the user |
| `auto` | `default` | `workspace-write` | `on-request` with `approvalsReviewer=auto_review` when the negotiated schema supports it; otherwise user review |
| `bypass` | `default` | `danger-full-access` | `never` |

The UI must retain the warning for bypass. If an installed Codex build does not support `auto_review`, report the degraded mapping in provider diagnostics rather than silently treating auto as bypass.

Workspace `disallowed_tools` currently contains Claude tool names. Introduce provider-aware deny rules in configuration. Preserve the old list for Claude and add a `provider_tool_policies` map rather than attempting unsafe string translation of names such as `Bash` or `mcp__...`.

### 4. Event normalization

Implement a table-driven translator and fixture-test every row:

- Agent message deltas -> `AssistantTextDelta`.
- Reasoning summary deltas -> `ThinkingEvent`; raw reasoning is ignored unless OpenAI explicitly marks it display-safe.
- Command, file change, MCP call, web search, image view, and collaboration item start/update/complete -> `ToolUseEvent` with stable item ID, status, summarized input, and output.
- Thread token usage -> `TokenUsageEvent`; preserve cached input and context-window values in the result usage map even if the current UI does not render all fields.
- Account rate-limit snapshot/update -> provider quota state. Sparse update fields merge; missing fields do not clear prior values.
- Turn completion -> `ResultEvent`, including thread ID, effective model, usage, quota, error status, and cost only when the protocol supplies it.
- Usage-limit error -> an error `ResultEvent` with a machine-readable quota classification used by hourly retry.
- Model reroute -> a status/tool event and update `effective_model`; never pretend the requested model ran.

For app-server requests:

- Command/file/permission request -> `PermissionRequestEvent`. Store the raw app-server request ID privately and display only a sanitized command/path summary.
- User-input request -> the existing structured question event path. Support choice and free-text answers.
- MCP elicitation -> structured question if it requests data, approval card if it requests permission. Never echo secrets from request payloads into logs or push notifications.
- On turn end, stop, or process exit, deny/cancel every unresolved request and remove its replay-buffer card.

### 5. Models, reasoning, and quota

Replace the Claude-only `THINKING_LEVELS` and hard-coded frontend bucket definitions with server-provided provider metadata.

1. `GET /api/models` returns provider sections. The Codex section is populated from `model/list` and includes supported reasoning efforts and image capability per model.
2. Cache the catalog briefly by executable version and account, but refresh on provider reconnect or model-list failure.
3. Preserve the model returned by the provider as `effective_model`.
4. Read account state and rate limits without printing tokens or the contents of Codex's credential store.
5. Update `web/src/lib/types.ts`, `web/src/lib/modelSections.ts`, `ModelSelector.vue`, and `ChatPanel.vue` so provider and model are separate dimensions. Remove the `ProviderKey = 'claude'` assumption.

### 6. Authentication, installation, and updates

Supported user flow:

1. Settings shows Codex as `not installed`, `installed / login required`, `ready`, `incompatible`, or `error`.
2. Installation is an explicit operator action. Use OpenAI's official standalone installer and record only the resolved binary path, install mechanism, and version in `.runtime/provider-installs.json`. Do not store credentials there.
3. `ciao auth codex` runs the resolved `codex login` flow. Offer device authorization (`codex login --device-auth`) for terminals without a usable browser.
4. The richer Settings flow may call app-server account login APIs and display the returned verification URL/code, then wait for the login-completed notification.
5. Readiness uses app-server account read/status. Do not decide readiness by parsing `auth.json` or macOS Keychain data.
6. `ciao upgrade` and Settings update Codex only through its recorded install mechanism; for the standalone install use the official Codex updater. Never replace a binary while a Codex chat is active.
7. Logout is explicit and confirmed, because it changes the user's global Codex login.

Capability gating is mandatory. At startup or after update, run app-server schema generation into `.runtime` or perform a no-turn handshake and verify the methods/events required by this plan. The check must cover thread start/resume/fork/read, turn start/steer/interrupt, model list, account read/rate limits, approvals, user input, skills, token usage, and collaboration items. An incompatible CLI stays disabled with an actionable update message.

Update these areas:

- `ciao/setup_status.py` and setup API payloads;
- `ciao/cli.py` auth, create-chat provider choices, diagnostics, and setup text;
- `ciao/main.py` startup provider checks and shutdown;
- `ciao/upgrade.py` install-method-aware status/update;
- `web/src/components/LoginView.vue`, `SettingsView.vue`, and `StartupView.vue`.

Remove the stale “Codex via Pi” Settings copy. Codex is a native provider and must not be represented as a connection nested under another provider.

### 7. Workspace instruction and asset synchronization

Keep these canonical, user-editable sources:

- `skills/<name>/SKILL.md`
- `commands/<name>.md`
- `subagents/<name>.md`

Extend `ciao/sync_skills.py` into a provider-aware asset synchronizer while retaining `ciao sync-skills` as a compatible command name.

#### Instructions

- Keep the existing root `CLAUDE.md` for Claude.
- Add a generated root `AGENTS.md` for Codex using the Ciaobot workspace guide plus any provider-neutral workspace additions.
- Do not overwrite a user's unrelated `AGENTS.md`. Use a clearly marked Ciaobot-managed import file such as `.ciao/instructions.md`, and have a short managed reference in `AGENTS.md`; if a conflicting unmanaged file cannot be merged safely, report workspace health `warn` and give an exact manual include instruction.
- Continue passing dynamic Ciaobot system, memory, and runtime context through the provider adapter. Static discovery files are not a substitute for per-chat vault context.

#### Skills

- Project Codex and Gemini both discover `.agents/skills/`. Mirror each canonical workspace skill there with a relative symlink.
- Build a Ciaobot-managed staging inventory from workspace `skills/`, packaged stock skills, and the names declared by `skills-lock.json`. Upstream fetches must no longer remain usable only from `.claude/skills`; copy/link the managed result into the staging inventory and then project it to every enabled provider.
- Preserve precedence as workspace > packaged stock > locked upstream, and never mirror unrelated user-global/provider-global skills into another provider.
- Continue `.claude/skills/` projection for Claude.
- Verify discovery with app-server `skills/list` after sync; surface missing or invalid skills in workspace health.

#### Commands

Codex has no documented project `.codex/commands` contract that Ciaobot should depend on.

- Add a provider-neutral slash-command resolver before `AgentRequest` dispatch. When input begins with a known `/name`, load `commands/name.md`, substitute `$ARGUMENTS`, and send the expanded prompt. Preserve the original visible user message in the Ciaobot transcript.
- Generate `.agents/skills/ciao-command-<name>/SKILL.md` wrappers so the same workflows are discoverable when the user opens Codex directly in the workspace. The direct-CLI invocation may be skill syntax rather than Ciaobot's slash syntax; Ciaobot itself retains `/remember`, `/critique`, and `/interrogation` exactly.
- Reject malformed frontmatter, invalid names, or ambiguous positional placeholders during sync instead of emitting broken skills.

#### Subagents

- Parse the existing YAML-frontmatter Markdown source once.
- Generate `.codex/agents/<name>.toml` with `developer_instructions` equal to the Markdown body and provider-native model/reasoning/sandbox fields only when explicitly configured.
- Merge a Ciaobot-owned `[agents.<name>]` registration into project `.codex/config.toml`, pointing at the generated config. Preserve user-owned TOML keys and refuse destructive conflict resolution.
- Never translate Claude tool names blindly. A canonical agent without a provider-specific tool list inherits the parent Codex tools. Add optional `providers.codex` metadata later if operators need restrictions.
- Remove only files carrying a Ciaobot-generated marker during prune.

#### MCP servers and connectors

- Do not copy `~/.claude`, `~/.codex`, OAuth connector state, or MCP credentials between providers.
- Ciaobot does not currently have a canonical provider-neutral MCP registry, so this plan synchronizes only Ciaobot-owned instructions, skills, commands, and subagents.
- If a later change introduces workspace-owned MCP definitions, add explicit provider compilers and secret references. Never infer a Codex MCP config from Claude tool names or credential files.

Update `ciao/web/agent_assets.py`, Settings copy, and health checks to show canonical sources plus each enabled provider's projection/discovery state instead of treating `.claude` as the only installed catalog.

### 8. History, subagents, and schedules

Provider history:

- Make Ciaobot's own turn transcript the stable UI history across handovers.
- Use `thread/read(includeTurns=true)` to restore provider messages where needed, but do not assume every command interaction is persisted; the generated app-server schema explicitly describes stored thread items as lossy for some interactions.
- Archive under `Logs/Chats/<chat-id>/codex/` and never copy Codex global credentials or unrelated global thread files into a workspace.
- New session archives the visible transcript, interrupts/disconnects the process, and clears the thread ID. Provider-side deletion is optional and must be an explicit cleanup operation, not required to reset Ciaobot.

Subagents:

- Treat `collabAgentToolCall` receiver thread IDs as subagent IDs.
- Map item start/status/completion to the existing subagent summary and count.
- Fetch a receiver's messages with `thread/read` and expose them through `/subagents/{id}/messages`.
- Preserve sender/receiver relationships so nested or parallel activity is not flattened incorrectly.
- Add a bounded post-turn drain. It ends when all observed collaboration calls are terminal, the user stops, or the configured timeout expires.

Schedules:

- Reuse `CodexProvider`; do not create a one-shot implementation.
- Default unattended schedules to workspace-write plus `approvalPolicy=never` only when the schedule is already configured for bypass/unattended execution. Otherwise, if Codex asks the absent user for approval or input, end the job as `needs_user` and keep the chat available for continuation.
- Preserve quota retry classification and autoarchive behavior.

## Concrete change set

Expected existing files to modify:

- `pyproject.toml` only if a lightweight TOML/schema dependency is genuinely required; Codex itself is not a Python package dependency.
- `ciao/models.py`
- `ciao/providers/base.py`, `ciao/providers/__init__.py`
- `ciao/provider_service.py`
- `ciao/web/project_chats.py`, `ciao/web/chat_broker.py`, chat/history/subagent routes
- `ciao/config.py`, `ciao/sessions.py`, `ciao/setup_status.py`
- `ciao/cli.py`, `ciao/main.py`, `ciao/upgrade.py`, `ciao/tool_path.py`
- `ciao/sync_skills.py`, `ciao/skills_inventory.py`, `ciao/web/agent_assets.py`
- the runtime/entity context helper currently implemented in `ciao/observability/hooks.py`
- stock workspace instructions and `CIAO_CUSTOMIZATION.md`
- `web/src/lib/types.ts`, `api.ts`, `modelSections.ts`
- `ChatPanel.vue`, `ModelSelector.vue`, `LoginView.vue`, `SettingsView.vue`, `StartupView.vue`, schedule forms, and `SubagentPanel.vue`

Expected new files:

- `ciao/providers/stdio_rpc.py`
- `ciao/providers/codex.py`
- focused Codex protocol fixtures and tests under `tests/`
- generated stock templates for Codex instructions/agent projection if needed

Do not mix a broad Ollama/OpenRouter redesign into this work.

## Delivery sequence and gates

### Phase 0: conformance spike

Build a test-only app-server client and prove, against an authenticated subscription account:

- start/resume/fork/read;
- text/reasoning/tool streaming;
- image input;
- command and file approval round trip;
- structured user input round trip;
- interrupt and same-turn steer;
- token/rate-limit events;
- a custom skill and custom subagent, including receiver-thread message readback.

Generate and save sanitized protocol fixtures—no prompts, paths, account identifiers, or tokens from a private workspace. If any required method is missing, stop and update this plan before production implementation.

### Phase 1: provider-neutral core

Land registry, capabilities, state migration, provider-delegated history/subagents, dynamic model metadata, and RPC transport. All existing Claude tests must stay green.

### Phase 2: Codex adapter

Land lifecycle, event mapping, permissions/questions, steering/stop, images, usage/quota, history, and subagents behind a disabled-by-default feature flag.

### Phase 3: setup and workspace assets

Land installation/auth/update/status plus instruction, skill, command, and subagent projections. Run sync twice in tests to prove idempotence and safe pruning.

### Phase 4: PWA and schedules

Land provider/model selection, handover, status cards, permissions/questions, subagent rendering, and scheduled execution. Enable Codex only after the acceptance suite passes.

## Required tests

Backend tests must include:

- RPC request/notification/server-request correlation, timeout, malformed line, stderr, and process exit.
- Every app-server event mapping using checked-in sanitized fixtures.
- No retry after the first visible/tool event.
- New/resume/fork/reset and provider handover persistence.
- Stop and same-turn steer races, including a stale active-turn ID.
- Approve, deny, disconnect, duplicate response, and turn-end cleanup.
- Structured choice and free-text questions.
- Image allowlist and path traversal rejection.
- Model/effort discovery and incompatible-model rejection.
- Sparse rate-limit merge, usage-limit retry, and effective-model reroute.
- Subagent start/message/completion and drain timeout.
- Unattended schedule success and `needs_user` failure.
- v3-to-new-state migration and existing Claude behavior.
- Codex install/status/auth command construction without touching real credentials.
- Asset conversion, conflicts, idempotence, safe prune, and discovery health.
- Runtime context and entity-tag equivalence with the existing Claude hook, without leaking the injected block into the visible user transcript.

Frontend tests must include provider selection, handover confirmation, dynamic reasoning options, approval/question cards, Codex status states, and subagent rendering.

Final verification:

```text
pytest tests/
cd web && npm run build
```

Also run an opt-in authenticated smoke suite on macOS using a disposable workspace. It must be skipped—not faked—in normal CI when no Codex subscription login is available.

## Definition of done

- A fresh user can install Codex, sign in with ChatGPT, select Codex in Ciaobot, and complete a tool-using chat without adding an API key.
- Restarting Ciaobot resumes the same Codex thread.
- Images, approvals, structured questions, stop, same-turn steering, modes, models, reasoning, usage, quota retry, file history, handover, schedules, skills, commands, and subagents pass the acceptance suite.
- An incompatible or logged-out Codex CLI fails closed with an actionable status.
- No token, account identifier, private path, transcript, or credential file is copied into the repository, logs, fixtures, or workspace assets.
- Claude behavior and its full test suite remain unchanged.

## Official sources

- [Codex authentication](https://developers.openai.com/codex/auth)
- [Codex CLI features and installation](https://developers.openai.com/codex/cli/features)
- [Codex app-server protocol](https://developers.openai.com/codex/app-server)
- [Codex AGENTS.md guidance](https://developers.openai.com/codex/guides/agents-md)
- [Codex skills](https://developers.openai.com/codex/skills)
- [Codex multi-agent configuration](https://developers.openai.com/codex/multi-agent)

Protocol names in this plan were additionally checked against JSON Schema generated by the installed Codex app-server on the research date. The implementation must regenerate and negotiate against the actual supported CLI instead of treating that local snapshot as a permanent contract.
