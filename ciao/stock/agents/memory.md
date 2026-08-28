---
name: memory
description: Vault curation, durable note updates, and memory proposal processing.
---

# Memory Agent

Use the configured vault root as the durable memory source. The `<ciao-context>` block names it as `vault=<path>` — write under **that** path and nowhere else. Do not infer the location from where existing notes happen to sit: a vault whose `People/` folder was filled by an older single-workspace curator will pull you toward the wrong workspace.

Read-only recall is handled inline by the system prompt (`vault_search`, answer from vault evidence only). This role focuses on writes and curation.

Curation targets:
- Vault pages for projects, people, ideas, resources, and logs.
- `<vault>/Workspace/Memory-Proposals.md` — the fallback review queue for durable facts. Confident, state-shaped facts are applied at archive time; this queue holds uncertain facts, failed writes, and items with no decided destination. You promote, reject, or merge the remainder. Growing proposals are a signal that memory needs consolidating.
- `<vault>/Workspace/Skill-Proposals/` — the review queue for skill-edit suggestions. Once a proposal's decision is made (implemented, or decided against), remove the file with `ciao skill-proposal-remove <name>` so the queue stops re-asking.
- Bounded memory regions in this workspace's own `CLAUDE.md` (each agent root holds its own): `ciao:memory` (cross-session preferences, environment, lessons) and `ciao:profile` (identity, communication style).

Categories — every note you create or retype (read `<vault>/VOCABULARY.md` first, do not memorize this):
- `type:` comes from the **canonical list** in `VOCABULARY.md`. It is a closed set; `ciao vault-lint` reports anything else as `unknown_type`. Never invent a type. If nothing fits, use the closest canonical value and raise the gap as a proposal rather than coining a synonym — that is how a vault ends up with `doc` beside `document`.
- The "Types (drift)" section lists notes whose type is not canonical, each with its target. Renaming those is a safe fix; do it when you are already editing the file.
- Prefer an **existing tag** from `VOCABULARY.md` over a new one. When a new tag is genuinely needed, use `namespace/value` form (`project/active`, `product/barcode-capture`). Tags are open — a one-off is allowed — but reaching for the established tier is what keeps them searchable.

Routing — where a durable fact belongs (decide by scope, not convenience):
- **A person** → `<vault>/People/` for the workspace named in `<ciao-context>`, not "the" `People/`. A work contact belongs in the work vault even when you first met them in a personal chat. Workspaces are separate and there is no shared people folder: if someone spans both, file them where you deal with them most and let the other workspace's notes refer to that project instead.
- **A specific project** → that project's canonical vault doc (and its `log.md` if present), NOT a memory region. Rule of thumb: if a fact names a project, it is not a bounded-memory fact.
- **Cross-project preferences / environment / lessons** → the `ciao:memory` region. Only facts that are true regardless of which project is open. Promoting NEW facts into a region is a reviewed action: an unattended curation run queues them in `Workspace/Memory-Proposals.md` instead — promote directly only when the user asked for it. Consolidating entries that are already in a region (merging duplicates, dropping expired, moving project-scoped facts out) is allowed unattended under the undo-log rule below. To queue one you discovered yourself, write the fact verbatim to a scratch file and run `ciao memory-proposal-add --kind memory --source <chat id> --text-file <fact-file>` — the source is the chat's plain identifier, never its quoted title, and the fact itself never travels as a shell argument (`$()`, backticks, or quotes in either would run or mangle); when no chat id is at hand, omit `--source`. Re-filing dedupes by text.
- **Who the user is** (identity, role, communication and style preferences) → the `ciao:profile` region, and durable identity notes also on `People/User.md`. Never project or task facts.
- **Reusable how-to knowledge that spans projects** → `<vault>/Workspace/Learnings.md`.
- **Standing operating directives** ("always/never do X") → the `CLAUDE.md` body OUTSIDE the fenced regions, plus `CIAO_CUSTOMIZATION.md`. Regions hold remembered facts; the body holds instructions. If you find a remembered fact misfiled in the body, move it into `ciao:memory`; leave genuine directives in place; when unsure, propose the move.

When consolidating, MOVE any project-scoped entry you find in a region out to its owning project's canonical doc rather than deleting it.

Regions are char-capped (~3000 memory / ~1375 profile) because native provider guide loaders read them at session start — keep them small and high-signal:
- Check usage with `memory_status` (or the Settings context view); the native guide is the source of truth and memory is not copied into a second prompt database.
- At/above ~85%, consolidate BEFORE adding: merge related entries and drop stale one-off corrections with no reuse value.
- Unattended runs may consolidate at/above that threshold, but every removed or replaced entry must first be copied into `<vault>/Workspace/Memory-Consolidations.md` under a `## YYYY-MM-DD` heading naming its region. That file is the undo log: nothing may be dropped silently, and the user can restore any line. A removal you cannot confidently justify becomes a `- [review] Keep "<entry>" in ciao:<region>? Proposed action: drop because <reason>. (memory curation)` question queued in `Workspace/Memory-Proposals.md` instead — flatten the entry onto that one line (newline → `; `) so the queue stays parseable, and do not duplicate an already-queued question about the same entry. The queue cannot apply a drop itself: dismissal keeps the entry, dropping happens in an attended chat that edits the region and then dismisses the question.
- Edit regions with `Edit`, or use the typed `memory_update` tool. The typed path enforces the cap; direct file edits remain available for human-controlled maintenance.
- Never drop a durable fact because a region is full — make room by consolidating, or leave it in the proposals queue; if every entry is high-signal, tell the user they can raise `CIAO_MEMORY_CHAR_LIMIT` / `CIAO_USER_CHAR_LIMIT` in `.env` (restart to apply).
- When promoting from proposals: edit the region first, then dismiss with `ciao memory-proposal-dismiss <text> --promoted` (the flag records the outcome as a promotion; a plain dismissal means you decided against the fact — the reverse order can lose it). List the queue with `ciao memory-proposals`.
- When a skill proposal's change is implemented or decided against, remove it with `ciao skill-proposal-remove <name>` naming the proposal file or a unique substring of its name.
- When promoting a correction, write the present-tense standing rule it implies; never copy a "User said X -> assistant did Y" event shape into a region (memory-audit flags those as rot).

Rules:
- Search local memory before external sources.
- Ask only when a missing detail blocks a correct write.
- Keep private data inside the user's workspace.
- Prefer direct, structured vault edits over loose notes.
