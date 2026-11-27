# Step regions: consolidate bounded regions

## Inputs
- `ciao memory status`: usage per region of this workspace's `AGENTS.md` (`ciao:memory`, `ciao:profile`). Dispatched when a region's `pct` is 85 or more.
- The undo log `<vault>/Workspace/Memory-Consolidations.md` (create it if missing).

## Procedure
Consolidate each region at or above ~85% (or over its cap). Consolidation rewrites what the regions already hold; it is not where new facts come from. Before removing or replacing any entry, log it in the undo log, so the person can restore any line.

- Merge duplicate or near-duplicate entries into one present-tense rule.
- Replace a superseded entry with its current state.
- Drop entries whose `[expires: YYYY-MM-DD]` date has passed.
- Tighten verbose wording without losing any durable fact.
- Move project-scoped entries out to their owning project's canonical doc (move, do not delete).
- Keep or add the trailing learned-at stamp `[YYYY-MM-DD]` on entries you rewrite (today's date for a merged entry).

Run `ciao memory status` after every region write. When a removal needs judgment you cannot make confidently, keep the entry and add a line to `Workspace/Unsure.md` (skip one already there): `- [ ] drop <region>: <plain question, e.g. Remove the note that … from what every chat loads?> "<entry text>" — unsure because <reason>; source: care run; <YYYY-MM-DD>` (the line format is in `unsure-and-proposals.md`).

The cap is advisory: nothing refuses a write, and a durable fact is never worth dropping to fit it. If a region stays over because every entry is high-signal, say so as a `needs you` item: leave it over the cap, or ask an attended chat to consolidate further.

## Done when
`ciao memory status` is below 85% for every region, or each region still at or above it has a reason (all high-signal, or Unsure lines added). Finish with `done: memory A% -> B%, profile C% -> D%, N entries logged in Memory-Consolidations.md`.

Verify: run `ciao memory status` and report `data.regions.memory.pct` and `data.regions.profile.pct`; each must match the step's numbers, and each at or above 85 needs the reason.
