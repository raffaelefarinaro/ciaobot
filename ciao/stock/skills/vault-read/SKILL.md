---
name: vault-read
description: Read-only vault retrieval and summarization using Ciaobot MCP vault tools.
---

# Vault Read

Use this skill for memory-only questions — answer from the local vault, not the web.

## Workflow

1. **Retrieve**
   - Check whether `<ciao-entities>` already surfaced a vault path from the user's prompt.
   - Use `vault_search`, then `vault_note_read` for the relevant paths. Use `vault_index_refresh` only when results look stale.

2. **Respond**
   - Answer from vault evidence only.
   - If nothing relevant is found, say so directly.

3. **Boundaries**
   - Do not perform web lookups.
   - Do not create or modify files.

## Tips

- Prefer `vault_search` for fuzzy text. Refresh the index only when needed.

## Output

- Keep concise.
- Quote or reference note paths when useful.
