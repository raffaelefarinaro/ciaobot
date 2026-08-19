# Per-workspace memory curation (with cross-workspace reconciliation)

Status: **all steps implemented 2026-08-19.** Step 8's tooling is built but not yet
applied to the reference vault — that is the user's call, and the judgement cases
need review either way.
Written: 2026-08-19
Revised: 2026-08-19 (implementation path corrected against the schedule store)
Scope: `ciao/stock/schedules.json`, `ciao/schedules.py`, `ciao/web/project_chats.py`,
`ciao/stock/agents/memory.md`, plus a reconciliation pass

Related: [`VAULT_VOCABULARY_PLAN.md`](VAULT_VOCABULARY_PLAN.md) — shares the
`system-workspace-hygiene` routine and the same scope taxonomy.

Fixes the observed bug where **every person — including work-only contacts — is
written to `personal/People/`** because the single stock curation schedule always
resolves to one workspace.

Observed on this install: 95 notes in `personal/People/`, 57 of which mention
`work`; the `work` vault has no `People/` folder at all.

---

## Problem

All three stock system schedules ship with `workspace: "default"`
(`ciao/stock/schedules.json:20,42,64`). `"default"` is not a real workspace and
nothing resolves it specially, so the resolver falls through to a single
workspace:

- `ciao/web/project_chats.py:4624` `_schedule_workspace_hint` → `"default"` is
  not a known workspace → legacy heuristic (`schedule_id` doesn't start with
  `sched-work`) → `"personal"`; if that is known it wins, otherwise
  `workspace_names()[0]`.

So the effective target is the primary workspace — literally `personal` on
installs that have one by that name, and the first registered workspace
otherwise. It is *not* a hardcoded `personal`, but the outcome is the same: one
vault gets curated and the others never do.

`ciao/stock/agents/memory.md` has no workspace-awareness at all — it names
`People/User.md` and the bounded regions, and says nothing about which
workspace's `People/` a person belongs in. Running it against the primary vault
is therefore enough to misfile every work contact.

This is a **routing bug**, not a data problem. The source material is already
per-workspace:

- Chats archive per-workspace (`ciao/transcripts.py`).
- Memory proposals write per-workspace
  (`ciao/memory_proposals.py:271` `append_proposals` →
  `<workspace-vault>/Workspace/Memory-Proposals.md`).
- The memory agent's routing targets are per-workspace (project docs, `People/`,
  the workspace `CLAUDE.md` `ciao:memory`/`ciao:profile` regions).

### What the PWA shows today

`_enrich_schedule` (`ciao/web/routes_api.py:4590-4593`) overwrites the
serialized `workspace` with the *effective* one, so the PWA displays these
routines as belonging to the primary workspace and the "Run this routine in"
select (`web/src/components/SchedulePanel.vue:289-298`) renders a valid
selection. Nothing looks broken in the UI — which is why the misfiling went
unnoticed. The stored value stays `"default"` until the user touches that select.

## Routine scope taxonomy

"Per-workspace or not" is the wrong question — three different things can be
workspace-scoped independently, and every stock routine mixes them:

- **Inputs** — the evidence the routine reads (archived chats, trajectories).
- **Subject** — the artifact it reasons about (a vault, a skill catalog, an index).
- **Outputs** — where it writes (a vault folder, a proposal queue, a chat).

A routine only wants fan-out when all three are workspace-scoped. When the
subject is global but inputs/outputs are not, fan-out duplicates work and
creates write conflicts; the fix there is **output routing**, not more runs.

| Routine | Inputs | Subject | Outputs | Verdict |
|---|---|---|---|---|
| `system-memory-curation` | per-workspace (chats, proposal queue) | per-workspace vault | per-workspace vault — **except the bounded regions, which are global** | **fan out**, after resolving the bounded-region conflict (step 0) |
| `system-skill-evolution` | per-workspace trajectories, **read globally today** | **global** skill catalog | per-workspace queue, **written to one workspace today** | **one run, route output** (step 6) |
| `system-workspace-hygiene` | whole vault | **global** `INDEX.md` + per-workspace `MEMORY.md`/queues | one chat, **mixing all workspaces** (step 7) | **one run, scope the report** |

Evidence for each verdict:

- **Curation.** Inputs are already partitioned: chats archive per-workspace
  (`ciao/transcripts.py`), proposals write per-workspace
  (`ciao/memory_proposals.py:271`). The write targets are per-workspace vault
  folders. Only the bounded regions break the pattern — see step 0.
- **Skills are global.** The catalog lives at `CIAO_WORKSPACE/skills` and
  `CIAO_WORKSPACE/.claude/skills` (`ciao/skill_evolution.py:73-91`) — the
  workspace *root* (`~/repos/ciao`), shared by every logical workspace. There is
  no `memory-vault/<workspace>/skills`. N runs would propose competing edits to
  the same files. Making skills per-workspace is a product change, not this bug.
- **Hygiene's subject is one shared artifact.** `ciao vault-index --write`
  regenerates a single `memory-vault/INDEX.md`; `scan_vault`
  (`ciao/vault_index.py:228`) rglobs the whole vault, and `_workspace_of`
  (`:106-114`) derives the workspace from the path prefix. `os-audit` already
  covers every workspace in one pass for the files that are per-workspace —
  `MEMORY.md` and the proposal queues, via `_per_workspace_vault_paths`
  (`ciao/os_audit.py:247`). Fanning out would rebuild the same index N times and
  emit N near-identical reports.

Reconciliation (step 8) is global by nature: it compares vaults against each
other.


## Plan

### 0. Blocker: the bounded memory regions are global

Fan-out cannot ship before this is decided. There is exactly **one** `CLAUDE.md`:
`memory_status` and `memory_update` both resolve
`config.workspace_root / "CLAUDE.md"` (`ciao/control_plane.py:359,385`) and
discard the principal's workspace when doing so. `ciao/memory_injector.py` does
no workspace filtering either — the same `ciao:memory` / `ciao:profile` regions
are injected into every session in every workspace.

The curation prompt explicitly tells the agent to *"promote only cross-project
facts into the bounded `ciao:memory` or `ciao:profile` regions of the workspace
`CLAUDE.md`"* and to *"consolidate before adding when a region is near its
cap."* Fanning it out naively means:

- **Two runs editing one shared region**, both at `00:01` UTC. `_guide_lock`
  (`ciao/memory_tool.py:402`) prevents corruption, but not two independent
  consolidation passes fighting over the same cap.
- **A cross-workspace leak.** A work fact promoted by the work run lands in a
  region injected into every personal session — the exact disclosure that
  `ciao/context/entity_tagger.py:205-215` fails closed on for vault entities.

Options:

- **A. Per-workspace bounded regions.** `memory_status`/`memory_update` resolve
  the guide from the principal's workspace, and the injector picks the active
  one. Correct long-term, and the tool surface already carries the workspace.
  Touches the control plane, injector, `os_audit.audit_memory`
  (`ciao/os_audit.py:480`), and `ciao memory-audit` (`ciao/cli.py:1337`), all of
  which assume one guide.
- **B. Keep regions global; forbid fanned-out runs from writing them.** Strip the
  bounded-region clause from the per-workspace curation prompt and leave region
  promotion to one global pass. Small change, ships with the fan-out, but keeps
  the shared-region model — meaning a genuinely work-only preference has nowhere
  workspace-scoped to live.
- **C. Stagger only.** Give each fanned-out run a distinct `daily_time_utc`.
  Removes the concurrency, not the leak. Not sufficient alone.

**Recommend B now, A as a follow-up** — B unblocks the misfiled-people fix
without a control-plane refactor, and A is a coherent piece of work worth doing
deliberately rather than as fan-out collateral. Stagger the times regardless
(C is cheap and the runs are independent).


### 1. Fan out per-workspace system routines in the schedule store

The obvious approach — "have the setup wizard write one curation schedule per
workspace" — **does not work**, and the reason constrains the whole design:

- System schedules are not per-install data. They are loaded from the packaged
  `ciao/stock/schedules.json` on every read (`ciao/schedules.py:430`
  `_load_system_definitions`), with a per-schedule overlay limited to
  `SYSTEM_STATE_FIELDS` (`ciao/schedules.py:32-38`:
  `enabled`, `last_triggered_on`, `last_dispatched_at`, `last_run_chat_id`,
  `workspace`) stored in `system_schedules_state.json`.
- `ScheduleStore.list_entries` **skips any runtime item with
  `scope == "system"`** (`ciao/schedules.py:281-282`), and `replace`/`delete`
  refuse system entries (`:348-350`, `:363-366`). A wizard-written system row in
  `schedules.json` would be silently dropped on the next read.

