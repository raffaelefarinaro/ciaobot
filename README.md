# Ciaobot

Ciaobot is an opinionated UI and UX layer for using Claude Code as a personal assistant and second brain. It is a local web app around agentic work: chats, projects, files, schedules, memory, and archived knowledge all live in one interface instead of being scattered across terminal sessions.

You can use it with the access you already have:

- Claude Code through your Claude subscription or Anthropic API key.
- Ollama Cloud, a local Ollama daemon, or compatible Ollama model routing.
- OpenRouter through an `OPENROUTER_API_KEY`.

The product model is project-first. A workspace can represent a life area such as personal, work, or a client. Each workspace contains projects. Each project has files, notes, decisions, and context that Ciaobot injects when you work inside that project, so the agent does not need to rediscover what you are talking about every time.

What Ciaobot does:

- Runs Claude Code-backed chats in a PWA with project and workspace navigation.
- Lets you create, preview, edit, and restore workspace files from the UI.
- Lets you schedule project or workspace routines to run when you choose.
- Archives chats into a markdown vault, then extracts session insights and drafts memory proposals for review.
- Keeps durable project context separate from short-lived chat state.
- Supports voice transcription, push notifications, model/provider settings, and local package updates from the UI.

What it does not do automatically:

- It does not promote memory proposals into your long-term memory files without review.
- It does not discard or rewrite an existing notes folder during onboarding.
- It does not make the provider choice permanent; chats and routines can route through configured backends.

Naming note: the user-facing product is **Ciaobot**. The Python package, import path, CLI command, and many environment variables are still named `ciao`/`CIAO_*` for compatibility.

## For Claude

**Read this file before making any change in `ciao/`, `web/`, `scripts/`, or `deploy/`.** It is the single canonical orientation doc for the codebase.

After the change, dispatch the `doc-updater` agent to keep this file truthful. CLAUDE.md is loaded on every prompt and stays focused on behavior; this README carries the architecture and dev workflow so it can be loaded on demand.

## What you need

### Required

- **Claude Code**: open this repository as the project so it picks up `CLAUDE.md`, `.claude/agents/`, `.claude/skills/`, and `.claude/commands/` (these are generated symlinks synced from the canonical source folders `subagents/`, `skills/`, and `commands/` by `ciao sync-skills` on startup).

### Per capability (optional)

