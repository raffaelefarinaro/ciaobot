# Webhook trigger store, ingress receiver and dispatch (#981, #1010, #1020)

> **This is not a finished webhook feature.** `ciao/webhooks.py` owns the
> private configuration and credential store for the webhook feature tracked in
> #974, plus the ingress receiver that records what a secret caused;
> `ciao/webhook_dispatch.py` turns an accepted event into an ordinary chat.
> What ships today is: management routes over the store (A2), one
> bearer-authenticated endpoint that records a durable receipt for an accepted
> event (A3), and the dispatch that launches that event as an ordinary project
> chat (A4). What does **not** ship is the part a user would call a *surface* —
> **there is no Automations UI, no receipt history page, no CLI, skill or
> recipe**: a `202` from the receiver means "your event is recorded", not "a
> turn ran", and no page, skill or recipe may claim otherwise. This document is
> the store's, the receiver's and the dispatcher's own contract.

## What it is

One JSON document holding the triggers an operator has configured and, beside
each one, the SHA-256 verifier of that trigger's secret. It answers two
questions and refuses everything else:

- "which triggers exist for this workspace?" — `list`, `get`
- "does this secret authorize this trigger right now?" — `authenticate`

and it owns the writes that answer them: `create`, `update`, `delete`,
`rotate_secret`, `revoke_workspace`.

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
store.delete(trigger_id: str, *, expected_revision: int) -> None
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

The store file and the lock file are both refused if the file itself is a link, if
the path contains a `.` or `..` component, or if it names no file, and the open
itself does not follow a link (`O_NOFOLLOW` on POSIX,
`FILE_FLAG_OPEN_REPARSE_POINT` on Windows). Only the final component is checked
for being a link: a link in a parent directory is a layout choice, not a
threat to this store — macOS resolves `/var` to `/private/var` on every install.

## The ingress receipts (#1010)

`POST /hooks/v1/{trigger_id}` records one `WebhookReceipt` per accepted event in
`<runtime>/webhook-receipts.jsonl`, beside the store. It is append-only JSONL,
0600, one `fsync` per row, latest row per id wins — the same protocol as
`Memory-Receipts.jsonl`, and deliberately *not* `job_runs.py`'s (unlocked,
fail-open). A whole-document trim bounds it in bytes and rows and never drops a
row that is still open.

| field | meaning |
| --- | --- |
| `id` | `wbrcpt_<sha256(trigger_id\|key)[:20]>`, plus a `.2`, `.3` generation for a later attempt of the same key |
| `trigger_id`, `trigger_name`, `workspace`, `project_id` | where the accepted event was headed, copied from the authenticated trigger so the record survives the trigger's own later deletion |
| `idempotency_key` | the sender's key, as received |
| `status` | `accepted`, `launched`, `failed` or `interrupted` |
| `body_digest` | SHA-256 of the request body **as it arrived**, hex |
| `event_text` | the sender's event text, at most 8,000 characters |
| `created_at`, `updated_at` | ISO-8601, UTC |
| `detail` | why a receipt failed or was interrupted; empty otherwise |

No credential is in it: the receipt names the trigger, never the secret that
authorized it.

**Dedupe.** `(trigger_id, idempotency_key)` names one attempt. The same key with
the same body returns the receipt already written and appends nothing; the same
key with a *different* body raises `idempotency_conflict` and leaves the recorded
event untouched — a key names one event, and picking which of two bodies to run
is not a decision this receiver may make. Past `DEDUPE_RETENTION_DAYS` (7) the
key is the sender's to reuse: the new attempt takes the next generation rather
than folding onto the old receipt, which would erase how it settled.

**Bounds.** `MAX_BODY_BYTES` (65536), `RATE_LIMIT_PER_MINUTE` (10) on an
in-process per-trigger sliding window — the credential *is* the caller, and a
shared IP is not — and `MAX_PENDING_RECEIPTS` (20) receipts left open per
trigger (`accepted` or `launched`). Dispatch settles a receipt in seconds, so the
pending bound is not what a healthy sender meets; it is what a stopped engine,
an unresolvable target, or a crash in the launch window amount to, and it is
what makes an unattended sender fill up and be told so with a `503` rather than
accumulating work nobody is doing.

