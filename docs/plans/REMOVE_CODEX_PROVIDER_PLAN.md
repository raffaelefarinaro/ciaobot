# Remove the Codex Runtime Provider

## Resume block

- Status: proposed, revised for review
- Current checkpoint: C2 (direction set; implementation not started)
- Next action: confirm the migration semantics and then begin C3 backend removal
- Blocker: none; the previous plan was directionally approved, but its shared-asset and persisted-chat assumptions needed correction
- Implementation repository: `/Users/raffaelefarinaro/repos/ciaobot`
- Generated plan output: `docs/plans/REMOVE_CODEX_PROVIDER_PLAN.md`
- Visual companions: none; this is an execution-surface and migration change, not a layout change
- Verified on: 2026-08-24 against the current registry, provider adapter, setup/sync code, chat lifecycle, REST/PWA code, stock workspace assets, MCP docs, and Codex-related tests

This block is the handoff contract. A model resuming this work must read it first, then read the current checkpoint, decisions, and open questions. This document is a plan only; no source implementation is included in this revision.

## Outcome and user value

Remove `codex` as a Ciaobot runtime provider. The supported runtime set becomes:

- `claude`: Claude Code / Anthropic
- `opencode`: the provider-neutral harness for OpenAI, Anthropic, OpenRouter, Ollama, local OpenAI-compatible endpoints, and other backends exposed by opencode

The change removes the Codex app-server transport, Codex authentication/status probing, Codex-specific model catalog and reasoning logic, Codex-specific one-shot routing, native Codex agent/MCP projections, and Codex UI surfaces.

The user-facing documentation must state the new model plainly: use `claude` for Claude Code; use `opencode` for everything else, including OpenAI models and any available OpenAI subscription/API flow supported by the opencode installation. Ciaobot must not claim that opencode provides a ChatGPT subscription entitlement unless the installed opencode version actually does so.

The trade-off is intentional: users currently relying on `codex login` or ChatGPT subscription access through Codex lose that Ciaobot backend. They must choose an opencode-connected backend or Claude Code. No automatic reroute is allowed because it could change model behavior, authentication, cost, or data handling without consent.

## Scope and non-goals

### In scope

- Delete `ciao/providers/codex.py` and its export from `ciao/providers/__init__.py`.
- Remove the Codex descriptor from `ciao/provider_registry.py`; the registry remains the only runtime-provider enumeration.
- Remove `CodexSettings` and the top-level `config.codex` field from `ciao/config.py`.
- Remove Codex-only environment handling: `CIAO_CODEX_BIN`, `OPENAI_BASE_URL`, and `OPENAI_API_BASE` as read by the Codex adapter. Confirm that no surviving provider uses these names before removing their documentation.
- Remove setup/auth/status handling for Codex, including `ciao auth codex`, the Codex setup-status row, and the Codex startup phase.
- Remove Codex-only backend branches from `oneshot.py`, `critique.py`, `insights.py`, `project_chats.py`, `routes_api.py`, `mcp_server.py`, `commands.py`, `skills_inventory.py`, and any remaining provider-specific helpers.
- Remove Codex-only API payload fields and validation: Codex model catalog, Codex metadata, Codex reasoning-level maps, Codex provider entries, and Codex participant examples.
- Remove Codex-specific PWA picker, onboarding, login, provider settings, routine settings, schedule defaults, labels, types, and tests.
- Remove Codex wrappers, native Codex agent definitions, and Ciaobot-managed Codex MCP blocks from workspace synchronization.
- Add a one-time, marked-only cleanup for Ciaobot-generated Codex artifacts. Preserve unmarked user-owned `.codex` files and TOML tables; do not delete a user's independent Codex setup.
- Preserve provider-neutral assets required by OpenCode: `.agents/skills/`, `AGENTS.md` → `CLAUDE.md`, `.claude/skills/`, `.opencode/`, and the canonical workspace sources.
- Preserve historical Markdown chat archives. Existing Codex chats must not be silently rerouted; their execution must fail clearly, while an existing normalized/archive transcript should remain readable where the generic archive path can serve it.
- Update current product, integration, API, architecture, stock workspace, MCP, and capability documentation.

