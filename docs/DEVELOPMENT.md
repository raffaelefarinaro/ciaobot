# Ciaobot Development Guide

Setup, dev workflow, testing, and change guidelines. For the system design, read `docs/ARCHITECTURE.md` first.

## Server install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
ciao setup --workspace /tmp/ciao-workspace
ciao run
```

`ciao setup` is idempotent. It writes the initial `.env`, seeds stock workspace files, copies the editable `CLAUDE.md` workspace guide, links `AGENTS.md` to that same guide for shared runtime discovery, copies `CIAO_CUSTOMIZATION.md`, and renders the server plist under `~/Library/LaunchAgents/`. Setup no longer generates the retired rumps agent or `Ciaobot Server.app`; it removes them when an older install left them behind. Existing custom `AGENTS.md` files are preserved. By default setup does not load launchd; add `--load-launchd` when you want it to run `launchctl`.

The weekly dependency-changelog review is an operator-owned routine, not part of the public app install. In a maintainer workspace it lives at `scripts/dependency_review.py` and invokes this checkout for the DAG/runtime; public release preparation uses only the generic helpers in `ciao/dependency_updates.py`.

A fresh first logical workspace and workspaces added later in Settings live at
`<CIAO_VAULT_ROOT>/<workspace-name>/`. Their registry path is read-only in the
PWA. Existing-folder setup preserves the selected notes in place so the
onboarding agent can inspect them; `ciao os-audit` then offers a model-guided,
backed-up migration into the standard named folder.

Common package CLI entry points:

```bash
ciao setup --workspace ~/ciao --workspace-name personal --load-launchd
ciao vault-index --workspace default --format json
ciao vault-search "project keyword" --limit 5
ciao vault-lint --vault-root memory-vault
ciao critique --input plan.md --type plan
ciao os-audit --json
ciao create-chat --prompt "Start here" --workspace default
ciao cleanup-sdk-blobs --workspace .       # dry-run by default
ciao label-hygiene --json                  # audit issue labels, dry-run by default
ciao dev                                   # backend :8543 + Vite :5173
ciao public-preflight export . /tmp/ciao-public-export
ciao public-preflight scan /tmp/ciao-public-export --private-patterns /tmp/private-patterns.txt
ciao package-smoke --skip-frontend
ciao auth claude --print-only              # show terminal OAuth command
ciao auth opencode --print-only            # show opencode login command
ciao auth opencode                         # run provider login helper
```

### macOS venv workarounds

On recent macOS, `scripts/run-ciao.sh` and the `scripts/dev.sh` wrapper source `scripts/ensure-deps.sh`, which injects two self-healing workarounds into `.venv/bin/activate`:

- A `DYLD_LIBRARY_PATH` pointing at Homebrew's `libexpat`. macOS 26+ ships a system `libexpat` missing a symbol Homebrew Python's `pyexpat` needs, which otherwise crashes pip and venv creation with `ImportError: ... Symbol not found`.
- An `SSL_CERT_FILE` pointing at the venv's certifi CA bundle. The python.org Python build ships no CA bundle, so the bare `urllib` calls in the server (e.g. OAuth token refresh in `ciao/web/auth.py`) fail with `SSLCertVerificationError`.

No manual step is needed; `ensure-deps.sh` handles both. If you set up the venv by hand and hit either error, run `scripts/ensure-deps.sh` once to repair `activate`.

### End-user distribution

The supported macOS release path is the one-line installer:

```bash
curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh
```

The installer downloads the signed Apple Silicon (aarch64) app archive, verifies
it with the published native verifier, and installs the bundled runtime into
`Ciaobot.app`.
When a configured workspace is already referenced by the LaunchAgent, it
preserves that workspace and password; on a clean machine it leaves setup to
the app's bootstrap onboarding rather than generating a hidden password. It
does not require Python, Homebrew, or sudo. The installer prints milestone
percentages, verification status, and a short multilingual Ciao greeting
sequence; `--dry-run` shows the same terminal treatment without changing files.
A DMG is intentionally not built or attached to releases.

The source template is `scripts/install.sh`. The release workflow substitutes
the verifier checksum and attaches `install.sh`, the verifier, the signed app
archive, its signature, and `latest.json`.

## Branching and releases

- **`develop`** is the integration branch. Feature and fix PRs target `develop`.
- **`main`** is release-only. Direct pushes and merges to `main` are blocked; only release PRs land there.
- **CI** (`.github/workflows/ci.yml`) runs on pushes to `develop` and on pull requests into `develop` or `main`.
- **Release prep:** from a clean checkout, run:

```bash
scripts/prepare-release --apply --create-pr --ready
```

  That cuts `release/vX.Y.Z` from `develop`, aligns the Python, PWA, desktop
  npm/Cargo/Tauri versions and lockfiles, refreshes `CHANGELOG.md`, runs release
  checks, and opens a PR into `main`. Use
  `--bump minor` or `--version X.Y.Z` when needed.

- **Publish:** merging the release PR into `main` triggers `.github/workflows/release-on-main.yml`, which creates the `vX.Y.Z` tag and GitHub release. `publish.yml` then builds the PWA, the embedded aarch64 runtime, the aarch64 app, the native verifier, installer, and updater metadata. It does not publish PyPI, Homebrew, or DMG artifacts. A follow-up job merges `main` back into `develop`.

One-time GitHub setup for a fresh clone or repo admin:

```bash
./scripts/configure-github-branches.sh
```

That sets `develop` as the default branch and enables pull-request + CI requirements on `develop` and `main`.

## Frontend build

Node 22 is the supported version (`.nvmrc`, and what CI uses). The floor is
`^20.19.0 || ^22.13.0 || >=24.0.0`, set by jsdom — below it every jsdom test file
fails to start its worker, and vitest still reports a pass for the subset that
ran. `npm test` preflights this and exits with an explanation rather than
producing a misleading green summary.

```bash
nvm use              # reads .nvmrc → Node 22
npm install          # optional root Node tooling
cd web
npm install
npm run build        # typecheck + Vite build, outputs to ciao/web/static/
npm test             # 61 test files under web/src
```

## macOS desktop development

The Tauri 2 shell requires macOS 13+ on Apple Silicon (arm64), Node 22.x, Rust
1.90.0 with the `aarch64-apple-darwin` target, and `swiftc` from the Xcode
Command Line Tools (it builds the `ciaobot-native` sidecar).

`desktop/native/main.swift` uses Apple's FoundationModels, whose
`GenerationOptions` initialiser was renamed: `sampling:` on the macOS 26 SDK,
`samplingMode:` on macOS 27. **The file deliberately uses the older
`sampling:`** — it is the only spelling that compiles on both, since the GitHub
macOS runner currently tops out at Xcode 26.6 (Swift 6.3.3, macOS 26 SDK) where
`samplingMode:` does not exist. On a macOS 27 SDK it still builds, with a
deprecation warning. Switch to `samplingMode:` only once the runner image ships
Xcode 27, or CI cannot build the sidecar at all.

CI and the publish workflow both select the newest Xcode installed on the runner
before building the sidecar and print `xcodebuild -version`, so a toolchain skew
is visible in the log instead of looking like a code regression.

`./scripts/check-desktop.sh` runs the whole gate — the same commands CI's
`build-desktop` job does — and asserts the sidecar ends up bundled, signed, and
runnable inside the built aarch64 app. Run it after any change under
`desktop/`; `--fast` skips the bundle build when you have not touched
`desktop/native/` or `tauri.conf.json`. `prepare-release` runs it too.

The individual steps, if you need them separately:

```bash
cd desktop
npm ci
npm run build            # desktop frontend (vite) only
npm run build:native     # Swift sidecar -> src-tauri/binaries/ (also runs via pretauri)
cd src-tauri
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
cd ..
npm run tauri build -- --target aarch64-apple-darwin
```

The main webview loads the live localhost PWA and must never be added to a
Tauri capability. While the engine is unreachable it loads the bundled
`startup.html` recovery page and automatically navigates to the PWA after
recovery; the same local page is reused by a hidden update window that the tray
shows immediately when an update starts. The update window receives native
progress events and renders them as boot-screen-style log rows (no interactive
toggle; the log is always visible). Test both
startup and update states when changing desktop startup or service lifecycle
code. The shell's IPC surface is deliberately tiny: exactly two Tauri commands
(`check_permission` / `request_permission`, backing the PWA's push-notification
permission flow, declared for the main/update capability) — everything else
about the desktop experience is driven from the tray in Rust, so remote page
content has no other IPC surface to reach. Keep it that way — adding a command
means re-introducing a bundled window to own it. Release builds require `TAURI_SIGNING_PRIVATE_KEY` and
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD`; Apple signing remains ad-hoc.
The main window keeps Tauri's native drag/drop handler enabled so Finder paths
are preserved. Rust creates a short-lived, single-use grant under the runtime
root; the local `/api/desktop-drop` route consumes it and either returns host
paths or transfers client files to the host project. Verify both a host
Finder-to-chat drop and a client-to-host transfer after changing this bridge.

