# Route memory proposals to their real destination

Status: approved (2026-08-21), implementation started
Written: 2026-08-21
Scope: `ciao/insights.py`, `ciao/memory_proposals.py`, `ciao/project_doc_update.py`, `ciao/proposal_kinds.py`, prompt/copy assets, tests; optional small PWA follow-up
Generated plan output: `docs/plans/MEMORY_PROPOSAL_ROUTING_PLAN.md`
Visual companions: none (backend routing change; no UI state to review)

## Resume block

- Status: implementing — C6 done, C7 partial (see Verification)
- Current checkpoint: C7
- Next action: full-suite green run once the concurrent auth/expiration refactor in the same tree stops breaking collection
- Blocker: unrelated working-tree changes (auth moved to `routes_auth`, `expiration_tag_error` re-export removed from `memory_injector`) break 5 test-module collections and 14 tests that import them
- Implementation repository: `/Users/raffaelefarinaro/repos/ciaobot`
- Verified on: 2026-08-21 against `ciao/insights.py`, `ciao/memory_proposals.py`, `ciao/project_doc_update.py`, `ciao/proposal_kinds.py`, `ciao/web/project_chats.py`, `ciao/web/routes_api.py`, `web/src/components/ProposalReviewPanel.vue`, `web/src/components/ProjectSidebar.vue`, `ciao/stock/agents/memory.md`, `ciao/stock/schedules.json`, `tests/test_memory_proposals.py`, `tests/test_proposal_kinds.py`

## Outcome and user value

Today every durable fact extracted from an archived chat is queued as "belongs
in a CLAUDE.md bounded region" (`[memory]` / `[profile]`), and some are even
auto-promoted there. The bounded regions are injected into **every session,
in every project**, so they fill with project-scoped facts that pollute all
other contexts until nightly curation (maybe) rescues them.

This plan makes the pipeline classify each fact by destination at the moment
it is extracted — project canonical doc, `People/<name>.md`,
`Workspace/Learnings.md`, or a bounded region — so CLAUDE.md stays small,
cross-project, and human.

## Scope and non-goals

In scope:

- Extraction prompts learn the destination taxonomy (scope classification).
- The proposal pass stops double-filing Decisions into regions when a
  canonical project doc owns them.
- Queue grammar gains destination kinds beyond `memory`/`profile`.
- Prompt/copy assets that name destinations are updated in one pass.

Out of scope for this release:

- Any change to auto-promote policy beyond what Phases 1–2 imply (stays
  User-corrections-only).
- A new PWA accept flow for the new kinds (they remain curator-promotable;
  see Q-02).
- Reworking `update_project_doc` safety guards or the nightly curation
  schedule mechanics.

## Current-state evidence

Observed (file read unless noted):

| # | Fact | Where |
|---|---|---|
| E1 | Extraction output schema is section-shaped: Errors, User corrections, New entities, Decisions, Reusable snippets, Open loops, Vault changes. No destination/scope concept. | `ciao/insights.py:240` (`_INSIGHTS_SYSTEM_PROMPT`), `:1031` (`_TEXT_MODE_SYSTEM_PROMPT`) |
| E2 | Proposal routing has exactly two targets: User corrections + Decisions → `[memory]`; `person:operator/user` entities → `[profile]`; other durable entities → `[memory]`. | `ciao/memory_proposals.py:54-55,153-186` |
| E3 | Decisions are double-filed: they feed the region queue *and* the project-doc fold triggers on Decisions/Open loops. | `memory_proposals.py:54` vs `ciao/project_doc_update.py:35` |
| E4 | Archive time knows the chat's canonical doc path, but only for non-auto projects, and never passes it to the proposal step. | `ciao/web/project_chats.py:4244-4262`; `proposals_from_archive` signature `memory_proposals.py:416` |
| E5 | "User corrections" are auto-promoted straight into CLAUDE.md regions at archive time (state-shaped text only). | `memory_proposals.py:51,244-300`; `insights.py:665` |
| E6 | Queue grammar is a single registry: `KINDS = ("memory", "profile", "user", "rehome")` with typed accept descriptors; counters, CLI, audit, control plane, and API derive from it. | `ciao/proposal_kinds.py:24-124`; consumers in `routes_api.py:6865+`, `os_audit.py`, `control_plane.py:398-406` |
| E7 | The PWA renders kind-aware review rows (labels, filters, region/rehome branches). | `web/src/components/ProposalReviewPanel.vue:95-201`, `ProjectSidebar.vue:1058-1064`, `web/src/lib/types.ts:1108-1129` |
| E8 | The destination taxonomy already exists canonically outside the pipeline: cross-project prefs/env/lessons → `ciao:memory`; identity/style → `ciao:profile` (+ `People/User.md`); reusable how-to → `Workspace/Learnings.md`; standing directives → CLAUDE.md body; project facts → canonical doc; people → `People/`. | `ciao/stock/agents/memory.md:26-29`; onboarding prompts `project_chats.py:1865-1889`; curation prompt `ciao/stock/schedules.json` |
| E9 | Bounded regions inject into every session; caps advisory (2200/1375 chars). | `ciao/memory_injector.py`, `ciao/config.py:521`, `ciao/stock/workspace/CLAUDE.md:30-38` |
| E10 | Downstream parsers depend on the section schema: trajectory counts `## User corrections`/`## Decisions`; doc fold triggers on section names; proposal pass uses `_split_sections`. | `ciao/trajectory_builder.py:195-275`, `project_doc_update.py:74-81`, `memory_proposals.py:76-103` |
| E11 | Test blast radius: ~25 `propose_from_insights` call sites plus proposal-kind tests. | `tests/test_memory_proposals.py`, `tests/test_proposal_kinds.py` |

