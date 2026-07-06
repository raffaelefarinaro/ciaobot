---
name: vault-read
description: Read-only vault retrieval and summarization workflow using recall_memory and vault files.
---

# Vault Read

Use this skill for memory-only questions.

## Workflow

1. Retrieve
- Use `recall_memory` first for people/projects/ideas/tasks/resources/places.
- If needed, read linked vault notes for context.

2. Respond
- Answer from vault evidence only.
- If nothing relevant is found, state that directly.

3. Boundaries
- Do not perform web lookups.
- Do not create or modify files.

## Output

- Keep concise.
- Quote or reference note paths when useful.
