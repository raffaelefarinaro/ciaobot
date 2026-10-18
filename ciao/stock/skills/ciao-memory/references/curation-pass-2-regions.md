# Pass 2: consolidate bounded regions

Check usage with `ciao memory status`. When a region is at or above ~85% of its cap (or over it), consolidate that region now. This run may edit regions for consolidation only. Before removing or replacing any entry, copy its original text into the undo log (`<vault>/Workspace/Memory-Consolidations.md`, under a `## YYYY-MM-DD` heading naming the region), so the user can restore any line.

Consolidation means:

- Merge duplicate or near-duplicate entries into one present-tense rule.
- Replace a superseded entry with its current state.
- Drop entries whose `[expires: YYYY-MM-DD]` date has passed.
- Tighten verbose wording without losing any durable fact.
- Move project-scoped entries out to their owning project's canonical doc (move, do not delete).
- Preserve or add the trailing learned-at stamp `[YYYY-MM-DD]` on entries you rewrite (today's date for a merged entry).

When a removal needs judgment you cannot confidently make, keep the entry and queue a yes/no question instead:

1. Write `Keep "<entry text>" in ciao:<region>? Proposed action: drop because <reason>. (memory curation)` to a scratch file.
2. File it with `ciao memory-proposal-add --kind review --text-file <file>`. The command flattens the text onto one line and dedupes an unanswered question already queued.

The queue cannot apply a drop. The user dismisses the question to keep the entry (`ciao memory-proposal-dismiss --text-file <file>`, with no `--action`, since the question template always contains double quotes), or asks an attended chat to drop it. The Proposals panel's accept button is disabled for review rows on purpose.

The cap is advisory: nothing refuses a write at edit time, and an over-cap write reports `over_cap`. A durable fact is never worth dropping just to fit the cap. If a region stays over cap because every entry is high-signal, say so and give the user their options: leave the region over its advisory cap, or ask any attended chat to consolidate further.
