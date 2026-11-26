# Pass 2: consolidate bounded regions

Check usage with `ciao memory status`. When a region is at or above ~85% of its cap (or over it), consolidate that region now. This run may edit regions for consolidation only. Before removing or replacing any entry, copy its original text into the undo log (`<vault>/Workspace/Memory-Consolidations.md`, under a `## YYYY-MM-DD` heading naming the region), so the user can restore any line. Create the file if it does not exist.

Consolidation means:

- Merge duplicate or near-duplicate entries into one present-tense rule.
- Replace a superseded entry with its current state.
- Drop entries whose `[expires: YYYY-MM-DD]` date has passed.
- Tighten verbose wording without losing any durable fact.
- Move project-scoped entries out to their owning project's canonical doc (move, do not delete).
- Preserve or add the trailing learned-at stamp `[YYYY-MM-DD]` on entries you rewrite (today's date for a merged entry).

When a removal needs judgment you cannot confidently make, keep the entry and add a line to `Workspace/Unsure.md` instead: `- [ ] drop <region>: "<entry text>" — unsure because <reason>; source: care run; <YYYY-MM-DD>`. Do not add a line that is already there. The person ticks it to approve the drop, or deletes the line to keep the entry. A later run removes a ticked entry after copying it into `Memory-Consolidations.md`.

The cap is advisory: nothing refuses a write at edit time, and an over-cap write reports `over_cap`. A durable fact is never worth dropping just to fit the cap. If a region stays over cap because every entry is high-signal, say so and give the user their options: leave the region over its advisory cap, or ask any attended chat to consolidate further.
