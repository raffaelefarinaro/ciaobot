# Chat reliability and loading plan

Plan for five reliability/loading fixes adapted from audited open-source wrappers around
Claude Code and opencode (claude-code-server, claude-code-remote, claude-web,
opencode-studio, conduit). Sources were code-audited on GitHub 2026-08-22; license
constraints recorded per workstream (ideas-only vs copyable).

## Resume block

- Status: implemented (all workstreams including W3, user approval 2026-08-22)
- Current checkpoint: C9
- Next action: none — live PWA pass pending next deploy
- Blocker: none
- Implementation repository: /Users/raffaelefarinaro/repos/ciaobot (branch: develop)
- Generated plan output: docs/plans/CHAT_RELIABILITY_AND_LOADING_PLAN.md
- Visual companions: none (document only)
- Verified on: 2026-08-22 against ciao/transcripts.py, ciao/web/routes_chat.py,
  ciao/web/project_chats.py, ciao/web/routes_api.py, ciao/providers/claude.py,
  ciao/providers/opencode.py, ciao/local_session.py, web/src/stores/projects.ts,
  docs/plans/AGENT_ROOTS_PROGRESS.md

## Outcome and user value

1. Long chats load fast: the PWA fetches a bounded tail window first and pulls older
   pages on scroll-up; oversized tool/thinking content is pruned server-side and fetched
   per-item only when expanded.
2. No turn is ever lost: partial turns survive server restarts and provider aborts as
   `is_partial` records instead of disappearing.
3. opencode turns survive transient SSE drops via reconnect + reconciliation polling,
   instead of failing the whole turn.
4. (Optional) The handover flow can warn when an externally-started CLI session is live
   on the same workspace, using PID-liveness detection.

## Scope and non-goals

In scope:

- W1 Incremental transcript persistence with crash-safe partial flush
- W2 Verify-only checkpoint for claude stdout buffer handling (already solved in-repo)
- W3 Minimal native CLI session detection (PID liveness) — optional, last
- W4 Message history pagination + pruning + lazy part fetch (backend and PWA)
- W5 opencode SSE reconnect + message-poll reconciliation backstop

Out of scope for this release:

- Cursor-based incremental sync replacing the ~15s full re-poll of `/messages`
- Pruning of archived Markdown exports (`Logs/Chats/...`)
- IM/channel integrations, batch approval inbox, multi-device aggregation
- Codex provider SSE changes (codex uses stdio JSON-RPC, not SSE)

## Current-state evidence

Observed facts (file:line verified by repo exploration on 2026-08-22):

- Transcript writes happen only at end of turn. `TranscriptStore.record_turn`
  appends the whole turn dict then rewrites the entire JSON file
  (`ciao/transcripts.py:91-107`, `:303-308`). Call sites:
  `ciao/web/project_chats.py:5432` (normal turn) and `:6340` (scheduled/error).
  There is no incremental flush during streaming.
- WebSocket disconnects cannot abort a turn. `/ws/chat/{chat_id}`
  (`ciao/web/routes_chat.py:75`) runs the SDK call in a broker-managed background
  task; on client disconnect only `forward_task.cancel()` runs
  (`routes_chat.py:309-314`). So the claude-code-server "sticky cancel scope drops
  the partial assistant message" bug does not exist here in that form. The real gap
  is process-level: a server crash or provider abort mid-turn loses the whole turn
  (aborted-run symptoms previously logged in `docs/plans/AGENT_ROOTS_PROGRESS.md:218-247`).
- Claude stdout parsing is already safe. Ciaobot drives `claude-agent-sdk`
  `ClaudeSDKClient`, not raw subprocess reads (`ciao/providers/claude.py:878-883`),
  raises the SDK decode buffer to 32 MiB via `max_buffer_size`
  (`claude.py:171-185`, `:526-529`), and converts oversized-message errors into a
  recoverable ResultEvent (`:197-201`, `:943-966`). The chunked-read fix from
  claude-code-server is therefore unnecessary here.