### Out of scope

- Changing Claude or OpenCode behavior, model catalogs, authentication, permissions, MCP semantics, or thinking levels except where a Codex-specific branch currently contaminates shared code.
- Adding a ChatGPT-subscription backend to OpenCode.
- Migrating Codex chats to OpenCode automatically.
- Rewriting or deleting historical `memory-vault/Logs/Chats/<chat-id>/codex/` files.
- Editing bundled `desktop/` build outputs. They are regenerated from the source package.
- Removing every historical occurrence of the word Codex from changelogs, historical plans, or attribution records. Those references need an explicit historical/retired status, not blind deletion.

## Current-state evidence

The following are observed from the files named below, not assumptions.

### Provider and configuration surface

- `ciao/provider_registry.py:1-17,105-150` enumerates `claude`, `codex`, and `opencode`; the Codex descriptor points to the adapter, auth command, system-skill discovery, status probe, Codex settings field, and Codex thinking-level union.
- `ciao/providers/codex.py` is a 1,642-line adapter owning binary discovery, ChatGPT/Codex login status, experimental app-server protocol probing, model discovery, approvals, questions, history, collaboration, cleanup, and `CodexSettings`.
- `ciao/config.py:16,483` imports and stores `CodexSettings`. `ciao/providers/codex.py:302-310` reads `OPENAI_BASE_URL` / `OPENAI_API_BASE`; the removal must verify no surviving provider reads them.
- `ciao/provider_service.py` already resolves factories through the registry, so removing one descriptor is the correct seam rather than adding a second provider condition.

### Shared assets that must not be removed

- `ciao/sync_skills.py:1448-1452` mirrors `.claude/skills/` into `.agents/skills/`. This is currently named with Codex counters, but `ciao/sync_skills.py:75-78` and `ciao/providers/opencode.py` document that OpenCode discovers `.agents/skills/` natively. The mirror must stay and its counters should become provider-neutral/shared names.
- `ciao/sync_skills.py:1303-1331` creates `AGENTS.md` as a relative link to `CLAUDE.md`. OpenCode also discovers `AGENTS.md`; removing this link would break the remaining provider's native guide discovery. Keep the link and rename Codex-specific local variables/comments.
- `ciao/web/agent_assets.py:389-435` checks `AGENTS.md`, `.agents/skills`, `.codex/agents`, and `.codex/config.toml`. Remove the Ciaobot-owned `.codex` health checks, but retain the shared guide and `.agents/skills` checks with provider-neutral labels.
- `ciao/skills_inventory.py:178-193` currently credits `.agents/skills` to both Codex and OpenCode. After removal it must credit that catalog only to OpenCode, while still reporting the catalog itself.
- `ciao/cli.py:423-432` ignores `.codex/` in workspace Git snapshots. Decide separately whether to retain this safety ignore for users who still run Codex outside Ciaobot; removal of the Ciaobot provider does not require deleting a user's external CLI data.

### Backend consumers

- `ciao/setup_status.py:24,437-450` imports and exposes Codex readiness; `ciao/setup_status.py:638-645` checks the shared `AGENTS.md` link and must not be deleted.
- `ciao/providers/oneshot.py:1-5,186-223,329-337` dispatches `codex:` entries to `CodexProvider`.
- `ciao/critique.py:37-142` detects Codex availability and adds a Codex voice to the default adversarial panel.
- `ciao/insights.py:160-186` recognizes `codex:` routing and Codex provider calls.
- `ciao/web/project_chats.py` contains Codex-specific fable handling, provider cleanup, slash-command expansion, subagent watching, title lookup, schedule classification, and provider dispatch branches. The generic OpenCode/Claude paths must remain intact.
- `ciao/web/routes_api.py:2254-2308,2664-2864,3212-3310,3699-3731,5503-5615` contains Codex reasoning validation, thread rendering, Codex thread reads, subagent reads, model catalog output, and API response fields.
- `ciao/mcp_server.py:1042-1070` documents Codex as a valid workspace provider; provider validation itself should remain registry-driven.
- `ciao/web/commands.py` has a deliberate Codex special case because Codex had no native project command contract. Once Codex is gone, retain only Claude/OpenCode command behavior and preserve generic picker behavior.

