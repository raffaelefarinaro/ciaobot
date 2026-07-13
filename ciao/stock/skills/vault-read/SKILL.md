---
name: vault-read
description: Read-only vault retrieval and summarization using ciao vault-search, vault-index, and file reads.
---

# Vault Read

Use this skill for memory-only questions — answer from the local vault, not the web.

## Workflow

1. **Retrieve**
   - Check whether `<ciao-entities>` already surfaced a vault path from the user's prompt.
   - Keyword or topic recall: `ciao vault-search "<query>" --limit 5`
   - Known entity (person, project, place, …): `ciao vault-index --name "<name>" --type <person|project|place|idea|resource|document>`
   - Related notes: `ciao vault-index --related-to "<vault/path>"` or `--neighbors "<vault/path>" --depth 2`
   - Read full note bodies with `Read` on paths under `memory-vault/` (or the workspace's configured vault root).

2. **Respond**
   - Answer from vault evidence only.
   - If nothing relevant is found, say so directly.

3. **Boundaries**
   - Do not perform web lookups.
   - Do not create or modify files.

## Tips

- Run `ciao vault-search` without a query to refresh the FTS index if results look stale.
- After large vault edits, `ciao vault-index --write` regenerates `INDEX.md` (also runs on server startup when enabled).
- Prefer `vault-search` for fuzzy text; prefer `vault-index` for typed entities and graph neighbors.

## Output

- Keep concise.
- Quote or reference note paths when useful.
