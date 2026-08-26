# Skills surface simplification — remove GitHub install & auto-update, keep folder/zip add

## Resume block

- Status: proposed
- Current checkpoint: C0
- Next action: Review plan and HTML companion, decide on zip validation scope and migration message
- Blocker: none
- Note (2026-08-25): still unimplemented — `ciao/skills_sync.py`, `scripts/skills_add.py`, and the `auto_update_github_skills` config/routes surface are all present. Written before the Codex provider removal (`9ab168ee`); where it mentions Codex projection in `sync_skills.py`, read that as the surviving claude/opencode projection.
- Implementation repository: raffaelefarinaro/ciaobot
- Generated plan output: docs/plans/SKILLS_GITHUB_SIMPLIFICATION_PLAN.md
- Visual companions: docs/plans/SKILLS_GITHUB_SIMPLIFICATION_PLAN.html (side-by-side Settings before/after)
- Verified on: 2026-08-23 against ciao/sync_skills.py:319, ciao/skills_sync.py:1, ciao/skills_inventory.py:15, ciao/config.py:477, ciao/web/routes_api.py:981/6686, web/src/components/SettingsView.vue:1481, scripts/skills_add.py:1, ciao/stock/skills (29), tests/test_sync_skills.py:340

## Outcome and user value

Remove the entire GitHub/package skill surface ( `npx skills add/update`, `skills-lock.json`, `skills_sync.py`, `CIAO_AUTO_UPDATE_GITHUB_SKILLS`, Settings GitHub section) and replace it with a single, local-folder truth: a skill is a folder `skills/<name>/SKILL.md` (or `skills/<name>.zip` → validated unzip). The agent learns one workflow — copy/extract into `skills/`, then `ciao sync-skills` (already the source of truth for Claude/Codex/opencode projection) — and workspace-git sync between operators replaces per-machine GitHub fetches. Users get a smaller, predictable Settings page, no `npx`/`git ls-remote` at boot, and a validated zip upload that guarantees discoverability.

## Scope and non-goals

In scope:

- Delete GitHub install path: `scripts/skills_add.py`, `ciao/skills_sync.py`, `skills-lock.json` handling in `ciao/sync_skills.py` (`_refresh_upstream_skills`, `_update_upstream_skills`, `_restore_missing_upstream_skills`, `_active_upstream_lock`, `_installed_upstream_names`, `_remove_upstream_skill`), `ciao/web/routes_api.py` `POST /api/admin/skills/add` + auto-update toggle, `web/src/components/SettingsView.vue` github section + toggle, `ciao/config.py` `auto_update_github_skills`, `ciao/cli.py` `--skip` help, `INTEGRATIONS.md` env var, `skills_inventory.py` github label.
- Keep and clarify local path: `skills/<name>/SKILL.md` → `.claude/skills` + `.agents/skills` via `sync_skills.py:_rebuild_custom_skill_links` (already exists), plus stock skills from `ciao.stock/skills`. No new sync needed.
- Add zip upload: Settings Skills tab `+ Add skill` supports `.zip` (and folder drop where Tauri grants allow). Backend `POST /api/skills/import` validates: zip contains exactly one top-level folder with `SKILL.md`, frontmatter `name`/`description` present, size ≤15KB (`skill_evolution.MAX_SKILL_BYTES`), no path traversal, then extracts to `skills/<name>/`.
- Teach agent: update `ciao/stock/skills/workspace-authoring` and `sop-authoring` (or `ciao-capabilities` catalog line) with one-paragraph rule: "add skill = place folder in `skills/` or upload validated zip, then `ciao sync-skills`; workspace git sync propagates to other operators".
- Migrate existing installs: on first boot after upgrade, if `skills-lock.json` exists, emit one operator action "GitHub skills removed — move folders from `.claude/skills/<name>` to `skills/<name>` manually; see Settings → Skills" and leave lock file untouched for manual recovery; no auto-conversion.

Out of scope for this release:

- Changing stock skills (`ciao.stock/skills`) or custom `skills/` format.
- Rebuilding the entire Settings Skills page beyond removing github section + adding zip button.
- Workspace-git sync itself (already exists via `ciao local_session` / git); this plan only removes the competing GitHub fetch.
- Skill versioning or private registry.

## Current-state evidence

Observed (read):

