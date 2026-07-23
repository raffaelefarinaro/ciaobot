# Ciaobot Architecture

Orientation doc for the codebase. Read this before making any change in `ciao/`, `web/`, `scripts/`, or `deploy/`. After a change that affects layout, capabilities, env vars, endpoints, or commands, update this file, `docs/DEVELOPMENT.md`, and `INTEGRATIONS.md` so they match the repo.

## App repo layout

```
README.md                      Product intro, quickstart, doc index.
CLAUDE.md / AGENTS.md          Contributor guide for coding agents. Loaded every prompt.
INTEGRATIONS.md                Operator config (env vars, OAuth, MCP connectors, server runtime knobs).
PWA_API.md                     PWA API reference (endpoints, auth flow, state path).
CONTRIBUTING.md                Contribution guide.
docs/                          Long-form docs (this file, DEVELOPMENT.md).

ciao/                          Python backend (Starlette).
  main.py                      Web server entry point, route wiring, startup hooks.
  cli.py                       Packaged `ciao` CLI: server, setup, dev, sync-skills, vault, memory, chat, public-preflight, and package-smoke commands.
  setup_status.py              Bootstrap/setup readiness API and wizard finish handler.
  dev.py                       Local dev runner: backend on :8543 plus Vite frontend on :5173. CLI: `ciao dev`.
  config.py                    Env var loading, workspace config.
  providers/                   Claude Agent SDK and Codex app-server providers plus routing helpers.
    stdio_rpc.py               Async JSON-lines RPC process transport used by CLI app-server adapters.
    codex.py                   Persistent Codex app-server adapter, model/account discovery, approvals, questions, history.
    routing.py                 Resolve intended backend (Anthropic / Ollama / OpenRouter) from a model ID and build per-turn env injection. Used by chats and one-shot automations.
    oneshot.py                 Single-turn provider call (max_turns=1, no tools). Used by critique.py and routine-model automations.
  context/                     Per-turn context injection (workspace, project, vault hints).
  observability/               Hooks: UserPromptSubmit (runtime context + vault entity tags) and PostToolUse (WebSearch backfill on Ollama-cloud routes). Plus logging, transcript capture.
  schedules.py                 Cron-style schedule dispatch.
  loops.py                     In-chat loops: re-dispatch a prompt into a fixed chat every N minutes.
  dag.py                       Tiny DAG runner (Node kinds: bash / prompt / gate / subagent / retention; edges: ok / fail / always). Subprocess nodes can merge per-node env overrides for routed models. Each node is timed via `job_runs.track_sync`, so Automation page shows per-node status. Used by skill evolution and dependency review.
  sessions.py                  Session state, auth, signed cookies, JSON-backed StateStore for `.runtime/state.json`.
  transcripts.py               Provider-neutral live transcripts and archives under memory-vault/Logs/Chats/.
  upgrade.py                   Self-update / deploy flow.
  voice.py                     Voice transcription: cloud (OpenAI) and local (mlx-whisper) engines, selected by `CIAO_TRANSCRIPTION_ENGINE`.
  app_settings.py              Runtime-mutable app settings (title, insights, and critique models, transcription engine/model), persisted at `.runtime/app_settings.json` and overlaid on CiaoConfig. Edited via Settings → Models.
  error_log.py                 Rotating file handler for server ERROR+ logs. Consumed by the error-triage schedule and the debug issue report.
  debug_report.py              Aggregate runtime issues (server error log + failed job runs) for the dev-mode `GET /api/debug/issues` endpoint and the `{{ISSUE_REPORT}}` schedule placeholder.
  job_runs.py                  Fail-open recorder for background-job runs. Appends one JSON record per run to `.runtime/job_runs.jsonl` (size-trimmed like `error_log.py`). Background tasks wrap their work in `track()`/`track_sync()`; `automation_summary()` feeds the Settings → Automation page.
  models.py                    Shared data models (ChatContext, AgentRequest, etc.).
  provider_service.py          Provider request builder and execution wrapper.
  control_plane.py             Provider-neutral, scope-enforcing application operations shared by MCP and PWA-owned managers.
  mcp_server.py                Embedded authenticated Streamable HTTP MCP adapter and scoped token registry.
  control_surfaces.py          Persist/read promoted per-provider legacy-vs-MCP decisions for Auto chats.
  control_surface_benchmark.py Paired live 12-scenario evaluator (latency, correctness, tools, tokens) and guarded winner promotion.
  signals.py                   Restart / deploy signals.
  execution_modes.py           Claude permission mode normalization.
  git_sync.py                  Startup git pull / merge-before-push helpers.
  local_session.py             Current-branch git sync flow (LocalSessionManager): preflight, commit + pull + push, conflict chat.
  rate_limits.py               Rate-limit tracking and persistence.
  insights.py                  Post-archive session insights extraction.
  critique.py                  Multi-model adversarial review. Runs each reviewer through ciao/providers/oneshot.py with per-model routing (OpenRouter / Ollama / Anthropic). CLI: `python -m ciao.critique`. Used by the adversarial-review skill.
  fts_search.py                SQLite FTS5 full-text indexing and search for vault and transcripts.
  vault_index.py               Build/query memory-vault/INDEX.md from frontmatter and wikilinks. CLI: `ciao vault-index`.
  vault_lint.py                Programmatic linter and wikilink validation for the memory-vault. CLI: `ciao vault-lint`.
  trajectory_builder.py        Parse filtered session JSONL into a structured trajectory (turns, tools, skills, errors) and persist to ~/.ciao/trajectories/YYYY-MM/<session-id>.json. CLI: `python3 -m ciao.trajectory_builder --list [...]`.
  skill_evolution.py           Weekly pass: mine trajectories, flag skills tied to non-success sessions, draft edit proposals to the vault's Workspace/Skill-Proposals/. 15KB skill cap, optional test gate, no auto-apply. `run_evolution_pass` builds a per-skill DAG (has_proposal → semantic → tests → write, with a write_stub fallback for over-cap skills) via `ciao/dag.py`.
  dependency_review.py         Weekly dependency-changelog review as a `ciao/dag.py` pipeline (read_baseline gate → installed-versions gate → research subagent fanning out release checks → write_baseline gate). PyPI/npm are authoritative for installable package versions; manifest updates are rolled back and the run fails if registry validation or dependency installation fails. Trigger: `python3 -m ciao.dependency_review [--model sonnet] [--dry-run]`. Merges and gates the write to `.runtime/dependency_baseline.json` so a flaky run can't corrupt it.
  skills_inventory.py          Build the Settings skill inventory from workspace skills and skills-lock.json, with installed Claude badges.
  sync_skills.py               Install packaged stock skills and mirror workspace skills, commands, and agents into `.claude/`. CLI: `ciao sync-skills`.
  skills_sync.py               Change-detect upstream skills in skills-lock.json so `ciao sync-skills` updates only moved repos. CLI: `ciao skills-sync`.
  cleanup_sdk_blobs.py         Maintenance: drop archived Claude SDK JSONL blobs. CLI: `ciao cleanup-sdk-blobs`.
  memory_injector.py           Read ~/.ciao/memory.md and ~/.ciao/user.md at session start, render as a system-prompt block; also injects the baseline Ciaobot system instructions into every chat.
  memory_tool.py               Bounded memory-file validation and CRUD used by CLI and the application control plane.
  memory_proposals.py          Scan archived `## Session insights` sections, propose memory entries to the vault's Workspace/Memory-Proposals.md.
  public_release.py            Public extraction allowlist, export copier, and private-data preflight scanner. CLI: `ciao public-preflight export <src> <dest>` then `ciao public-preflight scan <export-root> --private-patterns <file>`.
  release.py                   Release preparation: sync-bump versions across pyproject.toml, web/package.json, and package-lock.json; report dependency updates; regenerate the packaged gws-* stock skills from the installed gws CLI on `--apply`. CLI: `python -m ciao.release <version>`.
  gws_skills.py                Regenerate the curated gws-* stock skills from `gws generate-skills` and apply Ciaobot curation (profile-wrapper examples, auth notes, strip upstream openclaw metadata and See Also boilerplate). Used by `ciao/release.py`.
  package_version.py           Best-effort version probe: reads ciao.__version__ and queries the GitHub releases API for the latest published version. Powers GET /api/package/status.
  package_smoke.py             Wheel smoke target: build web, build wheel, install in a clean venv, and probe the installed app. CLI: `ciao package-smoke`.
  stock/                       Generic package-data assets for public installs: stock agents, commands, skills, launchd template, workspace docs, and system schedules.
    skills/                    Packaged generic skills (ciao-capabilities, web-research, workspace-authoring). Installed into every workspace's `.claude/skills/` by `ciao sync-skills`; a same-named workspace skill overrides the packaged copy.
    workspace/                 Agent-readable docs copied into installed workspaces (`CLAUDE.md`, `CIAO_CUSTOMIZATION.md`).
    schedules.json             System schedules (memory curation, skill evolution, weekly review).
    schedules/                 Long-form schedule assets (weekly-review-template.md).
  web/                         PWA web server (Python routes) + static assets (built by web/).