So the fan-out belongs in `_system_entries()` (`ciao/schedules.py:416-428`):

- Mark per-workspace routines in the stock definition, e.g. a new
  `"per_workspace": true` field on `system-memory-curation` (stripped by
  `_entry_from_item`'s known-field filter, so read it from the raw dict before
  constructing the entry).
- For a marked definition, emit one entry per registered workspace with a
  derived id — `<base-id>@<workspace>` — and `workspace` set to that name.
  Unmarked definitions keep their single literal id.
- Key the overlay by the derived id, so per-workspace `enabled` and
  `last_triggered_on` are independent. A workspace that is renamed or removed
  simply stops producing an entry; its stale overlay key is inert.

`ScheduleStore` needs the workspace list, which it does not have today (it takes
only `runtime_root`). Pass a `workspace_names: Callable[[], list[str]]` (or the
config) into `__init__` and default it to a single-element list so existing
callers and tests keep working.

**Call sites that assume one literal id per system routine** — each needs to
match on the base id, not the full id:

- `ciao/job_runs.py:196,202,219` — `JobSpec.schedule_id`, consumed as an exact
  membership test (`:702-706` `spec.schedule_id not in installed_schedules`).
  With derived ids the `skill_evolution` row would be silently hidden. Compare
  on the base id.
- `web/src/components/settings/SettingsAutomation.vue:149-151` — the job-step →
  schedule_id map. Needs to resolve a base id to whichever fanned-out row is
  relevant (or link to the routine list instead of one row).
- `tests/test_stock_package.py:23-25` and
  `tests/test_schedules.py:115-143,880-891` assert the literal ids.

### 2. Migrate the persisted `"default"` overlay

Fixing `ciao/stock/schedules.json` alone **does not fix existing installs.**
`workspace` is in `SYSTEM_STATE_FIELDS`, and `_replace_system_state`
(`ciao/schedules.py:461-467`) writes *every* field in that set on any save — so
the sentinel gets copied from the stock definition into the overlay the first
time anything about the routine changes. This install's
`.runtime/system_schedules_state.json` already holds
`"workspace": "default"` for all three routines, and the overlay wins over the
stock definition.

Required: when loading the overlay, drop or normalize a `workspace` value that
is not a currently registered workspace, instead of letting it shadow the
definition. That is also the general repair for a workspace the user has since
renamed.

### 3. Resolve, don't fail, on an unknown workspace

The original draft proposed making `_schedule_workspace_hint` fail loudly and
skip the run. Don't:

- "Skip" means the routine silently stops firing — strictly worse than curating
  one vault.
- The current behaviour is deliberate and tested:
  `tests/test_schedule_workspace_routing.py:108-152`
  `test_system_schedule_default_inherits_first_workspace_routing` asserts that a
  `"default"` system schedule inherits the first workspace's routing.

Instead:

- Keep a resolving fallback, but route it through `config.primary_workspace()`
  (`ciao/config.py:455-467`), whose docstring already states that callers must
  not hardcode `"personal"`. That deletes the `sched-work`-prefix heuristic's
  reach into system routines while preserving legacy behaviour for old
  `sched-work*` user entries.
- Log once at WARNING when a schedule carries an unresolvable workspace, so a
  misconfigured entry is visible without breaking dispatch.
- Update the test above to describe the intended fallback rather than deleting
  it.

After step 1, curation never takes this path — every fanned-out entry carries a
real workspace. The fallback only covers the global routines and legacy entries.

### 4. Decide what happens to the "Run this routine in" control

`web/src/components/SchedulePanel.vue:289-298` is a single-select that writes the
`workspace` overlay for a system routine. Once curation is fanned out, that
control is contradictory for it (there is one row per workspace already). Pick:

- **Per-workspace rows carry no select** — the workspace is part of the row's
  identity; the select stays only for the global routines (hygiene,
  skill-evolution), where it still meaningfully picks the provider/model and
  proposal-queue owner. *Recommended.*
- Alternatively keep the select and add a per-workspace enable toggle — more UI
  for no extra capability.

Also confirm `scheduleInWorkspace` (`web/src/lib/automationWorkspace.ts:3-8`)
still groups these correctly: it compares `schedule.workspace` exactly, and
`_enrich_schedule` supplies the effective value, so fanned-out rows land in the
right workspace panel without frontend changes.

