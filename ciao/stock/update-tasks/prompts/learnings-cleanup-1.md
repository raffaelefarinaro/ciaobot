# Review this workspace's `Learnings.md`

Run `ciao learnings-cleanup` in the workspace this task was offered for. It
prints one row per Active entry and writes nothing.

## What the rows mean

| Column | Read it as |
| --- | --- |
| `KEY` | the entry's own label; it is display, not identity |
| `ID` | the start of the `learning_id` in the entry's `<!-- ciao:learning … -->` comment — abbreviated for scanning, printed in full on the `id` line under the entry |
| `STATE` | `REMOVE`, `KEEP`, `LATER`, or `CONFLICT` |
| `REASON` | why, in the words of the check that decided it |
| `EVIDENCE` | the receipt or issue behind the decision |

The `id` and `rev` lines under each entry are the two values an approval names,
in full. Copy them from there: an approval is bound to the exact bytes it was
reviewed at, so an abbreviated id or revision is an approval of nothing.

`LATER` means the entry *is* settled but this run's cap was reached, or it is
settled and was not approved in this run. It is not work for you tonight.
`CONFLICT` is a line this code cannot read; it is never removed, and it is never
repaired. Say so and move on.

## What you may approve

Only a row the tool proposed to retire, and only with evidence you have checked
yourself. Read the skill the `EVIDENCE` column names, confirm the lesson is in
it, and only then approve.

`KEEP` rows are not yours to retire on this task's authority. An entry nothing
has ever proposed, an entry whose finding is still open, an entry somebody is
working on right now, and an entry whose only destination was an upstream issue
are all answers somebody has to give, and the honest answer is usually to leave
them. If you have a specific reason to retire one anyway, add `"reapprove": true`
to that row and say so in its `reason`, with the evidence in `evidence` — the
receipt keeps all three, and a reason you cannot evidence is a guess.

Never edit `Workspace/Learnings.md`, `Workspace/Skill-Proposals/`, the upstream
draft sidecar, any skill, or `AGENTS.md` by hand. Never invent a `learning_id`
or a link from an entry to a proposal. Never file an issue or open a pull
request: this workspace does not own the skills these lessons apply to, and the
drafts that route them there are somebody else's decision.

## Approving

Write a JSON list to a scratch file:

```json
[
  {
    "learning_id": "the full value on that row's id line",
    "entry_revision": "the full value on that row's rev line",
    "reason": "one line saying why this lesson is done",
    "evidence": "the receipt id, or what you read in the target that shows it"
  }
]
```

All four fields are required. An approval is bound to the exact bytes it was
reviewed at: if the entry has changed since, the approval is reported as stale
and nothing is removed, which means re-run the dry run and approve again.

Then:

```
ciao learnings-cleanup --apply --approval-file FILE
```

Only the approved rows are removed. Everything else in the file is preserved —
frontmatter except `updated:`, format notes, the `## Promoted / Resolved`
section, the BOM and CRLF line endings included. A receipt under
`.runtime/migration/` records the exact bytes each removal took out, and

```
ciao learnings-cleanup --revert <receipt> --apply
```

puts them back.

**A review that removes nothing is still a review.** Run `--apply` with an empty
approval list (`[]`) when you have read the whole table and the answer is that
nothing should go. That records a receipt for the revision you reviewed, and the
task is done. Generating the table, or waiting for an approval you are not going
to give, is not completion and will not satisfy the check.
