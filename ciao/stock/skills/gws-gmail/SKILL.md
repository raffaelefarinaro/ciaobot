---
name: gws-gmail
description: "Gmail: Send, read, and manage email."
metadata:
  version: 0.22.5
---

# gmail (v1)

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

```bash
scripts/gws-profile.sh <personal|work> gmail <resource> <method> [flags]
```

## Helper Commands

| Command | Description |
|---------|-------------|
| [`+send`](../gws-gmail-send/SKILL.md) | Send an email |
| [`+triage`](../gws-gmail-triage/SKILL.md) | Show unread inbox summary (sender, subject, date) |
| [`+reply`](../gws-gmail-reply/SKILL.md) | Reply to a message (handles threading automatically) |
| [`+reply-all`](../gws-gmail-reply-all/SKILL.md) | Reply-all to a message (handles threading automatically) |
| [`+forward`](../gws-gmail-forward/SKILL.md) | Forward a message to new recipients |
| [`+read`](../gws-gmail-read/SKILL.md) | Read a message and extract its body or headers |
| [`+watch`](../gws-gmail-watch/SKILL.md) | Watch for new emails and stream them as NDJSON |

## API Resources

### users

  - `getProfile` — Gets the current user's Gmail profile.
  - `stop` — Turn off push notification delivery for the given user mailbox. For more information, see [Configure push notifications in Gmail API](https://developers.google.com/workspace/gmail/api/guides/push).
  - `watch` — Set up or update a push notification watch on the given user mailbox. For more information, see [Configure push notifications in Gmail API](https://developers.google.com/workspace/gmail/api/guides/push).
  - `drafts` — Operations on the 'drafts' resource
  - `history` — Operations on the 'history' resource
  - `labels` — Operations on the 'labels' resource
  - `messages` — Operations on the 'messages' resource
  - `settings` — Operations on the 'settings' resource
  - `threads` — Operations on the 'threads' resource

## Discovering Commands

Before calling any API method, inspect it:

```bash
# Browse resources and methods
scripts/gws-profile.sh <personal|work> gmail --help

# Inspect a method's required params, types, and defaults
scripts/gws-profile.sh <personal|work> schema gmail.<resource>.<method>
```

Use `gws schema` output to build your `--params` and `--json` flags.
