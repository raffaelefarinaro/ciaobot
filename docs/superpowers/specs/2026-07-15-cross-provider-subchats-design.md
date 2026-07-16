# Cross-Provider Sub-Chats Design

Date: 2026-07-15

Status: Approved design, pending written-spec review

## Summary

Ciaobot will support user-requested, multi-turn conversations between the provider that owns a parent chat and a second provider route. These conversations appear as read-only sub-chats attached to the originating parent turn.

For example, a user can ask Claude to consult Codex before answering. Claude starts a Codex sub-chat, sends a task and selected context, receives Codex's reply, asks follow-up questions when useful, and then synthesizes the result in the parent chat. If Codex needs information Claude does not have, Claude asks the user in the parent chat and later resumes the same Codex session.

Ciaobot owns this lifecycle. Native Claude or Codex subagents remain provider-owned and continue to use their existing tracking paths.

## Goals

- Let the user explicitly ask the current owner agent to consult another configured provider route.
- Support several owner-participant exchanges before the owner answers the user.
- Preserve each participant's provider session across exchanges and parent turns.
- Show the exchange live in a read-only sub-chat attached to the parent turn.
- Allow the owner to relay a participant's clarification request to the user.
- Reuse the existing provider, streaming, permission, accounting, and workspace-context infrastructure.
- Persist sub-chat state and transcript across reloads and server restarts.
- Fail without breaking or replacing the parent chat.

## Non-goals

- Autonomous provider consultation without an explicit user request.
- Allowing the user to send messages directly into a provider sub-chat.
- Replacing the existing full-chat provider handover.
- Replacing native provider subagents or forcing them onto the new backend.
- Nested cross-provider consultations started by a participant.
- Parallel cross-provider consultations inside the same parent chat in the first version.
- Automatically restarting interrupted work after a server restart.

## Current architecture and constraints

Ciaobot already has most of the execution primitives needed for this feature:

- Every normal chat carries an explicit `provider`, `model`, and, for Claude-backed routing, `model_bucket`.
- `ProviderService` normalizes Claude and Codex streaming into one event contract.
- Ollama and OpenRouter currently run through `ClaudeProvider` with different routing environments. A distinct participant can therefore be a different provider, such as Codex, or a different route, such as Ollama.
- The model picker already supports full-chat handover. It closes the old provider session, keeps the visible chat, and injects prior visible messages into the fresh provider session once.
- Normal chats already support background streams, permissions, questions, tool activity, cancellation, persistence, and provider session resume.
- Native subagent UI is based on provider-owned transcripts. Cross-provider conversations cannot rely on that representation because Ciaobot must coordinate independent provider sessions.

The design therefore adds a Ciaobot-owned child-conversation layer. It does not add another provider abstraction or modify the meaning of full-chat handover.

## Product behavior

### Explicit initiation

The owner may start a provider sub-chat only after the user explicitly asks it to consult another provider or route. Naming the target is sufficient, for example:

> Ask Codex to review this proposal and discuss any disagreements before answering me.

The owner may decide what task and context to send, and how many follow-up exchanges are useful within the configured limits. It may not silently add another participant because it believes a second opinion would help.

This rule is enforced as agent guidance rather than treated as a security boundary. The owner already has shell and provider access. Every consultation is visible in the parent chat, so unauthorized use is auditable.

### Read-only sub-chat

The sub-chat is attached to the parent turn that started it. It is expanded while running and collapses after reaching a terminal state. Its header shows:

- Owner route and participant route, for example `Claude ↔ Codex` or `Claude ↔ Ollama`
- Participant model
- Status
- Duration
- Message count

The body shows explicit owner-to-participant and participant-to-owner messages, plus ordinary normalized tool activity. It does not expose private chain-of-thought. The user cannot compose a message inside the sub-chat, but may use platform controls such as cancel and permission approval.

Provider sub-chats do not appear in the sidebar, Recent chats, project chat lists, or search results as independent chats.

### Conversation flow

