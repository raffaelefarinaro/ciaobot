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

### macOS venv workarounds

On recent macOS, `scripts/run-ciao.sh` and the `scripts/dev.sh` wrapper source `scripts/ensure-deps.sh`, which injects two self-healing workarounds into `.venv/bin/activate`:

- A `DYLD_LIBRARY_PATH` pointing at Homebrew's `libexpat`. macOS 26+ ships a system `libexpat` missing a symbol Homebrew Python's `pyexpat` needs, which otherwise crashes pip and venv creation with `ImportError: ... Symbol not found`.
- An `SSL_CERT_FILE` pointing at the venv's certifi CA bundle. The python.org Python build ships no CA bundle, so the bare `urllib` calls in the server (e.g. OAuth token refresh in `ciao/web/auth.py`) fail with `SSLCertVerificationError`.

No manual step is needed; `ensure-deps.sh` handles both. If you set up the venv by hand and hit either error, run `scripts/ensure-deps.sh` once to repair `activate`.

## Frontend build

```bash
npm install          # optional root Node tooling
cd web
npm install
npm run build        # typecheck + Vite build, outputs to ciao/web/static/
```

After PWA changes, rebuild and either restart the service or use the **Deploy** button in PWA Settings. **Never restart the ciao service from inside a PWA chat** (you'd sever your own session); ask the operator to deploy.

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

## Skills, subagents, and slash commands

Packaged generic skills live in `ciao/stock/skills/` and are installed into every workspace's `.claude/skills/` by `ciao sync-skills` on startup. In a **workspace**, user-owned skills live in `skills/`, project agents in `subagents/`, and slash commands in `commands/`; `ciao sync-skills` mirrors them into the generated `.claude/` directories. A workspace skill with the same name as a packaged one overrides it.

Edit canonical sources, not the generated `.claude/` dirs. Do not run `npx skills update` or `gws generate-skills` ad-hoc; both re-expand the lockfile and repopulate bloat.

## Change guidelines

- **Doc the change.** After any change to `ciao/`, `web/`, `scripts/`, `deploy/`, or `pyproject.toml`, dispatch the `doc-updater` agent before declaring the task complete. It refreshes `docs/ARCHITECTURE.md`, this file, `CLAUDE.md`, and `INTEGRATIONS.md` against actual repo state. Skip only for pure bugfixes that touch nothing in layout, capabilities, install steps, env vars, endpoints, or commands.
- **New API routes must be documented.** Add the route to `PWA_API.md`; state-changing routes also need an Agent recipe or an allowlist entry in `tests/test_pwa_api_docs.py`. New `CIAO_*` env vars must land in `INTEGRATIONS.md` or the allowlist in `tests/test_env_vars_documented.py`. Both are test-enforced.
- **Never restart the ciao service yourself** from inside the PWA. Apply code changes and ask the operator to hit Deploy.
- **Never commit `.env` or API keys.** `.env` minimum: `PWA_AUTH_TOKEN`.
- **Keep edits minimal and consistent with existing patterns.** Don't refactor unrelated code; if unrelated changes appear, pause and ask.
- **Avoid destructive git** (force push, hard reset on shared branches) unless explicitly asked.
- **Write tests** for new Python behavior; add to `tests/`. PWA changes verify via `npm run build` typecheck at minimum.