### Frontend consumers

The PWA is not fully registry-driven yet, so “the picker just iterates the registry” is not sufficient as a plan claim.

- `web/src/lib/modelSections.ts` explicitly builds a Codex section and `web/src/lib/types.ts` exposes Codex-specific response fields and comments.
- `web/src/components/LoginView.vue` hard-codes Codex in the setup tour, provider radio buttons, and auth instructions.
- `web/src/components/SettingsView.vue` hard-codes Codex model/routine sections, labels, provider auth/logout messages, and provider loops.
- `web/src/components/ChatPanel.vue`, `NewScheduleForm.vue`, and `SchedulePanel.vue` contain Codex provider labels, fable selection, and default-model exceptions.
- `web/src/components/StartupView.vue` maps a `connect_codex` startup phase.
- `web/src/lib/fableModel.ts` is Codex-only and should be deleted unless a surviving provider genuinely uses the same pseudo-model contract.
- `web/src/lib/chatActivity.ts`, `web/src/stores/projects.ts`, and related tests contain Codex-specific rendering heuristics. Keep provider-neutral behavior and remove only assumptions about Codex commentary/request identifiers.

### Documentation and stock assets

- Current docs requiring edits include `README.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `INTEGRATIONS.md`, `PWA_API.md`, `docs/MCP.md`, `docs/VAULT_MIGRATION_PROMPT.md`, and `ciao/stock/skills/ciao-capabilities/SKILL.md`.
- `ciao/stock/workspace/CLAUDE.md`, `AGENTS.md`, and `CIAO_CUSTOMIZATION.md` describe Codex-specific generated files, provider support, and sync targets. They must be rewritten so `AGENTS.md` remains an OpenCode-compatible shared guide.
- `docs/plans/CODEX_CLI_PROVIDER_PLAN.md` should remain as history with a clear `Status: retired` banner and the removal release/version, not as an active implementation contract.
- `CHANGELOG.md` and older plans may retain historical Codex entries. Current docs and the capability skill must not advertise Codex as supported.

### Persistence and migration evidence

- `ChatInfo.provider` is persisted in `.runtime/web_projects.json` and can contain `codex` after removal; `_load()` in `ciao/web/project_chats.py:1315-1396` does not currently migrate unknown providers.
- `ProviderService._ensure_provider()` calls `provider_registry.require()` and therefore correctly rejects a new execution attempt with `ValueError("Unknown provider 'codex'")` once the descriptor is removed.
- `/api/chats/{id}/messages` currently reads Codex native threads for chats with a session, while `_messages_from_archived_transcript()` can already read a Markdown archive without starting a provider. Removing the native branch must preserve the generic archive fallback and avoid trying to read an unknown provider as Claude.
- Archive/reset/delete cleanup currently calls Codex-specific session deletion. After removal, historical Codex session IDs must not be sent to OpenCode or Claude cleanup APIs. Provider cleanup must become a safe no-op for the retired provider.
- Schedules and loops can persist provider names. Existing fixed chats or schedule entries referring to Codex must not create new Codex chats after the registry change; they need a clear unavailable result or an operator-visible migration path.

### Assumptions to verify on the target install

- The repository does not contain operator vault/runtime data. Before release, inspect the configured workspace's `.runtime/web_projects.json`, `.runtime/schedules.json`, `.runtime/loops.json`, and archive paths for active or scheduled `codex` records without printing private content.
- There may be user-owned `.codex/config.toml` or `.codex/agents/` files. Cleanup must be marker-gated and preserve unmarked files.
- OpenCode's current installation supports OpenAI API/OAuth paths required by the release note. If it does not, documentation must say “OpenCode-connected backend” rather than promise a specific OpenAI subscription flow.

## Recommended direction

Use one atomic removal PR, but execute it in dependency order and include a narrow data/asset migration. Do not add a feature flag: a flag would retain the provider code, protocol probe, auth surface, and test matrix that this change is intended to remove.

The key boundary is **execution capability versus historical data**:

- Remove all ability to start, resume, hand over to, schedule, or authenticate a Codex runtime.
- Do not reinterpret an old Codex chat as OpenCode or Claude.
- Keep old Markdown archives readable through the provider-neutral archive path when an archive exists.
- For a persisted active Codex chat with no readable archive, return a clear unavailable-provider error and offer “start a new chat with opencode” in the PWA. Do not silently change its provider.
- Do not call any Codex session-delete API after the adapter is removed. A retained old session ID is inert metadata, not an instruction to clean up through another provider.

## Alternatives and rejected options

### Feature flag / soft disable

Rejected. It keeps the adapter, auth/status probes, `.codex` projection, and fragile app-server protocol in the product and CI surface.

### Silent reroute old Codex chats to OpenCode

Rejected. It changes the provider, account, model resolution, billing, permissions, and possibly prompt semantics without explicit user consent.

### Delete all old Codex chats and archives

Rejected. The Ciaobot archive is user-owned durable data. Removing execution support does not justify destructive deletion.

### Remove every `.codex` directory

Rejected. A workspace may contain user-owned Codex configuration unrelated to Ciaobot. Remove only Ciaobot-marked wrappers and managed blocks; preserve the rest.

### Remove `AGENTS.md` and `.agents/skills/`

Rejected. Those are shared native discovery surfaces used by OpenCode. The old names and comments are Codex-biased, but the files are not Codex-only.

### Three partially broken PRs

Rejected. Registry, consumers, generated assets, tests, and docs must move together so intermediate builds do not advertise a provider whose implementation has already disappeared.

## Visual review

No companion is needed. The review question is whether provider execution, generated assets, persisted data, and public contracts remain consistent. Review the Markdown plan, the dependency-ordered diff, the API payload tests, and the fresh-workspace sync fixture instead of a visual mockup.

## Decisions and hard-to-reverse bets

| ID | Decision | Rationale | Status |
| --- | --- | --- | --- |
| D-01 | Delete Codex rather than feature-flag it | Achieves the intended reduction in runtime, auth, transport, and test surface | Retained from prior approval |
| D-02 | OpenCode is the route for non-Claude backends, including OpenAI where the installed OpenCode setup supports it | Avoids maintaining a second OpenAI harness; does not promise a subscription entitlement that OpenCode cannot provide | Retained; wording clarified |
| D-03 | Never silently reroute persisted Codex chats | Provider/account/cost changes require consent | Retained from prior approval |
| D-04 | Preserve historical archives and serve them read-only when possible | Archive data is durable user data; native Codex sessions are not required to read Markdown | Revised: preserve readable archive fallback rather than declaring all archives unsurfaced |
| D-05 | Remove Ciaobot-managed Codex projections with marker-gated cleanup | Prevent stale generated files while protecting user-owned `.codex` configuration | New recommendation |
| D-06 | Keep `AGENTS.md` and `.agents/skills/` for OpenCode | These paths are shared/native OpenCode discovery surfaces, not Codex-only assets | New correction |
| D-07 | `ciao auth codex` becomes an unknown-provider error | The command should cease to exist, with valid choices derived from the two-entry registry | Retained from prior approval |
| D-08 | Remove Codex-only `CIAO_CODEX_BIN`, `OPENAI_BASE_URL`, and `OPENAI_API_BASE` handling | These names are read by the deleted adapter; surviving use must be proved absent before removal | Revised to include `OPENAI_API_BASE` |
| D-09 | One atomic PR and one release note | Keeps the public contract and implementation synchronized and makes revert practical | Retained |

## Open questions with recommended defaults

| ID | Question | Recommended default | Status |
| --- | --- | --- | --- |
| Q-01 | What should happen to an existing active Codex chat with no Markdown archive? | Keep the record as unavailable historical metadata, reject execution with a clear error, and offer a new OpenCode chat; never reroute or delete it | Open |
| Q-02 | Should old archived Codex chats remain in the sidebar? | Yes, if the existing archive path is readable; show them as read-only/unavailable rather than pretending they are active OpenCode chats | Open |
| Q-03 | How should Ciaobot-generated `.codex` artifacts be removed? | Delete only files with Ciaobot markers and remove only the marked config blocks; preserve user tables/files, and leave an empty user config intact | Open |
| Q-04 | Should `.codex/` remain in the workspace `.gitignore`? | Yes for this release as a safety guard for external Codex use; remove only Ciaobot ownership/documentation, not the protective ignore | Open |
| Q-05 | Should historical MCP benchmark material mention Codex? | Keep it only under an explicitly historical heading, or remove it if it is presented as current support; current configuration examples must be Claude/OpenCode only | Open |
| Q-06 | What release version replaces the placeholder in retired-plan and release-note text? | Fill it during release preparation, not in the implementation PR unless the project convention requires it | Open |

## Not yet specified (fog of war)

- The exact API shape for an unavailable persisted provider is not yet chosen: a `provider_available` field, a `retired_provider` field, or a route-level error may be enough. The implementation should prefer the smallest shape that lets the PWA distinguish read-only history from an executable chat.
- The exact wording and placement of the PWA migration action is not yet chosen. It should be plain language, avoid raw provider internals, and use the existing new-chat/handover affordances rather than inventing a new workflow.
- The cleanup receipt/path for marked `.codex` artifacts is not yet chosen. It must be idempotent and must not make a failed partial cleanup look complete.

## Feedback and decision log

| ID | Location | Feedback | Decision | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| F-01 | Outcome | The user questioned whether removal makes sense because OpenAI users can authenticate through API and OAuth. | Removal still makes sense if OpenCode is the documented non-Claude route; explicitly call out the ChatGPT-subscription entitlement gap. | accepted | Prior plan feedback; `INTEGRATIONS.md` and OpenCode provider behavior |
| F-02 | Migration | The user said not to leave legacy Codex code for old chats. | No Codex adapter or native-session compatibility is retained. Provider-neutral read-only archive fallback is allowed because it does not execute or reroute Codex. | accepted, clarified | `routes_api.py:_messages_from_archived_transcript` |
| F-03 | CLI | The user said `ciao auth codex` should simply not exist. | Remove it from registry-derived choices; unknown-provider error is expected. | accepted | `ciao/cli.py:_auth_command_for_provider` |
| F-04 | Documentation | The user requested documentation say Claude Code uses `claude` and the rest uses `opencode`. | Apply that wording consistently to current docs, onboarding, capabilities, and provider settings. | accepted | Current plan direction |
| F-05 | Shared assets | This review found that `.agents/skills/` and `AGENTS.md` are also OpenCode discovery surfaces. | Do not remove them; remove only Codex wrappers/native projections and rename misleading comments/counters. | implemented in plan | `sync_skills.py`, `opencode.py`, `agent_assets.py` |
| F-06 | Persisted state | This review found that `.runtime/web_projects.json` can retain `provider: codex` and current message assembly has Codex-native reads. | Add an explicit read-only/unavailable behavior to the implementation checkpoint instead of claiming all old chats are simply invisible. | implemented in plan | `project_chats.py`, `routes_api.py` |

## Implementation checkpoints

Each checkpoint has an exit condition. Update this plan's Resume block as work advances; do not silently expand scope.

### C0. Start or resume

- Read this Resume block, current checkpoint, decisions, and open questions.
- Read `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `INTEGRATIONS.md`, `PWA_API.md`, `docs/MCP.md`, and the live files named in this plan.
- Confirm the worktree changes are unrelated or user-owned; do not revert them.