- **Google Workspace** (calendar, email, tasks, etc.): uses the [gws CLI](https://github.com/googleworkspace/cli). Install: `npm install -g @googleworkspace/cli`. Auth and OAuth client setup: see `INTEGRATIONS.md`.
- **Apple Intelligence** (title generation): optional. The `apfel` CLI provides local-first chat title generation using macOS on-device models. See `INTEGRATIONS.md`.
- **Other CLIs and MCP connectors** (notebooklm, opencli, BigQuery, Airtable, Slack, Atlassian, Zoom): see `INTEGRATIONS.md`.

## Repo layout

```
CLAUDE.md                      Behavior, identity, vault-first, delegation, writing style. Loaded every prompt.
README.md                      This file. Architecture and dev workflow. Read on demand.
INTEGRATIONS.md                Operator config (env vars, OAuth, MCP connectors, server runtime knobs).
PWA_API.md                     PWA API reference (endpoints, auth flow, state path).

ciao/                          Python backend (Starlette).
  main.py                      Web server entry point, route wiring, startup hooks.
  cli.py                       Packaged `ciao` CLI: server, setup, dev, sync-skills, vault, memory, chat, public-preflight, and package-smoke commands.
  dev.py                       Local dev runner: backend on :8543 plus Vite frontend on :5173. CLI: `ciao dev`.
  config.py                    Env var loading, workspace config.
  providers/                   Provider implementations: Claude Agent SDK (with Ollama env-injection route) and Pi subprocess.
  context/                     Per-turn context injection (workspace, project, vault hints).
  observability/               Hooks: UserPromptSubmit (runtime context + vault entity tags) and PostToolUse (WebSearch backfill on Ollama-cloud routes). Plus logging, transcript capture.
  schedules.py                 Cron-style schedule dispatch.
  dag.py                       Tiny DAG runner (Node kinds: bash / prompt / gate / subagent / retention; edges: ok / fail / always). Each node is timed via `job_runs.track_sync`, so Automation page shows per-node status. Used by `sched-skillevo` (`skill_evolution.py`) and `sched-depcheck` (`dependency_review.py`).
  sessions.py                  Session state, auth, signed cookies, JSON-backed StateStore for `.runtime/state.json`.
  transcripts.py               Archive Claude Code JSONL into memory-vault/Logs/Chats/.
  upgrade.py                   Self-update / deploy flow.
  voice.py                     Voice transcription: cloud (OpenAI) and local (mlx-whisper) engines, selected by `CIAO_TRANSCRIPTION_ENGINE`.
  app_settings.py              Runtime-mutable app settings (title, insights, and critique models, transcription engine/model), persisted at `.runtime/app_settings.json` and overlaid on CiaoConfig. Edited via Settings → Models.
  error_log.py                 Rotating file handler for server ERROR+ logs. Consumed by the weekly error-log schedule.
  job_runs.py                  Fail-open recorder for background-job runs. Appends one JSON record per run to `.runtime/job_runs.jsonl` (size-trimmed like `error_log.py`). Background tasks wrap their work in `track()`/`track_sync()`; `automation_summary()` feeds the Settings → Automation page.
  models.py                    Shared data models (ChatContext, AgentRequest, etc.).
  provider_service.py          Provider request builder and execution wrapper.
  signals.py                   Restart / deploy signals.
  execution_modes.py           Claude permission mode normalization.
  git_sync.py                  Startup git pull / merge-before-push helpers.
  local_session.py             Branch-per-instance local dev flow (LocalSessionManager): run on a `local` branch, hand back via PR. Secondary only.
  rate_limits.py               Rate-limit tracking and persistence.
  insights.py                  Post-archive session insights extraction.
  fts_search.py                SQLite FTS5 full-text indexing and search for vault and transcripts.
  vault_index.py               Build/query memory-vault/INDEX.md from frontmatter and wikilinks. CLI: `ciao vault-index`.
  vault_lint.py                Programmatic linter and wikilink validation for the memory-vault.
  trajectory_builder.py        Parse filtered session JSONL into a structured trajectory (turns, tools, skills, errors) and persist to ~/.ciao/trajectories/YYYY-MM/<session-id>.json. CLI: `python3 -m ciao.trajectory_builder --list [...]`.
  skill_evolution.py           Weekly pass: mine trajectories, flag skills tied to non-success sessions, draft edit proposals to memory-vault/personal/Workspace/Skill-Proposals/. 15KB skill cap, optional test gate, no auto-apply. `run_evolution_pass` is now async and builds a per-skill DAG (has_proposal → semantic → tests → write, with a write_stub fallback for over-cap skills) via `ciao/dag.py`; the function signature and returned `list[Path]` are unchanged.
  dependency_review.py         Weekly dependency-changelog review as a `ciao/dag.py` pipeline (read_baseline gate → installed bash → research subagent fanning out 7 GitHub release checks → write_baseline gate). Trigger: `python3 -m ciao.dependency_review [--model sonnet] [--dry-run]`. Merges and gates the write to `.runtime/dependency_baseline.json` so a flaky run can't corrupt it. `--model sonnet` is rewritten to `$CIAO_OLLAMA_SONNET_MODEL` when set, so scheduling with the tier alias reaches the configured Ollama model (the bundled CLI's `sonnet` alias resolves to `claude-sonnet-4-6`, which the Ollama proxy doesn't serve).
  skills_inventory.py          Build the Settings skill inventory from skills/ and skills-lock.json, with installed mirror badges for Claude/Pi.
  sync_skills.py               Install and mirror workspace skills, commands, and agents. CLI: `ciao sync-skills`.
  sync_agents_to_pi.py         Translate subagents into Pi-compatible agent files. CLI: `ciao sync-agents-to-pi`.
  skills_sync.py               Change-detect upstream skills in skills-lock.json so `ciao sync-skills` updates only moved repos. CLI: `ciao skills-sync`.
  cleanup_sdk_blobs.py         Maintenance: drop archived Claude SDK JSONL blobs. CLI: `ciao cleanup-sdk-blobs`.
  memory_injector.py           Read ~/.ciao/memory.md and ~/.ciao/user.md at session start, render as a system-prompt block.
  memory_tool.py               Bounded memory files + in-process SDK MCP server exposing the `memory` tool (add/replace/remove/read).
  memory_proposals.py          Scan archived `## Session insights` sections, propose memory entries to memory-vault/personal/Workspace/Memory-Proposals.md.
  public_release.py            Public extraction allowlist, export copier, and private-data preflight scanner. CLI: `ciao public-preflight export <src> <dest>` then `ciao public-preflight scan <export-root> --private-patterns <file>`.
  package_smoke.py             Wheel smoke target: build web, build wheel, install in a clean venv, and probe the installed app. CLI: `ciao package-smoke`.
  stock/                       Generic package-data assets for public installs: stock agents, commands, empty skills, launchd template, and system schedules.
    workspace/                 Agent-readable docs copied into installed workspaces (`CLAUDE.md`, `CIAO_CUSTOMIZATION.md`).
  web/                         PWA web server (Python routes) + static assets (built by web/).

web/                           Vue 3 PWA frontend.
  src/App.vue                  Root component.
  src/main.ts                  App bootstrap.
  src/router.ts                Vue Router config.
  src/components/              UI components (chat, projects, settings, voice, etc.).
  src/stores/                  Pinia stores.
  src/lib/                     Helpers (API client, formatters, etc.).
  package.json                 npm deps and scripts (build, typecheck, test). Includes a small React bridge for the Excalidraw file previewer.
  vite.config.ts               Vite config.
  tsconfig.json                TypeScript config.
  index.html                   PWA entry HTML.
  Vite + TypeScript. Build outputs to ciao/web/static/.

scripts/
  README.md                    Scripts directory orientation.
  run-ciao.sh                  Start the Ciaobot server in dev.
  dev.sh                       Compatibility wrapper for `ciao dev`.
  create-chat.py               Compatibility wrapper for `ciao create-chat`.
  ensure-deps.sh               Dependency verification and venv repair (macOS libexpat and SSL workarounds). Sourced by run-ciao.sh and dev.sh.
  gws-profile.sh               Switch GWS profile (personal | work). Uses exec, do NOT source.
  gws-personal.sh              Wrapper to run gws commands for the personal profile.
  gws-work.sh                  Wrapper to run gws commands for the work profile.
  gws-auth-helper.py           Interactive headless OAuth re-auth for gws (personal | work).
  gws-secrets.py               Backup and restore GWS credentials to/from .env in base64.
  install-custom-skills.sh     Compatibility wrapper for `ciao sync-skills`.
  skills_sync.py               Compatibility wrapper for `ciao skills-sync`.
  sync-claude-agents-to-pi.py  Compatibility wrapper for `ciao sync-agents-to-pi`.
  vault_index.py               Compatibility wrapper for `ciao vault-index`.
  vault-search.py              Compatibility wrapper for `ciao vault-search`.
  vault-lint.py                Compatibility wrapper for `ciao vault-lint`.
  work_chat_transcripts.py     Filter archived chats to work-workspace only.
  memory-cli.py                Compatibility wrapper for `ciao memory`. Used by subagents that cannot load the in-process MCP server.
  morning-briefing.py          Helper invoked by the work morning action briefing schedule (Phase -1). Combines MeteoSwiss weather with all three calendars (Personal, Work, Faraman).
  cleanup_sdk_blobs.py         Compatibility wrapper for `ciao cleanup-sdk-blobs`.
  backfill_insights.py         One-off: backfill session insights for existing archives.

deploy/
  com.ciao.server.plist        Generic launchd template mirror.
  homebrew/ciao.rb             Homebrew formula scaffold.
  README.md                    macOS launchd/template notes.

skills/                        Canonical source for custom skills; `ciao sync-skills` syncs to .claude/skills/ and ~/.pi/agent/skills/.
subagents/                     Canonical source for project agents (memory, secretary, researcher, doc-updater, comment-analyzer, pr-test-analyzer, silent-failure-hunter); synced to .claude/agents/ and ~/.pi/agent/agents/.
commands/                      Canonical source for slash commands (/remember, /critique, /interrogation); synced to .claude/commands/ and ~/.pi/agent/prompts/.
.claude/{skills,agents,commands}/  Generated symlinks/mirrors (gitignored). Edit the canonical sources above, not these.
.claude/skills/                Installed skills (managed; do not edit by hand).
memory-vault/                  Durable memory.
  MEMORY.md                    Curated personal priorities.
  INDEX.md                     Auto-generated frontmatter index (regenerated on server startup).
  People/  Projects/  Ideas/  Resources/  Places/  Workspace/  Documents/  Templates/  Logs/Chats/  Logs/Meetings/
  Work/                        Work context.
    MEMORY.md                  Business context (stakeholders, products, projects, patterns).
    products/  features/  references/  journal/daily/  journal/sprints/  automations/
    projects/active/           In-progress work projects (folder + same-named .md file).
    projects/completed/        Done projects.

tests/                         pytest suite for the Python backend.
.runtime/                      Local state (schedules.json, web_projects.json, topic_names.json, tasks/registry.json, dev-session.json, snapshots/<chat_id>/<urlencoded_path>/NNNN.snap, etc.). Not committed.
docs/                          Long-form internal docs.
pyproject.toml                 Python package metadata, package-data declarations, and dev/test deps.
```

## Setup

### Server install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
ciao setup --workspace /tmp/ciao-workspace
ciao run
```

`ciao setup` is idempotent. It writes the initial `.env`, seeds stock workspace files, copies agent-readable workspace docs (`CLAUDE.md`, `CIAO_CUSTOMIZATION.md`), renders `~/Library/LaunchAgents/com.ciao.server.plist`, and creates `~/Applications/Ciaobot.app`, which opens the local PWA. By default it does not load launchd; add `--load-launchd` when you want setup to run `launchctl`.

The Homebrew formula scaffold lives at `deploy/homebrew/ciao.rb`. It is a `--HEAD` formula until the public repo cuts a tagged release with a stable source URL and SHA. It installs Ciaobot into a `python@3.12` virtualenv, links the `ciao` CLI, and runs `ciao setup --load-launchd` from `post_install` only when a GUI launchd session is available. Headless installs print the terminal command to finish setup instead of failing silently.

Common package CLI entry points:

```bash
ciao setup --workspace ~/ciao --load-launchd
ciao memory read --target memory
ciao vault-index --workspace default --format json
ciao vault-search "project keyword" --limit 5
ciao vault-lint --vault-root memory-vault
ciao create-chat --prompt "Start here" --workspace default
ciao cleanup-sdk-blobs --workspace .       # dry-run by default
ciao dev                                   # backend :8543 + Vite :5173
ciao public-preflight export . /tmp/ciao-public-export
ciao public-preflight scan /tmp/ciao-public-export --private-patterns /tmp/private-patterns.txt
ciao package-smoke --skip-frontend
ciao auth claude --print-only              # show terminal OAuth command
ciao auth ollama                           # run provider login helper
```

On recent macOS, `scripts/run-ciao.sh` and the `scripts/dev.sh` wrapper still source `scripts/ensure-deps.sh`, which injects two self-healing workarounds into `.venv/bin/activate`:

- A `DYLD_LIBRARY_PATH` pointing at Homebrew's `libexpat`. macOS 26+ ships a system `libexpat` missing a symbol Homebrew Python's `pyexpat` needs, which otherwise crashes pip and venv creation with `ImportError: ... Symbol not found`.
- An `SSL_CERT_FILE` pointing at the venv's certifi CA bundle. The python.org Python build ships no CA bundle, so the bare `urllib` calls in the server (e.g. OAuth token refresh in `ciao/web/auth.py`) fail with `SSLCertVerificationError`.

No manual step is needed; `ensure-deps.sh` handles both. If you set up the venv by hand and hit either error, run `scripts/ensure-deps.sh` once to repair `activate`.

### Optional Node tooling

```bash
npm install
cd web
npm install
npm test
npm run build
```

### Frontend build

```bash
cd web
npm install
npm run build      # typecheck + Vite build, outputs to ciao/web/static/
```

After PWA changes, rebuild and either restart the service or use the **Deploy** button in PWA Settings. **Never restart the ciao service from inside the PWA chat** (you'd sever your own session); ask the operator to deploy.

## Architecture overview

**`ciao/`** is a Starlette web server that mounts the PWA frontend, exposes a JSON API for projects/chats/schedules/repos, and spawns provider subprocesses for each chat turn. Auth is a pre-shared token (`PWA_AUTH_TOKEN`) traded for a signed session cookie. Operational state lives in `.runtime/` under `CIAO_WORKSPACE`; durable memory lives under `CIAO_VAULT_ROOT` (default `<CIAO_WORKSPACE>/memory-vault`). Chat transcripts are streamed via WebSocket and archived to `Logs/Chats/<chat-id>/claude/` under the vault root (`transcripts.py`). When a chat is archived, the raw session JSONL is filtered and passed through a fast model to extract durable insights (errors, dead ends, new entities, decisions, reusable code) which are appended as a `## Session insights` section; this section is the preferred input for memory curation and downstream automation (`ciao/insights.py`, gated by `CIAO_INSIGHTS_DISABLED`, `CIAO_INSIGHTS_MIN_TURNS`, `CIAO_INSIGHTS_MODEL`). In the same archive hook, `ciao/trajectory_builder.py` writes a structured trajectory JSON (turns, tool counts, skills loaded, errors) to `~/.ciao/trajectories/YYYY-MM/<session-id>.json`; a weekly schedule runs `ciao/skill_evolution.py` to mine those trajectories and draft skill-edit proposals under `memory-vault/personal/Workspace/Skill-Proposals/` (gated by `CIAO_TRAJECTORIES_DISABLED`, `CIAO_TRAJECTORY_RETENTION_MONTHS`, `CIAO_SKILL_EVOLUTION_DISABLED`, `CIAO_SKILL_EVOLUTION_MODEL`). The per-skill pipeline inside `run_evolution_pass` is now a small DAG executed by `ciao/dag.py`, so each step (proposal generation, semantic check, test gate, write) shows up in the Automation page with its own timing and error. Each session also injects a bounded memory layer at the top of the system prompt: `~/.ciao/memory.md` (env facts, conventions, lessons) and `~/.ciao/user.md` (identity, preferences) are rendered as a labeled block by `ciao/memory_injector.py`, while `ciao/memory_tool.py` registers an in-process MCP server exposing a single `memory` tool the agent uses to add/replace/remove/read entries within char limits; `ciao/memory_proposals.py` mines archived `## Session insights` into `memory-vault/personal/Workspace/Memory-Proposals.md` for human or next-session promotion. The server registers a `UserPromptSubmit` hook (`ciao/observability/hooks.py`) that injects today's date, the per-turn `CIAO_ACTIVE_WORKSPACE`, GWS profile, and any vault entities mentioned in the prompt; entity matching is index-backed via `INDEX.md` under the vault root and is scoped to the active workspace plus `shared/...` roots. When the model calls `AskUserQuestion` (which the headless CLI can't render, and would otherwise auto-answer empty), `ProjectChatManager._drive` interrupts the turn so generation stops at the question; the PWA surfaces it in its own picker (persisted as `ChatInfo.pending_question` so it survives a reload) and the user's reply resumes the session as a fresh turn. The Claude Agent SDK is configured in `ciao/providers/claude.py` with a `fallback_model` chain (Opus to Sonnet to Haiku) and `setting_sources=["user", "project", "local"]` so `.claude/skills/`, `.claude/agents/`, and `.claude/commands/` auto-discover. Schedule dispatch (`ciao/schedules.py`) runs cron-shaped jobs that spawn the same provider subprocess pipeline as a chat turn. Background automations are instrumented through `ciao/job_runs.py`: title generation, schedule dispatch, session insights, memory proposals, trajectory capture, weekly skill evolution, weekly dependency review, monthly adoption report, and the startup/system tasks (git sync, vault index refresh, PWA rebuild, skills update, system upgrade, device-branch backup) each record one run (status, duration, model/provider, error) to `.runtime/job_runs.jsonl`. `GET /api/automation` serves the grouped view that powers Settings → Automation. Schedules carry an `archive_policy`: `manual` or `auto`. All schedules still execute through the normal chat pipeline for permissions and transcripts. `auto` runs a post-run classifier and archives only when the user does not need to see the result; failed, permission-blocked, retrying, or useful runs stay visible.

**Automatic behavior.** Ciaobot automatically archives chat transcripts, runs the configured session-insights model on archived session JSONL when insights are enabled, appends `## Session insights`, records a structured trajectory JSON, and then runs a heuristic pass over those insights to draft memory proposals. It also dispatches enabled schedules on the device where `CIAO_DISPATCH_SCHEDULES` is set, records background-job telemetry, refreshes the vault index, updates configured skill mirrors, and backs up the device branch according to the startup/system automation configuration. The Settings -> Models page only exposes routines that have a selectable model; heuristic follow-on jobs such as memory proposals and trajectory capture are tracked in Settings -> Automation instead.

**Not automatic.** Memory proposals are not promoted into `~/.ciao/memory.md` or `~/.ciao/user.md` without review. Skill-evolution output is a draft proposal under `memory-vault/personal/Workspace/Skill-Proposals/`; Ciaobot does not apply those edits automatically. Scheduled chats still run through the normal provider pipeline and permission flow. A schedule with `archive_policy: auto` is archived only when the post-run classifier says the result does not need user attention; failed, blocked, retrying, or useful runs remain visible. Routine-model choices do not change an active chat's selected provider/model.

**Providers.** Dispatch is provider-driven, not model-driven. Each chat and schedule carries an explicit provider and model. The main provider is `claude`, which runs Claude Code / Claude Agent SDK and can use Anthropic models directly, Ollama-routed models, or OpenRouter-routed `owner/model` IDs through environment injection. Ollama support covers cloud models and locally installed daemon models; OpenRouter support is enabled when `OPENROUTER_API_KEY` is set. Alias tiers such as `haiku`, `sonnet`, and `opus` can resolve differently per workspace model bucket, so a personal workspace can use Ollama while a work workspace keeps Anthropic aliases. The picker and routine settings expose the available Anthropic, Ollama, and OpenRouter options, while the backend keeps provider routing explicit so schedules and archived chats remain reproducible.

**`web/`** is a Vue 3 + Vite + Pinia + TypeScript PWA. The hierarchy is workspace -> project -> chat. Configured logical workspaces live in `CiaoConfig.workspaces` (loaded from `CIAO_WORKSPACES`, `.runtime/workspaces.json`, or legacy personal/work defaults) and are exposed through `GET /api/workspaces`. The Pinia project store loads that endpoint; the sidebar workspace switcher and empty-state General chat buttons render from the configured workspace list. Backend project discovery, completion, restore, schedule routing, spawned-agent `GWS_PROFILE`, `CIAO_ACTIVE_WORKSPACE`, and default model bucket also read from the workspace registry. Model bucket values are strings; the visible picker still has three routes, Claude Work, Claude Personal, and Pi Personal, while the backend accepts configured bucket names such as `anthropic` and `ollama`. Each workspace has a "General" project plus auto-discovered projects from its configured `vault_root/projects/active/`. Chats stream over WebSocket; voice recording captures audio and previews before transcribing; image uploads and pending comments are scoped to the active chat, then attached to that chat's next prompt. Pinned files are scoped per-chat (persisted in local storage, falling back to project scope if no active chat exists) and render in a split layout sidebar. If a turn fails before any provider progress with a quota or session-limit error, `ProjectChatManager` stores the last prompt and schedules an hourly deferred retry; the chat view and sidebar surface the pending state, and `/api/chats/{chat_id}/retry` lets the UI set, stop, or run that retry immediately. Write/Edit/MultiEdit/NotebookEdit tool calls are tagged with `file_touch` by `ciao/web/chat_broker.py` and surface as standalone `_filecard` entries on reload, rendered as inline clickable preview cards that open `FileViewerModal` (see `PWA_API.md` → "File-touch cards" for the WS + `/messages` contract). On every file-touch event the broker also schedules a debounced content snapshot via `ProjectChatManager.snapshots` (`ciao/web/file_snapshots.py`, `SnapshotStore`), which writes append-only per-(chat, file) copies under `.runtime/snapshots/<chat_id>/<urlencoded_path>/NNNN.snap` plus a sibling `meta.json`. The PWA reads these back via `/api/file-history`, `/api/file-content`, and `/api/file-restore` to power the FileViewerModal's Preview / History / Diff tabs and one-click restore; Preview renders text/markdown, images, and `.excalidraw` JSON diagrams via a read-only Excalidraw bridge, while History/Diff remain text-snapshot based. An in-modal Edit mode posts user-edited text content via `POST /api/workspace-file`, which captures a `tool="PWAEdit"` snapshot. Snapshots are wiped on chat delete and preserved on archive. Archived chats are read-only but can be continued into a new active chat via the 'Continue in new chat' button (calling `POST /api/chats/{chat_id}/continue`). A header "files touched" chip in `ChatPanel.vue` summarizes the dedupped set of files written/edited in the chat and links each entry back to the modal. Settings has four tabs: Home (deploy, notifications, instance toggle, theme selection and font scaling, local session), Models (provider/tier controls for title, insights, skill-evolution, critique, Ollama/OpenRouter alias models, visible main workspace/vault roots, and the voice transcription engine, read/written via `GET`/`PATCH /api/settings/routines`, persisted in `.runtime/app_settings.json` by `ciao/app_settings.py` and overlaid on the live config without a restart), Skills (expandable inventory with install badges), and Automation (per-process status, last-run time, duration, model/provider, recent history, and error text, split into Content automations and System, fed by `GET /api/automation`). The build outputs to `ciao/web/static/` so the same Starlette server hosts both the API and the PWA.

Rendered markdown is sanitized through the shared frontend renderer before any `v-html` use. Keep new markdown surfaces on that helper.

**Per-device working branch.** Every instance is identical (no primary/secondary, no cloud). On startup, each one checks out its own `dev/<device_name>` branch (cut from `origin/main`, reused across restarts, via `ciao/local_session.py::ensure_device_branch`) and works there. `CIAO_DEVICE_NAME` names the device (defaults to the sanitized hostname); a background loop pushes the branch for backup. Only the instance with `CIAO_DISPATCH_SCHEDULES` set (the always-on "main" device) dispatches scheduled automations, so schedules never double-fire when an occasional dev box is also running (`config.dispatch_schedules`).

**Commit-to-main / handover flow** (`ciao/local_session.py`, `LocalSessionManager`). When the user clicks "Commit to main" in Settings, `POST /api/local/handback` commits + pushes the device branch, then tries to merge it into `main`: a **clean merge** is pushed directly (plain git) and the device branch is re-pointed at the merged `main`. Workspace handback never requests app deploy; app updates happen through package upgrades. A **conflicting merge** is aborted and `POST /api/handover/merge` opens an **interactive chat** in the personal `General` project: the chat's agent merges the branch into `main`, resolving conflicts and asking the user via `AskUserQuestion` (push-notified) when ambiguous, then pushes `main` (it does not redeploy). After that chat lands, `POST /api/local/resync` brings the device branch up to the merged `main` by committing any pending work and merging `origin/main` into the branch (fast-forwarding in the normal case). It merges rather than force-resetting, so it works on the live workspace's dirty tree and never discards device-branch commits made after the last push. The same endpoint backs the Settings "Sync to main" button. Driven by the Settings "Commit to main" panel (always shown). See `PWA_API.md` → "Local session / handover flow".

**Package updates.** App code is a pip package after the split, so workspace git handback never implies a deploy. `GET /api/package/status` reports `ciao.__version__` and a best-effort latest version from the package index, and `POST /api/package/update` upgrades the package based on the active installation mode (Homebrew or pip venv) and restarts the server, allowing the Settings PWA panel to handle upgrades seamlessly.

**Setup readiness.** When `PWA_AUTH_TOKEN` is absent, Ciaobot starts in bootstrap mode using `~/.ciao/bootstrap` (or `CIAO_BOOTSTRAP_WORKSPACE`) and persists a generated temporary auth token under that workspace's `.runtime/`. `GET /api/setup-status` is public like startup status, because the first-run wizard needs it before a normal session exists. It reports workspace/vault/token/push-contact checks plus Claude Code, Ollama, and OpenRouter readiness probes, without returning secret values. The local first-launch URL `/?setup=<token>` redeems `.runtime/setup-token` on localhost only, sets the signed PWA session cookie, deletes the one-time token, and redirects to `/`. The wizard finishes with `POST /api/setup/finish`, which is bootstrap-only and localhost-only: it writes the real workspace `.env`, scaffolds the configured vault root, creates the LaunchAgent plist and local `Ciaobot.app` shortcut, then requests the normal restart exit so launchd can relaunch into the real workspace.

**Operator config** (env vars, OAuth client setup, deploy commands, MCP connector setup) lives in `INTEGRATIONS.md`. **API contracts** (endpoints, auth flow, state file paths) live in `PWA_API.md`.

## Project naming convention

Every configured workspace uses the same shape: a project is a folder under `<workspace.vault_root>/projects/active/<name>/` with a same-named main doc `<name>/<name>.md` (or `README.md`). On completion it moves to `<workspace.vault_root>/projects/completed/<name>/` via the PWA's "Complete" button (or `complete_project()`).

Completed projects can be restored. The sidebar footer has an archive icon next to "+ New Project" that opens a modal listing the current workspace's completed projects (`ProjectChatManager.list_completed_projects()` scans the `projects/completed/` tree read-only). Each entry has a Restore button: `restore_project(workspace, stem)` moves the folder back to `projects/active/`, flips frontmatter `status: completed` back to `active`, and re-runs auto-discovery to recreate the PWA project. The recreated project gets a fresh id; the original chats stay archived. Routes: `GET /api/projects/completed` (optional `?workspace=`) and `POST /api/projects/completed/restore` (see `PWA_API.md`).

- **Folder name** is the slug used internally (`vault_folder`). Lowercase + kebab-case is preferred but not enforced; spaces, mixed case, and underscores all work. Avoid path separators and leading dots.
- **PWA display name** comes from frontmatter `name:` (or `title:` if `name:` is absent), so it can be any human-friendly string with spaces (e.g. `name: AI Championship Project`). If both are missing, the folder name is used as the label.
- **Top-level reference docs** live directly under `memory-vault/personal/projects/` (e.g. `Ciaobot-Decisions.md`, `Ciaobot-Improvements.md`) and never "complete"; don't put real projects there.
- A stray `Projects/active/<Name>.md` at the top level gets auto-promoted to `<Name>/<Name>.md` on the next server start, so the folder+file invariant always holds.
- Every workspace has an auto-created `General` project bound to `projects/active/general/`. It is marked `is_auto` and displays the 'auto' chip in the sidebar. It's where ad-hoc chats land and where scheduled automations run; don't delete it.

## Local PWA dev

```bash
ciao dev
```

- Frontend: `http://localhost:5173`
- Dev backend: `http://127.0.0.1:8543`
- `ciao dev` intentionally avoids `localhost:8443` because editor/webview proxy processes can hijack that port and serve stale UI.

## Testing

```bash
source .venv/bin/activate
pytest tests/                  # Python backend tests
pytest tests/test_dag.py       # DAG runner only (Node/Edge/run, per-node timing)
ciao public-preflight scan <export-root> --private-patterns <file> # Public export private-data preflight
ciao public-preflight export . /tmp/ciao-public-export # Copy allowlisted public tree
ciao package-smoke --skip-frontend # Wheel install smoke test
ciao vault-index --workspace default --format json  # Query the vault index
ciao vault-search "keyword" --limit 5 # FTS search over the configured vault
ciao vault-lint --vault-root memory-vault # Vault hygiene lint
cd web && npm test             # Frontend unit tests
cd web && npm run build        # Typecheck + Vite build (frontend smoke test)
```

For PWA changes that touch running static assets, rebuild then deploy via the Settings panel. Don't auto-restart the service from inside a chat session.

## GWS profile and auth

- `GWS_PROFILE` controls which Google account `secretary` uses: `personal` or `work`.
- Always invoke `scripts/gws-profile.sh <profile> <gws-args>` directly. Do **not** `source` it, the script ends with `exec`.
- Token expiry symptom: `"token_error": "Token has been expired or revoked."`. Fix: `scripts/gws-profile.sh <profile> auth login`. If that fails on a headless server, use `python3 scripts/gws-auth-helper.py <profile>`. If `GOOGLE_WORKSPACE_CLI_CLIENT_ID` is set in `.env`, it can override the OAuth client to a wrong project; comment it out, or run `env -u GOOGLE_WORKSPACE_CLI_CLIENT_ID gws auth login --profile <profile>`.
- **Credentials Persistence:** Credentials can be stored in `.env` as base64-encoded tarballs under `GWS_PERSONAL_SECRETS_B64` and `GWS_WORK_SECRETS_B64`. They are auto-backed up on successful authentication via `scripts/gws-auth-helper.py`, and automatically restored if directories under `secrets/` are missing at runtime. Manual backup/restore can be run using `python3 scripts/gws-secrets.py [backup|restore] <personal|work>`.
- PWA-only (no shell) auth recovery: see the 2026-05-01 entry in `memory-vault/personal/Workspace/Learnings.md` (localhost-callback-relay flow).

## Skills

Custom skills live in `skills/` and are symlinked into `.claude/skills/` and mirrored to `~/.pi/agent/skills/` by `ciao sync-skills` on ciao startup. Project agents live in `subagents/` (symlinked into `.claude/agents/`); slash commands live in `commands/` (symlinked into `.claude/commands/` and mirrored to `~/.pi/agent/prompts/`). The same command runs the Pi agent converter (`ciao sync-agents-to-pi`), which translates `subagents/` into Pi-flavored files under `~/.pi/agent/agents/` for `@tintinweb/pi-subagents` (must keep YAML frontmatter at byte 0 and place the `# ciao-managed` marker after the frontmatter, or Pi subagent discovery skips the file). Edit the canonical source folders (`skills/`, `subagents/`, `commands/`); they re-sync to all providers on restart. **Do not edit the generated `.claude/` or `~/.pi/` dirs directly** and **do not run `npx skills update` or `gws generate-skills` ad-hoc.** See `skills/README.md` for the full flow.

## Change guidelines for code

- **Doc the change.** After any change to `ciao/`, `web/`, `scripts/`, `deploy/`, or `pyproject.toml`, dispatch the `doc-updater` agent before declaring the task complete. It refreshes this file, `CLAUDE.md`, and `INTEGRATIONS.md` against actual repo state. Skip only for pure bugfixes that touch nothing in layout, capabilities, install steps, env vars, endpoints, or commands.
- **Never restart the ciao service yourself** from inside the PWA. Apply code changes and ask the operator to hit Deploy.
- **Never commit `.env` or API keys.** `.env` minimum: `PWA_AUTH_TOKEN`.
- **Keep edits minimal and consistent with existing patterns.** Don't refactor unrelated code; if unrelated changes appear, pause and ask.
- **Avoid destructive git** (force push, hard reset on shared branches) unless explicitly asked.
- **Write tests** for new Python behavior; add to `tests/`. PWA changes verify via `npm run build` typecheck at minimum.

## Pointers

- `CLAUDE.md`: Claude Code behavior, vault-first retrieval, delegation, writing style.
- `INTEGRATIONS.md`: env vars, OAuth setup, MCP connectors, Ciaobot server runtime knobs (CIAO_* vars), deploy.
- `PWA_API.md`: API endpoints, auth flow, state paths.
- `web/README.md`: PWA frontend dev workflow, iOS Safari gotchas, design system tokens.
- `skills/README.md`: skill build and install flow.
- `memory-vault/personal/Workspace/Learnings.md`: durable lessons; promotion rules in the file header.
