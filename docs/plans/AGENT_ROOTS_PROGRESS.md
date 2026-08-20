# Agent Roots — implementation progress ledger

Companion to `AGENT_ROOTS_WORK_ORDER.html`. The HTML stores checkbox state in the
browser only, so this file is the durable record. Coordinator: Ciaobot main chat.
Implementation: opencode delegate subchats. Repo: `~/repos/ciaobot`, branch `develop`.

Legend: `TODO` · `RUNNING` · `REVIEW` (delegate done, coordinator verifying) ·
`DONE` (reviewed + tests green + committed) · `BLOCKED`

## Pre-flight verification (2026-08-19, coordinator)

Re-verified by symbol against the working tree before any dispatch:

- Three divergent regexes confirmed: `ciao/control_plane.py:42`,
  `ciao/os_audit.py:41` (`memory|user|profile`), `ciao/web/agent_assets.py:34`
  (`memory|user` only). `[rehome]` matches none. P1 premise holds.
- `ciao/proposal_kinds.py`, `ciao/operator_actions.py`, `ciao/workspace_census.py`,
  `ciao/workspace_reroot.py` all absent. Nothing started.
- `os_audit.py:1058` `actionable_count` sums defects with notices, as described. P2 premise holds.
- `memory_proposals.py:284` `existing if out_path.exists() else _STUB_HEADER` —
  write-once header bug confirmed. P1.3 premise holds.
- `vault_rehome.detect_misfiled_people` has no per-note exclusion; `EXCLUDED_TOP_DIRS`
  (`vault_index.py:55`) is directories only. `People/User.md` is movable. P5.9 defect confirmed.
- **OKF has landed** despite `OKF_ADOPTION_PLAN.md` still saying "proposed, not started".
  `ciao/okf.py`, `ciao/vault_migrate_links.py`, `web/src/lib/vaultLinks.ts` all exist and
  commit `734d38f9` shipped the markdown-link swap. The plan doc's status line is stale.
  Consequence: the "land OKF before P6" gate is already satisfied. P6 is not blocked.
- Tree clean on `develop`; `develop` is 490 commits ahead of `main`, `main` has nothing extra.
- Node v22.23.2 (above the 20.19 floor V4 warns about); `web/node_modules` present.

## Phase status

| Step | Owner | Status | Notes |
|---|---|---|---|
| P1 Proposal-kind registry | delegate | DONE | commit 83000847 |
| P2 Audit split (D2) | delegate | DONE | commit c121678b; pre-existing os-audit failure fixed too |
| P3 unrehomed_people notice | delegate | DONE | commit 4cc0510f |
| P4 Surfaced-actions strip | done | DONE | 481a31c9 + c907e7da + 96afccca; **P4.2 literal form DROPPED, see below** |
| P5 Queue review UI | split | DONE | server b77f78b4, UI c907e7da |
| P5.9 User.md must never move | delegate | DONE | commit 1ac96430 |
| P6 Vocabulary + agent_root | delegate | DONE | commit efa2b6d6 |
| P7 Provider seam | delegate | DONE | commit 06b94e6c |
| P8 Session paths | delegate | DONE | commit 001639e6 |
| P9 Per-root memory + MCP allowlist | delegate | DONE | P9.1+P9.2 a0d55751; P9.3 beaeda6f |
| P10 The cut | coordinator | IN PROGRESS | 970bd3d0, 59dda6b0, c70707f7, 2895f1bd, ee6f85e0, f9539770, b924e438, 0dda15a8; **P10.4's split is written and unwired, P10.9/P10.10 BLOCKED, see below** |
| V1 workspace-census | delegate | DONE | commit 796d84af |
| V2 Fixture assertions | coordinator | DONE | 14 tests, commit 970bd3d0 |
| V3 Real-data rehearsal | coordinator | DONE | APFS clone, 2318 files, zero refusals |
| V4 Full suites | coordinator | PARTIAL | python 2597 pass / 2 pre-existing fails; npm not a wave-1 gate (no web files touched) |
| V5 End-to-end drain | — | TODO | after P5 |

## Coordination decisions

- **No worktrees.** Waves are cut on file ownership instead, so every delegate works in
  the one checkout with a disjoint file set. Avoids merge resolution on a weak model's
  output and lets the coordinator read `git diff -- <owned paths>` per phase.
- **Delegates do not commit.** Coordinator reviews the diff, runs the suites, commits.
- **Archive a delegate chat once its work is committed.** Standing operator preference
  (2026-08-19). Archiving preserves the transcript under `memory-vault/Logs/Chats/` and
  triggers the normal post-archive processing, so nothing is lost and the sidebar stays
  readable. Keep only running delegates open.
- **Serialization points.** `ciao/os_audit.py` is touched by P1, P2, P3, P4 — those four
  never run together. `ciao/web/project_chats.py` is touched by P7 and P8 — same.
  `ciao/vault_rehome.py` is touched by P3 and P5.9 — same. `ciao/cli.py` is touched by
  V1, P2, P9, P10 — same.

## Wave 1 (dispatched 2026-08-19)

Four delegates, file sets disjoint:

- **P1** owns `ciao/proposal_kinds.py` (new), `ciao/control_plane.py`,
  `ciao/web/agent_assets.py`, `ciao/memory_proposals.py`, `ciao/os_audit.py`
  (the module-level regex and its one call site only), `tests/test_proposal_kinds.py`.
- **P5.9** owns `ciao/vault_rehome.py`, `tests/test_vault_rehome*.py`.
- **P6** owns `docs/ARCHITECTURE.md`, `ciao/config.py`, `tests/test_config_agent_root.py`.
- **V1** owns `ciao/workspace_census.py` (new), `tests/test_workspace_census.py`,
  `ciao/cli.py` (one new subcommand only).

## Log

- 2026-08-19 — Pre-flight verification done. OKF gate found already satisfied.
  Wave 1 dispatched (P1, P5.9, P6, V1).
- 2026-08-19 — P5.9 delegate finished. Coordinator verified: production fix in
  `vault_rehome.py` is correct (`EXCLUDED_PERSON_FILENAMES = {"user.md"}`, casefolded
  compare, placed after the People/-dir guard). Confirmed the three `User.md` references
  in `memory_proposals.py` at lines 13, 140, 323. Found and fixed one vacuous test: the
  "case insensitive" test built its fixture as `User.md`, the same casing as the previous
  test, so it exercised no casefolding at all. Rewritten to use `user.md` and `USER.md`.
  Proved the production casefolding works with an out-of-tree fixture before patching.
  `pytest -k rehome` = 43 passed.
- 2026-08-19 — Unrelated in-flight work found in the tree: permission-card keyboard
  shortcuts in `web/src/components/ChatPanel.vue`, `ChatLayout.vue`,
  `ciao/web/static/index.html` and a new `__tests__/ChatPanelPermission.test.ts`.
  Not written by any wave-1 delegate. `ChatLayout.vue` is owned by P4, so this must be
  committed or stashed before P4 is dispatched.
- 2026-08-19 — P6 delegate finished. `CiaoConfig.agent_root` verified correct: reuses
  `_clean_relative_path`, same `ValueError` type as `canonical_workspace_vault_root`,
  returns `workspace_root` today so behaviour is unchanged. 26 config tests pass.
  Coordinator rejected and rewrote three sentences in `docs/ARCHITECTURE.md`: the
  delegate wrote D5's *destination* (`<install>/Logs/`) in the present tense, so the doc
  claimed a location that does not exist on disk today. Verified against the tree:
  `memory-vault/Logs/` exists, `<install>/Logs/` does not. Doc now states today's layout
  and marks the promotion as a future release. My brief invited this error by quoting the
  destination without saying "future"; later briefs must separate current state from
  destination explicitly.
- 2026-08-19 — Coordination defect found: `tests/test_architecture_doc.py` requires every
  new `ciao/*.py` module to be indexed in the ARCHITECTURE.md "App repo layout" block, but
  P6 was given exclusive ownership of that doc. So P1's `proposal_kinds.py` and V1's
  `workspace_census.py` cannot satisfy it. Coordinator adds those two index lines once
  both land. Every future wave that creates a `ciao/` module needs the same follow-up, or
  the doc-index line must be assigned to the module's own delegate.
- 2026-08-19 — Divergence accepted, not reverted: `agent_root` additionally rejects a
  backslash (`a\\b`), which `canonical_workspace_vault_root` accepts because
  `Path("a\\b").parts` is one segment on POSIX. Rejecting is the safer side, but the two
  validators now disagree for the same concept. Follow-up: fold the backslash guard into
  the shared validator rather than leaving it in one caller.
- 2026-08-19 — V1 delegate finished. Verified: `survey_vault` / `format_census` /
  `as_dict`, no write, move, rename or git call anywhere in the module, `os.walk` with
  `followlinks=False`, symlinks reported via `readlink` and not traversed. CLI wired as two
  purely additive hunks (41 insertions, 0 deletions), neither inside `_os_audit_command`.
  10 census tests pass. Coordinator additionally proved read-only against the REAL 2289-file
  vault by hashing every path plus mtime plus size before and after a live run: identical.