Assumed (to verify during implementation):

- A1: The insights model can reliably make a coarse section-level scope call
  (project-scoped vs cross-project). Cheap-model drift is the known risk;
  mitigations are D-01/D-04.
- A2: Nightly curation currently under-routes (user report; not measured in
  code). The plan does not depend on this being true — removing the
  region-default fixes the outcome either way.

## Recommended direction

Classify **at extraction time, at section granularity**, route **at proposal
time using archive-time context**, and keep **review before any region write**.

### Phase 1 — stop double-filing (no prompt change)

Thread `project_doc_path` from `extract_and_append` into
`proposals_from_archive`. When a canonical doc exists, Decisions and Open
loops are excluded from region-bound proposals — the doc fold already owns
them (E3/E4). When no doc exists (auto projects), current queueing stands.

### Phase 2 — teach the prompts the taxonomy

Extend both extraction prompts with the destination rule block mirrored from
`stock/agents/memory.md` (E8), and add one section:

```
## Project facts
- <durable fact about this project only: constraint, status, decision>. [idx=N]
```

Rules added: preferences/corrections about the user are global; facts scoped
to this repo/project go in Project facts; people get filed as entities; do
not repeat a Project fact inside Decisions.

### Phase 3 — queue grammar grows destinations

`proposal_kinds.KINDS` += `project`, `people`, `learnings`, each with its own
accept descriptor type (so the region-edit path can never misroute them):

- `[project <doc-path>]` — fold into the named canonical doc
- `[people <Name>]` — create/update `People/<Name>.md`
- `[learnings]` — append to `Workspace/Learnings.md`

`propose_from_insights` emits them: Project facts → `[project]` (skipped when
the fold already ran), person entities → `[people]` instead of `[memory]`,
reusable snippets/lessons → `[learnings]`. Header copy in
`Memory-Proposals.md`, the curation schedule prompt, capabilities skill, and
onboarding strings are updated in the same pass.

### Phase 4 — tests and docs

Update `test_memory_proposals.py`, `test_proposal_kinds.py`,
`test_insights.py`; add a routing test per new kind; refresh
`docs/ARCHITECTURE.md:318` and the capabilities skill text.

## Alternatives and rejected options

### Per-bullet destination tags (`- [global] ...` inside every section)

More expressive, but the insights model is a cheap fast model; a tag it must
remember on every bullet drifts badly, and every downstream parser (E10)
would need to strip it. Section-level gets ~the same routing value at a
fraction of the failure modes. Rejected for now; revisit if mixed-scope
sections prove common (Q-03).

### Route everything at promotion time (curator-only)

No pipeline change, but this is today's de-facto design and it demonstrably
defaults everything to CLAUDE.md. The curator keeps final say over region
writes regardless.

### Auto-write new kinds without review

`update_project_doc` already writes without review, so `[project]` could too
— but People notes and Learnings appends have no equivalent guard rail today.
Keep them queued (D-04); revisit once accept flows exist.

## Decisions and hard-to-reverse bets

