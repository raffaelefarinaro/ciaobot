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
- [x] stale: re-check stale notes (4 listed, weekly due) — checked 4 notes (People/Rae.md, ...), fixed 1, Unsure 1; verify: 3 of 4 still listed, none of the fixed paths
- [!] guide-review: review the AGENTS.md body (weekly due) — blocked: AGENTS.md unreadable

## Hygiene
- index rebuilt; audit exit 1, 1 finding
- dead_link: Projects/Atlas.md links to People/Old.md
```

Checkbox lines appear only in the checklist above the first `##` heading; every section after it (`## Hygiene`, `## What needs you`) uses plain bullets or a numbered list. `Weekly retry:` names each weekly step still blocked and for how many runs in a row; after 3 runs it is dropped and listed under What needs you.

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
1. Retire the duplicate note People/Tom.md? Its facts are now in Tomas Berg's note; the proposal is Workspace/Proposals/retire-tom.md.
2. Should I drop "Office wifi password is on the fridge" from your standing memory? It looks out of date.
3. The review of AGENTS.md could not run (file unreadable).

Reply with the numbers to approve, or "skip the rest".
```

When nothing was due, the whole reply is one line: "Nothing new to file today — memory is tidy."