- "Local session" today means git workspace state, not CLI detection.
  `LocalSessionManager.status()` returns `{git_repo, branch, dirty, dev_mode}`
  (`ciao/local_session.py:392-420`), served at `GET /api/local/status`
  (`ciao/web/app.py:381`). There is no scan for externally-started
  claude/codex/opencode processes anywhere (only `pgrep` for the desktop bundle,
  `ciao/desktop_build.py:455-462`). DevicePanel implements node host/client
  failover, not CLI takeover (`web/src/components/DevicePanel.vue:83-99`,
  `ciao/web/routes_node.py:261`).
- Chat history is one unbounded flat array. `GET /api/chats/{chat_id}/messages`
  returns `handover_messages + current/rendered/result` with no pagination
  (`ciao/web/app.py:255` → `ciao/web/routes_api.py:3109-3441`; response assembly
  `:3137`, `:3186`, `:3247`, `:3441`). Tool_use blocks are already collapsed to
  summaries (`_activity` / `_filecard`, `routes_api.py:3294-3328`), but thinking
  blocks pass through verbatim (`:3329-3341`) and nothing truncates on this path.
  Each connected client re-polls roughly every 15s and the route walks full SDK
  JSONL lineage per segment each time (`get_session_messages_full`, `:3202-3238`;
  poll comment `:3223-3227`).
- Frontend replaces the whole array on load. `loadMessages` /
  `loadMessagesFromServer` in `web/src/stores/projects.ts:2251-2335`
  (`api.get('/api/chats/${chatId}/messages')` at `:2295-2297`), with
  generation-guarded retries and mid-stream hydration suppression.
- opencode SSE has no in-turn recovery. The stream is opened before prompting
  ("subscribe before prompting", `ciao/providers/opencode.py:1782-1789`), decoded
  via `SSEDecoder` (`:1804-1805`), filtered by sessionID (`:1814-1818`), and breaks
  on first `session.idle` (`:1829-1830`). An `httpx.HTTPError` mid-stream yields an
  error ResultEvent and ends the turn — no reconnect, no reconciliation
  (`:1835-1842`). Read timeout is disabled so idle streams stay open (`:939-941`).

Assumptions (not yet verified against running system):

- A1: transcript JSON files for active chats are small enough that adding one
  append-only sidecar per turn introduces no meaningful disk pressure. To verify in W1.
- A2: PWA chat rendering cost scales with item count more than payload bytes;
  pagination alone will visibly fix load time even before pruning. To verify in W4.

Prior art (audited):

| Fix | Source | License |
| --- | --- | --- |
| Shielded final flush, store failures never block stream | KenyonY/claude-code-server `router.py` | none — ideas only |
| Chunked stdout reads | KenyonY/claude-code-server `agent.py` | none — ideas only (not needed here, see W2) |
| mtime-cached JSONL scan + `os.kill(pid,0)` liveness | gldc/claude-code-remote `native_sessions.py` | none — ideas only |
| Pagination envelope `{items,total,offset,limit,hasMore,nextOffset}`, server-side pruning budgets, `ocLazy` per-part fetch protocol | canxin121/opencode-studio `server/src/opencode_proxy.rs`, `opencode_session.rs` | MIT — reusable |
| Per-session poll backstop alongside event stream | dibstern/conduit `src/lib/domain/relay/Services/message-poller.ts` | MIT — reusable |

## Recommended direction

One direction per workstream:

### W1 — Append-per-event turn journal with shielded finalization

Add an append-only JSONL journal per turn next to the existing transcript store:
`.runtime/transcripts/<ctx>/journal/<turn_index>.jsonl`. Provider event consumers
append sanitized events (throttled to ≥250ms or every N events). On normal turn end,
`record_turn` proceeds exactly as today and the journal file is deleted. On startup
(or on next chat access), any leftover journal becomes an `is_partial: true` turn
record appended to the transcript, then the journal is removed. Finalization paths
(`record_turn`, partial recovery) run under `asyncio.shield()` so a cancelled scope
cannot interrupt the write mid-file. Store-write failures remain log-only and never
break the streaming path.

Reuse: existing `TranscriptStore` layout and archive flow untouched; journals are a
sidecar, not a format change.

