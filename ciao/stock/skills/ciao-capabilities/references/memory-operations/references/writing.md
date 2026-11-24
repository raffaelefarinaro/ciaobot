# Writing documents and bounded regions

## Authorized documents

- Discussion and corrections are not write authorization. For requested notes, drafts or analysis, search for an existing document before creating one; use the active vault's `Workspace/` or the project's canonical document.
- Use the supplied canonical path without duplicating or renaming it. Only when discovering an unbound project's main file, look for `README.md` first, then `<folder-name>.md`; this is not a folder-naming rule. Update meaningful decisions and status only within authorized document or implementation work.
- Create vault notes with `ciao note create`, using the categories in `VOCABULARY.md`. When rewriting a note, preserve its frontmatter, including `type:`, `updated:` and `tags:`. Use relative Markdown links, not wikilinks. Frontmatter supplies the explicit metadata needed for reliable indexing and aging; an ordinary edit does not re-verify a note.
- Leave `updated:` unchanged on ordinary edits. Re-stamp only through a managed `note verify` still_valid/update verdict or `vault review keep`; follow the main skill's verification procedure.
- Run `ciao file surface <path>` for substantial or iterative deliverables, then inspect its response rather than assuming the preview opened.

## Bounded-region syntax

- Bounded memory is only the fenced `ciao:memory` and `ciao:profile` regions of `AGENTS.md`; their destinations and consolidation policy are in the main skill. Separate entries with `§`; temporary facts may use `[expires: YYYY-MM-DD]`.
- Queue a remembered fact with `ciao memory-proposal-add --text-file <file>`. Consolidate through native file edits or `ciao memory update`; its `--entry` argument must contain only text you wrote, never untrusted source text.
