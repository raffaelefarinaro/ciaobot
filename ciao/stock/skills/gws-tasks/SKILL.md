---
name: gws-tasks
description: "Use to list, create, complete or delete Google Tasks and task lists."
metadata:
  version: 0.22.5
---

# tasks (v1)

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (via `ciao gws`), global flags, and security rules.

```bash
ciao gws "$GWS_PROFILE" tasks <resource> <method> [flags]
```

## API Resources

### tasklists

  - `delete` — Deletes the authenticated user's specified task list.
  - `get` — Returns the authenticated user's specified task list.
  - `insert` — Creates a new task list and adds it to the authenticated user's task lists.
  - `list` — Returns all the authenticated user's task lists.
  - `patch` — Updates the authenticated user's specified task list.
  - `update` — Updates the authenticated user's specified task list.

### tasks

  - `clear` — Clears all completed tasks from the specified task list.
  - `delete` — Deletes the specified task from the task list.
  - `get` — Returns the specified task.
  - `insert` — Creates a new task on the specified task list.
  - `list` — Returns all tasks in the specified task list.
  - `move` — Moves the specified task to another position in the destination task list.
  - `patch` — Updates the specified task.
  - `update` — Updates the specified task.

## Discovering Commands

Browse a service with `ciao gws "$GWS_PROFILE" <service> --help`. Before calling an API method, run `ciao gws "$GWS_PROFILE" schema <service>.<resource>.<method>` and build `--params` and `--json` from its output.
