---
name: workspace-authoring
description: Create and maintain persistent working docs under memory-vault/Workspace.
---

# Workspace Authoring

Use this skill for draft/plan/analysis documents in `memory-vault/Workspace`.

## Document rules

- Prefer updating an existing related document over creating a duplicate.
- Use Obsidian-friendly markdown and wikilinks.
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
- Link to existing vault entities where useful (`[[People/...]]`, `[[Projects/...]]`).

4. Finalize
- Keep structure clear with headings and short sections.
