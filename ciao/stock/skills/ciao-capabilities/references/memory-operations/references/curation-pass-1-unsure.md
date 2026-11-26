# Step unsure: apply the ticked lines on the Unsure list

## Inputs
- `<vault>/Workspace/Unsure.md`: a `- [x]` line is approved by a person; a `- [ ]` line is not yours to act on.
- `unsure-and-proposals.md` in this folder: the line format and the table of kinds. Read it first.

## Procedure
1. For each ticked line, in file order, do what the last column of that table says for its kind. A fact you write goes to the destination the line names; a person ticking it is the approval, so a region write is allowed here. Run `ciao memory status` after every region write.
2. Delete a line only after its write succeeded and you re-read the destination. A line whose kind the table says to leave (`category:`, `repo <path>:`, `rule flip`) stays ticked: count it and report it as `needs you`, never as a new item.
3. An unticked `observation:` line that already names three distinct chat ids: write the fact to its profile or people destination and delete the line. Leave every other unticked line alone.
4. If a write fails or a destination is unclear, leave that line as it was and count it.

## Done when
Every ticked line was applied and deleted, or left on purpose by step 2. Finish with `done: applied N, left M ticked (<kinds>), promoted K observations`, putting each left line in `needs you: …` form.
