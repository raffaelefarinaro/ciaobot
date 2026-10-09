# Webhook trigger store, ingress receiver and dispatch (#981, #1010, #1020, #1034, #1039)

> **This is the whole of the webhook feature.** `ciao/webhooks.py` owns the
> private configuration and credential store for the webhook feature tracked in
> #974, plus the ingress receiver that records what a secret caused;
> `ciao/webhook_dispatch.py` turns an accepted event into an ordinary chat.
> What ships is: management routes over the store (A2), one bearer-authenticated
> endpoint that records a durable receipt for an accepted event (A3), the
> dispatch that launches that event as an ordinary project chat (A4), the
> Automations page that manages the triggers (A5), and the agent's own
> management surface — the `ciao webhook` noun and the matching operations —
> together with the user/sender recipes and the capability text that describes it
> (A6), and the session-authenticated **receipt-history read** over the same
> journal, with a per-trigger history in that Automations page (#1044). What the
> whole set does **not** ship: outbound callbacks, provider-specific signature
> adapters, public tunnels, model overrides. A `202` from the receiver means "your
> event is recorded", not "a turn ran", and no page, skill or recipe may claim
> otherwise. This document is the store's, the receiver's and the dispatcher's own
> contract.

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
             instructions: str) -> tuple[WebhookTrigger, str]
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
(`workspace`, `project_id`) is not a parameter at all**, because retargeting an
existing trigger under a secret somebody already holds is a trust change that needs its own design
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
| `status` | `accepted`, `launching`, `launched`, `failed` or `interrupted` |
| `body_digest` | SHA-256 of the request body **as it arrived**, hex |
| `event_text` | the sender's event text, at most 8,000 characters |
| `created_at`, `updated_at` | ISO-8601, UTC |
| `detail` | why a receipt failed or was interrupted; on a `launched` receipt, the chat the event became |

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
trigger (`accepted` or `launching`). Dispatch settles a receipt in seconds, so the
pending bound is not what a healthy sender meets; it is what a stopped engine,
an unresolvable target, or a crash in the launch window amount to, and it is
what makes an unattended sender fill up and be told so with a `503` rather than
accumulating work nobody is doing. A settled receipt — a `launched` one included
— hands its slot back, so a trigger whose events all succeed keeps accepting.

**Ordering, which is the whole point.** The `accepted` row is durable before any
launch is attempted, the `launching` allocation is durable before the model turn,
and the `launched` outcome is a third row written after the turn starts. A crash
in the window between the allocation and the outcome is genuinely ambiguous — the
turn may or may not have started — so `recover_interrupted` records
`interrupted` with a detail saying it needs review. Nothing replays it: a second
unattended run is a cost nobody asked for, and the receipt is the evidence that
something already happened. The allocation is deliberately *not* the outcome:
one state cannot honestly mean both, and conflating them rewrote every healthy
launch as a crash at the next boot.

**Deliberately not here.** The launch itself (`begin_launch`, `settle_launched`
and `settle_failed` are the dispatcher's entry points, and nothing in
`ciao/webhooks.py` calls them), outbound callbacks, and any provider-specific
signature adapter — the bearer secret is the whole authentication story.

## The receipt history (#1044)

A read surface over the same journal, so "what has this trigger received, and
which chat did each event become" has an answer that does not require reading a
JSONL file by hand. Two session-authenticated, workspace-scoped reads, beside the
management routes in `ciao/web/routes_webhooks.py`:

- `GET /api/webhooks/{trigger_id}/receipts?workspace=` — one trigger's events,
  beside its `trigger_id`, `trigger_name` and the `limit` it was read with.
- `GET /api/webhooks/receipts?workspace=` — the same rows for a whole workspace,
  across every trigger.

Both take the ordinary session cookie like every other `/api/*` route, and both
answer an unregistered or absent `?workspace=` with a **400** and a trigger that
is not that workspace's with the same **404** as an unknown id — a history is not
a way to confirm what another workspace has configured. The workspace-wide read
filters on each receipt's **own** `workspace` field rather than on the triggers
that exist now, which is what keeps a receipt readable after its trigger is
deleted, renamed or retargeted.

**The row is a projection, not the journal line.** `WebhookReceipt.to_public_dict`
is the only shape that leaves the engine: `receipt_id`, `trigger_id`,
`trigger_name`, `status`, `chat_id`, `event_text`, `created_at`, `updated_at` and
`detail`. No verifier — and deliberately none of the journal's own retry
machinery either: the sender's `idempotency_key` and the request `body_digest`
are what collapses a retry, and neither is what "what arrived?" asks for.
`chat_id` is derived from the `launched` receipt's `detail` (`"chat <id>"`) so a
caller never parses a chat id out of an engine's own prose, and `detail` stays
beside it because on a `failed` or `interrupted` row it is the only sentence
explaining what happened.

