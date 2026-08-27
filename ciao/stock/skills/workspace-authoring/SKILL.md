---
name: workspace-authoring
description: Create and maintain persistent working docs under memory-vault/Workspace.
---

# Workspace Authoring

Use this skill for draft/plan/analysis documents in `memory-vault/Workspace`.

> Skills are local folders: place `skills/<name>/SKILL.md` (or validated zip) then `ciao sync-skills`; workspace git sync propagates to other devices. No GitHub fetch.

## Document rules

- Prefer updating an existing related document over creating a duplicate.
- Use plain markdown. Cross-link notes with relative markdown links, not wikilinks.
- Use concise frontmatter:
  - `type: draft | plan | analysis | notes | reference`
  - `tags: []`
  - `created: YYYY-MM-DD`
  - `related: []`

## Workflow

1. Locate existing docs
- List and read relevant files in `memory-vault/Workspace`.

2. Create or update
- If no relevant file exists, create one with kebab-case filename.
- If one exists, edit in place and keep history in sections.

3. Connect context
- Link to existing vault entities where useful, as relative markdown links: `[Mo](../People/Mo.md)`, `[Apollo](../Projects/Apollo.md)`. The path is relative to the note you are writing, and the label is what the reader sees. Wikilinks (`[[People/Mo]]`) are no longer read by the index, backlinks, or the viewer.

4. Finalize
- Keep structure clear with headings and short sections.