web/                           Vue 3 PWA frontend.
  src/App.vue                  Root component.
  src/main.ts                  App bootstrap.
  src/router.ts                Vue Router config.
  src/components/              UI components (chat, projects, settings, voice, etc.).
  src/stores/                  Pinia stores.
  src/lib/                     Helpers (API client, formatters, etc.).
  package.json                 npm deps and scripts (build, typecheck, test). Includes a small React bridge for the Excalidraw file previewer.
  Vite + TypeScript. Build outputs to ciao/web/static/.

scripts/
  README.md                    Scripts directory orientation.
  run-ciao.sh                  Start the Ciaobot server in dev.
  dev.sh                       Compatibility wrapper for `ciao dev`.
  create-chat.py               Compatibility wrapper for `ciao create-chat`.
  ensure-deps.sh               Dependency verification and venv repair (macOS libexpat and SSL workarounds). Sourced by run-ciao.sh and dev.sh.
  gws-profile.sh               Switch GWS profile (personal | work). Uses exec, do NOT source.
  gws-auth-helper.py           Interactive headless OAuth re-auth for gws (personal | work).
  install-custom-skills.sh     Compatibility wrapper for `ciao sync-skills`.
  skills_add.py                Add an upstream skill repo to skills-lock.json.
  skills_sync.py               Compatibility wrapper for `ciao skills-sync`.
  vault_index.py               Compatibility wrapper for `ciao vault-index`.
  vault-search.py              Compatibility wrapper for `ciao vault-search`.
  vault-lint.py                Compatibility wrapper for `ciao vault-lint`.
  memory-cli.py                Compatibility wrapper for `ciao memory` on the legacy control surface.
  cleanup_sdk_blobs.py         Compatibility wrapper for `ciao cleanup-sdk-blobs`.
  backfill_insights.py         One-off: backfill session insights for existing archives.
  prepare-release              Release helper wrapper.

