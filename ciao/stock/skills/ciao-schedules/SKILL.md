---
name: ciao-schedules
description: Create, edit, list, or delete Ciaobot native schedules in `.runtime/schedules.json`. Trigger on "schedule", "recurring", "every Monday", "monthly reminder", or when converting ad-hoc checks into automation. Do not use claude.ai cloud Routines.
---

# Ciaobot Schedules

Ciaobot has its own scheduler. Each tick (every minute) the `ScheduleManager` reads `.runtime/schedules.json` and dispatches matching entries as new chat turns into a target PWA project or chat. No service restart needed when you add or edit a row: the store is reloaded on every tick.

## Where things live

- **Schedule store:** `<workspace root>/.runtime/schedules.json`
- **Dataclass + tick loop:** `ciao/schedules.py` (`ScheduleEntry`, `ScheduleStore`, `ScheduleManager`)
- **HTTP API:** `POST /api/schedules` (create), `GET /api/schedules` (list), `PATCH /api/schedules/{id}`, `DELETE /api/schedules/{id}`, `POST /api/schedule-run/{id}` (fire now).
- **Project / chat map:** `.runtime/web_projects.json` (find `web_project_id` by project name)

## API vs direct file write

The HTTP API is protected by a signed session cookie. From inside a Claude session you don't have that cookie, so calls return `401 unauthorized`. **Always write directly to `.runtime/schedules.json`** using the recipe below. The tick loop will pick the change up within ~60s.

## Entry shape

Every entry is a flat JSON object. Use exactly these keys (the store filters unknown keys at load time, but matching keeps the file diffable). `mode` is intentionally absent: it's runtime-only.

```json
{
  "archive_policy": "manual",
  "chat_id": 0,
  "created_at": "2026-05-11T13:42:00Z",
  "daily_time_utc": "09:00",
  "day_of_month": null,
  "days_of_week": ["mon"],
  "editable": true,
  "enabled": true,
  "frequency": "weekly",
  "last_triggered_on": "",
  "model": "",
  "prompt": "Your full prompt here.",
  "removable": true,
  "run_at_date": null,
  "schedule_id": "sched-dd1c0790",
  "scope": "user",
  "thread_id": null,
  "timezone_name": "UTC",
  "title": "",
  "web_chat_id": null,
  "web_project_id": "proj-72081e2d",
  "workspace": ""
}
```

### Field semantics (the non-obvious ones)

- **`daily_time_utc`** — misnamed. It's local time, interpreted in `timezone_name`. Format `"HH:MM"`.
- **`timezone_name`** — an IANA zone name (e.g. `Europe/Zurich`). Use the user's local timezone unless they ask for UTC.
- **`frequency`** — one of `daily`, `weekly`, `monthly`, `manual`, `once`.
- **`days_of_week`** — only used when `frequency="weekly"`. Lowercase 3-letter abbreviations: `mon tue wed thu fri sat sun`. `null` or empty means every day-of-week (effectively daily).
- **`day_of_month`** — 1–31, only for `frequency="monthly"`.
- **`run_at_date`** — `"YYYY-MM-DD"` in local tz, only for `frequency="once"`. Fires once then auto-deletes. Must be in the future or the API rejects it.
- **`web_project_id`** — when set, each run **creates a new chat** in this PWA project. Standard target for vault-aware automations.
- **`web_chat_id`** — when set, posts into one existing chat instead of opening a new one. Pick this only if conversation continuity matters.
- **`thread_id`** — legacy, leave `null` for PWA dispatch.
- **`chat_id`** — legacy, set to `0`.
- **`model`** — empty string lets the workspace pick its default at dispatch time. Override only when a specific model matters.
- **`schedule_id`** — `f"sched-{uuid.uuid4().hex[:8]}"`. Match the existing convention.
- **`last_triggered_on`** — empty string for new entries. The dispatcher writes the local-date string after each run.
- **`enabled`** — `true` (default) runs on schedule; `false` pauses auto-fire (manual "Run now" still works).
- **`scope`** — `"user"` for schedules you create; system schedules use `"system"`.

### Prompt placeholders

Two placeholders are substituted at dispatch time (used by maintenance schedules):

- **`{{ERROR_LOG}}`** — replaced with the tail of `.runtime/server_errors.log`.
- **`{{ISSUE_REPORT}}`** — replaced with a formatted report of server errors plus failed background-job runs.

After a successful run of a prompt containing either placeholder, the error log is cleared.

## Prompt conventions

Existing schedules follow a few patterns; copy them when writing new prompts.

0. **Draft before creating new recurring schedules.** For new recurring automations, show a concise draft and get user confirmation before writing `.runtime/schedules.json`, unless the user explicitly asks you to apply the change directly.
1. **First line states the goal in 3–7 words.** Acts as a chat title hint.
2. **Lean on CLAUDE.md and skills, don't restate them.** The dispatched run is a fresh chat inside the target project, so it inherits `CLAUDE.md` and every auto-activating skill. Only put schedule-specific logic in the prompt — file paths, decision rules, output shape.
3. **Vault edits only on signal change.** For periodic checks, instruct the dispatched run to report status in chat and only edit the vault when something actually changed. Otherwise the vault fills with identical "no change" bullets.
4. **Commit policy when the run edits the repo.** If the prompt writes to `memory-vault/` and the workspace is a git repo, end the edit step with a git commit.
5. **OOO / no-op early exit.** For daily/weekly review schedules, add a check at the top that skips with a one-line output if there's nothing to do (e.g., no transcripts to process). Keeps cost low.

