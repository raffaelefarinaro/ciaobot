# Vault migration

An install created before the per-workspace layout keeps every workspace in one
shared vault (`<install>/memory-vault/<workspace>/…`). The per-workspace layout
gives each registered workspace its own agent root
(`<install>/<workspace>/memory-vault/…`). This document is how one install gets
from the first to the second, and what to do about the other migration notices
it leaves behind.

Every step below is a `ciao` command. Nothing here moves a file, edits a
registry or rewrites a guide for you, and nothing here asks you to stage your
whole tree into a commit — the migration commands do the work, refuse when they
cannot classify something, write a receipt, and can be reversed from that
receipt. (The filename is historical: older revisions of this document taught
the manual steps, and `ciao workspace-reroot --mark-migrated` still names it as
the reader for an install that was already moved by hand.)

**Do this yourself, from a terminal at the install root.** The app's own
housekeeping cards are the primary path and carry their own buttons for exactly
this work; a Ciaobot chat can explain a condition, but the migration itself is
yours to press. One card in particular says so in its own prompt: *"Do NOT pass
`--apply` … the migration itself is mine to press."*

**Who this is for.** Installs created before the per-workspace layout. A fresh
`ciao setup` already builds the new layout, so a new install never needs this.

**Order matters, and only for Step 1.** Step 1 is the migration; Steps 2–5 are
independent, optional, and each applies to a different condition. No card at
all means Step 1 has nothing to do — it does **not** mean you are finished,
because a Home card and an audit notice are not the same set (see below) and
Steps 2–5 each answer for themselves.

**What still needs a person.** The commands handle the mechanical half — moving
files, repointing references, rebuilding derived indexes. Three decisions stay
yours, and each one names itself when you get there: which directory a
workspace's notes belong in (a refusal listing it as *unclassified* or
`vault_missing`), which canonical type a retired frontmatter type maps to (the
*unresolved* list), and which workspace a person belongs to (notes queued to
`Workspace/Memory-Proposals.md`). Resolve those with the operator; do not guess,
and do not re-derive any of it by hand.

---

## The two surfaces are not the same set

Home's housekeeping strip and `ciao os-audit` both report migration conditions,
and they do **not** cover the same ones. A condition can be on exactly one of
them, so "I ran the audit and it was clean" does not mean Home is empty, and an
empty Home does not mean the audit is.

| Condition | On Home? | In `os-audit`? | Mandatory? | Command |
|---|---|---|---|---|
| `workspace-unmigrated` — "Workspaces are still sharing one vault" | Yes, **blocking** | **No** | **Yes** | Step 1 |
| `vault-vocabulary` — some notes use a retired `type:` vocabulary | Yes | **No** | No | Step 5 |
| `vault-location:<workspace>` — "The `<name>` vault is not in its standard folder" | Yes | Yes, as `vault_outside_vault_root` | No | Step 2 |
| "The vault may still use the retired wikilink dialect" | Yes | Yes, as `unmigrated_vault_links` | No | Step 3 |
| `unrehomed_people` — no re-home has been recorded | Yes, as an **After this update** task (`unrehomed-people`), dismissible, and only when there is something to move | Yes | No | Step 4 |

Two consequences worth stating outright. The one **mandatory** notice
(`workspace-unmigrated`) exists only on Home, so an audit cannot tell you
whether the install still needs separating — check Home. And `unrehomed_people`
is the opposite: it is on both, but the two are **not** the same thing, and they
do not even ask the same question. Home offers a task you may dismiss, reopen
from Settings → Update task history, and finish — and it appears only when
`ciao vault-rehome`'s own plan finds a tag-obvious note to move. `ciao os-audit`
keeps reporting the broader condition whatever that card says, because a dismissed
card is a decision about a card. What stops the audit line is the receipt below —
not the dismissal, and not the card.

