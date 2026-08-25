# Vault vocabulary: closed types, stratified tags

Status: **steps 1-4 implemented 2026-08-19**; step 5 (promotion/merge proposals) outstanding
Written: 2026-08-19
Scope: `ciao/vault_lint.py`, `ciao/vault_index.py`, `ciao/os_audit.py`,
`ciao/stock/agents/memory.md`, `ciao/stock/schedules.json`

Stops the agent from inventing a new category every time it files a note, by
giving it a vocabulary to consult and a check that fails when it doesn't.

Related: per-workspace memory curation (implemented 2026-08-19; removed from
`docs/plans/` as done — see git history) — shares the
`system-workspace-hygiene` routine and the same global-vs-per-workspace
scope question.

---

## Problem

Measured on this vault, over the **indexed universe** — the 552 files that
`vault-index` and `vault-lint` actually see, after both exclude
`Logs/`, `Templates/`, `.obsidian/` (`ciao/vault_index.py:37`,
`ciao/vault_lint.py:51-52`). The 775 `telegram-transcript` files live under
`Logs/` and are irrelevant here.

**Types — 21 distinct, 9 used exactly once:**

```
107 log            96 project        95 person       49 skill-proposal
 40 note           30 reference      21 resource      4 product
  4 feature         3 idea            2 analysis      2 document
  1 planning-doc    1 template        1 doc           1 hackathon-log
  1 feature-brief   1 discussion-prep 1 place         1 project-log
  1 plan
```

The near-duplicate pairs are the tell: `doc` vs `document`, `plan` vs
`planning-doc`, `project-log` vs `log` (107), `hackathon-log` vs `journal`,
`feature-brief` vs `feature` (4).

**This already degrades a user-facing artifact.** `INDEX.md` groups by type, so
the generated index carries **31 type headings, 14 of them containing exactly
one file** — `### doc (1)` sits next to `### document (1)`. (31 > 21 because
headings are emitted per workspace.)

**Tags — 382 distinct, 234 used exactly once (61%), only 47 used ≥5 times.**
Tags are also where the vault is *already* doing something right: an emergent
namespace convention nobody wrote down — `project/active`,
`product/barcode-capture`.

### Why it happens

Nothing validates the value. `ciao/vault_index.py:257` takes frontmatter `type:`
verbatim and only falls back to `_infer_type` (the directory map) when it is
absent or empty. So any string the agent writes becomes a first-class category.

There *is* already a canonical set in code — the **values** of `DIR_TYPE_MAP`
(`ciao/vault_index.py:41-57`): `person`, `project`, `idea`, `resource`, `place`,
`document`, `workspace`, `reference`, `product`, `feature`, `content`,
`journal`, `automation`. It is never enforced, and it is missing the three types
the vault leans on hardest: `log` (107), `skill-proposal` (49), `note` (40).

Path inference is meanwhile doing real work and must be preserved: `journal`
(67 entries in `INDEX.md`) and `automation` (11) come *entirely* from
`_infer_type`, not from frontmatter — 90 of the 552 files carry no usable
`type:` and are typed by their directory.

## Why OKF is not the answer to this

The [Open Knowledge Format](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing)
is the same shape this vault already has, and it explicitly declines to solve
the category problem:

> OKF requires exactly one thing of every concept: a `type` field. Everything
> else is left to the producer.

Its own worked example is `type: BigQuery Table` — a free-text string. There is
no controlled vocabulary in OKF v0.1 by design, so adopting it would change
nothing about tag or type sprawl.

The vault is also already substantially conformant, which is unsurprising: the
announcement names *"Obsidian vaults wired to coding agents, the AGENTS.md /
CLAUDE.md family of convention files, repos full of `index.md` and `log.md`
artifacts"* as the pattern OKF formalizes.

