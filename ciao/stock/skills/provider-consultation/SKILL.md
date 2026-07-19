---
name: provider-consultation
description: Start and communicate with a second provider route (the participant) as a read-only sub-chat attached to the originating turn. Use when the user explicitly asks to consult, handoff to, delegate to, or route tasks to another model or provider (e.g. Codex, Ollama, OpenRouter, Fable, etc.).
allowed-tools:
  - mcp__ciaobot__consultation_start
  - mcp__ciaobot__consultation_send
  - mcp__ciaobot__consultation_close
  - mcp__ciaobot__consultation_cancel
  - mcp__ciaobot__consultation_extend
---

# Provider Consultation

You can spawn and communicate with a second provider route (the participant) as a read-only sub-chat attached to the originating turn. This is useful when the user explicitly asks to consult another model/provider (e.g. Codex/GPT-4 or another Claude model) for a specialized sub-task.

> [!IMPORTANT]
> Do **NOT** search for or invoke provider binaries such as `codex` or `ollama`. All cross-provider delegation flows through Ciaobot's `consultation_*` MCP tools.

## MCP usage

- `consultation_start` initializes the participant and sends the first task.
- `consultation_send` sends follow-up context or answers.
- `consultation_close` finalizes a successful consultation; `consultation_cancel` aborts active work.
- `consultation_extend` requires explicit user authorization.

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
