---
name: gws-slides
description: "Use to read or edit a Google Slides presentation."
metadata:
  version: 0.22.5
---

# slides (v1)

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (via `ciao gws`), global flags, and security rules.

```bash
ciao gws "$GWS_PROFILE" slides <resource> <method> [flags]
```

## API Resources

### presentations

  - `batchUpdate` — Applies one or more updates to the presentation. Other requests do not need to return information; these each return an empty reply.
  - `create` — Creates a blank presentation using the title given in the request.
  - `get` — Gets the latest version of the specified presentation.
  - `pages` — Operations on the 'pages' resource

## Discovering Commands

Browse a service with `ciao gws "$GWS_PROFILE" <service> --help`. Before calling an API method, run `ciao gws "$GWS_PROFILE" schema <service>.<resource>.<method>` and build `--params` and `--json` from its output.
