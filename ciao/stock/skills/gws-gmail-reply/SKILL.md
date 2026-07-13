---
name: gws-gmail-reply
description: "Gmail: Reply to a message (handles threading automatically)."
metadata:
  version: 0.22.5
---

# gmail +reply

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

Reply to a message (handles threading automatically)

## Usage

```bash
scripts/gws-profile.sh <personal|work> gmail +reply --message-id <ID> --body <TEXT>
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--message-id` | ✓ | — | Gmail message ID to reply to |
| `--body` | ✓ | — | Reply body (plain text, or HTML with --html) |
| `--from` | — | — | Sender address (for send-as/alias; omit to use account default) |
| `--to` | — | — | Additional To email address(es), comma-separated |
| `--attach` | — | — | Attach a file (can be specified multiple times) |
| `--cc` | — | — | CC email address(es), comma-separated |
| `--bcc` | — | — | BCC email address(es), comma-separated |
| `--html` | — | — | Treat --body as HTML content (default is plain text) |
| `--dry-run` | — | — | Show the request that would be sent without executing it |
| `--draft` | — | — | Save as draft instead of sending |

## Examples

```bash
scripts/gws-profile.sh <personal|work> gmail +reply --message-id 18f1a2b3c4d --body 'Thanks, got it!'
scripts/gws-profile.sh <personal|work> gmail +reply --message-id 18f1a2b3c4d --body 'Looping in Carol' --cc carol@example.com
scripts/gws-profile.sh <personal|work> gmail +reply --message-id 18f1a2b3c4d --body 'Adding Dave' --to dave@example.com
scripts/gws-profile.sh <personal|work> gmail +reply --message-id 18f1a2b3c4d --body '<b>Bold reply</b>' --html
scripts/gws-profile.sh <personal|work> gmail +reply --message-id 18f1a2b3c4d --body 'Updated version' -a updated.docx
scripts/gws-profile.sh <personal|work> gmail +reply --message-id 18f1a2b3c4d --body 'Draft reply' --draft
```

## Tips

- Automatically sets In-Reply-To, References, and threadId headers.
- Quotes the original message in the reply body.
- --to adds extra recipients to the To field.
- Use -a/--attach to add file attachments. Can be specified multiple times.
- With --html, the quoted block uses Gmail's gmail_quote CSS classes and preserves HTML formatting. Use fragment tags (<p>, <b>, <a>, etc.) — no <html>/<body> wrapper needed.
- With --html, inline images in the quoted message are preserved via cid: references.
- Use --draft to save the reply as a draft instead of sending it immediately.
- For reply-all, use +reply-all instead.
