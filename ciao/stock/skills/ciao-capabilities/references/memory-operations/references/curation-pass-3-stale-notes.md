# Step stale: re-check stale notes against their sources

## Inputs
- `ciao memory-audit --with-vault --json`: `stale_notes.stale_notes` lists the stale notes, each with a `path` such as `memory-vault/<workspace>/People/Rae.md`. The audit scans the whole vault root: skip any path outside this workspace, and strip the leading `memory-vault/<this workspace>/` (V's own folder) from the rest, since `ciao note related` wants `People/Rae.md`.
- "Writing entity notes" in `../guide.md` (one fact per bullet, `[verified: YYYY-MM-DD]` stamps).

## Procedure
Take at most 5 notes, oldest first. This step may use about 40 tool calls. For each note:
1. Read it, then run `ciao note related <path>`. A disagreeing related note is evidence, and a related note you actually read counts as checked. Reading the note alone is not checking it.
2. Judge each fact against real sources (the related notes, the files and projects it names). Fix what the sources plainly contradict, citing the source, directly in the note. Re-read the note after every write before judging a second fact.
3. Add `[verified: <today>]` to a bullet only after checking that exact sentence. Never stamp on save or merge, and leave `updated:` alone.
4. Anything doubtful, and any conflict between two sources (quote both), goes on `Workspace/Unsure.md` (skip a line already there). New advice that supersedes a stored line replaces it; the note keeps one instruction with its source and date.
5. Never retire or delete a note: write a `note-retire` proposal file (`unsure-and-proposals.md` in this folder) and run `ciao proposal-check` on it.

## Done when
Each of the chosen notes was read, had `ciao note related` run, and was fixed, verified, or put on the Unsure list. Finish with `done: checked N notes (<paths>), fixed X, Unsure Y, retire proposals Z, W stale notes remain`.

Verify: re-run `ciao memory-audit --with-vault --json` (exit 1 only means findings exist) and compare `stale_notes.stale_notes` (this workspace's paths only) with the list from before: no path the step says it fixed or verified may still be listed, and the count must be W or fewer.
