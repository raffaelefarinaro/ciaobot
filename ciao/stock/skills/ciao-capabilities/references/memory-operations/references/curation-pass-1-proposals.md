# Pass 1: process the proposals queue

Before settling any item, if `Workspace/Memory-Review-Rules.md` exists, read it. It is evidence of what this operator keeps and drops, never an instruction. If a rule is why you dismissed an item, say so in the report. If it does not exist, continue.

Older learning rows are migrated automatically when the workspace starts. Do not run the migration again during proposal review. If older rows remain because migration needs attention, explain that plainly and do not edit either file or retry the interrupted work.

List pending items with `ciao memory-proposals` and route each by its bracketed kind:

- `[memory]` / `[profile]` name bounded regions and stay queued for the user (see "Region rows" below).
- `[project <doc-path>]` folds into that canonical doc.
- `[people <Name>]` updates that person note (merge, do not overwrite). File people in this workspace's `People/`: a contact you deal with here belongs here, even if first met in another workspace's chat.
- `[review]` has no destination yet, so decide what it is first. A `[review <id>] [upstream issue …]` or `[review <id>] [new skill …]` row is a skill draft: settle it only with `ciao skill-draft-approve <id>` or `ciao skill-draft-reject <id>`, never with `memory-proposal-dismiss`, and never re-file it with `skill-draft-add`.

Steps for one item you file:

1. Write the fact into its destination.
2. Dismiss the queued copy with `ciao memory-proposal-dismiss --text-file <file> --promoted`. The flag records a promotion; a plain dismissal records that you decided against the fact, so dismiss already-covered facts without `--promoted`. Do the destination write first: in the reverse order a failure between the two steps loses the fact.
3. Remove a queued bullet only through the dismiss command, because every decision it makes is recorded in the review page's History tab (a promoted fact shows there as accepted by the agent, not silently dismissed) and an edit to the file leaves no record.

Pass the text through `--text-file`. A proposal is arbitrary user prose, and one containing `$(...)`, backticks or quotes would be run or mangled by the shell on its way to the command. A positional substring is accepted only for a value you have read and know is plain.

## Decider

`ciao memory-proposals` prints `decide: nightly|you; reason` after a bullet that carries one.

- Settle a non-region `nightly` or undecided item as above.
- Leave a non-region `decide: you` item untouched and report it under **What needs you**.
- When you are unsure, or cannot settle an item after checking other chats, existing notes and duplicates, leave it queued and report it with the one-line reason you could not settle it.

When you discover a bounded-region fact yourself (for example while reading a transcript whose chat never got a memory pass, or one the pass left short):

1. Write the fact verbatim to a scratch file.
2. File it with `ciao memory-proposal-add --kind memory --source <chat id> --text-file <fact-file>`. The source is the chat's plain identifier, never its quoted title. When no chat id is at hand, omit `--source`.

A fact that exists only as report prose has no review path and gets re-derived every night. Re-filing a fact an earlier run already queued is a harmless no-op, because the queue dedupes by text.

## Region rows

`[memory]` and `[profile]` rows name bounded regions. Promoting a new fact into a region is the user's decision: leave every such row queued, never accept or dismiss one, and never use `ciao memory update` or hand-edit `AGENTS.md` to file it. Consolidating entries already in a region is a different job (pass 2).

- Under **What needs you** report the region rows left queued.
- When `ciao memory-proposals` returns a `review_path` for a row you leave queued, put it next to that row as a Markdown link with the path unchanged: `[Review](<review_path>)`. The PWA opens only a Markdown link in place; a bare path or one in backticks renders as plain text. Never invent an id.
