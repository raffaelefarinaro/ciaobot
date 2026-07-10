---
name: gws-calendar-agenda
description: "Google Calendar: Show upcoming events across all calendars."
metadata:
  version: 0.22.5
---

# calendar +agenda

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

Show upcoming events across all calendars

## Usage

```bash
scripts/gws-profile.sh <personal|work> calendar +agenda
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--today` | — | — | Show today's events |
| `--tomorrow` | — | — | Show tomorrow's events |
| `--week` | — | — | Show this week's events |
| `--days` | — | — | Number of days ahead to show |
| `--calendar` | — | — | Filter to specific calendar name or ID |
| `--timezone` | — | — | IANA timezone override (e.g. America/Denver). Defaults to Google account timezone. |

## Examples

```bash
scripts/gws-profile.sh <personal|work> calendar +agenda
scripts/gws-profile.sh <personal|work> calendar +agenda --today
scripts/gws-profile.sh <personal|work> calendar +agenda --week --format table
scripts/gws-profile.sh <personal|work> calendar +agenda --days 3 --calendar 'Work'
scripts/gws-profile.sh <personal|work> calendar +agenda --today --timezone America/New_York
```

## Tips

- Read-only — never modifies events.
- Queries all calendars by default; use --calendar to filter.
- Uses your Google account timezone by default; override with --timezone.