| ID | Decision | Rationale | Status |
| --- | --- | --- | --- |
| D-01 | Per-bullet destination tags (`[memory]`, `[profile]`, `[project]`, `[people: <Name>]`, `[learnings]`, `[review]`), section schema unchanged | User approved tags now (F-03); sections stay stable for trajectory/doc-fold parsers (E10) | Approved |
| D-02 | Decisions/facts tagged `[project]` are dropped from the queue when the doc fold actually wrote; queued as `[project <doc-path>]` when it did not | Owner: "if the fold takes something in there is no suggestion anymore — stays if it needs to be checked" | Approved |
| D-03 | New destinations extend the `proposal_kinds` registry with distinct accept descriptor types | Single source of truth (module docstring contract); type-distinct accepts prevent misrouting | Approved |
| D-04 | **Revised:** auto-apply at archive time for every confidently-tagged bullet (regions via state-shape guard, project via fold, people stub note, learnings append); only `[review]` and write-failures queue. Supersedes "review-before-promote" | Owner: "everything can be autopromoted… if the model is not sure, keep it for human or agent review" | Approved |
| D-05 | Extraction prompt carries the destination taxonomy verbatim from `stock/agents/memory.md` | One taxonomy, already canonical; prompt and SOP must not diverge | Approved |
| D-06 | One-click PWA accept ships for `project`/`people`/`learnings`; `[review]` rows stay curator/human-routed | Owner chose "one-click accept now"; a review row has no known destination to act on | Approved |

## Open questions with recommended defaults

| ID | Question | Recommended default | Status |
| --- | --- | --- | --- |
| Q-01 | Should `[project]` bullets queue when the fold ran? | No — dropped when the fold wrote; queued otherwise | Resolved (D-02) |
| Q-02 | One-click PWA accept for new kinds? | Yes, this release (`project`/`people`/`learnings`) | Resolved (D-06) |
| Q-03 | Per-bullet scope tags now or defer? | Now | Resolved (D-01) |
| Q-04 | Auto-promote scope? | Everything confident; `[review]` + write-failures queue | Resolved (D-04) |

## Not yet specified (fog of war)

- How `[people]` accept should behave when the note already exists (append vs
  merge vs link-only) — becomes specifiable once a real queue shows duplicate
  person proposals.
- Whether per-workspace region writes from proposals need the shared-install
  leak guard the curation prompt enforces — depends on how multi-root installs
  behave with the new kinds.

## Feedback and decision log

| ID | Location | Feedback | Decision | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| F-01 | Whole plan | "All memory proposals end up in claude.md — not a good approach" | Plan adopts destination classification at extraction + routing at proposal time | accepted | This document; E2/E3/E5 |
| F-02 | Recommended direction | User asked for classification "in which file" for memory-file targets | Kinds carry explicit destinations (`[project <path>]`, `[people <Name>]`, `[learnings]`) | accepted | Phase 3 |
| F-03 | Open questions | Owner answers 2026-08-21: one-click accept now; fold-consumed facts leave the queue; auto-promote everything confident, unsure → review; per-bullet tags now | D-01/D-02/D-04/D-06 revised accordingly | implemented | This conversation |
| F-04 | Outcome section / README copy | "Nothing promoted without review" no longer holds under D-04 | Copy updated to "confident facts apply automatically; uncertain ones wait for review" | implemented | README.md, capabilities SKILL.md |

## Implementation checkpoints

### C0. Start or resume
Read this Resume block, then the live files named above; confirm status still matches the repo. Exit: next action recorded.

### C1. Ground the plan
Re-verify E1–E11 still hold (line numbers will drift). Exit: discrepancies noted here or fixed.

### C2. Set direction
Confirm D-01…D-05 and Q defaults with the user. Exit: plan marked approved.

### C3/C4. Artifact review
Done (this document; no companions needed).

### C5. Approval
User accepts direction. Exit: status → approved.

### C6. Implement
Phase order 1→4; each phase lands green before the next starts. Exit: all planned edits exist or are explicitly deferred.

### C7. Verify
Focused: `pytest tests/test_memory_proposals.py tests/test_proposal_kinds.py tests/test_insights.py`. Then `pytest tests/`. If PWA files changed: `cd web && npm run build`. Exit: recorded results.

### C8. Close
Status → complete; record commit. Exit: handoff-ready.

## Verification and rollout

Recorded 2026-08-22: `tests/test_memory_proposals.py` + `tests/test_proposal_kinds.py` 51 passed;
`tests/test_web_proposals.py` 35 passed; `pytest tests/` 2922 passed with 14 failures and
5 collection errors — every failure an import of `auth_login`/`auth_check`/`expiration_tag_error`
moved or un-exported by a concurrent refactor in the same working tree, none touching this plan's
files; `cd web && npm run build` passes. Behavioral checks after Phase 1: archiving a chat attached
to a real project produces no `[memory]` Decisions rows; archiving under General still does.
After Phase 2/3: a fixture transcript with a project-scoped decision, a global preference
correction, a new person, and a lesson yields four differently-addressed outcomes. Rollout is
same-repo (no migration): old `[memory]`/`[profile]` bullets stay valid kinds; the header refresh
in `append_proposals` already handles stale copy.
