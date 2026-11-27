# Step stale: re-check stale notes against their sources

## Inputs
- `ciao memory-audit --with-vault --json`: `stale_notes.stale_notes` lists the stale notes, each with a `path` such as `memory-vault/<workspace>/People/Rae.md`. The audit scans the whole vault root: skip any path outside this workspace, and strip the leading `memory-vault/<this workspace>/` (V's own folder) from the rest, since `ciao note related` wants `People/Rae.md`. A note is listed while its frontmatter `updated:` (else its file date) is 30 or more days old.
- "Writing entity notes" in `../guide.md` (one fact per bullet, `[verified: YYYY-MM-DD]` stamps, when `updated:` may change).

## Procedure
Take at most 5 notes, oldest first. This step may use about 40 tool calls. For each note:
1. Read it, then run `ciao note related <path>`. A disagreeing related note is evidence, and a related note you actually read counts as checked. Reading the note alone is not checking it.
2. Judge each fact bullet against real sources (the related notes, the files and projects it names). Fix what the sources plainly contradict, citing the source, directly in the note. Re-read the note after every write before judging a second fact.
3. Add `[verified: <today>]` to a bullet only after checking that exact sentence (a fixed bullet after checking its new text). Never stamp on save or merge.
4. When every fact bullet of the note now carries `[verified: <today>]`, set its frontmatter `updated:` to today: the note was re-checked in full, which is what `updated:` means. A note with any bullet unchecked or sent to the Unsure list, or with its facts only in prose, keeps its `updated:`. Change nothing else in the frontmatter.
5. Anything doubtful, and any conflict between two sources (quote both), goes on `Workspace/Unsure.md` (skip a line already there). New advice that supersedes a stored line replaces it; the note keeps one instruction with its source and date.
6. Never retire or delete a note: write a `note-retire` proposal file (`unsure-and-proposals.md` in this folder) and run `ciao proposal-check` on it.

## Done when
Each of the chosen notes was read, had `ciao note related` run, and was fixed, verified, or put on the Unsure list. Finish with `done: checked N notes, re-checked in full K (<paths>), fixed X, Unsure Y, retire proposals Z`. The other stale notes are for later runs: their number is not a `needs you` item or a note.

Verify: re-run `ciao memory-audit --with-vault --json` (exit 1 only means findings exist) and read `stale_notes.stale_notes` (this workspace's paths only): none of the K paths re-checked in full may still be listed, and each of them has `updated: <today>`. The total count is not the check: it falls by at most K per run.