**`unrehomed_people` is a receipt check, not a scan, and it can outlive Step 1.**
The audit raises it when the install has **more than one registered workspace**
and there is **no completed re-home receipt** — it does not walk the vault, so it
cannot tell you whether anything is actually misfiled. That has two consequences.
It stays silent on a single-workspace install, where every candidate comes back
with no counterpart to move to and offering a move would be an unactionable tile.
And it is **not** cleared by Step 1: the re-rooting writes no re-home receipt, so
after separating your workspaces the notice is still there, while `ciao vault-rehome`
now has nothing to move — the notes are already in per-workspace vaults and it
plans `<vault>/<workspace>/People` only.

**The Home task is narrower, and it does not survive Step 1 either.** A card is an
offer to do work, so it needs a reason to exist rather than a receipt's absence: it
appears only when the plan finds a **tag-obvious cross-workspace move**, which is
the damage the old global curation run actually did. Untagged contacts are normal
in any vault and are never counted as evidence. Because the moves only exist in the
shared layout, a separated install gets **no task at all** — there is no managed
command that can act there — while the notice keeps reporting.

What clears the audit line on a separated install is a completed receipt, and an
honest no-op run writes one: point the command at a root that exists and let it
record that there was nothing to do.

```bash
ciao vault-rehome --vault-root <install>/<workspace>/memory-vault --apply
```

That is a real run, not a trick: it refuses on a dirty tree, reports "No
tag-obvious misfiled people", and writes `vault-rehome.json` with
`status: migrated`, which is exactly what the audit reads. It is only the route to
settling a Home task when the task was already offered and you decided against the
move — it is never how one comes to be offered. If you would rather not run it, the
notice is a pending action and never turns the audit red — it is informational, not
a defect, and dismissing the Home card leaves it exactly where it was.

`workspace-unmigrated` is a precondition this install cannot get past on its
own, which is why it is prominent, permanent and retryable rather than
dismissible. It is deliberately *not* an app-wide lock: the realistic cause is
an uncommitted vault, and a lock would take away the assistant you need to fix
it. Everything else is a suggestion you may decline — the audit renders the ones
it carries under "Upgrade Actions (optional)" and counts them in
`pending_action_count`; they never turn the audit red.

**The automatic attempt.** Ciaobot already tries the re-rooting by itself at
every start, before anything reads the vault. So a `workspace-unmigrated` card
means that attempt **refused** — or that it has not run yet, or errored, in
which case the card says the move has not run rather than naming a reason. The
card's detail is the refusal list when there is one: up to three reasons, with a
count for the rest. The refusals and the fix are in Step 1.

## Step 0 — read the state before you change anything

Both of these are read-only.

```bash
ciao workspace-reroot --workspace .   # the plan: every move, every refusal
ciao os-audit                         # the whole hygiene report
```

The plan is the important one. It classifies every top-level path under the
vault into one of four reported buckets — **moved**, **kept global**,
**regenerated**, **ignored** — and whatever is left over lands in
*unclassified*, which is a refusal. It never writes and never guesses. The
`kept global` bucket is reported in the plan's JSON but is not itself populated:
the shared promotions appear among the moves with no workspace attached. They
are `Logs/` staying at the install root as `Logs/`, `Templates/` becoming
`templates-src/`, and `.obsidian/` becoming the install root's.
`INDEX.md`, `MEMORY.md` and `VOCABULARY.md` are *regenerated*, so they are
replaced rather than moved.

To survey a vault root you are not sure about — counts per directory,
symlinks, max depth, duplicate stems, unregistered directories — use
`ciao workspace-census --vault-root <path>`. It is read-only and always exits
`0`: an unregistered directory is information, not a failure.

## Step 1 — mandatory: separate the workspaces

There are two ways to run this, and they are the same code.

**From the app (the usual way).** Home's `workspace-unmigrated` card has a
**Separate them now** button. It runs the same entry point the upgrade uses,
inside the running app, and reports back how many moves it made and how many
chats will start a fresh session. Its chat button is for *asking what is
blocking it* — it prints the plan, and the migration stays yours to press. The
card's own prompt is explicit that the agent must not pass `--apply`.

