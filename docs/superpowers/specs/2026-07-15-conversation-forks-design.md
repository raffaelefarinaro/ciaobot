# Conversation Forks Design

Date: 2026-07-15

Status: Approved design, pending written-spec review

## Summary

Ciaobot will let the user fork a conversation from any completed final agent answer. Forking creates a new active chat in the same project and workspace, copies visible conversation history through the selected answer, and opens the new chat ready for the user's next message.

The fork starts a fresh provider session. It keeps the source chat's provider, model, model bucket, mode, and thinking level, but it does not share future messages or provider state with the source.

Forks receive deterministic titles such as `Original title · Fork 1` and `Original title · Fork 2`.

## Goals

- Add a Fork action to completed final agent-answer bubbles.
- Create an independent chat in the source chat's project and workspace.
- Preserve visible history through the selected answer.
- Keep provider and execution settings from the source chat.
- Use a fresh provider session that is seeded on the first post-fork user turn.
- Open the fork immediately so the user can continue from that point.
- Number all descendants from one root conversation without producing titles such as `Fork 1 · Fork 1`.
- Support forks from active and archived source chats when their project still exists.

## Non-goals

- Forking from user messages.
- Forking from reasoning, commentary, tool activity, system rows, file cards, or subagent panels.
- Automatically generating an answer immediately after the fork is created.
- Mutating, truncating, archiving, or otherwise changing the source chat.
- Keeping source and fork synchronized after creation.
- Switching provider or model as part of the fork action. The existing model picker and handover flow handle later changes.
- Representing forks as nested sidebar items in the first version.

## Approach

### Selected: provider-neutral history copy

The frontend sends the normalized visible message rows from the beginning of the chat through the selected final agent answer. The backend validates and normalizes that snapshot, creates a new chat, and stores the copied rows as the new chat's pre-session history and one-time provider context.

This follows the existing provider-handover and archived-chat-continuation pattern. It works for Claude, Codex, Ollama, and OpenRouter routes without depending on provider-specific history APIs.

### Rejected: native provider session fork

Claude and Codex expose session-fork primitives, but Ciaobot currently uses them to branch the latest provider state. They do not provide one consistent way to branch from an arbitrary earlier visible message across all routes.

### Rejected: temporary archive and continuation

Archived-chat continuation already creates a fresh chat from a transcript, but archiving the source solely to make a fork would trigger unrelated transcript, insight, memory, cleanup, and UI behavior. Forking must not alter the source chat.

## User experience

Every rendered final agent-answer bubble gets a Fork action beside the existing Copy and Read aloud actions. The action is not present on user messages or trace rows.

The button label and accessible name are `Fork conversation from here`. While creation is running, that bubble's fork action is disabled and shows a busy state.

On success:

1. The new chat is inserted into the same project's chat list.
2. The copied history is available immediately.
3. Ciaobot switches the active pane to the new chat.
4. The composer receives focus.
5. The new chat waits for the user's next message.

The source chat remains active or archived exactly as it was. If it still has work running, that work continues in the source chat after the UI switches to the fork.

On failure, Ciaobot keeps the source chat open and shows an error toast. It never leaves an empty fork record behind.

## Fork semantics

The fork point is a completed final agent answer. The copied snapshot includes every visible normalized row through that selected answer, including preceding user messages and useful visible tool or system rows. It excludes any later rows, current streaming output, queued messages, and pending comments.

Only the explicit inter-message history is copied. Source-chat runtime state is not copied:

- Provider session ID
- Retry state
- Pending structured question
- Queued messages
- Background-agent watcher state
- Active stream
- Loop attachment
- Read or unread state

The fork inherits:

- Project and workspace through the source `project_id`
- Provider
- Model
- Model bucket
- Mode
- Thinking level

The fork starts with no provider session ID. On the first user message, Ciaobot injects the copied history as one-time hidden context and starts a fresh provider session. Later turns resume that new session normally.

## Data model

`ChatInfo` gains persisted fork metadata:

- `forked_from_chat_id`: the immediate source chat
- `forked_from_turn_index`: the selected final-answer turn
- `fork_root_chat_id`: the first non-fork ancestor
- `fork_index`: the root-relative ordinal
- `fork_base_title`: the title captured when the first fork in the family is created

These fields are returned in the normal chat JSON so the UI and future navigation features can identify fork relationships. They do not change sidebar grouping in the first version.