**The read is bounded, and bounded where the trim is not.** `receipts_for()`
(fold the whole journal) exists because a dedupe decision genuinely has to know
about every row; the history read is a *question* about the recent tail, so it
walks the file backwards in `_REVERSE_WINDOW_BYTES` windows
(`_iter_rows_newest_first`) and stops at `RECEIPTS_HISTORY_LIMIT` (50). Reading
fifty rows costs fifty rows of memory rather than the whole journal's, and one
row per receipt id is all that is kept even though the journal holds three lines
for an event that walked `accepted → launching → launched` — walking backwards,
the first row seen for an id *is* that receipt's effective state, which is what
makes the early stop safe. The rows behind the cap are still in the journal and
still trimmed only by the journal's own bounds (4 MiB / 4000 settled rows);
`limit` is a cap on what a page draws, not on what is kept.

**Retention, stated honestly.** `DEDUPE_RETENTION_DAYS` (7) is a *dedupe*
window: it decides whether a sender reusing an `Idempotency-Key` collapses onto an
earlier receipt or takes the next generation of the id. A settled receipt stays
readable past it, and what ages out is the key, never the record of what
happened. Nothing here promises more than the journal keeps — once the trim drops
a settled row, it is gone, and that is the only thing that erases it.

**What the UI draws.** `web/src/components/WebhookTriggers.vue` gained a
per-trigger history under the trigger's row: one line per receipt with its
outcome in words, when it arrived, and a button to open the chat a `launched`
event became. An `interrupted` row is drawn like any other, because it is a
record that needs a person and not an error to hide. A sender still cannot read
any of this back — the surface is the operator's, and `202` remains the sender's
whole answer.

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
non-plan chat, and a webhook does not set it. The chat is created like any new
chat: its permission mode, model and provider are the operator's new-chat
defaults from Settings (so a `bypass` default applies here too — a deliberate
choice, one setting rather than a second per-trigger one), and an approval card raised in the
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

**What the states mean.** Two are open (the event is still in flight), three are
settled, and only the two open ones count against the pending bound:

| status | meaning | what happens next |
| --- | --- | --- |
| `launching` | **not an outcome**: the allocation is on disk and the turn has not reported back. The only state in which a crash is ambiguous. | a live dispatch settles it `launched` or `failed`; a process that died here is recorded `interrupted` at the next startup. It still occupies the trigger's pending slots while it is open |
| `launched` | the chat was created and the turn was started, and the receipt says so. **This is the success outcome**, not a pending one: the turn is now an ordinary chat and its progress lives there, not in the receipt. The `detail` names the chat. | nothing; it is terminal, so the pending bound stops counting it and the journal may trim it like any other settled row |
| `failed` | the launch was attempted and did not complete — an unresolvable target, a trigger that is gone, an allocation the journal would not record, or a turn that would not start. Terminal on purpose: a failed launch is an event that happened and failed, and re-running it is the operator's decision. | the operator fixes the target or retargets the trigger; the sender's next delivery with a new key is a new attempt |
| `interrupted` | the allocation was recorded but the outcome was not, so a process died inside the launch window. **Needs an operator.** | never replayed automatically — a second unattended turn is a cost nobody asked for — and a dispatch of an `interrupted` receipt is a no-op |

**The ordering, and the startup sweep.** `begin_launch` is durable *before*
`start_stream` is called, which is what makes `interrupted` honest rather than a
guess, and `settle_launched` records the success and the chat id as soon as
`start_stream` returns. At startup, `resume_pending` runs `recover_interrupted`
first — so the journal says what is ambiguous before any new turn starts — and
then dispatches the remaining `accepted` receipts, oldest first, once. Once
because dispatch is idempotent by receipt state: after a sweep each receipt it
touched is `launched` or `failed`, so a sweep that runs twice starts no second
turn. The sweep is bounded (`MAX_RESUMED_LAUNCHES`, 20): a journal that
accumulated `accepted` rows while nothing dispatched them would otherwise start a
turn per row at boot, and anything past the bound stays `accepted` for an
operator to look at.

**One receipt, one turn.** Receipt state cannot carry that on its own — reading
`accepted` and then writing the allocation spans several `await` points, so a
sweep that read its list of accepted receipts while a request was being served
could dispatch the same receipt twice — so `dispatch_receipt` claims the receipt
in an in-process `_IN_FLIGHT` set before its first `await` and the loser does
nothing at all. There is one engine and one journal, so the only two callers that
can race are in the same process.

