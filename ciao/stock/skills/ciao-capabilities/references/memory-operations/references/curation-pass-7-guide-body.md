# Step guide-review: review the workspace guide body

## Inputs
- The workspace guide `AGENTS.md` (the file holding the `ciao:memory` region), body only, outside the fenced regions.
- `<vault>/Workspace/Learnings.md` for x3-or-more entries; `unsure-and-proposals.md` in this folder for the proposal file format.

## Procedure
Review the guide file's body (AGENTS.md), outside the fenced `ciao:memory` and `ciao:profile` regions, for the things `memory-audit` cannot judge on shape alone. The body is the workspace's standing contract with every session, so a wrong claim there is asserted before the operator has said a word.

This step is review-only. You do not edit the guide body, because a change to what every session loads needs a person's approval. Write each finding that proposes a change as a `guide-edit` proposal file (see `unsure-and-proposals.md`), checked with `ciao proposal-check`; a finding you are only unsure about is a line on `Workspace/Unsure.md`. Report both as `needs you` items.

Look for:

- Misplacement. Apply three tests to any entry that reads like a fact or an instruction. Is it a thing or an instruction? An entity (a person, a system, an automation, a tool) belongs on a vault page, not in the guide body. Does a vault page already say it? Search before keeping it. Where would it fire? A gotcha that only matters inside one workflow belongs in that workflow's skill. Write a `guide-edit` proposal proposing the move, and keep only standing instructions that must load every turn.
- State versus event. A current value (price, status, owner, deadline, role, config setting, file location) is state: replace it in place with a fresh date rather than appending a second line. A thing that happened (a decision, a delivery, a correction) is an event and belongs in the journal, `log.md`, or `Workspace/Learnings.md`. Write a proposal or an Unsure line for each one you find.
- Duplicates of the core prompt or a tool's own docs. A guide section that only restates what the Ciaobot core prompt or an MCP/tool's own instructions already carry should be proposed for removal, since the guide is for the workspace-specific delta. Check that the other source actually carries it before proposing.
- Standing instructions that were never promoted. A learning that recurs (x3+) is a routing priority. Write a `guide-edit` proposal naming `AGENTS.md` as the target, and report it as a `needs you` item. If the body already states it, file nothing.
- Drift and bloat. Stale paths, superseded rules, and anything a more specific file (a skill, a project doc, the daily journal) covers more precisely are proposals or Unsure lines too, as is anything whose correct home is genuinely ambiguous.

## Done when
You read the whole body and applied every lens above. A run that never reached the guide is `blocked`. Finish with `done: reviewed AGENTS.md, N proposals (<paths>), M Unsure lines`, and give each proposal as a `needs you` item.

Verify: run `ciao proposal-check <file>` on every proposal path the done line names; each must print `ok`.
