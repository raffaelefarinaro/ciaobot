# Re-home person notes a workspace-wide curation run misfiled

Run `ciao vault-rehome` against this install's shared vault. It prints one row
per candidate person note and writes nothing.

**This task was offered because that preview would move something.** Its detector
runs the same plan the preview prints and only offers the card when at least one
move is tag-obvious — a person note whose tags name another registered workspace,
filed under the wrong one. That is the damage an old global curation run did. If
the preview turns out to find nothing, something changed since the offer: say so,
and do not treat the absence as work.

`ciao os-audit` reports a **broader** condition on the same install — "no
re-homing has ever been recorded", which is true of every install that never
needed one — as an optional pending action that never turns the report red. That
notice is not this card, dismissing this card does not silence it, and it is not
evidence that anything is misfiled.

## Where to run it

```
ciao vault-rehome                                    # when the default root is right
ciao vault-rehome --vault-root <install>/memory-vault # when it is not
```

The default is `CIAO_VAULT_ROOT` or `./memory-vault`, and on an install whose
workspaces have been separated into per-workspace roots there is no
`memory-vault` at the install root at all — the command then exits 1 without
writing anything. That layout has no workspace segment in its paths, so the plan
finds nothing there either, which is why this task does not appear on one. Each
workspace's own vault is listed in Settings → Workspaces; on a shared-layout
install every workspace's vault is under the one `<install>/memory-vault`.

Leave `--runtime-root` alone unless you have checked where the receipt belongs.
The receipt has to land in the same `.runtime` the engine reads, because that is
the only thing that clears the audit notice and settles this task. A receipt
written somewhere else settles nothing and the task simply comes back.

## Reading the output

| Line | Read it as |
| --- | --- |
| `Would move N person note(s), rewriting M reference(s)` | the plan; nothing has been written |
| `path -> destination (reason)` | one mechanical move, and the tag that decided it |
| `Queued for review — not moved, the tags do not decide it` | judgement cases; read them, do not move them |
| `Skipped:` (stderr) | a destination already occupied, or two notes racing for it |
| `Failed:` (stderr) | a write or a move that did not land |
| `No tag-obvious misfiled people in N note(s).` | nothing to move — see "If nothing moves" |

`--json` prints the same summary raw, and is the right form when you need a
field the text form only summarises.

The command exits non-zero when it refused, failed, or skipped a conflict. A
non-zero exit after an `--apply` is therefore not proof that nothing was written:
read the receipt line, and the `failed` entries, before saying the run did
nothing.

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
* A note whose destination is already occupied is reported under `Skipped:` and
  not moved. Which of two people keeps that filename is a content decision, and
  the command declines to make it.

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

The receipt belongs at `<runtime>/migration/vault-rehome.json`, inside the
runtime directory this engine reads. A receipt written anywhere else settles
nothing and this task simply comes back.

## If nothing moves

The run still writes a receipt when it has moved nothing and there was none to
begin with — an empty move list and `status: migrated` — which settles this task
and quiets the audit notice. That is honest here and only here: you previewed
first, you showed the operator, and the answer was that there was nothing to do.
It is a record of an inspection, not a way of clearing a card, and it is not how
this task came to be offered in the first place.

Say what you found. If the operator would rather not act on what is queued for
judgement, dismissing this task is the right answer, and it changes nothing about
the audit notice.