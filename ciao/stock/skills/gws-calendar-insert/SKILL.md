---
name: gws-calendar-insert
description: "Google Calendar: Create a new event."
metadata:
  version: 0.22.5
---

# calendar +insert

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

create a new event

## Usage

```bash
scripts/gws-profile.sh <personal|work> calendar +insert --summary <TEXT> --start <TIME> --end <TIME>
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--calendar` | — | primary | Calendar ID (default: primary) |
| `--summary` | ✓ | — | Event summary/title |
| `--start` | ✓ | — | Start time (ISO 8601, e.g., 2024-01-01T10:00:00Z) |
| `--end` | ✓ | — | End time (ISO 8601) |
| `--location` | — | — | Event location |
| `--description` | — | — | Event description/body |
| `--attendee` | — | — | Attendee email (can be used multiple times) |
| `--meet` | — | — | Add a Google Meet video conference link |

## Examples

```bash
scripts/gws-profile.sh <personal|work> calendar +insert --summary 'Standup' --start '2026-06-17T09:00:00-07:00' --end '2026-06-17T09:30:00-07:00'
scripts/gws-profile.sh <personal|work> calendar +insert --summary 'Review' --start ... --end ... --attendee alice@example.com
scripts/gws-profile.sh <personal|work> calendar +insert --summary 'Meet' --start ... --end ... --meet
```

## Tips

- Use RFC3339 format for times (e.g. 2026-06-17T09:00:00-07:00).
- The --meet flag automatically adds a Google Meet link to the event.

> [!CAUTION]
> This is a **write** command — confirm with the user before executing.
