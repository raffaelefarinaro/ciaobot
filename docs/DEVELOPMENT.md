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

`ciao setup` is idempotent. It writes the initial `.env`, seeds stock workspace files, copies the editable `CLAUDE.md` workspace guide, links `AGENTS.md` to that same guide for Codex, copies `CIAO_CUSTOMIZATION.md`, and renders the server plist under `~/Library/LaunchAgents/`. Setup no longer generates the retired rumps agent or `Ciaobot Server.app`; it removes them when an older install left them behind. Existing custom `AGENTS.md` files are preserved. By default setup does not load launchd; add `--load-launchd` when you want it to run `launchctl`.

Provider settings for custom compatible endpoints are persisted in the tracked workspace file `.ciao/custom_providers.json`; bearer tokens are kept separately in the gitignored `.runtime/custom_provider_tokens.json` and are never committed or returned by the API. Focused provider tests belong in `tests/test_custom_providers.py`.

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
ciao os-audit --json
ciao create-chat --prompt "Start here" --workspace default
ciao cleanup-sdk-blobs --workspace .       # dry-run by default
ciao label-hygiene --json                  # audit issue labels, dry-run by default
ciao dev                                   # backend :8543 + Vite :5173
ciao public-preflight export . /tmp/ciao-public-export
ciao public-preflight scan /tmp/ciao-public-export --private-patterns /tmp/private-patterns.txt
ciao package-smoke --skip-frontend
ciao auth claude --print-only              # show terminal OAuth command
ciao auth codex --print-only               # show Codex / ChatGPT login command
ciao auth opencode --print-only            # show opencode login command
ciao auth ollama                           # run provider login helper
ciao scaffold eval example --workspace .  # create evals/example.json
ciao eval --suite evals/example.json --workspace .
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

The installer downloads the signed universal app archive, verifies it with the
published native verifier, and installs the bundled runtime into `Ciaobot.app`.
When a configured workspace is already referenced by the LaunchAgent, it
preserves that workspace and password; on a clean machine it leaves setup to
the app's bootstrap onboarding rather than generating a hidden password. It
does not require Python, Homebrew, or sudo. A DMG is intentionally not built or
attached to releases.

The source template is `scripts/install.sh`. The release workflow substitutes
the verifier checksum and attaches `install.sh`, the verifier, the signed app
archive, its signature, and `latest.json`.

## Branching and releases

- **`develop`** is the integration branch. Feature and fix PRs target `develop`.
- **`main`** is release-only. Direct pushes and merges to `main` are blocked; only release PRs land there.
- **CI** (`.github/workflows/ci.yml`) runs on pushes to `develop` and on pull requests into `develop` or `main`.
- **Release prep:** from a clean checkout, run:

```bash
scripts/prepare-release --apply --run-release-evals --create-pr --ready
```

  That cuts `release/vX.Y.Z` from `develop`, aligns the Python, PWA, desktop
  npm/Cargo/Tauri versions and lockfiles, refreshes `CHANGELOG.md`, runs release
  checks, runs the Claude/Codex release scorecard, commits sanitized evidence
  under `release-evidence/vX.Y.Z/`, and opens a PR into `main`. Use
  `--bump minor` or `--version X.Y.Z` when needed. Live release evals require
  both provider logins and may spend provider tokens.

- **Publish:** merging the release PR into `main` triggers `.github/workflows/release-on-main.yml`, which creates the `vX.Y.Z` tag and GitHub release. `publish.yml` then builds the PWA, embedded runtimes, universal app, native verifier, installer, and updater metadata. It does not publish PyPI, Homebrew, or DMG artifacts. A follow-up job merges `main` back into `develop`.

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

The Tauri 2 shell requires macOS 13+, Node 22.x, Rust 1.90.0 with
`aarch64-apple-darwin` and `x86_64-apple-darwin` targets, and `swiftc` from the
Xcode Command Line Tools (it builds the `ciaobot-native` sidecar).

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
`build-desktop` job does — and asserts the sidecar ends up bundled, universal,
signed, and runnable inside the built app. Run it after any change under
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
npm run tauri build -- --target universal-apple-darwin
```

The main webview loads the live localhost PWA and must never be added to a
Tauri capability. While the engine is unreachable it loads the bundled
`startup.html` recovery page and automatically navigates to the PWA after
recovery; test both states when changing desktop startup or service lifecycle
code. The shell's IPC surface is deliberately tiny: exactly two Tauri commands
(`check_permission` / `request_permission`, backing the PWA's push-notification
permission flow, declared in the `main` capability) — everything else about the
desktop experience is driven from the tray in Rust, so remote page content has
no other IPC surface to reach. Keep it that way — adding a command means
re-introducing a bundled window to own it. Release builds require `TAURI_SIGNING_PRIVATE_KEY` and
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
ciao os-audit --json # Strict AI OS setup and context-hygiene audit
ciao memory-audit --json # Bounded-memory rot only (regions, no vault scan)
cd web && npm test             # Frontend unit tests
cd web && npm run build        # Typecheck + Vite build (frontend smoke test)
```

