# Check whether person notes need re-homing

Run `ciao vault-rehome` against this install's shared vault. It prints one row
per candidate person note and writes nothing.

**Nothing is known to be misfiled yet.** This task appeared because the install
has more than one registered workspace and has never recorded a re-homing — a
receipt check, not a scan. The command is how you find out whether there is
anything to do, and "nothing to do" is a complete and correct answer to this task.

## Where to run it

```
ciao vault-rehome                                    # when the default root is right
ciao vault-rehome --vault-root <install>/memory-vault # when it is not
```

The default is `CIAO_VAULT_ROOT` or `./memory-vault`, and on an install whose
workspaces have been separated into per-workspace roots there is no
`memory-vault` at the install root at all — the command then exits 1 without
writing anything. Each workspace's own vault is listed in Settings →
Workspaces; on a shared-layout install every workspace's vault is under the one
`<install>/memory-vault`.

Leave `--runtime-root` alone unless you have checked where the receipt belongs.
The receipt has to land in the same `.runtime` the engine reads, because that is
the only thing that clears this notice and settles this task. A receipt written
somewhere else clears nothing.

## Reading the output

| Line | Read it as |
| --- | --- |
| `No tag-obvious misfiled people in N note(s).` | nothing to move; see "Nothing to move" below |
| `Would move N person note(s), rewriting M reference(s)` | the plan; nothing has been written |
| `path -> destination (reason)` | one mechanical move, and the tag that decided it |
| `Queued for review — not moved, the tags do not decide it` | judgement cases; read them, do not move them |
| `Skipped:` (stderr) | a destination that is already occupied, or two notes racing for it |
| `Failed:` (stderr) | a write or a move that did not land |

`--json` prints the same summary raw, and is the right form when you need a
field the text form only summarises.

The command exits non-zero when it refused, failed, or skipped a conflict. A
non-zero exit after an `--apply` is therefore not proof that nothing was written:
read the receipt line, and the `failed` entries, before saying the run did
nothing.

## Which layout you are in

This decides what the command can do, so establish it before reading the plan.

* **Shared vault** (`<install>/memory-vault/<workspace>/…`). A note is examined
  only when it sits at `<workspace>/<person folder>/…` for a registered
  workspace, and it moves to the workspace its tags name. This is where the
  backlog this command exists for actually is, and the moves are real.
* **Per-workspace roots** (`<install>/<workspace>/memory-vault/…`). The notes are
  already separated, so a root's vault has no workspace segment and the plan
  finds nothing — and `--workspace-name` does not change that. There is no
  per-root form of this move.

The re-rooting writes no re-home receipt, so on a re-rooted install this notice
outlives the thing it was named for. See "Nothing to move" before reporting that
the command did nothing useful.

## What you may decide

* A note whose tags name one other registered workspace moves. That is a
  mechanical substitution with no decision in it.
* A note with **no** workspace-naming tag is a judgement call about someone's
  relationship to this user, and it is **never moved**. So is one whose tags name
  more than one other workspace, and one whose tags name both its own workspace
  and another — two workspaces is precisely the case the tags refuse to decide.
  Both are queued into that workspace's `Workspace/Memory-Proposals.md`, which has
  its own review surface. Read the queue entries and report them; resolving them
  is the user's.
* A note already cross-linked to a same-person note in the other workspace is not
  a candidate: the split was made on purpose.
* A tag naming a workspace that is not registered is not a signal, so the note is
  not a candidate at all. Say so rather than inventing a destination.

Never move, rename, edit or delete a note by hand, never edit a registry, and
never hand-edit a reference to a note that moved. Those rewrites are what make a
move break a vault, and the command does them through the receipt that reverses
them.

## Applying, with approval

Do not pass `--apply` until you have shown the operator the preview and they have
agreed to it in this chat. State what would move and how many references would be
rewritten, and what is queued for judgement and would **not** move. Then wait.

Two rails refuse the apply unless `--force` overrides them, and both protect the
operator rather than inconvenience them:

* an existing completed receipt — a second pass would overwrite the reverse map
  that undoes the first;
* uncommitted changes **in the files this run would write**, so `git checkout`
  stays an undo that needs nothing from us.

A refusal is information. Report it and stop; do not reach for `--force` unless
the operator asks for it and has understood what it overrides. Neither refusal
applies to the dry run above — it never writes, so it is never gated.

```
ciao vault-rehome --apply
ciao vault-rehome --vault-root <install>/memory-vault --apply
```

A run that could not move every note records `status: partial`, keeps asking, and
tells you to fix the cause and re-run; the reverse map carries forward, so
nothing already done is lost. Read the receipt line the command prints and report
what it says.

## Putting it back

```
ciao vault-unrehome --apply
ciao vault-unrehome --vault-root <install>/memory-vault --apply
```

An exact inverse, not a re-derivation: only the moves and the reference spans the
receipt names are reversed, so a note the user filed by hand is not dragged back
with them, and a file changed since the re-homing is left completely alone. It is
deliberately **not** gated on a clean vault — the re-homing is what made the
vault dirty. It accepts `--force` and ignores it. Proposals already queued are not
withdrawn; they are the user's to resolve.

## Nothing to move

If the preview reports no tag-obvious misfiled people, that is the finding, and
it is worth recording: on this install the re-homing has been looked at. A first
`--apply` over a root with nothing to do still writes a receipt — an empty move
list and `status: migrated` — which is what `ciao os-audit` reads and what
settles this task. That is a real run that really looked, not a trick, and it is
the honest way to finish on a re-rooted install where the notes are already
per-workspace.

The receipt belongs at `<runtime>/migration/vault-rehome.json`, inside the
runtime directory this engine reads. A receipt written anywhere else clears
nothing and this task simply comes back.

Say what you found before you apply it: on a shared vault it means nothing is
misfiled; on a re-rooted install it means the backlog this task was named for was
never in this layout.

## What this is not

`ciao os-audit` keeps reporting `unrehomed_people` as an optional pending action
regardless of this card, and it never turns the audit red. Recording the receipt
is what stops it; dismissing this task does not.