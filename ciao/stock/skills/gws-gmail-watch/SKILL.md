---
name: gws-gmail-watch
description: "Gmail: Watch for new emails and stream them as NDJSON."
metadata:
  version: 0.22.5
---

# gmail +watch

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

Watch for new emails and stream them as NDJSON

## Usage

```bash
scripts/gws-profile.sh <personal|work> gmail +watch
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--project` | — | — | GCP project ID for Pub/Sub resources |
| `--subscription` | — | — | Existing Pub/Sub subscription name (skip setup) |
| `--topic` | — | — | Existing Pub/Sub topic with Gmail push permission already granted |
| `--label-ids` | — | — | Comma-separated Gmail label IDs to filter (e.g., INBOX,UNREAD) |
| `--max-messages` | — | 10 | Max messages per pull batch |
| `--poll-interval` | — | 5 | Seconds between pulls |
| `--msg-format` | — | full | Gmail message format: full, metadata, minimal, raw |
| `--once` | — | — | Pull once and exit |
| `--cleanup` | — | — | Delete created Pub/Sub resources on exit |
| `--output-dir` | — | — | Write each message to a separate JSON file in this directory |

## Examples

```bash
scripts/gws-profile.sh <personal|work> gmail +watch --project my-gcp-project
scripts/gws-profile.sh <personal|work> gmail +watch --project my-project --label-ids INBOX --once
scripts/gws-profile.sh <personal|work> gmail +watch --subscription projects/p/subscriptions/my-sub
scripts/gws-profile.sh <personal|work> gmail +watch --project my-project --cleanup --output-dir ./emails
```

## Tips

- Gmail watch expires after 7 days — re-run to renew.
- Without --cleanup, Pub/Sub resources persist for reconnection.
- Press Ctrl-C to stop gracefully.
