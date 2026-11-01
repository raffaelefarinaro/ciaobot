# `ciao note verify`: payload rules and steps

Verify a note through the managed operation, never by editing the note: a hand edit leaves no receipt, no check state and no record of who decided. The verdict travels as a JSON file because every field is note prose or a citation, and prose is never safe as a shell argument:

```
ciao note verify --payload-file <file>
```

The file must be inside this workspace and under the size cap. Put long evidence in the vault and cite its path instead of pasting it.

## Fields

- `relative_path`: the note's vault-relative path.
- `expected_revision`: the lowercase hex SHA-256 of the note's full text as UTF-8, unmodified. Copy it from the `revision <hex>` the `stale_note` item (or `ciao note related`) prints instead of hashing the note yourself. A revision that does not match comes back `conflict` and verifies nothing.
- `outcome`: `still_valid`, `update`, `retire` or `unverified`.
- `coverage`: `complete` or `partial`. A `still_valid` needs `complete` plus a citation naming the note. An applied `still_valid` re-stamps the note's `updated:` to today.
- `evidence`: a list of citations, each with `source_type`, `source_ref`, `quoted` and `supports`.
- `before` / `after`: the exact text being replaced and its replacement. An `update` carries them, and its `after` carries a fresh `updated:` because the service does not re-stamp an update. An `update` is applied only when every evidence row is a citation someone could re-open.
- `reason`: one line saying why.

## Example: a whole-note `still_valid`

```json
{
  "relative_path": "People/Rae.md",
  "expected_revision": "9f2c6d1e0a7b4c35d8e1f0a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6",
  "outcome": "still_valid",
  "coverage": "complete",
  "evidence": [
    {
      "source_type": "path",
      "source_ref": "People/Rae.md",
      "quoted": "Rae leads the platform team.",
      "supports": "People/Rae.md"
    }
  ],
  "reason": "Checked against the March org chart."
}
```

## Judging one entry instead of the whole note

A note's `updated:` is one date for every fact in it, so a whole-note verdict would claim the address you just re-checked and the landlord's name from 2019. A `stale_entry` item exists to judge one fact. Follow these steps:

1. Read the item's `reason`. It prints `<note> at revision <hex>, entry identity <hex>, entry fingerprint <hex> at characters <start>-<end>`.
2. Copy the revision into `expected_revision`.
3. Copy the fingerprint into `entry_fingerprint`.
4. Add `entry` with that identity, all 64 characters. The item's label shows only the first twelve and the worklist key is a digest of the identity, so neither can be turned back into one, and anything but a full digest is refused before the note is opened.
5. Copy every value whole. The fingerprint is compared as a string, so a truncated one is a different value and comes back `conflict`.
6. Make `before` and `after` the entry's text, not the note's. A whole-note replacement there would rewrite every other fact in the file to correct one.
7. Call `ciao note verify --payload-file <file>`. The reply gains `scope: "entry"`. A `needs_review` entry verdict queues a `replace_entry`, `restamp_entry` or `retire_entry` proposal, whose accept changes or removes exactly that span and is undone from History like any other note write.
8. Re-read the note. The revision in the plan is the note as it stood when the worklist was computed, so a second `stale_entry` of the same note, or a `stale_note` item for a note you just wrote an entry of, comes back `conflict`. One bullet per note is the honest yield; the rest wait for a plan built against the note as it now is.

A `still_valid` on an entry with no `[verified:]` stamp adds one, on the same evidence a re-stamp needs (complete coverage and a citation naming the note); without that evidence it is recorded `unverified` and nothing is written. An entry whose stamp is unreadable (`[verified 2024-01-05]`, an impossible day) comes back `needs_review` with a `restamp_entry` repair.

```json
{
  "relative_path": "People/Rae.md",
  "expected_revision": "9f2c6d1e0a7b4c35d8e1f0a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6",
  "entry": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "entry_fingerprint": "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
  "outcome": "update",
  "coverage": "complete",
  "evidence": [
    {
      "source_type": "path",
      "source_ref": "Projects/Atlas.md",
      "quoted": "Rae moved to the data team in May.",
      "supports": "People/Rae.md"
    }
  ],
  "before": "- Rae leads the platform team. [verified: 2025-01-10]",
  "after": "- Rae leads the data team.",
  "reason": "Atlas project doc says she moved in May."
}
```

## Replies and refusals

- `applied`: written through a durable receipt.
- `needs_review`: the rule refused to write it unattended and one typed proposal is queued for the user. Retirement, an edit whose citations, all about this note, are incomplete, or a `still_valid` on a note with no `updated:` to stamp all come back this way. An unattended run reports it under **What needs you** and does not route around it.
- `already_checked`: a check for this exact revision is in its cooldown or waiting on a proposal.
- `failed`: the note could not be used at all.
- `unverified`: nothing was written, because no source was reached, coverage was partial, the input was too large to judge, the replacement equals the current text, or no evidence row names the note.
- `conflict`: nothing was written, because the note moved since you read it, or `before` is not the note's (or the entry's) exact current text. Re-read and judge the text that is there.
- `payload_required` / `payload_invalid` / `payload_too_large`: the payload was missing, outside the workspace, malformed, over the cap, or named an `outcome` outside the four above.
- `workspace_forbidden`: the payload named another workspace. Drop the `workspace` field or make it match.