tests/                         pytest suite for the Python backend.
pyproject.toml                 Python package metadata, package-data declarations, and dev/test deps.
```

## Workspace layout

The app repo above is the installable package. A **workspace** is a separate directory (created by `ciao setup`, never committed to this repo) that holds the user's data and per-instance state: one root folder containing the vault (`memory-vault/` by default) alongside `commands/`, `subagents/`, `skills/`, `.runtime/`, and config. Setup ensures the workspace is a git repository: if the chosen folder is not already inside one, it runs `git init -b main`, writes a `.gitignore` guarding `.env`, `.runtime/`, `.claude/`, and `*.log`, and makes an initial commit; pre-existing repos are left untouched apart from appending any missing `.gitignore` guards. The vault may also point elsewhere (`CIAO_VAULT_ROOT`, e.g. an existing notes folder): in that case setup initializes that folder as its own git repository (`git init -b main`, a minimal `.gitignore` for `.DS_Store` and `.obsidian/workspace*`, and an initial commit); a vault nested inside the workspace repo — the default layout — is never double-initialized.

```
.env                           Server, provider, workspace, and integration config.
CLAUDE.md                      Canonical editable workspace behavior instructions (seeded from ciao/stock/workspace/).
AGENTS.md                      Relative link to CLAUDE.md so Codex reads the same workspace instructions (custom existing files are preserved).
CIAO_CUSTOMIZATION.md          Local customization surface.
skills/  subagents/  commands/ Canonical user-owned sources; `ciao sync-skills` mirrors them into .claude/.
.claude/{skills,agents,commands}/  Generated Claude symlinks/copies (packaged stock assets + user sources). Do not edit by hand.
.agents/skills/                   Codex skill catalog and command/role wrappers. Locked packages installed by the upstream `skills` CLI are canonical here; Ciaobot-owned skills are linked here from `.claude/skills/`.
.codex/{config.toml,agents/}      Generated native Codex agent registrations and role instructions.
memory-vault/                  Durable markdown memory: MEMORY.md, INDEX.md, entity folders, projects/{active,completed}/, Workspace/, Logs/Chats/.
.runtime/                      Local state: schedules.json, web_projects.json, control_surface_decision.json, MCP/provider tool telemetry JSONL, server.lock, job runs/errors, snapshots/, transcripts/, state.json. Not committed.
secrets/                       OAuth credentials (gitignored).
```

## Server and chat pipeline

`ciao/` is a Starlette web server that mounts the PWA frontend, exposes a JSON API for projects/chats/schedules, and drives Claude Agent SDK sessions for each chat turn. Auth is a pre-shared token (`PWA_AUTH_TOKEN`) traded for a signed session cookie. Operational state lives in `.runtime/` under `CIAO_WORKSPACE`; durable memory lives under `CIAO_VAULT_ROOT` (default `<CIAO_WORKSPACE>/memory-vault`).

One backend process owns a runtime directory at a time. `ciao/instance_lock.py` holds `.runtime/server.lock` for the process lifetime, so a normal server and `ciao dev` cannot run against the same registry concurrently. Project/chat writes use a short-lived registry lock, merge field-level local deltas onto the latest revision, and append mutation IDs to `.runtime/web_projects.audit.jsonl`. Vault-backed project IDs are deterministic for newly discovered `(workspace, vault_folder)` pairs. At startup, archive discovery resolves both display names and vault-folder slugs; missing active rows are reconstructed from normalized runtime transcripts only when a surviving provider session or a non-deleted audit record proves they were active. Explicit deletion clears provider state, normalized transcripts, and session state so recovery cannot revive it.

Chat transcripts are streamed via WebSocket and archived to `Logs/Chats/<chat-id>/claude/` under the vault root (`transcripts.py`). The Claude Agent SDK is configured in `ciao/providers/claude.py` with a `fallback_model` chain (Opus to Sonnet to Haiku) and `setting_sources=["user", "project", "local"]` so `.claude/skills/`, `.claude/agents/`, and `.claude/commands/` auto-discover.

The same server embeds a Streamable HTTP MCP endpoint at `/mcp/`.
`CiaoControlPlane` wraps the existing project/chat, schedule, loop,
handoff, vault, memory, file-history, workspace-health, local-session, and
lifecycle managers; `CiaoMcpService` only performs bearer authentication, tool
registration, plan/handoff mutation policy, stable error envelopes, and
telemetry. Each managed provider process receives a short-lived capability
scoped to one chat/project/workspace/provider. Claude receives the server and
Authorization header through `ClaudeAgentOptions.mcp_servers`; Codex receives
per-process `mcp_servers.ciaobot.*` overrides and an environment token excluded
from its shell policy. When the MCP server is unavailable a chat degrades
gracefully to legacy with a logged WARNING instead of failing the turn.
Self-disconnecting operations defer until their caller chat drains. See
`docs/MCP.md`.

MCP is the default control surface (`config.control_surface = "mcp"`) for both
providers; `legacy` is a hidden fallback selectable via `CIAO_CONTROL_SURFACE`
or the per-chat `ChatInfo.control_surface` escape hatch (the PWA no longer
exposes a selector). `ChatInfo.control_surface` still permits legacy and MCP
chats to coexist. `auto` reads a guarded, provider-specific decision from
`.runtime/control_surface_decision.json`; missing, partial, tied, or malformed
decisions resolve to legacy. `ciao benchmark-control-surfaces` produces the
decision evidence from paired isolated full-stack runs and only promotes a
complete 12-scenario, five-repeat result.

Server restart requests drain chat work before shutting down: active broker streams, already-queued follow-up turns, and background-subagent watchers are allowed to settle, while brand-new turns are rejected once draining begins. When drain starts, `ProjectChatManager.begin_restart_drain` publishes `server_restarting` on `/ws/events` (and the connect snapshot carries `restarting: true`) so every open PWA shows the full-screen restart overlay instead of treating turn rejection as a chat error. Shutdown starts only after several consecutive idle observations so the handoff from a completed parent turn to its background-agent watcher or synthesis stream cannot create a false-idle race. The macOS menu-bar action uses `/api/active-chats` as a guard before its direct launchd restart and asks the operator to retry once active chats finish.

When the model calls `AskUserQuestion` (which the headless CLI can't render, and would otherwise auto-answer empty), `ProjectChatManager._drive` interrupts the turn so generation stops at the question; the PWA surfaces it in its own picker (persisted as `ChatInfo.pending_question` so it survives a reload) and the user's reply resumes the session as a fresh turn.

If a turn fails before any provider progress with a quota or session-limit error, `ProjectChatManager` stores the last prompt and schedules an hourly deferred retry; the chat view and sidebar surface the pending state, and `/api/chats/{chat_id}/retry` lets the UI set, stop, or run that retry immediately.

Conversation forks let a user start a new chat in the same project starting from any completed agent answer in an active or archived chat. When a fork is created via `/api/chats/{chat_id}/fork`, the system performs a provider-neutral history copy, copying the visible chat history up to the selected assistant response, normalizing the messages (with system notes for truncated older messages if bounds are exceeded), duplicating referenced images to ensure source independence, and initializing a new chat with `handover_context_pending=True`.

## Memory, insights, and self-improvement

When a chat is archived, the raw session JSONL is filtered and passed through a fast model to extract durable insights (errors, dead ends, new entities, decisions, reusable code) which are appended as a `## Session insights` section; this section is the preferred input for memory curation and downstream automation (`ciao/insights.py`, gated by `CIAO_INSIGHTS_DISABLED`, `CIAO_INSIGHTS_MIN_TURNS`, `CIAO_INSIGHTS_MODEL`).

