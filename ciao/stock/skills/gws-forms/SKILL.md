---
name: gws-forms
description: "Read and write Google Forms."
metadata:
  version: 0.22.5
---

# forms (v1)

> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), global flags, and security rules.

```bash
scripts/gws-profile.sh <personal|work> forms <resource> <method> [flags]
```

## API Resources

### forms

  - `batchUpdate` — Change the form with a batch of updates.
  - `create` — Create a new form using the title given in the provided form message in the request. *Important:* Only the form.info.title and form.info.document_title fields are copied to the new form. All other fields including the form description, items and settings are disallowed. To create a new form and add items, you must first call forms.create to create an empty form with a title and (optional) document title, and then call forms.update to add the items.
  - `get` — Get a form.
  - `setPublishSettings` — Updates the publish settings of a form. Legacy forms aren't supported because they don't have the `publish_settings` field.
  - `responses` — Operations on the 'responses' resource
  - `watches` — Operations on the 'watches' resource

## Discovering Commands

Before calling any API method, inspect it:

```bash
# Browse resources and methods
scripts/gws-profile.sh <personal|work> forms --help

# Inspect a method's required params, types, and defaults
scripts/gws-profile.sh <personal|work> schema forms.<resource>.<method>
```

Use `gws schema` output to build your `--params` and `--json` flags.
