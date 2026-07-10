---
name: gws-sheets-read
description: "Google Sheets: Read values from a spreadsheet."
metadata:
  version: 0.22.5
---

# sheets +read

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

Read values from a spreadsheet

## Usage

```bash
scripts/gws-profile.sh <personal|work> sheets +read --spreadsheet <ID> --range <RANGE>
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--spreadsheet` | ✓ | — | Spreadsheet ID |
| `--range` | ✓ | — | Range to read (e.g. 'Sheet1!A1:B2') |

## Examples

```bash
scripts/gws-profile.sh <personal|work> sheets +read --spreadsheet ID --range "Sheet1!A1:D10"
scripts/gws-profile.sh <personal|work> sheets +read --spreadsheet ID --range Sheet1
```

## Tips

- Read-only — never modifies the spreadsheet.
- For advanced options, use the raw values.get API.