Copied history uses the existing handover-message shape. The fork path also records the copied human-turn count so the new chat's future `turn_index` values continue after the copied history rather than restarting at zero.

Fork metadata is independent after creation. Renaming a source or fork does not rename existing descendants. Deleting a source does not delete its forks.

## Title numbering

For the first fork of a normal chat:

- `fork_root_chat_id` is the source chat ID.
- `fork_base_title` is the source title at creation time.
- `fork_index` is `1`.
- The title is `<fork_base_title> · Fork 1`.

For any later fork in the same family, including a fork created from another fork, Ciaobot finds the highest existing `fork_index` for that root and adds one. It reuses the stored `fork_base_title`.

Example:

- `Release planning`
- `Release planning · Fork 1`
- `Release planning · Fork 2`, even when created from Fork 1

Index allocation and chat creation happen in one synchronous manager operation, so two requests handled by the same process cannot receive the same index.

## Backend API

Add `POST /api/chats/{chat_id}/fork`.

The request contains:

- The normalized visible `messages` snapshot through the selected answer
- The selected `turn_index`

The route validates that:

- The source chat exists.
- Its project exists.
- The snapshot is non-empty and ends with a final assistant message.
- The requested turn matches the snapshot's final completed turn. For the
  provider-neutral payload, this is the zero-based count of preceding human
  messages.
- The request stays within message-count and payload-size safety limits.

The response is the new `ChatInfo` with `local: true`. The endpoint is documented in `PWA_API.md` because it changes chat state.

## Context and history limits

Fork creation accepts the same bounded normalized message representation used for explicit provider handover. If a source exceeds the safety limit, the oldest copied rows are omitted and the fork begins with a visible system note stating that earlier history was truncated.

The selected agent answer and its complete turn are always retained. The backend rejects a request when the selected turn alone exceeds the payload limit rather than silently cutting the selected answer.

Referenced image files are duplicated to fork-owned media refs during
creation, and the copied messages are rewritten to those refs. The fork's
`user_turn_images` map owns the duplicates. Deleting either source or fork
therefore cannot break images in the other chat.

## Error handling

- A missing source or project returns `404`.
- A malformed snapshot, invalid final role, mismatched turn, or oversized selected turn returns `400`.
- Forking from an unsupported rendered item is prevented in the UI and rejected by the backend.
- Persistence failure rolls back the in-memory new chat and returns `500`.
- Loading the copied history after creation may be retried without creating another fork because the successful response already contains one stable chat ID.

## Testing strategy

### Backend tests

- Fork an active Claude chat from an earlier final answer.
- Fork a Codex chat using the same provider-neutral snapshot contract.
- Fork an archived chat without changing its archived state.
- Inherit provider, model, bucket, mode, thinking level, project, and workspace.
- Start with a fresh provider session and one-time copied context.
- Exclude messages after the selected agent answer.
- Continue `turn_index` after copied human turns.
- Allocate root-relative `Fork 1`, `Fork 2`, and `Fork 3` names across fork-of-fork creation.
- Preserve descendants when a source is renamed or deleted.
- Reject user-message, trace-row, empty, mismatched-turn, and oversized snapshots.
- Roll back on persistence failure.

### Route and store tests

- Document and register the new state-changing route.
- Add the new chat to the correct project and switch to it.
- Load copied history immediately.
- Preserve the source's active stream when forking an earlier completed answer.
- Show a stable error toast without creating a duplicate fork on failure.

### Frontend tests

- Render the Fork action only on final agent-answer bubbles.
- Do not render it on user, system, reasoning, tool, file, or subagent rows.
- Send only history through the selected answer.
- Show per-message busy state and prevent double submission.
- Switch to the new chat and focus the composer after success.
- Keep mobile tap targets at least 44 pixels and preserve existing message-action behavior.

### Regression tests

- Full-chat handover
- Archived-chat continuation
- Chat deletion and archive
- Message copying and read-aloud actions
- Message hydration and WebSocket replay deduplication
- Native provider session fork behavior used for busy-session recovery

## Complexity estimate

Conversation forking is a small-to-medium feature. The existing chat creation, handover context, message actions, and archived continuation cover most of the mechanics. The main work is stable fork metadata and numbering, message slicing, the new route, and UI behavior.

## Related design

Cross-provider consultations are specified separately in [Cross-Provider Sub-Chats Design](2026-07-15-cross-provider-subchats-design.md). Conversation forks create user-owned sidebar chats. Provider sub-chats remain read-only child conversations owned by Ciaobot.