Exit evidence: the plan status and repository state agree.

### C1. Inventory and release preflight

- Run a source inventory for semantic Codex references in `ciao/`, `web/src/`, `tests/`, current docs, stock assets, `pyproject.toml`, and scripts.
- Classify every hit as runtime code, current documentation, generated/user asset handling, test fixture, or historical record. Do not use a blanket zero-match rule.
- On the target install before release, inspect only metadata and paths in `.runtime/web_projects.json`, `.runtime/schedules.json`, `.runtime/loops.json`, and `memory-vault/Logs/Chats/`; report counts/paths without printing private prompts or secrets.
- Back up the workspace before any generated-artifact cleanup.

Exit evidence: an allowlist of intentionally retained historical/general references and a count of persisted Codex chats/artifacts.

### C2. Registry, config, and adapter removal

- Remove the Codex descriptor and update registry module documentation to `claude` and `opencode`.
- Delete `ciao/providers/codex.py` and its package export.
- Remove `CodexSettings` and `config.codex`; update generic config comments and defaults.
- Remove Codex-only environment reads and docs, including both `OPENAI_BASE_URL` and `OPENAI_API_BASE`; retain an env name only if a surviving provider demonstrably reads it.
- Keep `provider_registry`, `provider_service`, and capability-driven routing generic.

