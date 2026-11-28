# Step unsure: apply the ticked lines on the Unsure list

## Inputs
- `<vault>/Workspace/Unsure.md`: a `- [x]` line is approved by a person; a `- [ ]` line is not yours to act on.
- `unsure-and-proposals.md` in this folder: the line format and the table of kinds. Read it first.

## Procedure
1. For each ticked line, in file order, do what the last column of that table says for its kind. A fact you write goes to the destination the line names (its `to:` field when it has one), and when the line quotes the exact line to write, write that line. A `to:` must be the region `profile` or `memory`, or a vault-relative `.md` note path with no leading `/` or `~`, no `..`, `.` or hidden (dot) segment, not under `Workspace/` or `automations/` and not `AGENTS.md`; check this yourself before every write, comparing case-insensitively and after Unicode compatibility folding (`WORKSPACE/`, `Workſpace/`, fullwidth letters and trailing dots or spaces count as the forbidden name), because the page's check does not stop a line from being ticked; for any other `to:` write nothing, leave the line ticked and count it with the lines left in step 2. After putting back a `restore to <region>:` entry, add an undo-log entry for it with `by: care unsure` (why: ticked restore), so the second-look step leaves it alone. Run `ciao memory status` after every region write.
2. Delete a line only after its write succeeded and you re-read the destination. A line whose kind the table says to leave (`category:`, `repo <path>:`, `rule flip`) stays ticked: count it; the done line reports it, never as a new item.
3. Unticked `observation:` lines that already name three distinct chat ids, at most 3 per run (the rest wait for the next run): read those chats first (the `archive_path` from `ciao chat get --chat <chat id>`, or `<agent root>/Logs/Chats/<chat id>/<provider>/*.md`). When the person's own turns show the pattern, write the fact to its profile or people destination, citing the chat ids as its source, then delete the line; otherwise leave the line. Leave every other unticked line alone.
4. If a write fails or a destination is unclear, leave that line as it was and count it.

## Done when
Every ticked line was applied and deleted, or left on purpose by step 1 or 2. Finish with `done: applied N, left M ticked (<kinds>), promoted K observations, W observations at x3 left for the next run`, and, when M is above 0, one item for the lines left: `needs you: M suggestions you approved need one more step from you, such as adding a category in Settings. Choose Open Unsure to see them. (path: Workspace/Unsure.md)`. The open lines are the run's one Unsure item, not yours.

Verify: count the remaining `- [x]` lines in `<vault>/Workspace/Unsure.md` (`grep -c '^- \[x\]'`); it must equal M, the lines left on purpose.
