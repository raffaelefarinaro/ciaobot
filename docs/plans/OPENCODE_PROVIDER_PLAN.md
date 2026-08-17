# opencode provider plan

Status: implementation plan
Research date: 2026-08-13
Target repository: `raffaelefarinaro/ciaobot`

## Verdict

opencode can be added as a first-class Ciaobot provider. It is the cheapest
third runner Ciaobot is likely to get, because it already consumes Ciaobot's
existing Claude-shaped workspace assets and because its transport is a
documented HTTP + SSE server rather than a bespoke stdio protocol.

**Exact parity with the current Claude integration is not possible today.** One
behavior has no equivalent:

1. **Non-destructive same-turn steering.** opencode exposes prompt and
   interrupt, but no method that injects a message into a running turn. The
   capability is requested upstream ([#21388], [#24298], [#32157]) and not
   shipped. Ciaobot can queue a follow-up for the next turn, which is not the
   same behavior as Claude's live `client.query()` or Codex `turn/steer`.

Unlike the Gemini assessment, the other two blockers recorded there do **not**
apply. opencode's `task` tool creates real child sessions carrying `parentID`,
discoverable through `GET /session/:id/children` with independently retrievable
messages, so Claude-style background subagent tracking is available. It also
exposes a provider-native session fork and a structured question request/reply
contract distinct from tool approval.

Therefore this plan targets **practical parity**: ship with `steer=false`
declared in provider metadata and the affordance hidden in the PWA. That
limitation does not prevent a useful opencode provider. It prevents an honest
claim of exact Claude feature parity.

[#21388]: https://github.com/anomalyco/opencode/issues/21388
[#24298]: https://github.com/anomalyco/opencode/issues/24298
[#32157]: https://github.com/anomalyco/opencode/issues/32157

## Supported scope

- Install, version, readiness, update, and logout/setup diagnostics.
- New, persisted, resumed, forked, handed-over, archived, and reset chats.
- Streamed assistant text, reasoning, tool calls/results, errors, and usage.
- Image input.
- PWA approve/deny handling for gated tools.
- Structured question cards with choice and free-text answers.
- Stop/cancel.
- Dynamic model selection and the provider's execution modes.
- Background subagents with per-agent message retrieval.
- Ciaobot transcript replay, file snapshots, schedules, and provider handover.
- Workspace skills, commands, subagent definitions, and project MCP servers.

It cannot currently provide same-turn steering, and it has no unified quota
snapshot because opencode is bring-your-own-provider.

## Parity matrix

| Ciaobot behavior | opencode surface | Verdict / decision |
| --- | --- | --- |
| Persistent chat | `POST /session`, `GET /session/:id` | Supported. Persist the session id immediately. |
| Resume | server-side session state | Supported. |
| Fork | `POST /session/:id/fork` | Supported natively. |
| Text streaming | `message.part.delta` + `message.part.updated` | Supported. Verified live. |
| Reasoning streaming | `reasoning` parts, distinguished by part type | Supported. Verified live. |
| Tool lifecycle | `tool` parts with a `state.status` machine | Supported. Verified live. |
| Images | prompt file/image parts | Supported after existing path/size validation. |
| Stop | `POST /session/:id/abort` | Supported. |
| Mid-turn follow-up | none | **Not supported.** Queue for the next turn. |
| Tool approval | `permission.v2.asked` + `POST /permission/:id/reply` | Supported. Reply is `once`/`always`/`reject`. |
| Structured question | `POST /question/:id/reply` and `/reject` | Supported. |
| Modes | per-session `agent` + `permission` rule list | Supported. Map by advertised agent ids, not position. |
| Mode switch mid-chat | `agent` re-sent per prompt; `permission` fixed at create | **Supported with rotation.** `PATCH /session/{id}` takes `permission` and returns 200 but does not apply it (verified: wildcard read back unchanged), so a stale or unreadable ruleset is never resumed; Ciaobot starts a fresh session with the current mode's rules. |
| Reasoning effort | per-model `variants` (`low`/`medium`/`high`/`max`), sent as `variant` | Supported. Narrowed per model from the catalog. |
| Models | `GET /provider`, `opencode models` | Supported, dynamic. Do not hard-code model ids. |
| Usage | token/cost on assistant message info | Supported. |
| Quota/reset time | none unified | **Not supported.** `quota=false`; classify upstream 429s only. |
| Transcript replay | `GET /session/:id/message` plus Ciaobot transcript | Supported. |
| Subagent execution | `task` tool → child session | Supported. |
| Background subagent tracking | `GET /session/:id/children`, `parentID` | Supported. |
| Subagent messages | `GET /session/:childId/message` | Supported. |
| Skills | reads `.claude/skills/<name>/SKILL.md` natively | Supported with no projection. |
| Instructions | reads `AGENTS.md` and `CLAUDE.md` natively | Supported with no projection. |
| Commands | `.opencode/command/*.md`, Markdown + `$ARGUMENTS` | Supported via a thin mirror. |
| Subagent definitions | `.opencode/agents/*.md`, YAML frontmatter | Supported via a frontmatter rewrite. |
| MCP servers | `opencode.json` `mcp` object | Supported. JSON, no marker fencing available. |
| Schedules | session in an unattended permission mode | Supported with interaction-safe failure handling. |

## Architecture

### 1. Land the provider-neutral foundation first

`GEMINI_CLI_PROVIDER_PLAN.md` assumed this landed with Codex. It only partly
did. `ciao/provider_service.py:15` is a real registry and
`ciao/providers/base.py:36` is a real capability record, but roughly 40 backend
and 20 frontend files still enumerate the literal pair `{"claude", "codex"}`.
Adding a third literal to each triples the debt, so the foundation comes first.

1. Introduce a `ProviderDescriptor` — id, label, factory, capabilities,
   thinking levels, tier families, auth command, status probe, upgrade hook,
   asset-sync adapter — and make `_PROVIDER_FACTORIES` a registry of them.
2. Convert every enumeration listed in §"Concrete change set" to read the
   registry rather than a literal.
3. Replace the four `codex_*_model` tier-pin scalars
   (`ciao/app_settings.py:57-60`) with the nested per-provider map already used
   by `custom_routing` (`:61-62`), reading the old keys for back-compat. Four
   new scalars per provider does not scale.
4. Drive the PWA's `BUCKET_DEFS` (`ChatPanel.vue:2039`) and
   `modelSections.ts:110-149` from a descriptor array returned by
   `GET /api/models` instead of hard-coded unions.

This step is a pure refactor. Claude and Codex behavior must not change and all
existing tests must stay green.

### 2. Implement `OpencodeProvider` over the HTTP server

Create `ciao/providers/opencode.py`. Use `opencode serve`, not `opencode run`:
headless run mode cannot route approvals and questions back to the PWA.

**Use one `opencode serve` process per active chat.** opencode is a real
multi-session server, so a shared process is tempting, but Ciaobot scopes the
control-plane MCP token per chat (`ciao/models.py:135-140`; compare Codex's
`shell_environment_policy.exclude` guard at `ciao/providers/codex.py:759-769`).
A shared server would force one long-lived token across every chat and lose
failure isolation. Bind loopback on an ephemeral port and set
`OPENCODE_SERVER_PASSWORD`. If Phase 0 shows MCP servers can be configured
per-session, revisit this and record the change here.

`httpx==0.28.1` is already a dependency (`pyproject.toml:26`); SSE is consumed
with `httpx.AsyncClient.stream`. No new package, and
`ciao/providers/stdio_rpc.py` is not involved.

Lifecycle:

1. Resolve `CIAO_OPENCODE_BIN`, then `ciao.tool_path.resolve_tool("opencode")`,
   then `PATH`.
2. Spawn `opencode serve` with the chat workspace as cwd; health-check
   `GET /global/health`.
3. Validate the served OpenAPI document at `/doc` against the set of operations
   Ciaobot requires, and fail closed on an incompatible build. This replaces
   Codex's hand-maintained `_REQUIRED_PROTOCOL_TOKENS`
   (`ciao/providers/codex.py:61-79`) with a machine-checkable equivalent.
4. Create or resume the session; persist the id before the first prompt.
5. Apply the mapped agent and permission config before prompting.
6. Normalize SSE into Ciaobot events; resolve permission and question requests
   exactly once.
7. `ActiveHandle.stop()` calls interrupt. `steer()` returns `False` so
   `ProviderService` keeps the message in the existing next-turn queue.
8. On shutdown, deny unresolved requests, terminate within a bounded timeout,
   and preserve the session id for resume.

As with Claude and Codex, retry only before any visible/model/tool event.

### 3. Mode and policy mapping

| Ciaobot mode | opencode |
| --- | --- |
| `plan` | built-in `plan` primary agent (read-only) |
| `normal` | `build` agent, `{"*": "ask"}` |
| `auto` | `build` agent, `edit` allow, `bash` ask |
| `bypass` | `build` agent, `{"*": "allow"}` |

Map by advertised agent id from `GET /agent`, not by assumed order. If an agent
is missing, disable that mode for opencode and explain why.

For schedules, a permission or question request with no live operator must
produce a visible `needs_user` job state. Never auto-approve merely because the
request came from a schedule.

### 4. Event and permission normalization

Implement a fixture-tested translator into the existing events in
`ciao/models.py`: assistant text → `AssistantTextDelta`, reasoning →
`ThinkingEvent`, tool parts → `ToolUseEvent` with a stable id, completion →
`ResultEvent`, token/cost → `TokenUsageEvent`, permission requests →
`PermissionRequestEvent`, question requests → the structured question event.

Present a sanitized tool name and input summary. Never publish environment
variables or full command output in push notifications. Stop, disconnect, turn
completion, and process exit must deny any unresolved request and remove replay
cards.

### 5. Models and usage

Populate model options from `GET /provider`. Do not hard-code model ids. Map
Ciaobot's `haiku`/`sonnet`/`opus`/`fable` tiers onto opencode models through the
per-provider tier map from §1.3, with operator pins winning only while the
pinned model is still visible. Preserve token and cost values where returned;
compute context percentage only when both used tokens and a trustworthy limit
are supplied. Set `quota=false` and classify upstream 429s without fabricating
reset timestamps.

### 6. Authentication, installation, and update handling

1. Settings states: `not installed`, `installed / login required`, `ready`,
   `incompatible`, and `error`.
2. Installation is an explicit operator action; record the mechanism and
   resolved executable in the existing provider-install record. Never record
   credentials.
3. `ciao auth opencode` launches the resolved interactive `opencode auth login`
   in a real terminal. Do not parse or copy cached credential files.
4. Readiness uses a bounded serve/health/`/doc` probe that never submits a model
   prompt.
5. Update only when no opencode process is active.

Document `CIAO_OPENCODE_BIN` in `.env.example`; `tests/test_env_vars_documented.py`
enforces this.

### 7. Workspace instructions and asset synchronization

Canonical Ciaobot sources stay provider-neutral: `skills/<name>/SKILL.md`,
`commands/<name>.md`, `subagents/<name>.md`. This is where opencode is
dramatically cheaper than Gemini would have been.

**Skills — no projection.** opencode discovers `.claude/skills/<name>/SKILL.md`
natively, walking up to the worktree root. Only
`ciao/skills_inventory.py:120-128` `_installed_targets` needs to report
`opencode` for that path.

**Instructions — no projection.** `AGENTS.md` is already a relative symlink to
`CLAUDE.md` (`_ensure_linked_workspace_guides`, `ciao/sync_skills.py:973`) and
opencode reads both.

**Commands.** Mirror `commands/*.md` into `.opencode/command/` with the existing
`_mirror_dir_symlinks` primitive (`ciao/sync_skills.py:547`). Canonical files
already use `$ARGUMENTS`, which opencode supports, so no conversion is needed —
unlike Gemini's TOML.

**Subagents.** Mirror `subagents/*.md` into `.opencode/agents/` with a
frontmatter rewrite that adds `mode: subagent` and preserves `description` and
the Markdown body. Omit `tools` and `model` unless canonical provider-specific
metadata is explicitly set. Far cheaper than the Codex TOML compiler
(`_install_codex_agents`, `ciao/sync_skills.py:878`).

**MCP servers.** Project `.mcp.json` → the `mcp` object in `opencode.json`.
Reuse the `_env_placeholder` credential guard (`ciao/sync_skills.py:697-701`)
so literal secrets are dropped and only `${VAR}` references are copied. JSON has
no comment syntax, so the marker-fence approach used for `.codex/config.toml`
does not transfer: track Ciaobot-owned server names in a
`.opencode/.ciao-managed-mcps.json` sidecar and prune only those. User-authored
entries always win on name collision.

Add the marker constants and `SyncSkillsResult` counters alongside the Codex
ones, and update `ciao/web/agent_assets.py` (workspace health path list,
broken-link scan, instruction-file inventory) and `ciao/evals.py`
`_prune_provider_targets`.

**Fix while here:** the Codex skill catalog is currently derived from
`.claude/skills` rather than from canonical sources
(`ciao/sync_skills.py:1071-1075`), which `GEMINI_CLI_PROVIDER_PLAN.md:198` flags
as a defect. Point opencode at the canonical inventory rather than copying the
bug.

Do not copy `~/.claude`, `~/.codex`, or `~/.config/opencode` credentials,
connector configuration, or MCP secrets between providers.

### 8. History, reset, subagents, and schedules

Ciaobot's transcript stays canonical for visible replay and cross-provider
handover; `ciao/transcripts.py` is already provider-keyed and needs no change.
Archive under `Logs/Chats/<chat-id>/opencode/`. Reset archives the transcript,
terminates the chat's server process, clears the session id, and starts a new
session on the next turn.

Subagents render as first-class background agents, sourced from child sessions
rather than parent tool text. Set `subagents`, `background_subagents`, and
`subagent_messages` to `true`.

Schedules reuse the same provider. Apply the schedule's mapped mode; if
permission or input is required with no live user, terminate as `needs_user`,
retain the chat, and never hang the job.

## Concrete change set

Existing files to modify:

- `ciao/provider_service.py`, `ciao/providers/base.py`, `ciao/providers/__init__.py`
- `ciao/models.py` (`THINKING_LEVELS`)
- `ciao/config.py`, `ciao/app_settings.py`, `ciao/model_tiers.py`
- `ciao/cli.py` (argparse `choices`, `_auth_command_for_provider`), `ciao/main.py`, `ciao/upgrade.py`
- `ciao/setup_status.py`, `ciao/custom_providers.py`
- `ciao/web/routes_api.py`, `ciao/web/project_chats.py`, `ciao/web/commands.py`, `ciao/web/agent_assets.py`
- `ciao/sync_skills.py`, `ciao/skills_inventory.py`
- `ciao/evals.py`, `ciao/eval_runner.py`, `ciao/release_evidence.py`, `evals/release.json`
- `web/src/lib/types.ts`, `modelSections.ts`, `fableModel.ts`
- `ChatPanel.vue`, `SettingsView.vue`, `LoginView.vue`, `StartupView.vue`,
  `NewScheduleForm.vue`, `SchedulePanel.vue`, `stores/projects.ts`, `stores/tasks.ts`
- `docs/ARCHITECTURE.md`, `PWA_API.md`, `README.md`, `docs/DEVELOPMENT.md`,
  `docs/MCP.md`, `.env.example`, `ciao/stock/skills/ciao-capabilities/SKILL.md`

New files:

- `ciao/providers/opencode.py`
- sanitized opencode protocol fixtures and focused tests under `tests/`

Do not add any opencode npm package to `web/package.json`; it is an external
provider executable, not a browser dependency.

## Delivery sequence and gates

### Phase 0: conformance spike — **done (2026-08-13, opencode 1.18.18)**

Run against a throwaway workspace with no credentials. Findings, several of
which corrected the published docs:

- **Two API surfaces exist.** The unprefixed `/session/...` shape is the one
  Ciaobot targets; `/api/...` is a separate experimental surface, and any
  unlisted `/api/*` path falls through to the web UI's SPA shell.
- **Abort, not interrupt.** v1 is `POST /session/{id}/abort`; `interrupt` only
  exists on the experimental surface.
- **Permission and question replies are global, not session-scoped**:
  `POST /permission/{requestID}/reply` (`once` / `always` / `reject`) and
  `POST /question/{requestID}/reply` / `/reject`.
- **Per-session `agent`, `model`, and `permission` are settable on
  `POST /session`** — so the mode mapping rides the session, not the process.
- **MCP remains server-scoped** (`/mcp`, `opencode.json`), which confirms the
  one-server-per-chat decision for MCP-token scoping.
- **SSE framing** is bare `data: {json}` with an `{id, type, properties}`
  envelope and no `event:` line; the union has 89 variants. The streaming ones
  Ciaobot consumes are the `session.next.*` family.
- **`session.error` fires both before and after `session.idle`**; the later
  copy appends a bundler stack trace. The turn loop keeps the first, which is
  both authoritative and clean.
- **Asset discovery is better than assumed**: `.claude/skills` *and*
  `.agents/skills` are both discovered natively — the latter is already
  generated for Codex — as are `AGENTS.md` and `CLAUDE.md`. Both `agents`/`agent`
  and `commands`/`command` directory spellings work; the plural is used.
- **The free tier needs no auth.** `GET /provider` reports
  `connected: ["opencode"]` out of the box, with seven free models. So a live
  turn *is* verifiable, and the acceptance gate below is closed.

Three findings only a live turn surfaced, each of which had shipped as a bug:

- **`POST /session` takes `permission` as a list of
  `{permission, pattern, action}` rules, not the `{"*": "ask"}` map from the
  config-file docs.** The map is rejected with a bare `{"_tag":"BadRequest"}`,
  so every session create failed.
- **The `session.next.*` events never fire.** Ordinary turns stream through
  `message.part.delta` (incremental) and `message.part.updated` (cumulative,
  restating the whole part). Consuming only the schema's `session.next.*`
  family produced no assistant text at all. Both sources must be reconciled by
  tracking how much of each part has been emitted, or every token doubles.
- **A `ReasoningPart` stores its content in a field called `text`, exactly like
  a `TextPart`.** Keying off the delta's `field` merged the model's private
  reasoning into the visible reply. The part's *type* — announced by
  `message.part.updated` before its deltas — is what separates them.

### Phase 0 procedure (for re-running on a version bump)

Against a disposable workspace, prove and save sanitized fixtures for: session
create/get/resume/fork; child sessions via `parentID`; SSE text, reasoning,
tool, and completion events; permission ask/reply; question reply and reject;
interrupt; image input; token and cost reporting. Then resolve three build
questions: whether MCP servers can be configured per session (decides the
process model), the exact asset directory names (`agents` vs `agent`, `command`
vs `commands`), and which API prefix the installed build serves — the published
docs show both `/session/...` and `/api/session/...` shapes.

Record any missing capability as a `false` flag rather than working around it.

### Phase 1: provider-neutral core — **landed**

`ciao/provider_registry.py` is now the single enumeration. Every backend
`{"claude", "codex"}` literal is gone; the frontend unions are open-ended
(`RuntimeProvider`). Specifically:

- `ProviderDescriptor` carries id, three label forms, and dotted paths for the
  factory, auth command, system-skill listing, status probe, and CLI upgrade,
  plus thinking levels, default-model/bucket data, and the tier-settings
  attribute. Paths resolve lazily, so importing the registry pulls in no SDK.
- `provider_service.supported_providers()` and the new `capabilities_for()`
  read the registry; the subagent-watcher gate in `project_chats.py` is now a
  `background_subagents` capability check rather than a provider-name test.
- `setup_status` and `upgrade_all` fan out over descriptors. `cli.py` argparse
  choices, workspace provider/bucket resolution in `config.py`, custom-provider
  runners, eval/release provider validation, and the Settings labels all derive
  from it.
- The four `codex_*_model` tier-pin scalars became `AppSettings.provider_routing`,
  a `{provider: {tier: model}}` map. The old flat keys are still read from disk
  and accepted on PATCH, and are still emitted by `GET /api/settings/routines`,
  so nothing client-side had to change in lockstep.

Still provider-named and deliberately deferred: the `connect_claude_code` /
`connect_codex` startup phases in `ciao/main.py`, whose ids are a UI contract
mirrored in `StartupView.vue`. Generalizing them means renaming Claude's phase,
which belongs with the Phase 4 PWA work rather than a pure refactor.

Remaining Phase 1 work: drive `BUCKET_DEFS` (`ChatPanel.vue`) and
`modelSections.ts` from a descriptor array on `GET /api/models` instead of the
hard-coded bucket list, and migrate `SettingsView.vue` onto `provider_routing`.

### Phase 2: opencode adapter — **landed and verified against a live model**

`ciao/providers/opencode.py` implements server lifecycle, `/doc` contract
verification, SSE event mapping, permissions, questions, abort, images,
model catalog, usage, history, and child-session reads. Registered in
`ciao/provider_registry.py`; `OpencodeSettings` carries its tier pins.

Verified end to end against `opencode/big-pickle` on the free tier:

- a plain turn streams assistant text, with reasoning kept separate;
- a tool-using turn reports the `bash` call and its result, announced once;
- usage and cost come back on the result event;
- resume carries context across turns in the same session;
- fork produces a new session id;
- `read_thread` replays the message history;
- in `normal` mode a gated `bash` call raises an approval card naming the tool
  and the exact command, linked to its tool call; approving runs it and denying
  does not.

A fourth live-only bug surfaced there: the real event is `permission.asked`
carrying `{permission, patterns, metadata, tool:{callID}}`, not the schema's
`permission.v2.asked` with `{action, resources}`. Reading the v2 shape produced
an approval card that said only "run a tool" with no detail — the operator
could not see what they were approving. Both shapes are now read.

The captured turn is checked in as `tests/fixtures/opencode/turn_with_tool.jsonl`
and replayed in tests, so the three live-only bugs above cannot regress.

### Phase 3: setup and workspace assets — **landed**

`ciao auth opencode`, the Settings readiness row, the startup phase, and
`ciao upgrade` all come from the registry. `ciao/sync_skills.py` projects
`.opencode/agents/`, `.opencode/commands/`, and the `mcp` object in
`opencode.json`; skills and instructions need no projection. Verified against
a real server: it loads every generated file. Sync is idempotent, prunes only
marked files, and drops literal secrets.

### Phase 4: PWA and schedules — **landed**

`GET /api/models` returns `opencode_models` and a registry-driven `providers[]`
array (labels, bucket, capabilities). The chat model picker has an opencode
section, and the bucket helpers route it correctly — including ahead of the
OpenRouter check, since both use `provider/model` ids.

Settings -> Providers renders a card per registered provider, labelled from the
payload rather than from an id map, so a new provider gets a correct card
instead of falling through to another provider's name. Tier pins for runtime
providers now read and write the nested `provider_routing` map; only the
env-backed Ollama/OpenRouter routes still use flat keys.

The composer discloses the steering limit from the capability flag rather than
a provider-name check: when `capabilities.steer` is false, the queued-message
block explains that the message is sent when the current response finishes.

**Note on the Settings card:** the previous implementation showed a hard-coded
placeholder skill list per provider (`web-search`, `stripe`, …) whenever the CLI
reported none. Those names were invented, and there is no honest generic
equivalent, so the fallback now says "None reported by this CLI" instead. This
changes what the Claude and Codex cards show.

## Required tests

Backend:

- SSE parsing: partial frames, malformed lines, server exit, reconnect.
- All event mappings from sanitized fixtures.
- No replay after the first visible or tool event.
- Session create/resume/fork/reset persistence and duplicate-history suppression.
- Images and path traversal rejection.
- Permission approve/deny/duplicate/disconnect/turn-end cleanup.
- Question choice and free-text round trips, plus reject.
- Active-turn follow-up queues without sending a destructive second prompt.
- Mode and model discovery, and unsupported-value rejection.
- Usage without invented reset time; `quota=false`.
- Child-session discovery, background agent counting, and message retrieval.
- Sync: command mirror, subagent frontmatter rewrite, `opencode.json` MCP
  projection, sidecar-only pruning, idempotence across two runs, and refusal to
  copy literal secrets.
- Registry refactor: unchanged Claude and Codex behavior.
- Install/status/auth command construction without real credential access.

Frontend:

- opencode selection and handover confirmation.
- Dynamic model and mode controls.
- Provider status/install/auth states.
- Approval and structured question cards.
- Queued-follow-up copy for the no-steer capability.
- Background subagent rendering.

Final verification:

```text
pytest tests/
cd web && npm run build && npm run test
```

Run the authenticated opencode smoke suite only when explicitly enabled; normal
CI must skip it when no cached login exists.

## Definition of done

- A fresh user can install opencode, authenticate, select it in Ciaobot, and
  complete a tool-using chat with approvals, questions, and subagents.
- Restarting Ciaobot resumes the same opencode session.
- Text/reasoning/tools, images, approvals, questions, stop, models, modes,
  usage, transcript, file snapshots, handover, schedules, skills, commands,
  subagents, and MCP projection pass.
- Unsupported steering is capability-gated and accurately described.
- No tokens, cached auth files, account identifiers, private paths, or private
  session contents enter the repository, fixtures, logs, or generated workspace
  assets.
- Claude and Codex behavior remains unchanged.

## Official sources

- [opencode server mode](https://opencode.ai/docs/server/)
- [opencode CLI](https://opencode.ai/docs/cli/)
- [opencode SDK](https://opencode.ai/docs/sdk/)
- [opencode agents](https://opencode.ai/docs/agents/)
- [opencode skills](https://opencode.ai/docs/skills/)
- [opencode commands](https://opencode.ai/docs/commands/)
- [opencode rules and instruction files](https://opencode.ai/docs/rules/)
- [opencode permissions](https://opencode.ai/docs/permissions/)
- [opencode MCP servers](https://opencode.ai/docs/mcp-servers/)

Implementation must negotiate the installed build rather than assume today's
surface is permanent. Re-run the Phase 0 gap tests on each minimum-version
bump; an upstream steer API would make strict parity possible later.
