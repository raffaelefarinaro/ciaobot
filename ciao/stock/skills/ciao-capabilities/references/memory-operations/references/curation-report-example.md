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
- [x] stale: re-check stale notes (4 listed, weekly due) — checked 4 notes, re-checked in full 3 (People/Rae.md, Projects/Atlas.md, People/Ana.md), fixed 1, Unsure 1; verify: none of the 3 re-checked paths still listed, 4 lines dated today in .care-stale-checked
- [x] hygiene: rebuild the index and fix what is safe (weekly due) — fixed 3 of 3 findings, left 0, proposals 0; verify: INDEX.md rewritten today, 0 of the 3 fixed findings in the re-audit, 3 -> 0
- [!] guide-review: review the AGENTS.md body (weekly due) — blocked: AGENTS.md unreadable

## Hygiene
- index rebuilt; audit exit 1, 3 findings before, 0 after; fixed 3

## What needs you
1. Tomas Berg had two notes, so his facts were moved into one and the old note can go. Choose Review proposal, then Approve or Reject. (path: Workspace/Proposals/retire-tom.md)
2. 2 suggestions are waiting in Unsure: the office wifi line and Rae's new team. Open Memory > Unsure and approve or reject them. (path: Workspace/Unsure.md)

## Notes
- The review of your guide could not read the file; it tries again on the next run.
- 2 memory passes were not re-read this time; the next run continues.
```

Checkbox lines appear only in the checklist above the first `##` heading; every section after it (`## Hygiene`, `## What needs you`, `## Notes`) uses plain bullets or a numbered list. `Weekly retry:` names each weekly step still blocked and for how many runs in a row; after 3 runs it is dropped and listed under What needs you.

## What needs you, and Notes

Each item under `## What needs you` follows the standard in `step-preamble.md`: one decision, at most two short sentences in everyday words (the situation, the recommendation, how to act), ending in `(path: …)` when there is a file to open. The app reads the path to pick the button: a proposal opens a review with Approve and Reject, `Workspace/Unsure.md` opens Unsure, `AGENTS.md` opens the guide, another vault file opens that file. Every item also offers "Fix with a chat".

- Open Unsure lines are one item for the whole list: "N suggestions are waiting in Unsure", naming at most the first 3, then "Open Memory > Unsure and approve or reject them.", with `(path: Workspace/Unsure.md)`.
- Hygiene fixes what a rule settles and leaves at most one item for skills that are too long (pointing at the first open proposal) and one for findings that need a decision, never one item per finding.
- A region still nearly full because everything in it is durable is one item: say so and recommend "Choose Fix with a chat to trim it together." with `(path: AGENTS.md)`.
- `## Notes` holds what the person need not act on: the steps' `note:` items, a blocked step that runs again next time, the interrupted ids, leftover counts. Plain `- ` bullets, never checkboxes.

A worked example, from a real run that wrote items nobody could act on. What the run wrote first, then what the standard asks for:

| The run wrote | Under the standard |
|---|---|
| "stale [!]: the stale-notes audit reads frontmatter `updated:`, which the step may not change, so stamped notes stay listed (297 stale). Decide whether … Retry 1 of 3." | Note: "Re-checking older notes did not finish; it tries again on the next run." |
| "Approve or reject `Workspace/Proposals/guide-trim-duplicates.md` (removes the tool status section and a duplicate chat-logs path from AGENTS.md)." | 1. "A proposal would trim your guide: it removes an outdated status section and a repeated folder path. Choose Review proposal, then Approve or Reject. (path: Workspace/Proposals/guide-trim-duplicates.md)" |
| "Both memory regions are still high (memory 97.6%, profile 95.1%); all remaining entries are durable. Choose what leaves in an attended chat. The scheduled-prompts rule on Unsure cannot go in until then." | 2. "Your always-loaded memory is nearly full (98% and 95%) and everything in it still matters. Choose Fix with a chat to trim it together. (path: AGENTS.md)" Note: "One suggestion in Unsure waits until memory has room." |
| "13 open lines in `Workspace/Unsure.md`: pick the canonical brief template, Project B brief status, a new People page, a stub merge, Calendar edits rule placement, and others. Tick `- [x]` to apply." | 3. "13 suggestions are waiting in Unsure, such as which brief template to keep, a project's status and a new People page. Open Memory > Unsure and approve or reject them. (path: Workspace/Unsure.md)" |
| "Second-look left 17 memory passes unread (cap); drain left Observations.md (~200 patterns) for the next run." | Notes: "17 memory passes were not re-read this time; the next run continues." and "About 200 older patterns are still to be moved to Unsure; the next run continues." |
| "Hygiene: 63 findings listed under `## Hygiene` (orphans, frontmatter, 6 skills over size budget)." | The step fixes what a rule settles (a missing or misspelled category, a link to a note that moved, a note nothing links to) and lists the rest under `## Hygiene`. 4. "6 skills are longer than they should be, and a proposal to shorten each is ready. Choose Review proposal to approve or reject the first. (path: Workspace/Proposals/shorten-skill-one.md)" 5. "4 notes need a decision the run cannot make alone, such as two notes for one person. Choose Fix with a chat to go through them. (path: Workspace/Care-Report.md)" |

## The final reply

The reply is read by someone who has never opened these files. It is 3 to 6 short lines: a verdict, then only the needs-you items, one plain sentence each, naming the place and the button in the app. Never a step id, a file field, a Markdown instruction ("tick `- [x]`") or a decision the run should make itself; a blocked step is in the report's Notes, not in the reply:

```
Care finished: 6 of 7 steps. 2 things need you.
1. Tomas Berg had two notes: review the proposal in Memory > Workspace care and approve or reject it.
2. Open Memory > Unsure and approve or reject the 2 suggestions waiting there.
```

When nothing needs the person, the reply is the verdict alone: "Care finished: 7 of 7 steps. Nothing needs you." When nothing was due, it is one line: "Nothing new to file today — memory is tidy."