**Ordering, which is the whole point.** The `accepted` row is durable before any
launch is attempted, and the `launched` allocation is durable before the model
turn. A crash in that second window is genuinely ambiguous — the turn may or may
not have started — so `recover_interrupted` records `interrupted` with a detail
saying it needs review. Nothing replays it: a second unattended run is a cost
nobody asked for, and the receipt is the evidence that something already
happened.

**Deliberately not here.** The launch itself (`begin_launch` and `settle_failed`
are the dispatcher's entry points, and nothing in `ciao/webhooks.py` calls
them), a receipt-list API, outbound callbacks, and any provider-specific
signature adapter — the bearer secret is the whole authentication story.

## Dispatch (#1020)

`ciao/webhook_dispatch.py` turns one accepted receipt into one ordinary chat.
Two callers, both deliberately thin: `ciao/web/routes_hooks.py` schedules
`dispatch_receipt` off the response path, and `ciao/main.py` calls
`resume_pending` once at startup for the events a restart left `accepted`.

**The response is a receipt, not a launch result.** The `202` does not wait on
the model turn. A turn can run for minutes, and a sender has no way to read
progress from it; what it gets back is the receipt id, and the chat the launch
created is an ordinary one the operator opens like any other.

**An ordinary chat, with no escalation.** `ProjectChatManager.start_stream` is
called as `start_stream(chat_id, prompt)` — the `unattended` flag is never
named. That flag is what `_effective_mode_for_chat` turns into `bypass` for any
non-plan chat, and a webhook is not authorization for it: the trigger's
authorization was for the trigger, not for the permissions its turn runs under.
So the turn runs with the trigger's configured mode (`normal`, `auto` or `plan`
— the only modes this store can represent), the chat takes its model and
provider from the operator's own Settings, and an approval card raised in the
turn is an ordinary approval card that surfaces in Needs-you, exactly as it does
for a wake turn or the `chat_prompt` route.

**The sender's only input is the event text.** The prompt is the trigger's
`instructions` followed by the receipt's `event_text`, quoted inside a fixed
`<webhook-event>` fence and labelled as data. A fence tag inside the payload is
escaped, so a sender cannot close the fence and have the rest of its own text
read as Ciaobot's framing. Nothing else in the body is a field the receiver
accepts at all (`{"text": …}` is the whole schema), so "a sender cannot choose
the workspace, project, model or permission" is a property of the shape rather
than a blocklist of field names to keep current.

**The target is the trigger's, and it has to resolve.** The pinned
`project_id` must exist *and* belong to the trigger's workspace; `null` means
that workspace's `General`. A deleted project, a project in another workspace, or
a workspace with no `General` each settle the receipt `failed` with a detail
naming the reason. Never a silent `General` fallback: the one thing an operator
must be able to trust is that deleting a project stops work arriving in it. A
trigger deleted between accepting the event and dispatching it settles `failed`
too — its instructions are gone, so there is no turn to build.

**The off switch reaches an event already in the journal.** A receipt launches
only while its trigger is still configured *and* enabled. `enabled` gates
authentication at the door and it gates the launch too, so an operator who
disables or revokes a trigger does not find it running turns that were accepted a
moment earlier. `revoke_workspace` disables *and* destroys the verifier, so that
one check is what keeps archive-and-restore safe here as well: an event accepted
just before the archive settles `failed` rather than launching into a workspace
whose old secret is already dead.

**What the three settled states mean.**

| status | meaning | what happens next |
| --- | --- | --- |
| `launched` | the chat was created and the turn was started. **This is the success outcome**, not a pending one: the turn is now an ordinary chat and its progress lives there, not in the receipt. | nothing; the pending bound stops counting it only when it is terminal, so it still occupies the trigger until the journal is trimmed |
| `failed` | the launch was attempted and did not complete — an unresolvable target, a trigger that is gone, or a turn that would not start. Terminal on purpose: a failed launch is an event that happened and failed, and re-running it is the operator's decision. | the operator fixes the target or retargets the trigger; the sender's next delivery with a new key is a new attempt |
| `interrupted` | the allocation was recorded but the outcome was not, so a process died inside the launch window. **Needs an operator.** | never replayed automatically — a second unattended turn is a cost nobody asked for — and a dispatch of an `interrupted` receipt is a no-op |