Exit evidence: importing config, provider service, and the registry succeeds; `provider_ids()` returns exactly `("claude", "opencode")`; no deleted module is imported.

### C3. Backend consumers and persisted-chat behavior

- Remove Codex branches from one-shot calls, critique defaults/availability caching, insights routing, title lookup, fable/thinking logic, subagent watchers, schedule classifiers, and provider cleanup.
- Remove Codex native thread/model reads from `routes_api.py`; retain the OpenCode and Claude branches and the generic Markdown archive parser.
- Ensure an unknown persisted provider is never passed to the Claude reader by default. For an old Codex chat, return readable archive content when available and a stable unavailable-provider error when execution/native history is required.
- Ensure new chat creation, workspace defaults, schedule creation, handover, and MCP workspace creation reject `codex` through the registry-derived validation path.
- Decide and test the smallest response metadata needed for the PWA to label an old chat read-only/unavailable.
- Remove Codex-specific `CIAO_PROVIDER` assumptions from surviving paths while keeping the generic environment marker for current providers.

Exit evidence: new Codex execution is impossible; old records do not crash startup, do not reroute, and do not invoke Claude/OpenCode as a substitute.

### C4. Sync, health checks, and generated-artifact cleanup

- Keep `.agents/skills/` mirroring because OpenCode reads it. Rename `codex_skills_*` counters and output to shared/provider-neutral names.
- Keep `_ensure_linked_workspace_guides()` and the `AGENTS.md` health check, but describe the guide as shared by Claude/OpenCode.
- Delete Codex wrapper generation, native `.codex/agents` generation, and Codex MCP projection.
- Add an idempotent cleanup pass that removes only `CODEX_WRAPPER_MARKER`, `CODEX_AGENT_MARKER`, `CODEX_CONFIG_BEGIN/END`, and `CODEX_MCP_CONFIG_BEGIN/END` artifacts. Preserve unmarked files, TOML, tables, and user-authored `AGENTS.md`.
- Remove `.codex` from Ciaobot workspace-health ownership checks. Retain `.codex/` in `.gitignore` unless Q-04 is explicitly changed.
- Update `skills_inventory.py` so `.agents/skills` is reported as visible to OpenCode, not Codex.

