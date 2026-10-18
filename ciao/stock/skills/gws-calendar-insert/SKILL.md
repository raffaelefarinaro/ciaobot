---
name: gws-calendar-insert
description: "Use to create a new calendar event, meeting or appointment, optionally with attendees and a Meet link."
metadata:
  version: 0.22.5
---

# calendar +insert

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (via `ciao gws`), global flags, and security rules.

create a new event

## Usage

```bash
ciao gws "$GWS_PROFILE" calendar +insert --summary <TEXT> --start <TIME> --end <TIME>
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--calendar` | — | primary | Calendar ID (default: primary) |
| `--summary` | ✓ | — | Event summary/title |
| `--start` | ✓ | — | Start time (RFC3339 with offset, e.g., 2026-06-17T09:00:00-07:00) |
| `--end` | ✓ | — | End time (RFC3339 with offset) |
| `--location` | — | — | Event location |
| `--description` | — | — | Event description/body |
| `--attendee` | — | — | Attendee email (can be used multiple times) |
| `--meet` | — | — | Add a Google Meet video conference link |

## Examples

```bash
ciao gws "$GWS_PROFILE" calendar +insert --summary 'Standup' --start '2026-06-17T09:00:00-07:00' --end '2026-06-17T09:30:00-07:00'
ciao gws "$GWS_PROFILE" calendar +insert --summary 'Review' --start '2026-06-17T14:00:00-07:00' --end '2026-06-17T15:00:00-07:00' --attendee alice@example.com
ciao gws "$GWS_PROFILE" calendar +insert --summary 'Meet' --start '2026-06-17T16:00:00-07:00' --end '2026-06-17T16:30:00-07:00' --meet
```

## Tips

- Use RFC3339 format for times (e.g. 2026-06-17T09:00:00-07:00).
- The --meet flag automatically adds a Google Meet link to the event.

> [!CAUTION]
> This is a **write** command — confirm with the user before executing.