**The ordering, and the startup sweep.** `begin_launch` is durable *before*
`start_stream` is called, which is what makes `interrupted` honest rather than a
guess. At startup, `resume_pending` runs `recover_interrupted` first — so the
journal says what is ambiguous before any new turn starts — and then dispatches
the remaining `accepted` receipts, oldest first, once. Once because dispatch is
idempotent by receipt state: after a sweep each receipt it touched is `launched`
or `failed`, so a sweep that runs twice starts no second turn. The sweep is
bounded (`MAX_RESUMED_LAUNCHES`, 20): a journal that accumulated `accepted` rows
while nothing dispatched them would otherwise start a turn per row at boot, and
anything past the bound stays `accepted` for an operator to look at.

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

The receiver raises `WebhookReceiverError` with a code of its own, and the route
maps codes to statuses rather than to exception classes, so adding a reason
cannot accidentally pick a status:

| code | HTTP | retryable | meaning |
| --- | --- | --- | --- |
| `invalid_event` | 400 | no | the body is not a JSON object, or not exactly `{"text": ...}` with bounded non-empty text |
| `invalid_idempotency_key` | 400 | no | the key is missing, empty, over 200 characters, or holds a control character |
| `idempotency_conflict` | 409 | no | this `(trigger, key)` was accepted with a different body; the recorded event is untouched |
| `payload_too_large` | 413 | no | the body is over `MAX_BODY_BYTES` |
| `rate_limited` | 429 | yes | over `RATE_LIMIT_PER_MINUTE` for this trigger; `Retry-After` says one window |
| `too_many_pending` | 503 | yes | the trigger already has `MAX_PENDING_RECEIPTS` open receipts |
| `receipt_unavailable` | 503 | yes | the journal could not be read, locked or appended to — the event was **not** recorded |
| `invalid_receipt` | 500 | no | a journal row cannot be decoded as a receipt this code wrote; failed closed rather than read as absent |

## Deliberately not supported yet

- **Unattended `bypass`.** `bypass` is a real `BridgeMode` (`ciao.models`) and
  is not representable here. An unattended turn is a trust decision, and a
  foundation that can store it hands that decision to whoever writes the store
  next.
- **Caller-supplied prompts.** `input_policy` is `event_text` only: fixed
  instructions plus bounded event text. A caller-prompt mode is a separately
  approved per-trigger choice, and payload URLs are never fetched.
- **Membership validation in the store.** `workspace` and `project_id` are
  checked for *shape* against the registry's own name rule, and that is all this
  store will ever do: refusing an unregistered workspace name here would make it
  own workspace lifecycle. Whether the workspace is registered and live, and
  whether the named project exists, is dispatch's obligation — and dispatch fails
  the launch rather than falling back to General.
- **A principal, a session or a login.** There is none.

## What the later children owe

- **A2 — management and lifecycle.** Session-authenticated `/api/*` routes over
  these methods (`ciao/web/routes_webhooks.py`: list, create, update, rotate and
  delete; a raw secret is returned once, by create/rotate only), and workspace
  archive → `revoke_workspace`, so archiving a workspace destroys its verifiers
  and restoring the name reactivates nothing. Shipped in #1001.
- **A3 — ingress.** A bearer-secret route that never accepts a cookie, carries
  no secret in a query string, and fails closed on `corrupt_store` instead of
  answering "no" to everything. It re-reads the document per request, so a
  revocation takes effect on the next call; a check that has already returned is
  a snapshot, and ordering an already-accepted request against a revocation that
  lands afterwards is the receiver's problem, not this store's. Shipped in #1010
  — `POST /hooks/v1/{trigger_id}` (`ciao/web/routes_hooks.py`) plus
  `WebhookReceiver` here. **It does not dispatch**: it records an `accepted`
  receipt and answers `202`.
- **A4 — dispatch.** Turning an authenticated trigger into an ordinary chat, with
  the same provenance and the same approval behaviour as any dispatch that is not
  a bypass. It calls `WebhookReceiver.begin_launch` before the model turn and
  `settle_failed` when the attempt does not complete, and it calls
  `recover_interrupted` at startup: a receipt left `launched` is genuinely
  ambiguous, so it is recorded `interrupted` and reviewed, never replayed. Shipped
  in #1020 — `ciao/webhook_dispatch.py`, with the receiver answering `202`
  without waiting on the turn and `main.py` sweeping at startup. See
  **Dispatch** above.
- **A5/A6 — surfaces.** The Automations UI and receipt history, the agent CLI,
  user recipes, capabilities and public docs — all of which must describe only
  what has actually shipped. Until they do, no surface may present a webhook
  event as though the receiver had run it.

Until a child ships, this file is the whole of the feature.
