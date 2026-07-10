---
name: gws-gmail-triage
description: "Gmail: Show unread inbox summary (sender, subject, date)."
metadata:
  version: 0.22.5
---

# gmail +triage

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

Show unread inbox summary (sender, subject, date)

## Usage

```bash
scripts/gws-profile.sh <personal|work> gmail +triage
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--max` | — | 20 | Maximum messages to show (default: 20) |
| `--query` | — | — | Gmail search query (default: is:unread) |
| `--labels` | — | — | Include label names in output |

## Examples

```bash
scripts/gws-profile.sh <personal|work> gmail +triage
scripts/gws-profile.sh <personal|work> gmail +triage --max 5 --query 'from:boss'
scripts/gws-profile.sh <personal|work> gmail +triage --format json | jq '.[].subject'
scripts/gws-profile.sh <personal|work> gmail +triage --labels
```

## Tips

- Read-only — never modifies your mailbox.
- Defaults to table output format.