Exit evidence: a fixture with generated and user-owned `.codex` content proves only generated content is removed; a fresh sync still produces `.agents/skills`, `AGENTS.md`, `.claude/`, and `.opencode/` correctly.

### C5. PWA and frontend contract

- Remove Codex from `ModelsResponse`, model sections, provider labels, model metadata, and reasoning-level handling.
- Remove `CODEX_FABLE_*` logic and its tests unless a surviving provider uses the same state shape.
- Remove Codex from LoginView provider choices/instructions, StartupView phase labels, SettingsView provider/routine/auth loops, ChatPanel labels and inference, and schedule forms.
- Keep provider lists driven by `GET /api/models.providers[]` where that is already the contract; do not replace the removed Codex branch with another hard-coded provider union.
- Retain OpenCode namespaced model behavior and Claude aliases; verify OpenCode models are still selectable and routines still resolve.
- Add a visible but compact read-only/unavailable state for old Codex chat records if C3 adds one.

Exit evidence: the PWA renders only Claude and OpenCode provider choices, no Codex fields are required in fixtures, and the OpenCode model/routine flows remain usable.

### C6. Tests

- Delete `tests/test_codex_provider.py`, `tests/test_codex_live_smoke.py`, and `tests/test_codex_routes.py`.
- Prune Codex cases from provider registry, setup status, CLI, critique, insights, thinking-level, chat routing, delegate, title, transcript, command-picker, skills inventory, agent-assets, MCP, and archive tests.
- Add focused tests for the revised behavior: registry has two providers; `ciao auth codex` errors; unknown persisted Codex records do not instantiate a provider; archived Markdown remains readable; cleanup preserves unmarked `.codex` content; `.agents/skills` and `AGENTS.md` remain; OpenCode model/routine paths remain.
- Update frontend fixtures and tests for two providers, especially `modelSections`, `ModelSelector`, `mountSmoke`, `SettingsView`, and project-store question/activity tests.

