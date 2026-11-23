---
description: Remember a durable fact or lesson in the workspace.
argument-hint: <what to remember>
---

# Remember: $ARGUMENTS

Turn `$ARGUMENTS` into one durable, present-tense fact — never "User said X → assistant did Y". Queue ordinary facts for review; a reusable lesson is recorded directly in `Workspace/Learnings.md` with `ciao memory learn`.

1. **Shape the fact.** One sentence, present tense, stating what IS true from now on. If it is only true from or until a date, append `[as-of: YYYY-MM-DD]` or `[expires: YYYY-MM-DD]`.
2. **Pick the destination** the way the proposals queue tags bullets:
   - cross-project preference, environment fact, or lesson → `memory`
   - who the user is (identity, role, communication style) → `profile`
   - true only within the active project → `project` (needs the project's canonical doc path as payload)
   - a durable fact about a person → `people` (needs the person's name as payload). For another entity category, `--kind` accepts only `memory`, `profile`, `project`, `people`, `repo`, `learnings` and `review`, so a category name is refused: use `people` when the thing is a person, otherwise `review`, naming the thing and its category in the fact.
   - specific to the linked code repository this chat works in → `repo` (needs the repository's absolute path as payload)
   - reusable how-to knowledge spanning projects → record it directly with `ciao memory learn` in step 3
   - durable but unsure where it belongs → `review`
   - durable, but about a thing no category in that **Categories** section covers → `review` as a new-category question. Say which thing and why no listed category fits; do not file it under the nearest one.
3. **File it.** For `memory`, `profile`, `project`, `people`, `repo` or `review`, write the fact verbatim to a scratch file and run `ciao memory-proposal-add --kind <destination> --text-file <fact-file>`, adding `--payload-file <payload-file>` for `project`, `people` and `repo`. Neither the fact nor the payload travels as a shell argument: both are user-controlled — the payload is a doc path or a person's name — and `$()`, backticks, or quotes in either would be run or mangled.

   **For a reusable lesson**, write the sentence to a scratch file and run `ciao memory learn --text-file <file>`. Add `--source <chat-id>` only when the actual source chat id is available; add `--request <id>` only when the actual request id is available. Never invent ids, turns, archive paths or excerpts. The command dedupes repeated sightings and writes the learning directly; report its result.
4. **Confirm what actually happened** — read the command's own output and report whether the fact was queued or the lesson was recorded.

If the user explicitly wants it live immediately, write it where step 2 routed it — the destination does not change just because the review step is skipped:

- `memory`/`profile` → edit the `ciao:memory` / `ciao:profile` bounded region in the workspace guide with your file-edit tool. The region cap is advisory on every path — the write goes through and reports `over_cap`; nothing refuses it and nothing shrinks the region, which is what consolidation is for. Search the region for a superseded entry first and replace it rather than appending; separate entries with `§`.
- `project` → the project's canonical doc.
- `people` → that person's note, in the folder its line in the **Categories** section names (`People/` for `person`). Give the note the `type:` that category lists, or `ciao vault-lint` reports it.
- `learnings` → `ciao memory learn`. A reusable workflow lesson also deserves a destination: a skill this workspace owns under `skills/` gets a proposal from `ciao skill-proposal-add NAME --input-file FILE`, and a lesson no skill covers gets a `[review]` draft from `ciao skill-draft-add --input-file FILE`. Read the `ciao-memory` skill for the routing contract; never create or edit a skill file here.

**When you write into a `project` or `people` note, follow the "Writing entity notes" steps in the `ciao-memory` skill.** They cover one fact per bullet, which `[verified:]` stamps you may add, leaving `updated:` alone, and re-reading the note after every write.

Then dismiss the queued copy with `ciao memory-proposal-dismiss --text-file <fact-file> --promoted` if one exists, reusing the same file from step 3. The fact never becomes a shell argument in either direction — a substring is only safe if you have read it and know it holds no metacharacter at all, and `;`, `&`, `|`, `<`, `>`, `*` and parentheses are as dangerous as quotes.

`ciao memory update` also writes a region directly and skips the review queue, but it takes the fact as a `--entry` shell argument, which the rule above forbids for user-supplied text — so do not use it for a fact from `$ARGUMENTS`, and never as a substitute for step 3.