In the same archive hook, `ciao/trajectory_builder.py` writes a structured trajectory JSON (turns, tool counts, skills loaded, errors) to `~/.ciao/trajectories/YYYY-MM/<session-id>.json`; a weekly schedule runs `ciao/skill_evolution.py` to mine those trajectories and draft skill-edit proposals under the vault's `Workspace/Skill-Proposals/` (gated by `CIAO_TRAJECTORIES_DISABLED`, `CIAO_TRAJECTORY_RETENTION_MONTHS`, `CIAO_SKILL_EVOLUTION_DISABLED`). The per-skill pipeline inside `run_evolution_pass` is a small DAG executed by `ciao/dag.py`, so each step (proposal generation, semantic check, test gate, write) shows up in the Automation page with its own timing and error.

Each session injects a bounded memory layer at the top of the system prompt: `~/.ciao/memory.md` (env facts, conventions, lessons) and `~/.ciao/user.md` (identity, preferences) are rendered as a labeled block by `ciao/memory_injector.py`. `ciao/memory_tool.py` owns validated, char-limited CRUD; the legacy CLI and the embedded Ciaobot MCP memory tools call that same implementation. `ciao/memory_proposals.py` mines archived `## Session insights` into the vault's `Workspace/Memory-Proposals.md` for human or next-session promotion. Daily memory curation also appends durable learnings to the vault's `Workspace/Learnings.md`; the weekly review promotes recurring high-confidence entries into CLAUDE.md rules.