| OKF v0.1 | This vault |
|---|---|
| directory of `.md` + YAML frontmatter | same |
| required `type`; optional `title`, `description`, `tags` | same four fields (`ciao/vault_index.py:74-89`), plus `aliases`, `related`, `workspace` |
| reserved `index.md`, `log.md` | already special-cased: `_FRONTMATTER_EXEMPT = {"index.md", "memory.md", "log.md"}` (`ciao/vault_lint.py:28`), and `ciao/stock/agents/memory.md` already routes to a project's `log.md` |
| cross-links as markdown links | wikilinks (`WIKILINK_RE`) **and** markdown links (`broken_markdown_links`) |
| `resource`, `timestamp` fields | not used |
| `index.md` for folder hierarchy | project folders use `README.md` |

**Verdict: keep OKF out of this plan.** Where it would pay off is portability —
a bundle format shippable as a tarball, producer/consumer independence, and
consuming third-party bundles. That is a separate, optional piece of work; see
the appendix.

## Design

### 1. Types: close the set

Introduce an explicit `CANONICAL_TYPES` frozenset in `ciao/vault_index.py`,
seeded with the existing `DIR_TYPE_MAP` values **plus** the three earned types:

```
person project idea resource place document workspace reference
product feature content journal automation      # existing DIR_TYPE_MAP values
log note skill-proposal                          # earned: 107 / 40 / 49 uses
```

And an `TYPE_ALIASES` map for the tail, applied on read and reported as drift:

```
doc            -> document        plan          -> document
planning-doc   -> document        project-log   -> log
hackathon-log  -> journal         feature-brief -> feature
analysis       -> ?               discussion-prep -> ?
template       -> (exempt: templates are excluded from the index anyway)
```

`analysis` (2) and `discussion-prep` (1) have no obvious home — resolve them by
looking at the files, not by inventing a type for them.

**Do not extend `DIR_TYPE_MAP` to do this.** Its *keys* are directory names and
`_workspace_of` (`ciao/vault_index.py:113-121`) tests membership in those keys
to decide whether a leading path segment is a folder type or a workspace name.
Adding a key silently changes workspace inference for any vault with a directory
by that name. The canonical vocabulary is a set of *values*, so it belongs in
its own constant.

`_infer_type` stays exactly as-is — it is what types the 90 frontmatter-less
files, and it can only ever produce canonical values.

### 2. Tags: stratify, don't close

Closing a 382-value open vocabulary would destroy what tags are for. Instead:

- Publish tags in usage tiers (`≥5 uses` = established, `2-4` = emerging,
  `1` = candidate/likely-typo).
- Write down the `namespace/value` convention that already exists
  (`project/active`, `product/barcode-capture`) so it is used deliberately.
- Instruct the agent to prefer an existing tag and to introduce a new one only
  when nothing fits — and, when it does, to use namespaced form.
- Flag *near-duplicate* singletons (`ai-analysis` / `ai-adoption` / `ai-practice`
  alongside `ai`) as merge candidates rather than errors.

No lint failure for an unknown tag. Tags get advice; types get enforcement.

### 3. Generate `VOCABULARY.md` from the index pass

`ciao vault-index --write` already parses every file's frontmatter to build
`INDEX.md` (`ciao/vault_index.py:232-269`, written at `:650-660`). Emitting a
second file from the same `entries` list costs no extra I/O:

```
memory-vault/VOCABULARY.md
  <!-- generated by ciao vault-index, do not edit by hand -->
  ## Types (canonical)     — the closed set, with live counts
  ## Types (drift)         — non-canonical values found, with file paths
  ## Tags (established)    — >=5 uses
  ## Tags (emerging)       — 2-4 uses
  ## Tags (candidates)     — 1 use, with near-duplicate hints
```

The canonical *set* is code (`CANONICAL_TYPES`); the *file* is a rendering of it
against the vault. That split is what keeps the file from becoming authoritative
by accident — see the traps.

### 4. Enforcement: one check, already 95% written

