# The Unsure list and proposal files

Memory is written directly, by the memory pass after a chat and by the care run alike. A fact you are confident of goes where it belongs now: a bounded region, a note, a project doc or `ciao memory learn`. Git backs the vault up, so a wrong write is undone by reverting it; nothing waits in a queue for approval. Two things are not written directly.

## The Unsure list

Anything you could not settle goes on one plain Markdown list, `<vault>/Workspace/Unsure.md`. Create it with a `# Unsure` heading when it is missing.

```
- [ ] <one-line fact or change> — unsure because <reason>; source: <chat id or note path>; <YYYY-MM-DD>
```

- One line per item, flattened, present tense. Never put a credential value on it; name the credential.
- Search the list before adding: do not add a line that is already there.
- The person ticks `[x]` to approve an item and deletes a line to reject it. On the next run (or when asked), write each ticked item to its destination, then delete the line. Never apply an unticked item.
- A pattern seen once is an observation, not a fact. Add it as `- [ ] observation: <pattern> — seen in: <chat id>`, and append each new distinct chat id to that same line. At three distinct chats write the fact to its profile or people destination and delete the line.
- A stale note you cannot settle against its sources, a conflict between two sources, a repository fact that would change a repo's AGENTS.md, and a new-category question all belong here.
- Report the number of new items under **What needs you**.

## Proposal files

A skill edit, a guide edit, an upstream GitHub issue or a note retirement is never made or filed by you unattended. When asked for one (or when a care run finds one worth making), write a proposal file: `<vault>/Workspace/Proposals/<slug>.md`, a lowercase slug such as `tighten-memory-skill.md`.

```
---
type: skill-edit
target: ciao-memory
title: Tighten the stale-note step
summary: One or two sentences a person can read in five seconds.
rationale: Why the change is worth making, with what you actually observed.
evidence:
  - Workspace/Care-Report.md
  - <chat id>
proposed_change: The exact wording or action to apply.
created: 2026-11-26
status: draft
---

Longer explanation in Markdown: context, alternatives, risks.
```

- `type` is `skill-edit`, `upstream-issue`, `guide-edit` or `note-retire`. `target` is the skill's name, the repository as `owner/name`, a path relative to the workspace (for example `AGENTS.md`) or the note's vault path ending in `.md`.
- `evidence` is a list of links, note paths or chat ids that a person can open. A claim with no evidence is not a proposal.
- `status` is `draft`, `ready`, `applied` or `rejected`. Write `draft` or `ready`; a person (or you, when asked) sets the other two after acting.
- No transcript excerpt, credential, private name or path outside the workspace belongs in an upstream-issue proposal: a stranger must be able to reproduce it from the file alone.
- After writing the file run `ciao proposal-check <file>`. It prints every problem at once; fix them all and run it again until it prints `ok`. Then tell the person where the file is.
- A person files the issue or applies the edit from the file, or asks you to in an attended chat. Set `status: applied` (and link the issue in the body) when it is done. Never edit a skill, the AGENTS.md body or a note you proposed to retire from an unattended run.
