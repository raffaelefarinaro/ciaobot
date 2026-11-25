# Ciao #3626 ordinary-folder capability probe

Research-only evidence for
[#3626](https://github.com/raffaelefarinaro/ciaobot/issues/3626), "Determine and
implement safe existing-note changes for folder destinations".

**Status: on the exercised APFS schedules and primitives, no tested primitive
provided an atomic expected-version replacement for an existing note against an
independent writer. Automatic and engine-applied reviewed replacement for folder
destinations remains disabled; retain the proposed diff for manual
application.** `O_EXCL` held for concurrent new-name namespace no-clobber only;
this probe makes no crash-safe or complete-creation claim. This is not a
universal impossibility claim: cloud and network backends and other filesystems
were not tested.

This directory adds an experiment, not a production adapter. It does not import
`ciao`, does not start the engine, does not touch a vault, a workspace, an
installed app, a cloud account, or a live shared brain. It writes only under a
fresh temporary directory and removes that directory by default. It does not
enable folder publishing or switch any destination from Git.

## Question

Can an ordinary filesystem give a writer an **atomic expected-version
precondition** for replacing an existing note, against every relevant writer,
including a second editor or a cloud-sync process that does not cooperate with
Ciaobot?

The probe tests the primitives that look promising on a local volume:

- hash / stat check followed by `rename` ("hash-then-rename");
- `renameatx_np(RENAME_SWAP)` on macOS;
- advisory `flock`;
- an exclusive `O_EXCL` claim file;
- vnode observation through `stat`;
- content-hash, size+mtime and inode identity as versions;
- a writer that re-reads after the first external write, then replaces;
- path-based and descriptor-relative root handling;
- a *local model* of an ETag-only writer on a locally dirty tree. This is not a
  test of any provider's conditional operation.

It also exercises concurrent `O_EXCL` creation of a **new** path. That is
namespace no-clobber for two creators in this run, not crash-safe or complete
creation, and not an existing-note update.

## Environment used for the recorded result

| Item | Value |
|---|---|
| OS | macOS 27.0.1, build 26A434 |
| Kernel | Darwin 27.0.0 (`xnu-13432.1.9~1/RELEASE_ARM64_T8132`) |
| CPU | arm64 (Apple silicon) |
| Filesystem | APFS, local volume (`apfs`, `stat` device `disk3s5`) |
| Python | CPython 3.15.2 (`/opt/homebrew/opt/python@3.15/bin/python3.15`) |
| Probe | `probe.py`, SHA-256 recorded in the JSON report |
| Date | 2026-11-25 |

The JSON report records the same values under `platform`, including the probe's
own SHA-256.

## Run it

From the repository root:

```sh
python3 experiments/ciao-3626-folder-probe/probe.py
```

The default run creates a fresh directory under `TMPDIR`, runs every test, prints
a summary, and deletes the directory. It exits 0 when the harness ran, even when
a capability is reported as failed; a non-zero exit means the probe itself could
not run.

Write the full machine-readable report:

```sh
python3 experiments/ciao-3626-folder-probe/probe.py \
  --iterations 3 \
  --aba-cycles 200 \
  --json /tmp/ciao-3626-results.json
```

Keep the disposable tree for inspection:

```sh
python3 experiments/ciao-3626-folder-probe/probe.py --keep
```

Run against another disposable volume (for example a test exFAT, SMB or NFS
mount). `--root` must name an empty directory that contains no live data; the
probe creates one child directory inside it and removes only that child:

```sh
python3 experiments/ciao-3626-folder-probe/probe.py \
  --root /Volumes/Disposable-Probe \
  --json /tmp/ciao-3626-other-backend.json
```

Do **not** point `--root` at a real shared folder, a live vault, a cloud sync
root, or a repository. The probe is deliberately adversarial: its external
writer ignores locks, claim files, symlinks and root swaps because a real editor
or sync agent may do the same.

## Recorded result

Recorded report:
[`results/macos-27.0.1-apfs.json`](results/macos-27.0.1-apfs.json).

Counts: **11 failed, 3 supported, 2 partial, 2 unverified**.

| Test | Status | What it demonstrated |
|---|---|---|
| `hash_then_rename_two_writers` | failed | Two processes both validated the same expected SHA-256 and both renamed over the target; one writer's change was lost with no conflict. |
| `flock_coop_cooperating_writers` | supported | Two writers that both honor one advisory lock serialized: one wrote, one reported a conflict. |
| `claim_coop_cooperating_writers` | supported | The winner held the `O_EXCL` claim until the other writer attempted it; the loser retried, acquired after release, re-read the new version, and refused with a conflict. Cooperative retry, not external exclusion. |
| `hash_then_rename_external_writer` | failed | A check-once hash-then-rename writer (no final reread) discarded an external write that landed after its check. |
| `flock_external_writer` | failed | A check-once writer holding an advisory lock discarded an external write; the external writer ignores the lock. |
| `claim_external_writer` | failed | A check-once writer holding an `O_EXCL` claim discarded an external write; the external writer ignores the claim. |
| `vnode_observation_external_writer` | failed | Observing the target's stat identity once before the external write, with no final reread, did not guard the replacement. |
| `renameatx_swap_external_writer` | failed | `RENAME_SWAP` moved the external bytes out of the target and discarded them; it does not compare versions. |
| `reread_detect_external_writer` | partial | A final reread after a *different* external write detected the change, and the writer refused without replacing. This is detection for that non-racy schedule; it does not make the reread and replacement atomic. |
| `reread_window_external_writer` | failed | The first external write returned identical bytes (a content-hash ABA); the reread passed, then a second external write landed after the reread but before the replacement, and was lost. |
| `remote_etag_only_local_dirty` | unverified | **Local model only, not a provider test:** an ETag-only implementation that trusts an unchanged remote version and performs an unconditional local replace lost an offline-dirty local file. A local content recheck detected this schedule. No cloud ETag semantics were exercised. |
| `content_hash_delete_recreate_aba` | failed | A content hash accepted a delete/recreate that returned identical bytes; the generation changed but the version did not. |
| `mtime_size_delete_recreate_aba` | failed | A size+mtime version accepted a delete/recreate after the mtime was restored. |
| `inode_reuse_cycles` | unverified | 200 create/unlink cycles used 200 distinct inodes here; reuse was not observed, but inode identity is not atomic with replacement and cannot be assumed stable on another backend. |
| `root_substitution_path_writer` | failed | A path-based writer followed a substituted root symlink and wrote into the decoy directory. |
| `root_substitution_dirfd_writer` | partial | Descriptor-relative traversal stayed in the original directory and refused an already-symlinked root, but the user-visible root no longer received the write. Containment is not root binding. |
| `root_aba_identical_clone` | failed | An identical clone at the visible root left the bytes unchanged; the probe asserts that the writer's payload landed in the detached directory. |
| `o_excl_new_note_create` | supported | Two writers creating the same new path produced exactly one file; the loser got `EEXIST` and wrote nothing. Namespace no-clobber only; not crash-safe or complete creation. |

The supported and partial rows are intentionally narrow. Cooperating writers
can be serialized by a shared lock or a retried claim, but a lock or claim is
**advisory**: it does not bind a process that does not honor it. A final reread
can detect an external change that landed before it; the `reread_window` test
shows a second write landing after that reread is still lost. `O_EXCL` protects
only the creation of a name that did not exist; it cannot replace an existing
note and is not evidence of crash-safe creation.

## Decision supported by this evidence

For an existing note on an ordinary folder, **on the exercised APFS schedules
and primitives**:

- **Unsupported:** an atomic expected-version replacement against an independent
  writer. Check-once hash-then-rename, swap, advisory locks, exclusive claims and
  vnode observation lost an external change. Content/mtime ABA was
  indistinguishable in the exercised schedules. A final reread detected an
  external write that landed before it, but a second write landing after the
  reread was lost.
- **Partial:** descriptor-relative traversal contains a root swap, but it cannot
  prove the user-visible root is still the selected root at commit. The caller
  would need a root/link-lifetime binding the exercised APFS primitives did not
  provide.
- **Supported narrowly:** `O_EXCL` namespace no-clobber for two concurrent
  creators. Not crash-safe, not complete creation, not an existing-note update.
- **Unverified:** every cloud-backed folder path (iCloud Drive, Dropbox,
  OneDrive, Google Drive for Desktop), every non-APFS local filesystem, every
  network mount, and the link-lifetime, consent-generation, journaling,
  reconciliation, revoke/retry and crash-after-acceptance requirements. The ETag
  row is a local model, not provider evidence. None of them may be counted as a
  supported ordinary-folder success from this probe.

Consequently, per #3626, automatic and engine-applied reviewed replacement for
existing notes on ordinary folders should remain disabled. A proposed diff
should be retained for manual application. A different versioned storage
destination or an explicitly upgraded provider-managed workflow is a separate
product decision and is not an ordinary-folder success.

## What this probe does not prove

- It does not test Ciaobot's grant, consent, link-lifetime, journal, receipt,
  Settings or producer/consumer paths. Those require a production adapter and
  are deliberately outside this experiment.
- It does not test a real cloud provider, network mount, or another local
  filesystem. Those rows in the capability matrix are **unsupported
  (unvalidated)**, not passes.
- It does not establish that no filesystem or provider can ever offer an atomic
  conditional replacement. It reports only the exercised APFS schedules and
  primitives; a provider-managed conditional operation is outside this probe.
- It does not claim that a cooperating lock is worthless. It is useful for two
  Ciaobot writers that share it; it is not an external-writer precondition.
- It does not claim inode reuse is impossible. It observed no reuse in 200
  cycles on APFS, which is not a guarantee.
- It does not modify `ciao/share_publish.py`, `ciao/shared_brain.py`,
  `ciao/share_proposals.py`, any Settings surface, or any adapter. The existing
  Git publish path is unchanged.

## Files

- `probe.py` — standalone, standard-library-only probe and writer subprocess.
- `CAPABILITY_MATRIX.md` — backend inventory, primitive matrix, and #3626
  requirement traceability.
- `results/macos-27.0.1-apfs.json` — full report from the recorded run.
