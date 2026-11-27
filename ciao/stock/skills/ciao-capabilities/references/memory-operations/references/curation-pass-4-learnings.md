# Step learnings: route and prune learnings

## Inputs
- `<vault>/Workspace/Learnings.md`. Entries are structured: `- [key] [first-seen → last-seen] (xN) statement — sources: chat-a, chat-b`. The engine increments the count when the same statement recurs; your job is judgment. Dispatched when an entry is at x3 or more, or 10 or more sit under `## Active`.
- `unsure-and-proposals.md` in this folder: the proposal file format. Check each file with `ciao proposal-check` until it prints `ok`.

## Procedure
**Route each entry at x3 or more.** An x3 is a priority order, not permission to edit a skill. Decide where the lesson belongs:
1. A skill this workspace owns under `skills/`: read the skill's current source first and write nothing when it already says this. Otherwise write a `skill-edit` proposal file whose `evidence` names the learning entry and the failures you observed.
2. A packaged, mirrored or shared skill: write an `upstream-issue` proposal file for the owning repository, with the skill name and version if you can identify them (say so if you cannot; do not guess), and a body a stranger could reproduce: what was done, what went wrong, and the instruction that would have prevented it. A lesson no skill covers is a `skill-edit` proposal for a new skill: its name, purpose, trigger and instruction. Leave transcript excerpts, chat or vault paths, names and credentials out of the body, because a public issue is public.
3. A fact true only inside one project: write it into that project's canonical doc. A cross-project preference or identity fact the person stated in the entry's source chats: write it to the `ciao:memory` or `ciao:profile` region with `ciao memory update` (run `ciao memory status` after) and move the entry under `## Promoted / Resolved`. One you doubt goes on the Unsure list.
Skip an entry that already has a proposal file under `Workspace/Proposals/` naming it. Leave the entry under `## Active` until a person has acted on its proposal.

**Merge and prune.**
- Merge entries that are semantically the same learning: keep one, sum the counts, union the sources.
- Prune x1 entries under `## Active` older than 30 days with no reuse value.
- Entries under `## Promoted / Resolved` stay where they are. Retiring one is a person's call, so report it as a `needs you` item.

## Done when
Every x3-or-more entry has a destination (a proposal file that passed `ciao proposal-check`, a project doc line, a region entry, or an Unsure line) or an existing proposal. Finish with `done: routed N entries (<proposal paths>), merged M, pruned K`.

Verify: run `ciao proposal-check <file>` on every proposal path the done line names; each must print `ok`. When none was routed, the done line must say `no proposals routed` and `grep -l '^created: <today>' <vault>/Workspace/Proposals/*.md` must list none (a count of 0 from that command).