### W2 — Verify-only: oversized-message resilience

No port needed. Add one focused regression test feeding an oversized tool_result
fixture through the claude consumer path asserting a recoverable ResultEvent and a
live stream. Confirms `max_buffer_size` wiring (`CIAO_CLAUDE_MAX_BUFFER_BYTES`).

### W3 — Native CLI session liveness (minimal)

New module `ciao/native_sessions.py`: scan `~/.claude/projects/**/*.jsonl` (mtime-keyed
cache, session id = filename stem) and `~/.claude/sessions/*.json` for
sessionId→pid maps; liveness via `os.kill(pid, 0)`. Expose
`GET /api/native/sessions?workspace=<path>` returning `{session_id, pid, cwd,
updated_at}` filtered to the workspace root. DevicePanel gains one warning row when a
live native session shares the node-handover target workspace; ChatPanel handover
confirm dialog surfaces it too. No adoption/resume functionality in this release.

### W4 — Paginated, pruned history with lazy part fetch

Backend:

- `GET /api/chats/{chat_id}/messages` gains optional query params
  `offset` (default 0 = newest end) and `limit` (default 50, cap 200). Response
  becomes `{items, total, offset, limit, hasMore, nextOffset}` where items keep the
  existing row shape. Omitting both params preserves today's unbounded array shape
  (compat for older clients); passing either param opts into the envelope.
- Server-side pruning budgets applied inside the window: thinking blocks truncated
  head(512)+tail(512) chars; any row whose source content was reduced carries
  `lazy: true` plus locator `{segment_id, item_id}`. Existing `_activity` /
  `_filecard` collapsing stays as-is.
- New endpoint `GET /api/chats/{chat_id}/messages/part?segment=<id>&item=<id>`
  returns the full content block for exactly that item, reading lineage once with a
  short TTL cache (1.2s, small LRU) shared with the list route walk.

Frontend (`web/src/stores/projects.ts` + chat panel):

- Initial chat load requests the newest window (`limit=50`).
- Scrolling above the loaded window prepends the previous page using
  `nextOffset`; spinner + preserve scroll anchor.
- Rows flagged `lazy` render the collapsed view; expanding triggers
  `getMessagePartDetail(locator)` with in-flight dedup keyed by locator; result
  merged into the row and `lazy` cleared.
- Mid-stream hydration suppression and generation guards kept unchanged.

### W5 — opencode reconnect + reconciliation poll

In `ciao/providers/opencode.py` turn loop: on SSE decode failure or premature
stream close (before `session.idle`), enter recovery instead of erroring: up to 3
reconnect attempts with short backoff; between attempts reconcile by fetching
`GET /session/{id}/message` every 2.5s and replaying unseen messages through the
normal event mapping. If `session.idle` is observed via poll, finish normally.
If the total recovery window (60s) expires without idle, synthesize a final
ResultEvent from `read_thread` data marked degraded rather than raising. Journal
(W1) records recovery markers for post-mortem.

## Alternatives and rejected options

### Rewrite transcript store to pure JSONL (no compact JSON)

Rejected: changes archive/export flows and every reader of the current format for no
user-visible gain; the sidecar approach gets crash safety without migration.

### Cursor-based incremental `/messages` sync (ETag/cursor)

Deferred to fog-of-war: bigger win long-term (kills the 15s full re-poll) but requires
a stable cursor contract across all three providers' lineages; envelope pagination
ships the user-visible benefit first.

### Adopting studio's Rust proxy layer wholesale

Rejected: Ciaobot's FastAPI layer already owns the serving path; the techniques port
in ~a day, a second proxy process adds deployment surface Ciaobot does not want.

### Full native-session adoption/resume (claude-code-remote style)

Rejected for this release: new product behavior beyond the audited fixes; only
detection + warning ships (W3 minimal).

## Visual review

Document only. The review question is architectural ordering and API shapes, which the
tables and workstream sections answer; no interaction/layout ambiguity warrants an HTML
companion.

## Decisions and hard-to-reverse bets