`_frontmatter_error` (`ciao/vault_lint.py:149-191`) already validates, in order:
frontmatter present → parses as YAML → is a dict → `type` present → `type`
non-empty → `type` is a string. It stops exactly one step short of *`type` is a
known value*.

Add an `unknown_type` kind there. It then flows to every existing consumer for
free:

- `run_validation` → `frontmatter_errors` (`ciao/vault_lint.py:495`)
- `ciao vault-lint` CLI output (`ciao/cli.py:1222-1225`)
- `os-audit` → `actionable_count` → exit code 1 (`ciao/os_audit.py:1003`)
- the weekly `system-workspace-hygiene` routine, which already runs `os-audit`
  and applies low-risk fixes

That is the whole enforcement story: roughly six lines plus a constant.

An aliased value should report as `unknown_type` with the suggested canonical
target in the message, so the hygiene routine can apply it as a safe fix.

### 5. Agent guidance

`ciao/stock/agents/memory.md` currently lists curation targets informally —
*"Vault pages for projects, people, ideas, resources, and logs"* — which is an
ad-hoc type list already. Replace it with a pointer to the canonical set and the
tag rule:

- Choose `type:` from the canonical set. Never invent one; if nothing fits, file
  the note with the closest type and raise it as a proposal.
- Prefer an existing tag from `VOCABULARY.md`; new tags use `namespace/value`.

The agent must read the vocabulary, not memorize it — otherwise it drifts the
moment the set changes.

### 6. Maintenance: proposals, not auto-apply

Two changes need a human decision, and both mirror the existing
`Memory-Proposals.md` promote/dismiss pattern (`ciao/memory_proposals.py`,
resolved via `memory_proposal_resolve`):

- **Promotion.** A non-canonical type or an emerging tag that crosses a usage
  threshold is a candidate for the canonical set — `skill-proposal` at 49 uses
  earned its place; `discussion-prep` at 1 did not.
- **Merge.** A singleton near-duplicate is a candidate for aliasing.