### Declarative live evaluations

Create a starter suite, then run it against the current workspace:

```bash
ciao scaffold eval example --workspace .
ciao eval --suite evals/example.json --workspace .
```

The suite is schema-version 1 JSON. It declares routing defaults and ordered
scenarios; each scenario selects exactly one `skill` or `subagent` target and
contains deterministic assertions such as output text, regular expressions,
and required or forbidden tools. Use `--filter` to select scenarios and
`--provider`, `--model`, `--output`, `--turn-timeout`, or `--startup-timeout`
to override execution settings. Routing precedence is CLI override, scenario,
then suite default.

Each scenario runs in a fresh temporary workspace and isolated Ciaobot server.
Only the selected target is staged, workspace-owned targets take precedence
over packaged targets, and the source workspace is not modified. Path and
symlink boundary checks reject targets that escape their canonical source.

The normal tests mock provider execution and require no credentials:

```bash
pytest -q tests/test_evals.py tests/test_eval_targets.py \
  tests/test_eval_runner.py tests/test_eval_cli.py
```

Two opt-in fixtures exercise real provider credentials and may spend tokens:

```bash
ciao eval --suite tests/fixtures/evals/skill-smoke.json \
  --workspace . --provider claude --output /tmp/ciao-eval-claude-skill

ciao eval --suite tests/fixtures/evals/subagent-smoke.json \
  --workspace . --provider codex --model sonnet \
  --output /tmp/ciao-eval-codex-subagent
```

After every scenario, the output directory contains atomic `results.json` and
`REPORT.md` snapshots. Results include status, assertion outcomes, output or
error, routing, selected/effective model, normalized tools, usage, token
totals, and timings. Live runs use the existing provider login and are not part
of the normal credential-free test suite.

### Public release evidence

Release scorecards use the schema-version-2 suite in `evals/release.json`.
They run both supported providers three times in cold, warm, and restart
mode. Cold runs use a fresh isolated workspace; warm runs repeat the measured
turns in one chat; restart runs preserve the synthetic vault while starting a
new server and chat.

```bash
ciao eval release \
  --suite evals/release.json \
  --workspace . \
  --version 0.0.0 \
  --output release-evidence/v0.0.0 \
  --from-ref v0.0.0

ciao eval compare \
  --baseline release-evidence/vPREVIOUS/summary.json \
  --current release-evidence/vCURRENT/summary.json
```

The generated `REPORT.md`, `summary.json`, `changes.json`, and
`rationale.md` are sanitized public artifacts. They contain aggregate
context/cache/token/latency metrics, tool and memory-source summaries, and
structural skill/MCP changes, but never prompts, model answers, vault content,
tool arguments, or credentials. Performance and cache regressions are
advisory; missing provider coverage and correctness failures stop release
preparation. The evidence remains reviewable in the release PR and worktree;
the release owner may attach or link it manually. No CI job runs or publishes
the scorecard.

Synthetic vault fixtures are the default. A local, sanitized vault can be used
for a private operator run with `--vault-root /path/to/vault`; external source
paths are hashed in the resulting public evidence and the vault is never copied
into the repository or release assets.

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
For sidebar chat-group changes, verify that a supervisor's delegate disclosure has a visible `aria-expanded` state, keeps a 44px touch target, hides and restores only its delegate rows, and automatically reopens when the selected chat is a delegate.
For composer drag-and-drop changes, test both local host and remote client roles:
host paths must be absolute, while client files must upload into the active
project on the host before the returned host path is inserted.

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
`type`, and reports broken relative Markdown links, broken wikilinks, orphan
pages, and duplicate stems. `INDEX.md`, `MEMORY.md`, and `log.md` are exempt
from the frontmatter requirement. External URLs, absolute paths, and anchors
are not treated as vault links.