Exit evidence: focused backend/frontend tests pass and no test imports the deleted adapter or asserts Codex as a supported provider.

### C7. Current documentation and stock assets

- Update `README.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `INTEGRATIONS.md`, `PWA_API.md`, `docs/MCP.md`, `docs/VAULT_MIGRATION_PROMPT.md`, and `ciao/stock/skills/ciao-capabilities/SKILL.md`.
- Update `ciao/stock/workspace/CLAUDE.md`, `AGENTS.md`, and `CIAO_CUSTOMIZATION.md` to remove Codex-specific support claims while retaining shared OpenCode discovery paths.
- Update `pyproject.toml` comments and any current provider capability text.
- Mark `docs/plans/CODEX_CLI_PROVIDER_PLAN.md` retired with the release version. Do not rewrite historical changelog entries; remove or label current Codex examples in `docs/MCP.md` rather than leaving them as live instructions.

Exit evidence: current documentation describes exactly two supported runtime providers and points OpenAI users to OpenCode without promising unsupported auth.

### C8. Verification and rollout gate

- Run focused tests first, then `pytest tests/`.
- Run `cd web && npm test` and `cd web && npm run build`.
- Run a clean package/smoke check if the release process requires it.
- Exercise API behavior against a test app: two provider descriptors, no Codex setup-status/model fields, `ciao auth codex` error, new Codex chat rejection, old archived Codex transcript read-only behavior, and OpenCode chat creation/model selection.
- Run `ciao sync-skills` on a fresh fixture and a fixture containing generated plus user-owned `.codex` files.
- Run the source/document inventory again and compare it with the intentional-retention allowlist.
- Record failures, skipped live-auth checks, and deployment limits in this plan.

Exit evidence: all required tests/builds pass; the release note and migration behavior match the actual API/PWA behavior.

## Verification and rollout

### Required local gates

```bash
source .venv/bin/activate
pytest tests/
cd web && npm test
cd web && npm run build
```

Focused checks should include the provider registry, config/setup status, CLI auth, provider routing, project chat lifecycle, transcript/archive routes, sync-skills, agent assets, skills inventory, MCP, and the OpenCode provider tests. The exact focused list may change after the inventory, but it must cover every edited subsystem.

### Behavioral acceptance criteria

- `provider_registry.provider_ids()` returns only `claude` and `opencode`.
- `ciao auth codex` exits non-zero with an unknown-provider error and lists only valid providers.
- `GET /api/setup-status` has no Codex entry; `GET /api/models` has no Codex catalog/metadata fields and returns only the two provider descriptors.
- New chats, workspaces, schedules, loops, handovers, and MCP workspace creation cannot select `codex`.
- Existing Codex chat records do not trigger provider construction, Claude fallback, OpenCode fallback, or startup failure. Readable Markdown archives remain viewable as read-only history; execution reports the migration clearly.
- OpenCode still discovers `AGENTS.md`, `.agents/skills/`, `.claude/skills/`, and its generated `.opencode/` assets.
- Ciaobot-generated Codex wrappers/config blocks are removed idempotently; user-owned `.codex` content is untouched.
- Current docs and onboarding describe `claude` for Claude Code and `opencode` for other backends. Historical plans/changelog entries are either marked historical or intentionally retained.

### Rollout

- Ship as one atomic PR and one release-note entry.
- Include a prominent migration note for users who authenticated through Codex/ChatGPT: Ciaobot no longer runs Codex; configure the desired backend in OpenCode or use Claude Code.
- Back up workspace/runtime data before generated-artifact cleanup.
- Do not delete archived Markdown or provider credentials owned by an external CLI.
- Revert the PR if needed; do not attempt to restore Codex state by automatically rewriting persisted chats.
