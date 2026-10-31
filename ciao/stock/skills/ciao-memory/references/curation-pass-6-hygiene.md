# Pass 6: weekly workspace hygiene

Run this on a full pass (`weekly_due`).

1. Run `ciao vault-index --write`. If it fails, report the failure and do not claim health. On success record it: `ciao curation-progress --holder <lease.holder> --key hygiene:vault-index`.
2. Run `ciao os-audit --json --scope workspace`. Exit 1 means reliable findings and is safe to continue. Exit 2 means unreliable evidence: report the scan errors, make no audit-derived repair, and leave the full pass due.
3. When the audit is reliable, apply only low-risk unambiguous repairs: dead links, obvious MEMORY.md path drift, and a non-canonical `type:` whose exact canonical target VOCABULARY.md already names. Leave instruction conflicts, skills, orphaned or duplicate notes, expiration tags, schedules and vocabulary proposals alone, since each needs a person's judgment. A `missing_frontmatter` finding on a file a script or schedule writes is not a note: do not add frontmatter to it; name it to the user and suggest a pattern for the vault's `.ciaoignore`.
4. Re-run the scoped audit. Record initial and final counts, safe repairs, unresolved findings and vocabulary proposals in the technical log. Log each unresolved finding with its category and the affected note's vault-relative path, not just a total. When that verification is reliable, record it: `ciao curation-progress --holder <lease.holder> --key hygiene:os-audit`.

Those two keys are what lets `curation-end` stamp `last_full_pass`. If neither is recorded, the weekly pass stays due, which is the right outcome for a run whose evidence was unreliable. Writing the marker by hand to make the page look green would hide that.

Install-wide runtime failures are visible through startup triage and pending upgrade work appears in housekeeping, so do not duplicate those global checks in every workspace.
