# Stopped-engine vault rebind (#3606)

`ciao vault-rebind` points one existing workspace at an already prepared notes folder (for example a cloud-synced one) while the engine runtime stays local. It is operator-initiated, reversible, and works only for a stopped engine. It never copies, moves, deletes or modifies either notes tree. The cloud client controls synchronisation, and only one engine may write the notes; the command says so in every preview.

## Commands

- `preview NAME DEST` (read-only): derives the current binding and every other registered root from the strictly validated registry (an explicit list of rows with absolute, symlink-free roots; legacy name-keyed, relative, duplicate-key or ambiguous registries are refused), compares both trees (regular files only, no symlinks, matching content, no overlap with protected roots) and prints the exact old and new path, the file count and a review token. The token binds the full registry bytes, every file digest and the file and directory identities of both trees, so a replaced folder with identical content invalidates it.
- `apply NAME DEST --token T`: the explicit reviewed action.
- `reverse-preview NAME --id ID` and `reverse NAME --id ID --token T`: put the old folder of a completed rebind back.
- `recover`: resolve an interrupted operation.

## Apply sequence

1. Take the engine's `server.lock` (non-blocking; a running engine, or another rebind, refuses) and then the interprocess registry claim (`acquire_registry_at`, non-blocking). Lock contention refuses with no writes beyond the lock files.
2. Refuse if a pending journal exists (run `recover`).
3. Re-derive the preview under both locks and compare the token in constant time. Any change to the registry, paths, files or identities refuses before anything is written. The private state directory must not lie inside a notes folder.
4. Write a private (0600, directory 0700) backup of the exact registry bytes under `.runtime/vault-rebind/`, read it back and verify it.
5. Write the durable journal `pending.json` (old and new registry bytes, digests, binding, operation id), fsync file and directory. Re-verify the trees once more; a change here removes the journal and refuses, since nothing has changed yet.
6. Replace the registry through `WorkspaceRegistryOwnership.replace` (byte-exact compare-and-swap, fsync), changing only `vault_root` of one row; unknown fields and other rows are preserved (the file is re-serialised as indented JSON).
7. Write `done-<id>.json`, then remove `pending.json`.

## Recovery

`recover` validates the journal (kind, id, digests of both images) and compares the live registry to its two images. Equal to the before image: nothing was applied, the journal is removed and the same reviewed token can be used again. Equal to the after image: completion is recorded. Anything else (a later edit, a tampered journal) refuses and keeps every journal and backup file as evidence. If an engine (or anyone) edited the registry while a journal was pending, `recover` refuses forever by design. The operator then reads `.runtime/vault-rebind/pending.json` and the `<id>.before.json` backup, decides by hand which binding the workspace should have, and either keeps the live registry as it is or edits only that workspace's `vault_root`; only after that is `pending.json` deleted (with the engine stopped), which lets a new preview and apply proceed. Nothing deletes the journal automatically. Crashes are tested at each step (after backup, after journal, before replace, after replace, after record).

## Reversal

A completed record names the old and new root. Reversal needs the workspace to still use the recorded new root, the old folder to be available and to match the current notes tree exactly, a fresh review token, and no earlier reversal of that record. It changes only that field, so later unrelated registry edits survive; it never restores a whole old registry and never deletes a notes tree. If notes were written to the new folder after the rebind, the trees differ and the reversal refuses; reconcile the folders yourself, nothing is discarded.

## Not covered

Reversal reads the old and new root from the digest-verified registry images in the record, not from the record's other fields. The registry rewrite re-serialises JSON, so the file is not byte-identical apart from the one field. The feature is POSIX-only (it needs `O_NOFOLLOW`, `O_DIRECTORY` and `flock`).

The comparison is a point-in-time check: a cloud client can change a tree after the final verification; the engine being stopped and the client being paused are the operator's responsibility. Existing installations (legacy, relative, cloud-hosted runtime roots) are not migrated silently; they are refused until explicit handling exists.
