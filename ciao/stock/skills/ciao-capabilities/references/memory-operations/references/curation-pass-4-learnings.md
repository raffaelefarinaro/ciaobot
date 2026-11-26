# Pass 4: maintain learnings

`Workspace/Learnings.md` entries are structured: `- [key] [first-seen → last-seen] (xN) statement — sources: chat-a, chat-b`. The engine increments the count when the same statement recurs; your job is judgment.

## Route an entry at x3 or more

An x3 is a priority order, not permission to edit a skill: it tells you which lesson to route first. Read `Workspace/Learnings.md`, decide where the lesson belongs, and route it:

1. A skill this workspace owns under `skills/`: read the skill's current source first, and write nothing when it already says this. Otherwise write a `skill-edit` proposal file (see `unsure-and-proposals.md`) whose `evidence` names the learning entry and the failures you observed, and run `ciao proposal-check` on it.
2. A packaged, mirrored or shared skill: write an `upstream-issue` proposal file for the owning repository, with the skill name and version if you can identify them (say so if you cannot; do not guess), and a body a stranger could reproduce: what was done, what went wrong, and the instruction that would have prevented it. A lesson no skill covers is a `skill-edit` proposal for a new skill: its name, purpose, trigger and instruction. Leave transcript excerpts, chat or vault paths, names and credentials out of the body, because a public issue is public.
3. A fact true only inside one project: write it into that project's canonical doc. A cross-project preference or identity: write it to its region when it is clearly true, otherwise put it on the Unsure list.

Leave the entry under `## Active` until a person has acted on its proposal.

## Merge and prune

- Merge entries that are semantically the same learning written differently: keep one, sum the counts, union the sources.
- Prune x1 entries under `## Active` that are older than 30 days with no reuse value.
- Entries under `## Promoted / Resolved` stay where they are. Retiring one is the attended `ciao learnings-cleanup` flow, which leaves a receipt, so report it under **What needs you** instead of moving it.
