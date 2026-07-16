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

`ciao setup` is idempotent. It writes the initial `.env`, seeds stock workspace files, copies the editable `CLAUDE.md` workspace guide, links `AGENTS.md` to that same guide for Codex, copies `CIAO_CUSTOMIZATION.md`, renders the server and menu bar plists under `~/Library/LaunchAgents/`, and creates `~/Applications/Ciaobot Server.app`, which starts the local service when needed and opens the `Ciaobot` PWA. Existing custom `AGENTS.md` files are preserved. By default setup does not load launchd; add `--load-launchd` when you want it to run `launchctl`.

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
ciao auth codex --print-only               # show Codex / ChatGPT login command
ciao auth ollama                           # run provider login helper
```

### macOS venv workarounds

On recent macOS, `scripts/run-ciao.sh` and the `scripts/dev.sh` wrapper source `scripts/ensure-deps.sh`, which injects two self-healing workarounds into `.venv/bin/activate`:

- A `DYLD_LIBRARY_PATH` pointing at Homebrew's `libexpat`. macOS 26+ ships a system `libexpat` missing a symbol Homebrew Python's `pyexpat` needs, which otherwise crashes pip and venv creation with `ImportError: ... Symbol not found`.
- An `SSL_CERT_FILE` pointing at the venv's certifi CA bundle. The python.org Python build ships no CA bundle, so the bare `urllib` calls in the server (e.g. OAuth token refresh in `ciao/web/auth.py`) fail with `SSLCertVerificationError`.

No manual step is needed; `ensure-deps.sh` handles both. If you set up the venv by hand and hit either error, run `scripts/ensure-deps.sh` once to repair `activate`.

### Homebrew distribution

macOS users can install from the [homebrew-ciaobot](https://github.com/raffaelefarinaro/homebrew-ciaobot) tap:

```bash
brew install raffaelefarinaro/ciaobot/ciaobot
```

The formula template lives in `deploy/homebrew/ciaobot.rb`. Regenerate it with:

```bash
./scripts/update-homebrew-tap.sh <version> <wheel-sha256>
```

On each GitHub release, `publish.yml` updates the tap automatically. Add a repo-scoped `HOMEBREW_TAP_GITHUB_TOKEN` secret to this repository so the workflow can push to `homebrew-ciaobot` (the default `GITHUB_TOKEN` cannot write across repos).

## Branching and releases

- **`develop`** is the integration branch. Feature and fix PRs target `develop`.
- **`main`** is release-only. Direct pushes and merges to `main` are blocked; only release PRs land there.
- **CI** (`.github/workflows/ci.yml`) runs on pushes to `develop` and on pull requests into `develop` or `main`.
- **Release prep:** from a clean checkout, run:

```bash
scripts/prepare-release --apply --create-pr --ready
```

  That cuts `release/vX.Y.Z` from `develop`, bumps `pyproject.toml`, `ciao/__init__.py`, `web/package.json`, and `web/package-lock.json`, refreshes `CHANGELOG.md`, runs release checks, and opens a PR into `main`. Use `--bump minor` or `--version X.Y.Z` when needed.

- **Publish:** merging the release PR into `main` triggers `.github/workflows/release-on-main.yml`, which creates the `vX.Y.Z` tag and GitHub release. `publish.yml` then builds the wheel, publishes to PyPI, and updates the Homebrew tap. A follow-up job merges `main` back into `develop`.

One-time GitHub setup for a fresh clone or repo admin:

```bash
./scripts/configure-github-branches.sh
```

That sets `develop` as the default branch and enables pull-request + CI requirements on `develop` and `main`.

## Frontend build

```bash
npm install          # optional root Node tooling
cd web
npm install
npm run build        # typecheck + Vite build, outputs to ciao/web/static/
```

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
cd web && npm test             # Frontend unit tests
cd web && npm run build        # Typecheck + Vite build (frontend smoke test)
```

For chat rendering changes, verify the compact `Activity` disclosure, `Outputs` placement, readable token labels, keyboard operation, and 44px touch targets at both desktop and narrow-phone widths.

## Skills, subagents, and slash commands

