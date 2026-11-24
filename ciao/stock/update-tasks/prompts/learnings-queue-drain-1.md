# Drain pending learning proposals

This workspace's older learning rows are migrated automatically in the
background. Do not ask the user to approve a dry run or explain internal
receipts. Check that `unresolved` and `failed` are empty before taking any
action. If either contains an item, explain that migration needs attention and
ask the user before running any command to inspect or repair it. Never run the
drain command with `--apply` automatically. Rows deliberately restored by the
user stay queued; do not migrate them again automatically. Explain that they
remain in Memory → To decide, where the user can ask to remove one exact queued
row using its current revision. If migration already moved it,
inspect `ciao learnings-cleanup --json` and match the exact learning ID and
entry revision. Use the attended `--apply --approval-file` flow with the user's
reason and evidence. That flow removes the whole canonical learning entry. If
it contains independent evidence or the request does not clearly cover the
whole entry, leave it intact and explain that a broader removal needs the
user's direction.

Do not edit `Memory-Proposals.md` or `Learnings.md` by hand. Migration carries
the source and request identity into Learnings.md and records each accepted row
in History. If migration is blocked or failed, explain plainly that it needs
attention and do not retry, recover, or revert automatically. Leave any
interrupted work for an attended review.