One caution about the live button: it runs while the server is up, and the
migration writes the chat state file — the same thing the CLI path asks you to
stop the app for, because a live server can overwrite the handover flags from
its in-memory copy. Let the run finish before you touch anything else, and do
not start another operation that writes chats until it has reported back.

**From the terminal.** `ciao workspace-reroot --apply --workspace .`, with the
app stopped, and from the engine that will serve the install. An older engine
has no per-root vault resolution at all and would boot with no vault, so an
`--apply` against the wrong engine is the one genuinely destructive mistake
available here. Stopping the app matters because the migration moves the vault
and writes the chat state file: a live server would both read a path that no
longer exists and overwrite the handover flags from its in-memory copy.

### What it refuses on, before touching anything

`--apply` is all-or-nothing, because a half-rooted install has no filter over a
still-prefixed index and would make every entity visible in every session. It
refuses outright when:

- the plan refuses: no vault directory, no registered workspace, a registered
  workspace with no vault directory, a destination that already exists and is
  not empty, or anything unclassified (an unregistered directory, a loose
  unrecognised file, a symlink);
- a tracked file it is about to move has uncommitted modifications — **this is
  the gate working.** The watched paths are the vault directory, the skill
  catalog the run moves, and the shared guide, not only the vault. Every move
  is a `git mv` and `git checkout` is the undo, so a release does not rewrite
  someone's layout on top of unsaved work;
- the primary workspace is not registered, so the guide's regions and the skill
  catalog have nowhere to go;
- the install has no git history to roll back to and one could not be created
  (no `git` binary, a credential that could not be kept out of the snapshot, or
  the snapshot commit failed).

Clear the refusals, re-run the plan, and watch the list shrink. The ones that
need you rather than a file move: a symlink or unregistered directory, and a
non-empty destination. Nothing is guessed about your own notes.

**About backups.** You do not need to take one by hand. The command calls
`ensure_rollback_history` first, which leaves a repository with at least one
commit completely alone, gives a repository with no commits a snapshot commit,
and creates a repository when there is none. Both snapshotting branches first get
a `.gitignore` carrying the exclusions in `_SNAPSHOT_IGNORES` — `.env`,
`secrets/`, `.runtime/` and the rest — so a `.env` or a `secrets/` directory
cannot be committed into the snapshot even when the install already had a git
repository with no commits. The write is additive: an existing `.gitignore` of
yours is kept and only the missing entries are appended.

A `.gitignore` does not unstage anything, so those excluded paths are also
dropped from git's **staging area** before the snapshot is taken — otherwise a
`.env` you had already run `git add` on would go into the commit anyway. Nothing
is deleted from disk: the files stay exactly where they are, they are only left
out of the snapshot, and every other file you had staged is still in it.

That is not taken on trust. A `.gitignore` of your own can re-admit a file the
exclusions removed — a `!` line such as `!.env` placed after them, or a
`sub/.gitignore` containing `!.env`, which applies to everything under `sub/` and
is never read by the exclusion write. So the staging area is read back
immediately before the commit, and **if anything in `_SNAPSHOT_IGNORES` is still
staged the migration refuses** (`unstage_failed`, one of the refusals listed
above) and makes no commit at all. Your files are still on disk either way;
remove or move the `!` line and re-run.

What the gate needs from *you* is that your own uncommitted work is committed or
stashed, with a message that says what it is. Do not stage the whole tree yourself to satisfy the check:
`git add -A` as a backup step commits whatever happens to be lying around, and
the snapshot the command takes is not the whole tree either — the vault history
is the thing you will want to read if a migration goes wrong.

### What it does when it runs