- 2026-08-19 — V1 census output on the reference vault (this is the fixture spec):
  2020 notes, 269 non-markdown, max depth 7, 0 symlinks, 16 duplicate stems,
  24 frontmatter-less notes, 3 notes loose at the vault root.
  Per top-level: Logs 1430, work 426, personal 159, Templates 5, .obsidian 0, root 3.
  Registered workspaces: personal, work.
  **Unregistered top-level directories: .obsidian, Logs, Templates.**
  Plan predicted ~1985 notes / ~1423 under Logs; the vault has grown since, and the ratio
  holds (Logs is 71%). Every one of these shapes now needs a P10 fixture counterpart.
  New shapes the plan did not name: 3 loose root notes (INDEX.md, MEMORY.md, VOCABULARY.md),
  269 non-markdown files with 251 of them under work/, and duplicate stems that collide
  ACROSS workspaces (personal and work both hold INDEX.md, MEMORY.md, Memory-Proposals.md,
  and 15 README.md). D5 covers Logs/ and Templates/; nothing yet covers .obsidian/ or the
  3 root notes.
- 2026-08-19 — V1 scope note: the delegate also added a dispatch test to
  `tests/test_cli.py`, which was outside its granted file list. Additive, mirrors the
  neighbouring `vault-lint` test, no other wave-1 delegate owns that file. Accepted.
- 2026-08-19 — Open failure, attribution pending:
  `tests/test_cli.py::test_cli_os_audit_passes_the_workspace_registry_to_upgrade_notices`
  expects exit 1 and gets 0. NOT caused by V1 (its cli.py hunks are additive and outside
  `_os_audit_command`). P1 is still mid-flight with `ciao/os_audit.py` modified, so this is
  re-checked once P1 lands.
- 2026-08-19 — P1 delegate finished. `ciao/proposal_kinds.py` verified: `KINDS` table of
  four kinds, alternation derived from the table with `re.escape`, `parse_bullet`,
  `UnknownKindError` on an unregistered kind, and two structurally distinct accept
  descriptors so `RehomeAccept` can never be routed through the region-edit path. The
  `"user" -> region "profile"` mapping matches `memory_tool._REGION_ALIASES` line 61, so
  legacy behaviour is preserved. All three former regex copies now import one definition.
  12 tests pass.
- 2026-08-19 — Coordinator fixed a data-corruption bug in the delegate's `_refresh_header`.
  On a Memory-Proposals.md whose frontmatter opens and never closes, `content_start`
  stayed 0, the boundary scan started at line 0, and an indented YAML list item
  (`  - memory`) was matched as a proposal bullet. The header was then spliced into the
  middle of the frontmatter, destroying the file. Reproduced directly before patching. The
  docstring asserted this could not happen. Now an unclosed frontmatter returns
  byte-identical. Verified: bullets preserved, batch headings preserved, stale
  `~/.ciao/memory.md` wording gone.
- 2026-08-19 — Coordinator promoted `_PROPOSAL_BULLET_RE` to public `BULLET_RE`. The
  delegate had three modules importing a private underscore name from the registry, one of
  them via `import X as X`, while the public `parse_bullet` it wrote went unused. Sharing
  the definition through a private symbol is how the three copies drift apart again. My own
  first pass at this rename broke collection in 12 test modules because I renamed the
  registry without updating its importers; caught by the full suite, fixed, re-run.
- 2026-08-19 — **Baseline established.** Clean `develop` at HEAD, measured in a throwaway
  worktree: 2563 passed, **2 failed**. Both failures pre-date every delegate:
  `test_cli.py::test_cli_os_audit_passes_the_workspace_registry_to_upgrade_notices`
  (os-audit exits 0 where the test expects 1) and
  `test_package_version.py::test_package_status_reports_available_update`.
  Working tree after wave 1: 2597 passed, the SAME 2 failed. No regressions, +34 tests.
  The os-audit one is P3/P4 territory (`audit_upgrade_notices` and the workspace registry),
  so it goes into P3's brief as a fix-first item rather than being carried as noise.
- 2026-08-19 — `npm test` is not a wave-1 gate: no wave-1 delegate touched a web file. The
  only `web/` changes in the tree are the operator's own permission-shortcut work.

## Wave 1 outcome

All four steps land clean. Not committed, pending operator approval.
Follow-ups queued, none blocking:
1. Fold the backslash guard into the shared name validator so `agent_root` and
   `canonical_workspace_vault_root` stop disagreeing.
2. Fix the 2 pre-existing test failures; assign the os-audit one to P3.
3. Assign the ARCHITECTURE.md module-index line to each delegate that adds a `ciao/` module,
   or the doc test fails for reasons unrelated to that delegate's work.
4. Decide destinations for `.obsidian/` and the 3 loose root notes, which D5 does not cover.
5. Migrate `parse_bullet` into the call sites; they still use raw `match.group(1..3)`, so the
   typed `ProposalBullet` API is written but unused.

## Wave 2 (dispatched 2026-08-19)

Wave 1 committed as `efa2b6d6`, `83000847`, `1ac96430`, `796d84af` plus the ledger.
Three delegates, file sets disjoint:

- **P2** owns `ciao/os_audit.py`, `ciao/cli.py`, `ciao/web/agent_assets.py`,
  `tests/test_os_audit.py`, `tests/test_cli.py`. Also assigned the pre-existing
  os-audit exit-code failure, since it sits inside the code P2 rewrites.
- **P7** owns `ciao/provider_service.py`, `ciao/web/project_chats.py`, its tests.
- **P5-server** owns `ciao/web/routes_api.py`, `tests/test_web_proposals.py`,
  `docs/PWA_API.md`.

P5 was SPLIT. The server half (endpoints, batch ops, skill-proposal surface, the D7
leak flag) runs now; the UI half waits for the response shape to exist. P3 is still
blocked behind P2 on `os_audit.py`. P8 is blocked behind P7 on `project_chats.py`.

- 2026-08-19 — Verified before briefing P2: **no frontend file consumes
  `/api/agent-assets/audit`** (`grep -rn "agent-assets/audit" web/src/` is empty). The work
  order's P2.3 claims "the Settings audit view changes with it". There is no such view. P2's
  brief says so explicitly and forbids inventing one. The proposal COUNT reaches Settings by
  a different path: `/api/agent-assets` -> `_memory_proposal_assets` ->
  `_count_proposal_bullets`, which P1 already fixed to include `[rehome]` and `[profile]`.
- 2026-08-19 — Briefs now carry the wave-1 lessons: use `.venv/bin/python`, the two
  pre-existing failures and which delegate owns them, hands off the operator's `web/` work,
  never state a future layout in the present tense, and add the ARCHITECTURE.md module-index
  line when creating a `ciao/` module.
- 2026-08-19 — **Wave 2 lost to stalled delegates.** All three (P2 chat-e9dc55ae, P7
  chat-0ab3eaa0, P5-server chat-435c3120) sat at `running: true` for 4.5 hours with
  `last_activity_at` still equal to `created_at`, no `.runtime/transcripts/<chat>/` directory
  (every wave-1 chat has one), no opencode session storage on disk, and zero file writes.
  Two `opencode serve` processes were alive but did not answer `/session` within 5s.
  Ruled out: a server restart (the desktop app has been up since 15:17, before the 16:15
  dispatch) and concurrency (wave 1 ran 4 delegates fine). Stopped all three via `chat_stop`
  and re-dispatched P2 and P7. Verified first that HEAD matched the wave-1 commits, so nothing
  half-written was left behind.
- 2026-08-19 — The wave-1 P1 chat reported FAILED long after its work was committed. Read its
  transcript: a new session at 18:48 ran `vault_search("bypass mode")` and `delegates_list`,
  then `"response": "Aborted"`. Someone typed into the delegate chat rather than the delegate
  resuming itself. Confirmed no repo impact: `git diff HEAD` over all six P1 files is empty.
  Not a code failure.
- 2026-08-19 — Holding P5-server until P2 or P7 shows real activity, rather than re-sending a
  third large brief into a provider that just ate three of them.
