---
name: gws-drive-upload
description: "Google Drive: Upload a file with automatic metadata."
metadata:
  version: 0.22.5
---

# drive +upload

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

Upload a file with automatic metadata

## Usage

```bash
ciao gws "$GWS_PROFILE" drive +upload <file>
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `<file>` | ✓ | — | Path to file to upload |
| `--parent` | — | — | Parent folder ID |
| `--name` | — | — | Target filename (defaults to source filename) |

## Examples

```bash
ciao gws "$GWS_PROFILE" drive +upload ./report.pdf
ciao gws "$GWS_PROFILE" drive +upload ./report.pdf --parent FOLDER_ID
ciao gws "$GWS_PROFILE" drive +upload ./data.csv --name 'Sales Data.csv'
```

## Tips

- MIME type is detected automatically.
- Filename is inferred from the local path unless --name is given.

> [!CAUTION]
> This is a **write** command — confirm with the user before executing.