Beyond the vault moves: it repoints the workspace registry, moves the shared
guide (`AGENTS.md`) into the primary root with `git mv` so its bounded memory
regions stay byte-identical and history follows, writes every other root the
same unbounded body with **empty** bounded regions (the primary's region
entries are queued into that root's `Workspace/Memory-Proposals.md` as one
parseable bullet each — nothing lost, nothing guessed), moves the skill catalog
to the primary root and creates `skills-src/` as the shared mirror source,
bootstraps each root's agent assets, flags every open chat for a context
handover (a provider session is keyed on the directory it started in, so the
next turn starts fresh carrying a context capsule), and rebuilds each root's
`INDEX.md`/`VOCABULARY.md` without the path prefix plus the full-text index,
whose every pre-migration row points at a path that no longer exists. It also
writes `Workspace/Skill-Triage.md` inside the primary workspace's vault — see
*After the separation* below.

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
  result. It moves nothing else, and because `read_receipt` gates on
  `status == "migrated"`, a rehearsal can never make the real migration look
  done.
- `--repair` **writes** — it is not one of the read-only commands. It is
  idempotent and a no-op when the install is already correct. It creates a
  missing root with its agent assets, renames a surviving legacy `CLAUDE.md`
  onto the root's `AGENTS.md` (reported as `legacy_guide` drift), re-mirrors
  packaged, own and shared skills into the root's catalog, rebuilds an
  `INDEX.md` that is absent or still keys entries under a workspace name, and
  drops and rebuilds the search index when an indexed path no longer resolves.
  It **refuses** (`status: not_rerooted`, exit `1`) on an install that has not
  re-rooted: there, a workspace prefix in the shared `INDEX.md` is correct and
  is exactly what the entity-visibility filter reads on, so "repairing" it would
  leave no filter over the index. Three further drifts are **reported, never
  guessed**, and any of them makes it exit `1`: `vault_missing` (a root with no
  vault — which notes belong to it is a question about your own material),
  `guide_unsplit` (a root with no guide while the install root still holds the
  pre-migration one), and `mcp_drift` (a `.mcp.json` absent against a shared one
  — an MCP entry grants live credentialed access).
- `--undo` restores the layout to a byte-identical tree. It is CLI only, on
  purpose: reverting the architecture is not a housekeeping button, and it
  exists so the forward migration is provably exact. It refuses without touching
  anything when a bootstrap-created directory does not hold exactly what the
  migration left in it (a file added underneath, a seeded command edited, a
  recorded file deleted) — an architecture rollback must not be the thing that
  destroys those. It does not un-derive the rebuilt `INDEX.md` and
  `VOCABULARY.md`; `git status` names them, and they are rebuilt from the notes
  on the next sync anyway.

### Already moved by hand: `--mark-migrated`

If an older revision of this document (or a person) already moved the vaults
into the new layout, tell the app, with one command:

```bash
ciao workspace-reroot --mark-migrated --workspace .
```

It moves nothing and writes the receipt with `origin: hand`, so the receipt
does not claim a migration that never ran. It **verifies** first: it refuses
if no workspace is registered, or if any registered workspace has no
`<workspace>/<vault leaf>` directory yet, and it names the ones that are
missing. If a receipt already exists it prints that there is nothing to do.
Then run `--repair`, which it tells you to, to rebuild the derived files.

The refusal names the command rather than a manual move: it says which
workspaces have no `<workspace>/memory-vault` yet, points at Step 1's `--apply`
as the thing that does the move, points here for what `--apply` refuses on, and
carries the one caveat the command cannot check for you — that `--apply` has to
run from the engine that will serve this install, with the app stopped. It has
just told you the layout is **not** finished. Either the install is still on the
shared layout, in which case run `--apply` and let the commands do the move; or
the layout is partly built by hand, in which case finish the directories the
refusal listed, re-run `--mark-migrated`, and let `--repair` rebuild the derived
files. What you should not do is re-derive the move yourself: that is the path
this document was rewritten to stop teaching, and it is the path with no
receipt.

### Was Step 1 already done?

There is no command whose output is `already_migrated`; use the state, not a
string:

- **No `workspace-unmigrated` card on Home, and no `memory-vault/` at the
  install root** — the migration already ran. A plain
  `ciao workspace-reroot --workspace .` on a migrated install refuses with
  `vault root is not a directory: …/memory-vault`, and that refusal is
  expected, not a problem.
- **A `workspace-unmigrated` card** — the automatic attempt refused, errored or
  has not run, and the card's detail is why. Go to Step 1.

## Step 2 — optional: put one workspace's vault in its standard folder

Optional, and only for a `vault-location:<workspace>` card or a
`vault_outside_vault_root` notice: one workspace whose vault sits somewhere
other than its standard per-workspace folder — usually adopted before that
convention, or hand-pinned at an external directory.

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
plan refuses or a tracked file under the source has uncommitted changes. This
command has no `--vault-root`: it works from the registry, which is where a
non-standard location is recorded in the first place.

One refusal is a real limitation rather than something to work around: if the
vault lives outside the install's git worktree — an external or hand-pinned
root — it cannot be moved with `git mv` and there is no automatic undo for it
here. The command says so and stops. That decision is yours, and the registry
it would have edited is yours to change; say so rather than moving the directory
yourself.

**A restart is required after a successful `--apply`, and after `--undo`** — the
undo's own result carries the same `restart_note`. Restart (Settings →
Restart) before the new location takes effect everywhere, including in the chat
that ran it.

## Step 3 — optional: convert `[[wikilinks]]` to markdown links

Optional, for the "may still use the retired wikilink dialect" card or an
`unmigrated_vault_links` notice. A wikilink is opaque body text to anything
that is not Obsidian or Ciaobot, so the vault's edges do not travel with the
notes.

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

**Run this before Step 1 if you can.** It takes a `--vault-root`, and on a shared
layout the default (`<install>/memory-vault`) is the right one: refs resolve
against the root you hand it, so a root that does not contain a note the link
points at is reported as a link that was already dead. After Step 1 there is no
install-root `memory-vault/` at all and every root is a separate vault, so you
must name one: `--vault-root <install>/<workspace>/memory-vault`. That converts
that one workspace's notes — and a link from there into another workspace is
then genuinely unresolvable from that root. The receipt is **per install**, one
file at `<runtime>/migration/vault-links.json`, so the first conversion marks
the whole install converted and a second per-root vault is refused as
"already migrated". Treat this as one conversion per install, not one per
workspace.

`--force` overrides exactly three rails, listed first because they are the
things it trades away. Reversibility is a fourth thing and is **not** one of
them — nothing you pass turns it off:

- **An existing receipt** (moved aside, not overwritten). This one protects the
  reverse map of an earlier run, so overriding it discards that undo.
- **A dirty vault git tree.** `git checkout` is supposed to stay an undo that
  needs nothing from the app, so overriding it gives up that.
- **A `--vault-root` nested inside the configured vault.** This is the only rail
  that gates the *preview* as well as the write, and deliberately so: refs
  resolve against the root passed here, so a too-narrow root makes the preview
  itself wrong — working links listed as dead. The other two protect a write, and
  gating their preview would mean reaching for `--force` just to look.

So `--force` is a decision, not a convenience, and all three cost something real
when you spend it. Reach for it only when the operator has decided the rail in
front of you is wrong, and say which one you are overriding.

**Reversibility is not a rail and `--force` cannot touch it.** The receipt
records every rewrite as an exact `(offset, from, to)` triple in the *migrated*
text, so `vault-unmigrate-links` is an inverse rather than a re-derivation: it
walks each file's edits back to front, checks the text still reads as the
receipt says, and restores the original bytes. A file edited since the
conversion is left entirely untouched, and the map may never narrow — a run
that could not write some note records `status: "partial"`, and each later run
adds to the map, so every note an earlier run converted stays restorable.

