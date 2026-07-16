# Cross-Provider Sub-Chats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an owner agent explicitly requested by the user hold a visible, read-only, multi-turn conversation with another configured provider route before returning a synthesized answer.

**Architecture:** Add a Ciaobot-owned `ProviderSubchatManager` beside normal chat orchestration. It persists bounded metadata plus append-only normalized JSONL events, gives each active participant its own `ProviderService`, and exposes authenticated HTTP operations consumed by a stock skill and local CLI. Parent-chat events drive a read-only Vue panel attached to the originating turn.

**Tech Stack:** Python 3.12, asyncio, Starlette, pytest, Vue 3, Pinia, TypeScript, Vitest, JSON/JSONL persistence

---

## Task 1: Define and persist provider sub-chat records

**Files:**

- Create: `ciao/provider_subchats.py`
- Create: `tests/test_provider_subchats.py`

- [ ] **Step 1: Write record round-trip tests**

Create a `ProviderSubchatRecord` with owner and participant route tuples, persist it to `.runtime/provider_subchats.json`, reload the manager, and assert all route, parent, status, timing, message-count, usage, quota, and error fields survive.

- [ ] **Step 2: Run the test and confirm the missing-module failure**

Run: `pytest tests/test_provider_subchats.py -q`

Expected: FAIL because `ciao.provider_subchats` does not exist.

- [ ] **Step 3: Add immutable route identity and record types**

Use a complete route identity rather than provider alone:

```python
@dataclass(frozen=True, slots=True)
class ProviderRoute:
    provider: str
    model: str
    model_bucket: str = ""
    label: str = ""

@dataclass(slots=True)
class ProviderSubchatRecord:
    subchat_id: str
    parent_chat_id: str
    parent_turn_index: int
    workspace: str
    project_id: str
    owner: ProviderRoute
    participant: ProviderRoute
    participant_session_id: str = ""
    status: str = "created"
```

Add the remaining approved timestamps, cumulative active seconds, message count, usage, quota, last error, and limit-extension fields.

- [ ] **Step 4: Implement atomic metadata persistence**

Write through a sibling temporary file and `Path.replace()`. Bound the index to existing records only, reject invalid statuses on load, and reconcile persisted `created` or `running` records to `interrupted` during startup.

- [ ] **Step 5: Implement append-only transcript storage**

Write normalized rows to `.runtime/provider_subchats/<id>.jsonl` with `timestamp`, `type`, `role`, `content`, and event-specific fields. Ignore malformed historical lines on read. Never persist private chain-of-thought events.

- [ ] **Step 6: Add list/read/delete tests**

Cover listing by parent chat and turn, event replay order, malformed-line tolerance, and deleting both metadata and transcript files.

## Task 2: Execute one participant turn through `ProviderService`

**Files:**

- Modify: `ciao/provider_subchats.py`
- Modify: `ciao/web/project_chats.py`
- Modify: `tests/test_provider_subchats.py`

- [ ] **Step 1: Add a fake provider lifecycle test**

Patch `ProviderService` with a fake that yields text, tool activity, permission/question events, result, usage, and session ID. Assert `start()` transitions `created -> running -> waiting_owner`, appends owner and participant rows, publishes parent-chat events, and returns structured JSON with the completed participant reply.

- [ ] **Step 2: Run the lifecycle test and confirm failure**

Run: `pytest tests/test_provider_subchats.py -q`

Expected: FAIL because start/send execution is missing.

- [ ] **Step 3: Expose a parent-route request builder**

Refactor the existing private project-chat routing helpers into one reusable method that resolves workspace, project, mode, model bucket routing, extra environment, and disallowed tools without starting a normal chat turn. Both normal chat execution and provider sub-chats must use the same route rules.

- [ ] **Step 4: Build participant requests**

Construct `AgentRequest` with the participant route, inherited parent mode and restrictions, no parent session ID on start, and the stored participant session ID on send. Add:

```text
You are the participant in a provider consultation. You are talking to the owner agent, not directly to the user. Ask the owner for missing information when needed. Do not start another provider consultation.
```

Set `CIAO_PROVIDER_SUBCHAT_ID` and `CIAO_PARENT_CHAT_ID` in `extra_env`.

- [ ] **Step 5: Normalize live events**

Persist and publish user-visible assistant text, tool use, permission requests, structured questions, errors, result, effective model, usage, and quota. Keep thinking events out of transcript persistence. Store the participant provider's current session ID after every turn.

- [ ] **Step 6: Implement limits and transitions**

Allow one non-terminal consultation per parent chat, 12 explicit owner/participant messages, and 30 cumulative active minutes. Count only provider execution time, not `waiting_owner`. Set terminal states to `completed`, `cancelled`, `failed`, or `interrupted`. Require a fresh user-authorized extension flag before raising either limit.

