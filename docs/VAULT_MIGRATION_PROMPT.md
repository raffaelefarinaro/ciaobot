# Vault migration

An install created before the per-workspace layout keeps every workspace in one
shared vault (`memory-vault/<workspace>/…`). The per-workspace layout gives each
registered workspace its own agent root (`<workspace>/memory-vault/…`). This
document is how one install gets from the first to the second.

Every step below is a `ciao` command. Nothing here moves a file, edits a
registry or rewrites a guide for you, and nothing here asks you to stage your
whole tree into a commit — the migration commands do the work, refuse when they
cannot classify something, write a receipt, and can be reversed from that
receipt. (The filename is historical: older revisions of this document taught
the manual steps, and `ciao workspace-reroot --mark-migrated` still names it as
the reader for an install that was migrated by hand.)

You can run these yourself, or hand this file to a Ciaobot chat with shell
access. Nothing here touches a real vault unless you pass `--apply` (or
`--mark-migrated`).

**Who this is for.** Installs created before the per-workspace layout. A fresh
`ciao setup` already builds the new layout, so a new install never needs this.
**If Home shows no `workspace-unmigrated` card, stop** — see *Was it already
migrated?* at the end of Step 1.

**What still needs a person.** The commands handle the mechanical half —
moving files, repointing references, rebuilding derived indexes. Three
decisions stay yours, and each one names itself when you get there: which
directory a workspace's notes belong in (a refusal listing it as *unclassified*
or *vault_missing*), which canonical type a retired frontmatter type maps to
(the *unresolved* list), and which workspace a person belongs to (notes queued
to `Workspace/Memory-Proposals.md`). Resolve those with the operator; do not
guess, and do not re-derive any of it by hand.

---

## What is telling you to do this

Home's housekeeping strip and `ciao os-audit` report the same conditions as
pending actions. **Exactly one of them is mandatory.**

| Condition | Mandatory? | Command |
|---|---|---|
| `workspace-unmigrated` — "Workspaces are still sharing one vault" | **Yes.** Blocking: unmissable, and it cannot be hidden. | `ciao workspace-reroot --apply` |
| `vault-location:<workspace>` — "The `<name>` vault is not in its standard folder" | No | `ciao vault-relocate <name>` |
| `unmigrated_vault_links` — the vault may still use `[[wikilinks]]` | No | `ciao vault-migrate-links` |
| `unrehomed_people` — person notes may be filed in the wrong workspace | No | `ciao vault-rehome` |
| `vault-vocabulary` — some notes use a retired `type:` vocabulary | No | `ciao vault-migrate`, then decide what is left |

`workspace-unmigrated` is a precondition this install cannot get past on its
own, which is why it is prominent, permanent and retryable rather than
dismissible. It is deliberately *not* an app-wide lock: the realistic cause is
an uncommitted vault, and a lock would take away the assistant you need to fix
it. Everything else is a suggestion you may decline — `ciao os-audit` reports
the optional ones under "Upgrade Actions (optional)" and counts them in
`pending_action_count`; they never turn the audit red.

**The automatic attempt.** Ciaobot already tries the re-rooting by itself at
every start, before anything reads the vault. So a `workspace-unmigrated` card
means that attempt **refused**, and the card's detail is the refusal list
(three reasons at a time, with a count for the rest). The refusals and the fix
are in Step 1, below.

---

## Step 0 — read the state before you change anything

Both of these are read-only.

```bash
ciao workspace-reroot --workspace .   # the plan: every move, every refusal
ciao os-audit                         # the whole hygiene report
```

The plan is the important one. It classifies every top-level path under the
vault into exactly one of four reported buckets — **moved**, **kept global**,
**regenerated**, **ignored** — and whatever is left over lands in
*unclassified*, which is a refusal. It never writes and never guesses.

The plan output also names the promotions, listed among the moves with no
workspace attached: `Logs/` stays at the install root as `Logs/`, `Templates/`
becomes `templates-src/`, and `.obsidian/` becomes the install root's.
`INDEX.md`, `MEMORY.md` and `VOCABULARY.md` are *regenerated*, so they are
replaced rather than moved.