The server registers a `UserPromptSubmit` hook (`ciao/observability/hooks.py`) that injects today's date, the per-turn `CIAO_ACTIVE_WORKSPACE`, GWS profile, and any vault entities mentioned in the prompt; entity matching is index-backed via `INDEX.md` under the vault root and is scoped to the active workspace plus `shared/...` roots.

## Schedules and background automation

Schedule dispatch (`ciao/schedules.py`) runs cron-shaped jobs that use the same provider pipeline as a chat turn. Every schedule resolves to one workspace, either through its target project/chat or its persisted `workspace` assignment. Empty `provider` and `model` values remain dynamic: each run inherits the selected workspace's current defaults, while a fixed-chat target inherits that chat. The Automations view is workspace-scoped, and built-in routines expose a workspace assignment without making their packaged prompt editable. System schedules ship in `ciao/stock/schedules.json` (memory curation, skill evolution, weekly review); user schedules live in the workspace's `.runtime/schedules.json`, while mutable built-in state (including workspace assignment) lives in `.runtime/system_schedules_state.json`. On startup, each enabled schedule whose latest expected occurrence was missed runs once immediately; Ciaobot does not replay every skipped interval or alter the prompt with the missed occurrence's date. Dependency review and runtime error triage are workspace-local schedules for operators who maintain an app checkout (see `ciao/dependency_review.py`). Schedule prompts support two placeholders substituted at dispatch: `{{ERROR_LOG}}` (server error-log tail) and `{{ISSUE_REPORT}}` (error log plus failed job runs); the error log is cleared only after a clean run.

Loops (`ciao/loops.py`) are the sub-day sibling of schedules: a loop is bound to one existing PWA chat and re-dispatches its prompt every N minutes (floor: 1), keeping the conversation and its context going — e.g. "check my PRs for review changes every 10 minutes". Entries live in `.runtime/loops.json`; runtime start/stop state lives in the `LoopManager`, so a loop with `autostart: true` begins running at server boot while the rest stay stopped until started from the Automations page. Iterations always run with the target chat's own model and mode (loops never override them), overlap protection is skip-not-queue (an iteration due while the chat still has a turn in flight is skipped and retried on the next ~20s tick), and there is no downtime catch-up — cadence simply resumes. The PWA's Schedules page is titled "Automations" and hosts both schedules and loops (`GET/POST /api/loops`, `PATCH/DELETE /api/loops/{id}`, `POST /api/loop-run/{id}`).

Background automations are instrumented through `ciao/job_runs.py`: title generation, schedule dispatch, session insights, memory proposals, trajectory capture, weekly skill evolution, weekly dependency review, and startup/system tasks (git sync, vault index refresh, PWA rebuild, skills update, device-branch backup) each record one run (status, duration, model/provider, error) to `.runtime/job_runs.jsonl`. `GET /api/automation` serves the grouped view that powers Settings → Automation.

Schedules carry an `archive_policy`: `manual` or `auto`. All schedules execute through the normal chat pipeline for permissions and transcripts. `auto` runs a post-run classifier and archives only when the user does not need to see the result; failed, permission-blocked, retrying, or useful runs stay visible.

**Automatic behavior.** Ciaobot automatically archives chat transcripts, runs the configured session-insights model on archived session JSONL when insights are enabled, appends `## Session insights`, records a structured trajectory JSON, and then runs a heuristic pass over those insights to draft memory proposals. It also dispatches enabled schedules, records background-job telemetry, refreshes the vault index, updates configured skill mirrors, and backs up the device branch according to the startup/system automation configuration. The Settings → Models page only exposes routines that have a selectable model; heuristic follow-on jobs such as memory proposals and trajectory capture are tracked in Settings → Automation instead.

**Not automatic.** Memory proposals are not promoted into `~/.ciao/memory.md` or `~/.ciao/user.md` without review. Skill-evolution output is a draft proposal under the vault's `Workspace/Skill-Proposals/`; Ciaobot does not apply those edits automatically. Scheduled chats still run through the normal provider pipeline and permission flow. A schedule with `archive_policy: auto` is archived only when the post-run classifier says the result does not need user attention; failed, blocked, retrying, or useful runs remain visible. Routine-model choices do not change an active chat's selected provider/model.

## Providers

