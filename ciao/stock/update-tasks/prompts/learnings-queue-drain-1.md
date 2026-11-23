# Drain pending learning proposals

Run `ciao learnings-queue-drain` for this workspace and show the rows it found.
An empty `rows` list means no rows are currently queued only when both
`unresolved` and `failed` are empty. If either contains an item, report that a
prepared or damaged transaction needs recovery or inspection; do not say there
is no work, and do not treat an empty queue as proof the transaction completed.
Explain the reported receipt state and ask the user before running any command
with `--apply`. Applying a pending batch records a reversible receipt. Reverting
an applied batch restores its queued rows with
`ciao learnings-queue-drain --revert RECEIPT --apply`.

Do not edit `Memory-Proposals.md` or `Learnings.md` by hand. The command carries
the source and request identity into Learnings.md and records each accepted row
in History. If a transaction is unresolved or damaged, explain what the command
reported and wait for the user's approval before recovery or reversion.
