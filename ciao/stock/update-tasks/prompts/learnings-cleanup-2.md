# Review this workspace's `Learnings.md`

Run `ciao learnings-cleanup` in the workspace this task was offered for. It
prints one row per Active entry and writes nothing.

Your job is to read every entry, keep the lessons that still make sense, and
retire the rest. The table tells you what the bookkeeping knows; it does not
tell you whether a lesson is still true. Most legacy entries say `KEEP` only
because no skill proposal ever linked a finding to them, so you have to judge
them yourself.

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

`CONFLICT` is a line this code cannot read; it is never removed and never
repaired. Say so and move on.

## Judging each entry

Read the entry itself in `Workspace/Learnings.md`, not just its key. Then check
it against the present, and decide.

**Retire it** when you can show one of these:

- **Its subject is gone.** The file, module, provider, command, package,
  dependency or workflow it is about no longer exists. Check: search the
  repository or install it names, its dependency manifests, and the CLI's
  `--help`.
- **It is superseded.** The thing it works around has been fixed, replaced,
  or turned into a supported feature, or it is pinned to a version long since
  left behind. Check the installed version, or the code that now does it.
- **It already lives somewhere durable.** The same lesson is in a skill, in
  bounded memory (`AGENTS.md`), or in a vault note. Read the target and confirm
  it says the same thing.
- **It duplicates another entry.** Keep the clearest or most recent one and
  retire the others, naming the one you kept.
- **It is a one-off.** A note about a single past event, with nothing in it a
  future run could reuse.

**Keep it** when it is still true and still useful, when you cannot check it
from here, or when you are unsure. A lesson that might matter is cheaper to
keep than to lose, and the next review can look at it again.

**Leave these alone whatever you think of them:** a row whose reason is
`pending` (a finding is open or a chat is working on it), `reopened`,
`stock_waiting_upstream`, or `held_back`. Somebody else is deciding those
right now.

Every retirement needs evidence you checked yourself in this run: the path that
is missing, the version that is installed, the skill section that already holds
the lesson. "Probably outdated" is not evidence.

## What not to touch

Never edit `Workspace/Learnings.md`, `Workspace/Skill-Proposals/`, the upstream
draft sidecar, any skill, or `AGENTS.md` by hand. Never invent a `learning_id`
or a link from an entry to a proposal. Never file an issue or open a pull
request: this workspace does not own the skills these lessons apply to, and the
drafts that route them there are somebody else's decision. If a lesson that
should be kept belongs in a skill or in `AGENTS.md`, mention it in your
summary; do not move it.

## Approving

Write a JSON list to a scratch file, with one object for every entry you retire:

```json
[
  {
    "learning_id": "the full value on that row's id line",
    "entry_revision": "the full value on that row's rev line",
    "reason": "one line saying why this lesson is done",
    "evidence": "what you checked, e.g. ciao/providers/pi.py is gone and nothing imports PiProvider",
    "reapprove": true
  }
]
```

`learning_id`, `entry_revision`, `reason` and `evidence` are required.
`reapprove` is required on any row the table did not mark `REMOVE`; it says a
person, not the bookkeeping, decided this one, and the receipt keeps it with the
reason and the evidence. A `REMOVE` row needs no `reapprove`, but check its
`EVIDENCE` like any other: read the skill it names and confirm the lesson is in
it.

Before applying, give the user the list: each entry you are retiring with its
reason and evidence, and a count of what you are keeping. Then apply:

```
ciao learnings-cleanup --apply --approval-file FILE
```

Only the approved rows are removed. Everything else in the file is preserved —
frontmatter except `updated:`, format notes, the `## Promoted / Resolved`
section, the BOM and CRLF line endings included. If an entry changed since the
dry run, its approval is reported as stale and it is not removed: re-run the dry
run and approve again. A receipt under `.runtime/migration/` records the exact
bytes each removal took out, and

```
ciao learnings-cleanup --revert <receipt> --apply
```

puts them back.

**A review that removes nothing is still a review.** If you have read every
entry and they all still hold, run `--apply` with an empty approval list (`[]`).
That records a receipt for the revision you reviewed, and the task is done.
Generating the table, or waiting for an approval you are not going to give, is
not completion and will not satisfy the check.
