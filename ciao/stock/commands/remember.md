---
description: Remember a durable fact or lesson in the workspace.
argument-hint: <what to remember>
---

# Remember: $ARGUMENTS

Before writing, read the engine-packaged durable-memory guide named in the Ciaobot system instructions. It contains the durable-memory policy and entity-note procedure.

Turn `$ARGUMENTS` into one durable, present-tense fact — never "User said X → assistant did Y". The user asked you to remember it, so write it where it belongs now. Git backs the vault up, so a wrong write is undone by reverting it.

1. **Shape the fact.** One sentence, present tense, stating what IS true from now on. If it is only true from or until a date, append `[as-of: YYYY-MM-DD]` or `[expires: YYYY-MM-DD]`. Never include a credential's value; name the credential.
2. **Pick the destination:**
   - cross-project preference, environment fact, or lesson about how the user works → the `ciao:memory` region
   - who the user is (identity, role, communication style) → the `ciao:profile` region
   - true only within the active project → the project's canonical doc
   - a durable fact about a person → that person's note, in the folder its line in the **Categories** section of `VOCABULARY.md` names (`People/` for `person`), with the `type:` that category lists. For another entity category use the category's folder and type; if none covers the thing, put a new-category question on the Unsure list instead of filing it under the nearest one.
   - reusable how-to knowledge spanning projects → **For a reusable lesson**, write the sentence to a scratch file and run `ciao memory learn --text-file <file>`; add `--source <chat-id>` only when the actual source chat id is available, and `--request <id>` only when the actual request id is available. Never invent ids, turns, archive paths or excerpts. The command dedupes repeated sightings and writes the learning directly; report its result.
   - specific to the linked code repository this chat works in → ask the user; a repo's AGENTS.md is theirs to change, so add it to the Unsure list unless they say to edit it
   - durable but you are unsure where it belongs or whether it is true → one line on `Workspace/Unsure.md` (see the guide's "Unsure list and proposal files")
3. **Write it.**
   - `memory`/`profile`: edit the bounded region in the workspace guide with your file-edit tool, or use `ciao memory update` with text you wrote yourself. The region cap is advisory on every path: the write goes through and reports `over_cap`; consolidation is what shrinks a region. Search the region for a superseded entry first and replace it rather than appending; separate entries with `§`.
   - project doc or person note: follow the "Writing entity notes" steps in the durable-memory guide (one fact per bullet, which `[verified:]` stamps you may add, leave `updated:` alone, re-read after every write).
4. **Confirm what actually happened.** Read the command output or re-read the file, and report where the fact now lives.

A reusable workflow lesson also deserves a destination: when a skill this workspace owns should change, or a shared skill needs an upstream issue, write a proposal file as the guide describes and run `ciao proposal-check` on it. Never create or edit a skill file here.