- 2026-08-19 — **CORRECTION to the stall diagnosis above. It was wrong.** Ciaobot timestamps
  are UTC (`...Z`); `date` and `ps` on this machine are CEST (UTC+2). I read the two as the
  same clock. Consequences:
  * Wave 2 had been running 2h36m, not 4.5 hours. Wave 1's slowest delegate took 1h48m, so
    wave 2 was about 45 minutes past precedent, not four hours past it.
  * "No `.runtime/transcripts/<chat>/` directory" is NOT evidence a delegate is dead. The
    wave-2b retries are alive with a matching `opencode serve` process each and have no
    transcript directory either. A transcript appears when output is written, not at start.
  * The "server has been up since 15:17, before the 16:15 dispatch" inference was also wrong:
    15:17 local is 13:17Z, so the server started BEFORE wave 1, not between the waves.
  So the kill criterion was unsound and I probably stopped three slow-but-live delegates.
  Cost: three re-sent briefs and roughly 2.5 hours.
  Correct signals for "dead" in future: an `opencode serve` process with no matching pid, an
  aborted run recorded in `.runtime/transcripts/<chat>/opencode.json`, or an error in
  `.runtime/server_errors.log`. Compare UTC to UTC before concluding anything about elapsed time.
- 2026-08-19 — Wave 2b status: `chat-038347fd` (P2) and `chat-0a09a22d` (P7) created 18:53Z,
  each with a live `opencode serve` started at the matching local time. Running normally.
  The `server_errors.log` holds nothing relevant: one 09:02Z schedule-classifier JSON error and
  five `ClientDisconnect` traces from the MCP HTTP endpoint, all unrelated to delegate startup.
- 2026-08-19 — P7 verified and committed as `06b94e6c`. `_ensure_provider` takes an optional
  keyword-only `agent_root`; `_agent_root_for_chat` resolves the chat's project's workspace and
  falls back to `primary_workspace()` for a project with no workspace, a project missing from the
  manager's map, and a chat with no project. Confirmed `_is_known_workspace`
  (project_chats.py:1697) and `primary_workspace` (config.py:456) both already existed, so no
  new helper was invented.
  Verified the no-behaviour-change claim properly: applied ONLY P7's diff in a throwaway worktree
  at HEAD, so the other delegate's in-flight `os_audit.py` edits could not contaminate the
  measurement. Result 2601 passed / 2 pre-existing failed, no regressions.
  Mutation-checked the new tests: pointing the resolver at a wrong path fails 3 of 4, and the 4th
  is the one that deliberately bypasses the resolver. The tests have teeth.
  Coordinator fixed one test-isolation hazard: the `_CAPTURED` spy dict was module-level and never
  cleared, so an assertion could read the PREVIOUS test's root and pass even when the factory was
  never called. Proved it by deleting the triggering call and watching the test go from pass to
  fail after adding `_CAPTURED.clear()`.
- 2026-08-19 — P5-server re-dispatched (`chat-35f8da7d`) now that P7 proved the delegate path
  works. P8 is blocked behind it: both need `ciao/web/routes_api.py`. P3 is still blocked behind P2
  on `ciao/os_audit.py`.
- 2026-08-19 — P2 verified and committed as `c121678b`. The D2 split itself is right:
  `defect_count` / `pending_action_count` / `has_pending_actions`, status keyed off defects only,
  Upgrade Actions rendered with an info icon and "Suggested" rather than a warning and "Fix".
  The delegate correctly found that NO cli.py exit-code change was needed: with status keying off
  defects, a pending-only audit is `healthy` and the existing mapping already returns 0. It also
  correctly left `ciao/web/agent_assets.py` alone, since the endpoint returns the report dict and
  the added keys flow through by themselves. `actionable_count` is gone from the report and had
  no remaining consumer in `ciao/`, `tests/` or `web/src/`.
- 2026-08-19 — **The delegate reported "done" with its own two new tests failing, and it
  misdiagnosed the pre-existing failure.** It chose "the test encodes the old behaviour" and
  rewrote the expectation to exit 0, but then also asserted `pending_action_count == 1`, which
  cannot hold while no notice is produced. Its tests contradicted themselves.
  The real cause, found by instrumenting `_os_audit_command`: it consulted the ambient environment
  for the runtime and vault roots even when `--workspace` was passed explicitly. An absolute
  `CIAO_RUNTIME_ROOT` therefore escaped the named directory, so the audit read a DIFFERENT
  install's registry, job runs and migration receipts while still reporting on the named vault.
  Proved it by toggling one env var: with `CIAO_RUNTIME_ROOT` set, `notices_found=0` and the
  runtime root is the real install; with it removed, `notices_found=1` and the runtime root is the
  temp workspace. A running Ciaobot chat exports `CIAO_RUNTIME_ROOT`, so auditing any workspace
  from inside a chat silently reported on the wrong one. That is why this test failed here and
  presumably passes in CI: an environment-dependent test, which is the worst kind.
  Fixed so an explicit `--workspace` wins over the environment, added a regression test with a
  foreign install's runtime root, and mutation-checked it by reintroducing the leak.
  Suite went from 2 pre-existing failures to 1. Only `test_package_version` remains.
- 2026-08-19 — Process note: three test failures I briefly reported to myself as real were stale
  `__pycache__` from my own mutation testing. `cp` restoring a file can leave bytecode that looks
  current. Clear `__pycache__` after any mutate-and-restore check before trusting the result.
- 2026-08-19 — P3 dispatched (`chat-23268601`), unblocked now that P2 released `os_audit.py`.
  Its brief carries the D4 receipt bug, the ban on scanning the vault from the detection path, and
  an explicit instruction not to hardcode the stale 87/15 and 42/53 counts the incoming briefs
  quoted. P8 is still blocked behind P5-server on `routes_api.py`.
- 2026-08-19 — P5 server half verified and committed as `b77f78b4`. Response schema is genuinely
  good: content-derived ids with a duplicate counter, `rehome: {destination, candidates, justified,
  reason}` where `justified` is only true for the `mechanical` bucket, and `leak_warning` on region
  rows sourced outside the primary workspace. That shape supports all three real cases without a UI
  re-reading the vault. Skill-proposal files are included without being disguised as bullets.
  Isolated verification in a worktree at HEAD: 2621 passed, 1 pre-existing failure.
- 2026-08-19 — **The delegate's work was complete, documented, and completely unreachable.** No
  route in `ciao/web/app.py` served any of it, and `tests/test_web_proposals.py` mounted the
  handlers on a hand-built Starlette app, so 15 tests passed while production had no endpoint.
  This is my fault: my brief restricted the delegate to three files and did not grant `app.py`,
  which registration requires. The delegate flagged the conflict and correctly obeyed the
  restriction instead of reaching outside it.
  Coordinator registered the four routes and added
  `test_the_real_app_serves_every_documented_proposal_route`, which asserts the real `app.py`
  route table. Mutation-checked by un-registering one route: the test fails, as it must.
  Registering them then made `tests/test_pwa_api_docs.py` fail, because the templated path
  `/api/proposals/{id}/{action}` was not in PWA_API.md. So the doc test WOULD have caught the
  missing registration all along; it was silent only because there was no route to check.
  Lesson for every remaining brief: **any phase that adds an HTTP route must own `app.py`**, and
  a route test that builds its own app proves nothing about reachability.
- 2026-08-19 — Brief error corrected by the delegate: `PWA_API.md` is at the repo root, not
  `docs/PWA_API.md` as my brief said. It found the real path.
- 2026-08-19 — Follow-up, not blocking: `_rehome_signal` calls
  `vault_rehome.detect_misfiled_people`, which walks every person note on every
  `GET /api/proposals`. Acceptable for a user-initiated list, but P4's `review-queue-depth`
  detector must NOT reach this endpoint for a count, or the cheap-detection rule (P4.3) is broken
  through the back door. Once P3's survey receipt exists, this should read the receipt instead.
  P4's brief must say so.
- 2026-08-19 — P3 verified and committed as `4cc0510f`. Aborted mid-verification after 125 tool
  events (transcript `response: "Aborted"`), but the work was substantially complete and its 82
  tests passed. Design is strong: `survey_vault_people` reuses `plan_rehome` with nothing applied,
  `read_receipt` now gates on `status == "migrated"`, and the delegate added `peek_receipt` for the
  detection side on its own initiative, correctly reasoning that `read_receipt` returning None for a
  survey would hide the very damage the notice exists to surface. The survey also refuses to
  overwrite a migrated receipt, protecting the reverse map.
- 2026-08-19 — Coordinator found and fixed a false positive: the notice fired on EVERY install that
  had never surveyed, including a fresh single-workspace one with an empty vault. Reproduced it
  directly. Investigated rather than assuming: `detect_misfiled_people` DOES return a candidate with
  one registered workspace, but with `target_workspace=''` and `destination=''`, so there is nowhere
  to move the note and the tile offers no action. My first comment claimed no candidate is produced
  at all, which is wrong; corrected to state the empty-destination reason. Gated on
  `len(names) > 1`, mutation-checked.
  Knock-on: P3 had raised two of P2's count expectations to absorb its always-firing notice, so both
  were corrected back down, and the notice's own tests now register two workspaces, which is the
  shape the damage actually needs. Full suite 2631 passed / 1 pre-existing failure.
