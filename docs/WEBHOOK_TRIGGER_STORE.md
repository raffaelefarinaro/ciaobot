# Webhook trigger store (internal foundation, #981)

> **This is not a shipped webhook feature.** `ciao/webhooks.py` is the private
> configuration and credential store for the webhook feature tracked in #974,
> and it is the first child of it (#981). There is **no HTTP endpoint, no
> receiver, no dispatch, no chat creation and no startup wiring**: nothing in
> this engine constructs a `WebhookStore` today, and nothing you can call
> reaches it. Do not document a request shape, an auth header, a capability or
> a user recipe for webhooks anywhere yet — there is none to document, and a
> page that implies one would be wrong until the ingress child (#981's
> successors) actually ships. This document is the store's own contract, for the
> children that will build on it.

## What it is

One JSON document holding the triggers an operator has configured and, beside
each one, the SHA-256 verifier of that trigger's secret. It answers two
questions and refuses everything else:

- "which triggers exist for this workspace?" — `list`, `get`
- "does this secret authorize this trigger right now?" — `authenticate`

and it owns the writes that answer them: `create`, `update`, `rotate_secret`,
`revoke_workspace`.

## File schema

`{"schema": 1, "triggers": {"<trigger_id>": {"trigger": {...}, "secret_sha256": "..."}}}`

Keys are `uuid.uuid4().hex` (32 lowercase hex characters) — opaque, carrying
nothing about the target. `secret_sha256` is the lowercase hex SHA-256 of the
raw secret, or the empty string for a record whose verifier has been destroyed
by revocation. The public record is a flat object:

| field | type | notes |
| --- | --- | --- |
| `trigger_id` | string | `uuid4().hex`; must equal the key it is filed under |
| `name` | string | 1–120 characters after surrounding whitespace is trimmed |
| `workspace` | string | a registered-style workspace name (the rule `ciao.workspaces` uses) |
| `project_id` | string or `null` | `null` means the workspace's General project |
| `instructions` | string | at most 16,000 characters; empty is stored as given |
| `enabled` | boolean | `true` only after an explicit `update` |
| `mode` | string | `normal`, `auto` or `plan` |
| `input_policy` | string | `event_text` — the only value this child supports |
| `created_at`, `updated_at` | string | ISO-8601, UTC offset required |
| `revision` | integer | per-record, from 1, +1 per change to that record |

Reading is strict on purpose. A file that is not a schema-1 document, an unknown
`schema`, a record with a missing, mistyped or unknown field, a key that is not
an id this code mints, an id a record disagrees with, a timestamp with no UTC
offset, a revision that is not an integer ≥ 1, or a `secret_sha256` that is
neither 64 hex characters nor empty: each one raises and leaves the bytes
exactly as they were. **A store is never reset, migrated, repaired or silently
emptied.** The records in it are the only copy of what an operator configured,
and a "recovered" store would quietly delete every trigger. Migration is a
deliberate, separate change with its own schema number.

A missing file is the one benign case: it is a store that has never been
written, and reading it creates nothing — not the file, not its directory.

## API

```python
store = WebhookStore(path: Path, clock: Callable[[], datetime] | None = None)

store.list(workspace: str) -> list[WebhookTrigger]
store.get(trigger_id: str) -> WebhookTrigger
store.create(*, name: str, workspace: str, project_id: str | None = None,
             instructions: str, mode: WebhookMode = "auto") -> tuple[WebhookTrigger, str]
store.update(trigger_id: str, *, expected_revision: int, name: str | None = None,
             instructions: str | None = None, enabled: bool | None = None) -> WebhookTrigger
store.rotate_secret(trigger_id: str, *, expected_revision: int) -> tuple[WebhookTrigger, str]
store.revoke_workspace(workspace: str) -> int
store.authenticate(trigger_id: str, secret: str) -> WebhookTrigger | None
```

`path` is explicit and nothing is read from operator state, so the class is
inert until a later child decides where the engine's file lives (the plan names
`<runtime>/webhooks.json`). `clock` is the injectable UTC clock that stamps
records; the default reads `datetime.now(UTC)`.

`list` returns the workspace's triggers ordered by `(created_at, trigger_id)` —
total and stable, so the id breaks a same-second tie and the order never
depends on dict iteration. Reads take no lock: the file is replaced atomically,
so a lock-free reader sees one whole document.

`update` and `rotate_secret` take `expected_revision`, the revision the caller
read. A mismatch raises `revision_conflict` and writes nothing, so two callers
editing one trigger cannot silently overwrite each other; the loser re-reads and
decides again. An `update` that would change nothing returns the stored record
unchanged, with its own revision, and writes nothing.

Only `name`, `instructions` and `enabled` are mutable. **The target
(`workspace`, `project_id`) and the `mode` are not parameters at all**, because
retargeting an existing trigger, or escalating its permission mode under a
secret somebody already holds, is a trust change that needs its own design
rather than an optional argument.

## Credentials

- A secret is 32 random bytes from `secrets.token_urlsafe`, returned raw exactly
  once, by `create` and by `rotate_secret`, and only after the write that stores
  its verifier has succeeded. If the write fails, the secret is discarded and
  nothing is returned.
- Only `SHA-256(secret)` is stored, in a separate internal record from the
  public `WebhookTrigger`, so no dict, `repr`, traceback or public record can
  carry it. The internal record keeps the verifier out of its own `repr` too.
- Verification is `hmac.compare_digest` on the digest. Presented input is
  bounded (128 characters) and shape-checked before it is hashed, so an
  unbounded request cannot turn a check into unbounded work, and nothing is
  repaired: a secret with a trailing newline does not verify.