### 5. Update the memory agent routing guidance

`ciao/stock/agents/memory.md` currently mentions no workspace routing for
people. State the rule explicitly: a person belongs in the **active workspace's**
`People/`, and a person who genuinely spans workspaces belongs in
the workspace they deal with them in. Without this, a correctly-targeted curator still has no reason
to prefer the right folder.

### 6. Route skill-evolution output to the workspace that produced the evidence

Same class of bug as curation, different mechanism — and it is live today,
independent of any fan-out:

- **Inputs leak across workspaces.** Trajectories record their owning workspace
  (`ciao/trajectory_builder.py:318`), but `list_trajectories`
  (`ciao/trajectory_builder.py:411-433`) has **no workspace filter** — it globs
  `*/*.json` and returns every workspace's sessions.
- **Output lands in one workspace.** `_resolve_proposals_dir`
  (`ciao/skill_evolution.py:96-107`) writes to
  `<workspace-vault>/Workspace/Skill-Proposals`, choosing
  `CIAO_ACTIVE_WORKSPACE` or falling back to `config.primary_workspace()`.

So work-session evidence yields skill proposals in the personal queue, and the
proposal text can quote work session content into the personal vault.

Do **not** fan this out — the skill catalog is global (see the taxonomy), so N
runs would propose competing edits to the same files. Instead:

- Add a `workspace` filter to `list_trajectories`, consistent with the existing
  `month`/`since`/`skill` filters.
- Group the run's findings by the owning workspace and write each group to that
  workspace's `Skill-Proposals` queue in a single pass.
- A proposal whose evidence spans workspaces should say so and go to one queue
  by an explicit rule, not by whichever workspace happened to be primary.

The routine stays a single global entry with a real workspace for its
provider/model.

### 7. Scope the hygiene report to the workspace it is reported in

`system-workspace-hygiene` runs once and reports into one chat, but its findings
cover every workspace: `_per_workspace_vault_paths` (`ciao/os_audit.py:247`)
collects `MEMORY.md` and proposal-queue paths from all of them. So work-vault
findings surface in a personal chat. Fan-out is the wrong fix (one shared
`INDEX.md`); scope the *output* instead.

Blocked on a missing capability: `os-audit --workspace` takes a **filesystem
path** (`ciao/cli.py:1999-2003`), the workspace root — there is no way to scope a
run to the logical workspace `work`. Adding a logical-workspace filter is the
prerequisite.

Then either group the report into per-workspace sections in one run (cheap,
keeps one index rebuild), or split the routine in two: a global index rebuild
plus a per-workspace audit. Note the weekly index rebuild is already partly
redundant — `vault_index` also runs on server startup
(`ciao/job_runs.py:216-219`), which makes the split cheaper than it looks.

