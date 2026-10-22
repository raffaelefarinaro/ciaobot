# Pass 3: re-verify memory

Run `ciao memory-audit --json` daily and act on `aging_state_entries`, `event_shaped_entries` and `superseded_state_candidates` as described below.

## Weekly vault review

On a full pass, do this first:

1. Run `ciao vault review list` and inspect each candidate's evidence with `ciao vault review show <path>`.
2. Run `ciao memory-audit --json --with-vault --vault-root <this workspace's vault>`. It may queue candidates.
3. Report each candidate and its evidence under **What needs you** and leave it undecided; it stays queued for the next attended pass.

Every decision on a candidate (keep, trash, restore, complete, restore-completed, delete) is refused unattended with `unattended_forbidden`, so run none of them. There is no archive operation either, and moving or editing a note to imitate one skips the receipt. Orphan status is only a linking signal, never proof that a note is disposable. The queue's signals are `unverified` (facts unchecked past the type's horizon, the same notes `stale_notes` lists, so re-verify rather than retire), `unlinked`, `possible_duplicate`, `superseded_language` (evidence names the line) and `weak_provenance`.

In an attended follow-up, decide to keep any candidate you also intend to edit before making the edit. Its ID includes the note's content hash, so editing first makes the later decision fail; an edit does not stamp `updated:`. This pass itself leaves all candidates undecided.

## `aging_state_entries`

Region entries whose `[as-of:]` or learned-at stamp has aged past its horizon. Re-verify each against recent chats and project docs, then:

- update the entry with a fresh stamp if the fact changed;
- refresh the stamp if it still holds;
- treat it as a consolidation candidate (pass 2) if it no longer matters.

Report malformed date tags instead of guessing.

## `stale_notes`

These are the same notes the `stale_note` pass of pass 0 planned, oldest first. That pass plans a handful per run and skips any note a recent check already settles, so a short list is the cooldown working, not work you are missing.

For each item:

1. Read the note, then run `ciao note related <relative_path>` and read the related notes' claims about the same subject. A note that disagrees is evidence: cite it (`source_type: "path"`, `source_ref` its vault-relative path, `quoted` the conflicting line).
2. Judge the note's facts against real sources.
3. Settle it through the managed `note verify` operation, never by editing Markdown. The payload rules, the field list and an example are in `note-verify.md` in this folder.
4. Read the reply's `status` and act on it:
   - `applied`: written through a durable receipt. Nothing more to do.
   - `needs_review`: the rule refused to write it unattended and one `note_edit` proposal is queued for the user. Report it under **What needs you** and move on.
   - `unverified`: nothing this pass reached could settle it, and nothing was written. It will be asked again.
   - `conflict`: the note moved since you read it. Re-read it and judge the text that is actually there.
   - `already_checked`: a check for this exact revision is in its cooldown or waiting on a proposal.
   - `failed`: the note could not be used at all.
5. Record the key you settled with `ciao curation-progress --holder <lease.holder> --key <key>`, so the next run resumes instead of re-asking.

When the related notes disagree and the real sources do not settle which is current, pick no side:

1. Record this note `unverified` with the conflict as the reason.
2. File `<path A> says "<x>" but <path B> says "<y>"; which is current? (memory curation)` with `ciao memory-proposal-add --kind review --text-file <file>`, as pass 2 does.
3. Report it under **What needs you**.

When the sources do settle it, the verdict on this note is `update` (or `still_valid`), and the other note gets its own verdict as described next.

A note marked `"retrieved_recently": false` is both stale and unused by recall, which makes it the strongest demotion candidate. Propose it rather than act on it, because disuse alone never justifies removing a durable fact. Never delete it unattended: a retirement is never applied by this pass at all, only proposed.

## A note you judged along the way gets its own verdict

If you checked another note's facts against a real source while checking this one, send that note its own `note verify` payload, with the `revision` that `ciao note related` printed as its `expected_revision` and its own evidence. The check is then recorded and the nightly worklist will not plan that note during the cooldown. Do this only for related notes with `"due": true`: `due: false` means the worklist would not plan it, either because the note is not stale for its type (a journal never is) or because a check already settles it.

Reading a note is not checking it, and citing it as evidence is not checking it either. Send `still_valid` only for a note whose facts you actually checked. These extra verdicts are not worklist items and have no progress key. Re-read the note after every write here too.

## `stale_entry` items

When pass 0 hands you a `stale_entry` item, one call judges that one fact instead of the whole note. The numbered steps for it are in `note-verify.md`. Three things to know before you start:

- The item's `reason` names which kind of staleness it is, and the difference is work. `unverified for 400d against a 90d horizon` is a fact somebody checked and time passed. `nobody has recorded a [verified:] check on this entry` is a fact nobody has ever looked at. The second wants the same evidence and no more, and a `still_valid` on it is refused until the entry carries a stamp to move, so read the note before deciding it needs a source.
- Run `ciao note related` on the entry's note too, and treat a disagreeing note exactly as above.
- Record the key you settled with `ciao curation-progress --holder <lease.holder> --key <key>`, like any other item.

## The `uncovered` count

The `uncovered` count in the audit is a finding too. It is the note text this system cannot read as facts: a paragraph, a table, a quote, a code block, or a bullet carrying one of those. Such a note is unmeasured rather than clean, and nothing in this pass can fix it by stamping it. The fix is writing the facts as one assertion per `-` bullet. If a note you touch has its facts in prose, say so under **What needs you** rather than re-stamping the file around it.

## Writing notes

The rules for writing person and project notes (one fact per bullet, which stamps you may add, when `updated:` moves) live in the `ciao-memory` skill under "Writing entity notes". Follow them whenever this pass edits a note.

## `event_shaped_entries` and `superseded_state_candidates`

Rephrase or merge these under the pass-2 contract.