1. The user explicitly asks the owner to consult a named provider route.
2. The owner starts a sub-chat with a task and selected context.
3. Ciaobot creates an independent participant provider session and streams its response into the sub-chat.
4. The participant's completed reply is returned to the owner as the result of the consultation action.
5. The owner may send another message using the same sub-chat ID. Each call reuses the participant session.
6. If the participant asks for information the owner has, the owner replies directly.
7. If the participant asks for information only the user can provide, the owner ends its current parent turn with that question. The sub-chat enters `waiting_owner` and remains resumable.
8. On the user's next message, the owner receives a compact reminder about the open sub-chat and may relay the relevant answer to the participant.
9. When the owner has enough information, it closes the sub-chat and gives the user a synthesized answer in the parent chat.

The participant receives only the task and context the owner passes. Ciaobot does not copy the full parent transcript automatically.

## Architecture

### ProviderSubchat record

Each sub-chat has a persisted record with these fields:

- `subchat_id`
- `parent_chat_id`
- `parent_turn_index`
- `workspace`
- `project_id`
- Owner provider, model, model bucket, and user-facing route label
- Participant provider, model, model bucket, and user-facing route label
- Participant provider session ID
- Status: `created`, `running`, `waiting_owner`, `completed`, `cancelled`, `failed`, or `interrupted`
- Created, started, updated, and completed timestamps
- Cumulative active duration
- Exchanged-message count
- Normalized usage and quota data
- Last structured error, when present

Metadata is stored separately from transcript events so provider output does not inflate the normal `web_projects.json` chat store. Storage uses:

- `.runtime/provider_subchats.json` for the bounded metadata index
- `.runtime/provider_subchats/<subchat_id>.jsonl` for append-only normalized messages and stream events

Archived parent transcripts include a compact provider-subchat section beneath the originating turn. Deleting the parent chat deletes its provider-subchat metadata and JSONL transcripts.

### ProviderSubchatManager

`ProviderSubchatManager` owns creation, message dispatch, persistence, limits, cancellation, and restart reconciliation. It is independent of `ProjectChatManager` chat CRUD and native `subagent_tracking`, but it receives the parent chat manager as a dependency for parent metadata, workspace routing, events, and deletion or archive hooks.

Every active sub-chat gets its own `ProviderService`. This remains true when owner and participant use the same harness with different routes, such as Anthropic through Claude and Ollama through Claude. Route identity is the complete provider, model, and model-bucket tuple, not just the `provider` string.

Participant requests inherit the parent chat's:

- Workspace and project environment
- Permission mode
- Workspace tool restrictions
- Provider routing configuration

The participant request also receives a Ciaobot consultation instruction stating that it is talking to an owner agent, not directly to the user. It may ask the owner for clarification and must not start another cross-provider consultation. Ciaobot marks the participant environment with its current sub-chat ID, and the backend rejects nested consultation creation from that environment.

### Owner-facing action

Ciaobot exposes one provider-neutral consultation capability with operations to:

- Start a sub-chat
- Send the next owner message
- Close a completed consultation
- Cancel active work
- Extend a limit after fresh user authorization

Both Claude and Codex must be able to invoke the same capability. The product-facing interface is a stock consultation skill backed by a local Ciaobot CLI command and authenticated HTTP API. This follows the existing `ciao create-chat` pattern and avoids adding a new MCP server solely for this feature.

A start or send call waits for one participant turn to finish, while the participant stream is also published to the PWA. The command returns structured JSON containing the sub-chat ID, status, participant reply, usage, and error details. The owner can then decide whether to continue or close.

The local API includes read endpoints for parent sub-chats and transcript events, plus state-changing endpoints for start, send, close, cancel, limit extension, permission responses, and structured-question responses. These endpoints must be documented in `PWA_API.md` when implemented.

### Frontend event model

Provider-subchat events use the existing global event socket for lifecycle updates and a sub-chat event stream for participant output. The frontend normalizes native subagents and provider sub-chats into a shared presentation model, while preserving their different backend sources.

The frontend extracts the visual shell of `SubagentPanel` into a shared read-only child-conversation component. Provider sub-chat messages come from Ciaobot's JSONL transcript rather than provider session discovery.

## Permissions and tool use

Read-only describes the user's conversation surface, not the participant's tool access. The participant may use tools allowed by the parent chat's mode and workspace policy.

Permission and structured-question events are routed through the same PWA interaction patterns used by normal chats. A pending approval can appear inside the expanded sub-chat, and the user may approve or deny it. The owner remains blocked until the participant turn completes, is cancelled, or fails.

External sends and other actions that already require confirmation continue to require it. Explicitly requesting a consultation does not grant broader authority than the original parent task.