While here: `_per_workspace_vault_paths` iterates **every child directory** of
the vault root, so `Logs/` and `Templates/` are treated as workspaces. Harmless
today (the candidate files don't exist), but it should read
`config.workspace_names()` / `workspace_vault_root` like
`ciao/web/agent_assets.py:456-459` already does.

### 8. Re-home the already-misfiled people

**`shared/` is removed, and that simplifies this step to one operation.** The
draft specified a three-way reconciliation: dedupe people present in both vaults,
promote genuinely cross-workspace entities to `shared/`, and re-home mis-filed
ones. Measured on a real two-workspace vault, the first two branches have no
subjects:

- **Zero people exist in both trees.** Eight note names appear in both, and all
  eight are structural (`README`, `log`, `general`, `Memory-Proposals`, and four
  dated weekly-review files). There is nothing to dedupe.
- **All 70 cross-workspace references are symptoms, not requirements.** 47
  personal→work edges are `personal/People/<work colleague>.md` pointing at
  `work/projects/…`; 23 work→personal edges are work specs citing
  `personal/People/<colleague>`. Re-home those people and every one of those
  edges becomes workspace-internal.
- **Tag overlap is 14 of 186**, and it is technology topics (`claude-code`,
  `mcp`, `skills`) plus work terms that leaked in alongside the misfiled people.
  Shared vocabulary is a `VOCABULARY.md` concern, not shared storage.

So the `shared/` visibility branch was deleted from
`ciao/context/entity_tagger.py` rather than completed: nothing could read such a
note anyway (every workspace-scoped tool roots at `<vault>/<workspace>` and
`_safe_relative` rejects anything outside it), and no real case wanted one. That
also removes this step's security-sensitive prerequisite.

What remains is a **one-way move with a review queue**:

| Bucket | Count on the reference vault | Action |
|---|---|---|
| Work signal only | **77** | Move to `work/People/`. The tags already say so — `scandit` (77), `colleague` (42), `customer` (4), `partner` (1), `work` (1). |
| Tags naming **both** work and personal | **1** (`Oliver`: colleague + friend + scandit) | Withheld for review — never auto-moved. |
| Personal signal only | **8** (`family` ×5, `friend` ×3) | Correctly stays in `personal/People/`. |
| No signal at all | **9** | Genuine judgement — propose, never guess. |

An earlier draft of this table said 42/53. That was a measurement error: the
counter missed `scandit` on notes using the YAML block-list form, so it reported
only the `colleague` count. Re-measured with a real YAML parse, the mechanical
set is 77 — the folder genuinely is mostly work contacts, which is the bug
restated rather than an over-broad rule. Note also `Mo` carries `ex-colleague`,
a tag no signal set anticipated; it lands in review because `friend` is present,
which is the safe outcome by construction rather than by enumeration.

Requirements unchanged from the draft: cross-workspace **read** access to compare
the two trees, weekly rather than daily, and **never auto-apply** — findings go
to a review queue mirroring the `Memory-Proposals.md` promote/dismiss pattern
(`ciao/memory_proposals.py`, resolved via `memory_proposal_resolve`). Moving a
note also has to fix the links that point at it, which the OKF link work makes
mechanical.

This routine is *global*, not per-workspace — it compares vaults against each
other, so do not mark it `per_workspace`.

## Hardcoded `personal`/`work` fallbacks (audit, don't blanket-remove)

The built-in `personal`/`work` pair (`ciao/config.py:217` `_legacy_workspaces`)
is a **fallback only** — fresh installs get a user-named registry from the
wizard (`ciao/cli.py:785`). Removing the seed is a large, risky refactor
touching ~15 sites, most of them legitimate legacy compat. Only the routing-bug
sites are actionable here.

| Location | Hardcoded fallback | Action |
|---|---|---|
| `ciao/web/project_chats.py:4630-4632` | schedule → `"personal"` heuristic | **Fix** (step 3): use `config.primary_workspace()` |
| `ciao/web/project_chats.py:1682` | `_workspace_names()` → `("personal","work")` | keep (legacy) |
| `ciao/web/project_chats.py:1764,1782,1942-43,2162` | `"personal"` | keep (legacy) |
| `ciao/web/routes_api.py:180,5679` | `{"personal","work"}` / `or "personal"` | keep (legacy) |
| `ciao/web/agent_assets.py:447` | `("personal","work")` | keep (legacy) |
| `ciao/vault_index.py:81,110,113` | `"personal"` | keep (legacy) |
| `ciao/cli.py:707` | `name or "personal"` | keep (legacy) |
| `ciao/gws_auth.py:47,111-114,122,135` | `BUILTIN_PROFILES` | keep (legacy) |
| `ciao/config.py:465` | `primary_workspace()` prefers `"personal"` | keep — documented, and step 3 depends on it |
| `ciao/config.py:223,368,950` | `gws_default_profile="personal"` | keep (legacy) |
| `ciao/providers/base.py:22-24`, `ciao/observability/hooks.py:88-96` | `{"personal","work"}` | keep — deliberate: `CIAO_WORKSPACE` is a filesystem path now, and these preserve only the two legacy *context* values |
| `ciao/evals.py:769,1203` | `workspace_name="personal"` | keep (legacy) |

## Status

| Step | State |
|---|---|
| 0. Bounded-region blocker | **done, option B** — the curation prompt no longer writes `ciao:memory`/`ciao:profile`; cross-project facts stay queued for the user. Option A (per-workspace guides) remains a deliberate follow-up. Times are staggered too (option C). |
| 1. Fan out in `_system_entries` | **done** — `per_workspace` marker on the definition, `<base>@<workspace>` ids, `system_base_id` for consumers, 7-minute stagger. |
| 2. Overlay migration | **done** — `_normalized_overlay` drops an unresolvable persisted workspace, so the live `"default"` self-heals on read. No separate migration needed. |
| 3. Resolve-don't-fail fallback | **done** — `_schedule_workspace_hint` routes through `config.primary_workspace()` and logs the mismatch. |
| 4. UI | **done** — a fanned-out row shows explanatory text instead of the workspace select; "Run now" resolves the base id to a real schedule (it would otherwise 404). |
| 5. Agent routing guidance | **done** — people route to the active workspace's `People/`; there is no shared people folder. |
| 6. Skill-evolution output routing | **done** — `list_trajectories(workspace=…)`, one pass per workspace writing to that workspace's queue. The "cross-workspace evidence to one queue by rule" refinement is not implemented: each workspace gets its own proposal. |
| 7. Hygiene report scoping | **done** — split into a global `system-vault-index` (one shared `INDEX.md` + `VOCABULARY.md`) and a `per_workspace` `system-workspace-hygiene`. `os-audit` gained `--workspace-name`, defaulting from `CIAO_ACTIVE_WORKSPACE` so the static packaged prompt needs no templating. |
| 8. Re-home misfiled people | **done** — `ciao/vault_rehome.py`, `ciao vault-rehome` (dry-run default) + inverse, review queue for judgement cases. Not yet applied to the reference vault. Previously: — but no longer blocked: `shared/` is deleted rather than completed, so the security-sensitive second-root work is gone. 42 tag-obvious moves plus 53 needing judgement. |
| `shared/` mechanism | **removed** — the visibility branch, its tests, the agent guidance and the curation prompt. Measured overlap did not justify it; see step 8. |
| Queued facts surfaced | **done** — the curation prompt now lists every cross-project fact it left queued, numbered, so a reply can name which to promote. |

## Order

1. **Bounded-region decision** (step 0) — a blocker for fan-out, and cheap under
   option B.
2. **Overlay migration** (step 2) — without it nothing else takes effect on an
   existing install.
3. **Resolve-don't-fail fallback** (step 3) — small, independent.
4. **Fan out curation per workspace** (step 1) + **UI decision** (step 4) +
   **agent routing guidance** (step 5) — fixes the observed bug. Stagger the
   fanned-out times.
5. **Skill-evolution output routing** (step 6) — independent of the fan-out and
   fixes a live leak; can ship in parallel.
6. **Hygiene report scoping** (step 7) — needs the logical-workspace filter on
   `os-audit` first.
7. **Reconciliation pass** (step 8) — the cross-workspace cleanup of the ~57
   already-misfiled notes. No longer needs `shared/`, which was removed.

Steps 2, 3, and 6 are independent of each other and of the fan-out. Only step 4
of this list needs step 0 resolved.

## Tests to add

- `_system_entries` emits one curation entry per registered workspace and
  exactly one hygiene/skill-evolution entry.
- Fanned-out curation entries carry distinct `daily_time_utc` values.
- A per-workspace overlay (`enabled: false` on one workspace's curation row)
  does not affect the others.
- An overlay carrying a stale/unregistered `workspace` is normalized rather than
  shadowing the definition.
- `job_runs` still shows the `skill_evolution` row when installed schedule ids
  are derived.
- Adding a workspace produces its curation row without a restart-only migration.
- `list_trajectories(workspace="work")` returns only work trajectories, and a
  skill-evolution run writes each group to its own workspace's queue.
- Under option A only: `memory_update` from a work principal does not touch the
  personal guide.

## Open questions

- Step 0: option B now and A later, or A directly? A is the right end state but
  touches the control plane, injector, `audit_memory`, and `memory-audit`.
- Should the re-homing queue be per-workspace or one file? Per-workspace mirrors
  the existing proposals pattern and reuses `memory_proposal_resolve`; one file is
  simpler for a job that compares vaults against each other.
- Should reconciliation route by tag automatically or always ask? Recommend:
  propose by tag, user confirms.
- Should the fan-out be keyed on registered workspaces only, or on workspaces
  that actually have a vault with content? A freshly-added empty workspace would
  otherwise get a nightly no-op run. Recommend: register all, and let the
  routine's own "reply with a one-line no-op and stop" clause handle emptiness.
- Should skills themselves become per-workspace? Out of scope here — the catalog
  is deliberately global at the workspace root. Worth its own note if you want a
  work-only skill set.
- A cross-workspace skill proposal (evidence from two workspaces): one queue by
  rule, or duplicated into both?