Note also that `ciao vault-unmigrate-links` accepts `--force` and **ignores**
it — reversing deliberately has no dirty-vault or receipt rail, because a
successful migration is what makes the vault dirty, and every span is
re-checked before it is touched. `ciao vault-unrehome` does the same.

`ciao vault-lint --migrate-links [--apply] [--force]` is the same conversion
through the linter's entry point, so the receipt and all three rails behave
identically.

## Step 4 — optional: re-home person notes

Optional, for an `unrehomed_people` notice — and note from the table above that
this notice reports the *absence of a receipt* rather than scanning for misfiled
notes, while the Home task (`unrehomed-people`) is offered only when this
command's plan has a tag-obvious move in it. Read the section above first: after
Step 1 the notice can be there with nothing for this command to move, no Home task
appears, and the honest no-op run is what clears the notice. Before Step 1 the
command does its real work, and the task appears with it: person notes that a
global memory-curation run filed into one workspace's `People/`. The routing bug
is fixed; this moves the backlog.

```bash
ciao vault-rehome                                  # the plan; changes nothing
ciao vault-rehome --apply                          # move what is tag-obvious
ciao vault-unrehome --apply                        # move it all back exactly
```

**The moves are a shared-layout remedy, so run them before Step 1.** It plans
`<vault>/<workspace>/People`: a note is only examined when it sits under a
workspace segment for a *registered* workspace, because a root's own vault has
no other workspace to be misfiled from. After Step 1 the notes are already in
per-workspace vaults with no workspace segment, so the plan finds nothing —
passing `--workspace-name` does not change that, and a per-root `--vault-root`
is not a supported way to move anything. What *is* left after Step 1 is the
receipt, and the no-op run described in the section above is what writes it.

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

`--force` here overrides the same two write-time rails as in Step 3 — an
existing receipt and a dirty vault tree — and this command has no nesting rail,
because it plans a vault's own layout rather than a slice of one. `--json`
outputs the raw summary. The receipt
(`<runtime>/migration/vault-rehome.json`) is an exact reverse map — moves as
`from`/`to` pairs, rewrites as `(offset, from, to)` triples in the rewritten
text — so `vault-unrehome --apply` is an inverse that leaves a file alone on
any mismatch. Reversibility here is not a rail either: as in Step 3,
`vault-unrehome` accepts `--force` and ignores it, because it is deliberately
*not* gated on a clean vault (the re-homing is what made it dirty) and every
span is re-checked before it is touched. Proposals already written to a review
queue are not withdrawn; they are yours to resolve. A run that could not move
everything says so and exits `1`, and the reverse map carries forward, so
re-running loses nothing already done.

## Step 5 — optional: settle the frontmatter vocabulary

Optional, for the `vault-vocabulary` card — which, again, is a Home card the
audit does not carry.

```bash
ciao vault-migrate          # the plan; changes nothing
ciao vault-migrate --apply  # write the renames
```

This one does two things, not one. It **keeps the stock categories that no
longer ship** (product, feature, automation, document, reference, content) when
notes still carry them, by copying the ones in use into this vault's own
`entity-types.yaml` — which is a write to that file, and is why the retentions
are reported first, before the renames are judged. And it **renames aliased
types** (`project-log` → `journal`) and reports every type with **no** canonical
equivalent. Only aliased types are renamed, and only when the current value is
exactly the alias being replaced, so a hand-edit racing the migration is not
clobbered.

Those unresolved types need you. A type with no alias target is reported and
left alone on purpose: choosing a category is your call, and guessing would bury
a real decision inside a migration. They are listed under `unresolved` in the
receipt — one `vault-vocabulary.<label>-<digest>.json` per vault under
`<runtime>/migration/`, because one runtime root serves every workspace's vault,
which is why the receipt is keyed on the vault and this command can be run once
per workspace.