To survey a vault root you are not sure about — counts per directory,
symlinks, max depth, duplicate stems, unregistered directories — use
`ciao workspace-census --vault-root <path>`. It is read-only and always exits
`0`: an unregistered directory is information, not a failure.

## Step 1 — mandatory: separate the workspaces

```bash
ciao workspace-reroot --apply --workspace .
```

**Stop the app first, and run it from the engine that will serve the install.**
The migration moves the vault and writes the chat state file, so a live server
would both read a path that no longer exists and overwrite the handover flags
from its in-memory copy. An older engine has no per-root vault resolution at
all and would boot with no vault, so an `--apply` against the wrong engine is
the one genuinely destructive mistake available here.

### What it refuses on, before touching anything

`--apply` is all-or-nothing, because a half-rooted install has no filter over a
still-prefixed index and would make every entity visible in every session. It
refuses outright when:

- the plan refuses: no vault directory, no registered workspace, a registered
  workspace with no vault directory, a destination that already exists and is
  not empty, or anything unclassified (an unregistered directory, a loose
  unrecognised file, a symlink);
- a tracked file under the vault has uncommitted modifications — **this is the
  gate working.** Every move is a `git mv`, and `git checkout` is the undo, so a
  release does not rewrite someone's layout on top of unsaved work;
- the install has no git history to roll back to and one could not be created
  (no `git` binary, or the snapshot commit failed).

Clear the refusals, re-run the plan, and watch the list shrink. The ones that
need you rather than a file move: a symlink or unregistered directory, and a
non-empty destination. Nothing is guessed about your own notes.

**About backups.** You do not need to take one by hand. The command calls
`ensure_rollback_history` first, which leaves a repository with at least one
commit completely alone, gives a repository with no commits a snapshot commit,
and creates a repository plus a `.gitignore` when there is none. That snapshot
excludes `.runtime/`, `.env`, `.credentials`, `secrets/`, `node_modules/`,
`.venv/`, `.DS_Store` and `.obsidian/workspace*` — a safety net must not commit
your provider keys. What the gate needs from *you* is that your own
uncommitted work is committed or stashed, with a message that says what it is.
Do not stage the whole tree to satisfy the check: `git add -A` commits whatever
happens to be lying around, and the vault history is the thing you will want to
read if a migration goes wrong.

### What it does when it runs

Beyond the vault moves: it repoints the workspace registry, moves the shared
guide (`AGENTS.md`) into the primary root with `git mv` so its bounded memory
regions stay byte-identical and history follows, writes every other root the
same unbounded body with **empty** bounded regions (the primary's region
entries are queued into that root's `Workspace/Memory-Proposals.md` as one
parseable bullet each — nothing lost, nothing guessed), moves the skill
catalog to the primary root with a blank `Workspace/Skill-Triage.md` sheet and
creates `skills-src/` as the shared mirror source, bootstraps each root's agent
assets, flags every open chat for a context handover (a provider session is
keyed on the directory it started in, so the next turn starts fresh carrying a
context capsule), and rebuilds each root's `INDEX.md`/`VOCABULARY.md` without
the path prefix plus the full-text index, whose every pre-migration row points
at a path that no longer exists.

The result is JSON, and the exit code is `0` only when `status` is `migrated`.

### The receipt is the layout, not bookkeeping

`CiaoConfig.agent_root()` answers "per-root" only when
`.runtime/migration/workspace-rooting.json` says `status: migrated`. Every
layout-dependent path reads that file, so the receipt **is** the discriminator:
without it, files sit in the new layout while the app keeps resolving the old
one, which is the one combination that breaks everything.

### Rehearsing, repairing, undoing

```bash
ciao workspace-reroot --rehearse --workspace .   # record a survey, move nothing
ciao workspace-reroot --repair  --workspace .    # reconcile an already-migrated install
ciao workspace-reroot --undo    --workspace .    # reverse a completed migration
```

