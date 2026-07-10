---
name: gws-sheets-append
description: "Google Sheets: Append a row to a spreadsheet."
metadata:
  version: 0.22.5
---

# sheets +append

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

Append a row to a spreadsheet

## Usage

```bash
scripts/gws-profile.sh <personal|work> sheets +append --spreadsheet <ID>
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--spreadsheet` | ✓ | — | Spreadsheet ID |
| `--values` | — | — | Comma-separated values (simple strings) |
| `--json-values` | — | — | JSON array of rows, e.g. '[["a","b"],["c","d"]]' |

## Examples

```bash
scripts/gws-profile.sh <personal|work> sheets +append --spreadsheet ID --values 'Alice,100,true'
scripts/gws-profile.sh <personal|work> sheets +append --spreadsheet ID --json-values '[["a","b"],["c","d"]]'
```

## Tips

- Use --values for simple single-row appends.
- Use --json-values for bulk multi-row inserts.

> [!CAUTION]
> This is a **write** command — confirm with the user before executing.
