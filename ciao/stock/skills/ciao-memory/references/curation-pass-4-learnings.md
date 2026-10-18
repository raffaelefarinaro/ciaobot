# Pass 4: maintain learnings

`Workspace/Learnings.md` entries are structured: `- [key] [first-seen → last-seen] (xN) statement — sources: chat-a, chat-b`. The engine increments the count when the same statement recurs; your job is judgment.

## Route an entry at x3 or more

An x3 is a priority order, not permission to edit: it tells you which finding to file first, and a lesson is resolved only when a person applied or rejected the finding. Read `Workspace/Learnings.md`, decide where the lesson belongs, and file the matching item:

1. A skill this workspace owns under `skills/`: read the skill's current source first, and file nothing when it already says this. Otherwise file a skill proposal with `ciao skill-proposal-add NAME --input-file FILE`. The `origins` entry names that entry's `learning_id`, the one specific thing in it this change addresses, and the sha256 of that entry's own canonical line as `source_revision`. The canonical line is the exact bytes of the bullet you read, `ciao:learning` comment included.
2. A packaged, mirrored or shared skill, or a lesson no skill covers: file a `[review]` draft with `ciao skill-draft-add --input-file FILE`. Somebody else maintains packaged skills, so you do not edit them.
   - For a packaged skill give the skill name, the owning repository and version if you can identify them (say so in the draft if you cannot; do not guess), and a `body` a stranger could reproduce: what was done, what went wrong, and the instruction that would have prevented it.
   - For a lesson no skill covers give the proposed skill name, its purpose, the trigger that should load it and the instruction it should carry.
   - Leave transcript excerpts, chat or vault paths, names and credentials out of the body, because a public issue is public.
3. A fact true only inside one project: use the existing `[project]` accept. A cross-project preference or identity goes to the user under **What needs you**, never into a region.

Leave the entry under `## Active`.

## Merge and prune

- Merge entries that are semantically the same learning written differently: keep one, sum the counts, union the sources.
- Prune x1 entries under `## Active` that are older than 30 days with no reuse value.
- Entries under `## Promoted / Resolved` stay where they are. Retiring one is the attended `ciao learnings-cleanup` flow, which leaves a receipt, so report it under **What needs you** instead of moving it.
