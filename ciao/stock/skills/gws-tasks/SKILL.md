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

  - `delete` — Deletes the authenticated user's specified task list. If the list contains assigned tasks, both the assigned tasks and the original tasks in the assignment surface (Docs, Chat Spaces) are deleted. The user's default task list cannot be deleted.
  - `get` — Returns the authenticated user's specified task list.
  - `insert` — Creates a new task list and adds it to the authenticated user's task lists.
  - `list` — Returns all the authenticated user's task lists.
  - `patch` — Updates the authenticated user's specified task list.
  - `update` — Updates the authenticated user's specified task list.

### tasks

  - `clear` — Clears all completed tasks from the specified task list. The affected tasks will be marked as 'hidden' and no longer be returned by default when retrieving all tasks for a task list.
  - `delete` — Deletes the specified task from the task list. If the task is assigned, both the assigned task and the original task (in Docs, Chat Spaces) are deleted. To delete the assigned task only, navigate to the assignment surface and unassign the task from there.
  - `get` — Returns the specified task.
  - `insert` — Creates a new task on the specified task list. A user can have up to 20,000 non-hidden tasks per list and up to 100,000 tasks in total at a time.
  - `list` — Returns all tasks in the specified task list. A user can have up to 20,000 non-hidden tasks per list and up to 100,000 tasks in total at a time.
  - `move` — Moves the specified task to another position in the destination task list.
  - `patch` — Updates the specified task.
  - `update` — Updates the specified task.

## Discovering Commands

Browse a service with `ciao gws "$GWS_PROFILE" <service> --help`. Before calling an API method, run `ciao gws "$GWS_PROFILE" schema <service>.<resource>.<method>` and build `--params` and `--json` from its output.
