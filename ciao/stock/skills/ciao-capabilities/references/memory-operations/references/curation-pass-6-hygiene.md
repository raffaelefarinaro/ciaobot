# Step hygiene: weekly workspace hygiene (mechanical)

The orchestrator runs this itself with plain `ciao` commands, no subagent and no model judgment. Dispatched weekly.

## Inputs
None beyond the workspace: both commands read the configured vault.

## Procedure
1. Run `ciao vault-index --write`. If it fails, the step is blocked with its error; do not claim health.
2. Run `ciao os-audit --json --scope workspace`. Exit 1 means reliable findings and is fine. Exit 2 means unreliable evidence: block the step with the scan errors and make no audit-derived claim.
3. Record in Care-Report.md under `## Hygiene`: the index result, the finding count, and each finding with its category and the affected note's vault-relative path (not just a total). Make no repair: a dead link, a non-canonical `type:` or any other finding is a `needs you` item. A `missing_frontmatter` finding on a file a script or schedule writes is not a note: name it and suggest a pattern for the vault's `.ciaoignore`.

Install-wide runtime failures are visible through startup triage and pending upgrade work appears in housekeeping, so do not duplicate those global checks here.

## Done when
Both commands ran and the section is written. Evidence: `index rebuilt; audit exit N, F findings (<categories>)`.

Verify: re-read Care-Report.md and confirm the `## Hygiene` section holds the finding count and one line per finding.
