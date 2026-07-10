---
name: gws-gmail-read
description: "Gmail: Read a message and extract its body or headers."
metadata:
  version: 0.22.5
---

# gmail +read

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

Read a message and extract its body or headers

## Usage

```bash
scripts/gws-profile.sh <personal|work> gmail +read --id <ID>
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--id` | ✓ | — | The Gmail message ID to read |
| `--headers` | — | — | Include headers (From, To, Subject, Date) in the output |
| `--format` | — | text | Output format (text, json) |
| `--html` | — | — | Return HTML body instead of plain text |
| `--dry-run` | — | — | Show the request that would be sent without executing it |

## Examples

```bash
scripts/gws-profile.sh <personal|work> gmail +read --id 18f1a2b3c4d
scripts/gws-profile.sh <personal|work> gmail +read --id 18f1a2b3c4d --headers
scripts/gws-profile.sh <personal|work> gmail +read --id 18f1a2b3c4d --format json | jq '.body'
```

## Tips

- Converts HTML-only messages to plain text automatically.
- Handles multipart/alternative and base64 decoding.
