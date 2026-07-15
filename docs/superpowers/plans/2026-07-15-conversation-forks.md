# Conversation Forks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user fork an active or archived conversation from any completed final agent answer into an independent chat in the same project.

**Architecture:** Add provider-neutral fork metadata and a synchronous `ProjectChatManager.fork_chat()` operation that validates a bounded visible-message snapshot, allocates a root-relative fork number, duplicates image ownership, and persists a fresh chat with one-time handover context. Expose it through one authenticated route and a final-answer-only action in the Vue chat panel.

**Tech Stack:** Python 3.12, Starlette, pytest, Vue 3, Pinia, TypeScript, Vitest

---

## Task 1: Lock the fork contract with manager tests

**Files:**

- Create: `tests/test_chat_fork.py`
- Modify: `ciao/web/project_chats.py`

- [ ] **Step 1: Add a manager test fixture and the happy-path test**

Create a temporary `ProjectChatManager`, project, and source chat. Exercise this contract:

```python
fork = manager.fork_chat(
    source.chat_id,
    messages=[
        {"role": "user", "content": "Question", "turn_index": 0},
        {"role": "assistant", "content": "Answer"},
    ],
    turn_index=0,
)

assert fork.project_id == source.project_id
assert fork.title == "Original · Fork 1"
assert fork.session_id == ""
assert fork.handover_context_pending is True
assert fork.user_turn_count == 1
assert fork.handover_messages[-1]["content"] == "Answer"
```

- [ ] **Step 2: Run the test and confirm the missing-method failure**

Run: `pytest tests/test_chat_fork.py -q`

Expected: FAIL because `ProjectChatManager.fork_chat` does not exist.

- [ ] **Step 3: Add inheritance and family-numbering tests**

Cover provider, model, model bucket, mode, thinking level, active and archived sources, `Fork 1` through `Fork 3`, and a fork of Fork 1 using the root title rather than producing `Fork 1 · Fork 1`.

- [ ] **Step 4: Add rejection tests**

Assert `ValueError` for empty snapshots, snapshots ending in a user/system/activity row, mismatched turn indexes, oversized selected turns, and missing projects. Assert `KeyError` for missing source chats.

- [ ] **Step 5: Add rollback and source-independence tests**

Patch `_save` to fail during fork persistence, then assert the allocated chat is removed and copied images are deleted. Verify renaming or deleting the source after a successful fork does not change or remove the fork.

## Task 2: Implement fork metadata, validation, persistence, and image ownership

**Files:**

- Modify: `ciao/web/project_chats.py`
- Test: `tests/test_chat_fork.py`

- [ ] **Step 1: Add persisted metadata to `ChatInfo`**

Add fields with backward-compatible empty defaults:

```python
forked_from_chat_id: str = ""
forked_from_turn_index: int | None = None
fork_root_chat_id: str = ""
fork_index: int = 0
fork_base_title: str = ""
```

Include them in `ChatInfo.to_dict()`, `_load()`, and `_state_payload()`.

- [ ] **Step 2: Add fork-specific message normalization**

Build on `_normalize_handover_messages`, but require the final normalized row to have `role == "assistant"`. Compute the selected turn as the zero-based count of preceding human rows and reject a mismatch. Preserve the selected turn when applying the 80-message and 60,000-character limits. If older rows are removed, prepend a visible system note:

```python
{
    "role": "system",
    "content": "Earlier conversation history was omitted when this fork was created.",
}
```

- [ ] **Step 3: Add root-relative title allocation**

For a non-fork source, use its ID and current title as the root and base title. For a fork source, reuse its `fork_root_chat_id` and `fork_base_title`. Compute `max(fork_index) + 1` across existing family members.

- [ ] **Step 4: Duplicate referenced images**

For each copied user row with `image_refs`, resolve every source ref, copy its bytes through `save_image_upload()`, replace the row refs, and populate the new chat's `user_turn_images`. The new refs must be owned by the fork so source deletion cannot break them.

- [ ] **Step 5: Implement atomic `fork_chat()`**

Create the new chat with inherited provider settings, then set fork metadata, copied messages, `handover_context_pending`, thinking level, and the copied human-turn count. If any later validation, copy, or save fails, remove the new chat and copied images before re-raising.

- [ ] **Step 6: Run the manager tests**

Run: `pytest tests/test_chat_fork.py tests/test_project_chat_persistence.py -q`

Expected: PASS.

## Task 3: Add the authenticated fork API

**Files:**

- Modify: `ciao/web/routes_api.py`
- Modify: `ciao/web/app.py`
- Create: `tests/test_chat_fork_route.py`

- [ ] **Step 1: Write route tests first**

Register only the new route in a small Starlette test app and cover:

```python
response = client.post(
    f"/api/chats/{source.chat_id}/fork",
    json={"messages": messages, "turn_index": 0},
)
assert response.status_code == 200
assert response.json()["title"] == "Original · Fork 1"
```

Also cover missing source/project as `404`, malformed JSON and invalid snapshots as `400`, and persistence failure as `500` without a leftover chat.

- [ ] **Step 2: Run the route test and confirm the missing-route failure**

Run: `pytest tests/test_chat_fork_route.py -q`

Expected: FAIL because `chat_fork` is missing.

- [ ] **Step 3: Implement and register `chat_fork`**

Parse `messages` as a list and `turn_index` as a non-negative integer. Call `pcm.fork_chat()`. Return `fork.to_dict(local=True)` on success. Register:

```python
Route("/api/chats/{chat_id}/fork", chat_fork, methods=["POST"]),
```

- [ ] **Step 4: Run route and API-doc guard tests**

Run: `pytest tests/test_chat_fork_route.py tests/test_pwa_api_docs.py -q`

Expected: the route test passes and the docs guard remains red until Task 5 documents the new recipe.

## Task 4: Add the final-answer-only PWA action

**Files:**

- Modify: `web/src/lib/types.ts`
- Modify: `web/src/stores/projects.ts`
- Modify: `web/src/stores/projects.test.ts`
- Modify: `web/src/components/ChatPanel.vue`
- Modify: `web/src/components/__tests__/mountSmoke.test.ts`

- [ ] **Step 1: Add frontend types and a failing store test**

Add fork metadata to `Chat`, then test a new action:

```ts
const fork = await store.forkChat(sourceId, messages, 0)
expect(apiPost).toHaveBeenCalledWith(`/chats/${sourceId}/fork`, {
  messages,
  turn_index: 0,
})
expect(store.activeChatId).toBe(fork.chat_id)
expect(store.messages[fork.chat_id]).toEqual(expect.any(Array))
```

The store action posts the snapshot, inserts the returned chat, disconnects only the source view socket, switches to the new chat, stores the copied messages immediately, and connects the fork's socket.

- [ ] **Step 2: Run the store test and confirm failure**

Run: `cd web && npm test -- src/stores/projects.test.ts`

Expected: FAIL because `forkChat` does not exist.

- [ ] **Step 3: Implement the store action**

Normalize the copied snapshot with the same `normalizeMessages()` path used by hydration. Do not send queued messages, current streaming text, or pending comments.

- [ ] **Step 4: Add the bubble action to `ChatPanel.vue`**

Render `Fork conversation from here` only inside `item.kind === 'assistant'`. Derive the source slice from the selected `ChatMessage`'s index in the active normalized message array. Count preceding user rows for `turn_index`. Maintain a per-message busy key to prevent duplicate requests.

On success, navigate to the returned chat route and focus the composer on the next Vue tick. On failure, retain the source chat and use the existing toast/error surface.

- [ ] **Step 5: Preserve action ergonomics**

Keep the action in the same Copy/Read aloud group, stop click propagation, add `aria-label="Fork conversation from here"`, and retain a 44px touch target without changing message selection behavior.

- [ ] **Step 6: Run frontend tests and type checking**

Run: `cd web && npm test -- src/stores/projects.test.ts src/components/__tests__/mountSmoke.test.ts`

Run: `cd web && npm run build`

Expected: PASS.

## Task 5: Document and verify the fork feature

**Files:**

- Modify: `PWA_API.md`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `web/README.md`
- Test: `tests/test_pwa_api_docs.py`

- [ ] **Step 1: Add the Agent recipe**

Document `POST /api/chats/{chat_id}/fork`, its `messages` and `turn_index` body, same-project behavior, fresh-session semantics, title numbering, and response.

- [ ] **Step 2: Update user and architecture documentation**

Mention the final-answer Fork action in `README.md`, the provider-neutral history-copy path in `docs/ARCHITECTURE.md`, and the final-answer-only UI convention in `web/README.md`. Preserve concurrent compact-chat edits.

- [ ] **Step 3: Run targeted regressions**

Run:

```bash
pytest \
  tests/test_chat_fork.py \
  tests/test_chat_fork_route.py \
  tests/test_chat_continue_route.py \
  tests/test_chat_handover.py \
  tests/test_chat_messages_archived.py \
  tests/test_project_chat_persistence.py \
  tests/test_pwa_api_docs.py -q
```

Run: `cd web && npm test && npm run build`

Expected: PASS.

- [ ] **Step 4: Inspect the final diff**

Confirm no compact-chat work was overwritten, no built static asset is staged unintentionally, and the implementation matches `docs/superpowers/specs/2026-07-15-conversation-forks-design.md`.