- `--rehearse` classifies everything and writes the receipt with the survey's
  result. It moves nothing, and because `read_receipt` gates on
  `status == "migrated"`, a rehearsal can never make the real migration look
  done.
- `--repair` is idempotent and a no-op when the install is already correct. It
  creates a missing root with its agent assets, renames a surviving legacy
  `CLAUDE.md` onto the root's `AGENTS.md` (reported as `legacy_guide` drift),
  re-mirrors packaged, own and shared skills into the root's catalog, rebuilds
  an `INDEX.md` that is absent or still keys entries under a workspace name,
  and drops and rebuilds the search index when an indexed path no longer
  resolves. It **refuses** (`status: not_rerooted`, exit `1`) on an install
  that has not re-rooted: there, a workspace prefix in the shared `INDEX.md` is
  correct and is exactly what the entity-visibility filter reads on, so
  "repairing" it would leave no filter over the index. Three further drifts are
  **reported, never guessed**, and any of them makes it exit `1`:
  `vault_missing` (a root with no vault — which notes belong to it is a
  question about your own material), `guide_unsplit` (a root with no guide
  while the install root still holds the pre-migration one), and `mcp_drift`
  (a `.mcp.json` absent against a shared one — an MCP entry grants live
  credentialed access).
- `--undo` restores the layout to a byte-identical tree. It is CLI only, on
  purpose: reverting the architecture is not a housekeeping button, and it
  exists so the forward migration is provably exact. It refuses without touching
  anything when a bootstrap-created directory does not hold exactly what the
  migration left in it (a file added underneath, a seeded command edited, a
  recorded file deleted) — an architecture rollback must not be the thing that
  destroys those. It does not un-derive the rebuilt `INDEX.md` and
  `VOCABULARY.md`; `git status` names them, and they are rebuilt from the notes
  on the next sync anyway.

### After the separation: the skill catalog and the triage sheet

The migration moves the whole skill catalog to the primary root rather than
copying it into every root — most skills are scoped to one workspace, and a copy
everywhere is the shared catalog rebuilt — and creates `skills-src/` as the
shared mirror source. What it refuses to do is attribute a customised skill to
a root, because guessing hands one workspace's tooling to another. Instead it
writes `Workspace/Skill-Triage.md` **inside the primary workspace's vault**,
listing every non-stock skill with its destination cell blank.

Filling that sheet in is your decision, not a command's: for each skill, say
what it is for and which workspace it belongs to, move the approved ones into
that workspace's `skills/`, and leave the rest where they are. Then mirror the
catalogs:

```bash
ciao sync-skills --workspace <root>   # add --verbose for the per-skill detail
```

`sync-skills` installs and mirrors the local catalog (`skills/`, `commands/`,
`subagents/`) into each root's generated `.claude/` and `.agents/` trees, and it
is also where the vault vocabulary migration of Step 5 is gated, so a run may
report work of its own. It is idempotent; re-run it after editing a catalog.

Two more cards the same migration leaves behind, both optional and both
chat-only, are decided with the operator and then mirrored the same way:

- **"N skill(s) need a workspace"** — the triage sheet is still unanswered.
  Nothing in the app clears it, so it stays until you fill it in.
- **"N GitHub skill(s) are no longer installed"** — `skills-lock.json` is only a
  record now; the GitHub install surface that used to restore the cached copy is
  gone. Copy each cached copy from `.claude/skills/<name>/` or
  `.agents/skills/<name>/` into `skills/<name>/` and re-run `ciao sync-skills`.
  If no cached copy exists, the skill is gone and has to be re-added.