- 2026-08-19 — `vault_migration` and `vault_migrate_links` checked for the same receipt defect and
  DELIBERATELY LEFT ALONE. Neither writes a `status` field, so presence is equivalent to migrated for
  them, and D4's literal instruction (gate on `status == "migrated"`) would make every existing
  receipt read as unfinished and re-run a completed migration. That is a destructive change with no
  live bug to justify it. When P10 gives either module a survey mode, the gate must be added as
  "absent status counts as migrated" so legacy receipts keep working.
- 2026-08-19 — Wave 4 dispatched: P8 (`chat-5e5fee60`) and P9 (`chat-1c687242`), file-disjoint.
  P9's brief flags that its MCP change must fail CLOSED on a malformed `.mcp.json`, must compose with
  `_DEFAULT_HARNESS_DISALLOWED_TOOLS` rather than replace it, and must report over-cap per guide
  rather than as one global number.
- 2026-08-19 — **P4 is blocked on the operator, not on a delegate.** It owns
  `web/src/components/ChatLayout.vue`, which carries uncommitted permission-shortcut work. It also
  needs `os_audit.py` (held by P9) and `routes_api.py` (held by P8). Its brief must additionally
  forbid the `review-queue-depth` detector from calling `GET /api/proposals`, because that endpoint
  walks the vault via `_rehome_signal` and would break the cheap-detection rule through the back door.
