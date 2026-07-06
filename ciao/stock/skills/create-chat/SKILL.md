---
name: create-chat
description: Create a new chat from within a chat session using the `ciao create-chat` CLI, preserving current model/settings by default. Use when you want to start a new chat to continue a sub-topic, spin off a subtask, or context-clear.
allowed-tools:
  - Bash
---

# Create Chat

You can create a brand new chat session programmatically and trigger a model turn with an initial prompt. This is useful when the current conversation gets too long, when a task has a natural sub-topic that deserves its own clean context, or when spawning a dedicated task to run in parallel.

## Usage

Run the packaged CLI from the workspace root using the `Bash` tool:

```bash
ciao create-chat --prompt "<initial_message>" [options]
```

### Required Arguments
- `--prompt` — The starting prompt that the model will immediately begin responding to in the new chat.

### Optional Arguments
- `--title` — The title of the new chat. Defaults to "New Chat" if omitted (and the auto-titler will rename it after the first turn).
- `--workspace` — Target logical workspace. Defaults to the current workspace of the parent chat (inherited via `CIAO_ACTIVE_WORKSPACE`).
- `--project` — Target project ID or case-insensitive project name (e.g. `proj-xxxx` or a project name). Defaults to the current project (inherited via `CIAO_ACTIVE_PROJECT`), falling back to the workspace's "General" project.
- `--model` — Model override. Defaults to the current model (inherited via `CIAO_MODEL`), falling back to the workspace default.
- `--provider` — Provider override. Defaults to the current provider (inherited via `CIAO_PROVIDER`).
- `--model-bucket` — Model bucket override. Defaults to the current bucket (inherited via `CIAO_MODEL_BUCKET`).
- `--workspace-root` — Workspace directory containing `.env`. Defaults to the current directory.

## Example Recipes

### 1. Spawning a new chat for a sub-topic in the current project/workspace (Recommended)
Keep same settings and project, but start a fresh context:
```bash
ciao create-chat --prompt "Let's research the new API changes we just discussed." --title "API Research"
```

### 2. Spawning a chat in another workspace's General project
```bash
ciao create-chat --prompt "Remind me to pick up milk later" --workspace personal
```

### 3. Spawning a chat with a specific model override
```bash
ciao create-chat --prompt "Critique this design doc carefully" --model sonnet --title "Design Doc Critique"
```

## Feedback and Output
On success, the command outputs the created chat ID, settings applied, and the direct URL to open it in the PWA:
```
Success: Created chat 'API Research' (chat-e3a5f28b)
Workspace: work | Project: proj-72081e2d
Model: sonnet (claude)
PWA URL: http://127.0.0.1:8443/chat/chat-e3a5f28b
```
Report the created chat details and link back to the user so they can follow up or check on the progress!
