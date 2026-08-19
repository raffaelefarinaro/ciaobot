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
| P3 unrehomed_people notice | delegate | RUNNING | chat-23268601 |
| P4 Surfaced-actions strip | — | TODO | needs P2, P3 |
| P5 Queue review UI | split | RUNNING | server half re-dispatched chat-35f8da7d; UI half in wave 3 |
| P5.9 User.md must never move | delegate | DONE | commit 1ac96430 |
| P6 Vocabulary + agent_root | delegate | DONE | commit efa2b6d6 |
| P7 Provider seam | delegate | DONE | commit 06b94e6c |
| P8 Session paths | — | TODO | needs P6; shares `project_chats.py` with P7 |
| P9 Per-root memory + MCP allowlist | — | TODO | needs P2, P6 |
| P10 The cut | — | TODO | gated on all of P1–P9 + V1–V3 |
| V1 workspace-census | delegate | DONE | commit 796d84af |
| V2 Fixture assertions | — | TODO | with P10 |
| V3 Real-data rehearsal | — | TODO | with P10 |
| V4 Full suites | coordinator | PARTIAL | python 2597 pass / 2 pre-existing fails; npm not a wave-1 gate (no web files touched) |
| V5 End-to-end drain | — | TODO | after P5 |

## Coordination decisions

- **No worktrees.** Waves are cut on file ownership instead, so every delegate works in
  the one checkout with a disjoint file set. Avoids merge resolution on a weak model's
  output and lets the coordinator read `git diff -- <owned paths>` per phase.
- **Delegates do not commit.** Coordinator reviews the diff, runs the suites, commits.
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