## Limits and lifecycle

- A parent chat may have only one non-terminal provider sub-chat at a time.
- Sequential completed consultations are allowed.
- A consultation may exchange up to 12 explicit owner and participant messages before pausing.
- A consultation may consume up to 30 cumulative minutes of active participant execution before pausing.
- Time spent waiting for the owner or user does not count toward the active-time limit.
- The owner must ask the user before extending either limit. Each authorization grants another block of 12 messages and 30 active minutes.
- A participant cannot start another provider sub-chat.
- While waiting between parent turns, the participant session ID and transcript remain persisted. No model process is considered actively running.

When a parent chat is archived, active participant work is cancelled and its transcript is preserved. When a parent chat is deleted, its sub-chat records and transcripts are removed.

## Failure handling

Failures are local to the provider sub-chat and never replace or corrupt the parent provider session.

- Missing installation or authentication fails before participant dispatch.
- Invalid routing or model selection returns a validation error.
- Quota, provider, tool, and permission failures are normalized, persisted, displayed, and returned to the owner.
- User cancellation stops the participant's active handle, preserves the transcript, and returns `cancelled` to the waiting owner action.
- A server shutdown marks every `running` sub-chat as `interrupted` on startup.
- Interrupted work never restarts automatically. After a later explicit user request, the owner can resume the stored provider session or start a replacement consultation.
- If resume fails because the provider session is unavailable, the owner receives a structured error and may ask the user whether to start a fresh sub-chat with the preserved transcript as selected context.

The parent owner is always responsible for explaining a failed consultation to the user or continuing without it.

## Model and route selection

The user must name a participant route, such as Codex, Claude, Ollama, or OpenRouter. Naming a specific model is optional.

When the user names only a route, Ciaobot resolves the participant model using the same defaults and alias mapping shown in the model picker for that route. When the user names a model, the owner passes it through and the backend validates it against the route's available catalog.

The owner may not silently replace the named route with another route. Existing same-route model fallback behavior applies to capability errors and is shown in the sub-chat transcript.

## Accounting and observability

Participant usage, effective model, quota details, duration, and terminal status are recorded on the sub-chat. Usage also feeds the existing aggregate accounting path so a consultation is not invisible in Ciaobot's totals.

Active provider sub-chats contribute to the existing active-chat indicator for menu-bar and restart-drain behavior. Logging uses sub-chat IDs and parent chat IDs, without duplicating prompt content into normal application logs.

## Testing strategy

### Unit tests

- Record serialization, metadata index updates, and JSONL append behavior
- Valid and invalid state transitions
- One-active-sub-chat enforcement
- Message and active-time limits, including explicit extension
- Parent archive and delete hooks
- Restart reconciliation from `running` to `interrupted`
- Route identity across Claude, Codex, Ollama, and OpenRouter configurations
- Context isolation and nested-consultation rejection

### Manager and API tests

- Start, send, close, cancel, resume, and extension flows with fake providers
- Multi-turn owner-participant clarification
- A clarification that pauses across two parent turns
- Permission, structured-question, quota, authentication, and provider failures
- CLI authentication, structured output, and blocking-call cancellation
- Usage and active-chat accounting

### Frontend tests

- Live event streaming and message ordering
- Expanded-running and collapsed-terminal presentation
- Read-only composition behavior
- Permission and cancel controls
- Reload reconstruction from persisted transcript
- Multiple sequential sub-chats attached to their correct parent turns

### Regression tests

- Existing native subagent tracking and synthesis nudges
- Full-chat handover between Claude and Codex
- Anthropic, Ollama, and OpenRouter route switching
- Parent chat archive, deletion, restart drain, and message loading

Provider-specific live smoke tests remain optional and separate from the deterministic test suite.

## Complexity estimate

This is a medium-sized feature rather than a provider rewrite. A reliable first version is expected to take roughly two to three engineering weeks, including backend lifecycle and persistence, the agent-facing command, API routes, frontend streaming and presentation, restart behavior, documentation, and regression coverage.

## Related design

User-created branches of a normal conversation are specified separately in [Conversation Forks Design](2026-07-15-conversation-forks-design.md). Conversation forks create independent sidebar chats. Provider sub-chats remain read-only child conversations attached to a parent turn.
