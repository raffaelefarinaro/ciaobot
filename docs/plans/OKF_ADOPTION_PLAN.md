# OKF adoption: hard swap to markdown links

Status: proposed, not started
Written: 2026-08-19 (revised same day after reading OKF v0.2 SPEC.md)
Decision: **hard swap.** Markdown links become the only cross-link dialect.
No dual-dialect reader, no permanent fallback. Existing vaults are converted by
a one-shot migration offered at update, at vault check, and at onboarding onto
an existing folder.

Scope: `ciao/vault_index.py`, `ciao/vault_lint.py`, `ciao/web/routes_api.py`,
`ciao/context/entity_tagger.py`, `ciao/insights.py`, `ciao/observability/hooks.py`,
`ciao/setup_status.py`, `ciao/config.py`, `ciao/cli.py`, `ciao/fts_search.py`,
`ciao/stock/*`, `web/src/lib/wikilinks.ts`, `web/src/lib/safeMarkdown.ts`,
`web/src/components/FileViewerModal.vue`, `PinnedFilePanel.vue`, tests.
New: `ciao/vault_migrate_links.py`, `ciao/okf.py`.

---

## 0. What the spec actually requires (read this first)

The previous revision of this plan was built on the premise that OKF *requires*
markdown links, and treated the link dialect as a conformance gap. That is
wrong, and the correction matters because it changes the justification (not the
decision). From [`SPEC.md`](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
v0.2, verbatim:

- "Concepts **MAY** link to other concepts using standard markdown links."
  Wikilinks are never mentioned, and never forbidden.
- "An `index.md` file **MAY** appear in any directory"; "A `log.md` file **MAY**
  appear at any level."
- "`type` is the only always-required key; a concept carrying just `type` is
  fully conformant."
- "Consumers **MUST** tolerate broken links: a link whose target does not exist
  in the bundle is not malformed."

So the vault is *already* conformant: it enforces non-empty `type`
(`ciao/vault_lint.py:150`) and nothing else is mandatory. **The hard swap is not
a conformance fix.** Its real payoff is narrower and worth stating honestly:

1. **The graph travels.** Today a third-party OKF consumer reading the vault
   sees notes but no edges — a wikilink is opaque body text to anything that
   isn't Obsidian or Ciaobot. After the swap, the link structure is machine-
   readable by any consumer, which is the whole point of a portable bundle.
2. **One dialect, one parser.** Wikilinks and markdown links are currently
   parsed by different code with different rules (see §1). Collapsing to one
   removes a standing source of divergence.
3. **The Obsidian-specific guidance can be deleted** (§6).

What the swap does *not* buy: conformance (already have it), a type vocabulary
(OKF declines to specify one — see §8), or provenance (independent, see §7).

## 1. The actual starting state

Worth pinning down precisely, because the old inventory blurred it. The two
dialects are **not** symmetrical today:

| | wikilinks `[[Foo]]` | relative markdown links `[Foo](./Foo.md)` |
|---|---|---|
| Memory Map edges (`scan_vault`) | yes (`vault_index.py:308`) | **no** |
| Backlinks tab (`_links_in`) | yes (`vault_lint.py:136`) | **no** |
| File viewer navigation | yes (`a.file-link`, `wikilinks.ts:140`) | **no** — renders as an inert `<a href>` |
| Broken-link linting | yes (`broken_links`) | yes (`broken_markdown_links`, `vault_lint.py:585`) |
| Emitted by tagger / insights / hooks | yes | no |

Markdown links are **validated but not honoured**. That asymmetry is the real
scope of the swap: every "yes" in column 1 has to move to column 2, and the
third row is the one the old plan missed entirely (§5.1).

Also true today, and unaffected: no `![[embed]]` syntax exists anywhere in the
codebase, so there is no transclusion case to handle.

## 2. Target link form: relative, not bundle-relative

OKF permits both `[Foo](/dir/Foo.md)` (bundle-relative) and `[Foo](./Foo.md)`
(relative). **Choose relative.** Two reasons, one of them decisive:

- A relative link resolves when the vault is opened in Obsidian, on GitHub, or
  in any plain markdown editor. A leading-`/` link resolves in an OKF consumer
  and nowhere else. Since "your notes stay browsable in your own tools" is the
  property users actually feel, relative wins.
- **The linter already refuses to validate absolute targets.**
  `_markdown_link_error` returns `None` for anything starting with `/`
  (treated as non-local), so bundle-relative links would be silently exempt
  from broken-link checking — we would lose the `broken_markdown_links`
  coverage we already have. Relative links are checked.

Cost of relative form: a note's outgoing links break when the note moves. Accept
it; note-moving already breaks `related:` frontmatter the same way.

### 2.1 Transformation table

| Wikilink | Becomes | Note |
|---|---|---|
| `[[People/Mo]]` | `[Mo](./People/Mo.md)` | label = last segment, matching `wikilinks.ts:135` |
| `[[People/Mo\|Mo Salah]]` | `[Mo Salah](./People/Mo.md)` | alias becomes the label |
| `[[People/Mo#History]]` | `[Mo](./People/Mo.md)` | **anchor dropped** — see below |
| `[[#Heading]]` | *left alone* | pure in-page anchor; `_extract_body_wikilinks` already skips these |
| `[[Nowhere]]` (unresolvable) | `[Nowhere](./Nowhere.md)` | best-effort path; OKF explicitly tolerates broken links |

**Dropping anchors is not a regression.** The frontend already parses the anchor
group and then discards it (`_anchor` is unused in `linkifyPlainSegment`,
`wikilinks.ts:132`), and every resolved wikilink renders as `href="#"` with the
real target in `data-file-path`. Nothing scrolls to headings today. Record
dropped anchors in the migration receipt so the information isn't lost, and
revisit only if the viewer learns anchor scrolling.

**Unresolvable refs get converted anyway.** Leaving them as `[[...]]` would
defeat the hard swap and leave a permanent second dialect. They were already
reported as `broken_links`; after migration they report as
`broken_markdown_links`. Same count, different bucket — say so in the release
notes so the number moving doesn't read as a regression.

### 2.2 Frontmatter `related:` stays bare

Do **not** put markdown links in YAML. `related: [Mo](./People/Mo.md)` is just a
string to YAML, and `_normalize_related_value` (`vault_index.py:183`) would hand
the literal text to `_resolve_related`, which fails. OKF has no opinion on
frontmatter link syntax.

Target form is the bare vault-relative ref that already works:
`related: [People/Mo]`. The migration should normalize any `[[People/Mo]]` values
found in frontmatter to bare refs, and only *then* may the wikilink branch of
`_normalize_related_value` be deleted.

## 3. Pre-requisite: fix the reserved-filename casing bug

Ship this first, on its own, independent of OKF. The old plan had this backwards:
it claimed the linter's casing diverges, when the linter is the one place that
gets it right (`file.relative.name.lower() in _FRONTMATTER_EXEMPT`,
`vault_lint.py:150`). The case-*sensitive* comparisons are elsewhere:

- `ciao/vault_index.py:241` — `if rel_from_vault.name in {"INDEX.md", "MEMORY.md"}`
- `ciao/fts_search.py:200` — `exclude_files={"INDEX.md", "MEMORY.md"}`
- `ciao/vault_lint.py:504` — same literal set

A lowercase `index.md` — exactly what OKF names, and what an agent or an
imported bundle will produce — therefore gets indexed as an ordinary note, and
becomes a god-node in the Memory Map: the failure `_plain_ref` exists to prevent
(`vault_index.py:658`). Casefold all three comparisons.

Note what this makes unnecessary: **no rename is required.** `index.md` is a
MAY. Keeping `INDEX.md`/`MEMORY.md` on disk is conformant, and avoids an atomic
rename on a case-insensitive filesystem. Drop that work from the plan; keep only
the casefold fix, which is strictly a bug.

### 3.1 One free win while we're here

`format_md` renders index entries as backticked non-links via `_plain_ref`,
specifically to avoid a god-node in Obsidian's graph. Once we're not optimizing
for Obsidian's graph view, those can become real markdown links — which is what
OKF's `index.md` progressive disclosure wants anyway. This is safe: `scan_vault`
skips `INDEX.md` (`vault_index.py:241`), so it never becomes a Ciaobot node
either. Also delete `_wikilink` (`vault_index.py:648`) — it has no call sites.

## 4. The migration

One implementation, three entry points, always explicit.

### 4.1 `ciao/vault_migrate_links.py`

A pure `rewrite_note(text, source_path, filename_index) -> (new_text, changes)`
plus a driver. It must reuse, not reimplement, the existing skip logic:
`FENCED_CODE_RE`, `INLINE_CODE_RE`, `vault_lint._is_escaped` (so `\[[...]]`
survives), and `_resolve_related`/`_build_filename_index` for target resolution.

Never touch: `Logs/`, `Templates/`, `.obsidian/` (all three already excluded by
`EXCLUDED_TOP_DIRS`, `EXCLUDED_VAULT_DIRS`, and `vault_lint.py:54`), and
`INDEX.md` (regenerated anyway).

### 4.2 Receipt and reversibility

Follow the existing idiom in `ciao/macos_service.py`: a JSON receipt written
atomically via a `.tmp` sibling and `os.replace` (`macos_service.py:589-591`),
with presence of the receipt as the idempotency check (`already_migrated`,
`macos_service.py:511`).

```
<runtime_root>/migration/vault-links.json
  { schema: 1, migrated_at, vault_root, git_head_before,
    files_scanned, files_rewritten,
    rewrites: [{path, line, from, to}],      # exact reverse map
    unresolved: [...], anchors_dropped: [...] }
```

Because every rewrite is recorded, `ciao vault-unmigrate-links` is an exact
inverse, not a guess. That is the safety net that makes a hard swap defensible.

Safety rails on the CLI: `--dry-run` prints a diff and is the **default**;
refuse to run on a git-dirty vault without `--force`; refuse to run twice unless
`--force` (receipt present).

### 4.3 The three triggers

| Trigger | Hook point | Behaviour |
|---|---|---|
| **On update** | server startup, which already runs `vault-index` (`entity_tagger.py:3`) | **detect and surface only.** Receipt absent + wikilinks present ⇒ raise a setup/health notice with the exact command. Never rewrite. |
| **On vault check** | `ciao vault-lint`, and a finding in `os_audit` | report unmigrated state as a hygiene finding, so the weekly routine surfaces it; `ciao vault-lint --migrate-links` performs it |
| **Onboarding an existing folder** | `detect_vault_mode() == "existing"` (`cli.py:563`) | offer it in the onboarding chat — `cli.py:836-838` already anticipates exactly this ("the onboarding chat can inspect them before proposing a migration") |

**One deliberate deviation from "hard swap, no fallback":** the update trigger
detects and offers; it does not silently rewrite. Rewriting a user's own notes
without a confirmation is not a decision an upgrade should make on their behalf,
and an unattended rewrite has no good failure story on a vault that isn't in
git. The dialect still ends up fully swapped — the prompt is unavoidable at
every entry point, not dismissible-and-forgotten. If you want it truly
automatic on update, say so and I'll make startup migrate directly when the
vault is a clean git repo (recoverable) and prompt otherwise.

## 5. Code changes

### 5.1 Frontend — the highest-risk item

`wikilinks.ts` is **repurposed, not deleted.** Rename to `vaultLinks.ts`.

- Keep: `joinRelative`, `docDirFor`, `buildMarkdownIndex`, and
  `resolveWikilinkTarget` (rename `resolveVaultLinkTarget`) — resolving a
  relative href needs the same machinery as resolving a wikilink ref.
- Delete: `WIKILINK_RE`, `extractWikilinks`, `linkifyWikilinksInMarkdown`.
- **Add — do not skip this:** a `link()` renderer override in
  `renderFileMarkdown` (`safeMarkdown.ts:79`). Today the only overridden
  renderer is `image()`. A converted link renders as a plain
  `<a href="./People/Mo.md">`, and `onMdClick` only intercepts `a.file-link`
  (`FileViewerModal.vue:947`) — so **clicking a migrated link would do nothing
  useful.** The override must resolve in-vault relative hrefs and emit
  `<a class="file-link" href="#" data-file-path="...">`, matching what
  `wikilinks.ts:140` emits now. External and non-vault links pass through
  unchanged.
- Preserve the unresolved-link affordance: emit the existing
  `<span class="wikilink-unresolved">` (rename the class) when resolution fails,
  so a dangling link stays visibly non-clickable instead of becoming a dead
  anchor.

Consumers to update: `FileViewerModal.vue:413`, `PinnedFilePanel.vue:296`.

### 5.2 Backend

| File | Change |
|---|---|
| `ciao/vault_index.py` | `_extract_body_wikilinks` → `_extract_body_links` (parse relative markdown destinations); `_strip_body_wikilinks` (note-delete) likewise; drop the wikilink branch in `_normalize_related_value` (after §2.2); delete dead `_wikilink`; `_plain_ref` → real links (§3.1); casefold the reserved-name check |
| `ciao/vault_lint.py` | `_links_in` yields markdown destinations (the machinery exists — `_inline_markdown_destinations`, `_REFERENCE_DESTINATION_RE`); retire `WIKILINK_RE`; fold `broken_links` into `broken_markdown_links` |
| `ciao/web/routes_api.py` | `_references_note`/`vault_backlinks` follow `_links_in`; `vault_graph` follows `scan_vault` (no direct change); note-delete stripping follows `_strip_body_wikilinks` |
| `ciao/context/entity_tagger.py` | emit `[Name](./People/Name.md)` instead of `[[People/Name]]` |
| `ciao/insights.py` | change the model instruction to write markdown links |
| `ciao/observability/hooks.py` | same, for the `[[People/Name]]` bullets |
| `ciao/fts_search.py` | casefold `exclude_files` only (body text is dialect-agnostic) |
| `ciao/cli.py` | new `vault-migrate-links` / `vault-unmigrate-links`; `--migrate-links` on `vault-lint`; onboarding offer; keep the `.obsidian/workspace*` gitignore entry (`cli.py:494`) — users may still open Obsidian |

Unchanged: `ciao/os_audit.py`, `memory_audit.py`, `memory_proposals.py`,
`project_doc_update.py`, `setup_status.py`, `config.py` — these read
`type`/`related`/frontmatter and are unaffected by body link syntax. (The old
plan listed `setup_status.py` and `config.py` as needing lowercase renames;
§3 removes that work.)

### 5.3 Docs and stock assets

`ciao/stock/skills/workspace-authoring/SKILL.md` (drop the wikilink rule),
`ciao/stock/agents/memory.md`, `ciao/stock/schedules.json` (the hygiene prompt
says "dead wikilinks"), `weekly-review-template.md` (same),
`ciao-capabilities/SKILL.md`, `README.md`, `CIAO_CUSTOMIZATION.md`,
`docs/ARCHITECTURE.md`.

### 5.4 Tests

Concentrated, not diffuse — `tests/test_vault_index.py` (25 wikilink
occurrences) and `web/src/lib/wikilinks.test.ts` (7) are most of it, then
`test_vault_lint.py` (7), `test_vault_backlinks.py` (6), `test_cli.py` (4).
The remaining hits are incidental (`[[` inside unrelated fixtures).

Add: round-trip tests for the migration (migrate → unmigrate → byte-identical),
a code-fence/escaped-link non-rewrite test, an unresolvable-ref test, and a
frontend test that a converted link is clickable — that last one is what would
have caught §5.1.

## 6. What we give up

- **Obsidian `[[` autocomplete, native backlinks, and graph tuning.** Not
  access: Obsidian renders relative markdown links fine, so a converted vault
  still opens and browses. Ergonomics only, and this is the deliberate trade.
- **Obsidian plugins/templates that key off wikilink syntax.**
- Nothing else. `.obsidian/` is already excluded everywhere and gitignored.

## 7. Provenance fields: take them separately

`generated`, `verified`, `status`, `stale_after`, `sources`, `resource` are all
optional additive frontmatter and share no code with the link swap. Ship them as
their own change, justified on their own merits: the vault is agent-written and
currently cannot answer "where did this come from, how trusted is it, is it
still current". OKF standardizes the *spelling*, which is a tiebreaker on
naming, not a reason to adopt.

Correct one claim from the old plan while doing it: **nothing in code reads
`updated:` for staleness.** The ">90 days" rule is prose in
`weekly-review-template.md` judged by the model; `os_audit`'s
`stale_path_entries` is about broken paths, not dates. So `stale_after` does not
"replace an ad-hoc heuristic" — it would add enforcement that does not exist.
That is a better argument, and a different one.

`okf_version` belongs **only** on a bundle-root `index.md` ("a bundle-root
`index.md` MAY carry an `okf_version` key"), not on every note.

Non-goal: `type: Attested Computation` (SPEC §10). Nothing in Ciaobot produces
attested computations; leave it unimplemented rather than half-modelled.

## 8. Relationship to VAULT_VOCABULARY_PLAN

`VAULT_VOCABULARY_PLAN.md` kept OKF out of scope for type/tag sprawl. That
conclusion stands, and for a stronger reason than that plan knew: OKF requires
only `type`, with no controlled vocabulary at all. It therefore neither fixes
sprawl nor constrains how we fix it. The two plans need no coordination beyond
not colliding on frontmatter keys.

## 9. Sequencing

1. **Casing bug** (§3) — standalone, ships now, fixes a live god-node bug.
2. **`ciao/vault_migrate_links.py` + receipt + both CLI commands** (§4), with
   `--dry-run` default. Nothing consumes it yet; no user-visible change.
3. **The reader swap** (§5.1 + §5.2) as one coherent PR — frontend renderer
   override, `_links_in`, `scan_vault`, backlinks, note-delete, tagger,
   insights, hooks. The features must be markdown-link-clean *before* any note
   is rewritten.
4. **Wire the three triggers** (§4.3) and update docs/stock (§5.3).
5. **Migrate the reference vault**, delete the Obsidian guidance.
6. Provenance fields (§7) and `ciao/okf.py` export, independently, later.

## Traps

- **The `marked` `link()` override is mandatory, not optional.** Skip it and
  every migrated link silently stops navigating — the viewer only intercepts
  `a.file-link` (`FileViewerModal.vue:947`). This is the single most likely way
  to ship a broken swap.
- **Don't ship the reader swap and the note migration together.** Step 3 before
  step 5, or the graph/backlinks/viewer go dark mid-release.
- **Don't put markdown links in frontmatter `related:`** (§2.2) — YAML sees a
  string and `_resolve_related` fails.
- **Don't add `resource`/`generated` as `DIR_TYPE_MAP` keys.** `_workspace_of`
  tests membership in its *keys* to distinguish a type-folder from a workspace
  name (`vault_index.py:108-119`); a new key silently breaks workspace
  inference.
- **Don't rewrite inside code fences, inline code, or escaped `\[[`.** Reuse
  `FENCED_CODE_RE`/`INLINE_CODE_RE`/`_is_escaped` rather than writing a third
  variant of the skip logic.
- **Don't migrate `Logs/`, `Templates/`, `.obsidian/`** — excluded from index,
  lint, and FTS; they should stay untouched.
- **Don't relax vault-lint to OKF's permissive conformance.** Consumers MUST
  tolerate broken links; producers may be stricter. Strictness is a superset,
  not a violation.
- **Don't rename `INDEX.md`/`MEMORY.md`.** `index.md` is a MAY; the rename buys
  nothing and risks a case-insensitive-filesystem collision.

## Open questions

- Should the update trigger migrate automatically on a clean git vault rather
  than prompt? (§4.3 deviation — currently specified as detect-and-offer.)
- Do we ever need `ciao vault-import`? With the swap done, an OKF bundle is
  readable in place, so import reduces to a copy.
- Is `ciao/okf.py` export still worth building, given the swapped vault is
  already a conformant bundle? Probably just `okf_version` on a root `index.md`
  plus a tarball helper.
- Anchors: record-and-drop now (§2.1), or teach the viewer anchor scrolling
  first and preserve them?

---

*Revised 2026-08-19 against OKF v0.2 SPEC.md, read directly rather than from the
blog post. Symbol references verified against the working tree at revision time;
re-anchor by name — `vault_index.py` / `routes_api.py` are under active
development.*
