---
name: gws-shared
description: "gws CLI: Shared patterns for authentication, global flags, and output formatting in Ciaobot."
metadata:
  version: 0.22.5
  openclaw:
    category: "productivity"
    requires:
      bins:
        - gws
---

# gws — Shared Reference

## Ciaobot integration

Ciaobot ships stock `gws-*` skills and routes Google API calls through profile-aware wrappers.

### Install

Install `@googleworkspace/cli` globally, or use **Settings → Integrations → Install gws** in the PWA.

### Profiles

Use `scripts/gws-profile.sh <personal|work> <gws-subcommand...>` — **never** `source` the script (it ends with `exec`).

| Profile | Config dir | Typical services |
|---------|------------|------------------|
| `personal` | `<workspace>/secrets/gws-personal/` | Gmail, Calendar, Tasks |
| `work` | `<workspace>/secrets/gws/` | Drive, Docs, Sheets, Slides, Gmail, Calendar, Tasks |

The active workspace's `gws_profile` field selects which profile a chat uses (injected as `GWS_PROFILE`).

### OAuth setup (PWA)

1. Create an OAuth 2.0 client in [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials) (Desktop app, or Web app with redirect URI `http://localhost`).
2. Enable the APIs you need (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks).
3. Download the JSON credentials file.
4. In **Settings → Integrations**, upload `client_secret.json` for each profile, then **Connect Google Account**.

### OAuth setup (terminal)

```bash
scripts/gws-profile.sh personal auth login --full
scripts/gws-profile.sh work auth login --full
```

Headless servers: `python3 scripts/gws-auth-helper.py <personal|work>`.

### Output parsing

`gws` stdout may start with a non-JSON banner line (`Using keyring backend: file`). Strip it before parsing JSON.

### Common mistakes

- Do **not** chain `scripts/gws-profile.sh personal gws calendar ...` — the wrapper already execs `gws`. Pass the subcommand directly: `scripts/gws-profile.sh personal calendar ...`
- Do **not** use `gcloud auth print-access-token` for Drive/Docs — insufficient scopes.
- Use `--full` for complete scopes; partial `--services` logins can miss calendar scope.
- Request bodies (e.g. Tasks title/notes) go in `--json`, not `--params`.
- For shared-drive files, add `supportsAllDrives: true` in `--params`.

## Global Flags

| Flag | Description |
|------|-------------|
| `--format <FORMAT>` | Output format: `json` (default), `table`, `yaml`, `csv` |
| `--dry-run` | Validate locally without calling the API |
| `--sanitize <TEMPLATE>` | Screen responses through Model Armor |

## CLI Syntax

```bash
gws <service> <resource> [sub-resource] <method> [flags]
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
  gws sheets +read --spreadsheet ID --range 'Sheet1!A1:D10'

  # CORRECT
  gws sheets +read --spreadsheet ID --range "Sheet1!A1:D10"
  ```
- **JSON with double quotes:** Wrap `--params` and `--json` values in single quotes so the shell does not interpret the inner double quotes:
  ```bash
  gws drive files list --params '{"pageSize": 5}'
  ```

## Upstream docs

- CLI repo: https://github.com/googleworkspace/cli
- Issues: https://github.com/googleworkspace/cli/issues
