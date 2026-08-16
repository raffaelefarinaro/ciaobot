---
name: ciao-support
description: Diagnose Ciaobot failures and apply the repository's GitHub issue-label convention.
---

# Ciaobot support

Use this skill when the user reports that Ciaobot itself is failing or asks to
prepare a public issue. Keep excerpts focused and redact secrets, private
paths, provider keys, and transcript content.

## Local evidence

Inspect, when present:

- `.runtime/server_errors.log`
- `.runtime/job_runs.jsonl`
- `.runtime/ciao.stderr.log` and `.runtime/ciao.stdout.log` for macOS service issues

State clearly when evidence is missing or empty. Do not claim the problem is
fixed from a log tail alone.

## GitHub issue labels

When the operator has approved creating an issue in `raffaelefarinaro/ciaobot`,
use one classification label matching the title prefix:

| Prefix | Label |
|---|---|
| `[Bug]` | `bug` |
| `[Feature]` or `[Goal]` | `enhancement` |
| `[Docs]` | `documentation` |
| `[Chore]` | `chore` |

`[Report]` is retired. `[Agent]` follows the content classification. Do not
ask for GitHub credentials; use the browser or `gh` only after approval.