Two drift cards per workspace — "has no folder" and "is missing generated
assets" — are fixed by their run button, which is `ciao workspace-reroot
--repair`: it recreates the root with its assets and re-mirrors the catalog, and
exits `0` once there is nothing left to report. It exits `1` only when the
install has not re-rooted, when it errored, or for one of the three
report-only drifts listed above — never because of these two.

### Already moved by hand: `--mark-migrated`

If an older revision of this document (or a person) already moved the vaults
into the new layout, tell the app, with one command:

```bash
ciao workspace-reroot --mark-migrated --workspace .
```

It moves nothing and writes the receipt with `origin: hand`, so the receipt
does not claim a migration that never ran. It **verifies** first: it refuses
if no workspace is registered, or if any registered workspace has no
`<workspace>/<vault leaf>` directory yet — telling the app a comfortable lie
about a layout you have not finished building is worse than refusing. If a
receipt already exists it prints that there is nothing to do. Then run
`--repair`, which it tells you to, to rebuild the derived files.

### Was it already migrated?

There is no command whose output is `already_migrated`; use the state, not a
string:

- **No `workspace-unmigrated` card on Home, and no `memory-vault/` at the
  install root** — the migration already ran. A plain
  `ciao workspace-reroot --workspace .` on a migrated install refuses with
  `vault root is not a directory: …/memory-vault`, and that refusal is
  expected, not a problem.
- **A `workspace-unmigrated` card** — the automatic attempt refused, and the
  card's detail is why. Go to Step 1.

## Step 2 — optional: put one workspace's vault in its standard folder

Optional, and only for a `vault-location:<workspace>` card: one workspace whose
vault sits somewhere other than its standard per-workspace folder — usually
adopted before that convention, or hand-pinned at an external directory.

```bash
ciao vault-relocate <name>              # the plan; changes nothing
ciao vault-relocate <name> --apply      # move it and repoint the registry
ciao vault-relocate <name> --undo       # reverse the last completed relocation
```

Dry-run by default, and it touches only the named workspace — distinct from
Step 1, which migrates every registered workspace at once. When the vault sits
at some other path entirely, the whole directory is that workspace's own
content and moves as a unit. When the vault *is* the shared vault root, each
top-level entry is classified instead: this workspace's content moves, another
workspace's folder and vault-wide shared state (`Logs/`, `Templates/`, the
generated indexes) are left alone. Every move is a `git mv`, and a refusal is
a refusal, not a suggestion — so when the plan reports anything it **could not
classify** (a symlink, most often), resolve only those with the operator. Do
not ask about the move itself, and do not re-derive it by hand.

Its own receipt lives under `<runtime>/migration/`, kept separate from Step 1's
so neither run's status can be mistaken for the other's, and `--undo` is the
exact `git mv` in reverse. `--apply` refuses before touching anything if the
plan refuses or a tracked file under the source has uncommitted changes.

One refusal is a real limitation rather than something to work around: if the
vault lives outside the install's git worktree — an external or hand-pinned
root — it cannot be moved with `git mv` and there is no automatic undo for it
here. The command says so and stops. That decision is yours, and the registry
it would have edited is yours to change; say so rather than moving the directory
yourself.

**A restart is required after a successful `--apply`** (Settings → Restart)
before the new location takes effect everywhere, including in the chat that
applied it.

## Step 3 — optional: convert `[[wikilinks]]` to markdown links

Optional, for a `unmigrated_vault_links` notice. A wikilink is opaque body
text to anything that is not Obsidian or Ciaobot, so the vault's edges do not
travel with the notes.

```bash
ciao vault-migrate-links            # the plan; changes nothing
ciao vault-migrate-links --apply    # write the conversion
ciao vault-unmigrate-links --apply  # restore the notes byte for byte
```

Every command here is dry-run unless `--apply` is passed. Body wikilinks become
relative Markdown links resolved through the same index the graph used, so a
link the graph could follow becomes a link that points at the same file.
Frontmatter `related:` values are normalised to **bare** refs
(`People/Mo`), never to Markdown links — YAML would hand `[Mo](./People/Mo.md)`
to the resolver as a literal string and the ref would not resolve. Skipped
entirely: `Logs/`, `Templates/`, `.obsidian/`, the regenerated `INDEX.md` and
`VOCABULARY.md` (but *not* the hand-curated `MEMORY.md`, whose links are real
content), anything inside app state or a checked-out venv, and any match inside
a fenced block, an inline code span, or behind an escaping backslash. Anchors
are dropped rather than lost — nothing scrolls to a heading today, and the
anchor is recorded in the receipt so teaching the viewer anchor scrolling later
does not need the notes back. A ref that resolves to nothing is converted to a
best-effort path anyway: a link whose target does not exist is a *broken* link,
which is a `broken_markdown_links` finding, not a malformed one.

Two rails, both real:

- **Reversibility.** `<runtime>/migration/vault-links.json` records every
  rewrite as an exact `(offset, from, to)` triple in the *migrated* text, so
  `vault-unmigrate-links` is an inverse rather than a re-derivation: it walks
  each file's edits back to front, checks the text still reads as the receipt
  says, and restores the original bytes. A file edited since the conversion is
  left entirely untouched. The map may never narrow — a run that could not
  write some note records `status: "partial"`, and each later run adds to the
  map, so every note an earlier run converted stays restorable.
- **Refusals.** A dirty vault git tree, an existing receipt (moved aside, not
  overwritten), or a `--vault-root` nested inside the configured vault — the
  last one refuses in the preview too, because refs resolve against the root it
  is handed, so a workspace subtree would make every link out to the vault's
  shared notes look dead. `--force` overrides the first two.

`ciao vault-lint --migrate-links [--apply] [--force]` is the same conversion
through the linter's entry point, so the receipt and both rails behave
identically.

## Step 4 — optional: re-home person notes

Optional, for an `unrehomed_people` notice — person notes that a global
memory-curation run filed into one workspace's `People/`. The routing bug is
fixed; this moves the backlog.

```bash
ciao vault-rehome                                  # the plan; changes nothing
ciao vault-rehome --apply                          # move what is tag-obvious
ciao vault-unrehome --apply                        # move it all back exactly
```

A note whose tags name another workspace is a mechanical substitution with no
decision in it, so it moves and every reference to it is repointed: wikilinks,
relative Markdown links (recomputed against the linking note's directory,
including the moved note's own outbound links) and frontmatter refs. **A note
with no workspace-naming tag is a judgement call about someone's relationship
to you, and it is never moved** — it is queued in that workspace's
`Workspace/Memory-Proposals.md`, which already has a review surface, and
`--workspace-name` (repeatable) is how you tell it which registered names play
the roles its tags imply. A role that binds to no registered workspace is not a
candidate at all: there is nowhere to move it, and inventing a directory is
worse than leaving it where it is.

`--force` and `--json` behave as in Step 3, and the receipt
(`<runtime>/migration/vault-rehome.json`) is an exact reverse map — moves as
`from`/`to` pairs, rewrites as `(offset, from, to)` triples in the rewritten
text — so `vault-unrehome --apply` is an inverse that leaves a file alone on any
mismatch. It is deliberately *not* gated on a clean vault: the re-homing is what
made it dirty. Proposals already written to a review queue are not withdrawn;
they are yours to resolve. A run that could not move everything says so and
exits `1`, and the reverse map carries forward, so re-running loses nothing
already done.

## Step 5 — optional: settle the frontmatter vocabulary

Optional, for a `vault-vocabulary` tile.

```bash
ciao vault-migrate          # the plan; changes nothing
ciao vault-migrate --apply  # write the renames
```

This one keeps the stock categories that no longer ship (product, feature,
automation, document, reference, content) when notes still carry them, renames
aliased types (`project-log` → `journal`), and reports every type with **no**
canonical equivalent. Only aliased types are applied, and only when the current
value is exactly the alias being replaced, so a hand-edit racing the migration
is not clobbered.

Those unresolved types need you. A type with no alias target is reported and
left alone on purpose: choosing a category is your call, and guessing would
bury a real decision inside a migration. They are listed under `unresolved` in
the receipt — one `vault-vocabulary.<label>-<digest>.json` per vault under
`<runtime>/migration/`, because one runtime root serves every workspace's vault
— and that receipt is what the tile reads. Its chat button asks for the
categorisation decision. Decide the `type:` line per note — and if the run turns
out to have been incomplete for a note written after the migration, re-running
the preview and applying it again re-applies the mechanical renames and rewrites
the receipt, which clears the tile.

Note what this command does **not** have: there is no `vault-unmigrate`. The
renames are gated on an exact current value, so an unwanted one is a one-line
frontmatter edit, and the unresolved types were never touched at all.

## Verify

```bash
ciao os-audit
```

Read-only. Human-readable Markdown by default, `--json` for automation, and the
status *is* the exit code: `0` healthy, `1` reliable findings, `2` the scan
could not inspect what it needed and the report is not a clean bill of health.
Paste the output rather than summarising it, and do not report success while
these are non-empty:

- `vault_hygiene.broken_markdown_links` — a reference nobody re-pointed
- `memory_hygiene.marker_errors` — a bounded region that is malformed
  (duplicated or missing markers make the region unwritable, so every later
  memory write fails)
- `search_index.missing` / `search_index.stale_rows` — derived state that was
  not rebuilt; `ciao workspace-reroot --repair` is the fix for both

`ciao vault-lint` is the faster subset: frontmatter, broken relative Markdown
links, orphan pages and near-duplicate stems, exiting `0` only on a clean
vault.

## The "After this update" tasks

Home has a second, separate group: the **After this update** tasks, driven by
`ciao/update_task_catalog.py` and packaged in `ciao/stock/update-tasks/`. A
task there is a *decision that persists* until somebody reverses it, with a
server-owned prompt, a per-`(id, revision)` record, dismissal, and a reopen in
Settings → Update task history.

**None of the migration notices in this document are catalog tasks today.**
They are operator-action tiles — a *view of the machine*, recomputed on every
render — and `ciao os-audit` reports the findings independently. The tiles
render even when the work has been done, because "completion evidence" for most
of them is only the condition's absence; a catalog task needs a receipt a
completion check can read, and only a managed remedy that writes one has it. The
catalog's one shipped row, `learnings-cleanup` (with its packaged prompt at
`prompts/learnings-cleanup-1.md`), is about `Workspace/Learnings.md` and is
unrelated to layout. If a future release moves a migration notice into that
catalog, it moves with a receipt behind it, and `docs/DEVELOPMENT.md` will say
so — do not assume any of these are offered there.

The `workspace-unmigrated` card is not a catalog task either, and must not
become one: a task is dismissible and revision-suppressed, which is precisely
wrong for a blocker whose whole point is that it does not go away until the
migration runs.

---

## What this does not do

- **It does not move chat history.** Provider sessions are keyed on the working
  directory they ran in, so chats from before the migration keep their
  transcripts under the old path. Ciaobot reads both; nothing to do. Step 1
  flags every open chat for a context handover instead of leaving it to chance.
- **It does not touch your own scripts.** Anything of yours that hardcodes
  `memory-vault/<workspace>/…` breaks — the path is now
  `<workspace>/memory-vault/…`. Grep for it and decide each hit:
  `grep -rn 'memory-vault/' --include='*.py' --include='*.sh' --include='*.json' .`
- **It does not move your MCP configuration.** A root's `.mcp.json` is reported
  as `mcp_drift`, never recomposed: an MCP entry grants live credentialed
  access, and per-root composition does not exist. An earlier attempt at
  inferring reachability silently removed two working integrations from a live
  install, so the decision is yours.
- **It does not re-derive a file's location for you.** A directory the plan
  calls unclassified, a type with no canonical equivalent and a person with no
  workspace-naming tag are all reported and left alone. They are the same three
  questions in every case, and the answer is about your material, not about
  path arithmetic.

For the module-level design, see `workspace_reroot.py`,
`vault_relocate.py`, `vault_migrate_links.py`, `vault_rehome.py` and
`vault_migration.py` in the `## App repo layout` block of
[docs/ARCHITECTURE.md](ARCHITECTURE.md); for the update-task contract, see
[docs/DEVELOPMENT.md](DEVELOPMENT.md).