- [ ] **Step 7: Implement cancel, close, extend, and disconnect**

`cancel()` stops the active `ProviderService`; `close()` accepts only idle non-terminal records; `extend()` records the authorization timestamp and increased limits; terminal transitions disconnect and release the per-subchat service.

- [ ] **Step 8: Run manager tests**

Run: `pytest tests/test_provider_subchats.py -q`

Expected: PASS.

## Task 3: Wire API operations, permissions, restart drain, and parent lifecycle

**Files:**

- Modify: `ciao/web/app.py`
- Modify: `ciao/web/routes_api.py`
- Modify: `ciao/web/routes_chat.py`
- Modify: `ciao/web/project_chats.py`
- Modify: `ciao/main.py`
- Create: `tests/test_provider_subchat_routes.py`
- Modify: `tests/test_restart_drain.py`

- [ ] **Step 1: Write authenticated route tests**

Cover:

```text
GET    /api/chats/{chat_id}/provider-subchats
GET    /api/provider-subchats/{subchat_id}/events
POST   /api/chats/{chat_id}/provider-subchats
POST   /api/provider-subchats/{subchat_id}/messages
POST   /api/provider-subchats/{subchat_id}/close
POST   /api/provider-subchats/{subchat_id}/cancel
POST   /api/provider-subchats/{subchat_id}/extend
POST   /api/provider-subchats/{subchat_id}/permission-response
POST   /api/provider-subchats/{subchat_id}/question-response
```

Test missing parent/subchat, invalid target route, nested creation marker, duplicate active consultation, unauthorized extension, and normal structured results.

- [ ] **Step 2: Run route tests and confirm missing-handler failures**

Run: `pytest tests/test_provider_subchat_routes.py -q`

Expected: FAIL.

- [ ] **Step 3: Construct and attach the manager in `ciao/main.py`**

Instantiate it after `ProjectChatManager`, store it at `app.state.provider_subchat_manager`, and attach parent lifecycle callbacks. Test apps may inject a manager directly.

- [ ] **Step 4: Implement and register route handlers**

Validate provider via `supported_providers()`, model and bucket using the same project-chat validation, and explicit `user_authorized: true` on start and extend. Start/send wait for one participant turn while events continue through the global event bus.

- [ ] **Step 5: Route permission and question responses**

Look up the active sub-chat service and use the same provider-specific response methods as normal chats. Publish request IDs in live events. Reject stale responses with `409`.

- [ ] **Step 6: Integrate parent lifecycle**

Deleting a parent deletes its sub-chats. Archiving a parent cancels active work and appends a compact `## Provider consultations` section to the durable transcript. Add a compact reminder for `waiting_owner` sub-chats to the next parent prompt prefix.

- [ ] **Step 7: Integrate safe restart behavior**

Include active provider sub-chats in the restart-drain count and stop accepting new starts while restart drain is active. A process that starts with persisted running work marks it interrupted and does not resume automatically.

- [ ] **Step 8: Run backend integration tests**

Run:

```bash
pytest \
  tests/test_provider_subchats.py \
  tests/test_provider_subchat_routes.py \
  tests/test_restart_drain.py \
  tests/test_chat_subagents.py -q
```

Expected: PASS.

## Task 4: Add the owner-facing CLI and stock skill

**Files:**

