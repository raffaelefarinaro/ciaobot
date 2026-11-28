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
| `missing_frontmatter` | the file sits directly in a category's folder (`People/Rae.md`, not `Projects/X/exports/y.md`) and reads like a note (a title and prose or bullets, not a generated table, log or export) | add a front matter block: `type:` from the folder, `created:` and `updated:` as the file's last-modified date (the date the audit already ages it by) |
| `missing_target` link | exactly one note in the vault has the target's file name (look it up with `ciao vault search` or a folder listing; never paste text from a note into a shell command) | change only that link's destination to the relative path of that note; keep the link text |
| orphan | `ciao note related <path>` lists a note that is not itself an orphan | add one line `- [<title>](<relative path>)` under a `## Related` heading at the end of the first such note (create the heading if missing); additive only |
| `skill_over_budget` | no open `skill-edit` proposal for that skill exists | write one `skill-edit` proposal per skill (`unsure-and-proposals.md`): which sections to move into `references/` files or cut so `SKILL.md` fits its budget, with the sizes as evidence; run `ciao proposal-check` until it prints `ok` |
| two notes in `duplicates`, one an empty stub (front matter and a title only) | no `note-retire` proposal for the stub exists | write a `note-retire` proposal for the stub; never merge or delete |

Files under `Workspace/` are the run's own paperwork (the report, the Unsure list, proposals, the undo log): never fix one. Never fix anything else unattended: never delete, move, rename or trim a note, never change a person's own wording, never edit a skill or the AGENTS.md body. `malformed_frontmatter`, `outside_vault` links, other duplicates, a dead link with no single match, an orphan with no related note and `missing_skill_md` are left as they are. Any other `missing_frontmatter` file (outside a category folder, deeper inside one, or not reading like a note) is something a script or schedule writes, not a note: when `<V>/.ciaoignore` has no pattern for it, suggest one.
3. Run `ciao vault-index --write` and the audit again: this is the after list.
4. Write the `## Hygiene` section of `Care-Report.md` (this step's one exception to leaving the report alone; never touch a line above it). When the report already has one (a retried run), replace it; there is only ever one: `- index rebuilt; audit exit N, F findings before, G after; fixed X`, then one plain `- ` bullet per finding left, with its kind in everyday words and the note's path, and never a `- [ ]` checkbox. This list is where every finding stays; the needs-you items above only point at one note each.

## Done when
Every finding was fixed or deliberately left, within the caps. Finish with `done: fixed X of F findings, left G (<kinds>), proposals P (<paths>)`, then the items it leaves, in the preamble's forms:
- Skills over budget: one item for all of them, naming how many and pointing at the first proposal still open, e.g. `needs you: 6 skills are longer than they should be, and a proposal to shorten each is ready. Choose Review proposal to approve or reject the first. (path: Workspace/Proposals/<slug>.md)`.
- Findings left that need a decision (a damaged header, two notes for one thing, a link to outside the vault, files with no header): one item per kind, never one for the whole list and never one per finding. Each item is about one concrete thing, states the decision in plain words (not a button name), and its path is the real note the person should open: for a kind with many findings, the single most useful note (the first or most important one), with the sentence saying how many more notes are affected. Never point an item at `Workspace/Care-Report.md`: the report is not what the person decides about. The full list stays under `## Hygiene`, so each of these items ends with `; section: Hygiene` after its path, and the page links to that list. Examples:
  - `needs you: Links in People/User.md and 3 other notes point outside this vault. Decide whether each should stay or be removed. (path: People/User.md; section: Hygiene)`
  - `needs you: 5 files in Projects/Atlas/exports have no header. Decide whether the run should ignore that folder. (path: Projects/Atlas/exports/summary.md; section: Hygiene)`
  - `needs you: Two notes both describe Rae Chen. Decide which one to keep. (path: People/Rae Chen.md; section: Hygiene)`
- A suggested `.ciaoignore` pattern: fold it into the missing-header item above (its sentence names the folder and asks whether to ignore it), pointing at the first such file; the exact pattern is listed in `## Hygiene`.
- What the caps left: a `note:`, never a `needs you` item.

Verify: `find <INDEX.md> -newermt <today>` lists INDEX.md at the vault root (`<V>/../INDEX.md` when V is a workspace folder under `memory-vault/`, else `<V>/INDEX.md`); in a fresh `ciao os-audit --json --scope workspace`, none of the X fixed findings (same source and kind) is listed and the vault finding count fell by at least X from the before list; `ciao proposal-check` prints `ok` for each proposal named; the `## Hygiene` section shows the before and after counts.