**Outcomes, because they are not all success.** The command exits `1` when
anything is `unresolved` or `failed` — so a run that did every mechanical rename
it could still exits `1`, by design, until you categorise the rest. A dry run
filters the types the retention is about to claim out of its `unresolved` list,
because nothing has been written yet and those are not your decision to make;
so the list a preview asks you to categorise is only the list it can see.

**`--apply` writes no receipt; the card is cleared by the card's own button.** Two
commands, and only one of them can change what the card says:

- `ciao vault-migrate --apply` renames the notes, keeps the retired categories and
  records nothing. It never touches `<runtime>/` at all, so the CLI cannot change
  what the card says — not even when the renames it just made were the whole of
  the card's complaint.
- `sync-skills` writes the keyed per-vault receipts, but it is gated on the
  install-wide receipt being absent, and it skips any vault whose keyed receipt
  already exists. Once every vault has one, it does not re-scan, so a note
  written afterwards with a retired type is not picked up and the receipt is not
  refreshed.

So the button is how a decision gets recorded. **Apply mechanical renames**
re-migrates every vault whose receipt still lists `unresolved` types and
**rewrites that vault's own keyed receipt** — the file the card's install-wide
read is built from — so once you have fixed the `type:` lines (or the chat has
fixed them for you), the next press re-scans, the types are gone from the
receipt, and the card disappears. Two things it deliberately does not do: it does
not write the pre-keying unkeyed `vault-vocabulary.json`, which the install-wide
read consults only on a single-vault install, and it does not touch a vault
whose receipt is already complete — that root's receipt is the record of its own
migration, not this card's business. So the card reports work in the vaults it
names; if you fix a `type:` line in a vault the card never named, delete that
vault's receipt and let `sync-skills` migrate it again. Treat the card as a
pointer to work, refreshed by its own button, not as a status light somebody
else keeps in step with your edits.

Note what this command does **not** have: there is no `vault-unmigrate`. The
renames are gated on an exact current value, so an unwanted one is a one-line
frontmatter edit, and the unresolved types were never touched at all.

## After the separation: the skill catalog and the triage sheet

The migration moves the whole skill catalog to the primary root rather than
copying it into every root — most skills are scoped to one workspace, and a copy
everywhere is the shared catalog rebuilt — and creates `skills-src/` as the
shared mirror source. What it refuses to do is attribute a customised skill to
a root, because guessing hands one workspace's tooling to another. Instead it
writes **`Workspace/Skill-Triage.md` inside the primary workspace's vault**,
listing every non-stock skill with its destination cell blank.

Filling that sheet in is your decision, not a command's: for each skill, say
what it is for and which workspace it belongs to, move the approved ones into
that workspace's `skills/`, and leave the rest where they are. Then mirror the
catalogs:

```bash
ciao sync-skills --workspace <root>
```

`sync-skills` is **not** read-only and takes no `--apply`. It installs and
mirrors the local catalog (`skills/`, `commands/`, `subagents/`) into each
root's generated `.claude/` and `.agents/` trees, **prunes** generated assets
that are no longer in the catalog, and re-mirrors the shared `skills-src/`
skills. It also runs two vault migrations itself, each gated on a receipt so the
cost is paid once rather than on every boot: the Step 5 vocabulary migration,
and the retention of retired stock categories. "Gated on a receipt" is the whole
of that guarantee, and it is worth being precise about what follows from it: once
a vault has a vocabulary receipt, `sync-skills` does not re-scan that vault, so
running it is not a way to make it notice a retired type someone reintroduced
later. To re-scan one, delete its receipt and run the Step 5 command. Read the
receipt rather than expecting a preview — `sync-skills` reports nothing about
the scan it skipped. It is idempotent, and re-running it after editing a
catalog is the intended use.

Two more cards are about the catalog, and both are **chat-only** — there is no
command that answers them:

- **"N GitHub skill(s) are no longer installed"** — `skills-lock.json` is only a
  record now; the GitHub install surface that used to restore the cached copy is
  gone. Copy each cached copy from `.claude/skills/<name>/` or
  `.agents/skills/<name>/` into `skills/<name>/` and re-run `ciao sync-skills`.
  If no cached copy exists, the skill is gone and has to be re-added.
