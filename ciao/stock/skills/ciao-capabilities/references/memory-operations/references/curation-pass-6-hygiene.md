# Step hygiene: weekly workspace hygiene (mechanical)

The orchestrator runs this itself with plain `ciao` commands, no subagent and no model judgment. Dispatched weekly.

## Inputs
None beyond the workspace: both commands read the configured vault.

## Procedure
1. Run `ciao vault-index --write`. If it fails, the step is blocked with its error; do not claim health.
2. Run `ciao os-audit --json --scope workspace`. Exit 1 means reliable findings and is fine. Exit 2 means unreliable evidence: block the step with the scan errors and make no audit-derived claim.
3. Record in Care-Report.md under `## Hygiene`: the index result, the finding count, and each finding with its category and the affected note's vault-relative path (not just a total), one plain `- ` bullet per finding and never a `- [ ]` checkbox, because checkbox lines are the run's step list. Make no repair. With any findings, the step leaves one item for all of them, never one per finding or per category: `needs you: The vault index lists F issues, such as <at most 3 kinds in everyday words, e.g. notes nothing links to>. Open a chat to fix them with you. (path: Workspace/Care-Report.md)`. A `missing_frontmatter` finding on a file a script or schedule writes is not a note: name it and suggest a pattern for the vault's `.ciaoignore`.

Install-wide runtime failures are visible through startup triage and pending upgrade work appears in housekeeping, so do not duplicate those global checks here.

## Done when
Both commands ran and the section is written. Evidence: `index rebuilt; audit exit N, F findings (<categories>)`, plus that one `needs you` item when F is above 0.

Verify: check that INDEX.md at the vault root (`<V>/../INDEX.md` when V is a workspace folder under `memory-vault/`, else `<V>/INDEX.md`) was rewritten today (`find <path> -newermt <today>` lists it), and that the `## Hygiene` section in Care-Report.md records `audit exit N` with the finding count and one line per finding.
