# Vault migration by prompt

Hand this to a capable model, in a chat with shell access, inside the install you
want migrated. It moves a shared-layout vault (`memory-vault/<workspace>/…`) to
the per-workspace layout (`<workspace>/memory-vault/…`).

**Who this is for.** Installs created before the per-workspace layout. A fresh
`ciao setup` already builds the new layout directly, so a new install never needs
this. If `ciao workspace-reroot --workspace .` reports `already_migrated`, stop —
there is nothing to do.

**Why a prompt and not a command.** Deciding *what goes where* — which note
belongs to which workspace, how to split a shared `CLAUDE.md` — is judgement, and
a model is good at it. The steps below exist because the *execution* is where a
model is unreliable, and each one names the specific failure it prevents.

`ciao workspace-reroot --apply` still exists and does all of this atomically with
a receipt and an undo. Prefer it when it works. Use this prompt when it refuses
on input it cannot classify, or when you want to decide the mapping yourself.

---

## The prompt

> You are migrating a Ciaobot vault from the shared layout to the per-workspace
> layout. Work in the install root. Follow these steps in order and do not skip
> the verification.
>
> **1. Back up before touching anything.**
> The vault must be a git repository with a clean tree, because `git checkout` is
> the only undo you have.
> ```
> git status --porcelain          # must be empty; commit or stash first
> git add -A && git commit -m "before vault migration"
> ```
> If the directory is not a repository: `git init && git add -A && git commit`.
> Do not proceed without a commit to return to. Add `.env`, `secrets/` and
> `.runtime/` to `.gitignore` first — a snapshot must not capture credentials.
>
> **2. Read the census; do not re-derive it.**
> ```
> ciao workspace-census
> ciao os-audit
> ```
> Use these as your input. The census names every top-level directory, loose
> non-markdown files, duplicate note names, symlinks and unregistered
> directories. Anything it lists that you cannot classify is a question for the
> operator, not a guess.
>
> **3. Move with `git mv`, never `mv`.**
> For each workspace `W` registered in `.runtime/workspaces.json`:
> ```
> mkdir -p W && git mv memory-vault/W W/memory-vault
> ```
> Then promote the shared, workspace-independent directories:
> ```
> git mv memory-vault/Logs Logs
> git mv memory-vault/Templates templates-src
> ```
> `git mv` keeps history attached to the file; plain `mv` looks identical
> afterwards and loses it silently. **An empty directory cannot be `git mv`d**
> ("source directory is empty") — git does not track empty directories, so there
> is nothing to move: `rmdir` it and move on.
>
> **4. Re-point every inbound reference.** This is the step most likely to break
> things quietly, so do it deliberately. A note that moved to another workspace is
> now referenced across a root, and references come in three dialects that all
> need updating:
> - wikilinks — `[[Mo]]`, `[[People/Mo]]` → `[[work/People/Mo]]`
> - frontmatter — `related: People/Mo` → `related: work/People/Mo`
> - markdown links — `[Mo](./Mo.md)` → a **relative path recomputed on disk**,
>   e.g. `../../../work/memory-vault/People/Mo.md`
>
> Rules that matter:
> - A reference from a note in the *same* workspace stays root-relative
>   (`People/Mo`). Only a reference that now crosses a workspace gains the
>   workspace prefix.
> - Recompute markdown links against the real directories, not against the
>   logical name. The path has one more level in it than you expect
>   (`<workspace>/memory-vault/…`).
> - **Leave a reference you cannot resolve alone.** Rewriting a link that was
>   already broken turns one broken link into a differently broken one and buries
>   the original.
> - `MEMORY.md` at a vault root is *not* regenerated. If it links to a note that
>   moved, fix it by hand.
>
> **5. Split the shared `CLAUDE.md`.** Each workspace root needs its own guide.
> The bounded regions look like this and the markers must appear **exactly once**
> per region per file:
> ```
> <!-- ciao:memory:start cap=3000 -->
> - a durable fact
> <!-- ciao:memory:end -->
> <!-- ciao:profile:start cap=1375 -->
> - an identity fact
> <!-- ciao:profile:end -->
> ```
> Duplicated or missing markers are the classic failure here and they make the
> region unwritable — every later memory write fails. Give the shared regions'
> entries to the workspace they actually describe; leave the other workspace's
> regions present but empty. Then link `AGENTS.md` to `CLAUDE.md` in each root
> (`ln -s CLAUDE.md AGENTS.md`) so the remaining providers share the guide.
>
> **6. Tell the install it is migrated, then rebuild what is derived.** Run the
> commands; do not hand-edit.
> ```
> ciao workspace-reroot --mark-migrated   # records the layout; verifies it first
> ciao workspace-reroot --repair          # per-root INDEX.md, VOCABULARY.md, search index
> ciao sync-skills                        # per-root .claude/ and .opencode/ catalogs
> ```
> The first command is not optional and must come first: `agent_root()` answers
> "per-root" only when a receipt says so, and `--repair` refuses without one. It
> checks that every registered workspace really has its `<workspace>/memory-vault`
> directory and refuses if one is missing, so it cannot record a layout you have
> not finished building.
> The search index and the per-root indexes are generated files. Editing them by
> hand produces something that looks right and answers from the wrong paths.
>
> **7. Verify, and only then say you are done.**
> ```
> ciao os-audit
> ```
> Paste the whole output. It is the safety net that replaces the migration
> engine's receipt, and it specifically catches the two things step 4 and step 5
> get wrong:
> - `vault_hygiene.broken_markdown_links` — a reference you did not re-point
> - `memory_hygiene.marker_errors` — a region you split incorrectly
> - `search_index.missing` / `search_index.stale_rows` — a rebuild you skipped
>
> Do not report success while any of those is non-empty. If something is wrong and
> you cannot fix it, `git reset --hard` back to step 1's commit and say so.

---

## What this does not do

- **The receipt is not optional.** `agent_root()` answers "per-root" only when
  `.runtime/migration/workspace-rooting.json` says `status: migrated`, and every
  layout-dependent path reads that — the receipt is the layout discriminator, not
  migration bookkeeping. Step 6's `--mark-migrated` writes it, and records
  `origin: hand` so the receipt does not claim a migration that never ran.
- **It does not touch your own scripts.** Anything of yours that hardcodes
  `memory-vault/<workspace>/…` breaks — the path is now
  `<workspace>/memory-vault/…`. Grep for it:
  `grep -rn 'memory-vault/' --include='*.py' --include='*.sh' --include='*.json' .`
- **It does not move chat history.** Provider sessions are keyed on the working
  directory they ran in, so chats from before the migration keep their transcripts
  under the old path. Ciaobot reads both; nothing to do.
