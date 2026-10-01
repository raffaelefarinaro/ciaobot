# Move this workspace's vault into its standard folder

Run the preview for **this task's workspace**:

```
ciao vault-relocate {workspace}
```

It prints the move it would make and writes nothing. Replace `{workspace}` with
the workspace name this task was offered for — the same name the card names and
the same name the registry uses. Do not guess it from a directory name: the
registry is the layout, and a folder that happens to exist is not a workspace.

**This task was offered because that preview would move something.** The engine
compares the workspace's registered vault against the folder this install's
layout puts it in, and both exist and differ. If the preview refuses, that is a
real property of this install rather than something to work around — read
"Refusing" below.

`ciao os-audit` reports the same condition as an advisory pending action that
never turns the report red. That notice is not this card, dismissing this card
does not silence it, and it will keep reporting a mismatch that appears later.

## Reading the preview

| Line | Read it as |
| --- | --- |
| the entries it lists, or `whole_directory` | what `--apply` would move |
| `move` / `skip` / `unclassified` per entry | this workspace's own content / another workspace's or vault-wide shared state / something it will not classify |
| `refusals` | why this shape cannot be relocated automatically |
| `refused: true` | nothing will be moved; read "Refusing" |

`--json` prints the same plan raw, and is the right form when you need a field
the text form only summarises.

## Applying, with approval

Do not pass `--apply` until you have shown the operator this preview and they
have agreed to it in this chat. State what would move — the whole directory, or
these specific entries — and that the registry entry for this workspace will be
repointed to the standard folder. Then wait.

```
ciao vault-relocate {workspace} --apply
```

Two rails refuse the apply unless `--force` overrides them, and both protect the
operator:

* uncommitted changes **in the files this run would move**, so `git checkout`
  stays an undo that needs nothing from us;
* an existing completed receipt, whose reverse map `--undo` depends on.

Every move goes through git, so history follows the file.

A refusal is information. Report what it refused and why, and stop. Do not reach
for `--force` unless the operator asks for it and has understood what it
overrides.

## After it moves: restart

The apply itself reports this, and it is not optional:

```
Ciaobot needs a restart (Settings -> Restart) before the new location takes
effect everywhere, including in an open chat.
```

The running server is a separate process that resolved the old location when it
started. It keeps writing to the old path until it restarts, so tell the
operator to restart before writing more notes through this workspace. Do not
restart it for them.

## Putting it back

```
ciao vault-relocate {workspace} --undo
```

An exact inverse, not a re-derivation: only the moves the receipt names are
reversed, and only this workspace's registry entry is restored — another
workspace added or edited since is left alone. It **also needs a restart**, for
the same reason the apply does.

`--undo` **removes the receipt** when it succeeds. That is deliberate: this
task is done only when a completed relocation is recorded *and* the layout still
agrees, so undoing the move makes the work outstanding again and the card comes
back rather than disappearing over a vault that is outside its folder again.

If the operator prefers not to relocate, dismissing this task is the right
answer, and it changes nothing about the audit notice.

## Refusing

Most of these are permanent properties of an install, not transients, so a retry
refuses the same way. Report the refusal and let the operator decide; do not
work around it by moving anything yourself.

| Refusal | What it means |
| --- | --- |
| a symlink at the source or the destination | one path is a link, which this command will not follow; that shape is finished by hand |
| destination exists and is not empty | something is already there — ask the operator what it is before touching either folder |
| the vault root **is** the install root | an existing-folder setup whose top level mixes vault content with `.git`, `CLAUDE.md` and `.runtime`; **finished by hand** |
| the vault lives outside the install's git worktree | an external or hand-pinned root: git cannot move it, there is no automatic undo, and the only route is a manual backup-and-move |
| the shared vault root is claimed by more than one workspace | ownership of its loose top-level content is ambiguous, so nothing was classified |
| this vault contains another registered workspace's vault | moving it whole would carry that workspace's notes along |
| the canonical destination is at or beneath the vault | relocating would nest the vault inside itself; the workspace's `vault_root` needs repointing first |
| uncommitted changes | the rail above; commit or stash them, then retry |

An entry classified `unclassified` is the same kind of answer for one name: raise
just that one with the operator, and do not treat it as a reason to move the
rest by hand.

## Never

Never move, copy or delete a note or folder yourself, and never use git to do
it. Never edit
`<runtime>/workspaces.json`, or a workspace's `vault_root` in it, and never edit
`entity-types.yaml`, a guide or any index. Never hand-write a receipt. Those are
exactly the paths the engine refuses, and the registry repointing that makes the
move correct is the part no manual route can reproduce reliably.