---
name: gws-sheets
description: "Use for spreadsheet-level work (create, format, add sheets; raw Sheets API); to read values use gws-sheets-read, to add a row use gws-sheets-append."
metadata:
  version: 0.22.5
---

# sheets (v4)

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (via `ciao gws`), global flags, and security rules.

```bash
ciao gws "$GWS_PROFILE" sheets <resource> <method> [flags]
```

## Helper Commands

| Command | Description |
|---------|-------------|
| [`+append`](../gws-sheets-append/SKILL.md) | Append a row to a spreadsheet |
| [`+read`](../gws-sheets-read/SKILL.md) | Read values from a spreadsheet |

## API Resources

### spreadsheets

  - `batchUpdate` — Applies one or more updates to the spreadsheet. For example, if you applied 4 updates and the 3rd one had a reply, then the response will have 2 empty replies, the actual reply, and another empty reply, in that order.
  - `create` — Creates a spreadsheet, returning the newly created spreadsheet.
  - `get` — Returns the spreadsheet at the given ID.
  - `getByDataFilter` — Returns the spreadsheet at the given ID.
  - `developerMetadata` — Operations on the 'developerMetadata' resource
  - `sheets` — Operations on the 'sheets' resource
  - `values` — Operations on the 'values' resource

## Discovering Commands

Browse a service with `ciao gws "$GWS_PROFILE" <service> --help`. Before calling an API method, run `ciao gws "$GWS_PROFILE" schema <service>.<resource>.<method>` and build `--params` and `--json` from its output.
