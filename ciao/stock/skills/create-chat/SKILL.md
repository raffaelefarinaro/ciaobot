---
name: create-chat
description: Create a new Ciaobot chat and start its first turn using Ciaobot MCP tools. Use for a clean sub-topic, separate task, or context reset.
allowed-tools:
  - mcp__ciaobot__projects_list
  - mcp__ciaobot__chat_create
  - mcp__ciaobot__chat_send
---

# Create Chat

You can create a brand new chat session programmatically and trigger a model turn with an initial prompt. This is useful when the current conversation gets too long, when a task has a natural sub-topic that deserves its own clean context, or when spawning a dedicated task to run in parallel.

## MCP workflow

1. Use `context_get` for the current project or `projects_list` to resolve another project. Never guess IDs.
2. Call `chat_create`, inheriting provider/model unless the user requested an override. Common overrides: a different `project`, `workspace`, `model`, or `provider`.
3. Call `chat_send` with the initial prompt.
4. Report the created chat ID and title, and link the user to the new chat so they can follow up or check on progress.