| ID | Decision | Rationale | Status |
| --- | --- | --- | --- |
| D-01 | Journal is an append-only JSONL sidecar; main transcript format unchanged | Crash safety without data migration; archives untouched | Proposed |
| D-02 | Pagination opt-in via `offset`/`limit` params; param-less responses keep legacy array shape | Older desktop/PWA clients keep working during rollout | Proposed |
| D-03 | Envelope shape `{items,total,offset,limit,hasMore,nextOffset}`; newest-end paging | Matches audited studio contract; `hasMore`+`nextOffset` avoid client arithmetic | Proposed |
| D-04 | Lazy marker `{lazy:true, segment, item}` resolved by dedicated part endpoint | One round trip per expanded item; dedup keyable | Proposed |
| D-05 | Thinking blocks pruned head512+tail512 when lazy; tool summaries unchanged | Thinking is the only verbatim-growth source found | Proposed |
| D-06 | W5 recovery window 60s, ≤3 reconnects, 2.5s poll cadence | Bounded failure modes; matches conduit cadence | Proposed |
| D-07 | W3 detection read-only; no adoption/resume | Keeps release scoped to warning UX | Proposed |

## Open questions with recommended defaults

| ID | Question | Recommended default | Status |
| --- | --- | --- | --- |
| Q-01 | Default page size (50) and cap (200)? | 50/200; tune after live check with largest real chat | Open |
| Q-02 | Should W3 also scan codex/opencode processes? | Claude only this release (JSONL format known); extend later | Open |
| Q-03 | Should archived `.md` exports gain truncation markers? | No — archives stay verbatim by design | Open |

## Not yet specified (fog of war)

- Replacing the ~15s per-client full re-poll with cursor-based incremental updates
  (depends on W4 envelope adoption and per-provider cursor semantics).
- Compaction policy for transcript stores once journals prove out (size ceilings,
  archival triggers).
- Whether scheduled/background turns should surface partial results in the PWA
  after crash recovery (product decision pending W1 field behavior).

## Feedback and decision log

| ID | Location | Feedback | Decision | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| F-01 | Scope | User: filter to Python-relevant fixes; no new feature categories like IM | IM/batch/multi-machine dropped; mechanics-only kept | implemented | chat 2026-08-22 |
| F-02 | Scope | User: implement all audited fixes | All five mapped; W2 downgraded to verify-only after evidence | implemented | Current-state evidence |
| F-03 | Evidence | Grounding contradicted two assumptions: "local session" is git state, not CLI detection; stdout chunk-read already covered by SDK buffer config | Plan rewritten around real gaps (crash-safe journal, pagination/pruning, SSE recovery); W3 rescoped to optional minimal detection | implemented | local_session.py:392-420; claude.py:171-201 |
| F-04 | D-01 | Shielded finalization is unnecessary: `record_turn` / journal finalization are synchronous, so asyncio cancellation cannot interrupt them mid-write; the shield requirement is satisfied trivially | Implemented without shield wrappers; rationale recorded in stream_chat comment | implemented | project_chats.py `_journalled_stream` block |
| F-05 | D-04 | Lazy locator shipped as a single absolute index `i` instead of `{segment_id, item_id}` | Simpler and provider-agnostic — one locator scheme works across claude lineage, codex/opencode threads, transcripts, and archived fallbacks because it indexes the assembled list rather than provider internals | implemented | routes_api.py `_prune_rows_for_wire`, `chat_message_part` |
| F-06 | W2 | Both regression tests already existed in tests/test_providers.py (`test_claude_run_streaming_recovers_from_oversized_message`, `test_claude_options_raise_sdk_buffer_above_default`) | No new test needed; both verified green | implemented | tests/test_providers.py:808, :879 |
| F-07 | W5 | Degraded result reuses the existing `ResultEvent.fallback_final` flag instead of adding a new field; clean poll reconciliation sets `fallback_final=False` | Avoids a wire-format change; semantics documented in code | implemented | opencode.py `run_streaming` tail |

## Implementation outcome (2026-08-22)

- **W1** — `TurnJournal` + `TranscriptStore.open_turn_journal/record_partial_turn/recover_journals` in
  `ciao/transcripts.py`; wired into `ProjectChatManager.stream_chat`; recovery runs once at manager init.
  Tests: `tests/test_turn_journal.py`.
