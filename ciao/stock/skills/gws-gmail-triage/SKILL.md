---
name: gws-gmail-triage
description: "Gmail: Show unread inbox summary (sender, subject, date)."
metadata:
  version: 0.22.5
---

# gmail +triage

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (via `ciao gws`), global flags, and security rules.

Show unread inbox summary (sender, subject, date)

## Usage

```bash
ciao gws "$GWS_PROFILE" gmail +triage
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--max` | — | 20 | Maximum messages to show (default: 20) |
| `--query` | — | — | Gmail search query (default: is:unread) |
| `--labels` | — | — | Include label names in output |

## Examples

```bash
ciao gws "$GWS_PROFILE" gmail +triage
ciao gws "$GWS_PROFILE" gmail +triage --max 5 --query 'from:boss'
ciao gws "$GWS_PROFILE" gmail +triage --format json | jq '.[].subject'
ciao gws "$GWS_PROFILE" gmail +triage --labels
```

## Tips

- Read-only — never modifies your mailbox.
- Defaults to table output format.