- A webhook secret is **not** a dashboard credential. The login password and the
  session cookie grant nothing here and cannot be presented to `authenticate`,
  whose whole signature is a trigger id and that trigger's own secret.
- `authenticate` returns a `WebhookTrigger` — a configuration record — not a
  principal, a user, a session or a permission.

## States, and the difference between them

- **Disabled by default.** A new trigger is stored with `enabled: false` and
  cannot authenticate until it is enabled on purpose, with the current
  revision. Configuring a trigger while it is disabled — renaming it, rewriting
  its instructions — is always allowed; being configured is not being callable.
- **Rotation replaces the credential.** `rotate_secret` invalidates the previous
  secret immediately and advances the revision. It is the way to recover a lost
  or exposed secret. It preserves `enabled` in both directions: rotating does
  not enable a disabled trigger (a secret is not consent to run anything), and
  rotating an enabled one leaves it enabled.
- **Revocation removes the authorization.** `revoke_workspace` disables every
  trigger in a workspace *and destroys its verifier*, advancing only the records
  that changed and returning how many. A second call returns 0 and writes
  nothing: it is idempotent. Because the verifier is gone, setting `enabled`
  back to `true` restores nothing — `update` refuses with `invalid_trigger`
  until the trigger has been rotated. That is what makes archive-and-restore
  safe: a workspace name that comes back cannot reactivate an old credential.
  Triggers in other workspaces are untouched.

## Privacy and durability on disk

The file and its advisory lock are created owner-private — `0o600` on POSIX, a
protected DACL on Windows, through `ciao.os_support.private` — and the mode of
the file being replaced is deliberately not carried over, so a store left
readable by anything else is tightened by the next write rather than preserved.

Every mutation re-reads the document *inside* its lock, so two store objects (or
two processes) cannot lose one another's records. The lock is
`ciao.async_reads.keyed_lock` for writers in this process plus an owner-private
advisory sibling lock (`<name>.lock`, via `ciao.os_support.locks`) for writers
in another. That combination is a **lock, not a sandbox**: it coordinates the
clients that take it, and it does not stop a local process that writes the file
without taking it.

A write is serialized to a unique sibling temp created by `mkstemp_private`,
flushed and fsynced, then moved into place with one `os.replace` (retried on
Windows). A reader sees the old document or the new one, never half of either,
and only that call's temp is cleaned up afterwards. If creating, writing or
replacing fails, the previous document stands and the call raises — a freshly
minted secret is never handed back as usable when it was not stored.

The store path and the lock path are both refused if they are links, contain a
`.` or `..` component, or name no file, and the open itself does not follow a
link (`O_NOFOLLOW` on POSIX, `FILE_FLAG_OPEN_REPARSE_POINT` on Windows).

## Errors

One class, `WebhookStoreError`, with a stable `code`:

| code | meaning |
| --- | --- |
| `invalid_trigger` | an argument is not a trigger this store will store: a wrong type, an empty or overlong name, a bad target, an unsupported mode or input policy, enabling a revoked trigger, or an `expected_revision` that is not a revision. Nothing was written. |
| `unsupported_schema` | the file is a schema this code does not implement. Never rewritten, never migrated. |
| `corrupt_store` | the file is there and cannot be read as a schema-1 document (bad JSON, a missing/mistyped/unknown field, a foreign id, an unreadable file). Never reset. |
| `not_found` | no such trigger id. |
| `revision_conflict` | `expected_revision` is not the record's current revision. Nothing was written. |
| `unsafe_path` | the store path or its lock is a link, holds a `.`/`..` component, names no file, is a directory, or cannot be opened. Nothing was written. |

A failed **write** (permissions, disk full, a refused replace) is *not* one of
these codes: it propagates as the `OSError` it is, because a write that did not
happen must not be reported as a store state.

## Deliberately not supported yet

- **Unattended `bypass`.** `bypass` is a real `BridgeMode` (`ciao.models`) and
  is not representable here. An unattended turn is a trust decision, and a
  foundation that can store it hands that decision to whoever writes the store
  next.
- **Caller-supplied prompts.** `input_policy` is `event_text` only: fixed
  instructions plus bounded event text. A caller-prompt mode is a separately
  approved per-trigger choice, and payload URLs are never fetched.
- **Membership validation.** `workspace` and `project_id` are checked for
  *shape* against the registry's own name rule. Whether the workspace is
  registered and live, and whether the named project exists, is a later
  service's obligation: an explicit project that has been deleted must fail
  rather than fall back to General, and that check cannot live here.
- **A principal, a session or a login.** There is none.

## What the later children owe

- **A2 — management and lifecycle.** Session-authenticated `/api/*` routes over
  these methods, and workspace archive → `revoke_workspace`, so archiving a
  workspace destroys its verifiers and restoring the name reactivates nothing.
- **A3 — ingress.** A bearer-secret route that never accepts a cookie, carries
  no secret in a query string, and fails closed on `corrupt_store` instead of
  answering "no" to everything. It re-reads the document per request, so a
  revocation takes effect on the next call; a check that has already returned is
  a snapshot, and ordering an already-accepted request against a revocation that
  lands afterwards is the receiver's problem, not this store's.
- **A4 — dispatch.** Turning an authenticated trigger into an ordinary chat, with
  the same provenance and the same approval behaviour as any unattended dispatch
  that is not an unattended bypass.
- **A5/A6 — surfaces.** The Automations UI, the agent CLI, user recipes,
  capabilities and public docs — all of which must describe only what has
  actually shipped.

Until a child ships, this file is the whole of the feature.
