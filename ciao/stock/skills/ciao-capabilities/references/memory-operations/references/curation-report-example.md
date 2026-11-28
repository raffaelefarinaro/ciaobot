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
2. Suggestions are waiting for your decision. Open Memory > Unsure and approve or reject them. (path: Workspace/Unsure.md)

## Notes
- The review of your guide could not read the file; it tries again on the next run.
- 2 memory passes were not re-read this time; the next run continues.
```

Checkbox lines appear only in the checklist above the first `##` heading; every section after it (`## Hygiene`, `## What needs you`, `## Notes`) uses plain bullets or a numbered list. `Weekly retry:` names each weekly step still blocked and for how many runs in a row; after 3 runs it is dropped and listed under What needs you.

## What needs you, and Notes

Each item under `## What needs you` follows the standard in `step-preamble.md`: one decision, at most two short sentences in everyday words (the situation, the recommendation, how to act), ending in `(path: …)` when there is a file to open. The app reads the path to pick the button: a proposal opens a review with Approve and Reject, `Workspace/Unsure.md` opens Unsure, `AGENTS.md` opens the guide. A proposal, `Workspace/Unsure.md` and `AGENTS.md` each get that one button only. An item that names another vault note is a decision about it: "Decide in a chat" pins the note and opens a chat that asks what to do, with "Open note" beside it. An item with no path offers "Fix with a chat". When the item's details are listed under a heading of the report, the tail names it after the path (`(path: People/User.md; section: Hygiene)`, or `(section: Hygiene)` alone), and the app adds "See details", which opens the report at that heading.

- Open Unsure lines are one item for the whole list, whoever added them: "Suggestions are waiting for your decision. Open Memory > Unsure and approve or reject them. (path: Workspace/Unsure.md)". It has no number: the report is a snapshot of the last run and the list changes after it, so the app shows the live count on the Open Unsure button.
- Hygiene fixes what a rule settles and leaves at most one item for skills that are too long (pointing at the first open proposal) and one item per kind of finding that needs a decision (its path the most useful real note, never `Workspace/Care-Report.md`, then `; section: Hygiene`), never one item per finding.
- A region still above 100% of its cap because everything in it is durable is one item: say so, in whole percentages, and recommend "Decide what to trim." with `(path: AGENTS.md)`. Only a region above 100% of its cap gets an item and is named, never a lower percentage; a region between 85% and 100% that stays high-signal is at most a note, and no item is written when no region is above 100%.
- `## Notes` holds what the person need not act on: the steps' `note:` items, a blocked step that runs again next time, the interrupted ids, leftover counts. Plain `- ` bullets, never checkboxes.

A worked example, from a real run that wrote items nobody could act on. What the run wrote first, then what the standard asks for:

| The run wrote | Under the standard |
|---|---|
| "stale [!]: the stale-notes audit reads frontmatter `updated:`, which the step may not change, so stamped notes stay listed (297 stale). Decide whether … Retry 1 of 3." | Note: "Re-checking older notes did not finish; it tries again on the next run." |
| "Approve or reject `Workspace/Proposals/guide-trim-duplicates.md` (removes the tool status section and a duplicate chat-logs path from AGENTS.md)." | 1. "A proposal would trim your guide: it removes an outdated status section and a repeated folder path. Choose Review proposal, then Approve or Reject. (path: Workspace/Proposals/guide-trim-duplicates.md)" |
| "Both memory regions are still high (memory 112.4%, profile 104.1%); all remaining entries are durable. Choose what leaves in an attended chat. The scheduled-prompts rule on Unsure cannot go in until then." | 2. "Your always-loaded memory is nearly full (112% and 104%) and everything in it still matters. Decide what to trim. (path: AGENTS.md)" Note: "One suggestion in Unsure waits until memory has room." |
| "13 open lines in `Workspace/Unsure.md`: pick the canonical brief template, Project B brief status, a new People page, a stub merge, Calendar edits rule placement, and others. Tick `- [x]` to apply." | 3. "Suggestions are waiting for your decision. Open Memory > Unsure and approve or reject them. (path: Workspace/Unsure.md)" |
| "Second-look left 17 memory passes unread (cap); drain left Observations.md (~200 patterns) for the next run." | Notes: "17 memory passes were not re-read this time; the next run continues." and "About 200 older patterns are still to be moved to Unsure; the next run continues." |
| "Hygiene: 63 findings listed under `## Hygiene` (orphans, frontmatter, 6 skills over size budget)." | The step fixes what a rule settles (a missing or misspelled category, a link to a note that moved, a note nothing links to) and lists the rest under `## Hygiene`. 4. "6 skills are longer than they should be, and a proposal to shorten each is ready. Choose Review proposal to approve or reject the first. (path: Workspace/Proposals/shorten-skill-one.md)" 5. "Links in People/User.md and 3 other notes point outside this vault. Decide whether each should stay or be removed. (path: People/User.md; section: Hygiene)" (one item per kind, its path the most useful real note, never the report, and the section that lists the rest) |

A later run copied its steps' old lines as they were, so the page could show no button for them. Each breaks the standard (backticks, a step named, a Markdown instruction, no path), the self-check in `step-preamble.md` prints all three, and the orchestrator rewrites them:

| The run wrote | Under the standard |
|---|---|
| "Memory regions are still high (memory 111.6%, profile 103.8%) and every entry is a durable rule: choose in an attended chat what leaves." | 1. "Your always-loaded memory is nearly full (112% and 104%) and everything in it still matters. Decide what to trim. (path: AGENTS.md)" |
| "Open lines in `Workspace/Unsure.md`: 17 (4 added by the stale step). Tick `- [x]` to apply." | 2. "Suggestions are waiting for your decision. Open Memory > Unsure and approve or reject them. (path: Workspace/Unsure.md)" |
| "Approve or reject `Workspace/Proposals/agents-md-trim-duplicates.md` and the other proposals filed today." | 3. "A proposal would remove repeated lines from your guide. Choose Review proposal, then Approve or Reject. (path: Workspace/Proposals/agents-md-trim-duplicates.md)" Each other proposal filed that day is its own item with its own path. |

## The final reply

The reply is read by someone who has never opened these files. It is 3 to 6 short lines: a verdict, then only the needs-you items, one plain sentence each, naming the place and the button in the app. Never a step id, a file field, a Markdown instruction ("tick `- [x]`") or a decision the run should make itself; a blocked step is in the report's Notes, not in the reply:

```
Care finished: 6 of 7 steps. 2 things need you.
1. Tomas Berg had two notes: review the proposal in Memory > Workspace care and approve or reject it.
2. Open Memory > Unsure and approve or reject the suggestions waiting there.
```

When nothing needs the person, the reply is the verdict alone: "Care finished: 7 of 7 steps. Nothing needs you." When nothing was due, it is one line: "Nothing new to file today — memory is tidy."
