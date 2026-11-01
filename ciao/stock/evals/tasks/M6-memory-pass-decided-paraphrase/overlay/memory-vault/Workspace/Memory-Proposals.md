---
tags: [ciao, memory, proposals]
---
# Memory Proposals

Memory proposals awaiting a decision. Each batch is timestamped. The memory pass records what it is confident about; what lands here is what it was unsure about, or a write that failed.

Destinations: `[memory]` / `[profile]` are the bounded `ciao:memory` / `ciao:profile` regions of the workspace `AGENTS.md` (edit the region first, then dismiss with `ciao memory-proposal-dismiss --text-file <file> --promoted` so the outcome counts as a promotion); `[project <doc-path>]` folds into that canonical doc; `[people <Name>]` updates `People/<Name>.md`; `[learnings]` appends to `Workspace/Learnings.md`; `[review]` has no known destination yet — decide what it is first. Standing directives stay in the AGENTS.md body outside the fenced regions.