Packaged generic skills live in `ciao/stock/skills/` and are installed into every workspace's `.claude/skills/` by `ciao sync-skills` on startup. This includes Ciaobot-specific skills (`ciao-capabilities`, `ciao-automations`, `vault-read`, …) and the upstream **`gws-*` skills** for Google Workspace (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, Forms). In a **workspace**, user-owned skills live in `skills/`, project agents in `subagents/`, and slash commands in `commands/`; `ciao sync-skills` mirrors them into the generated `.claude/` directories. Locked GitHub/package skills follow the upstream `skills` CLI layout: their canonical directories live under `.agents/skills/`, with provider links under `.claude/skills/`; synchronization preserves either that layout or older `.claude`-canonical installs. A workspace skill with the same name as a packaged one overrides it.

The `gws-*` stock skills are regenerated from the installed `gws` CLI via `ciao/gws_skills.py` on release (`python -m ciao.release --apply`). The generator output is passed through Ciaobot curation: profile-wrapper command examples, integration auth notes in `gws-shared`, stripped upstream `openclaw` metadata and See Also boilerplate. Ciaobot-specific gws conventions also live in the system prompt (`ciao/system_prompt.md`).

Edit canonical sources, not the generated `.claude/`, `.agents/`, or `.codex/` dirs. Do not run `npx skills update` ad-hoc (it re-expands the lockfile and repopulates bloat); regenerate the `gws-*` skills through `ciao/release.py` rather than calling `gws generate-skills` by hand.

## DAG-style schedules (maintainers)

Some packaged schedules are multi-step workflows (load state, gate, model call, write). For these, use `ciao.dag` rather than a long `async def`:

- `Node(id, kind, model='', timeout_s=180.0, payload={})` — kinds: `bash`, `prompt`, `gate`, `subagent`, `retention`.
- `Edge(src, dst, when='ok')` — `when` is `ok` (default), `fail`, or `always`.
- `run(dag, edges, job=..., label=..., initial_ctx={})` — records each node in `.runtime/job_runs.jsonl`.

Canonical example: `ciao/skill_evolution.py:_process_skill_dag`. Use a DAG when there are 3+ sequential steps with branching and you want per-step timing on the Automation page.

`ScheduleManager.catch_up()` runs once at server startup. It dispatches only the latest missed occurrence for each enabled schedule, leaves the prompt unchanged, and records the missed occurrence's local date so a later slot on the startup day can still fire normally. Cover changes to this behavior in `tests/test_schedules.py`.

`ProviderSubchatManager` handles routing, limits, and executing participant turns. Cover changes to this behavior in `tests/test_provider_subchats.py` (for manager logic/limit tracking) and `tests/test_provider_subchat_routes.py` (for Starlette HTTP handlers).

## Change guidelines

- **Doc the change.** After any change to `ciao/`, `web/`, `scripts/`, `deploy/`, or `pyproject.toml`, refresh `docs/ARCHITECTURE.md`, this file, `CLAUDE.md`, and `INTEGRATIONS.md` against actual repo state before declaring the task complete. Skip only for pure bugfixes that touch nothing in layout, capabilities, install steps, env vars, endpoints, or commands.
- **New API routes must be documented.** Add the route to `PWA_API.md`; state-changing routes also need an Agent recipe or an allowlist entry in `tests/test_pwa_api_docs.py`. New `CIAO_*` env vars must land in `INTEGRATIONS.md` or the allowlist in `tests/test_env_vars_documented.py`. Both are test-enforced.
- **Never restart the ciao service yourself** from inside the PWA. Apply code changes and ask the operator to hit Deploy.
- **Never commit `.env` or API keys.** `.env` minimum: `PWA_AUTH_TOKEN`.
- **Keep edits minimal and consistent with existing patterns.** Don't refactor unrelated code; if unrelated changes appear, pause and ask.
- **Avoid destructive git** (force push, hard reset on shared branches) unless explicitly asked.
- **Use the branch model in `CONTRIBUTING.md`.** Day-to-day PRs target `develop`; release PRs target `main`.
- **Write tests** for new Python behavior; add to `tests/`. PWA changes verify via `npm run build` typecheck at minimum.
- **Verify UI accessibility.** For PWA layout changes, check keyboard operation, visible focus, browser zoom, and 44px mobile targets at a narrow-phone viewport in addition to the build.