Read mutations are cross-device notification mutations too: the engine emits a
clear control for the chat, remote PWA service workers close their matching
notification tag, and the macOS shell removes delivered native banners. Keep
the desktop notification-log and service-worker tests aligned when changing
notification identifiers or payload shapes.

### Rebuilding the shell from Settings → Restart

With `CIAO_DEV_MODE=true`, Settings → Restart also rebuilds this bundle, but only
when a watched source under `desktop/` is newer than
`src-tauri/target/release/bundle/macos/Ciaobot.app` (see `WATCHED_SOURCES` in
`ciao/desktop_build.py`). Everything else keeps the restart fast: a release Rust
build costs minutes, so an engine-only or PWA-only change must not pay for it.

The step builds the native arch with `--bundles app` and no updater artifacts (a
dev machine has no signing key), stages the result at
`/Applications/.Ciaobot.app.deploy`, then quits the running app, swaps it into
`/Applications/Ciaobot.app`, and opens it again just before the engine restart.
Two things are load-bearing:

- The quit has to happen first. `tauri-plugin-single-instance` makes `open` focus
  the running instance instead of launching the new binary.
- The swap is staged, not in place. Deleting a bundle under a live process leaves
  it reading pages from a removed inode. If the app refuses to quit within 20s,
  the bundle stays staged and is swapped in on the next restart.

