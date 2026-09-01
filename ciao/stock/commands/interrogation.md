---
description: Turn a vague project or durable entity into a useful canonical note.
argument-hint: <project, person, idea, or topic>
---

# Interrogation: $ARGUMENTS

Collect enough detail to create or improve a durable vault page in the active workspace's vault (the `<ciao-context>` block names it as `vault=<path>` — write under **that** path, and nowhere else).

1. **Search before asking.** Run `vault_search` for the subject (try 2–3 reformulations — a nickname, a paraphrase, a distinctive noun). Improving an existing note beats creating a duplicate; recall is lexical, so an existing note may not surface on the obvious query.
2. **Fill the biggest gaps.** Ask 1–3 targeted questions at a time — goal, done-ness, deadline/cadence, stakeholders, constraints. Stop asking once you have enough to avoid a stub; do not interrogate for completeness.
3. **Write the note only when it clears the stub bar**, following the vault's own conventions:
   - `type:` comes from the canonical list in `<vault>/VOCABULARY.md` — never invent one; `ciao vault-lint` reports anything else as `unknown_type`. If nothing fits, use the closest canonical value and raise the gap as a vocabulary proposal.
   - People and project notes get an `aliases:` frontmatter list with the relationship terms and paraphrases someone would actually search for ("brother-in-law", "hourly rate", a nickname) — aliases are what make a paraphrased question find the note.
   - Route by scope: a person → `People/`, a project → its canonical project doc, an idea or resource → the matching vault folder. A project fact never goes into the bounded `ciao:memory`/`ciao:profile` regions.
4. **Confirm** destination and one-line summary of what you saved.