# Step hygiene: weekly vault hygiene, fixing what is safe to fix

Dispatched weekly. It rebuilds the index, audits the vault, fixes the findings a rule below settles without judgment, and leaves the rest for a person. This step may use about 40 tool calls and fix at most 25 notes per run.

## Inputs
- `ciao vault-index --write` and `ciao os-audit --json --scope workspace` (both read the configured vault; exit 1 from the audit means findings exist, exit 2 means its evidence is unreliable).
- In the audit's JSON, `vault_hygiene` holds four lists: `frontmatter_errors` (`{source, kind, message}`, kind `missing_frontmatter`, `malformed_frontmatter`, `missing_type`, `invalid_type` or `unknown_type`; an `unknown_type` message ends `use '<canonical>'` when the vocabulary has one), `broken_markdown_links` (`{source, target, resolved, kind}`, kind `missing_target` or `outside_vault`), `orphans` (note paths nothing links to) and `duplicates` (lists of paths in one folder under one name). `skill_audit.issues` holds `skill_over_budget` (`{skill, path, size_bytes, max_bytes}`) and `missing_skill_md`. Paths are relative to the vault the audit read; skip any outside this workspace. Region findings belong to the regions step: leave them.
- The **Categories** section of `<V>/VOCABULARY.md`: each category's folder and canonical `type:`.

## Procedure
1. Run `ciao vault-index --write`; if it fails, the step is blocked with its error. Run the audit and keep its JSON as the before list. With exit 2, block the step with the scan errors and make no claim.
2. Fix, in this order, until 25 notes are fixed. Re-read a note after every write. Before replacing or removing any existing line, log it in the undo log with `by: care hygiene`; an added line needs no entry.

| Finding | Fix it yourself when | The fix |
|---|---|---|
| `unknown_type` with `use '<canonical>'` | always | set `type:` to that canonical value |
| `missing_type`, `invalid_type` | the note sits in exactly one category's folder | set `type:` to that category's canonical type |
| `missing_frontmatter` | the note sits in a category's folder | add a front matter block: `type:` from the folder, `created:` and `updated:` as the file's last-modified date (the date the audit already ages it by) |
| `missing_target` link | exactly one note in the vault has the target's file name (`find <V> -name '<name>'`) | change only that link's destination to the relative path of that note; keep the link text |
| orphan | `ciao note related <path>` lists a note that is not itself an orphan | add one line `- [<title>](<relative path>)` under a `## Related` heading at the end of the first such note (create the heading if missing); additive only |
| `skill_over_budget` | no open `skill-edit` proposal for that skill exists | write one `skill-edit` proposal per skill (`unsure-and-proposals.md`): which sections to move into `references/` files or cut so `SKILL.md` fits its budget, with the sizes as evidence; run `ciao proposal-check` until it prints `ok` |
| two notes in `duplicates`, one an empty stub (front matter and a title only) | no `note-retire` proposal for the stub exists | write a `note-retire` proposal for the stub; never merge or delete |

Never fix anything else unattended: never delete, move, rename or trim a note, never change a person's own wording, never edit a skill or the AGENTS.md body. `malformed_frontmatter`, `outside_vault` links, other duplicates, a dead link with no single match, an orphan with no related note and `missing_skill_md` are left as they are. A `missing_frontmatter` file outside every category folder is something a script or schedule writes, not a note: when `<V>/.ciaoignore` has no pattern for it, suggest one.
3. Run `ciao vault-index --write` and the audit again: this is the after list.
4. Write the `## Hygiene` section of `Care-Report.md` (this step's one exception to leaving the report alone; never touch a line above it): `- index rebuilt; audit exit N, F findings before, G after; fixed X`, then one plain `- ` bullet per finding left, with its kind in everyday words and the note's path, and never a `- [ ]` checkbox.

## Done when
Every finding was fixed or deliberately left, within the caps. Finish with `done: fixed X of F findings, left G (<kinds>), proposals P (<paths>)`, then the items it leaves, in the preamble's forms:
- Skills over budget: one item for all of them, naming how many and pointing at the first proposal still open, e.g. `needs you: 6 skills are longer than they should be, and a proposal to shorten each is ready. Choose Review proposal to approve or reject the first. (path: Workspace/Proposals/<slug>.md)`.
- Findings left that need a decision (a damaged header, two notes for one thing, a link to outside the vault): one item, e.g. `needs you: 4 notes need a decision the run cannot make alone, such as two notes for one person. Choose Fix with a chat to go through them. (path: Workspace/Care-Report.md)`.
- A suggested `.ciaoignore` pattern: one item with the pattern.
- What the caps left: a `note:`, never a `needs you` item.

Verify: `find <INDEX.md> -newermt <today>` lists INDEX.md at the vault root (`<V>/../INDEX.md` when V is a workspace folder under `memory-vault/`, else `<V>/INDEX.md`); in a fresh `ciao os-audit --json --scope workspace`, none of the X fixed findings (same source and kind) is listed and the vault finding count fell by at least X from the before list; `ciao proposal-check` prints `ok` for each proposal named; the `## Hygiene` section shows the before and after counts.