`system-workspace-hygiene` proposes; it does not rewrite frontmatter across the
vault on its own. Its existing prompt already draws this line ("apply only
low-risk, unambiguous fixes"), and an *aliased* type is exactly that — a rename
with a known target. Promoting a new type into the canonical set is not.

### 7. Scope: global core, per-workspace extension

`scandit` (116) and `colleague` (42) are work-flavored; `daily-log` (141) is
generic. So:

- **Types: one global canonical set.** They are structural, and `INDEX.md` is a
  single shared artifact — per-workspace type sets would make the index
  incoherent.
- **Tags: global established tier, per-workspace tails.** Render one
  `VOCABULARY.md` with tags grouped by workspace where they are used, so the
  agent working in `work` isn't nudged toward personal tags.

This is the same distinction the curation plan's scope taxonomy draws: global
subject, per-workspace inputs and outputs.

## Traps

- **Don't derive the canonical set from the vault.** A purely generated
  vocabulary re-blesses all 382 tags and 21 types and enforces nothing — it
  documents the sprawl instead of stopping it. The canonical set is a curated
  constant; the generated file only reports against it.
- **Don't mass-rewrite frontmatter in the first pass.** 9 singleton types is a
  half-hour of manual review with better judgment than any rule. Land the check
  and the vocabulary first; migrate by hand or by proposal.
- **Don't lint tags.** 234 singletons would produce 234 findings on the first
  run and train everyone to ignore the audit.
- **Don't touch `DIR_TYPE_MAP` keys** (see §1) — `_workspace_of` depends on them.
- **Don't let `unknown_type` land unmigrated.** Adding the check before aliasing
  the 9 singletons means `os-audit` immediately exits 1 with findings the
  hygiene routine can't fix, on every run. Alias first, or ship the check and
  the alias map together.

## Status

| Step | State |
|---|---|
| 1. `CANONICAL_TYPES` + `TYPE_ALIASES` | **done** — `ciao/vault_index.py`; 16 canonical types, 9 aliases. `analysis`→`reference`, `discussion-prep`→`note` resolved by reading the files. |
| 2. `VOCABULARY.md` generation | **done** — `write_vocabulary_file`, emitted by `vault-index --write`. No timestamp, so it is byte-deterministic. |
| 3. `unknown_type` lint | **done** — `ciao/vault_lint.py:_frontmatter_error`, names the alias target. |
| 4. Agent guidance | **done** — a "Categories" section in `ciao/stock/agents/memory.md`. |
| 5. Promotion/merge proposals | **outstanding** — hygiene does not yet propose promoting an emerging tag or merging a near-duplicate. |
| Migration (added to scope) | **done** — `ciao/vault_migration.py`, receipt-gated under `.runtime/migration/`, run from the install/upgrade skill sync; `ciao vault-migrate` for manual use, dry-run unless `--apply`. |

Measured on the reference vault: 10 notes across 9 non-canonical types, all with
alias targets, so the automatic migration resolves all of them and leaves nothing
for the user to categorise.

## Order

1. **`CANONICAL_TYPES` + `TYPE_ALIASES` constants** — decide `analysis` and
   `discussion-prep` by reading the four files involved.
2. **`VOCABULARY.md` generation** in `vault-index --write` — read-only, ships
   safely on its own, and makes the current state legible.
3. **`unknown_type` in `_frontmatter_error`** — together with, or after, the
   alias migration.
4. **Agent guidance** in `ciao/stock/agents/memory.md`.
5. **Promotion/merge proposals** from `system-workspace-hygiene`.

Steps 1-2 are independently useful: even with no enforcement, a generated
vocabulary is what the agent needs to stop guessing.

## Tests

- `CANONICAL_TYPES` is a superset of every `DIR_TYPE_MAP` value — so
  `_infer_type` can never produce a value the linter rejects.
- `_frontmatter_error` returns `unknown_type` for a novel type, and names the
  alias target when one exists.
- Every alias target is itself canonical (no alias chains).
- `VOCABULARY.md` is regenerated deterministically — same vault, same bytes
  (matching `INDEX.md`'s existing generated-file contract).
- Exempt filenames (`index.md`, `memory.md`, `log.md`) and `Templates/` are
  still skipped by the new check.
- A vault whose every type is canonical produces zero `unknown_type` findings
  and a `healthy` audit status.

## Open questions

- Where do `analysis` (2) and `discussion-prep` (1) belong? Needs a look at the
  files.
- Threshold for promoting an emerging tag or type into the canonical set — 5
  uses matches the tier boundary, but it should be a deliberate number.
- Should `note` (40) be canonical, or is it the untyped default that should be
  narrowed into `reference` / `idea` / `log`? Canonicalizing it makes the vague
  option legitimate.
- `telegram-transcript` / `cli-transcript` sit under `Logs/` and are excluded
  from both index and lint. Leave them unvalidated, or give them a canonical
  `transcript` type with a `source:` field for when they surface elsewhere?

## Appendix: optional OKF conformance

Not needed for the vocabulary work; worth its own decision if exporting or
importing knowledge bundles ever matters. The gap is small:

- Add `resource:` (canonical URL) and `timestamp:` to frontmatter.
- Standardize folder entry files on `index.md` — project folders currently use
  `README.md`, and `README.md` is read directly by
  `ciao/web/project_chats.py` (`_iter_vault_entries`), so this is a real change,
  not a rename.
- Emit a bundle export (`ciao vault-export`?) producing a conformant tarball for
  a workspace or a subtree.

The `index.md` / `log.md` reserved filenames and the `type`-required rule are
already satisfied.

---

*Line references verified against the working tree on 2026-08-19. `ciao/vault_index.py` was under concurrent edit during drafting — re-anchor by symbol name if a number looks off.*