The quit goes through AppleScript, not the tray's Quit item: the tray item also
stops the engine, which is not what a rebuild wants.

The bundled engine is resolved from `Ciaobot.app/Contents/Resources/ciao-runtime`.
Development builds may still use the checkout's interpreter, but a packaged app
must never fall back to Homebrew or another `PATH` installation.

After PWA changes, rebuild and either restart the service or use the **Deploy** button in PWA Settings. **Never restart the ciao service from inside a PWA chat** (you'd sever your own session); ask the operator to deploy.

Restart requests made through the running server enter a drain phase: existing chats and background agents finish before shutdown, and new turns are not admitted during that window. Directly killing the process bypasses this protection.

## Local PWA dev

```bash
ciao dev
```

- Frontend: `http://localhost:5173`
- Dev backend: `http://127.0.0.1:8543`
- `ciao dev` intentionally avoids `localhost:8443` because editor/webview proxy processes can hijack that port and serve stale UI.
- A development backend refuses to start when another Ciaobot backend already owns the same runtime directory. Stop the launchd service first, or use a separate `CIAO_RUNTIME_ROOT`; changing only the port is not sufficient because both servers would otherwise mutate the same project/chat registry.

## Testing

```bash
source .venv/bin/activate
pytest tests/                  # Python backend tests
pytest tests/test_schedule_workspace_routing.py  # Workspace/provider/model inheritance for schedules
pytest tests/test_dag.py       # DAG runner only (Node/Edge/run, per-node timing)
ciao public-preflight scan <export-root> --private-patterns <file> # Public export private-data preflight
ciao public-preflight export . /tmp/ciao-public-export # Copy allowlisted public tree
ciao package-smoke --skip-frontend # Wheel install smoke test
ciao vault-index --workspace default --format json  # Query the vault index
ciao vault-search "keyword" --limit 5 # FTS search over the configured vault
ciao vault-lint --vault-root memory-vault # Vault hygiene lint
ciao critique --input plan.md --type plan # Multi-model adversarial review panel
ciao os-audit --json # Strict AI OS setup and context-hygiene audit
ciao memory-audit --json # Bounded-memory rot only (regions; add --with-vault for note aging)
cd web && npm test             # Frontend unit tests
cd web && npm run build        # Typecheck + Vite build (frontend smoke test)
```

The Settings → Automations list is registry-driven: `GET /api/automation` carries
each job's static `trigger` sentence, `schedule_id`, `one_time`, `uses_model`,
and `produces_outcome`. Do not infer those from a job's latest run — never-run
jobs are intentionally included in the response. When adding a job to
`job_runs.REGISTRY`, give it a `trigger` (the page's answer to "when does this
run?"); when removing one, add its id to `job_runs.RETIRED_JOBS`, because
`job_runs_latest.json` keeps the last run of every job it ever saw and the row
would otherwise linger with a stale badge. Set `schedule_only=True` only when a
schedule is the job's *sole* trigger: such a job is hidden on machines where
that schedule is not installed.

For chat rendering changes, verify the compact `Activity` disclosure, `Outputs` placement, readable token labels, keyboard operation, and 44px touch targets at both desktop and narrow-phone widths. Markdown tables should shrink-wrap on desktop and keep readable first-column labels inside a horizontally scrollable table viewport on narrow screens.
For HTML artifact changes, keep the preview self-contained: inline scripts/styles and `data:` media are allowed, while external requests and `blob:` sources must remain blocked. Use the fixtures under `tests/fixtures/html_artifacts/` plus the focused workspace-HTML tests.
For workspace navigation changes, verify that unmodified `1`–`9` keys follow the visible sidebar workspace order, do not fire from text inputs, and keep working in the automations view. The sidebar key labels should remain visible and accessible at narrow widths. An open `AskUserQuestion` card takes those digits over for its own options while it is up (Design System rule S7) and hands them back when it closes, so check both states after touching either handler.
On the home screen, also verify that it shows only the selected workspace's chats (switching workspaces swaps the content) and that arrow keys follow the rendered lane layout: up/down moves between stacked lanes, left/right moves within a lane.
For sidebar chat-group changes, verify that a chat's subagent disclosure has a visible `aria-expanded` state, keeps a 44px touch target, hides and restores only its subagent rows, and automatically reopens when the open route is one of those subagents.
For composer drag-and-drop changes, test both local host and remote client roles. Document drops preserve host sources and add Markdown companions; remote supported documents persist only Markdown through the dedicated chat endpoint:
host paths must be absolute, while client files must upload into the active
project on the host before returned original/Markdown paths are inserted. The
generic ProjectView upload remains unchanged.

## Quality gates

Backend type-checking, coverage, and dependency audits run in CI and are also available locally via `scripts/dev-commands.sh`:

```bash
scripts/dev-commands.sh typecheck   # mypy ciao (blocking in CI)
scripts/dev-commands.sh coverage    # pytest --cov=ciao --cov-report=term-missing
scripts/dev-commands.sh lint        # eslint over web/src (advisory in CI)
scripts/dev-commands.sh audit       # pip-audit + npm audit (advisory in CI)
scripts/dev-commands.sh all         # everything above
```

`mypy ciao` is a blocking CI step — keep it green (config in `pyproject.toml` under `[tool.mypy]`). The frontend lint and both dependency audits are advisory (`|| true`) so a fresh upstream advisory can't block a release; review their output rather than ignoring it. Install `pip install -e '.[test]'` (Python) and `cd web && npm ci` (frontend) to get the tools. A `.pre-commit-config.yaml` wires trailing-whitespace/ruff/mypy/eslint hooks for `pre-commit install`.

### Vault lint

`ciao vault-lint` is a read-only, deterministic check for the Markdown vault.
It validates the frontmatter on each page, including a non-empty string
`type`, and reports broken relative Markdown links, orphan pages, and
duplicate stems. Relative Markdown links are the vault's only cross-link
dialect, so there is one broken-link bucket, not one per dialect. `INDEX.md`, `MEMORY.md`, and `log.md` are exempt
from the frontmatter requirement. External URLs, absolute paths, and anchors
are not treated as vault links.

Use `--vault-root` to choose a vault. Otherwise it uses `CIAO_VAULT_ROOT` or
`./memory-vault`. A clean scan exits 0. Findings, a missing vault root, or an
incomplete traversal exit 1. It never reports a clean vault when it could not
inspect every path.

### AI OS audit

`ciao os-audit` checks required workspace roots, vault frontmatter, relative
Markdown links, orphans and duplicate stems, skill budgets,
instruction clashes, bounded-memory hygiene, pending memory proposals, and
failed background jobs. Human-readable Markdown is the default; use `--json`
for automation. A vault scan that cannot inspect all paths is reported as an
audit error, not as a healthy result.

The status and process exit code are a stable contract:

| Status | Exit | Meaning |
|---|---:|---|
| `healthy` | 0 | The scan completed reliably and found no actionable items. |
| `needs_attention` | 1 | The scan completed reliably and found actionable items. |
| `error` | 2 | Required evidence could not be inspected reliably. Findings may still be present, but the report is not a clean bill of health. |

The daily `system-memory-curation` schedule is presented as **Workspace care**. Its stock `memory-curation` skill runs lightweight memory passes nightly and uses `Workspace/Curation-Log.md`'s `last_full_pass` marker to catch up the deeper weekly work after downtime. A full pass runs `ciao vault-index --write` before `ciao os-audit --json --scope workspace`; a failed index rebuild or audit exit 2 leaves the marker overdue and prevents a healthy/no-op claim. Exit 1 means reliable findings and the pass continues with only safe structural repairs.

### Bounded-memory rot audit

`ciao/memory_audit.py` checks whether the content of the always-loaded
`ciao:memory` / `ciao:profile` regions has rotted, as opposed to the mechanical
checks (caps, expiry, exact duplicates) that `audit_memory` already ran. It rests
on one rule: a remembered fact is either **state**, a current value that gets
replaced when it changes, or an **event**, a thing that happened which gets
appended to a log and never edited. The regions are a state surface.

Three detectors, all model-free, because a model asked to tally a few hundred
entries returns a confident number and a different one tomorrow:

| Detector | Finds | Counted as actionable |
|---|---|---|
| `event_shaped_entries` | Transcript residue: `User said ... -> assistant ...`, leftover `[idx=]` citations. Belongs in `Workspace/Learnings.md`. | Yes |
| `stale_path_entries` | A cited path that does not exist. The only detector with hard evidence. | Yes |
| `superseded_state_candidates` | Two entries asserting state about one subject, meaning a value was appended instead of replaced. | No |

The detectors are tuned for precision over recall: one that cries wolf trains
the reader to skip the report. Two consequences worth knowing before you widen
them. A file extension alone does not make a token a path, because the engine
and vault live in sibling repos and a correct entry may cite `ciao/cli.py` from
the vault repo, where no `ciao/` exists; a token must be explicitly rooted
(`~/`, `/`, `./`) or start at a directory that exists in this workspace. And a
path outside both the workspace and `$HOME` is counted as unverifiable rather
than stale, since it may belong to another machine. `paths_checked` and
`paths_unverifiable` are reported so an empty finding list is never mistaken for
full coverage.

`superseded_state_candidates` is deliberately excluded from `total_issues`,
matching `rule_overlaps_found`. It is a judgement the user may legitimately
decline, and a finding that can never be cleared would pin the whole audit at
`needs_attention` until people stop reading it.

The same state/event rule also decides how **age** is read on vault notes: an
entity note (person, project) asserts current state, so going unverified past
its type's horizon is a review candidate; logs and journals record events,
which never go stale. `find_stale_notes` ages each note from frontmatter
`updated:` (a deliberate "I re-checked this" claim) or file mtime, against
per-type horizons (project 30d, person 90d, everything else 180d; `Workspace/`
queue files exempt — flagging an inbox for being an inbox is noise). Like
`superseded_state_candidates` these findings are informational: they surface in
`os-audit`'s memory section, the Memory Map sidebar ("Needs review"), and the
daily `system-memory-curation` run, which re-verifies each note and stamps
`updated:` when the facts still hold.

`ciao memory-audit` reads one file and skips the vault scan, so the daily
`system-memory-curation` schedule can afford to call it and fix what it finds.
`--with-vault` adds the note-aging pass (still informational, never changes the
exit code). Exit 0 clean, 1 findings, 2 a region could not be read.

## Skills, subagents, and slash commands

Packaged generic skills live in `ciao/stock/skills/` and are installed into every workspace's `.claude/skills/` by `ciao sync-skills` on startup. This includes Ciaobot-specific skills (`ciao-capabilities`, `web-research`, `workspace-authoring`, …) and the upstream **`gws-*` skills** for Google Workspace (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, Forms). The `gws-*` skills are gated on the workspace having a Google account linked: `sync_workspace_skills` resolves each agent root's effective profile (its `gws_profile`, else the operator default only when that account actually exists) and skips the GWS skills when the workspace has no profile connected — shipping wrappers that name a credential directory nobody created just produces auth errors. In a **workspace**, user-owned skills live in `skills/`, project agents in `subagents/`, and slash commands in `commands/`; `ciao sync-skills` mirrors them into the generated `.claude/` directories. Locked GitHub/package skills follow the upstream `skills` CLI layout: their canonical directories live under `.agents/skills/`, with provider links under `.claude/skills/`; synchronization preserves either that layout or older `.claude`-canonical installs. A workspace skill with the same name as a packaged one overrides it.

The `gws-*` stock skills are regenerated from the installed `gws` CLI via `ciao/gws_skills.py` on release (`python -m ciao.release --apply`). The generator output is passed through Ciaobot curation: profile-wrapper command examples, integration auth notes in `gws-shared`, stripped upstream `openclaw` metadata and See Also boilerplate. Ciaobot-specific gws conventions live in `gws-shared`; only the short profile-wrapper routing rule belongs in the compact core (`ciao/system_prompt.md`).

The stock `visual-plan` skill produces local Markdown plans with an optional self-contained HTML companion, including diagrams drawn as inline SVG. It is the one-stop planning surface for work that needs an approval gate and a cross-session resume contract. It deliberately draws a boundary against the stock `workspace-authoring` skill: `workspace-authoring` owns routine working docs (notes, analyses, drafts with no approval gate), while `visual-plan` owns plans that must be approved and survive a provider switch. Each skill's description names the other's territory. Visual plans are local Markdown artifacts; only one file is pinned at a time, and Plan mode cannot produce one (the skill refuses and explains instead of failing on the write). Interactive HTML companions are authored by loading the stock `html-artifact` skill. A same-named workspace skill overrides the packaged copy; refresh a workspace with `ciao sync-skills --skip-upstream`, and roll back a future removal at the package level by reverting the stock skill from the next build.

### Provider context and native memory

Normal chats send one compact provider-neutral context capsule containing the
active workspace/project, canonical document, date, retrieval hint, entity
matches, and unattended-turn marker. Stable routing facts are sent once per
provider session; handovers use a separate bounded excerpt. Claude and OpenCode
receive the same compact Ciaobot core, while their native
`CLAUDE.md`/`AGENTS.md` loaders remain the only source of bounded memory.
`memory_tool.py` prunes valid expired entries before provider startup and
exposes `memory_status`/`memory_update` without creating a second memory store.

Edit canonical sources, not the generated `.claude/` or `.agents/` dirs. Do not run `npx skills update` ad-hoc (it re-expands the lockfile and repopulates bloat); regenerate the `gws-*` skills through `ciao/release.py` rather than calling `gws generate-skills` by hand.

## DAG-style schedules (maintainers)

Some packaged schedules are multi-step workflows (load state, gate, model call, write). For these, use `ciao.dag` rather than a long `async def`:

- `Node(id, kind, model='', timeout_s=180.0, payload={})` — kinds: `bash`, `prompt`, `gate`, `subagent`, `retention`.
- `Edge(src, dst, when='ok')` — `when` is `ok` (default), `fail`, or `always`.
- `run(dag, edges, job=..., label=..., initial_ctx={})` — records each node in `.runtime/job_runs.jsonl`.
- `subagent` nodes accept an opt-in `payload['requires']` post-condition list: each item is a file path that must exist and be non-empty after the node ran, or `{"path": ..., "contains": "<regex>"}` where at least one line must match. Paths may reference ctx like the prompt and resolve against `payload['cwd']`; `contains` regexes are used verbatim (never ctx-formatted, so quantifiers like `{2}` are safe). Exit 0 with unmet requirements fails the node (guards against an unauthenticated subagent silently doing nothing); DAGs without `requires` are unchanged.

Canonical example: `ciao/skill_evolution.py:_process_skill_dag`. Use a DAG when there are 3+ sequential steps with branching and you want per-step timing on the Automation page.

`ScheduleManager.catch_up()` runs once at server startup. It dispatches only the latest missed occurrence for each enabled schedule, leaves the prompt unchanged, and records the missed occurrence's local date so a later slot on the startup day can still fire normally. Cover changes to this behavior in `tests/test_schedules.py`. Packaged system routines are excluded when the startup falls inside the post-setup grace window (`ciao/setup_marker.py`, 24h from a first-time setup): a brand-new install is greeted by its onboarding chat, and the routines fire at their next regular tick instead of all replaying missed runs in parallel. Cover that in `tests/test_setup_catch_up_grace.py`.

Sidebar subagent rows are fed by `GET /api/subagents/running` (dispatch metadata only, active chats only) and the store's poll, which replaces the whole map so a finished agent's row disappears. Only agents the parent session can name get a row — background dispatches, plus opencode children; a foreground Task is recorded in the parent JSONL by its own completion, so it is never running by the time it is nameable. Their read-only view is `SubagentChatView.vue` on `/chat/:chatId/subagent/:agentId`, fed by `GET /api/chats/{id}/subagents`. Claude agent ids arrive bare from the parent JSONL and `agent-`-prefixed from the local transcript fallback, so both surfaces normalise before comparing or routing. Cover changes in `tests/test_running_subagents.py` and `web/src/components/__tests__/ProjectSidebar.test.ts`.

## MCP control plane

`ciao/control_plane.py` is the provider-neutral application boundary;
`ciao/mcp_server.py` is only its authenticated MCP adapter. Add business rules
to managers/control-plane methods, not tool handlers. Every tool must declare
read/write/destructive annotations, return a stable envelope, enforce scoped
workspace/project/chat access, and have focused protocol plus domain tests.
Self-affecting operations must defer until the caller chat drains. Provider
tokens must never enter the model's shell environment or telemetry arguments.

See `docs/MCP.md` for the catalog and provider configuration.

## Change guidelines

- **Doc the change.** After any change to `ciao/`, `web/`, `scripts/`, `deploy/`, or `pyproject.toml`, refresh `docs/ARCHITECTURE.md`, this file, `CLAUDE.md`, and `INTEGRATIONS.md` against actual repo state before declaring the task complete. Skip only for pure bugfixes that touch nothing in layout, capabilities, install steps, env vars, endpoints, or commands.
- **New API routes must be documented.** Add the route to `PWA_API.md`; state-changing routes also need an Agent recipe or an allowlist entry in `tests/test_pwa_api_docs.py`. New `CIAO_*` env vars must land in `INTEGRATIONS.md` or the allowlist in `tests/test_env_vars_documented.py`. Both are test-enforced.
- **Never restart the ciao service yourself** from inside the PWA. Apply code changes and ask the operator to hit Deploy.
- **Never commit `.env` or API keys.** `.env` minimum: `PWA_AUTH_TOKEN` (the dashboard password; protection is on unless `PWA_AUTH_REQUIRED=false`).
- **Keep edits minimal and consistent with existing patterns.** Don't refactor unrelated code; if unrelated changes appear, pause and ask.
- **Avoid destructive git** (force push, hard reset on shared branches) unless explicitly asked.
- **Use the branch model in `CONTRIBUTING.md`.** Day-to-day PRs target `develop`; release PRs target `main`.
- **Write tests** for new Python behavior; add to `tests/`. PWA changes verify via `npm run build` typecheck at minimum.
- **Verify UI accessibility.** For PWA layout changes, check keyboard operation, visible focus, browser zoom, and 44px mobile targets at a narrow-phone viewport in addition to the build.
