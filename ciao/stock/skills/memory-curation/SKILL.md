---
name: memory-curation
description: Nightly Workspace care — process memory proposals and learnings every day, and run overdue weekly vault indexing, stale-note review, and safe workspace hygiene. Used by the system Workspace care schedule; also loadable in an attended chat when the user asks to curate, consolidate, or clean up memory.
---

# Memory curation

Care for this workspace's durable memory and vault. The `<ciao-context>` block names the vault as `vault=<path>` — write under that path and nowhere else. Use one memory agent for vault writes, and run every mutating pass sequentially. Pass 0 computes which of the passes below actually have work; run those in order and skip the rest without checking them yourself.

## Ground rules

- **You are an unattended run: defer, never ask, never route around the absent reviewer.** Do not ask questions or wait for approval, and do not find another path to an approval-requiring action. Finish the safe work, then report every deferred item in the final reply under **What needs you**. The full deferred list: promoting a NEW fact into a bounded region, trashing/restoring/permanently deleting a vault note, writing another workspace, creating or moving an automation into another workspace, and public or destructive git actions.
- **Do not promote new facts into the bounded `ciao:memory` or `ciao:profile` regions.** A promotion rewrites what every session of this workspace loads, and an unattended run has no reviewer. Leave cross-project facts in the proposals queue for the user to promote. Consolidating entries ALREADY in a region is different and allowed under the undo-log rule below.
- **Nothing is dropped silently.** Before removing or replacing any region entry, copy its original text into `<vault>/Workspace/Memory-Consolidations.md` under a `## YYYY-MM-DD` heading naming the region — create the file if missing. It is the undo log; the user can restore any line.
- **The region cap is advisory.** Nothing refuses a write at edit time; an over-cap write reports `over_cap` and consolidation afterwards is what bounds the region. Never treat a full region as a reason to refuse or drop a durable fact.
- **Queue and log stay separate.** `Workspace/Memory-Proposals.md` holds pending proposal bullets and nothing else — never append pass reports, notes, or prose there. Pass reports go to `Workspace/Curation-Log.md` only.
- Report only what was processed. If nothing needs processing, reply with a one-line no-op and stop.

## 0. Start the run

**Do this before reading anything else in the vault.** Run:

```
ciao curation-begin --json
```

It does three jobs, and each replaces work you would otherwise do by hand:

1. **It serializes the run.** One curation run per vault at a time, and archive-time auto-apply stands down while the lease is held — so a chat being archived cannot append to a region you are halfway through rewriting. **Exit 75 means another run holds the lease: stop immediately and say nothing happened.** Do not curate anyway.
2. **It computes the worklist.** Pending proposals, region usage, aging entries, learnings, the weekly marker, log sizes and skill proposals are all mechanical checks; code has already run them. **`"empty": true` means there is genuinely nothing to do — reply with the one-line no-op and stop.** The lease is already released in that case. Do not open a single file to double-check.
3. **It applies the run budget.** Work only the passes under `planned`, and only the `keys` listed there. Anything under `deferred` is next run's work, not yours — it is already recorded and will not be lost.

**Keep the `lease.holder` string from that JSON.** Every follow-up command of this run must carry it as `--holder`; both require it and refuse to act once the lease is no longer yours. That is what stops a run that ran past its lease from writing the vault underneath the run that replaced it, so do not invent a holder and do not omit the flag.

As you finish items, record them so a short run resumes rather than restarts:

```
ciao curation-progress --holder <lease.holder> --key <key> --key <key>
```

Close every run, including a failed one:

```
ciao curation-end --holder <lease.holder> --status ok|failed --planned <n> --completed <n>
```

`curation-end` releases the lease, records planned/completed/deferred counts so a quiet night is distinguishable from skipped or failed work, and stamps `last_full_pass` itself — but only when both weekly checks (pass 6) were recorded done, the status is `ok`, and the lease is still yours. You never write that marker by hand. Exit 75 from either command means the lease was lost: stop curating and report the run as failed.

## Choose the pass

