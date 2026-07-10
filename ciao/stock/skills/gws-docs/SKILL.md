---
name: gws-docs
description: "Read and write Google Docs."
metadata:
  version: 0.22.5
---

# docs (v1)

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

```bash
scripts/gws-profile.sh <personal|work> docs <resource> <method> [flags]
```

## Helper Commands

| Command | Description |
|---------|-------------|
| [`+write`](../gws-docs-write/SKILL.md) | Append text to a document |

## API Resources

### documents

  - `batchUpdate` — Applies one or more updates to the document. Each request is validated before being applied. If any request is not valid, then the entire request will fail and nothing will be applied. Some requests have replies to give you some information about how they are applied. Other requests do not need to return information; these each return an empty reply. The order of replies matches that of the requests.
  - `create` — Creates a blank document using the title given in the request. Other fields in the request, including any provided content, are ignored. Returns the created document.
  - `get` — Gets the latest version of the specified document.

## Discovering Commands

Before calling any API method, inspect it:

```bash
# Browse resources and methods
scripts/gws-profile.sh <personal|work> docs --help

# Inspect a method's required params, types, and defaults
scripts/gws-profile.sh <personal|work> schema docs.<resource>.<method>
```

Use `gws schema` output to build your `--params` and `--json` flags.
