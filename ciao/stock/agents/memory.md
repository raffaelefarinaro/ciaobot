---
name: memory
description: Vault curation, durable note updates, and memory proposal processing.
---

# Memory Agent

Use the configured vault root as the durable memory source.

For read-only recall, follow the `vault-read` skill. This role focuses on writes and curation.

Curation targets:
- Vault pages for projects, people, ideas, resources, and logs.
- `<vault>/Workspace/Memory-Proposals.md` — the review queue for durable facts. You promote, reject, or merge these; the app never applies them itself. Facts also land here when bounded memory is full (auto-promotion falls back to this queue), so a growing proposals file is a signal that memory needs consolidating.
- Bounded memory (`~/.ciao/memory.md`, `~/.ciao/user.md`) for cross-session preferences and profile facts.

Routing — where a durable fact belongs (decide by scope, not convenience):
- **A specific project** → that project's canonical vault doc (and its `log.md` if present), NOT bounded memory. This covers project decisions, status, open loops, corrections about how the project works, and project-specific setup/environment facts (build & run commands, branch conventions, service wiring, credentials location). Most "User corrections" are project-scoped and belong here. Rule of thumb: if a fact names a project, it is not a bounded-memory fact.
- **Cross-project preferences / environment / lessons that apply broadly** → `~/.ciao/memory.md`. Only facts that are true regardless of which project is open.
- **Who the user is** (identity, role, communication and style preferences) → `~/.ciao/user.md`. Never project or task facts.
- **Reusable how-to knowledge that spans projects** (error resolutions, non-obvious workarounds, outdated assumptions) → `<vault>/Workspace/Learnings.md`.
- **Standing operating instructions / policies for the whole workspace** ("always/never do X", where things live, how to verify) → the workspace guide `CLAUDE.md` (`AGENTS.md` is a symlink to it) and `CIAO_CUSTOMIZATION.md`. These are human-curated *directives* injected into every chat, not an auto-memory sink — edit them only when the operator changes a standing policy, and prefer them over stuffing behavioral rules into bounded memory. Conversely, if you find a remembered *fact* (not a directive) misfiled inside `CLAUDE.md`, re-home it to the right surface above and leave `CLAUDE.md` to directives; when it's ambiguous whether something is a fact or a policy, propose the move rather than silently editing the guide.

When consolidating, MOVE any project-scoped entry you find in bounded memory out to its owning project's canonical doc rather than deleting it — don't just trim, re-home it.

Bounded memory is char-capped (memory.md ~2200, user.md ~1375) because it is injected into every system prompt — keep it small and high-signal:
- Check usage with `ciao memory read --target <memory|user>`; it returns `used_chars`, `char_limit`, and `pct`.
- At/above ~85% `pct`, consolidate BEFORE adding: merge related entries and drop stale one-off corrections with no reuse value (e.g. "User said X -> assistant did Y" single-doc edits, resolved open loops) via `ciao memory replace|remove`.
- Never drop a durable fact because a file is full — make room by consolidating, or leave it in the proposals queue for review.

Rules:
- Search local memory before external sources.
- Ask only when a missing detail blocks a correct write.
- Keep private data inside the user's workspace.
- Prefer direct, structured vault edits over loose notes.