**Rule of thumb on length.** Aim for ≤1000 chars for simple checks, ≤4000 for review-style aggregations. A plain "read URL, compare, report" check should stay short. Re-skim and prune any prompt that drifts past those bounds.

## Find the project ID

```bash
python3 -c "
import json
data = json.load(open('.runtime/web_projects.json'))
for pid, p in data['projects'].items():
    if 'PROJECT_NAME_SUBSTRING' in p.get('name', '').lower():
        print(pid, '|', p['name'], '|', p['workspace'])
"
```

## Recipe: create a new schedule

Run from the workspace root:

```python
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

path = Path('.runtime/schedules.json')
data = json.loads(path.read_text())

entry = {
    'archive_policy': 'manual',
    'chat_id': 0,
    'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    'daily_time_utc': '09:00',          # local time in timezone_name
    'day_of_month': None,
    'days_of_week': ['mon'],            # weekly only; lowercase abbrevs
    'editable': True,
    'enabled': True,
    'frequency': 'weekly',              # daily | weekly | monthly | manual | once
    'last_triggered_on': '',
    'model': '',                        # empty = workspace default
    'prompt': '<full self-contained prompt with emoji sentinel>',
    'removable': True,
    'run_at_date': None,                # only for frequency='once'
    'schedule_id': f"sched-{uuid.uuid4().hex[:8]}",
    'scope': 'user',
    'thread_id': None,
    'timezone_name': 'UTC',             # or the user's IANA zone
    'title': '',
    'web_chat_id': None,                # mutually exclusive with web_project_id
    'web_project_id': 'proj-XXXXXXXX',  # find via web_projects.json
    'workspace': '',
}

data.setdefault('schedules', []).append(entry)
path.write_text(json.dumps(data, indent=2, sort_keys=True))
print('Added', entry['schedule_id'])
```

## Verify after writing

Confirm the entry loads cleanly and pick a next-run time:

```python
import json
from ciao.schedules import ScheduleEntry, compute_next_run

raw = json.load(open('.runtime/schedules.json'))
target = [s for s in raw['schedules'] if s['schedule_id'] == 'sched-XXXXXXXX'][0]
known = {f.name for f in ScheduleEntry.__dataclass_fields__.values()}
entry = ScheduleEntry(**{k: v for k, v in target.items() if k in known})
print('next run:', compute_next_run(entry))
```

A `None` from `compute_next_run` means the entry is malformed (bad time string, missing `run_at_date` for `once`, etc.). Fix before claiming success.

## Edit / disable / delete

- **Edit:** read the JSON, mutate the matching entry, write back. Don't change `schedule_id` or `created_at`. Clear `last_triggered_on` if you want the new schedule to fire today.
- **Temporarily disable / pause:** set `enabled: false`. The tick loop skips it; the UI shows a "paused" badge and manual "Run now" still works. Set back to `true` to resume. The old `frequency: "manual"` hack still works but `enabled: false` is preferred.
- **Delete:** filter the entry out of `data['schedules']` and write the file back. System schedules (`scope: "system"`, packaged with the app) cannot be deleted, only disabled.

## Cross-references in the vault

When a schedule exists to maintain a specific project or doc, mention it once in that doc by `schedule_id`. Example:
`Auto-rechecked weekly: Ciaobot schedule \`sched-dd1c0790\` (Mon 09:00, project \`<name>\`).`
That way the schedule's purpose is discoverable from the artifact it owns, not just from the JSON file.

## Loops (in-chat interval automations)

Loops are the sub-day sibling of schedules and live next to them on the PWA's Automations page. A loop is bound to **one existing chat** (`web_chat_id`) and re-dispatches its prompt every `interval_minutes` (floor: 1), so the conversation keeps its context between iterations — e.g. "check my PRs for review changes every 10 minutes".

- **Store:** `<workspace root>/.runtime/loops.json` (`ciao/loops.py`: `LoopEntry`, `LoopStore`, `LoopManager`).
- **HTTP API:** `GET/POST /api/loops`, `PATCH/DELETE /api/loops/{id}` (PATCH `{"running": true|false}` starts/stops), `POST /api/loop-run/{id}` (fire one iteration now).
- **Entry shape:** `loop_id` (`loop-<8hex>`), `prompt`, `web_chat_id`, `created_at`, `interval_minutes`, `title`, `autostart`, `last_run_at`, `last_status`.

Semantics that differ from schedules:

- **No model field.** Iterations always run with the target chat's current model/mode; the user changes the chat to change the loop.
- **`autostart`** — `true` means the loop begins running when the server boots; `false` means it stays stopped after a restart until started manually. Run/stopped state itself is runtime-only (held by the `LoopManager`), so **adding an entry directly to `loops.json` does not start it** — the user must start it from the Automations page (or it starts on the next boot if `autostart` is set). Prefer the UI/API flow over direct file writes for loops.
- **Skip, not queue.** If the chat still has a turn in flight when an iteration is due, the iteration is skipped and retried on the next ~20s tick (`last_status: "busy"`).
- **No catch-up** after downtime; cadence just resumes.
- **Prompt convention:** instruct the run to reply with a short fixed line (e.g. "no changes") when nothing happened, so iterations stay cheap and the chat stays scannable.

## When NOT to use this skill

- Cloud-side claude.ai Routines or the `/schedule` skill → they can't read the vault and bypass project dispatch.
- One-off ad-hoc reminders that the user will action manually → use the user's task system instead of a `once` schedule when one is configured.
- Sub-day recurrence inside one conversation → that's a loop, not a schedule (see the Loops section above).