Use `--vault-root` to choose a vault. Otherwise it uses `CIAO_VAULT_ROOT` or
`./memory-vault`. A clean scan exits 0. Findings, a missing vault root, or an
incomplete traversal exit 1. It never reports a clean vault when it could not
inspect every path.

### AI OS audit

`ciao os-audit` checks required workspace roots, vault frontmatter, relative
Markdown links, wikilinks, orphans and duplicate stems, skill budgets,
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

The weekly `system-workspace-hygiene` schedule runs `ciao vault-index --write` before `ciao os-audit --json`. A failed index rebuild blocks link and index repairs and prevents a healthy/no-op claim. The prompt treats audit exit 1 as findings and continues, but treats exit 2 as an unreliable scan and reports the errors without claiming success.

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

`ciao memory-audit` reads one file and skips the vault scan, so the daily
`system-memory-curation` schedule can afford to call it and fix what it finds.
Exit 0 clean, 1 findings, 2 a region could not be read.

## Skills, subagents, and slash commands

Packaged generic skills live in `ciao/stock/skills/` and are installed into every workspace's `.claude/skills/` by `ciao sync-skills` on startup. This includes Ciaobot-specific skills (`ciao-capabilities`, `web-research`, `workspace-authoring`, …) and the upstream **`gws-*` skills** for Google Workspace (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, Forms). In a **workspace**, user-owned skills live in `skills/`, project agents in `subagents/`, and slash commands in `commands/`; `ciao sync-skills` mirrors them into the generated `.claude/` directories and projects `.mcp.json` MCP servers into `.codex/config.toml` for Codex chats. The generated MCP block preserves user-owned Codex server tables and copies only environment references, never literal credentials. Locked GitHub/package skills follow the upstream `skills` CLI layout: their canonical directories live under `.agents/skills/`, with provider links under `.claude/skills/`; synchronization preserves either that layout or older `.claude`-canonical installs. A workspace skill with the same name as a packaged one overrides it.

The `gws-*` stock skills are regenerated from the installed `gws` CLI via `ciao/gws_skills.py` on release (`python -m ciao.release --apply`). The generator output is passed through Ciaobot curation: profile-wrapper command examples, integration auth notes in `gws-shared`, stripped upstream `openclaw` metadata and See Also boilerplate. Ciaobot-specific gws conventions also live in the system prompt (`ciao/system_prompt.md`).

Edit canonical sources, not the generated `.claude/`, `.agents/`, or `.codex/` dirs. Do not run `npx skills update` ad-hoc (it re-expands the lockfile and repopulates bloat); regenerate the `gws-*` skills through `ciao/release.py` rather than calling `gws generate-skills` by hand.

## DAG-style schedules (maintainers)

Some packaged schedules are multi-step workflows (load state, gate, model call, write). For these, use `ciao.dag` rather than a long `async def`:

- `Node(id, kind, model='', timeout_s=180.0, payload={})` — kinds: `bash`, `prompt`, `gate`, `subagent`, `retention`.
- `Edge(src, dst, when='ok')` — `when` is `ok` (default), `fail`, or `always`.
- `run(dag, edges, job=..., label=..., initial_ctx={})` — records each node in `.runtime/job_runs.jsonl`.

Canonical example: `ciao/skill_evolution.py:_process_skill_dag`. Use a DAG when there are 3+ sequential steps with branching and you want per-step timing on the Automation page.

`ScheduleManager.catch_up()` runs once at server startup. It dispatches only the latest missed occurrence for each enabled schedule, leaves the prompt unchanged, and records the missed occurrence's local date so a later slot on the startup day can still fire normally. Cover changes to this behavior in `tests/test_schedules.py`.

Delegates are normal chats carrying `spawned_from_chat_id`; the wake-on-completion path lives in `ProjectChatManager` (`_queue_delegate_wake` / `_flush_delegate_wake`). Result-ready toasts and pushes are skipped for delegates via `_announce_result_ready`; permission / question pushes are not. Cover changes in `tests/test_delegates.py`.

## MCP control plane

`ciao/control_plane.py` is the provider-neutral application boundary;
`ciao/mcp_server.py` is only its authenticated MCP adapter. Add business rules
to managers/control-plane methods, not tool handlers. Every tool must declare
read/write/destructive annotations, return a stable envelope, enforce scoped
workspace/project/chat access, and have focused protocol plus domain tests.
Self-affecting operations must defer until the caller chat drains. Provider
tokens must never enter the model's shell environment or telemetry arguments.

See `docs/MCP.md` for the catalog and Claude/Codex configuration.

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