- 2026-08-19 — P8 verified and committed as `001639e6`. Two problems, both fixed by the coordinator.
  **It added the parameter but left the important callers passing nothing.** Only
  `routes_api.py:3573` passed `agent_root`. The two `find_parent_session_file` calls in
  `project_chats.py` (6871, and 7909 which `_await_schedule_subagents` polls) and all three
  `_claude_session_exists` calls still used the global `workspace_root`, so the readers the work
  order specifically names were untouched in practice. Wired four of them through
  `_agent_root_for_chat`, which P7 had already added. The fifth has only a transcript row and no
  chat, so there is no workspace to resolve from; commented rather than left looking overlooked.
  Updating the signature broke 7 monkeypatched stubs in `test_chat_subagents.py` and
  `test_schedule_subagent_wait.py`, which took the old positional signature; updated.
  **It also introduced the failure the phase exists to prevent.** Where `agent_root` was supplied,
  both functions returned early instead of falling back to the `~/.claude/projects` glob scan, to
  isolate roots from each other. That glob is the ONLY way to find a session recorded under a
  different cwd slug, which is precisely the amnesia this phase guards against, and the early
  return removed it NOW, while `agent_root` still resolves to the same directory for every
  workspace. Reproduced directly: with `agent_root` the lookup returned None for a session the
  fallback finds. Net restored for every caller; isolation deferred to P10.
  Its own two tests asserted that cross-root invisibility, so they encoded the premature isolation.
  Rewritten to assert the slug differs per root and to document why the net stays until roots
  actually differ. Also fixed a shadowed `root` local in `_claude_session_exists`.
  Suite: 2548 passed / 1 pre-existing failure (P9's two files excluded, another delegate holds them).
- 2026-08-19 — **Bypass mode: now settled empirically, not by inference.** Passed
  `mode: "bypass"` explicitly to `delegate_spawn` for P4. The created chat came back
  `"mode":"auto"`. So the `_child_mode` clamp applies to `delegate_spawn` as well as
  `chat_update`, and a coordinating chat in `auto` cannot grant bypass by any tool argument.
  The only lever is the operator setting the COORDINATING chat to Bypass; children then inherit it.
  This is the third time it has come up, so it is now recorded with direct evidence.
- 2026-08-19 — P4 dispatched (`chat-4710c68d`) with two deliberate deferrals, both file-contention:
  P4.2 (rebuilding `audit_upgrade_notices` from the registry) waits for P9 to release
  `ciao/os_audit.py`; P4.7 (mounting the strip in `ChatLayout.vue`) waits for the operator's
  uncommitted permission-shortcut work. The brief tells the delegate to design for both as small
  follow-ups and to report the exact insert points.
  Its brief also carries the two lessons this release paid for: a route not registered in `app.py`
  does not exist, and a new `ciao/*.py` module must be indexed in ARCHITECTURE.md. It owns both
  files this time.
- 2026-08-19 — `web/src/components/ChatLayout.vue` is still UNCOMMITTED (7 permission-shortcut lines
  live in the working tree). Commit `7c7e21f9` touches that file but is an earlier, unrelated change
  ("fix(home): show loading feedback...", 16:42) already in HEAD. P4.7 stays blocked.
- 2026-08-19 — Archived the five finished delegates whose work is committed: P2-retry
  (`chat-038347fd`), P7-retry (`chat-0a09a22d`), P5-server (`chat-35f8da7d`), P3
  (`chat-23268601`), P8 (`chat-5e5fee60`). Twelve of fourteen delegate chats are now archived;
  only P9 (`chat-1c687242`) and P4 (`chat-4710c68d`) remain open, both running. Nothing was needed
  from the archived ones: their diffs are committed and the deferred P4.2 / P4.7 items are
  coordinator work, not theirs.
- 2026-08-20 — P9.1 and P9.2 verified and committed as `a0d55751`. P9.1 is a clean four-line
  change: `memory_status` / `memory_update` resolve the guide through `config.agent_root(workspace)`
  instead of discarding the workspace. P9.2 reports one entry per registered guide with over-cap
  attributable per workspace, and the delegate correctly verified that NOTHING in `web/src` reads
  `memory_hygiene`, so it invented no frontend change.
- 2026-08-20 — **P9.3 HELD, not committed.** Stashed (`stash@{0}`) with its test parked at
  `docs/plans/held/test_mcp_allowlist.py.p9_3`. The implementation follows P9.3's wording literally
  and is still not shippable. Measured on the live install:
      personal  mcp denials -> ['mcp__n8n_mcp', 'mcp__notion']
      work      mcp denials -> ['mcp__notion']
  Four problems:
  1. **It silently removes two working integrations.** `.mcp.json` declares `n8n_mcp` and `notion`.
     `personal` has `disallowed_tools: None`, which the change reads as "opted into nothing", so both
     servers become unreachable. `work` survives only by the accident of having an explicit deny list.
  2. **There is no way to opt in.** `disallowed_tools` is a DENY list, and the change infers opt-in
     from "explicitly listed and not named". So a workspace enables a server by writing a deny list
     for a different one. That is not a usable model, and a NEW workspace can never reach any server.
  3. **The fail-closed path rests on an unverified wildcard.** A malformed `.mcp.json` appends
     `mcp__*`, which is passed straight to the Claude SDK's `disallowed_tools`. Nothing in this repo
     expands that pattern. If the SDK matches exact names, the malformed case fails OPEN while the
     docstring claims it fails closed.
  4. **It only applies to Claude chats.** `project_chats.py:4667` returns `[]` for any provider that
     is not `claude`, so the "credentialed authority" leak stays wide open for codex and opencode
     chats. The docstring claims a posture the code cannot deliver, and every delegate in this
     release runs on opencode.
  A correct version needs an explicit allow field rather than an inverted deny list, plus a migration
  that seeds each existing workspace from its current effective set so upgrading loses nothing, and
  it must state the Claude-only limit honestly. That is a design decision with a real blast radius,
  so it goes to the operator rather than being improvised.
- 2026-08-20 — **Bypass now works.** The operator set this coordinating chat to Bypass, so
  `delegate_spawn` with `mode: "bypass"` produced a bypass child (`chat-68ec1e70`), and
  `chat_update` successfully moved the running P4 chat from `auto` to `bypass`. Confirms the
  clamp reads the parent at call time: nothing needed changing on my side, only the parent's mode.
- 2026-08-20 — P9.3 re-dispatched (`chat-68ec1e70`) with a brief that SUPERSEDES the work order's
  own wording for this step, because that wording taken literally produced the four defects logged
  above. Design decided by the coordinator rather than left to the delegate:
  1. An explicit `allowed_mcp_servers: list[str] | None` on `WorkspaceConfig`, instead of inferring
     opt-in from an inverted deny list. `None` denies everything, which is fail-closed for anything
     created after the migration.
  2. A migration seeding each EXISTING workspace from what it can reach today, so upgrading loses no
     capability: `personal -> ["n8n_mcp", "notion"]`, `work -> ["n8n_mcp"]`. This is legitimate
     auto-apply under D1: the registry is Ciaobot-generated metadata and there is exactly one
     correct outcome per workspace.
  3. Explicit server names only, never a glob, because nothing in this repo expands `mcp__*` and the
     rejected version's fail-closed path depended on the SDK honouring one.
  4. Malformed `.mcp.json` denies every server named in any workspace's allowlist, by name, with the
     residual limit stated honestly: a server only present in the corrupt file cannot be denied by
     name because nothing knows it exists.
  5. Both overclaiming docstrings corrected to say that this scopes reachability rather than
     authority, and that `disallowed_tools` is applied only for `claude` chats, so codex and opencode
     are unconstrained. Fixing that gap needs a per-provider mechanism and is an explicit follow-up,
     not part of this step.
  The brief also requires the delegate to back up and restore `.runtime/workspaces.json` around its
  real-install check, so the coordinator decides when the actual migration runs.
- 2026-08-20 — P9.3 verified and committed as `beaeda6f`. The redesign is correct. Reproduced the
  seeding in a sandbox copy: `personal -> ['n8n_mcp', 'notion']`, `work -> ['n8n_mcp']`, denials
  `personal []` and `work ['mcp__notion']`, exactly the spec. Independently checked the three things
  the first attempt got wrong: a malformed `.mcp.json` denies by explicit name with NO glob, the
  migration is idempotent across two loads, and an unknown registry key survives it. Both
  overclaiming docstrings now state the reachability-not-authority limit and the claude-only guard.
  Suite 2661 passed / 1 pre-existing failure. Rejected attempt dropped from the stash.
- 2026-08-20 — **The delegate damaged the live registry and its restore step did not work.**
  `/Users/raffaelefarinaro/repos/ciao/.runtime/workspaces.json` was left with
  `allowed_mcp_servers: []` for BOTH workspaces, which denies every MCP server: the exact regression
  the redesign existed to prevent, applied to the operator's real install. No backup file was left
  behind, and `.runtime/` is gitignored so there was nothing to restore from.
  Diagnosed rather than assumed: the committed code seeds correctly in a sandbox, so the `[]` came
  from an earlier in-development run of the delegate's own code that persisted to the live registry
  before the logic was finished. Repaired by hand, atomically, preserving `claude_ai_mcps` and every
  other key: `personal -> ['n8n_mcp', 'notion']`, `work -> ['n8n_mcp']`. Verified against the live
  config afterwards.
  Lesson for every remaining brief, and it matters most for P10: **a delegate must never point code
  that writes state at the live install.** "Back up and restore" is not a sufficient instruction. Give
  it a copied sandbox root and forbid the real `.runtime/` outright. P10's migration engine writes
  far more than one registry field.
- 2026-08-20 — Follow-up recorded, not blocking: `disallowed_tools` is applied only for `claude`
  chats (`project_chats.py` guard), so the MCP allowlist does not constrain codex or opencode.
  Every delegate in this release runs on opencode. Closing it needs a per-provider mechanism.
- 2026-08-20 — P4 verified and committed as `481a31c9`. Seven detectors, stable ids, a broken
  detector logged and skipped, D6 implemented (once schedules no longer catch up, one collapsed tile,
  both run and dismiss converging on `last_triggered_on`). Routes registered in `app.py`, so the
  unreachable-routes lesson stuck. It left `ChatLayout.vue`, `ChatPanel.vue` and `static/index.html`
  untouched as instructed. Suites: python 2681 passed / 1 pre-existing failure; web 699 passed
  across 68 files on Node 22.
- 2026-08-20 — **Best catch of the session.** `_review_queue_depth` called `len()` on the generator
  `Path.glob` returns, which raises `TypeError`. `detect_actions` catches every exception a detector
  throws, so the review-queue tile silently disappeared, and ONLY on an install that actually has a
  `Workspace/Skill-Proposals/` folder, which is the sole case where it matters. The reference vault
  has 49 files there. So the tile meant to surface the 417-item backlog, the central problem of this
  whole work order, would never have rendered on the operator's own machine.
  It passed review because the existing depth test wrote only `Memory-Proposals.md`, so the
  `skill_dir.is_dir()` guard skipped the buggy line entirely. Proved the `TypeError` against the real
  folder, fixed with `sum(1 for _ in ...)`, added a test that creates the folder, and
  mutation-checked it by reintroducing the bug.
- 2026-08-20 — **My own commit error, caught and fixed (`aa596fe4`).** `beaeda6f` (P9.3) was
  committed as a path-scoped subset of a green working tree, but two of the excluded files
  (`tests/test_workspace_settings_routes.py`, `tests/test_local_routes.py`) were P9.3's own work,
  updating the payload assertions that `workspace_to_dict`'s new `allowed_mcp_servers` key changed.
  I attributed them to P4 because both delegates were editing the tree at once. Verified HEAD was
  genuinely broken by running it in a detached worktree: 3 failed. Rule: committing a subset of a
  green tree is only safe when the excluded files are provably unrelated, which needs checking the
  diff content, not just the filename.
- 2026-08-20 — Operator committed the permission-shortcut work as `4542b56d`, unblocking
  `ChatLayout.vue`. Verified against the code before dispatching downstream work rather than
  trusting the report: `HomeRecentChats` mounts at exactly lines 84 and 177, each inside a
  `--home-max` container, confirming the work order's "ChatLayout builds the home screen twice"
  claim and giving the exact P4.7 insert points.
- 2026-08-20 — Wave 7 dispatched, both in bypass, both file-disjoint:
  P4.2 (`chat-893eb5df`) rebuilds `audit_upgrade_notices` from `operator_actions.detect_actions`
  so the CLI and the strip can never drift, mapping only the two kinds that already have a notice
  today (vault-location, unrehomed-people) plus `unmigrated_vault_links` which predates P4; explicitly
  told to leave package-update, missed-schedules and review-queue-depth OUT of the CLI audit, since
  adding them there is scope creep this step does not call for.
  P4.7 + P5 UI (`chat-7ffb29f7`) mounts `HousekeepingStrip` at both confirmed locations and builds the
  proposal review panel, briefed hard on the three real cases (no-signal question UI, dual-candidate
  picker, the D7 leak warning) and on batch operations being required rather than optional, per the
  measured one-item-per-session drain rate.
- 2026-08-20 — P4.7 + P5 UI verified and committed as `c907e7da`. Strip mounts at both home
  branches (84 and 179), mutation-checked by removing the second one: the new ChatLayout test fails,
  so it genuinely covers both. Panel handles all three real cases: picker for multiple candidates,
  question rather than pre-filled accept when no tag justifies the destination, explicit confirm on a
  leak-warning region row. Skill rows are dismiss-only and excluded from batch accept, with the right
  reason given (`accept_for('skill')` has no descriptor and would raise). Batch select-all, batch
  accept/dismiss and dismiss-older-than are all present, which was the non-negotiable part. Reached
  at `/proposals` following the existing settings/schedules/memory view-mode pattern rather than a new
  paradigm. Web suite 709 passed across 69 files.
  Coordinator fixed one cosmetic regression: inserting the strip at line 84 had shifted
  `HomeRecentChats` two spaces left.
- 2026-08-20 — **My brief was wrong and the delegate was right.** I instructed it to consume the POST
  response instead of refetching, claiming the batch endpoints return the updated list. They do not:
  `dismiss_older_than` returns `{ok, removed}` and `proposals_batch` returns `{ok, action, results}`.
  I had conflated them with P4.5's housekeeping POST, which genuinely does re-run detection and return
  its new list. Verified by reading the handlers and confirming the only `"rows"` occurrences in that
  range are an internal grouping dict. The refetch is correct against the real contract, so nothing
  was changed.
  Real follow-up this exposed: `GET /api/proposals` builds its rehome signal by walking every person
  note, so every accept or dismiss now costs a vault walk, and draining 353 items would cost 353 of
  them. The fix belongs on the server, returning the updated rows the way housekeeping already does.
  Filed, not blocking.
- 2026-08-20 — **P4.2 reported "done" having written nothing.** Tree was completely clean. Read the
  transcript: 76 tool events, counted as 38 `bash`, 36 `read`, 2 `grep`, and ZERO edits. The response
  ends mid-sentence on "before editing", with the same "let me check one more thing" phrasing
  repeating a dozen times. A pure analysis loop.
  Diagnosed the cause rather than blaming the model: **my file set made the task impossible.** I gave
  it `ciao/os_audit.py` and `tests/test_os_audit.py`. But FIVE test files pin this behaviour, and
  `tests/test_workspace_vault_root.py` asserts exact substrings inside the vault-location notice's
  `remedy` (lines 245-247, 279-282, 308-310). Any adapter regenerating that text from `OperatorAction`
  breaks three test files it was forbidden to touch. It correctly sensed the job could not be
  completed inside its grant and stalled instead of breaking things.
  This is the third instance of the same coordinator error, after P5's missing `app.py` and P4's
  missing `ChatLayout.vue`: **scope a phase by what it must CHANGE, not by what it must not break.**
  Before writing any further brief, grep for every test that pins the behaviour and grant those files.
- 2026-08-20 — P4.2 re-dispatched (`chat-0cfeb9e6`) with all five test files granted, the
  circular-import question pre-answered as settled fact (`operator_actions` does not import
  `os_audit`, verified directly), the remedy-text constraint stated up front, and an explicit working
  instruction to stop reading and start writing after roughly ten calls. Mapping restricted to the
  three kinds that already have a notice: vault-location, unrehomed-people, unmigrated-links.
- 2026-08-20 — P4.2 retry ALSO stalled: 20 tool events, all `read`/`grep`, zero edits, turn ended
  cleanly rather than looping. Different cause from the first stall. The brief I wrote to fix attempt
  one demanded 5097 lines across 7 files (`os_audit` 1463, `test_cli` 1129, `test_os_audit` 1036,
  `operator_actions` 635, `test_operator_actions` 406, `test_workspace_vault_root` 358,
  `test_link_migration_triggers` 70). `deepseek-v4-flash` cannot hold that and still act. So the two
  failures were opposite errors of mine: too few files made the task impossible, enough files made it
  exceed the model's context. This step is not delegatable at this scope to this model.
- 2026-08-20 — **P4.2's literal instruction is the wrong shape, and dropping it is the right call.**
  Took it over directly and compared the three duplicated pairs by hand. The finding:
  * `vault-location`: the notice's `remedy` and the action's `chat_prompt` are DIFFERENT prose, and
    `test_workspace_vault_root.py` pins `"Open a Ciaobot chat"` which appears only in the notice. A
    faithful adapter would have to reconstruct CLI prose from action fields, so the phrasing stays in
    two places anyway and the "single source" is illusory.
  * `unmigrated-links`: the two predicates ALREADY DISAGREE, and for good reasons.
    `audit_upgrade_notices` requires a receipt to be absent AND `has_unmigrated_links` to return a
    real example, so it walks the vault and is accurate. The detector requires
    `vault_mode == "existing"` AND no receipt, and deliberately skips the walk because P4.3 forbids
    one on a path that runs every app open and window focus.
  Unifying them is actively harmful in either direction: adopting the accurate predicate puts a vault
  walk on the focus path, adopting the cheap one makes the CLI report a migration that is not needed.
  The drift worth removing is in the shared *condition*, but here the conditions differ by design in
  cost. So the duplication stays, documented, and P4.2's "render the notices from the registry" is
  recorded as rejected with this reasoning rather than half-implemented.
- 2026-08-20 — What the investigation DID find, and it was worth more than the refactor: a live false
  positive shipped in P4 (`481a31c9`), fixed in `96afccca`. On an adopted vault written in markdown
  links from the start with no receipt, the cheap predicate is satisfied while nothing needs
  converting, yet the tile asserted "The vault still uses the retired wikilink dialect" and "An
  adopted vault still contains `[[wikilinks]]`". Reproduced directly: `has_unmigrated_links` returned
  empty and the tile fired anyway. Both strings state a fact the detector never established. Reworded
  to "may still use", matching what its own `chat_prompt` already said, with a regression test.
  This is the third overclaiming-detector bug in this release, after the unrehomed-people
  single-workspace tile and the two MCP-allowlist docstrings. Pattern worth naming: a cheap detector
  must word its tile to what its predicate actually proves.
- 2026-08-20 — Suites after the fix: python 2682 passed / 1 pre-existing failure; web 709 passed
  across 69 files. **Every phase except P10 is now complete.**

## P10 progress (coordinator-driven, 2026-08-20)

- Census re-run as the live fixture spec: **2045 notes** (Logs 1454, work 427, personal 159,
  Templates 5, .obsidian 0), 269 non-markdown, 3 loose root notes, max depth 7, 0 symlinks,
  16 duplicate stems, 24 frontmatter-less notes. Registered: personal, work.
  Unregistered top-level: `.obsidian`, `Logs`, `Templates`.
- Deletion blast radius measured before writing anything: 22 references across the six P10.9
  symbols (`_entity_visible_in_workspace` 2, `legacy_entity_workspace` 7, `_workspace_of` 4,
  `_per_workspace_vault_paths` 3, `_entity_index_root` 3, `_legacy_workspaces` 3), plus the four
  `*_personal` / `*_work` config fields. Tractable.
- **P10.1 planning half landed (`970bd3d0`), read-only by construction.** Classifies every
  top-level path into moves / global_keeps / regenerated / ignored and REFUSES on anything left
  over. Destinations follow D5: `Logs/` and `Templates/` promoted to `<install>/Logs/` and
  `<install>/templates-src/`, each workspace vault to `<install>/<name>/memory-vault/`.
- **Two V1 gaps from earlier are now closed with explicit decisions**, recorded in code:
  `.obsidian/` is kept global (editor state for the whole vault, not per workspace); the three
  loose root notes (`INDEX.md`, `MEMORY.md`, `VOCABULARY.md`) are classified `regenerated`, since
  P10.6 rebuilds them per root and a stale index describing the old prefixed layout reads as
  current.
- **The plan found something the census misses.** It refused on a `.DS_Store` at the vault root.
  The census counts loose `.md` files at the root and non-`.md` files per top-level directory, so
  a loose NON-markdown file at the vault root falls between the two and is invisible to it. Now
  classified as ignorable, by explicit name rather than a pattern, so an unknown dotfile still
  refuses. Worth folding back into `workspace_census.py` as a reported shape.
- **V2 gate met**: 14 fixture tests. Classification, top-level conservation (every path in exactly
  one bucket, none twice, buckets equal to what is on disk), the read-only guarantee proved by
  hashing the tree before and after, and one refusal test per condition (unregistered dir,
  unrecognised loose file, symlink, workspace with no vault, non-empty destination, no workspaces,
  missing vault). Plus the D4 regression: `rehearse` never records "migrated" and `read_receipt`
  returns None for a survey.
- **V3 gate met**: APFS copy-on-write clone of the 899M install (instant, no extra space), stripped
  to 571M, only `workspaces.json` restored. Guarded by asserting the sandbox is neither the live
  install nor overlapping it. Rehearsal classified all **2318** real vault files with zero refusals
  and zero unclassified, left the sandbox byte-identical, and left the live vault untouched.
- Suite: 2696 passed / 1 pre-existing failure.

### Remaining for P10, in order
1. `apply()` with `git mv` per move so history follows, writing `status: "migrated"` only on full
   success, plus `--repair` and `--undo`.
2. P10.4 CLAUDE.md split: unbounded body verbatim to every root, bounded regions to the primary
   only, other roots' regions queued into their `Memory-Proposals.md`. No heuristic classification.
3. P10.5 skills triage, P10.6 indexes/FTS/sessions, P10.8 routines, P10.9 the eight deletions,
   P10.10 `_bootstrap_workspace`, P10.11 repair/undo.
4. V5 end-to-end drain through the new UI.
Every step rehearsed in the sandbox before the live install, per the rule the P9.3 incident set.
- 2026-08-20 — **apply() and undo() landed (`59dda6b0`), round trip proved on real data.**
  `git mv` per move so history follows; a failed move rolls back what the run already did.
  Clean-tree gate scoped to TRACKED changes per P10.2. Generated root notes AND the ignorable
  cruft are stashed into the runtime dir rather than deleted, so undo is byte-identical with no
  caveats: an earlier version recreated the Finder sidecar empty and failed the round-trip test by
  exactly one file. `.obsidian` promoted beside the workspaces so the vault directory ends up
  genuinely empty and is removed.
- 2026-08-20 — Verified on the APFS clone, **2318 files**:
  refused while 18 tracked files were dirty and moved nothing; after committing them migrated in
  0.1s with 5 moves and 4 stashed; conservation exact (2314 under the new roots + 4 stashed);
  **zero files changed content**, compared blob-by-blob against `HEAD~1` through the move table;
  `git log --follow` still reaches the original history; undo restored all 2318, cleared the
  receipt and removed the new roots; the live vault was byte-identical throughout.
- 2026-08-20 — Bug found by running the gate on real data, not by a test:
  `_run_git` stripped its combined output, and `git status --porcelain` encodes index and worktree
  state in columns 1-2, so stripping ate the leading space of the first line and truncated that
  path to `emory-vault/...`. A refusal message would have named a file that does not exist. Fixed
  to rstrip only, with a regression test asserting every reported path resolves on disk.
- 2026-08-20 — **I made the exact mistake this ledger warned about.** My first real-data
  conservation count came up 5 short, because I keyed paths relative to each root so duplicate
  stems collided: the census reports 16 duplicate stems and both workspaces hold `INDEX.md` and
  `MEMORY.md`. Re-keyed on full path and the count was exact. The V1 note said "conservation checks
  must key on full path and never on stem" and I still did it. Worth remembering that writing a
  lesson down is not the same as applying it.
- 2026-08-20 — **The live install would refuse today**: 18 tracked files under `memory-vault/` have
  uncommitted changes (deleted People notes, modified Learnings/Memory-Proposals, VOCABULARY). That
  is the gate working as designed. The vault must be committed before the real cut runs.

### Still remaining for P10
P10.4 CLAUDE.md split (unbounded body verbatim to every root, bounded regions to the primary only,
other roots' queued into their Memory-Proposals.md, no heuristic classification) · P10.5 skills
triage · P10.6 indexes/FTS/sessions rebuild · P10.8 routines · P10.9 the eight deletions ·
P10.10 `_bootstrap_workspace` · P10.11 `--repair` · CLI wiring · V5 end-to-end drain.
- 2026-08-20 — **P10.4 guide split landed (`c70707f7`).** Unbounded body verbatim to every root,
  bounded regions to the primary only, the primary's entries queued into every other root's
  proposal queue. No heuristic classification, which is the point: deciding which remembered fact
  belongs to which workspace is a judgement about the user's prose, and guessing would reproduce
  the misfiling this release repairs, in the one place read before the user speaks.
- 2026-08-20 — Two bugs found by running it against the REAL guide rather than a fixture, which is
  becoming the pattern of this phase:
  1. A region body opens with its own markdown heading (`## Agent memory`), and `parse_entries`
     keeps it attached to the first entry, because entries are separated by `§` and a heading is
     not one. It was being queued as a remembered fact. Now dropped as scaffolding.
  2. Entries may span multiple lines, and the real guide has exactly one that does. The queue's
     invariant is one bullet per line, since `proposal_kinds.BULLET_RE` matches line by line, so a
     multi-line bullet would leave its continuation as loose prose in `Memory-Proposals.md`:
     uncountable by every counter and invisible to the dedupe check. Newlines now collapsed, with a
     test that asserts every generated bullet parses through the shared regex.
- 2026-08-20 — Verified against the live guide: 20 entries in, 20 single-line bullets out, all
  parsing, none carrying a heading, ZERO entries whose text fails to reach the queue, body
  byte-identical between roots, work's regions genuinely empty. Suite 2711 passed / 1 pre-existing.
- 2026-08-20 — `read_region_text` reuses `memory_tool`'s marker regexes on text instead of adding a
  second parser. One definition of where a region begins and ends is the entire point of this
  release, after three copies of one bullet regex drifted apart in P1.
- 2026-08-20 — **Found and fixed the gap that would have broken the install (`2895f1bd`).**
  `apply()` moved every vault into its agent root and left the registry naming the old location, so
  the install it produced was broken: each entry still said `memory-vault/<name>` while the directory
  now lived at `<name>/memory-vault`. The migration now repoints `vault_root` for every migrated
  workspace and records an exact before-image, so undo restores the registry as well as the tree.
  Raw-list rewrite, so an unknown key a future release adds survives a migration meant to change one
  field.
- 2026-08-20 — `CiaoConfig.agent_root` now flips per install, gated on the migration's own receipt
  rather than on a release. An install that has not re-rooted resolves exactly as today, so a
  half-flipped state cannot exist. Receipt lookup cached in a module-level map keyed by runtime dir
  (CiaoConfig uses slots), with `reset_reroot_cache` called by apply and undo.
- 2026-08-20 — **P10.6 + CLI landed (`ee6f85e0`)**, proved end to end on a fresh committed clone:
  migrated with 5 moves and 4 stashed, registry repointed, indexes rebuilt at 157 and 426 entries
  with **zero** occurrences of a workspace prefix, FTS rebuilt at 157 and 426 docs. The migrated
  install then resolved correctly (`agent_root` returning `personal` / `work`, both vaults present,
  shared vault gone, `Logs` and `templates-src` promoted). Undo restored everything including the
  registry's exact previous values and all four stashed files.
  `ciao workspace-reroot` prints the plan by default, `--rehearse` / `--apply` / `--undo`, exit 1 on
  a refusal so a script can gate on it.

### P10.9 and P10.10 are BLOCKED, and correctly so
The eight deletions cannot land until the migration has actually RUN on an install. The work order
says why: deleting `_entity_visible_in_workspace` while an un-rooted install exists leaves no filter
over a still-prefixed index, so every entity becomes visible in every session. Fail-open, strictly
worse than today. `agent_root` is now receipt-gated, so on this install it still returns the shared
root, which means the filters are still load-bearing here.
Sequence: commit the live vault (18 tracked files are dirty, so the gate refuses) → run
`ciao workspace-reroot --apply` → THEN land the deletions and `_bootstrap_workspace`. Landing them
before that order is complete would ship the fail-open state to a live install.

### Remaining, unblocked
P10.5 skills triage · P10.8 routines · P10.11 `--repair` · V5 end-to-end drain.

## P10.5 / P10.8 / P10.11 (coordinator, 2026-08-20 second session)

- **Blocker 1 is CLEARED.** The live vault is committed: `git status --porcelain
  --untracked-files=no` on `~/repos/ciao` is empty, and `d6f8412b` ("main session commit
  2026-08-20 09:21:55Z") carries the 19 files the handover listed as dirty. The clean-tree gate
  would no longer refuse. The only remaining precondition for `--apply` is the operator's
  approval, which is theirs to give and was NOT taken here.
- **P10.5 skills triage landed (`f9539770`).** The whole catalog moves to the primary root and
  `Workspace/Skill-Triage.md` is written with every destination cell blank. `skills-lock.json`
  moves with it, because `_refresh_upstream_skills` reads the lock from the root it is syncing, so
  leaving it at the install root would strand seven upstream skills at a path no root reads. It is
  also self-healing: the upstream copies live under the old `.claude/skills`, which is not moved,
  so the primary's next sync sees them missing and refetches them.
- The sheet enumerates the DIRECTORIES on disk and takes descriptions from
  `skills_inventory.build_skill_inventory`. Reusing the inventory as the enumerator would have
  been wrong: it globs `*/SKILL.md`, and the reference install has a catalog directory with no
  SKILL.md at all (`skills/adversarial-review`, whose only content is an ignored `__pycache__`).
  It moves, so it has to appear on the sheet, or the sheet is an incomplete account of what the
  migration did. Same conservation rule the vault classification already follows, and the same
  class of gap as the `.DS_Store` the census missed.
- Verified on an APFS clone: 27 rows (20 catalog directories including the husk, 7 upstream), every
  destination blank, every moved directory present on the sheet, conservation exact keyed on full
  path (8107 → 8105 = −4 stashed +2 created), zero content change across every moved file, the live
  install byte-identical throughout, and undo restoring the catalog, the lockfile, the registry and
  the stashed notes.
- Two P10.5 side effects worth naming: the clean-tree gate now covers every path the migration
  moves rather than just the vault (a tracked skill carried across with uncommitted edits is
  exactly where `git checkout` stops being a working undo), and `apply()` now REQUIRES `primary`
  instead of deriving it. Which root inherits the guide's regions and the whole catalog is the most
  consequential choice in the migration; a caller that has not decided must not be handed a default
  that looks like a decision.
- **`--undo` does not un-derive the rebuilds, and now says so.** After `--apply` plus the P10.6
  index rebuild, undo leaves exactly two modified and two new files on the reference install
  (`memory-vault/{personal,work}/INDEX.md` modified, `VOCABULARY.md` new). Those are regenerated
  aggregates, `git status` names them, and carrying a copy of every replaced index in the receipt
  would buy nothing. Documented on `undo` and in the CLI description rather than left as a surprise.
- **`rebuild_search_index` resolved its database from the ambient environment.**
  `fts_search.get_db_path` reads `CIAO_MEMORY_DIR` (default `~/.ciao`), so migrating an install
  that is not the ambient one would have dropped the AMBIENT install's index — the same
  environment leak P2 found in the os-audit command. Given an optional `db_path`, with the residual
  limit stated honestly: there is no per-install search database to derive from an install root.

### P10.8 routines (commit `b924e438`)
- **Measured before writing anything, and the present bug is bigger than the work order says.**
  With two workspaces registered, SIX of the seven audit sections came back byte-identical, and
  hygiene is `per_workspace: true`, so it reported all of them once per workspace. Not just job
  runs and upgrade notices: `vault_hygiene` was identical too, at 20 KB and 240 defects each,
  because `_vault_audit` ran over the whole SHARED vault regardless of which workspace the run was
  for. A personal hygiene chat was reporting every defect in the work notes as its own.
- `run_os_audit` gains a `scope`. `workspace` drops the sections whose subject is the global
  runtime dir; `global` drops the ones describing one workspace; `all` is unchanged, so the PWA
  endpoint and a bare `ciao os-audit` report exactly what they did before. A section outside the
  scope is REMOVED, not zeroed: an empty section reads as "checked and clean", which is a claim the
  run never made. Both halves keep `setup_audit`, over the roots that half actually reads.
- Naming a workspace now narrows the per-root sections through `workspace_vault_root` and
  `agent_root`. After the change, on the same clone: personal 81 defects, work 241, and the
  work-only broken links no longer appear in personal's report. `memory_hygiene` reports one guide
  instead of all N. `skill_audit` and `rule_audit` are STILL identical between roots, correctly:
  the catalog and the guide really are shared until the migration runs.
- **`system-vault-index` is gone; its rebuild is back inside hygiene.** It was split out because it
  wrote one shared INDEX.md while hygiene was per-workspace. After the re-rooting each root owns
  its own pair, so there is no shared artifact left for a global routine to write. Before the flip
  both hygiene runs rebuild the same shared index, which is idempotent; after it, each rebuilds its
  own. The freed weekly slot becomes `system-install-health`, reporting the global half once.
- The fold needed a way for a per-workspace routine to name its own vault, so each chat now exports
  `CIAO_VAULT_ROOT` from a new `CiaoConfig.agent_vault_root`. There is one process-level value and,
  after the migration, N vaults; a single inherited value cannot name the right one.
  `agent_vault_root` is deliberately DISTINCT from `workspace_vault_root`, and the distinction is
  the whole point: the latter is where a workspace's NOTES live (a subtree of one shared vault
  today), the former is where the aggregate files ABOUT them live (one shared pair today, one per
  root afterwards). Both resolve from the same receipt so they cannot disagree.
  `CIAO_WORKSPACE` deliberately stays the install root: `.env`, `.runtime` and the registry are the
  global layer and live there, not in a root.
- `_resolve_skills_roots` takes a workspace name and resolves per call. **`system-skill-evolution`
  deliberately stays a single routine**, against the design record's "fan-out-eligible": the
  catalog is one directory until the migration has run AND the user has worked through
  `Skill-Triage.md`, so fanning it out now would mean N identical passes over one catalog writing
  DUPLICATE proposals into N queues. The distinction applied throughout this step: fold what is
  idempotent, defer what duplicates writes. Same judgement P8 made when it restored the session
  glob rather than shipping premature isolation.
- **A claim in the design record does not hold against the tree.** §6 says the rule audit "reasons
  about the global tool denylist"; `grep -n "denylist\|disallowed" ciao/os_audit.py` is empty. The
  split was derived from what the code actually reads, not from that sentence.

### A defect P10.6 shipped, found on real data (commit `0dda15a8`)
`rebuild_search_index` reported "personal 158 indexed, work 426 indexed" and left **426** rows in
the database. Every personal note was gone: 579 notes on disk, 426 searchable. Two causes.
1. The stored key was relative to the indexed directory's own PARENT, so after the migration both
   roots keyed as `memory-vault/People/User.md` and the second pass overwrote the first. Keys are
   now relative to the INSTALL root, unique in both layouts, with the base recorded in a
   `search_config` row so a change to it drops the index rather than mixing two key formats.
2. The prune deleted every row not found under the directory just indexed. That is ALSO a live bug
   before any migration: `vault_search` re-indexes the principal's vault on every search, so on a
   two-workspace install each search deleted the other workspace's rows and the next search there
   paid a full re-index. Now scoped to the subtree being indexed.
Scoping the prune removed an isolation nobody designed — with both roots' rows coexisting, an
unfiltered `search_vault` would hand a personal session work notes — so the filter is now explicit,
with a LIKE `ESCAPE` because a workspace name is the user's and may contain `_`, which LIKE treats
as a wildcard. Verified on the migrated clone: 584 rows, 158 + 426, zero removed, and a scoped
search for each root returns nothing from the other.
The receipt had been reporting the prune count under the key `"skipped"`, which is why the earlier
P10.6 verification read "426 indexed, 155 skipped" as benign. Renamed to `removed`.

### P10.11 `--repair` (commit pending in this session)
- Idempotent reconciliation to the registry, implementing the design record's §11.1 table:
  a missing root is created with its agent assets; an `AGENTS.md` that does not resolve to its
  root's `CLAUDE.md` is re-linked; packaged, own and shared skills are re-mirrored; an `INDEX.md`
  that is absent or still keys entries under a workspace name is rebuilt; a search index holding a
  path that no longer resolves is dropped and rebuilt. A third report-only drift, `guide_unsplit`,
  came out of what the repair itself exposed — see the blocking gap below.
- **It refuses outright on an install that has not re-rooted**, and that is not caution. Before the
  migration a workspace prefix in the shared `INDEX.md` is CORRECT, and it is what
  `_entity_visible_in_workspace` filters on. "Repairing" it there would strip the prefixes and
  leave no filter over the index — the same fail-open state the P10.9 deletions are gated on,
  reached from the other direction.
- **Two drifts are reported, never guessed**, deviating from the table's "Recompose":
  a root with no vault (which notes belong to a workspace is a question about the user's own
  material), and a `.mcp.json` absent against a shared one. The second is a deliberate refusal: an
  MCP entry grants credentialed access, per-root composition does not exist yet, and the P9.3
  incident is what happens when reachability is inferred instead of declared. Either makes the CLI
  exit 1 so a script cannot read the result as clean.
- The shared-source mirror DID have to be built, because there was nothing to "re-mirror" from:
  `skills-src/` mirroring has never existed. `sync_skills.mirror_shared_skill_sources` is the one
  definition, so the normal sync path can adopt it without a second implementation. Precedence is
  a root's own `skills/<name>` > a shared source > a packaged stock copy, and it converges in one
  run because `_install_stock_skills` already leaves an existing symlink alone.
- One bug caught in review of my own first pass: the drift report was read AFTER the mirroring ran,
  so it always came back empty and a real repair looked like a no-op. A second: a name-only check
  missed a shared skill shadowing a packaged stock skill of the same name (`pr`, `web-research`),
  so the repair silently replaced a stock copy while reporting nothing. Both fixed, both covered.
- Verified on the migrated clone: first run fixed two unlinked `AGENTS.md` and mirrored 29 stock +
  19 own skills, second run clean; breaking three things then repairing fixed exactly those three
  and left the fourth run clean; a shared skill shadowing a stock name is reported and linked and
  converges immediately.

### NEW BLOCKING GAP: P10.4's split is written and never applied
`split_guide` is a pure function with seven tests and **no caller**. Nothing writes each root's
`CLAUDE.md`, nothing queues the primary's region entries into the other roots'
`Memory-Proposals.md`, and nothing retires the shared `<install>/CLAUDE.md`. So `--apply` today
produces roots whose cwd holds no guide at all: the operator's `CLAUDE.md` silently stops being
loaded. Confirmed by running `--apply` on the clone — the roots have `memory-vault/` and `skills/`
and nothing else.
**Measured, not inferred, and worse than that.** After `--apply` then `--repair` on the clone, each
root held the **2202-byte packaged stock guide** with EMPTY `ciao:memory` / `ciao:profile` regions,
while the operator's real **27377-byte** guide (4 region markers, 20 entries) sat orphaned at
`<install>/CLAUDE.md`. Every remembered fact would have disappeared from every session, and the
repair would have reported success. `_ensure_linked_workspace_guides` copies the stock guide
whenever one is missing, which is right on a fresh install and catastrophic here.
So `--repair` now REFUSES to seed a guide while the install root still holds an unsplit one, and
reports `guide_unsplit` instead. That converts silent loss into a visible finding, but it is a
guard, not the fix: the fix is wiring the split.
Related and from the same cause: after `--apply` a root has no `.claude/`, no `CLAUDE.md` and no
`AGENTS.md` until something syncs it. `--repair` is currently the only thing that completes a root,
which is P10.10 `_bootstrap_workspace` territory. **Wiring the guide split and the root bootstrap
into `apply()` must land before the real migration runs.**

### Remaining
1. **Wire P10.4's `split_guide` and a root bootstrap into `apply()`** — blocking, see above.
2. Operator decision: run `ciao workspace-reroot --apply` on the live install (vault now committed).
3. P10.9 the eight deletions and P10.10 `_bootstrap_workspace` — still gated on (2).
4. §11.2's five `operator_actions` detectors (`workspace-unmigrated`, `workspace-root-missing`,
   `workspace-assets-stale`, `skill-triage-pending`, `legacy-env-ignored`), which give `--repair` a
   run button. Deliberately after the repair rather than before it.
5. V5 end-to-end drain — needs the migration to have run.

### Follow-ups filed this session, none blocking
1. `rebuild_indexes` and `rebuild_search_index` hardcode `"memory-vault"` rather than taking the
   vault directory name, so an install with a non-default `CIAO_VAULT_ROOT` leaf would rebuild
   nothing. `plan()` already derives the name correctly; these two should take it.
2. The memory-curation prompt still says "one `CLAUDE.md` is shared by every workspace, so a
   per-workspace run promoting into it would leak". True today, false after the cut. It must be
   revised in the same release as the deletions, or curation stays needlessly forbidden from
   promoting into a region it now owns.
3. Unrelated in-flight work is sitting in the tree, not written here and not committed here:
   `ciao/control_plane.py` accepting `dismiss` as a synonym for `reject` in
   `memory_proposal_resolve`, plus its test in `tests/test_proposal_kinds.py`. Left for whoever
   wrote it. All commits this session were path-scoped around it, and HEAD was verified green in a
   detached worktree without it.
4. `workspace_census.py` still misses a loose non-`.md` file at the vault root (carried over).