- **"N skill(s) need a workspace"** — this card is raised from **the triage
  sheet the separation wrote**, the `Workspace/Skill-Triage.md` in the primary
  workspace's vault described above. It is found through the separation's
  receipt, which records the file it created, so the card follows the sheet to
  whatever folder your vault uses rather than looking for a path of its own.

  The count is the number of rows whose **Destination** cell is still blank, so
  it falls as you fill the sheet in and disappears once every row is answered.
  There is no button and no command: the destinations are yours to decide, and
  writing a destination you did not confirm is the exact guess the sheet exists
  to prevent.

Two drift cards per workspace — "has no folder" and "is missing generated
assets" — are fixed by their run button, which is `ciao workspace-reroot
--repair`: it recreates the root with its assets and re-mirrors the catalog, and
exits `0` once there is nothing left to report. It exits `1` only when the
install has not re-rooted, when it errored, or for one of the three
report-only drifts listed above — never because of these two.

## Verify

```bash
ciao os-audit
```

Read-only. Human-readable Markdown by default, `--json` for automation, and the
status *is* the exit code: `0` healthy, `1` reliable findings, `2` the scan
could not inspect what it needed and the report is not a clean bill of health.
On a re-rooted install it resolves the per-workspace vault on its own, so no
`--vault-root` is needed here; `--workspace-name` narrows the per-workspace
sections to one workspace. Paste the output rather than summarising it, and do
not report success while these are non-empty:

- `vault_hygiene.broken_markdown_links` — a reference nobody re-pointed
- `memory_hygiene.marker_errors` — a bounded region that is malformed
  (duplicated or missing markers make the region unwritable, so every later
  memory write fails)
- `search_index.missing` / `search_index.stale_rows` — derived state that was
  not rebuilt; `ciao workspace-reroot --repair` is the fix for both

`ciao vault-lint` is the faster subset: frontmatter, broken relative Markdown
links, orphan pages and near-duplicate stems, exiting `0` only on a clean
vault. It takes `--vault-root` if you want one workspace's notes.

## The "After this update" tasks

Home has a second, separate group: the **After this update** tasks, driven by
`ciao/update_task_catalog.py` and packaged in `ciao/stock/update-tasks/`. A
task there is a *decision that persists* until somebody reverses it, with a
server-owned prompt, a per-`(id, revision)` record, dismissal, and a reopen in
Settings → Update task history.

**Only one migration notice in this document is a catalog task, and it is the
one whose remedy writes a receipt.** `unrehomed_people` ships as
`unrehomed-people` (install-scoped, with its packaged prompt at
`prompts/unrehomed-people-1.md`): it is finished when
`<runtime>/migration/vault-rehome.json` records a **completed** run — including
the honest no-op run above, and **not** a `partial` one, not a run that was
refused, and not a file nobody could read. Every other notice here is an
operator-action tile — a *view of the machine*, recomputed on every render — and
`ciao os-audit` reports the findings independently, so the audit may still name a
finding whose card you hid. Those tiles render even when the work has been done,
because "completion evidence" for most of them is only the condition's absence; a
catalog task needs a receipt a completion check can read, and only a managed
remedy that writes one has it. The catalog's other shipped row,
`learnings-cleanup`, is about `Workspace/Learnings.md` and unrelated to layout.
`docs/DEVELOPMENT.md` has the classification table for all of them, including the
ones that are deliberately not tasks — do not assume any of these are offered
there.

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

For the module-level design, see `workspace_reroot.py`, `vault_relocate.py`,
`vault_migrate_links.py`, `vault_rehome.py` and `vault_migration.py` in the
`## App repo layout` block of [docs/ARCHITECTURE.md](ARCHITECTURE.md); for the
update-task contract, see [docs/DEVELOPMENT.md](DEVELOPMENT.md).
