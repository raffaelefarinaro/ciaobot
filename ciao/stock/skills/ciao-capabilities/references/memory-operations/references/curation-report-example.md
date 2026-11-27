# Sample Workspace care report and reply

`Workspace/Care-Report.md` is a checklist the orchestrator builds first and ticks as each step finishes:

```
# Care report 2026-11-26
Weekly done: 2026-11-26
Weekly retry: guide-review=1
Interrupted last run: guide-review
- [x] unsure: apply ticked Unsure lines (2 ticked) — applied 2, left 0 ticked; verify: 0 `- [x]` lines left
- [x] second-look: re-read what the memory passes wrote (2 archived since 2026-11-25) — read 2 passes, checked 9 lines, fixed 1 (merged People/Tom.md into People/Tomas Berg.md; note-retire proposal Workspace/Proposals/retire-tom.md), unsure 0, left 8; verify: proposal-check ok, People/Tomas Berg.md holds the moved lines
- [x] closed-tasks: learn from tasks finished without a memory pass (3 completed since 2026-11-25) — uncovered 3, read 3 tasks, skipped 0, wrote 1 fact (Projects/Atlas.md), unsure 0, left 2; verify: uncovered 3 = 3 + 0 + 0, Projects/Atlas.md has "- Atlas launch moved to March, decided with Rae"
- [x] regions: consolidate regions at or above 85% (memory 91%) — memory 91% -> 72%, 3 entries logged in Memory-Consolidations.md; verify: memory.pct 72, profile.pct 40
- [x] stale: re-check stale notes (4 listed, weekly due) — checked 4 notes, re-checked in full 3 (People/Rae.md, Projects/Atlas.md, People/Ana.md), fixed 1, Unsure 1; verify: none of the 3 re-checked paths still listed
- [!] guide-review: review the AGENTS.md body (weekly due) — blocked: AGENTS.md unreadable

## Hygiene
- index rebuilt; audit exit 1, 1 finding
- dead_link: Projects/Atlas.md links to People/Old.md

## What needs you
1. Tomas Berg had two notes, so his facts were moved into one and the old note can go. Open the proposal and approve or reject it. (path: Workspace/Proposals/retire-tom.md)
2. 2 suggestions are waiting for your tick in Unsure: the office wifi line and Rae's new team. Tick the ones you agree with. (path: Workspace/Unsure.md)
3. The vault index lists 1 issue: a note links to a page that no longer exists. Open a chat to fix it with you. (path: Workspace/Care-Report.md)

## Notes
- The review of your guide could not read the file; it tries again on the next run.
- 2 memory passes were not re-read this time; the next run continues.
```

Checkbox lines appear only in the checklist above the first `##` heading; every section after it (`## Hygiene`, `## What needs you`, `## Notes`) uses plain bullets or a numbered list. `Weekly retry:` names each weekly step still blocked and for how many runs in a row; after 3 runs it is dropped and listed under What needs you.

## What needs you, and Notes

Each item under `## What needs you` follows the standard in `step-preamble.md`: one decision, at most two short sentences in everyday words (the situation, the recommendation, how to act), ending in `(path: …)` when there is a file to open. The app reads the path to pick the button: a proposal opens a review with Approve and Reject, `Workspace/Unsure.md` opens Unsure, `AGENTS.md` opens the guide, another vault file opens that file. Every item also offers "Fix with a chat".

- Open Unsure lines are one item for the whole list: "N suggestions are waiting for your tick in Unsure", naming at most the first 3, with `(path: Workspace/Unsure.md)`.
- Hygiene findings are one item ("The vault index lists N issues …") with `(path: Workspace/Care-Report.md)`, never one item per finding.
- A region still nearly full because everything in it is durable is one item: say so and recommend "Ask a chat to trim it with you." with `(path: AGENTS.md)`.
- `## Notes` holds what the person need not act on: the steps' `note:` items, a blocked step that runs again next time, the interrupted ids, leftover counts. Plain `- ` bullets, never checkboxes.

A worked example, from a real run that wrote items nobody could act on. What the run wrote first, then what the standard asks for:

| The run wrote | Under the standard |
|---|---|
| "stale [!]: the stale-notes audit reads frontmatter `updated:`, which the step may not change, so stamped notes stay listed (297 stale). Decide whether … Retry 1 of 3." | Note: "Re-checking older notes did not finish; it tries again on the next run." |
| "Approve or reject `Workspace/Proposals/guide-trim-duplicates.md` (removes the tool status section and a duplicate chat-logs path from AGENTS.md)." | 1. "A proposal would trim your guide: it removes an outdated status section and a repeated folder path. Open the proposal and approve or reject it. (path: Workspace/Proposals/guide-trim-duplicates.md)" |
| "Both memory regions are still high (memory 97.6%, profile 95.1%); all remaining entries are durable. Choose what leaves in an attended chat. The scheduled-prompts rule on Unsure cannot go in until then." | 2. "Your always-loaded memory is nearly full (98% and 95%) and everything in it still matters. Ask a chat to trim it with you. (path: AGENTS.md)" Note: "One suggestion in Unsure waits until memory has room." |
| "13 open lines in `Workspace/Unsure.md`: pick the canonical brief template, Project B brief status, a new People page, a stub merge, Calendar edits rule placement, and others. Tick `- [x]` to apply." | 3. "13 suggestions are waiting for your tick in Unsure, such as which brief template to keep, a project's status and a new People page. Tick the ones you agree with. (path: Workspace/Unsure.md)" |
| "Second-look left 17 memory passes unread (cap); drain left Observations.md (~200 patterns) for the next run." | Notes: "17 memory passes were not re-read this time; the next run continues." and "About 200 older patterns are still to be moved to Unsure; the next run continues." |
| "Hygiene: 63 findings listed under `## Hygiene` (orphans, frontmatter, 6 skills over size budget)." | 4. "The vault index lists 63 issues, such as notes nothing links to, notes missing their details and 6 skills that are too long. Open a chat to fix them with you. (path: Workspace/Care-Report.md)" |

## The final reply

The final reply starts with `Care complete: N/N` (or `Care incomplete: N/M, blocked: <ids>`) and is read by someone who has never opened the docs. Use everyday words, name a file only when they must open it, and skip sections that are empty:

```
Care incomplete: 5/6, blocked: guide-review

What I did
- Filed 2 facts into their project notes.
- Re-read what yesterday's memory passes wrote; folded a duplicate note for Tom into Tomas Berg's note.
- Read 3 tasks you closed without a note; added the new Atlas launch date to the Atlas project note.
- Tidied standing memory from 91% to 72% full; the removed lines are saved in Memory-Consolidations.md.
- Re-checked 4 older notes; fixed one date in the Atlas project note.

What needs you
1. Tomas Berg had two notes, so his facts were moved into one and the old note can go. Open the proposal in Workspace care and approve or reject it.
2. 2 suggestions are waiting for your tick in Unsure.
3. The vault index lists 1 issue. Open a chat to fix it with you.

Notes
- The review of your guide could not read the file; it tries again on the next run.
```

When nothing was due, the whole reply is one line: "Nothing new to file today — memory is tidy."