Dispatch is provider-driven, not model-driven. Each chat and schedule carries an explicit provider and model. `claude` runs Claude Code / Claude Agent SDK and can use Anthropic models directly, Ollama-routed models, or OpenRouter-routed `owner/model` IDs through environment injection. `codex` runs one persistent OpenAI Codex app-server process per active chat over stdio JSON-lines RPC. It uses the user's `codex login` account, discovers models and reasoning efforts from `model/list`, maps Ciaobot modes to Codex sandbox/approval policies, sends native images, and normalizes streaming text, reasoning, tools, token usage, rate limits, approvals, structured questions, stop, steer, thread history, and collab-agent activity into the existing PWA contract. Its `fable` preset resolves to the discovered Sol-family model and always dispatches with Ultra reasoning effort. Ollama support covers cloud models and locally installed daemon models; the Claude route also remaps internal tier/control-plane slots so subagents and auto-mode classifiers stay on Ollama. OpenRouter support is enabled when `OPENROUTER_API_KEY` is set. The backend keeps provider routing explicit so schedules, handovers, and archived chats remain reproducible.

## Frontend

`web/` is a Vue 3 + Vite + Pinia + TypeScript PWA. The hierarchy is workspace → project → chat. Configured logical workspaces live in `CiaoConfig.workspaces` (loaded from `CIAO_WORKSPACES`, `.runtime/workspaces.json`, or legacy personal/work defaults) and are exposed through `GET /api/workspaces`. The Pinia project store loads that endpoint; the sidebar workspace switcher and empty-state General chat buttons render from the configured workspace list. Backend project discovery, completion, restore, schedule routing, spawned-agent `GWS_PROFILE`, `CIAO_ACTIVE_WORKSPACE`, and default model bucket also read from the workspace registry. Model bucket values are strings; the visible picker groups Claude routes by Anthropic, Ollama, and OpenRouter backends, while the backend accepts configured bucket names such as `anthropic` and `ollama`.

Primary navigation uses native button/link semantics with visible keyboard focus, and mobile controls keep a 44px minimum target. The global Cmd/Ctrl+K command palette creates a chat in the current project, searches existing chats, opens Automations or Settings, and toggles the persisted theme. Browser zoom remains enabled; Settings font scaling is an additional preference rather than a replacement for platform accessibility controls. Automation details preserve their title on narrow screens by moving secondary actions into an overflow menu, collapse long prompt previews, and explain stale project/chat targets without exposing raw runtime IDs.

Each workspace has a "General" project plus auto-discovered projects from its configured `vault_root/projects/active/`. Chats stream over WebSocket; voice recording captures audio and previews before transcribing; image uploads and pending comments are scoped to the active chat, then attached to that chat's next prompt. Pinned files are scoped per-chat (persisted in local storage, falling back to project scope if no active chat exists) and render in a split layout sidebar.

Write/Edit/MultiEdit/NotebookEdit tool calls are tagged with `file_touch` by `ciao/web/chat_broker.py` and surface as standalone `_filecard` entries on reload, rendered as inline clickable preview cards that open `FileViewerModal` (see `PWA_API.md` → "File-touch cards" for the WS + `/messages` contract). On every file-touch event the broker also schedules a debounced content snapshot via `ProjectChatManager.snapshots` (`ciao/web/file_snapshots.py`, `SnapshotStore`), which writes append-only per-(chat, file) copies under `.runtime/snapshots/<chat_id>/<urlencoded_path>/NNNN.snap` plus a sibling `meta.json`. The PWA reads these back via `/api/file-history`, `/api/file-content`, and `/api/file-restore` to power the FileViewerModal's Preview / History / Diff tabs and one-click restore; markdown notes also expose a Backlinks tab backed by `/api/vault/backlinks`, using the same relative/path/stem wikilink resolution as the preview. Preview renders text/markdown, images, and `.excalidraw` JSON diagrams via a read-only Excalidraw bridge, while History/Diff remain text-snapshot based. An in-modal Edit mode posts user-edited text content via `POST /api/workspace-file`, which captures a `tool="PWAEdit"` snapshot, and guards dirty edits against close, reload, or opening another file. Snapshots are wiped on chat delete and preserved on archive.

Archived chats are read-only but can be continued into a new active chat via the "Continue in new chat" button (calling `POST /api/chats/{chat_id}/continue`). A header "files touched" chip in `ChatPanel.vue` summarizes the dedupped set of files written/edited in the chat and links each entry back to the modal.

Background subagents (Agent tool dispatches with `run_in_background`) stay visible after the parent turn ends. `ciao/subagent_tracking.py` parses the parent session JSONL for async dispatches and their `<task-notification>` completions; a per-chat watcher in `ProjectChatManager` publishes `chat_subagents_ready {remaining}` on `/ws/events` whenever the running count changes (the connect snapshot carries `background_agents` so reloads heal stale counts). While agents run, the PWA shows a pulsing "N agents" pill in the chat header and a dimmed sidebar dot, polls `/api/chats/{id}/subagents` for a near-live feed, and renders each transcript in a `SubagentPanel` anchored under the dispatching turn via the server-computed `turn_index`. Between turns the manager also drains the idle SDK stream (`ClaudeProvider.drain_events`): when a finished agent triggers a CLI-initiated parent follow-up turn, its events are forwarded live on a background `ChatStream` and announced like a normal result — without the drain those buffered messages would silently corrupt the next turn's `receive_response()`.