- `ciao/sync_skills.py:304` `_refresh_upstream_skills` reads `skills-lock.json`, checks `CIAO_AUTO_UPDATE_GITHUB_SKILLS` (default false), then either restores missing or calls `skills_sync.remote_heads` + `plan` + `npx skills update/add` with `SKILLS_NPX_TIMEOUT=180`. Contains 200+ lines of upstream logic.
- `ciao/skills_sync.py:1` pure `plan`/`build_cache` + `git ls-remote` parallel fetch (8 workers, 30s timeout) to decide `to_update`/`to_prune`. Skips fetch when heads unchanged.
- `scripts/skills_add.py:68` `add_skill` runs `npx -y skills add <repo> --skill <name> --agent claude-code -y`, infers `--skill` from `/skills/<name>` URL segment. Adds to `skills-lock.json`.
- `ciao/skills_inventory.py:19` `github` label = comes from `skills-lock.json`; counts `custom/github/stock` shown in Settings. `_read_lock_entries` reads lock file.
- `ciao/config.py:477` `auto_update_github_skills: bool = False` loaded from `CIAO_AUTO_UPDATE_GITHUB_SKILLS`.
- `ciao/web/routes_api.py:981` `auto_update_github_skills` in settings GET/PATCH, `6686` `POST /api/admin/skills/add` runs `scripts/skills_add.py`.
- `web/src/components/SettingsView.vue:1481` `github / package skills` section + toggle `autoUpdateGithubSkills` (lines 1371,3124,3332). Also `web/src/lib/types.ts:637` `auto_update_github_skills`.
- `ciao/stock/skills` 29 packaged skills (observed `ls`). Installed via `_install_stock_skills` with `.ciao-stock-skill` marker, shadowed correctly by `skills/<name>`.
- `tests/test_sync_skills.py:340` stock marker test, `388/443` lock file tests for auto-update on/off.
- `ciao/workspace_reroot.py` treats `skills-lock.json` as a migrated catalog file (the workspace-isolation plan that introduced this is implemented and removed from `docs/plans/`).

Assumed: users who installed GitHub skills have them cached under `.agents/skills` and `.claude/skills` (installed check). Their count is small (<5 per install from trajectories).

## Recommended direction

**Delete, don't abstract.** Remove the 3 GitHub-owned modules and every branch that reads `skills-lock.json` or calls `npx`. Keep the local-folder surface that already works and add one validated entry point (zip).

Concrete:

1. Backend removal: delete `ciao/skills_sync.py`, `scripts/skills_add.py`, `scripts/skills_sync.py` wrapper, and in `ciao/sync_skills.py` remove all upstream helpers (lines 151-366) and the `refresh_upstream` flag from `sync_workspace_skills`. Keep only `_install_stock_skills`, `_rebuild_custom_skill_links`, `mirror_shared_skill_sources`, and Codex/MCP projection. Remove `skills-lock.json` reads from `skills_inventory.py` (github label → never returned; counts only custom+stock).
2. Config/routes: drop `auto_update_github_skills` from `ciao/config.py`, `ciao/web/routes_api.py` settings, `INTEGRATIONS.md`, `PWA_API.md`, and `web/src/lib/types.ts`. Replace `POST /api/admin/skills/add` with `POST /api/skills/import` that accepts `multipart/form-data` zip, validates (zip slip check, exactly one `*/SKILL.md`, frontmatter `name`/`description`, ≤15KB), extracts to `skills/<name>/`, then calls `sync_workspace_skills`.
3. Frontend: in `SettingsView.vue` remove github section (1481-1517) + auto-update toggle, keep `custom` + `stock` inventory. Add `+ Add skill` button with two inputs: `Choose folder` (Tauri drop grant path when available, else `webkitdirectory`) and `Upload zip` (file input). Show validation errors inline; on success toast "Skill <name> added — available to all operators after next sync".
4. Agent teaching: one sentence in `ciao/stock/skills/workspace-authoring/SKILL.md` (and `sop-authoring` if present): "Skills are local folders: place `skills/<name>/SKILL.md` (or validated zip) then `ciao sync-skills`; workspace git sync propagates to other devices. No GitHub fetch."
5. Migration: `ciao/upgrade.py` or first `sync_workspace_skills` run detects `skills-lock.json` exists → log operator action, do not delete. Keep `.agents/skills` cached copies readable via inventory for one release, then user manually moves needed ones.

