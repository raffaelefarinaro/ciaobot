---
name: provider-consultation
description: Start and communicate with a second provider route (the participant) as a read-only sub-chat attached to the originating turn. Use when the user explicitly asks to consult another model or provider.
allowed-tools:
  - Bash
---

# Provider Consultation

You can spawn and communicate with a second provider route (the participant) as a read-only sub-chat attached to the originating turn. This is useful when the user explicitly asks to consult another model/provider (e.g. Codex/GPT-4 or another Claude model) for a specialized sub-task.

## Usage

All commands must be executed via the `Bash` tool from the workspace root.

```bash
# Start a provider consultation
ciao provider-chat start --chat-id <parent_chat_id> --provider <provider> --model <model> --message "<initial_prompt>"

# Send follow-up messages to the participant
ciao provider-chat send --subchat-id <subchat_id> --message "<prompt>"

# Close/finalize the consultation
ciao provider-chat close --subchat-id <subchat_id>

# Cancel/abort active work
ciao provider-chat cancel --subchat-id <subchat_id>

# Extend consultation message/time limits
ciao provider-chat extend --subchat-id <subchat_id>
```

## Protocol & Guidelines

1. **Explicit Request**: Start a consultation **only** after the user explicitly asks for another provider or route. Do not spawn consultations unsolicited.
2. **Consultation Lifecycle**:
   - Use `start` to initialize and send the first task message to the participant.
   - Use `send` to continue the conversation if follow-up clarifications or instructions are needed.
   - Use `close` once the consultation has successfully finished and enough information has been gathered.
3. **Clarifications & Relaying**: If the participant asks a clarifying question that requires user knowledge, relay that request through the parent chat to the user, collect the user's answer, and send it back to the participant via `send`.
4. **Read-only Child**: Do not let the user write directly into the child sub-chat. You act as the sole conduit.
5. **No Nesting**: Never start a nested provider consultation inside an existing sub-chat.
6. **Limit Extensions**: If the sub-chat hits its message limit (12 messages) or time limit (30 minutes), ask the user for explicit authorization before executing the `extend` command.
