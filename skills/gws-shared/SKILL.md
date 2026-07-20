---
name: gws-shared
description: "gws CLI: Shared patterns for authentication, global flags, and output formatting."
metadata:
  version: 0.22.5
---

# gws — Shared Reference

## Installation

Install `gws` from Settings → Integrations (or see the Ciaobot README). The binary must be on `$PATH`.

## Authentication (Ciaobot)

Run every Google API call through the profile wrapper — never bare `gws`:

```bash
scripts/gws-profile.sh <personal|work> <service> <subcommand> [flags]
```

Use the chat's `GWS_PROFILE` unless the user asks otherwise. The wrapper routes credentials and execs `gws`. Do not `source` it and do not repeat the `gws` binary after the profile name.

OAuth setup: Settings → Integrations. Config dirs: `secrets/gws-personal/` (personal), `secrets/gws/` (work).

## Global Flags

| Flag | Description |
|------|-------------|
| `--format <FORMAT>` | Output format: `json` (default), `table`, `yaml`, `csv` |
| `--dry-run` | Validate locally without calling the API |
| `--sanitize <TEMPLATE>` | Screen responses through Model Armor |

## CLI Syntax

```bash
scripts/gws-profile.sh <personal|work> <service> <resource> [sub-resource] <method> [flags]
```

### Method Flags

| Flag | Description |
|------|-------------|
| `--params '{"key": "val"}'` | URL/query parameters |
| `--json '{"key": "val"}'` | Request body |
| `-o, --output <PATH>` | Save binary responses to file |
| `--upload <PATH>` | Upload file content (multipart) |
| `--page-all` | Auto-paginate (NDJSON output) |
| `--page-limit <N>` | Max pages when using --page-all (default: 10) |
| `--page-delay <MS>` | Delay between pages in ms (default: 100) |

## Security Rules

- **Never** output secrets (API keys, tokens) directly
- **Always** confirm with user before executing write/delete commands
- Prefer `--dry-run` for destructive operations
- Use `--sanitize` for PII/content safety screening

## Shell Tips

- **zsh `!` expansion:** Sheet ranges like `Sheet1!A1` contain `!` which zsh interprets as history expansion. Use double quotes with escaped inner quotes instead of single quotes:
  ```bash
  # WRONG (zsh will mangle the !)
  scripts/gws-profile.sh <personal|work> sheets +read --spreadsheet ID --range 'Sheet1!A1:D10'

  # CORRECT
  scripts/gws-profile.sh <personal|work> sheets +read --spreadsheet ID --range "Sheet1!A1:D10"
  ```
- **JSON with double quotes:** Wrap `--params` and `--json` values in single quotes so the shell does not interpret the inner double quotes:
  ```bash
  scripts/gws-profile.sh <personal|work> drive files list --params '{"pageSize": 5}'
  ```
