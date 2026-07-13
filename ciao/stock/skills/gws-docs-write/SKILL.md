---
name: gws-docs-write
description: "Google Docs: Append text to a document."
metadata:
  version: 0.22.5
---

# docs +write

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

Append text to a document

## Usage

```bash
scripts/gws-profile.sh <personal|work> docs +write --document <ID> --text <TEXT>
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--document` | ✓ | — | Document ID |
| `--text` | ✓ | — | Text to append (plain text) |

## Examples

```bash
scripts/gws-profile.sh <personal|work> docs +write --document DOC_ID --text 'Hello, world!'
```

## Tips

- Text is inserted at the end of the document body.
- For rich formatting, use the raw batchUpdate API instead.

> [!CAUTION]
> This is a **write** command — confirm with the user before executing.