`Workspace/Curation-Log.md` carries `last_full_pass: YYYY-MM-DD` in its YAML frontmatter. Create the file/frontmatter if needed without discarding an existing body. Run the daily passes every night. Also run every pass marked **weekly** when the marker is absent or more than seven days old; this overdue rule matters more than the weekday, because a powered-off server must not permanently miss its weekly care. `curation-begin` has already evaluated this rule and reports it as `weekly_due`; trust it rather than re-reading the date. Update `last_full_pass` only after both the index refresh and final audit complete reliably — `curation-end` enforces that. A failed or unreliable full pass remains due for the next run.

## 1. Process the proposals queue

Start from the recent archived chats: prefer their existing session-insights sections, and sweep any archive since the last run that has none (its extraction failed or predates insights) — those are exactly the chats whose facts never entered the queue. Archive time already auto-applies confidently-tagged facts, so what remains queued is unsure or failed. List pending items with `ciao memory-proposals` and route each by its bracketed kind:

- `[memory]` / `[profile]` name bounded regions and **stay queued** for the user.
- `[project <doc-path>]` folds into that canonical doc.
- `[people <Name>]` updates that person note (merge, never overwrite). File people in this workspace's `People/`; a contact you deal with here belongs here, even if first met in another workspace's chat.
- `[learnings]` files into `Workspace/Learnings.md` (see pass 4).
- `[review]` has no destination yet — decide what it is first.

File a fact into its destination first, then dismiss with `ciao memory-proposal-dismiss --text-file <file> --promoted` (the flag records a promotion; a plain dismissal means you decided against the fact — the reverse order can lose it). Dismiss already-covered facts without `--promoted`. Never remove a bullet by editing the file. Every decision this command makes is recorded in the review page's History tab, so a promoted fact shows there as accepted by the agent, not silently dismissed.

Pass the text via `--text-file`, not as an argument: a proposal is arbitrary user prose, and one containing `$(...)`, backticks or quotes would be run or mangled by the shell on its way to the command. A positional substring is still accepted for a value you have read and know is plain.

When you discover a bounded-region fact yourself — e.g. reading a transcript whose chat never grew a session-insights section — write the fact verbatim to a scratch file and file it with `ciao memory-proposal-add --kind memory --source <chat id> --text-file <fact-file>`. The source is the chat's plain identifier, never its quoted title, and the fact never travels as a shell argument: `$()`, backticks, or quotes in either would run or mangle in a shell. When no chat id is at hand, omit `--source`. A fact that exists only as report prose has no review path and gets re-derived every night; re-filing a fact an earlier run already queued is a harmless no-op because the queue dedupes by text.

## 2. Consolidate bounded regions

Check usage with `memory_status`. When a region is at or above ~85% of its cap (or over it), consolidate that region now — this run may edit regions for consolidation only:

