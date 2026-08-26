# Route Google Workspace auth through the installed `ciao` CLI

## Resume block

- Status: complete
- Current checkpoint: C8 (closed)
- Next action: none
- Blocker: none
- Implementation repository: this repository (`ciaobot`)
- Implemented 2026-08-26: new `ciao/gws_wrapper.py` (`ciao gws`) and `ciao/gws_auth_helper.py` (`ciao gws-auth-helper`); `auth_status` runs `gws auth status` directly (dropped `wrapper_path`); Settings hints + `types.ts`; `gws_skills.py` rewrite rules + regenerated stock `gws-*` skills from `gws` 0.22.5; `system_prompt.md`/`secretary.md`; repo scripts are thin shims; docs + tests. Verified: focused tests pass, full `pytest tests/` 3111 passed, `cd web && npm run build` succeeded, `ciao gws auth status` runs without the wrapper.
- Generated plan output: `docs/plans/GWS_CLI_WRAPPER_PLAN.md`
- Visual companions: none (no spatial/UI-state question to answer)
- Verified on: 2026-08-26 against `scripts/gws-profile.sh`, `scripts/gws-auth-helper.py`, `ciao/gws_auth.py`, `ciao/gws_skills.py`, `ciao/cli.py`, `ciao/config.py`, `ciao/web/routes_api.py`, `web/src/components/SettingsView.vue`, `ciao/system_prompt.md`, `ciao/stock/agents/secretary.md`, `INTEGRATIONS.md`, `docs/ARCHITECTURE.md`, `desktop/src-tauri/src/service.rs`, `scripts/build-bundled-runtime.sh`, `scripts/install.sh`, `tests/test_gws_auth.py`, `tests/test_gws_skills.py`, `tests/test_core_prompt.py`, `tests/test_workspace_settings_routes.py`

## Outcome and user value

Today the Google Workspace integration depends on two files that only exist in a git checkout of the repo: `scripts/gws-profile.sh` (the profile wrapper) and `scripts/gws-auth-helper.py` (headless re-auth). Someone who installed Ciaobot.app has neither, so:

- the Settings → Workspaces card shows terminal commands (`scripts/gws-profile.sh <profile> auth setup --login`, `python3 scripts/gws-auth-helper.py <profile>`) that cannot run;
- the `gws-*` skills tell the agent to run `scripts/gws-profile.sh "$GWS_PROFILE" ...`, which fails in an installed workspace;
- the token-health monitor silently degrades to `available: false, reason: "wrapper script not found"`.

The fix routes every one of these through the `ciao` CLI that ships inside the installed app (`.../ciao-runtime/bin/ciao`), so the same command works on a dev checkout and on an installed app. The repo scripts become thin shims so nothing external breaks.

## Scope and non-goals

In scope:

- New `ciao gws` subcommand: profile-aware passthrough to the `gws` CLI (replaces the bash wrapper's logic).
- New `ciao gws-auth-helper` subcommand: headless OAuth re-auth moved into the package (replaces `scripts/gws-auth-helper.py`).
- `ciao.gws_auth.auth_status` runs `gws auth status` directly with the computed env (no bash wrapper dependency).
- Update Settings hints (`routes_api.py` payload + `SettingsView.vue` hardcoded hint) to the new commands.
- Update `gws_skills.py` curation/rewrite rules and regenerate the stock `gws-*` skills; update `system_prompt.md` and `ciao/stock/agents/secretary.md`.
- Keep `scripts/gws-profile.sh` and `scripts/gws-auth-helper.py` as thin shims forwarding to the new subcommands.
- Update docs (`INTEGRATIONS.md`, `docs/ARCHITECTURE.md`) and tests.

Out of scope for this release:

- Adding `ciao` to the user's shell PATH automatically (installer symlink). The UI will show `ciao gws ...`; setup already prints an `export PATH=...` hint. Revisit only if users report friction.
- Changing the PWA-native OAuth flow (upload `client_secret.json` → browser exchange). It already works on installed apps and is untouched.
- Renaming the `gws` CLI or its service names.

## Current-state evidence

Observed (read the files):

- `scripts/gws-profile.sh` (60 lines): resolves profile (positional first-arg disambiguation against `GWS_SERVICE_NAMES`, else `GWS_PROFILE`), sets `GOOGLE_WORKSPACE_CLI_CONFIG_DIR=<root>/secrets/gws[-<slug>]` (legacy `personal`→`gws-personal`, `work`→`gws`), unsets `GOOGLE_APPLICATION_CREDENTIALS`, `exec gws "$@"`. Lives only in the repo.
- `scripts/gws-auth-helper.py` (316 lines): interactive headless OAuth re-auth. Duplicates logic already in `ciao/gws_auth.py` (`FULL_SCOPES`/`_PERSONAL_SCOPES`, `exchange_code`, `save_credentials`, `extract_email_from_id_token`, `profile_config_dir`). `ciao/gws_auth.py:52-53` already flags the duplication.
- `ciao/gws_auth.py:103-118` `profile_config_dir(config, profile)` is the single source of truth for the credential dir mapping; `:140-142` `slugify_profile`; `:76-89` `GWS_SERVICE_NAMES`.
- `ciao/gws_auth.py:538-592` `wrapper_path()` + `auth_status()`: runs `bash <workspace>/scripts/gws-profile.sh <profile> auth status`; returns `available: False, reason: "wrapper script not found"` when the file is absent.
- `ciao/web/routes_api.py:1404-1409,1476-1477`: builds `setup_command = "scripts/gws-profile.sh {profile} auth login --full"` and `headless_auth_command = "python3 scripts/gws-auth-helper.py {profile}"`; exposes `wrapper_available`/`helper_available`/`wrapper_path`/`headless_helper_path`.
- `web/src/components/SettingsView.vue:1207-1213` renders `setup_command`/`headless_auth_command`; `:1223` hardcodes `scripts/gws-profile.sh {{ profile.name }} auth setup`.
- `ciao/gws_skills.py:47-63` `_CIAOBOT_SHARED_HEAD` and `:143-169` `rewrite_gws_commands` emit `scripts/gws-profile.sh "$GWS_PROFILE" ...`; `curate_gws_skill` applies them. Stock skills under `ciao/stock/skills/gws-*/SKILL.md` are generated + curated.
- `ciao/system_prompt.md:25` and `ciao/stock/agents/secretary.md` reference `scripts/gws-profile.sh`.
- `INTEGRATIONS.md:88-98,331` and `docs/ARCHITECTURE.md:149-150` document the repo scripts.
- `ciao/cli.py:2876` `build_parser()` adds subcommands; `:3862` `main()` dispatches via `args.func`. `ciao/config.py:1259` `CiaoConfig.from_env()` loads config from env/`.env`.
- Bundled engine: `~/Applications/Ciaobot.app/Contents/Resources/ciao-runtime/bin/ciao`; the launcher (`scripts/build-bundled-runtime.sh:157`) exports `PATH="$root/bin:$PATH"`, so agent-spawned shells resolve `ciao`. `desktop/src-tauri/src/service.rs:37-54` `resolve_ciao` resolves the bundled engine.
- `gws` CLI 0.22.5 installed at `/opt/homebrew/bin/gws` (needed to regenerate stock skills).
- Tests referencing the wrapper: `tests/test_gws_auth.py:219-267`, `tests/test_gws_skills.py:160`, `tests/test_core_prompt.py:28`, `tests/test_workspace_settings_routes.py:535,573`.

Assumed (not yet verified):

- Installed users do not have `ciao` on PATH in their own terminal by default; setup prints an `export PATH=...` hint (`ciao/cli.py:256-276,293-296`). Agent-spawned shells do get it via the launcher PATH export.
- The signed updater replaces the bundle in place, so the engine path is stable across updates.

## Recommended direction

Add two `ciao` subcommands and route every GWS terminal path through them.

1. **`ciao gws`** — passthrough that replicates the bash wrapper in Python, reusing `ciao.gws_auth`:
   - Resolve profile: first positional arg that is not a service name and does not start with `-` (mirror `GWS_SERVICE_NAMES` disambiguation), else `GWS_PROFILE` env, else the workspace's default profile.
   - Compute `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` via `gws_auth.profile_config_dir(config, profile)`; unset `GOOGLE_APPLICATION_CREDENTIALS`.
   - `os.execvp("gws", ...)` with the remaining args; friendly error when `gws` is not on PATH (point to Settings → Workspaces install button).
   - New module `ciao/gws_wrapper.py` with a `main(argv)`; registered in `build_parser()`.

2. **`ciao gws-auth-helper`** — move `scripts/gws-auth-helper.py` into the package as `ciao/gws_auth_helper.py`, reusing `ciao.gws_auth` (drop the duplicated `FULL_SCOPES`/exchange/save code). Registered in `build_parser()`.

3. **`gws_auth.auth_status`** — stop shelling out to the bash wrapper. Compute the env in-process and run `[gws_bin, "auth", "status"]` directly (it already resolves `gws` via `tool_path.resolve_tool`). Remove `wrapper_path()` and its uses.

4. **Settings hints** — `routes_api.py`:
   - `setup_command` → `ciao gws {profile} auth login --full`
   - `headless_auth_command` → `ciao gws-auth-helper {profile}`
   - Replace `wrapper_available`/`helper_available`/`wrapper_path`/`headless_helper_path` with a single `cli_available` (whether `gws` is on PATH) or drop them if the UI does not use them (verify; `SettingsView.vue` only reads `setup_command`/`headless_auth_command`).
   - `SettingsView.vue:1223` hardcoded hint → `ciao gws {{ profile.name }} auth setup`.

5. **Skills** — update `gws_skills.py`:
   - `_CIAOBOT_SHARED_HEAD` prose → `ciao gws "$GWS_PROFILE" <service> <subcommand> [flags]`.
   - `rewrite_gws_commands` → prefix `ciao gws "$GWS_PROFILE" ` instead of `scripts/gws-profile.sh "$GWS_PROFILE" `.
   - Regenerate the stock `gws-*` skills with the installed `gws` 0.22.5 (release flow `ciao release` does this; run the same regeneration here).
   - Update `ciao/system_prompt.md:25` and `ciao/stock/agents/secretary.md`.

6. **Shims** — rewrite `scripts/gws-profile.sh` to `exec ciao gws "$@"` and `scripts/gws-auth-helper.py` to `exec ciao gws-auth-helper "$@"` (or `python -m ciao.cli ...`), so dev checkouts and any external references keep working.

7. **Docs + tests** — update `INTEGRATIONS.md`, `docs/ARCHITECTURE.md:149-150`, and the affected tests; add tests for the new subcommands (profile resolution, env computation, exec) and for `auth_status` via direct `gws` invocation.

## Alternatives and rejected options

### Seed `scripts/gws-profile.sh` into the workspace during setup

Copy the wrapper + helper into `<workspace>/scripts/` at setup time, keeping the existing relative-path hints and skills unchanged.

Rejected: it leaves two sources of truth (repo scripts + seeded copies) that drift, does not fix the agent-side skills on already-configured workspaces without a re-seed, and the user explicitly asked for the CLI-based approach ("with the ciao cli that gets installed automatically"). The CLI is the single canonical entry point.

### Keep the bash wrapper and only change the hints

Rejected: the wrapper still only exists in the repo; installed users would still be shown a command that cannot run.

### Add a `ciao` PATH symlink in the installer

Rejected for this release: writing to `/usr/local/bin` may need sudo and `~/.local/bin` may not be on PATH; setup already prints an `export PATH=...` hint. Deferred as a follow-up if users report friction.

## Visual review

Not applicable. This is a CLI/plumbing change with no spatial or UI-state question to answer; the Settings card text change is a one-line string swap.

## Decisions and hard-to-reverse bets

| ID | Decision | Rationale | Status |
| --- | --- | --- | --- |
| D-01 | Add `ciao gws` and `ciao gws-auth-helper` subcommands as the canonical GWS terminal entry points | The bundled engine ships with every install; one entry point works on dev and installed apps | Proposed |
| D-02 | `auth_status` runs `gws auth status` directly with in-process env, dropping the bash wrapper | Removes the filesystem dependency; `gws` is already resolved via `tool_path` | Proposed |
| D-03 | Keep `scripts/gws-profile.sh` and `scripts/gws-auth-helper.py` as thin shims | Back-compat for dev checkouts and any external references; cheap | Proposed |
| D-04 | Regenerate stock `gws-*` skills from the installed `gws` 0.22.5 | Keeps the generated files consistent with the new rewrite rules | Proposed |
| D-05 | UI shows `ciao gws ...`; no installer PATH symlink this release | Consistent with existing CLI docs; setup already prints a PATH hint | Proposed |

## Open questions with recommended defaults

| ID | Question | Recommended default | Status |
| --- | --- | --- | --- |
| Q-01 | Should `ciao gws` also accept `--profile <name>` in addition to the positional form? | Yes — add `--profile` for clarity; keep positional for skill compatibility | Open |
| Q-02 | Does the Settings UI use `wrapper_available`/`helper_available`/`wrapper_path`/`headless_helper_path` anywhere? | Verify; if unused, replace with a single `cli_available` flag | Open |
| Q-03 | Should the repo shims forward via `ciao` (PATH) or via the absolute engine path? | `ciao` on PATH for dev checkouts; document the absolute path for installed users | Open |

## Not yet specified (fog of war)

- Whether the PWA Settings card should offer a "copy command" button with the absolute engine path for installed users who lack `ciao` on PATH. Not phraseable until Q-03 is resolved and the UI text is finalized.
- Whether the `gws-shared` skill's "Install gws from Settings → Workspaces" note needs a companion note about `ciao` being on PATH. Likely, but depends on the final hint wording.

## Feedback and decision log

| ID | Location | Feedback | Decision | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| F-01 | chat | "for someone that installed ciaobot, where is this?" (the wrapper) | Root cause: wrapper only exists in a git checkout; route through the installed `ciao` CLI | accepted | this plan |
| F-02 | chat | "ideally we can do this with the ciao cli that gets installed automatically otherwise it makes no sense" | Adopt the `ciao gws` / `ciao gws-auth-helper` subcommand approach | accepted | D-01 |

## Implementation checkpoints

Each checkpoint has an exit condition so another model can tell whether the work is actually ready to move on.

### C0. Start or resume

- Read the Resume block and current checkpoint.
- Read the project canonical document (`docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`) and the live files named by the plan.
- Confirm the plan's status still matches the repository.

Exit evidence: the plan records the next concrete action and any blocker.

### C1. Ground the plan

- Name the actual files, symbols, routes, components, and data shapes involved (done above).
- Separate observed facts from assumptions (done above).

Exit evidence: a reader can verify the current-state claims without relying on chat history.

### C2. Set direction

- Choose one recommended approach (done above).
- Record alternatives and rejected options (done above).
- Mark unresolved items as open questions with defaults (done above).

Exit evidence: the plan is executable in one direction.

### C3. Build the review artifact

- Write the Markdown plan (this file).
- No visual companions needed.

Exit evidence: the plan is surfaced and self-contained.

### C4. Review the artifact

- Inspect the plan for filler, invented paths, and claims not grounded in the files read.

Exit evidence: the first screen makes the proposed outcome understandable without the chat transcript.

### C5. Get approval

- Surface the plan and name the files/areas implementation will touch.
- Capture user comments in the feedback log.
- Mark the plan `approved` only after the user accepts the direction.

Exit evidence: the plan states what is approved and what remains deferred.

### C6. Implement

- Re-read the approved plan before editing.
- Work through the tasks in order:
  1. `ciao/gws_wrapper.py` + `ciao gws` subcommand.
  2. `ciao/gws_auth_helper.py` + `ciao gws-auth-helper` subcommand (reuse `ciao.gws_auth`).
  3. `gws_auth.auth_status` direct invocation; remove `wrapper_path()`.
  4. `routes_api.py` hints + `SettingsView.vue:1223`.
  5. `gws_skills.py` rewrite/curation + regenerate stock skills; `system_prompt.md`; `secretary.md`.
  6. Repo shims (`scripts/gws-profile.sh`, `scripts/gws-auth-helper.py`).
  7. Docs (`INTEGRATIONS.md`, `docs/ARCHITECTURE.md`) and tests.
- Update the plan when scope changes instead of silently changing course in chat.

Exit evidence: every planned source change has a corresponding implementation or an explicit deferral.

### C7. Verify

- Run focused tests first (`tests/test_gws_auth.py`, `tests/test_gws_skills.py`, `tests/test_core_prompt.py`, `tests/test_workspace_settings_routes.py`), then `pytest tests/`.
- Run `cd web && npm run build` after the frontend change.
- Verify the new subcommands manually: `ciao gws --help`, `ciao gws-auth-helper --help`, and a `ciao gws <profile> auth status` against a real profile.
- Record failures, skipped checks, and live verification limits.

Exit evidence: the plan distinguishes verified behavior from work that is only merged or assumed to be deployed.

### C8. Close or hand off

- Set the final status to `complete`, `deferred`, or `blocked`.
- Record the last verified commit or deployment state.
- Leave a next action only when work remains.

Exit evidence: another model can tell whether it should continue, verify, or stop.

## Verification and rollout

- Backend: `pytest tests/` (focused first, then full).
- Frontend: `cd web && npm run build`.
- Manual: run `ciao gws --help`, `ciao gws-auth-helper --help`, and `ciao gws <profile> auth status` against a real profile; confirm the Settings card shows the new commands.
- Rollout: this ships in the next release via the normal `ciao release` flow (which regenerates the stock `gws-*` skills from the installed `gws` CLI). The repo shims keep dev checkouts working immediately.
- Note: the agent-side `gws-*` skills only take effect in a workspace after `ciao sync-skills` refreshes the `.claude/skills/` catalog (runs on startup).
