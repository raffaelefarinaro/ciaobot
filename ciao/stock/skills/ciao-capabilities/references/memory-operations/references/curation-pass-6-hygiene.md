# Pass 6: weekly workspace hygiene

Run this weekly (see the prompt for when it is due).

1. Run `ciao vault-index --write`. If it fails, report the failure and do not claim health. On success, note it in the report.
2. Run `ciao os-audit --json --scope workspace`. Exit 1 means reliable findings and is safe to continue. Exit 2 means unreliable evidence: report the scan errors, make no audit-derived repair, and leave the full pass due.
3. When the audit is reliable, apply only low-risk unambiguous repairs: dead links, obvious MEMORY.md path drift, and a non-canonical `type:` whose exact canonical target VOCABULARY.md already names. Leave instruction conflicts, skills, orphaned or duplicate notes, expiration tags, schedules and vocabulary proposals alone, since each needs a person's judgment. A `missing_frontmatter` finding on a file a script or schedule writes is not a note: do not add frontmatter to it; name it to the user and suggest a pattern for the vault's `.ciaoignore`.
4. Re-run the scoped audit. Record initial and final counts, safe repairs, unresolved findings and vocabulary proposals in the technical part of the report you write to `Workspace/Care-Report.md`. Log each unresolved finding with its category and the affected note's vault-relative path, not just a total.

If the index rebuild failed or the audit exited 2, say the weekly hygiene is still due in the report: the next run sees no completed hygiene section and repeats it.

Install-wide runtime failures are visible through startup triage and pending upgrade work appears in housekeeping, so do not duplicate those global checks in every workspace.