- Merge duplicate or near-duplicate entries into one present-tense rule.
- Replace a superseded entry with its current state.
- Drop entries whose `[expires: YYYY-MM-DD]` date has passed.
- Tighten verbose wording without losing any durable fact.
- Move project-scoped entries out to their owning project's canonical doc (move, never delete).
- Preserve or add the trailing learned-at stamp `[YYYY-MM-DD]` on entries you rewrite (today's date for a merged entry).

When a removal needs judgment you cannot confidently make, do not remove it; queue a yes/no question instead by appending `- [review] Keep "<entry text>" in ciao:<region>? Proposed action: drop because <reason>. (memory curation)` to `Workspace/Memory-Proposals.md`. Flatten the entry onto that one line (newline → "; ") so the queue stays line-parseable, and skip appending when an unanswered question about the same entry is already queued. The queue itself cannot apply a drop: the user dismisses the question (`ciao memory-proposal-dismiss --text-file <file>`, since the question template always contains double quotes) to KEEP the entry, or asks an attended chat to drop it. The Proposals panel's accept button is intentionally disabled for review rows.

Never drop a durable fact merely to fit a cap. If a region remains over cap because every entry is genuinely high-signal, say so and give the user their options: leave the region over its advisory cap, or ask any attended chat to consolidate further.

## 3. Re-verify memory

Run `ciao memory-audit --json` daily and act on `aging_state_entries`, `event_shaped_entries`, and `superseded_state_candidates` under the pass-2 contract.

For the **weekly** pass, first run the scoped `vault_review` tool (or the equivalent review endpoint) and inspect its evidence, then run `ciao memory-audit --json --with-vault --vault-root <this workspace's vault>`. It may queue candidates, but unattended care must never trash or permanently delete a note. Keep and archiving non-destructively are allowed; trash and permanent deletion require an attended action. Leaving a candidate undecided is also fine — it stays queued for the next attended pass. Orphan status is only a linking signal, never proof that a note is disposable.

Act on these sections:

- **`aging_state_entries`** — region entries whose `[as-of:]` or learned-at stamp has aged past its horizon. Re-verify each against recent chats and project docs: update the entry (fresh stamp) if the fact changed, refresh the stamp if it still holds, or treat it as a consolidation candidate (pass 2) if it no longer matters. Report malformed date tags instead of guessing.
- **`stale_notes`** — open each note and re-verify its facts. If a fact changed, correct the note; if it still holds, set frontmatter `updated:` to today (YYYY-MM-DD) — the review workflow's keep disposition does this for you; if it no longer matters, fold anything still useful into MEMORY.md, a person note, or the relevant project doc, then archive it non-destructively or leave it queued for attended review. Never delete it unattended. A note marked `"retrieved_recently": false` is both stale and unused by recall — the strongest demotion candidate — but disuse alone never justifies removing a durable fact.
- **`event_shaped_entries` / `superseded_state_candidates`** — rephrase or merge under the pass-2 contract.

## 4. Maintain learnings

`Workspace/Learnings.md` entries are structured: `- [key] [first-seen → last-seen] (xN) statement — sources: chat-a, chat-b`. The engine increments the count when the same statement recurs; your job is judgment:

- **Promote** an entry at x3 or more into canonical guidance (the AGENTS.md body or the relevant skill/doc), citing its sources, then move it under `## Promoted / Resolved` with the destination named.
- **Merge** entries that are semantically the same learning written differently: keep one, sum the counts, union the sources.
- **Prune** x1 entries older than 30 days with no reuse value. Move anything pruned or resolved to `Workspace/Learnings-Archive.md` (create with `search: false` frontmatter) rather than deleting.

## 5. Keep recall sharp

When you create or update People and project notes, add an `aliases:` frontmatter list with the relationship terms and paraphrases someone would actually search for ("brother-in-law", "hourly rate", a nickname). Recall is lexical; aliases are what make "how much do I charge per hour" find the consulting rate note.

## 6. Weekly workspace hygiene

On a full pass:

1. Run `ciao vault-index --write`. If it fails, report the failure and do not claim health. On success record it: `ciao curation-progress --holder <lease.holder> --key hygiene:vault-index`.
2. Run `ciao os-audit --json --scope workspace`. Exit 1 means reliable findings and is safe to continue; exit 2 means unreliable evidence, so report scan errors, make no audit-derived repair, and leave the full pass due.
3. When reliable, apply only low-risk unambiguous repairs: dead links, obvious MEMORY.md path drift, and a non-canonical `type:` whose exact canonical target is already named by VOCABULARY.md. Do not rewrite instruction conflicts, skills, orphaned or duplicate notes, expiration tags, schedules, or vocabulary proposals.
4. Re-run the scoped audit. Record initial/final counts, safe repairs, unresolved findings, and vocabulary proposals in the technical log. When that verification is reliable, record it: `ciao curation-progress --holder <lease.holder> --key hygiene:os-audit`.

Those two keys are what lets `curation-end` stamp `last_full_pass`. Record neither and the weekly pass stays due, which is the correct outcome for a run whose evidence was unreliable — do not write the marker by hand to make the page look green.

Install-wide runtime failures remain visible through startup triage and Settings → Automation; pending upgrade work appears in housekeeping. Do not duplicate those global checks in every workspace.

## 7. Review the workspace guide body

On a full pass, review the guide file's body (AGENTS.md), outside the fenced `ciao:memory` and `ciao:profile` regions, for the things `memory-audit` cannot judge on shape alone. This is the workspace's standing contract with every session, so a wrong claim there is asserted before the operator has said a word.

- **Misplacement.** Apply the three tests before any entry that reads like a fact or an instruction: Is it a thing or an instruction? An entity (a person, a system, an automation, a tool) belongs on a vault page, not in the guide body. Does a vault page already say it? Search before keeping it. Where would it actually fire? A gotcha that only matters inside one workflow belongs in that workflow's skill, not in the guide. Move entity and gotcha facts out of the body to their vault page or skill; keep only standing instructions that must load every turn.
- **State vs event.** A current value (price, status, owner, deadline, role, config setting, file location) is state: replace it in place with a fresh date, never append a second line. A thing that happened (a decision, a delivery, a correction) is an event: move it to the journal, `log.md`, or `Workspace/Learnings.md`, never keep it as an ever-loaded instruction.
- **Duplicates the core prompt or a tool's own docs.** When a guide section only restates something the Ciaobot core prompt or an MCP/tool's own instructions already carry, remove it rather than keep it for redundancy. The guide is for the workspace-specific delta. Two recurring shapes: a section that restates the core operating contract ("use Ciaobot's typed tools, do not create provider-native recurring automations"), and an MCP section that argues behavior already guaranteed by the connector's own system prompt (connectors can flap, OAuth cannot re-consent unattended). Verify the other source actually carries it before deleting (the core prompt's defuddle rule and the `web-research` skill both cover URL reading; an agent's `.claude/agents/*.md` are real files, not symlinks like skills).
- **Promote.** A learning that recurs (x3+) in `Workspace/Learnings.md` belongs here as a standing instruction, citing its sources.
- **Drift and bloat.** Cut stale paths, superseded rules, and anything a more specific file (a skill, a project doc, the daily journal) already covers more precisely. Tighten verbose wording without losing a durable fact. Flag, do not silently rewrite, anything whose correct home is genuinely ambiguous: append a `[review]` question to `Workspace/Memory-Proposals.md` exactly as pass 2 does, so the operator decides.
- **Record it.** Every body edit is a guide change, so the guide lock applies: hold it before writing and release after, exactly like a region write. When a removal needs judgment you cannot make confidently, queue it for the operator rather than dropping it.

Record the pass only when the review was actually completed: `ciao curation-progress --holder <lease.holder> --key guide:guide-body`. This key is not one of the two required hygiene keys, so an over-budget run that never reached the guide must not report it as done.

## 8. Rotate the logs

When `Workspace/Curation-Log.md` or `Workspace/Weekly-Review-Log.md` exceeds ~64KB, move its body to a dated archive (`Curation-Log-YYYY-MM.md`) with `search: false` frontmatter and start the live file fresh with a pointer to the archive.

## 9. Skill proposals

Review `Workspace/Skill-Proposals/`. A proposal already implemented, or one you decide is not worth building, is a resolved decision: remove it with `ciao skill-proposal-remove <name>` (or `python3 -m ciao.cli skill-proposal-remove <name>`) naming the proposal file or a unique substring. Only remove a proposal after its change is actually in place or decided against. Leave proposals that belong in a bounded region queued.

## 10. Report

Two audiences, two registers.

**The log** (`Workspace/Curation-Log.md`): append the full technical pass report — processed counts, changed files, per touched region chars before → after with what was merged/dropped/moved, and the undo-log path.

**The chat reply** (what the user actually reads): plain language, no jargon, no file paths unless the user must open one, no internal terms like "bounded region", "proposal kind", or "undo log" without saying what they mean. Structure it as:

1. **What I did** — one short line per action, in everyday words ("Merged two duplicate notes about your insights model", "Retired 1 expired fact", not "consolidated ciao:memory 2431→2205 chars").
2. **What needs you** — every cross-project fact waiting for approval, one numbered line each, quoted in full, ending with how to act ("reply with the numbers to remember, or 'skip the rest'"). If a question is queued ("should I drop X?"), ask it as a plain yes/no.
3. **Nothing else.** If a section is empty, leave it out. If nothing at all happened, the whole reply is one line ("Nothing new to file today — memory is tidy.").

The test for the reply: someone who has never read the docs should understand every sentence and know exactly what, if anything, they are being asked to do.
