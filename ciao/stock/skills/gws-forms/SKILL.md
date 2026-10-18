---
name: gws-forms
description: "Use to read a Google Form, its questions and responses, or to edit it."
metadata:
  version: 0.22.5
---

# forms (v1)

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (via `ciao gws`), global flags, and security rules.

```bash
ciao gws "$GWS_PROFILE" forms <resource> <method> [flags]
```

## API Resources

### forms

  - `batchUpdate` — Change the form with a batch of updates.
  - `create` — Create a new form using the title given in the provided form message in the request. *Important:* Only the form.info.title and form.info.document_title fields are copied to the new form. To create a new form and add items, you must first call forms.create to create an empty form with a title and (optional) document title, and then call forms.update to add the items.
  - `get` — Get a form.
  - `setPublishSettings` — Updates the publish settings of a form.
  - `responses` — Operations on the 'responses' resource
  - `watches` — Operations on the 'watches' resource

## Discovering Commands

Browse a service with `ciao gws "$GWS_PROFILE" <service> --help`. Before calling an API method, run `ciao gws "$GWS_PROFILE" schema <service>.<resource>.<method>` and build `--params` and `--json` from its output.
