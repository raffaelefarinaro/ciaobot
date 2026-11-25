# #3626 capability matrix — ordinary-folder existing-note changes

This matrix is the research output for
[#3626](https://github.com/raffaelefarinaro/ciaobot/issues/3626). It is evidence
for a decision, not an implementation claim. The probe and the recorded report
are in this directory; no production adapter was changed.

## Decision rule

A backend qualifies only if, at the mutation seam, it can bind all of:

1. the immutable base version **and** its bytes;
2. the materialized root that the user selected;
3. the link lifetime and consent generation;
4. an exact result (winner, conflict, or indeterminate), without a blind
   replacement.

For an ordinary folder, the relevant test is not "can two cooperative Ciaobot
writers share a lock?" It is "can an independent editor or sync agent write
between the version check and the replacement without the writer noticing?"
Hash-then-rename, swap, advisory locks, exclusive claims, vnode observation and
remote ETag-only checks were tested because they are the cheap candidates. A
backend that was not validated is recorded as **unsupported**, not as a pass.

## Backend inventory

Tested platform for the local row: macOS 27.0.1 (build 26A434), Darwin 27.0.0,
arm64, APFS local volume, CPython 3.15.2. See
[`results/macos-27.0.1-apfs.json`](results/macos-27.0.1-apfs.json).

| Backend / access path | Tested here | Authority over the user-visible path | Local dirty vs remote version | Atomic expected-version condition | Symlink / root substitution | Undo / reconciliation | Verdict |
|---|---|---|---|---|---|---|---|
| **APFS local volume** (macOS 27.0.1) | **Yes** — 16 isolated tests | Local namespace only. Any process with write permission on the directory can rename, unlink, create or substitute a symlink at the user-visible path. Ciaobot has no exclusive authority. | No remote version exists. An ETag-style remote version cannot see an offline local edit; the ETag-only simulation lost the local change. | **No.** Hash-then-rename, `RENAME_SWAP`, `flock`, `O_EXCL` claim and vnode observation all lost an independent write. `O_EXCL` protects only creation of a new name. | Path-based writes follow a substituted root; descriptor-relative traversal contains the write but commits to a detached directory; an identical clone at the visible root was not distinguishable by content/version. | No native operation identity. An app-level journal can record intent, but the replacement itself cannot be made conditional; crash-after-acceptance is ambiguous and was not tested. | **Unsupported for existing-note replacement. Create-only no-clobber supported.** |
| HFS+ local volume | No | Not validated. | Not validated. | Not validated; POSIX rename has no compare-and-swap. | Not validated. | Not validated. | **Unsupported (unvalidated).** |
| Linux ext4 / XFS / btrfs | No | Not validated. | Not validated. | Not validated. `renameat2` offers namespace operations (`NOREPLACE`, `EXCHANGE`), not a content/version compare. | Not validated. | Not validated. | **Unsupported (unvalidated).** |
| exFAT / FAT32 local volume | No | Not validated. | Not validated. | Not validated; weaker rename and permission semantics than POSIX. | Not validated. | Not validated. | **Unsupported (unvalidated).** |
| SMB / CIFS mount | No | Provider/server and client cache own the path; oplocks and locks are not a portable precondition. | Not validated; local cache vs server revision is provider-specific. | Not validated; server rename atomicity and lock authority vary. | Not validated. | Not validated. | **Unsupported (unvalidated).** |
| NFS mount | No | Server owns the namespace; client locks are not a content precondition. | Not validated. | Not validated; no portable compare-and-swap. | Not validated. | Not validated. | **Unsupported (unvalidated).** |
| iCloud Drive (File Provider) | No — documentation only, no live data | File Provider materializes/evicts files; the provider owns synchronization. | Not validated; local placeholder state and remote state are separate. | Not validated; no ordinary-folder conditional replacement exposed to Ciaobot. | Not validated. | Not validated; provider conflict copies are not an operation journal. | **Unsupported (unvalidated).** |
| Dropbox folder | No — documentation only, no live data | Dropbox sync daemon owns synchronization; it may create conflict copies. | Not validated; local dirty state is not a remote revision. | Not validated; no atomic expected-version replacement available through the ordinary path. | Not validated. | Not validated. | **Unsupported (unvalidated).** |
| OneDrive folder (Files On-Demand) | No — documentation only, no live data | Provider owns hydration/synchronization. | Not validated. | Not validated; no ordinary-folder compare-and-swap. | Not validated. | Not validated. | **Unsupported (unvalidated).** |
| Google Drive for Desktop | No — documentation only, no live data | Provider owns synchronization. | Not validated. | Not validated; no ordinary-folder compare-and-swap. | Not validated. | Not validated. | **Unsupported (unvalidated).** |
| Git working tree + remote (existing Ciaobot shared-brain path) | No — existing production path, not changed | Git owns the checkout; local dirty state is explicit, and remote publication uses commits/refs rather than replacing a user-visible note in place. | Local dirty is a first-class Git concept; a fetch/push cycle is not the same as an ETag on a locally edited tree. | Not an ordinary-folder primitive. The existing Git path is versioned at the ref/commit level and is out of scope for #3626's ordinary-folder question. | Git tracks paths; this probe did not test Git root/link behavior. | Existing Git publication/reconciliation is unchanged. | **Out of scope; unchanged; not evidence for an ordinary folder.** |
| Provider-managed object store with conditional `If-Match` / ETag PUT (S3, GCS, WebDAV, etc.) | No | Provider owns the object; not a user-visible folder path. | Provider revision is authoritative; local dirty state is not represented unless the client uploads it. | Could be atomic where the provider offers a true conditional PUT, but it is not an ordinary folder and was not tested. | Not applicable to a local folder root. | Provider-specific; not tested. | **Not counted. A different versioned destination or upgraded provider workflow is a separate product decision.** |

### Backend conclusion

No ordinary-folder backend in this inventory qualifies. The only tested local
backend (APFS) fails the mutation-seam precondition, and every cloud/network
backend is unvalidated and therefore unsupported under #3626's rule.

## Primitive results on the tested APFS volume

All tests use isolated temporary files. The two-writer tests use two separate
processes and file barriers; the external-writer tests use one writer process
plus a parent process that edits the target after the writer's check. No sleep
is used to "win" the interesting race.

| Primitive | Version check | Independent external writer | Delete/recreate ABA | Root substitution | Result |
|---|---|---|---|---|---|
| Check SHA-256 then `rename` (hash-then-rename) | Both writers saw the same expected SHA-256. | Lost the external write. | Content hash accepted identical recreated bytes. | Path follows a substituted root. | **Failed** (`hash_then_rename_two_writers`, `hash_then_rename_external_writer`) |
| `renameatx_np(RENAME_SWAP)` (macOS) | Not a version check; swaps prepared bytes into the target. | Lost the external write; external bytes moved to the prepared name and discarded. | Swapping a recreated target cannot distinguish generations. | Path-level swap does not bind the root. | **Failed** (`renameatx_swap_external_writer`) |
| Advisory `flock` on a sidecar | Cooperating writers serialized correctly. | External editor ignored the lock; write lost. | Lock file is not a generation. | Does not bind the root. | **Supported for cooperating writers only; failed for external writers** (`flock_coop_cooperating_writers`, `flock_external_writer`) |
| Exclusive `O_EXCL` claim file | Cooperating writers serialized correctly. | External editor ignored the claim; write lost. | Stale claim/ABA is not a content version. | Does not bind the root. | **Supported for cooperating writers only; failed for external writers** (`claim_coop_cooperating_writers`, `claim_external_writer`) |
| `stat` / vnode observation before replace | Observed the target's identity, then replaced. | External write landed after the observation and was lost. | Observation is a point-in-time fact, not an atomic precondition. | Does not bind the root. | **Failed** (`vnode_observation_external_writer`) |
| Content SHA-256 as the expected version | Detects a different final content. | A different content would be detected only if re-read before replacing; that re-read is not atomic. | Identical recreated bytes collide. | Content hash cannot distinguish an identical root clone. | **Failed** (`content_hash_delete_recreate_aba`, `remote_etag_only_local_dirty`) |
| Size + `mtime_ns` as the expected version | ETag-like weak version. | Does not see local dirty if only the remote ETag is checked. | `mtime` can be restored after delete/recreate; weak version collided. | Does not bind the root. | **Failed** (`mtime_size_delete_recreate_aba`) |
| `(dev, ino, size, mtime_ns, ctime_ns)` | Stronger local identity. | Still not atomic with replacement. | Ctime/inode changed in the tested delete/recreate, but inode reuse was not observed in 200 cycles and is not guaranteed. | Still not atomic with a root swap. | **Unverified / not sufficient** (`content_hash_delete_recreate_aba`, `inode_reuse_cycles`) |
| Path-based write after a root swap | No version check. | The external writer's root swap is enough; the path write follows it. | A cloned root can look identical. | **Follows the substituted root.** | **Failed** (`root_substitution_path_writer`) |
| Descriptor-relative write (`dir_fd` + `O_NOFOLLOW`) after a root swap | No content version. | Does not exclude an external writer; it only avoids following the swapped root. | An identical clone leaves the visible path unchanged and the write detached. | **Contains symlink follow; does not bind the visible root.** | **Partial** (`root_substitution_dirfd_writer`, `root_aba_identical_clone`) |
| Remote ETag-only writer on a locally dirty tree | Remote version still matches. | Replaces the local dirty file; local change lost. | ETag does not represent local generation. | Not applicable to a remote object; not a folder root. | **Failed** (`remote_etag_only_local_dirty`) |
| `O_EXCL` create of a **new** path | No expected version needed. | A second creator gets `EEXIST`; no overwrite. | Not applicable to an existing note. | Not a replacement primitive. | **Supported, create-only** (`o_excl_new_note_create`) |

### Primitive conclusion

The cheap primitives fail not because they are implemented badly, but because
an ordinary filesystem exposes no atomic "replace only if the current bytes are
still version V" operation. A lock or claim only coordinates writers that obey
it. A descriptor-relative traversal prevents escaping through a swapped root,
but it does not prove the visible root is still the selected one at commit.

## #3626 requirement traceability

| #3626 requirement | Status | Evidence / reason |
|---|---|---|
| Inventory ordinary local mounts and cloud-backed folder access paths. | **Partially met.** | APFS local tested; HFS+, Linux filesystems, exFAT, SMB, NFS, iCloud, Dropbox, OneDrive and Google Drive listed but unvalidated. |
| Document tested platform/version for each candidate primitive. | **Met for APFS.** | macOS 27.0.1 / Darwin 27.0.0 / arm64 / APFS / CPython 3.15.2 in the JSON report. |
| Test authority over the user-visible path and local dirty/remote versions. | **Failed on APFS.** | Local path authority is shared with any writer; ETag-only simulation lost the local dirty change. |
| Test an atomic expected-version condition. | **Failed on APFS.** | Two writers both validated the same base and both replaced; external writes were lost. |
| Test symlink/root substitution behavior. | **Failed / partial.** | Path writes followed a substituted root; `dir_fd` contained but detached; identical clone ABA was not distinguishable. |
| Test undo/reconciliation semantics. | **Unverified.** | No native operation identity; the probe has no journal/receipt implementation. |
| Hash-then-rename, swap, advisory locks, vnode observation and remote ETag-only on a locally edited tree fail these criteria. | **Confirmed.** | All corresponding tests failed against an independent writer. |
| Mark an unvalidated backend unsupported. | **Done.** | See the backend table: unvalidated rows are `unsupported`, not passes. |
| For any qualifying backend, implement an explicit capability on that adapter. | **Not applicable; no backend qualified.** | No production adapter or Settings surface was changed. |
| Bind immutable base version plus bytes, materialized root, link lifetime, consent generation and exact result. | **Not implemented / unverified.** | Probe covers base bytes and root behavior only. Link lifetime, consent generation and exact result require the production path and were not tested. |
| Journal intent, execute one version-conditional replacement, preserve all other bytes, report conflict without writing when the version differs, reconcile ambiguous outcomes by operation identity. | **Not achievable with the tested primitives.** | External/loser bytes were lost in tests; no atomic conditional replacement or filesystem operation identity exists on APFS. |
| Never switch silently from a selected folder to a remote-object backend. | **Policy unchanged.** | No production change; this probe does not switch destinations. |
| Surface actual destination and authority in Settings. | **Not in scope / unverified.** | No Settings change. |
| Test independent external editor writes at the mutation seam, offline dirty local state, remote revision changes, delete/recreate ABA, root swap, revoke/retry and crash after acceptance. | **Partially tested.** | External writes, offline dirty, ETag-only, ABA and root swap tested. Revoke/retry and crash-after-acceptance require the engine and are unverified. |
| Both producers update an existing note under a standing grant; a second workspace reads the exact result. | **Not tested / blocked.** | Requires a production adapter and consumer path; out of scope for this probe. |
| The losing writer's change must survive every conflict. | **Failed for noncooperating writers.** | The external-writer tests lost the external change. Cooperating lock/claim tests preserved the loser only by refusing to write. |
| Prove zero destructive calls on unsupported mounts. | **Probe makes no production calls; engine behavior unverified.** | The probe writes only to its disposable temp directory. No engine call was made, so this is not evidence about an adapter. |
| Retain the proposed diff for manual application. | **Required decision; not implemented here.** | No automatic or engine-applied existing-note folder replacement is enabled. |

## Unsupported / unverified register

These are explicitly **not** passes:

- Every cloud-backed folder path (iCloud Drive, Dropbox, OneDrive, Google Drive
  for Desktop) — unvalidated, unsupported.
- SMB/CIFS and NFS mounts — unvalidated, unsupported.
- HFS+, ext4, XFS, btrfs, exFAT and FAT32 — unvalidated, unsupported.
- Linux `renameat2` semantics — untested here; namespace flags are not a
  content/version compare.
- Link lifetime, consent generation and exact-result binding — not testable
  without the production adapter.
- Journaling, receipt reconciliation, crash after acceptance, revoke/retry —
  not implemented and not tested.
- Producer A / reader workspace B consumption under a standing grant — not
  tested; blocked on a supported adapter.
- Settings destination/authority display — not changed and not tested.
- "Zero destructive calls on unsupported mounts" as an engine property — the
  probe itself makes no production calls, but that is not an adapter test.
- Inode stability across delete/recreate — not observed in 200 APFS cycles, but
  not guaranteed on any backend.

## Independent review

Recorded evidence:

```sh
python3 experiments/ciao-3626-folder-probe/probe.py \
  --iterations 3 \
  --aba-cycles 200 \
  --json /tmp/ciao-3626-review.json
```

Expected report: 11 failed, 3 supported, 1 partial, 1 unverified; the three
supported rows are the two cooperating-writer rows and `O_EXCL` create-only.
The probe exits 0 for that report because the harness ran. A non-zero exit means
the probe itself failed to run.

Check the recorded run against the code:

```sh
shasum -a 256 experiments/ciao-3626-folder-probe/probe.py
python3 - <<'PY'
import json
r = json.load(open("experiments/ciao-3626-folder-probe/results/macos-27.0.1-apfs.json"))
print(r["platform"])
print(r["summary"]["capabilities"])
PY
```

The recorded report's `platform.probe_sha256` must match the probe file used for
a review run. The probe is standard-library-only and does not import `ciao`; a
reviewer can copy just this directory to a disposable machine and run it there.