**A crash between the chat and the allocation leaves an orphan chat.** The chat
is created before `begin_launch`, so a process that dies in between leaves an
empty chat that will never carry a turn, and the sweep then creates a second chat
for the same event. It is harmless — no turn ran, and one empty chat is not a
record of anything — but it is why a project may show a "New Chat"-titled chat
that no event ever ran in.

## Errors

One class, `WebhookStoreError`, with a stable `code`:

| code | meaning |
| --- | --- |
| `invalid_trigger` | an argument is not a trigger this store will store: a wrong type, an empty or overlong name, a bad target, an unsupported input policy, enabling a revoked trigger, or an `expected_revision` that is not a revision. Nothing was written. |
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

## Deliberately not supported

Every item below was rechecked against the shipped code when the last child
(#1039) landed, and every one of them is still true: none is an oversight
waiting for a later release, and each is a refusal somebody could otherwise
have taken.

- **A per-trigger permission mode.** Triggers used to store `normal`/`auto`/
  `plan`; that was a second permission setting beside the new-chat default, and
  it was removed. A record still carrying `mode` is an unknown key, so a
  schema-1 store written before the removal reads as `corrupt_store`: delete
  `<runtime>/webhooks.json` and recreate its triggers. Dispatch never passes
  `unattended`, so the forced-`bypass` path for automations does not apply.
- **Caller-supplied prompts.** `input_policy` is `event_text` only: fixed
  instructions plus bounded event text. A caller-prompt mode is a separately
  approved per-trigger choice, and payload URLs are never fetched.
- **Membership validation in the store.** `workspace` and `project_id` are
  checked for *shape* against the registry's own name rule, and that is all this
  store will ever do: refusing an unregistered workspace name here would make it
  own workspace lifecycle. Whether the workspace is registered and live, and
  whether the named project exists, is dispatch's obligation — and dispatch fails
  the launch rather than falling back to General. The one place a *caller* is
  checked is the control plane (`workspace_webhook_*`), which refuses an
  unregistered workspace name before it reaches the store at all.
- **A principal, a session or a login.** There is none. Management is
  session-authenticated (`/api/*`) or agent-scoped (`ciao webhook`); ingress is
  authorized by one trigger's own secret. No surface turns a webhook secret into
  a user, a session or a permission.
- **Outbound callbacks, provider-specific signature adapters, public tunnels and
  model overrides.** The bearer secret is the whole authentication story, a
  trigger decides only where its events run, and a sender never reaches model
  selection.

## What the children owed, and what shipped

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
  a bypass. It calls `WebhookReceiver.begin_launch` before the model turn,
  `settle_launched` once `start_stream` has returned (with the chat id, so a
  settled receipt names the chat the event became) and `settle_failed` when the
  attempt does not complete, and it calls `recover_interrupted` at startup: a
  receipt left `launching` is genuinely ambiguous, so it is recorded `interrupted`
  and reviewed, never replayed, while a `launched` one is left as the success it
  recorded. Shipped in #1020 — `ciao/webhook_dispatch.py`, with the receiver
  answering `202` without waiting on the turn and `main.py` sweeping at startup.
  See **Dispatch** above.
- **A5 — the Automations UI.** The section on the Automations page that lists a
  workspace's triggers, creates one, reveals and copies its one-time secret (and
  says the old one is dead after a rotation), shows the sender's own recipe, and
  enables, disables, rotates and deletes. Shipped in #1034. It did not show
  receipt history then, because there was no receipt-list API to render; #1044
  added that read and this section's per-trigger history under the row (see **The
  receipt history** above).
- **A6 — the agent surface, the recipes and the capabilities text.** Scoped
  management from a chat: `ciao webhook list|create|update|rotate|delete`
  (`ciao/agent_cli.py`) over `workspace_webhook_*` (`ciao/control_plane.py`) and
  the five operations in the shared table (`ciao/mcp_server.py`) — each
  revision-checked exactly as the routes are, each returning the public record
  and, for create and rotate only, the one-time secret, which is never logged or
  stored in the clear. Alongside them: the manage recipe and the external sender
  recipe in `PWA_API.md`, the noun/verb table and the shown-once caveat in the
  `ciao-cli` skill, and the Webhook triggers subsection in `ciao-capabilities`.
  Shipped in #1039, verified end to end by `tests/test_webhook_journey.py`:
  configure → signed request → accepted receipt → exactly one ordinary chat,
  with a duplicate collapsed onto the same receipt and a same-key different-body
  refused `409`.

Every child of #974 has now shipped, so nothing is left on this list and this
document is the whole of the feature: a surface added later has to describe what
is here rather than what was once planned. The one thing the original plan named
that no child of #974 built — a receipt-history view — shipped afterwards in
#1044 as a read surface over this journal (see **The receipt history** above),
with the bounded cap and the retention truth stated there.