Why this direction: workspace-git already syncs `skills/` between operators (the user's stated need). GitHub fetch duplicates that with network, `npx`, and `git ls-remote` at boot (180s timeout risk, see `sync_skills.py:59`). Zip validation gives the same "drop a skill" UX without importing arbitrary repo state, and matches the existing `skills/<name>/SKILL.md` contract that all providers already discover.

## Alternatives and rejected options

### Keep GitHub as optional, behind feature flag
Leave `skills-lock.json` + `skills_sync.py` but default off. Rejected: keeps 300+ lines of dead code, tests, and UI for a surface user explicitly wants removed; flag still requires `npx` at restore time and the same boot-time complexity.

### Auto-convert `skills-lock.json` entries to `skills/` folders on upgrade
Download each locked repo's skill folder and copy into `skills/`. Rejected: requires network at upgrade, reintroduces `npx` for one more release, and silently duplicates skills user may have already abandoned. Manual move with one operator action is safer and matches the "remove, don't migrate" ask.

### Keep github skills but move to pure `.agents/skills` without `npx update`
Freeze installed copies and never fetch. Rejected: still ships `skills-lock.json` and github inventory, and the "no auto-update" promise is already the default (`false`). Full removal is cleaner than frozen-but-present.

### Replace zip with GitHub URL paste that clones locally
Accept `https://github.com/owner/repo/tree/main/skills/foo` and `git clone --depth 1` one folder. Rejected: reintroduces git/network at add time, the user asked to remove GitHub entirely, and zip covers the same use case via golder/folder link.

## Visual review

HTML companion `docs/plans/SKILLS_GITHUB_SIMPLIFICATION_PLAN.html` shows side-by-side Settings → Skills before/after: left has `auto-update toggle` + `github / package skills` list + `npx` badge; right has only `custom skills` + `stock skills` + `+ Add skill` (folder/zip) with validation checklist. No Excalidraw needed — relationships are linear (folder → sync → catalog).

## Decisions and hard-to-reverse bets

| ID | Decision | Rationale | Status |
| --- | --- | --- | --- |
| D-01 | Remove `skills-lock.json` + `skills_sync.py` entirely | User wants simpler surface; workspace-git replaces cross-operator sync | Proposed |
| D-02 | No auto-update, no `npx skills` calls | Boot `update_skills` phase hangs on 180s timeout with no benefit when default is false | Proposed |
| D-03 | Skills are `skills/<name>/SKILL.md` only | Already the canonical form; all providers discover it after `sync_skills` | Proposed |
| D-04 | Add zip upload with validation (SKILL.md, frontmatter, 15KB, zip-slip) | Requested; guarantees discoverability without teaching git | Proposed |
| D-05 | Agent rule: "add skill = folder/zip → `ciao sync-skills` → workspace sync" | Matches existing `gws-shared` profile-wrapper rule style; one paragraph | Proposed |
| D-06 | Migration: warn, don't auto-convert | Avoids network at upgrade and silent duplication | Proposed |

## Open questions with recommended defaults

| ID | Question | Recommended default | Status |
| --- | --- | --- | --- |
| Q-01 | Zip validation strictness: require frontmatter `name` equals folder name? | Yes — reject if mismatch, show "folder `foo` must contain `name: foo`" | Accepted 2026-08-23 (user: yes) |
| Q-02 | Allow `.tgz`/`.tar.gz` in addition to `.zip`? | No — zip only for v1; add later if needed | Accepted 2026-08-23 (user: yes, zip only) |
| Q-03 | Keep reading cached `.agents/skills` for one release after removal, or drop immediately? | Drop immediately; inventory shows only `skills/` + stock. Cached `.agents/skills` still works as provider catalog but not as UI source | Accepted 2026-08-23 (user: yes) |
| Q-04 | Show stock skills as read-only in Skills settings, or hide? | Keep as read-only list (already) — no change | Open |

## Not yet specified (fog of war)

- Whether zip should also carry `commands/` or `subagents/` inside the same archive (likely not — keep skills isolated for v1).
- Whether to add a CLI `ciao skills import <zip>` alongside the PWA endpoint (follows naturally from the same validator).

## Feedback and decision log

| ID | Location | Feedback | Decision | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| F-01 | Screenshot 2026-08-23-bigquery-data | Empty proposals shouldn't be proposals | Fixed: under-cap stub no longer written, schedule --min-sessions 2 | implemented | ciao/skill_evolution.py:744, ciao/stock/schedules.json:97 |
| F-02 | Skills surface | Remove GitHub install + autoupdate, keep folder/zip | This plan | open | User message 2026-08-23 |

## Implementation checkpoints

### C0. Start or resume
- Read Resume block, ARCHITECTURE.md:73/82/187, sync_skills.py:319, skills_inventory.py:15.
Exit: plan status matches repo.

### C1. Remove GitHub surface (backend)
- Delete `ciao/skills_sync.py`, `scripts/skills_add.py`, `scripts/skills_sync.py`, `scripts/install-custom-skills.sh` wrapper if redundant.
- In `ciao/sync_skills.py` delete upstream helpers (≈200 lines) and `CIAO_AUTO_UPDATE_GITHUB_SKILLS` branch; keep stock/custom/mirror. Update imports.
- In `ciao/skills_inventory.py` remove `_read_lock_entries`, github counts/label.
- In `ciao/config.py` remove `auto_update_github_skills`, in `ciao/web/routes_api.py` remove settings field + `POST /api/admin/skills/add`, add `POST /api/skills/import`.
- In `ciao/cli.py`, `ciao/upgrade.py`, `ciao/workspace_reroot.py`, `ciao/local_session.py` drop `skills-lock.json` references.
- Update `INTEGRATIONS.md`, `PWA_API.md`, `docs/ARCHITECTURE.md`.

Exit: `rg skills-lock` returns 0 hits outside tests expecting removal; `rg "npx.*skills"` 0 hits.

### C2. Frontend simplification
- In `web/src/components/SettingsView.vue` remove github section + toggle, add `+ Add skill` folder/zip inputs, wire to new endpoint. In `web/src/lib/types.ts` remove field.
- Keep custom/stock lists; no new route.

Exit: Settings → Skills shows only custom + stock, zip error handling works, no github label.

### C3. Zip validation + discoverability
- Implement `ciao/skill_import.py` (or inline in `routes_api.py`) `validate_skill_zip(path)`: zip-slip, single top-level folder, `SKILL.md` exists, frontmatter parse, size check, returns errors list. On success extract to `skills/<name>/` (refuse overwrite unless `--force`), then `sync_workspace_skills`.
- Tests: valid zip, missing SKILL.md, traversal, oversize, name mismatch.

Exit: `pytest tests/test_skill_import.py` new file passes; uploaded zip appears in inventory and is discoverable via `.claude/skills` symlink.

### C4. Agent teaching + migration
- Edit `ciao/stock/skills/workspace-authoring/SKILL.md` + `sop-authoring` one paragraph.
- Add operator action in `ciao/operator_actions.py` for leftover `skills-lock.json` (info, not blocking).
- Write migration note in `docs/DEVELOPMENT.md`.

Exit: agent can answer "how to add skill via golder/folder?" with folder/zip + sync.

### C5. Get approval
- Surface plan + HTML. Name files above.
Exit: user approves or defers Q-01..Q-04.

### C6. Implement
- Work checkpoints C1-C4 sequentially, updating Resume block.

### C7. Verify
- `rg -n "skills-lock|skills_sync|CIAO_AUTO_UPDATE" ciao/ web/` 0 hits (allow stock schedule comment if kept).
- `pytest tests/test_sync_skills.py tests/test_skills_inventory.py tests/test_workspace_reroot.py` — update expectations (no github).
- Manual: Settings Skills add zip → inventory → `.claude/skills` symlink → `ciao sync-skills` → other operator sees file after git sync.

### C8. Close
- Set status complete, record commit, remove legacy `skills-lock.json` from new installs.

## Verification and rollout

- Unit: new `test_skill_import.py` (5 cases), update `test_skills_inventory` to remove github, update `test_sync_skills` to remove upstream refresh tests.
- Integration: `ciao sync-skills` on workspace with only `skills/` + stock, no `npx` invoked.
- Rollout: behind app version bump; existing installs get operator action warning, no auto-delete. New installs never create `skills-lock.json`. Feature is additive (zip) not destructive until C1 merges.
