# Pass 1: process the proposals queue

List pending items with `ciao memory-proposals` and route each by its bracketed kind:

- `[memory]` / `[profile]` name bounded regions and stay queued for the user.
- `[project <doc-path>]` folds into that canonical doc.
- `[people <Name>]` updates that person note (merge, do not overwrite). File people in this workspace's `People/`: a contact you deal with here belongs here, even if first met in another workspace's chat.
- `[learnings]` files into `Workspace/Learnings.md` (see the learnings pass).
- `[review]` has no destination yet, so decide what it is first.

Steps for one item you file:

1. Write the fact into its destination.
2. Dismiss the queued copy with `ciao memory-proposal-dismiss --text-file <file> --promoted`. The flag records a promotion; a plain dismissal records that you decided against the fact, so dismiss already-covered facts without `--promoted`. Do the destination write first: in the reverse order a failure between the two steps loses the fact.
3. Remove a queued bullet only through the dismiss command, because every decision it makes is recorded in the review page's History tab (a promoted fact shows there as accepted by the agent, not silently dismissed) and an edit to the file leaves no record.

Pass the text through `--text-file`. A proposal is arbitrary user prose, and one containing `$(...)`, backticks or quotes would be run or mangled by the shell on its way to the command. A positional substring is accepted only for a value you have read and know is plain.

When you discover a bounded-region fact yourself (for example while reading a transcript whose chat never got a memory pass, or one the pass left short):

1. Write the fact verbatim to a scratch file.
2. File it with `ciao memory-proposal-add --kind memory --source <chat id> --text-file <fact-file>`. The source is the chat's plain identifier, never its quoted title. When no chat id is at hand, omit `--source`.

A fact that exists only as report prose has no review path and gets re-derived every night. Re-filing a fact an earlier run already queued is a harmless no-op, because the queue dedupes by text.
