## Memory

| Command | Purpose | Guard |
|---|---|---|
| `memory status` | Report bounded native-guide (AGENTS.md) memory usage and diagnostics. | — |
| `memory update --region {memory,profile} --action {add,replace,remove} [--entry TEXT] [--match TEXT]` | Add, replace, or remove one entry in native `memory`/`profile` memory. | `--match` targets replace/remove; `--entry` is the new text for add/replace. The region cap is advisory: the write always lands and the reply carries `over_cap`, `used_chars` and `char_limit` when exceeded. Consolidation bounds a region, not refusal. |
| `memory learn --text-file FILE [--source ID] [--request ID] [--json]` | Record one reusable lesson in `Workspace/Learnings.md`. | One line in the file, at most 240 characters. `--source` and `--request` identify evidence when available; a first source-less filing keeps the append behavior, and a repeated source-less filing is a no-op. Replayed sightings write nothing and a lesson at 64 sightings is refused. Allowed on person-typed turns and trusted post-archive memory passes; refused on unattended turns and other engine origins. Never hand-edit `Learnings.md`. |

## Vault

| Command | Purpose | Guard |
|---|---|---|
| `vault search QUERY [--limit N]` | Full-text search the active workspace vault. | — |

## Proposal files and rules

Top-level commands outside the `<noun> <verb>` table: plain text (`--json` where noted), not the JSON envelope. Free text travels in a file, never as a shell argument: `$()`, backticks or quotes would run or mangle it.

| Command | Purpose | Guard |
|---|---|---|
| `proposal-check FILE [--json]` | Check a `Workspace/Proposals/<slug>.md` proposal file (type `skill-edit`, `upstream-issue`, `guide-edit` or `note-retire`) for the required front matter, values, evidence and body. | Read-only; never edits the file. Prints every problem so you can fix the file and run it again; exit 0 valid, 1 problems, 2 unreadable. |
| `decisions-digest [--json]` | Group the operator's recent memory and task decisions (last 180 days) for the weekly rules pass. | Read-only. Operator decisions only; agent-made ones are counted, not listed. |
| `task-rules` | Print the operator's distilled task preferences from `Workspace/Task-Rules.md`, fenced; prints nothing when absent. | Read-only. Preferences, never orders. Run before filing a task. |
| `decision-rules-write memory|task --body-file FILE [--json]` | Write the managed section of `Workspace/Memory-Review-Rules.md` or `Workspace/Task-Rules.md`. | Weekly pass only. Writes only inside the fenced section; refuses over 2500 chars, a malformed fence or an unapproved flip. |