- Modify: `ciao/cli.py`
- Create: `ciao/stock/skills/provider-consultation/SKILL.md`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_agent_assets.py`
- Modify: `tests/test_stock_package.py`

- [ ] **Step 1: Add failing CLI parser and HTTP tests**

Define this stable surface:

```text
ciao provider-chat start --chat-id ID --provider codex --model MODEL --message TEXT
ciao provider-chat send --subchat-id ID --message TEXT
ciao provider-chat close --subchat-id ID
ciao provider-chat cancel --subchat-id ID
ciao provider-chat extend --subchat-id ID
```

Assert JSON output contains `subchat_id`, `status`, `reply`, `usage`, and `error`. Verify start refuses when `CIAO_PROVIDER_SUBCHAT_ID` is already set.

- [ ] **Step 2: Run CLI tests and confirm failure**

Run: `pytest tests/test_cli.py -q -k provider_chat`

Expected: FAIL.

- [ ] **Step 3: Implement the CLI group**

Reuse the authenticated local HTTP helper and current `CIAO_CHAT_ID`, `CIAO_PROVIDER`, `CIAO_MODEL`, `CIAO_MODEL_BUCKET`, `CIAO_ACTIVE_WORKSPACE`, and `CIAO_ACTIVE_PROJECT` environment defaults. Always send `user_authorized: true` only when the command is invoked according to the skill contract.

- [ ] **Step 4: Create the stock skill**

The skill must state:

- Start only after the user explicitly asks for another provider or route.
- Use `start`, then zero or more `send` calls with the same ID.
- Relay participant clarification requests through the parent chat when only the user can answer.
- Do not let the user write directly into the child conversation.
- Close when enough information is gathered.
- Never start a nested provider consultation.
- Ask for fresh user authorization before `extend`.

- [ ] **Step 5: Run stock-asset tests**

Run: `pytest tests/test_cli.py tests/test_agent_assets.py tests/test_stock_package.py -q`

Expected: PASS.

## Task 5: Add the read-only live provider sub-chat panel

**Files:**

- Modify: `web/src/lib/types.ts`
- Modify: `web/src/stores/projects.ts`
- Modify: `web/src/stores/projects.test.ts`
- Create: `web/src/components/ProviderSubchatPanel.vue`
- Modify: `web/src/components/SubagentPanel.vue`
- Modify: `web/src/components/ChatPanel.vue`
- Modify: `web/src/components/__tests__/mountSmoke.test.ts`

- [ ] **Step 1: Add record/event types and failing store tests**

The store loads child records for the active parent, loads replay events on expansion, and updates them from global WebSocket events keyed by `parent_chat_id` and `subchat_id`. Verify events for other chats do not leak into the active panel.

- [ ] **Step 2: Run store tests and confirm failure**

Run: `cd web && npm test -- src/stores/projects.test.ts`

Expected: FAIL.

- [ ] **Step 3: Implement store hydration and live updates**

Add maps keyed by parent chat and sub-chat ID. Handle `provider_subchat_created`, `provider_subchat_status`, `provider_subchat_event`, and `provider_subchat_deleted`. Reload from the API after reconnect or `pageshow` so iOS WebSocket suspension does not lose messages.

- [ ] **Step 4: Extract a shared child-conversation shell**

Reuse the current SubagentPanel visual language for header, disclosure, status, duration, and transcript rows. Keep native subagent data and provider-subchat data paths distinct. Do not expose a composer.

- [ ] **Step 5: Implement `ProviderSubchatPanel.vue`**

Show `Owner ↔ Participant`, participant model, status, duration, message count, explicit owner and participant messages, tool activity, errors, and permission/question controls. Running panels start expanded; terminal panels start collapsed. Markdown goes through `safeMarkdown`.

- [ ] **Step 6: Attach panels to the originating turn**

In `ChatPanel.vue`, render child panels after the final answer associated with `parent_turn_index`, or inside the live trace while that turn is still running. Keep the user unable to send participant messages. Expose cancel and approval controls only.

- [ ] **Step 7: Run frontend tests and build**

Run: `cd web && npm test -- src/stores/projects.test.ts src/components/__tests__/mountSmoke.test.ts`

Run: `cd web && npm run build`

Expected: PASS.

## Task 6: Document, account for, and verify provider consultations

**Files:**

- Modify: `PWA_API.md`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/DEVELOPMENT.md`
- Modify: `web/README.md`
- Modify: `ciao/stock/skills/ciao-capabilities/SKILL.md`
- Modify: `tests/test_pwa_api_docs.py`

- [ ] **Step 1: Document every state-changing route**

Add Agent recipes for start, send, close, cancel, extend, permission response, and question response. Document read routes, explicit initiation, read-only UI, limits, restart behavior, and route identity.

- [ ] **Step 2: Update product and architecture docs**

Explain how provider consultations differ from full-chat handover, normal sidebar chats, and provider-native subagents. Include the metadata/JSONL storage locations, lifecycle manager, CLI/skill contract, and parent deletion/archive behavior.

- [ ] **Step 3: Include accounting without double counting**

Aggregate participant usage and cost under the parent chat's existing accounting scope, tagged with `provider_subchat_id`. Verify the participant's result is not counted again when quoted in the owner answer.

- [ ] **Step 4: Run all targeted backend tests**

Run:

```bash
pytest \
  tests/test_provider_subchats.py \
  tests/test_provider_subchat_routes.py \
  tests/test_cli.py \
  tests/test_agent_assets.py \
  tests/test_stock_package.py \
  tests/test_restart_drain.py \
  tests/test_chat_handover.py \
  tests/test_chat_subagents.py \
  tests/test_pwa_api_docs.py -q
```

Expected: PASS.

- [ ] **Step 5: Run frontend verification**

Run: `cd web && npm test && npm run build`

Expected: PASS.

- [ ] **Step 6: Run the full Python suite**

Run: `pytest -q`

Expected: PASS.

- [ ] **Step 7: Inspect the final diff**

Confirm no unrelated compact-chat work was overwritten or staged, no provider secrets or environment values were added, and the result matches `docs/superpowers/specs/2026-07-15-cross-provider-subchats-design.md`.
