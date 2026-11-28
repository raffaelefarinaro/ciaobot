# The Unsure list and proposal files

Memory is written directly, by the memory pass after a chat and by the care run alike. A fact you are confident of goes where it belongs now: a bounded region, a note, a project doc or `ciao memory learn`. A replaced or removed line goes to the undo log first (`../guide.md`), so a wrong write can be undone; nothing waits in a queue for approval. Two things are not written directly.

## The Unsure list

Anything you could not settle goes on one plain Markdown list, `<vault>/Workspace/Unsure.md`. Create it with a `# Unsure` heading when it is missing. One line per item:

```
- [ ] [<kind> <qualifier>:] <plain first sentence> [<detail>] — unsure because <short reason>; [to: <destination>;] source: <chat id or note path>; <YYYY-MM-DD>
```

The person reads this list in Memory · Unsure, one item at a time: its first sentence is the title, the reason says why it waits, the source links to the chats or notes it came from, and *If you approve* shows the quoted line and the destination before they choose. So write the line for them:

- **The first sentence** is one plain sentence that says what you are asking or suggesting, so that someone who has never opened the vault understands it: "Should I remember that Mo now works at Acme?", "Remove the reminder about the staging password from what every chat loads?". End it with `?` or `.`. No internal words in it: no region, entry, vault, kind, care step, note path or chat id. Use the person's own names for people and projects.
- **The detail**, only when the line needs it, follows the first sentence: the exact entry in quotes for `drop`, `restore to` and `rule flip` lines (a care run copies it from here), `old -> new` for a rule flip, `seen in: <chat id>, …` for an observation. On a line with no kind, and on an observation, put the exact line you would write in quotes when you know it: `"Mo works at Acme since November 2026."`. The page shows it as what approving saves, and a care run writes that line.
- **The reason** is one short clause after ` — unsure because ` (` — removed because ` on a `restore to` line): why you did not write it yourself.
- **The destination**, when you know it: `; to: <vault path of the note>` (`People/Mo.md`, `Projects/Atlas.md`) or `; to: <region>` (`profile`, `memory`). Give it on a line with no kind and on an observation; leave it out when you do not know where the fact belongs. The page shows it under the quoted line, and a care run writes there.
- Then `; source: <chat id or note path>` (several chat ids joined by `, ` or `and`) and `; <YYYY-MM-DD>`.

A line with no kind is a fact or change to a note, project doc or region. These kinds are allowed, and no others:

| Line starts with | Meaning | A ticked line, in an unattended run |
|---|---|---|
| (no kind) | a fact or change whose destination or truth you doubted | write it to its destination (the `to:` note or region when the line has one; the quoted line when it has one), then delete the line |
| `observation:` `<sentence> — seen in: <chat id>, …` | a pattern seen in fewer than three chats | write it to its profile or people destination, then delete the line |
| `drop <region>: <sentence> "<entry>"` | remove a region entry you were not sure about | copy the entry into `Workspace/Memory-Consolidations.md`, remove it, delete the line |
| `restore to <region>: <sentence> "<entry>"` | a region entry a care step removed, offered back | add the entry back with `ciao memory update`, delete the line; never offered for a permission, address, credential or instruction |
| `category:` | a thing no category in VOCABULARY.md covers | not yours to add (categories belong to Settings): leave the line, report it as a `needs you` item: "add the category, then ask me to file the note" |
| `repo <path>:` | a fact for a linked repository's AGENTS.md | leave the line and report it as a `needs you` item; an attended chat applies it, because an unattended run writes only under the vault |
| `rule flip <memory\|task>/<key>: <sentence> <old> -> <new>` | a change to a decision rule | leave the line and report it; an attended chat applies it with `ciao decision-rules-write` |

- Flatten to one line, present tense. Never put a credential value on it; name the credential.
- A line written before this format (its text starting with the entry or a run of notes, with no plain first sentence) is still read and acted on as the table says; do not rewrite it only to change its wording.
- Search the list before adding: do not add a line that is already there.
- Re-read `Unsure.md` immediately before each edit to it, and change only the lines you mean to: the person ticks, unticks and removes lines from the Memory · Unsure page at any time.
- The person ticks `[x]` to approve an item and deletes a line to reject it. On the next run (or when asked), act on each ticked line as the table says. Never act on an unticked line. A ticked line you leave in place stays ticked; mention it once per run, not as a new item.
- An observation line collects distinct chat ids: append each new one to the same line. At three distinct chats write the fact to its profile or people destination and delete the line.
- A stale note you cannot settle against its sources, a conflict between two sources (quote both), and anything else you doubt belong here.
- Count the lines you add in your done line. They are not `needs you` items one by one: the care run lists every open line once, as one item pointing at `Workspace/Unsure.md`.
- The person approves or rejects from the Memory · Unsure page or by editing the file; both leave the same file. The page ticks `[x]`, unticks, or deletes the one line.

A worked example, one line of each shape:

```
# Unsure

- [ ] Should I remember that Mo now works at Acme? He mentioned leaving last month. "Mo works at Acme since November 2026." — unsure because two notes disagree and neither has a date; to: People/Mo.md; source: chat-1a2b3c4d; 2026-11-26
- [ ] observation: You prefer a short status update before a long explanation. — seen in: chat-1a2b3c4d, chat-5e6f7a8b — unsure because it has shown up in two chats so far; source: chat-5e6f7a8b; 2026-11-25
- [ ] drop memory: Remove the reminder about the staging password from what every chat loads? "Staging credentials rotate every Friday; ask before rotating" — unsure because the rotation may still be in place; source: care run; 2026-11-25
- [ ] restore to profile: Put back that you work from Lisbon on Mondays? "Works from the Lisbon office on Mondays" — removed because no message of yours said it; source: chat-9c8d7e6f; 2026-11-24
- [ ] category: Add a category for recipes? Three cooking notes fit none of the current ones. — unsure because no category in VOCABULARY.md covers them; source: chat-0f1e2d3c; 2026-11-24
- [ ] repo ~/repos/app: Run the API tests with `uv run pytest -n auto`? — unsure because only one chat used that command; source: chat-4b5c6d7e; 2026-11-23
- [ ] rule flip memory/stale-days: Re-check notes every 45 days instead of every 30? 30 -> 45 — unsure because the operator has not approved the change; source: care run; 2026-11-23
```

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