Agent handoffs (sub-chats) allow a parent chat agent to spawn and communicate with a second provider route (the participant) as a read-only sub-chat attached to the originating turn. Managed by `ProviderSubchatManager` (`ciao/provider_subchats.py`), sub-chats persist their metadata under `.runtime/provider_subchats/subchats.json` and transcripts as `.jsonl` files in the same directory. The execution loop (`run_consultation_turn`) runs the participant turn in a background worker, enforcing active limit constraints (e.g. 12 messages or 30 minutes, extendable via user authorization). Sub-chats publish state changes and events live to the PWA over the `/ws/events` socket. The PWA displays these in a dedicated, interactive `ProviderSubchatPanel` anchored to the dispatching turn, supporting controls to extend limits, cancel, or close the sub-chat. Any permission or structured question requests emitted by participant tools are intercepted and routed through the panel for the operator to approve or answer.


Settings tabs: Home (deploy, notifications, theme and font scaling, local session, dev-mode Debug card), Models (provider/tier controls for title, insights, critique, Ollama/OpenRouter tier routing, visible main workspace/vault roots, and the voice transcription engine, read/written via `GET`/`PATCH /api/settings/routines`, persisted in `.runtime/app_settings.json` by `ciao/app_settings.py` and overlaid on the live config without a restart), Providers (Claude Code/Codex CLI authentication plus Ollama, OpenRouter, and OpenAI cloud voice credentials), Workspaces, Agent Context (a project/workspace-independent context-building guide ordered to match the provider pipeline: one CLI instructions block — the workspace `CLAUDE.md` and `AGENTS.md` are linked, so there is no per-CLI switch — then Ciaobot system instructions, memory layers, and per-turn context; `workspace_health` and `setup_status` both check that `AGENTS.md` resolves to `CLAUDE.md` and warn without blocking when a custom AGENTS.md diverges), Skills (expandable inventory with Claude/Codex install badges), and Automation (per-process status, last-run time, duration, model/provider, recent history, and error text, split into Content automations and System, fed by `GET /api/automation`). `GET /api/agent-assets` surfaces the underlying context inventory with provider/workspace metadata plus generated Ciaobot system/runtime prompt blocks, subagents, and slash commands; draft memory proposals remain available to backend workflows but are not shown as a Context-tab review queue. `POST /api/agent-assets/subagents` and `POST /api/agent-assets/commands` create workspace-owned assets under `subagents/` or `commands/`, mirror a note into `memory-vault/Workspace/`, and sync Claude `.claude/` links, Codex `.agents/skills/` compatibility wrappers, and native `.codex/agents/` registrations.

When a chat reply, approval request, or model question needs the user's attention, `PushManager` appends its payload to `.runtime/notifications.jsonl` before attempting Web Push. The macOS menu-bar companion tails that bounded log, starting at its current end on launch so it never replays old alerts, and posts native notifications for new entries. Its regular refresh still derives the unread menu and badge from `.runtime/web_projects.json`.

Rendered markdown is sanitized through the shared frontend renderer before any `v-html` use. Keep new markdown surfaces on that helper. Completed chat traces render as compact `Activity` disclosures; deduplicated file touches move to an `Outputs` group below the final answer, while interrupted turns retain them inside the trace. The build outputs to `ciao/web/static/` so the same Starlette server hosts both the API and the PWA.

## Workspace git sync

Every instance is identical (no primary/secondary, no cloud). Sync and branch backup operate on the repo containing the vault root (`ciao/local_session.py::sync_root`): with the default layout (vault inside the workspace) that resolves to the workspace repo; a vault that lives elsewhere in its own repo is synced there instead, and a missing/non-git vault falls back to the workspace root. Ciaobot never creates or switches local branches: it works on whatever branch that checkout is currently on (`ciao/local_session.py::workspace_branch`). A background loop pushes that branch for backup; roots that are not git repositories or have no `origin` remote skip all git background work with a single INFO log (a fresh `ciao setup` initializes local repos, but they have no remote until the user adds one).

**Sync flow** (`ciao/local_session.py`, `LocalSessionManager`). When the user clicks "Sync with Remote" in Settings, `POST /api/local/handback` commits pending work, pulls from origin (merge-based), and pushes the current branch. Workspace sync never requests app deploy; app updates happen through package upgrades. A **conflicting pull** is left in the tree and `POST /api/handover/merge` opens an **interactive chat** in the personal `General` project: the chat's agent resolves the conflicts, asking the user via `AskUserQuestion` (push-notified) when ambiguous, then pushes the branch (it does not redeploy). After that chat lands, `POST /api/local/resync` merges `origin/<branch>` into the checkout after committing any pending work (fast-forwarding in the normal case). It merges rather than force-resetting, so it works on the live workspace's dirty tree and never discards local commits. See `PWA_API.md` → "Workspace git sync".

## Package updates and setup