- **W2** — verified already-covered; no changes.
- **W3** — `ciao/native_sessions.py` (`NativeSessionScanner`, `live_sessions_for_workspace`),
  `GET /api/native/sessions`, warning row in `DevicePanel.vue`. Tests: `tests/test_native_sessions.py`.
- **W4** — envelope `{items,total,offset,limit,hasMore,nextOffset}` + thinking pruning + lazy markers +
  part endpoint with TTL cache (`ciao/web/routes_api.py`); store-side index-addressed merge,
  `loadOlderMessages`/`expandMessagePart` (`web/src/stores/projects.ts`); scroll-up paging with anchor +
  lazy reasoning button (`web/src/components/ChatPanel.vue`). Tests:
  `tests/test_chat_messages_pagination.py`. Docs: PWA_API.md routes table + notes.
- **W5** — reconnect loop (≤3) + `_reconcile_interrupted_turn` poll backstop with quiescence signature in
  `ciao/providers/opencode.py`. Tests: `tests/test_opencode_sse_recovery.py`.

Verification evidence: full suite 2889 passed / 1 skipped after fixes (including
`test_architecture_doc` after indexing `native_sessions.py`); `cd web && npm run build` clean.

## Implementation checkpoints

Each checkpoint has an exit condition so another model can tell whether the work is
actually ready to move on.

### C4. W4 backend — paginated, pruned history

- Add envelope + params to `chat_messages` (`ciao/web/routes_api.py`), pruning +
  lazy locators, part endpoint with shared TTL/LRU cache.
- Tests: pagination math (empty/short/long), compat shape without params, pruning
  budgets, part endpoint 404/valid cases.

Exit evidence: `pytest tests/ -k messages` green including new tests; envelope shape
documented in docstring.

### C5. W4 frontend — windowed load + lazy expand

- Tail-window initial load, scroll-up prepend, lazy expand + dedup in
  `web/src/stores/projects.ts` and chat panel components.
- Keep generation guards and hydration suppression semantics.

Exit evidence: `cd web && npm run build` clean; manual check that opening a long chat
fetches one bounded request and scroll-up pages.

### C6. W1 — turn journal + shielded finalization

- Journal writer in provider event consumers (throttled), finalization under
  `asyncio.shield`, startup recovery to `is_partial` records, journal cleanup.
- Cover all providers' turn paths via the shared orchestration point
  (`project_chats.py:5432` area), not per-provider copies.

Exit evidence: unit tests simulate crash-mid-turn (kill task, restart store) and
assert `is_partial` recovery; normal turns leave no journal files behind.

### C7. W5 — opencode recovery loop

- Recovery state machine in `opencode.py` turn loop per D-06; degraded-result path
  via `read_thread`.

Exit evidence: tests with a scripted flaky SSE fixture assert reconciliation replays
missed events and idle-via-poll finishes cleanly; `pytest tests/` full suite green.

### C8. W2 + W3 — regression test, optional detection

- Oversized-tool_result regression test (W2).
- `ciao/native_sessions.py` + endpoint + DevicePanel/handover warnings (W3),
  only if approved at review time.

Exit evidence: focused tests green; W3 explicitly accepted or deferred in feedback log.

### C9. Verify and close

- Full `pytest tests/`; `cd web && npm run build`.
- Live PWA verification through the normal deploy/reload workflow only.
- Update plan status; record verified vs merged-only behavior.

## Verification and rollout

- Order: C4 → C5 → C6 → C7 → C8 → C9. Each checkpoint lands with its own tests;
  no cross-checkpoint rebases.
- Backend: focused pytest runs per checkpoint, full suite before close.
- Frontend: `npm run build` after each frontend-touching checkpoint; manual PWA pass
  (long-chat open, scroll-up, lazy expand, kill-server-during-turn recovery) via the
  standard deploy/reload workflow.
- Rollback: W1/W4/W5 are independently revertible; journal sidecars are inert if the
  recovery code is reverted; envelope params default-off for old clients.
