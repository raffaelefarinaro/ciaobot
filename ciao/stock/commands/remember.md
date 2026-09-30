---
description: Queue a durable fact for memory review instead of writing memory directly.
argument-hint: <what to remember>
---

# Remember: $ARGUMENTS

Turn `$ARGUMENTS` into one durable, present-tense fact — never "User said X → assistant did Y" — and queue it for review instead of writing memory directly, so a person sees it before it becomes always-loaded context and the outcome is logged. The queue dedupes by exact text; it does **not** reconcile a fact against the entries already there, so accepting an updated preference appends it beside the superseded one. Say so when that is likely, but do not edit existing always-loaded memory before this proposal is approved unless the user explicitly asks for an immediate write.

1. **Shape the fact.** One sentence, present tense, stating what IS true from now on. If it is only true from or until a date, append `[as-of: YYYY-MM-DD]` or `[expires: YYYY-MM-DD]`.
2. **Pick the destination** the way the proposals queue tags bullets:
   - cross-project preference, environment fact, or lesson → `memory`
   - who the user is (identity, role, communication style) → `profile`
   - true only within the active project → `project` (needs the project's canonical doc path as payload)
   - a durable fact about a person → `people` (needs the person's name as payload). Another entity category works the same way: pick it from the **Categories** section of the vault's `VOCABULARY.md` and note the person's or thing's name as the payload.
   - reusable how-to knowledge spanning projects → `learnings` (see the provenance rule in step 3 — a lesson usually has no archived turn behind it)
   - durable but unsure where it belongs → `review`
   - durable, but about a thing no category in that **Categories** section covers → `review` as a new-category question. Say which thing and why no listed category fits; do not file it under the nearest one.
3. **File it.** Write the fact verbatim to a scratch file and run `ciao memory-proposal-add --kind <destination> --text-file <fact-file>`, adding `--payload-file <payload-file>` for `project` and `people`. Neither the fact nor the payload travels as a shell argument: both are user-controlled — the payload is a doc path or a person's name — and `$()`, backticks, or quotes in either would be run or mangled. Re-filing an identical fact is a harmless no-op; the queue dedupes by text.

   **A lesson keeps the origin it really has.** A `learnings` fact is usually remembered in a chat that is never archived, so there is no transcript turn to cite. Add `--request <id>` naming this request — the id of the turn, message or request the user gave you — and the accepted line in `Workspace/Learnings.md` cites it as `req:<id>`, which is also what makes filing it twice a no-op rather than a second sighting that inflates the recurrence count. **Never manufacture a turn, a chat id, an archive path or an `excerpt` to fill the citation**: a citation to a conversation that never happened is worse than a request id, because a later settlement trusts it. If the chat *was* archived and you know its id, pass `--source <chat-id>` as well; both may be present, and `--request` is only accepted for `--kind learnings`, because that is the one accept that records it. `--request` takes plain letters, digits, dot, dash and underscore, up to 64 characters.
4. **Confirm what actually happened** — read the command's own output, do not assume it queued. It reports queued, already in the queue, *previously dismissed and therefore NOT queued*, or already promoted. A dismissed fact was rejected before and the queue will not re-offer it; ask whether to file it again, and if they say yes, rerun with `--allow-dismissed`. A promoted fact should already be live in its destination. When it did queue, tell them it is waiting for review in `Workspace/Memory-Proposals.md` (the PWA Proposals panel shows it), not silently written into memory.

If the user explicitly wants it live immediately, write it where step 2 routed it — the destination does not change just because the review step is skipped:

- `memory`/`profile` → edit the `ciao:memory` / `ciao:profile` bounded region in the workspace guide with your file-edit tool. The region cap is advisory on every path — the write goes through and reports `over_cap`; nothing refuses it and nothing shrinks the region, which is what consolidation is for. Search the region for a superseded entry first and replace it rather than appending; separate entries with `§`.
- `project` → the project's canonical doc.
- `people` → that person's note, in the folder its line in the **Categories** section names (`People/` for `person`). Give the note the `type:` that category lists, or `ciao vault-lint` reports it.
- `learnings` → `Workspace/Learnings.md` under `## Active`, carrying the same `--request <id>` the queued copy used, so the immediate write and the queue agree about where the sighting came from. A reusable workflow lesson also deserves a destination: a skill this workspace owns under `skills/` gets a proposal from `ciao skill-proposal-add NAME --input-file FILE`, and a lesson no skill covers gets a `[review]` draft from `ciao skill-draft-add --input-file FILE`. Read the `ciao-memory` skill for the routing contract; never create or edit a skill file here.

Then dismiss the queued copy with `ciao memory-proposal-dismiss --text-file <fact-file> --promoted` if one exists, reusing the same file from step 3. The fact never becomes a shell argument in either direction — a substring is only safe if you have read it and know it holds no metacharacter at all, and `;`, `&`, `|`, `<`, `>`, `*` and parentheses are as dangerous as quotes.

`ciao memory update` also writes a region directly and skips the review queue, but it takes the fact as a `--entry` shell argument, which the rule above forbids for user-supplied text — so do not use it for a fact from `$ARGUMENTS`, and never as a substitute for step 3.
