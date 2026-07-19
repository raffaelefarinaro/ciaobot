---
name: ciao-automations
description: Create, edit, list, run, pause, or delete Ciaobot native schedules and in-chat loops. Trigger on "schedule", "recurring", "every Monday", "monthly reminder", "loop", "every 10 minutes", "keep checking", or when converting ad-hoc checks into automation. Do not use claude.ai cloud Routines.
---

# Ciaobot Automations

Ciaobot has a timezone-aware native scheduler and sub-day chat loops. Schedules dispatch recurring, manual, or one-off prompts into a target project or chat. Loops re-send one prompt into a fixed chat every N minutes and retain that chat's context.

On server startup, an enabled schedule with a missed latest occurrence runs once; older intervals are not replayed. Loops do not catch up after downtime.

## Control surface

- Use the authenticated Ciaobot MCP tools: `projects_list`, `chats_list`, `schedule_preview`, the `schedule_*` lifecycle tools, and the `loop_*` lifecycle tools.
- Do not call the signed-cookie PWA API with curl and do not edit `.runtime` JSON directly.

The domain implementation is in `ciao/schedules.py` and `ciao/loops.py`; the PWA and MCP share those validated managers.

## MCP workflow

1. Use `projects_list` or `chats_list` to resolve a target by name. Never guess an ID.
2. For a new recurring schedule, show a concise draft and obtain confirmation unless the user already explicitly asked you to apply it.
3. Call `schedule_preview`. Treat a missing/invalid `next_run` as a validation failure.
4. Call `schedule_create`; report its `schedule_id` and next run.
5. Use the lifecycle-specific tool afterward: update, pause/resume, run now, or delete. System schedules cannot be deleted and expose only safe editable fields.

For loops, resolve one existing `chat_id`, then call `loop_create`; call `loop_start` when it should run immediately. `autostart=true` controls boot behavior only.

## Schedule fields

- **`daily_time`** — local `HH:MM` time interpreted in `timezone` (the persisted legacy field is named `daily_time_utc`).
- **`timezone`** — IANA name such as `Europe/Rome`. Use the user's local timezone unless they ask for UTC.
- **`frequency`** — `daily`, `weekly`, `monthly`, `manual`, or `once`.
- **`days_of_week`** — weekly only; lowercase `mon tue wed thu fri sat sun`.
- **`day_of_month`** — 1–31, monthly only.
- **`run_at_date`** — `YYYY-MM-DD`, once only, and must be in the future.
- **`project_id`** — creates a fresh chat in that project per run. Preferred for vault-aware automation.
- **`chat_id`** — posts into one existing chat. Use only when conversation continuity matters.
- **`model` / `provider`** — empty inherits the target workspace at dispatch time. Override only when necessary.
- **`archive_policy`** — `manual` or `auto`.
- **`enabled`** — paused schedules remain runnable manually.

System schedules have `scope=system`. They cannot be removed and only their allowed overlay fields can change.

### Prompt placeholders

- `{{ERROR_LOG}}` — sanitized tail of the server error log.
- `{{ISSUE_REPORT}}` — formatted server errors plus failed background jobs.

After a clean run that uses a placeholder, Ciaobot clears the consumed error log.

## Prompt conventions

1. Start with the goal in 3–7 words; it becomes a useful chat-title hint.
2. Keep only schedule-specific logic in the prompt. Fresh project runs already inherit canonical docs and skills.
3. Edit the vault only when a signal changed; avoid repeated no-change notes.
4. If a run edits a git-backed vault, include the desired commit policy.
5. For routine reviews, exit early with a one-line no-op when there is nothing to process.

Aim for at most 1000 characters for a simple check and 4000 for an aggregation/review.

## Loops

- Loops have no model field; each iteration uses the target chat's current model and mode.
- `autostart=true` starts it on server boot. Live running/stopped state is manager-owned.
- If the target chat is busy, an iteration is skipped and retried on a later tick; it is not queued.
- If the target chat is missing or archived, the loop stops.
- A loop prompt should give a short fixed no-change response so repeated turns stay cheap and scannable.

## Cross-reference durable automations

When a schedule owns a project or document, mention its returned `schedule_id` once in that artifact, for example: `Auto-rechecked weekly: Ciaobot schedule sched-dd1c0790 (Mon 09:00).`

## When not to use this skill

- Do not use cloud-side claude.ai Routines or a provider `/schedule`; they bypass Ciaobot's project/vault dispatch.
- Prefer the user's task system for a simple one-off reminder they will action manually, when one is configured.
- Use a loop, not a schedule, for sub-day recurrence that must retain one conversation's context.
