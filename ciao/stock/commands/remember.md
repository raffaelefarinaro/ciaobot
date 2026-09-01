---
description: Queue a durable fact for memory review instead of writing memory directly.
argument-hint: <what to remember>
---

# Remember: $ARGUMENTS

Turn `$ARGUMENTS` into one durable, present-tense fact — never "User said X → assistant did Y" — and queue it for review instead of writing memory directly, so a person sees it before it becomes always-loaded context and the outcome is logged. The queue dedupes by exact text; it does **not** reconcile a fact against the entries already there, so accepting an updated preference appends it beside the superseded one. Say so when that is likely, and curate the region in the same pass.

1. **Shape the fact.** One sentence, present tense, stating what IS true from now on. If it is only true from or until a date, append `[as-of: YYYY-MM-DD]` or `[expires: YYYY-MM-DD]`.
2. **Pick the destination** the way the insights extractor tags bullets:
   - cross-project preference, environment fact, or lesson → `memory`
   - who the user is (identity, role, communication style) → `profile`
   - true only within the active project → `project` (needs the project's canonical doc path as payload)
   - a durable fact about a person → `people` (needs the person's name as payload)
   - reusable how-to knowledge spanning projects → `learnings`
   - durable but unsure where it belongs → `review`
3. **File it.** Write the fact verbatim to a scratch file and run `ciao memory-proposal-add --kind <destination> --text-file <fact-file>`, adding `--payload-file <payload-file>` for `project` and `people`. Neither the fact nor the payload travels as a shell argument: both are user-controlled — the payload is a doc path or a person's name — and `$()`, backticks, or quotes in either would be run or mangled. Re-filing an identical fact is a harmless no-op; the queue dedupes by text.
4. **Confirm what actually happened** — read the command's own output, do not assume it queued. It reports one of three things: queued, already in the queue, or *previously dismissed and therefore NOT queued*. The last one means the user rejected this fact before and the queue will not re-offer it; say so and ask whether to file it anyway rather than reporting success. When it did queue, tell them it is waiting for review in `Workspace/Memory-Proposals.md` (the PWA Proposals panel shows it), not silently written into memory.

If the user explicitly wants it live immediately, write it where step 2 routed it — the destination does not change just because the review step is skipped:

- `memory`/`profile` → edit the `ciao:memory` / `ciao:profile` bounded region in the workspace guide, or use `memory_update` (the typed path enforces the cap). Search the region for a superseded entry first and replace it rather than appending; separate entries with `§`.
- `project` → the project's canonical doc.
- `people` → that person's note in `People/`.
- `learnings` → `Workspace/Learnings.md` under `## Active`.

Then dismiss the queued copy with `ciao memory-proposal-dismiss --text-file <fact-file> --promoted` if one exists, reusing the same file from step 3. The fact never becomes a shell argument in either direction — a substring is only safe if you have read it and know it holds no metacharacter at all, and `;`, `&`, `|`, `<`, `>`, `*` and parentheses are as dangerous as quotes.

There is no `ciao memory` command.