**Package updates.** App code is a pip package (or a Homebrew formula on macOS), so workspace git handback never implies a deploy. `GET /api/package/status` reports `ciao.__version__` and a best-effort latest version from the GitHub releases API (`CIAO_GITHUB_REPO` overrides the repo), and `POST /api/package/update` upgrades based on the active installation mode: `brew upgrade ciaobot` when installed from the [homebrew-ciaobot](https://github.com/raffaelefarinaro/homebrew-ciaobot) tap, or the `.whl` asset of the latest GitHub release in a pip venv. The Settings PWA panel handles upgrades seamlessly. Pip upgrades never install from the PyPI index directly in-app (the release wheel is the source of truth); editable git checkouts must use `git pull`.

**Setup readiness.** When `PWA_AUTH_TOKEN` is absent, Ciaobot starts in bootstrap mode using `~/.ciao/bootstrap` (or `CIAO_BOOTSTRAP_WORKSPACE`) and persists a generated temporary auth token under that workspace's `.runtime/`. `GET /api/setup-status` is public like startup status, because the first-run wizard needs it before a normal session exists. It reports workspace/vault/token/push-contact checks plus Claude Code, Codex, Ollama, and OpenRouter readiness probes, without returning secret values. Codex install/auth checks use bounded `codex --version` and `codex login status` subprocesses and generate the installed app-server JSON schema into a temporary directory to fail closed when required thread, turn, approval, question, usage, skills, or collaboration methods are missing; `ciao auth codex` launches the native login flow, with `--device-auth` available for headless machines. The local first-launch URL `/?setup=<token>` redeems `.runtime/setup-token` on localhost only, sets the signed PWA session cookie, deletes the one-time token, and redirects to `/`. The wizard finishes with `POST /api/setup/finish`, which is bootstrap-only and localhost-only: `workspace` is required (the wizard's primary folder question), the chosen provider is persisted on the first logical workspace, and `vault_root` is optional, defaulting to `<workspace>/memory-vault`. It writes the real workspace `.env`, scaffolds the configured vault root and provider asset catalogs, ensures workspace and vault are (in) git repositories with their protective `.gitignore`s (see "Workspace layout"), creates the server and menu bar LaunchAgents plus the local `Ciaobot Server.app` recovery launcher, then requests the normal restart exit so launchd can relaunch into the real workspace. The PWA is installed as `Ciaobot`; the native launcher keeps the stable `local.ciaobot.app` bundle identifier but uses a distinct indigo terminal icon so macOS presents the UI and service roles separately. The menu bar reads and toggles the two launchd labels as one visible Start at Login setting. A foreground `ciao run` supervises that exit itself: when the server exits with the restart code, the CLI re-execs a fresh `ciao run` (picking up newly installed code) instead of dying.

## Project naming convention

Every configured workspace uses the same shape: a project is a folder under `<workspace.vault_root>/projects/active/<name>/` with a same-named main doc `<name>/<name>.md` (or `README.md`). On completion it moves to `<workspace.vault_root>/projects/completed/<name>/` via the PWA's "Complete" button (or `complete_project()`).

Completed projects can be restored. The sidebar footer has an archive icon next to "+ New Project" that opens a modal listing the current workspace's completed projects (`ProjectChatManager.list_completed_projects()` scans the `projects/completed/` tree read-only). Each entry has a Restore button: `restore_project(workspace, stem)` moves the folder back to `projects/active/`, flips frontmatter `status: completed` back to `active`, and re-runs auto-discovery to recreate the PWA project. Auto-discovery derives a stable id from the workspace and folder slug; the original chats stay archived. Routes: `GET /api/projects/completed` (optional `?workspace=`) and `POST /api/projects/completed/restore` (see `PWA_API.md`).

- **Folder name** is the slug used internally (`vault_folder`). Lowercase + kebab-case is preferred but not enforced; spaces, mixed case, and underscores all work. Avoid path separators and leading dots.
- **PWA display name** comes from frontmatter `name:` (or `title:` if `name:` is absent), so it can be any human-friendly string with spaces (e.g. `name: AI Championship Project`). If both are missing, the folder name is used as the label.
- Top-level reference docs that live directly under `projects/` never "complete"; don't put real projects there.
- A stray `projects/active/<Name>.md` at the top level gets auto-promoted to `<Name>/<Name>.md` on the next server start, so the folder+file invariant always holds.
- Every workspace has an auto-created `General` project bound to `projects/active/general/`. It is marked `is_auto` and displays the "auto" chip in the sidebar. It's where ad-hoc chats land and where scheduled automations run; don't delete it.

## Related docs

- `docs/DEVELOPMENT.md`: setup, dev workflow, testing, change guidelines.
- `INTEGRATIONS.md`: env vars, OAuth setup, MCP connectors, server runtime knobs.
- `PWA_API.md`: API endpoints, auth flow, state paths.
- `docs/MCP.md`: agent control-plane security, tool catalog, provider process configuration, and numeric release evaluation.
- `web/README.md`: PWA frontend dev workflow, iOS Safari gotchas, design system tokens.
