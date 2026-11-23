# Drain pending learning proposals

Run `ciao learnings-queue-drain` for this workspace and show the rows it found.
If the preview is empty, report that no work remains. Otherwise explain that
applying records one reversible batch receipt, then run
`ciao learnings-queue-drain --apply` only after the user asks to apply it. Keep
the receipt path in the report. It restores the whole batch with
`ciao learnings-queue-drain --revert RECEIPT --apply`.

Do not edit `Memory-Proposals.md` or `Learnings.md` by hand. The command carries
the source and request identity into Learnings.md and records each accepted row
in History.
