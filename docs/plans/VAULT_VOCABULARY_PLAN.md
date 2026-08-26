# Vault vocabulary: promotion/merge proposals (step 5)

Status: **proposed, checkpoint C0, blocker: none**
Written: 2026-08-19; trimmed 2026-08-27 to the outstanding scope
Scope: `ciao/os_audit.py`, `ciao/stock/schedules.json` (the
`system-workspace-hygiene` routine)

Steps 1-4 of the original vocabulary plan are **done** and no longer need
re-reading in full — see git history for the full design if background is
needed:

- `CANONICAL_TYPES` + `TYPE_ALIASES` — `ciao/vault_index.py`, 16 canonical
  types, 9 aliases.
- `VOCABULARY.md` generation from the index pass — `write_vocabulary_file`.
- `unknown_type` lint, naming the alias target — `ciao/vault_lint.py:_frontmatter_error`.
- Agent guidance — the "Categories" section in `ciao/stock/agents/memory.md`.
- Migration tooling (added to scope, also done) — `ciao/vault_migration.py`,
  `ciao vault-migrate`, dry-run unless `--apply`.

Related: per-workspace memory curation (implemented 2026-08-19; removed from
`docs/plans/` as done) — shares the `system-workspace-hygiene` routine and
the same global-vs-per-workspace scope question this step also touches.

## What's left: step 5 — promotion/merge proposals

`system-workspace-hygiene` today only lints (`unknown_type`) and applies
alias-target renames as a low-risk fix. It does not yet propose the two
changes that need a human decision, mirroring the existing
`Memory-Proposals.md` promote/dismiss pattern
(`ciao/memory_proposals.py`, resolved via `memory_proposal_resolve`):

- **Promotion.** A non-canonical type or an emerging tag that crosses a usage
  threshold is a candidate for the canonical set — e.g. `skill-proposal` at
  49 uses earned its place in the original sweep; a 1-use type would not.
- **Merge.** A singleton near-duplicate tag (e.g. `ai-analysis` /
  `ai-adoption` / `ai-practice` alongside `ai`) is a candidate for aliasing.

`system-workspace-hygiene` should *propose*, not rewrite frontmatter across
the vault on its own — its existing prompt already draws this line ("apply
only low-risk, unambiguous fixes"), and an aliased type is exactly that: a
rename with a known target. Promoting a new type into the canonical set is
not, and needs the same human review as the original 9 singleton types did.

### Scope carried over from steps 1-4

- **Types stay a single global canonical set** — structural, and `INDEX.md`
  is one shared artifact, so promoting a type is a global decision.
- **Tags stay global-established / per-workspace-tail** — a promotion or
  merge proposal for a tag should say which workspace(s) it's used in.

### Open questions

- Threshold for promoting an emerging tag or type into the canonical set — 5
  uses matched the tier boundary in the original sweep, but it should be a
  deliberate number, possibly configurable.
- Where do near-duplicate merge proposals surface — inline in the hygiene
  routine's existing output, or a new `Vocabulary-Proposals.md` alongside
  `Memory-Proposals.md`?

### Tests to add

- A type crossing the promotion threshold produces a proposal, not an
  automatic `CANONICAL_TYPES` change.
- A singleton tag with an obvious near-duplicate (edit-distance or shared
  prefix) produces a merge proposal; a singleton with no near-duplicate does
  not.
- `system-workspace-hygiene` still applies alias-target renames automatically
  (existing step-4 behavior) and does not regress when proposals are added.

---

*Line references verified against the working tree on 2026-08-19; re-anchor
by symbol name if a number looks off.*
