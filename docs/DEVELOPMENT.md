# Ciaobot Development Guide

Setup, dev workflow, testing, and change guidelines. For the system design, read `docs/ARCHITECTURE.md` first.

## Server install

Linux production and every installer-managed macOS install restart the engine
from Settings through `POST /api/admin/restart`; `CIAO_DEV_MODE=true` on a
source checkout retains the source deploy workflow.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
ciao setup --workspace /tmp/ciao-workspace
ciao run
```

`ciao setup` is idempotent. It writes the initial `.env` (including the selected port), seeds stock workspace files, copies the editable `AGENTS.md` workspace guide (both providers discover it natively; nothing writes a `CLAUDE.md` any more) and copies `CIAO_CUSTOMIZATION.md`. On macOS it also renders the server plist under `~/Library/LaunchAgents/` and removes retired launcher bundles. Existing custom `AGENTS.md` files and configuration values are preserved. By default setup does not load launchd; add `--load-launchd` on macOS to run `launchctl`. Linux setup creates no desktop/service files unless an explicit `--launch-agents-dir` requests an offline plist export. See [Linux hosting](LINUX.md) for systemd and HTTPS deployment.

The weekly dependency-changelog review is an operator-owned routine, not part of the public app install. In a maintainer workspace it lives at `scripts/dependency_review.py` and invokes this checkout for the DAG/runtime; public release preparation uses only the generic helpers in `ciao/dependency_updates.py`.

A fresh first logical workspace and workspaces added later in Settings live at
`<CIAO_VAULT_ROOT>/<workspace-name>/`. Their registry path is read-only in the
PWA. Existing-folder setup preserves the selected notes in place so the
onboarding agent can inspect them; `ciao os-audit` then offers a model-guided,
backed-up migration into the standard named folder. Settings → Workspaces →
Archive never deletes or merges a workspace: it moves the folder intact to
`<install>/.archived-workspaces/<name>-<YYYYMMDD-HHMMSS>/` and unregisters it
(`ciao/workspace_archive.py`); tests for it live in
`tests/test_workspace_archive.py`.

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

The release publishes the **engine only** (#653). Its assets are the wheel, the
signed engine manifest, and the installer — there is no app archive, no
updater feed, no native verifier and no bundled runtime attached, so a machine
still running the retired macOS app has no update path left and is moved with
the terminal one-liner below. `install.sh` — the one-liner every README, site
page and stale external link already names — is the **engine** installer, so a
first-time user gets the engine and nobody can install the app.
`install-engine.sh` is published as the same bytes under the other name the
already-merged transition release's hand-over fetched (#604); the app
installer is not published under any name.

The retired app installer is no longer in the tree. A DMG is intentionally
not built or attached to releases. The release workflow generates the public
`install.sh` from `scripts/install-engine.sh`; legacy app migration and
uninstall support remains for existing installations.

`scripts/install-engine.sh` is the installer the release serves: it verifies the
signed engine manifest with the release minisign key embedded in the script, and
the wheel's digest and size, before anything is installed; installs the verified
wheel with `uv tool install`; writes the install receipt with absolute paths;
then runs `ciao setup` and `ciao service start` and prints the one-time login URL
to the terminal. It refuses to take over an engine the retired app manages
unless it is re-run with `--migrate`, and refuses to overwrite a `ciao` it did
not install. `--migrate`
is the app→terminal hand-over (#576): after the same manifest and digest
verification, it classifies the Mac from the verified wheel, takes before-images
of the two plists, the shim, the install receipt and any existing uv tool
environment in `~/.local/state/ciaobot/migration/before/` (an absent file is
recorded as absent, so a rollback removes only what the migration created, and
the tool environment is copied only when one is already there — the common
hand-over from the retired app has none), and refuses to touch anything if that
app is still running 20 s after it was asked to quit. It then either
repoints `com.ciao.server` at the new engine and, only once that engine answers
with the version just installed, retires the app's own agent — restoring the
plists, the shim, the receipt, the tool environment and the launchd job on any
failure, including a launchctl that refuses to put them back, and loading and
starting that agent again whenever the transaction had already booted it out
(the two are tracked separately, because a plist that is still there is not the
same fact as a job launchd still holds) — or, for a client,
disables the local engine and installs no service at all, leaving the user to
sign in at the remote host. A client failure undoes the same state and never
claims an engine was restored, because there was none. `--as-host` /
`--as-client URL` are required when the node state cannot be read or trusted;
the URL is checked with the classifier's own rule (scheme, host name, no
credentials or whitespace), so an override like `https://` is refused before a
single label or file is touched rather than after this Mac has given up its own
engine. The migration receipt in `~/.local/state/ciaobot/migration/` (`schema`,
`phase`, `before` block, `version`, `retiring_desktop`, `app_bundle`,
`started_at`) is what
makes the whole thing
resumable: a retry reuses those originals rather than snapshotting the tool the
previous attempt installed, and only a receipt that parses, records all five
before-images at the paths this installer writes them to, still has them on
disk, and agrees with the installed service — its version, its entry point and
tool environment, and for a host the `com.ciao.server.plist` program pointing
at that entry point rather than inside the app bundle — is treated as
"already migrated"; a corrupt, wrong-schema, incomplete or stale one is refused
with recovery instructions instead, and a stale settled receipt is re-run from
the originals it kept, out loud. An interrupted run records `interrupted`
rather than pretending to have finished, a retirement that launchctl refuses
leaves the app's agent in place instead of reporting success, and a `retiring`
receipt that was being taken to `migrated` when the process stopped is finished
rather than treated as an ordinary installer-managed engine. Any receipt naming
an unfinished host hand-over (`started`, `installed_no_start`, `interrupted`)
is finished the same way: by the time those phases are on disk, `ciao setup` has
usually already repointed `com.ciao.server` at the tool this script installed, and
an engine outside a `.app` is exactly what the classifier calls an ordinary
install — so the kind and the workspace come from the receipt, which is the only
thing left that knows what the run was doing, and taking the host path from them
is what stops the app's own agent from staying loaded next to the engine that
replaced it. That path also settles the workspace before it takes any
before-image or asks the app to quit, and only ever hands over the one the
engine being replaced runs in: a `--workspace` naming a different directory, one
that does not exist, or a directory with no `.env` in it is refused, no
workspace is created during a hand-over, and an `--as-host` override on a state
whose workspace could not be recovered has to name an existing one rather than
get a fresh `~/Ciaobot`. Taking a different workspace would start a second engine
with a fresh password and a fresh runtime root next to the real ones, retire the
app's agent, and leave the original and every chat in it behind while the receipt
still named the original. A hand-over that reaches `migrated` also removes the
retired `Ciaobot.app` — the bundle the classifier named, guarded to a `.app`
directory, deleted only after the app's own agent is gone, and reported rather
than fatal if it would not go (`ciao desktop uninstall` is the manual fallback).
Nothing before that removes it: `--no-start` leaves the app's agent loaded, a
client gets somebody else's engine, and every rollback needs the bundle because
the engine it hands back runs out of it. The bundle is recorded in the receipt
(`app_bundle`) because a resumed run can no longer read it from the engine plist,
which the first run already repointed. The ordinary, non-`--migrate` path is
unchanged: there
`--workspace` is how a workspace is named, and it is created. The workflow
attaches it as the `install-engine.sh` release asset, and again as `install.sh`.

The classifier was the retired app's own hand-over bridge for that transition
release (#604): its updater installed a signed `.app.tar.gz`, which cannot run a
shell script, so an app user never re-runs the one-liner on their own. The app
therefore asked the classifier (`ciao service migration-classify`, a read-only
bridge to `ciao.engine_migration`) what this Mac is at every launch, and
offered to hand the engine over when the answer was a live `desktop_host`,
`desktop_client` or `desktop_invalid` — `engine`, `none` and `desktop_stale` were
offered nothing. The offer appeared once, guarded by its own
`engine_migration_notice_shown` setting field, with a **Move Engine to the
Terminal Installer…** item directly under Update for a user who dismissed it.
Choosing it downloaded the `install-engine.sh` asset for the app's own version —
never `latest`, which could move mid-install — from the pinned
`releases/download/v<version>` URL, ran it detached with `--migrate` and `nohup`
in its own process group, and quit so the installer's 20 s `quit_desktop_app`
wait and the launchd hand-over could proceed. A `desktop_invalid` Mac was asked
whether it was the host (`--as-host`) or a client of an address
(`--as-client URL`); neither is ever guessed, and the installer re-validates the
URL from the verified wheel before it touches anything. The migration ran in the
runtime root, not the app bundle — the app was quitting and the bundle could be
replaced underneath it — and its transcript is at
`<runtime root>/engine-migration.log`.

None of that ships any more: the app is retired (#579) and its bundle is never
published, so nobody can take that path. What stays is the engine side —
`ciao service migration-classify` still reads and classifies, and
`install-engine.sh --migrate` still does the hand-over for a machine that has
not run `ciao desktop uninstall` yet.

## Branching and releases

- **`develop`** is the integration branch. Feature and fix PRs target `develop`.
- **`main`** is release-only. Direct pushes and merges to `main` are blocked; only release PRs land there.
- **CI** (`.github/workflows/ci.yml`) runs on pushes to `develop` and on pull requests into `develop` or `main`.
  Every PR runs the `linux-server` job: `mypy ciao`, `pytest -n auto tests/`,
  `npm test`, `npm run build` and a package smoke test, in about 5 minutes. The
  full macOS `test` job (coverage, browser tests, and the engine wheel this
  branch would publish, installed into a throwaway venv and cold-started — the
  launchd, installer, engine-update and migration coverage — about 18 minutes)
  is gated by `if: github.event_name != 'pull_request' || github.base_ref ==
  'main'`, so it runs on pushes (including to `develop`) and on PRs into `main`,
  but not on a PR into `develop`. Nothing builds the app any more, so there is
  no diff for it to react to; a macOS-only regression in a `develop` PR shows up
  on the post-merge push run instead.
- **Release prep:** from a clean checkout, run:

```bash
scripts/prepare-release --apply --create-pr --ready
```

  That cuts `release/vX.Y.Z` from `develop`, aligns the Python and PWA versions
  and lockfiles, refreshes `CHANGELOG.md`, runs release
  checks, and opens a PR into `main`. Use
  `--bump minor` or `--version X.Y.Z` when needed.

- **Publish:** merging the release PR into `main` triggers `.github/workflows/release-on-main.yml`, which creates the `vX.Y.Z` tag and GitHub release. `publish.yml` then builds the PWA and the engine wheel, verifies the wheel in a clean environment, signs the engine manifest with the release minisign key (`ciaobot-engine-manifest.json` + `.sig`, gated by `ciao.release_manifest verify`) and attaches five engine assets: `install.sh`, `install-engine.sh`, the wheel, the manifest and its signature. Since #653 it publishes no app, no `latest.json` feed, no native verifier and no bundled runtime, and its signer is `@tauri-apps/cli` run standalone, so it never depended on the now-deleted `desktop/` tree. It does not publish PyPI, Homebrew, or DMG artifacts. A follow-up job merges `main` back into `develop`.

One-time GitHub setup for a fresh clone or repo admin:

```bash
./scripts/configure-github-branches.sh
```

That sets `develop` as the default branch and enables pull-request + CI requirements on `develop` and `main`.

### GitHub code scanning

CodeQL runs through GitHub's repository-level default setup and is independent of
Ciao CI. GitHub also offers an optional **AI Scan for pull requests** workflow;
that workflow is GitHub-managed (`dynamic/agents/github-advanced-security`) and
its model is injected by GitHub, so there is no model pin in `.github/workflows`
to update in this repository. If a run reports `The requested model is not
supported`, check the repository setting before changing application code:

```bash
gh api repos/raffaelefarinaro/ciaobot/code-scanning/ai-scan
```

If the owner chooses to remove the failing advisory invocation (while keeping
CodeQL enabled), disable it explicitly:

```bash
gh api repos/raffaelefarinaro/ciaobot/code-scanning/ai-scan \
  --method PATCH -f pr_scan=disabled
```

The setting is reversible. Re-enable AI Scan only after GitHub/Copilot reports
that its configured model is supported; do not make Ciao CI pass or fail based
on this advisory workflow. The GitHub Settings → Code security → Code scanning
page provides the same toggle when the REST API is unavailable.

## Frontend build

Node 22 is the supported version (`.nvmrc`, and what CI uses). The floor is
`^22.22.2 || ^24.15.0 || >=26.0.0`, set by jsdom 30 — below it every jsdom test
file fails to start its worker, and vitest still reports a pass for the subset
that ran. `npm test` preflights this and exits with an explanation rather than
producing a misleading green summary.

The floor was `^20.19.0 || ^22.13.0 || >=24.0.0` under jsdom 29, which is why
Node 20 was supported. jsdom 30 raised all three lines and dropped the 20.x one,
so Node 20 is no longer supported here — it is EOL, and following jsdom keeps
the declared range and the real one identical.

```bash
nvm use              # reads .nvmrc → Node 22
npm install          # optional root Node tooling
cd web
npm install
npm run build        # typecheck + Vite build, outputs to ciao/web/static/
npm test             # 61 test files under web/src
```

## macOS engine development

The macOS app is retired and its source tree is gone (`#656`): the release is the
engine, and the one-line installer installs that. Nothing under `desktop/` is
built, so there is no Rust or native-shell step to run here.

`ciao desktop uninstall` stays for the compatibility window: run it to remove a
`Ciaobot.app` an older install left behind, along with the launch agents and
`ciao` shim pointing into it.

After PWA changes, rebuild and either restart the service or use the **Deploy** button in PWA Settings. **Never restart the ciao service from inside a PWA chat** (you'd sever your own session); ask the operator to deploy.

Restart requests made through the running server enter a drain phase: existing chats and background agents finish before shutdown, and new turns are not admitted during that window. Directly killing the process bypasses this protection. A drain that is cancelled (an update whose drain timed out) replays the background-run and CLI-task wakes it deferred instead of losing them, since no restart follows to deliver them.

## Local PWA dev

The installed-app share target is a service-worker-intercepted multipart POST
to `/share-target`, not an API endpoint. The worker stores the received payload
in browser IndexedDB; Home reviews it before adding it to a draft. Keep that
request off the authenticated `/api/*` surface and do not auto-send it. Browser
support for OS share targets varies; test the incoming path in a real installed
Chromium PWA and check the unsupported-browser attachment path as well.


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
ciao eval contracts --json # Deterministic behavioral-eval guard checks (model-free; CI half)
ciao eval run --model <id> --label candidate # Bounded model-backed behavioral probe (explicit; cost ceiling)
ciao eval compare --baseline a.json --candidate b.json # Baseline/candidate delta with provenance
cd web && npm test             # Frontend unit tests
cd web && npm run build        # Typecheck + Vite build (frontend smoke test)
```

When the Critique panel picker is Automatic, `ciao/critique.py` collects the
distinct effective default models of configured workspaces, skipping providers
that are not signed in. The `/critique` command and critique skill use this same
panel; `tests/test_critique.py` covers its resolution.

When removing a background job, add its id to `job_runs.RETIRED_JOBS`:
`job_runs_latest.json` keeps the last run of every job it ever saw, and the
debug report would otherwise keep listing it with a stale last run.

For memory-backup changes, use `tests/test_backup_service.py`: it drives a temporary
install against a local bare remote on an injected clock, so it never touches a real
repository, a real remote or wall-clock time. The cadence assertions are about what the
loop asked to wait for, the offline path is exercised by making a push time out, and the
run's own scope and readiness rules are pinned from both sides (a note is committed, a
file outside the scope is not, a clean run creates nothing). `tests/test_local_routes.py`
covers the three routes. The five-minute interval is the named constant
`BACKUP_INTERVAL_S` in `ciao/backup_service.py`, and its runs are recorded in
`job_runs` under the id `branch_backup`.
Settings → General shows a compact online backup state and last successful upload;
the configured scope, repository, and raw diagnostic are in a native "Backup details"
disclosure ("Review details" when attention is needed). A coverage gap — paths git
already tracks that the scope refuses to commit, so a repository that is also a
checkout is only partly covered — is a neutral note under whatever the state says,
driven by the status's `coverage_gap` count rather than by parsing the `reason`
sentence, and it never reddens a backed-up install (#733). The five-minute cadence is
in the section description. There is no separate backup guide.

For chat rendering changes, verify the compact `Activity` disclosure, `Outputs` placement, readable token labels, keyboard operation, and 44px touch targets at both desktop and narrow-phone widths. Markdown tables should shrink-wrap on desktop and keep readable first-column labels inside a horizontally scrollable table viewport on narrow screens.

For engine-update changes, `tests/test_engine_update.py` is the contract: launchd, uv, the service starter, the engine's HTTP surface, the clock and sleep are all doubles, so nothing in it may start a real service, move a real environment or open a socket. An update that a reboot or a killed job interrupted is picked up by a durable `com.ciao.recover` LaunchAgent that `apply_update` installs (from the staged interpreter, `StartInterval` 30) *before* the engine is stopped, and that `run_apply` re-points at the retained `previous-env` the instant the live env is renamed aside — so the swap never consumes the directory the net runs from — the crash window where the live env is renamed aside leaves the engine unable to start, so a recovery reached from inside the engine is a recovery that cannot run. Its program is `run-recover --operation <id>`: `recover_apply` takes the lock without waiting, stands down for a tick when a swap holds it, never posts (it owns no drain) and never mutates a record it did not find stranded, retires its own job and plist when there is nothing left to recover or once the rollback has settled, and otherwise runs the same total `_rollback` the apply would have, using the retained `previous-env` as the evidence that the move happened. `recover_interrupted_apply` from `ciao/main.py` (macOS only) stays as the fast path for the cases where the engine does come back: it recognises the record and bootstraps the detached `com.ciao.updater` job in `run-recover` mode. The rollback is never the engine's own doing, because it cannot boot itself out and restore the env it is running out of. In the other direction, `run_apply` refuses an apply when the loaded `com.ciao.server` does not run the env the install receipt names. Staging resolves the release once, into a real `uv tool env` under `<stage>/tool/<dist>` with `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` pinned to the stage dir, and records the resolved set as `Operation.env_freeze`; the apply then *moves* that env into the live env's place instead of installing the wheel again, re-pointing only what a move breaks (the `bin` shebangs and the `bin_dir` entry points), so the swap needs no network and cannot pick up a dependency released since staging. Do not run `ciao update apply`, `run-apply`, `run-recover` or real `launchctl` against your own install to check any of it.

For HTML artifact changes, keep the preview self-contained: inline scripts/styles and `data:` media are allowed, while external requests and `blob:` sources must remain blocked. Use the fixtures under `tests/fixtures/html_artifacts/` plus the focused workspace-HTML tests. The response body carries the injected comment bridge (`ciao/web/artifact_bridge.py`): keep it ES5, marker-tagged, and idempotent, never inject ahead of a doctype (a `<script>` before it renders the artifact in quirks mode), and keep `action: 'ready'` deferred to `DOMContentLoaded` — that message is the parent's only cue to push comment highlights, and anything pushed earlier reaches a frame that is still loading. `tests/test_workspace_html.py` asserts the injection and the header contract together; `web/src/lib/artifactBridgeScript.test.ts` runs the script itself in jsdom for anchoring and highlight behaviour.
For workspace navigation changes, verify that unmodified `1`–`9` keys follow the visible sidebar workspace order, do not fire from text inputs, and keep working in the automations view. The sidebar key labels should remain visible and accessible at narrow widths. An open `AskUserQuestion` card takes those digits over for its own options while it is up (Design System rule S7) and hands them back when it closes, so check both states after touching either handler.
On the home screen, also verify that it shows only the selected workspace's chats (switching workspaces swaps the content) and that arrow keys follow the rendered lane layout: up/down moves between stacked lanes, left/right moves within a lane.
A memory pass is not a chat row anywhere, so its whole surface is the one
**memory insights** section below the tiers. `web/src/lib/memoryInsights.ts` owns
that derivation — one row per archived conversation, the in-flight archive and the
pass joined, the pass winning the phase, and every label — and
`web/src/lib/__tests__/memoryInsights.test.ts` drives it with no Pinia. When you
touch it, check the overlap case (an archive request still in flight while its
pass is already running must stay ONE row), the fallback (no pass yet, so the row opens
the archived transcript), the dead entry (no file and no pass, so the row is
disabled), the counter rules (`chatIsAttentionItem` is what the Home nav badge
and the lane status sentence both ask), and that the pass never reappears in
`activeChatsAll`, `projectChats`, `totalUnread`, the sidebar or a schedule
target. A pass's own archive is not a second row: `memoryInsights` skips any chat
that is a pass when reading archive state.
For Work details changes, verify the rail and the narrow-pane drawer together: both render `AgentContextSection.vue` and the running-subagent list, and the ⓘ toggle moves focus between the rail heading and the chat-body tab.
For composer drag-and-drop changes, test the desktop-drop grant path end to
end. Drops preserve the source file and add Markdown companions, and return
bounded opaque file references rather than absolute paths; the server expands a
reference only when building the provider prompt. The generic ProjectView
upload remains unchanged.

For security changes, the boundary is one origin and one session: every
`/api/*` route needs the signed cookie (minus the small public allowlist in
`ciao/web/auth.py`), every `/ws/*` handshake is checked for same-origin before
the session, and every state-changing `/api/*` request must present a matching
`Origin`/`Referer`. The loopback-only set (`_LOOPBACK_ONLY_API`, the update
drain and the local feed) is gated on the TCP peer address, never the `Host`
header. Do not add a capability or an origin exception for a page the model or
a remote browser can influence. Auth/origin coverage is in
`tests/test_auth_security.py`; hostile artifact coverage is in
`tests/test_workspace_html.py`. This does not yet confine broad file endpoints,
isolate browser storage/service workers, or replace the host password protocol
with revocable per-device credentials; those remain follow-up work for #546.

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

### Learnings migration

`ciao learnings-migrate` converts an existing workspace's
`Workspace/Learnings.md` onto the canonical record model in
`ciao/learning_records.py`, which is also what the writer (`append_learning`) and
the curation worklist read. Run it once per install that predates #755 — the
legacy shapes it converts are entries nothing downstream can attribute, so a
sighting of one mints a *new* record instead of adding to the one already there,
and the `xN` the care schedule promotes on quietly stops meaning anything.

```bash
ciao learnings-migrate --vault-root memory-vault/personal   # preview
ciao learnings-migrate --vault-root memory-vault/personal --apply
ciao learnings-migrate --vault-root memory-vault/personal \
  --revert .runtime/migration/learnings-20260929-230309.json --apply
```

It is dry-run by default and the preview *is* the apply, not a description of it.
Only the spans of recognized `## Active` entries are rewritten: frontmatter,
format notes, the whole `## Promoted / Resolved` section, a BOM and CRLF line
endings all survive byte for byte, and no entry is ever dropped. A line whose
shape cannot be read is reported on stderr, kept exactly as written, and reflected
in a non-zero exit — a shape nothing downstream can count must not be reported as
a finished run.

Each applied run writes one timestamped receipt under
`<runtime>/migration/` holding the exact spans it replaced, so `--revert` restores
the original bytes rather than re-deriving what the line probably said. Every span
is re-checked before it is replaced, and a file edited since the migration is
left entirely untouched rather than half-reverted. A receipt is only written for a
run that actually wrote; reversing a dry run would corrupt the file instead of
restoring it. The command is idempotent: a migrated line renders as itself, so a
second `--apply` finds nothing to change and writes no second receipt.

`Learnings.md` is bookkeeping rather than an entity note, so this is deliberately
*not* a `commit_note_change` — see `ciao/learnings_migrate.py` for why, and for
the lock it takes against a concurrent `[learnings]` accept.

One consequence worth knowing when reading a vault mid-migration: a line that was
*read* rather than witnessed keeps its unknown recurrence. A plain bullet renders
`[unknown → unknown] (?)` and a date-only legacy line `[2024-05-01 → 2024-05-01] (?)`,
and both stay there. `curation_run` skips an entry whose count is unknown rather than
treating it as `x1`, so it is not promotable until attributable evidence
accumulates — a bullet nobody can attribute has no recurrence, and inventing one
would be a decision nobody made. A `[learnings]` accept is the one exception, because
the writer witnessed that sighting: it files the entry at today's date and `(x1)`
with no citation, and every source it is given from then on counts on top of that.
The migration is what gives the read lines an identity to accumulate evidence against.

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

The daily `system-memory-curation` schedule is presented as **Workspace care**. Its packaged prompt runs lightweight memory passes nightly and uses `Workspace/Curation-Log.md`'s `last_full_pass` marker to catch up the deeper weekly work after downtime. A full pass runs `ciao vault-index --write` before `ciao os-audit --json --scope workspace`; a failed index rebuild or audit exit 2 leaves the marker overdue and prevents a healthy/no-op claim. Exit 1 means reliable findings and the pass continues with only safe structural repairs. A full pass also reviews the workspace guide body (AGENTS.md) for misplacement, drift, and bloat, applying the same state-vs-event and entity-placement rules the regions follow; that model-judged review is separate from the two required weekly checks, so an over-budget run that never reaches it does not suppress the next week's guide care.

### Bounded-memory rot audit

`ciao/memory_audit.py` checks whether the content of the always-loaded
`ciao:memory` / `ciao:profile` regions has rotted, as opposed to the mechanical
checks (caps, expiry, exact duplicates) that `audit_memory` already ran. It rests
on one rule: a remembered fact is either **state**, a current value that gets
replaced when it changes, or an **event**, a thing that happened which gets
appended to a log and never edited. The regions are a state surface.

The write policy for every path that can touch durable memory — attended
remember, the memory pass, unattended curation, direct edit, and proposal
acceptance — is stated once in `ciao/memory_policy.py` and described in
`docs/ARCHITECTURE.md` under "Memory write policy matrix". Two rules matter for
any change here: the region cap is **advisory** on every path (a write goes
through and reports `over_cap`; consolidation, not refusal, bounds a region),
and an **unattended run defers** approval-requiring work and reports it instead
of asking or routing around the missing reviewer. `tests/test_memory_policy.py`
pins the matrix to the stock assets and the docs, so a copy that contradicts it
fails the suite.

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
which never go stale. One predicate, `memory_audit.note_verification`, ages each note from
frontmatter `updated:` (a deliberate "I re-checked this" claim) or file mtime,
against per-type horizons (project 30d, person 90d, everything else 180d; types
resolved through the vault's alias table; `log`, `journal` and `workspace`
exempt — flagging an inbox for being an inbox is noise). `find_stale_notes`,
the Memory Map's `stale` flag and the vault-review `unverified` signal all call
it, so the three cannot disagree. The map additionally leaves `stale` off notes
the review queue never lists (`Workspace/` paths, templates,
`projects/completed/`), so its "unchecked" count is what the queue can show.
Like `superseded_state_candidates` these findings are informational: they
surface in `os-audit`'s memory section, the Memory Map, the vault-review queue,
and the daily `system-memory-curation` run, which re-verifies each note and
stamps `updated:` when the facts still hold.

`ciao memory-audit` reads one file and skips the vault scan, so the daily
`system-memory-curation` schedule can afford to call it and fix what it finds.
`--with-vault` adds the note-aging pass (still informational, never changes the
exit code). Exit 0 clean, 1 findings, 2 a region could not be read.

The vault-review queue (`ciao/vault_review.py`, the `Review` panel, and
`POST /api/vault/review`) disposes of a candidate with one of six dispositions,
and every one of them appends a row to the append-only
`Workspace/Vault-Review.jsonl`: `keep` (stamps `updated:` and suppresses until
the note's own bytes change), `reopen` (undoes a keep), `trash`/`restore` (the
reversible workspace trash, which nothing purges on a timer), `delete` (attended
permanent deletion, only from the trash and only with exact confirmation), and
`complete`. A sixth, `vanished`, is written by the system for a note that left
the vault by an ordinary file delete, so the ledger accounts for what left.

`complete` is the one that MOVES a note rather than removing it, and it exists
because retiring a finished project was the wrong instrument: `trash` left
`status: active` in a note that had left the active tree, and every note linking
to it kept a reference to a file the queue had stopped listing. It accepts a
`type: project`, or any note under `projects/` that declares no type at all (a
declared type always wins, so a `type: person` note filed there is not a
project), and moves it to `projects/completed/` — the whole folder for
`projects/active/<slug>/`, the one file for a flat `projects/<name>.md`.
Whatever the candidate was, a folder project moves as a folder, so **every** note
under it moves and every reference to any of them is repointed, in both
dialects, through the pure `vault_rehome.rewrite_references` primitive.
`restore_completed` reverses one such row, and looks each recorded undo image up
where the completion left the note but writes it back through where the project
has just landed — a note inside the folder has a different path in each of those
two moments. Three ordering rules are load-bearing if you touch it: the
links are rewritten BEFORE the move, because resolving them needs the notes to
still be on disk; the ledger append is inside the same transaction, so a
failure anywhere puts the move and every rewritten note back together.

In the `Review` panel, a candidate the backend marked `completable` offers
**Complete** in place of **Retire** — never both, since a project that finished
and a project that is wrong are different findings and the user has to be able
to tell them apart. `completable` is computed in the payload, and the panel
reads it: the UI has no alias table and no view of the `projects/` layouts, so a
second definition of project-ness there would be free to disagree with the one
that decides whether the click is honoured. It answers the same three questions
`complete_project_note` asks before it writes anything — is this a project
(`_is_project_candidate`), is there a layout to complete into
(`_completed_path_for`), and is the destination free (`_completion_move_to`) —
so a row that offers Complete is a click the engine accepts. The third one is
why a candidate carries its vault root: it is a question about the disk, it
answers `false` when `projects/completed/<name>` is already occupied, and the
row falls back to **Retire** instead of a button whose only possible answer is
409. The flag is not a second verdict, and it is deliberately narrow: it never
offers Complete on a row that Retire could have handled.

**Retire is therefore unreachable on a completable project row**, which is a
consequence of "in place of" and not an oversight: there is one terminal action
per row, and a project row spends it on closing the project out. A project that
is wrong rather than finished has no row-level trash today; a second action on
those rows is a follow-up, not a bug. **Complete** asks for confirmation where
**Retire** does not, for the same reason: `restore_completed` exists on the
engine but no panel surface calls it, and a completed note is in neither the
candidate list nor the trash, so a misclick has no in-app way back.

Completing clears the row like any other disposition; `restore_completed` is not
surfaced in the panel yet (a completed note is in neither the candidate list nor
the trash, so there is no row to hang it on), and a surface for it is a
follow-up.

## Skills, subagents, and slash commands

Packaged generic skills live in `ciao/stock/skills/` and are installed into every workspace's `.claude/skills/` by `ciao sync-skills` on startup. This includes Ciaobot-specific skills (`ciao-capabilities`, `ciao-memory`, `ciao-support`, `workspace-authoring`, …) and the upstream **`gws-*` skills** for Google Workspace (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, Forms). The `gws-*` skills are gated on the workspace having a Google account linked: `sync_workspace_skills` resolves each agent root's effective profile (its `gws_profile`, else the operator default only when that account actually exists) and skips the GWS skills when the workspace has no profile connected — shipping wrappers that name a credential directory nobody created just produces auth errors. In a **workspace**, user-owned skills live in `skills/`, project agents in `subagents/`, and slash commands in `commands/`; `ciao sync-skills` mirrors them into the generated `.claude/` directories. A workspace skill with the same name as a packaged one overrides it.

No subagents are packaged by default. Attended vault writes and proposal handling use `ciao-memory`; the Workspace care schedule carries the unattended lease, worklist, and approval rules and runs vault writes sequentially in its own chat. Reading and Google Workspace tasks use the provider's own web-fetch and the `gws-*` skills directly. Startup sync prunes retired stock-managed `memory`, `researcher`, and `secretary` from generated Claude/OpenCode inventories; custom subagents and unmanaged agent files are left alone.

The `gws-*` stock skills are regenerated from the installed `gws` CLI via `ciao/gws_skills.py` on release (`python -m ciao.release --apply`). The generator output is passed through Ciaobot curation: profile-wrapper command examples, integration auth notes in `gws-shared`, stripped upstream `openclaw` metadata and See Also boilerplate. Ciaobot-specific gws conventions live in `gws-shared`; only the short profile-wrapper routing rule belongs in the compact core (`ciao/system_prompt.md`).

The stock `visual-plan` skill produces local Markdown plans with an optional self-contained HTML companion, including diagrams drawn as inline SVG. It is the one-stop planning surface for work that needs an approval gate and a cross-session resume contract. Routine working docs (notes, analyses, drafts without an approval gate) use the core prompt's file-routing rules instead; `visual-plan` is reserved for plans that must be approved and survive a provider switch. Visual plans are local Markdown artifacts; only one file is pinned at a time, and Plan mode cannot produce one (the skill refuses and explains instead of failing on the write). Interactive HTML companions are authored by loading the stock `html-artifact` skill. A same-named workspace skill overrides the packaged copy; refresh a workspace with `ciao sync-skills --skip-upstream`, and roll back a future removal at the package level by reverting the stock skill from the next build.

### Provider context and native memory

Normal chats send one compact provider-neutral context capsule containing the
active workspace/project, canonical document, date, retrieval hint, entity
matches, and unattended-turn marker. Stable routing facts are sent once per
provider session; handovers use a separate bounded excerpt. Claude and OpenCode
receive the same compact Ciaobot core, while their native
The `AGENTS.md` guide loaders remain the only source of bounded memory.
`memory_tool.py` prunes valid expired entries before provider startup and
exposes `memory_status`/`memory_update` without creating a second memory store.

Edit canonical sources, not the generated `.claude/` or `.agents/` dirs. Do not run `npx skills update` ad-hoc (it re-expands the lockfile and repopulates bloat); regenerate the `gws-*` skills through `ciao/release.py` rather than calling `gws generate-skills` by hand.

OpenCode integration tests pin the V2 server contract: `/api/info` must report
2.0.16+ before `/openapi.json` is checked, V2 message/session pagination must
follow cursors, SSE frames are read from each event's `data` object, and child
lifecycle comes from `/api/session/active` rather than the previous execution's
`Session.Info.outcome`. When changing `ciao/providers/opencode.py`, replay the
sanitized V2 fixtures and run a tiny real turn against the installed OpenCode
2.x binary; route renames alone are insufficient because V2 also changed
permissions, forms, prompts, and projected message shapes. Keep V2 internal
credential resources covered in both absolute and workspace-relative spellings
when a custom runtime root is configured. Keep form constraints and explicit
empty-versus-cancel semantics in the provider/PWA contract. Native form and
permission cards stay mounted until a matching `(chat_id, session_id,
request_id)` response is acknowledged. The PWA keeps complete response frames
in memory only (never localStorage), queues them across closed or failed
sockets, and retries them on reconnect; a timeout makes the card retryable but
does not discard the queued frame. Backend state is cleared only after a typed
V2 success or an authoritative stale result. Resolution events are ephemeral
for attached tabs, while the persisted chat snapshot reconciles tabs that were
disconnected.

## DAG-style schedules (maintainers)

A schedule that is a multi-step workflow (load state, gate, model call, write) can use `ciao.dag` rather than a long `async def`:

- `Node(id, kind, model='', timeout_s=180.0, payload={})` — kinds: `bash`, `prompt`, `gate`, `subagent`.
- `Edge(src, dst, when='ok')` — `when` is `ok` (default), `fail`, or `always`.
- `run(dag, edges, job=..., label=..., initial_ctx={})` — records each node in `.runtime/job_runs.jsonl`.
- `subagent` nodes accept an opt-in `payload['requires']` post-condition list: each item is a file path that must exist and be non-empty after the node ran, or `{"path": ..., "contains": "<regex>"}` where at least one line must match. Paths may reference ctx like the prompt and resolve against `payload['cwd']`; `contains` regexes are used verbatim (never ctx-formatted, so quantifiers like `{2}` are safe). Exit 0 with unmet requirements fails the node (guards against an unauthenticated subagent silently doing nothing); DAGs without `requires` are unchanged.

No shipped producer runs a DAG today — the weekly skill-evolution pass was the canonical example and was retired in #697 — so `tests/test_dag.py` is what to read for a worked pipeline, one case per node kind and per edge. Use a DAG when there are 3+ sequential steps with branching and you want per-step timing on the Automation page.

`ScheduleManager.catch_up()` runs once at server startup on the host; like `tick()`, it returns an empty list without touching anything when the legacy node-state startup gate says this machine is not the host (the one boot verdict from `ciao.legacy_node_state`, which is the whole gate now that no route can rewrite the role under a running server). It dispatches only the latest missed occurrence for each enabled schedule, leaves the prompt unchanged, and records the missed occurrence's local date so a later slot on the startup day can still fire normally. Cover changes to this behavior in `tests/test_schedules.py`. Packaged system routines are excluded when the startup falls inside the post-setup grace window (`ciao/setup_marker.py`, 24h from a first-time setup): a brand-new install is greeted by its onboarding chat, and the routines fire at their next regular tick instead of all replaying missed runs in parallel. Cover that in `tests/test_setup_catch_up_grace.py`.

The Work details *Subagents running* list (rail and drawer Activity tab; the left sidebar no longer lists them) is fed by `GET /api/subagents/running` (dispatch metadata only, active chats only) and the store's poll, which replaces the whole map so a finished agent's row disappears. Only agents the parent session can name get a row — background dispatches, plus opencode children; a foreground Task is recorded in the parent JSONL by its own completion, so it is never running by the time it is nameable. Their read-only view is `SubagentChatView.vue` on `/chat/:chatId/subagent/:agentId`, fed by `GET /api/chats/{id}/subagents`. Claude agent ids arrive bare from the parent JSONL and `agent-`-prefixed from the local transcript fallback, so both surfaces normalise before comparing or routing. Cover changes in `tests/test_running_subagents.py` and the ChatPanel Work details tests.

## Agent control plane (CLI-first since S6)

`ciao/control_plane.py` is the provider-neutral application boundary;
`ciao/mcp_server.py` holds the shared operation table, the bearer-token
registry, and the envelope/plan-mode gate/telemetry that the dispatcher
(`ciao/agent_surface.py`) runs. Add business rules to managers/control-plane
methods, not shell handlers. Every operation carries a stable envelope, enforces
scoped workspace/project/chat access, and has focused domain tests.
Self-affecting operations must defer until the caller chat drains.

Since S6 the agent runs every operation as `ciao <noun> <verb>` in the managed
provider's shell. `ProjectChatManager.build_agent_request` deliberately injects
`CIAO_AGENT_TOKEN` (and `CIAO_AGENT_URL`) into that **foreground** shell — this
is the point of the CLI surface (D-01), not a leak. `ciao/background.py` strips
`CIAO_AGENT_TOKEN` (and `PWA_AUTH_TOKEN`) from every background child so a
detached command cannot call back with the caller's authority. Keep that split
when changing token delivery; do not add the token to telemetry arguments.

See `docs/AGENT_CLI.md` for the catalog and provider configuration.

### Off-loop vault reads

Vault reads are synchronous disk/SQLite work and must never run inline in an
async handler. Use `ciao/async_reads.py`:

- `await run_read(key, operation)` runs a complete read on a dedicated bounded
  executor (`MAX_VAULT_READ_WORKERS`, default 4) and coalesces identical
  in-flight reads by `key`. `operation` must open and close its own SQLite
  connection so a connection never crosses threads. Admission is capped at
  `MAX_VAULT_READ_BACKLOG` (default 12) outstanding reads; over the cap a caller
  waits for a slot without blocking the loop. Do not bypass `run_read` for heavy
  reads, or that backpressure is lost.
- Keep the operation whole: walk + parse + query for one logical read, not a
  per-file `to_thread` call. `control_plane.vault_search` /
  `vault_index_refresh` and the `vault_backlinks` / `vault_markdown_paths`
  routes are the reference shapes.
- Resolve scope (workspace, principal, paths) on the calling thread before
  submitting, so a bad principal fails fast and the worker only does I/O.
- Cancellation only detaches the awaiter; the worker cannot be stopped. Each
  caller awaits its own bridge future, so cancelling one waiter of a coalesced
  key does not cancel the shared work its other waiters depend on. The
  coalescing map and bounded width keep that from becoming unbounded background
  work, and every future's error is observed. Do not wrap mutations of
  event-loop-owned managers this way — only complete read operations.
- Serialize the write phase of any operation that shares a synchronous store
  with `keyed_lock(key)`: concurrent `index_vault` passes against one
  `vault-fts.db` race SQLite's single file-level write lock and fail with
  `database is locked`. Take the lock only around the writes; leave the
  read-only query outside it so reads stay concurrent. Every same-database
  writer must take it — the archive postprocess's `index_file` is dispatched
  through `run_read` under `keyed_lock(f"fts-index:{db_path}")` too, since its
  callers are async and the control plane's index passes now run in workers.
- Call `reset_vault_read_executor()` in test setup/teardown for isolation.

Cover changes in `tests/test_async_vault_reads.py`, which pins the heartbeat,
bounded-concurrency, no-cross-thread-SQLite, cancellation and recovery
contracts and reports p50/p95 heartbeat latency before and after.

### Update tasks: state, applicability and the freshness window

`ciao/update_task_catalog.py` holds the task *definitions*; `ciao/update_tasks.py`
(#756) holds where a task's progress is remembered and whether it applies. The
state document is `{"schema": 1, "tasks": {"<id>@<revision>": {...}}}`, in
`<vault>/Workspace/Update-Tasks.json` for a `scope: "workspace"` task and
`<runtime>/update-tasks.json` for a `scope: "install"` one. `Workspace/Update-Tasks.json`
is in `vault_index.RESERVED_UNINDEXED_FILES` — a guard rather than a fix for a
live leak, since every consumer of that set reads markdown only today, so no
index or lint consumer can treat the file as a note. Reading a record and writing
the one that replaces it is one `keyed_lock` section (a read outside it drops a
concurrent `chat_id`/`attempted_fingerprint` with no error), and a write refuses
to build on a state document it could not read.

Adding a task means adding its detector name to
`update_task_catalog.DETECTORS` **and** an implementation to
`update_tasks.DETECTOR_FUNCTIONS`, plus the same pair for its
`completion_check`. A name with no implementation resolves to `unknown` (and
loads as a `not_implemented` warning), which is why no task ships without one:
the three applicability states are `applicable` and `not_applicable` — both
positive claims, each requiring a detector that ran and returned evidence — and
`unknown` for an absent or failing detector, a detector returning something that
is not a `Detection`, and a state file that exists but cannot be read. Never make
a failure path return `applicable`, and never let a cached answer answer for a
state file the install cannot read.

`APPLICABILITY_TTL_S` (300s) is a named constant, not an env var and not a
Settings option: it is how long a detector answer may be reused, not a decision
an operator has asked to make. The window is **per task**, not per scope, and both
its token and its age are checked per answer: a task recomputed never extends
another task's window, and no answer is reused under a change token it was not
computed for. The state file is re-read on every call (so a dismissal takes effect
immediately) and an unreadable one is never served from the cache; a caller that
knows the workspace changed passes a different `change_token` to invalidate at
once. Answers whose reason is in `UNCACHEABLE_REASONS` (a detector that raised, a
detector that returned the wrong shape, an unreadable state file) are not cached
at all. Change the constant or the token contract and update the tests that pin
them, and see `tests/test_update_tasks.py`.

### Update tasks: the launch

`ciao/web/update_task_launch.py` (#761) is the only thing that turns a task into
a chat. Two rules, and both are the reason it is a server module rather than
browser code:

- **The prompt is server-owned.** It is read from the packaged
  `prompt_resource` with `read_prompt`, and no parameter accepts prompt text from
  a caller — a chat launched for "review the legacy rows" that is told to do
  something else is worse than no chat, because the record says the task ran.
  What travels instead is `prompt_digest = sha256(prompt)[:16]`, carried in the
  record and in the chat's helper, so a reader can tell whether the instructions
  changed without shipping them twice.
- **Start is idempotent per `(task id, revision)`.** The whole launch runs inside
  `update_tasks._record_lock` — the same read-and-replace section the recorders
  use, and deliberately the same one, because a chat id written outside it is
  lost with no error. Inside it: read the record, and if it already names a chat
  that still exists, return it with `resumed: true` and create nothing. A record
  naming a chat that is gone is the recoverable case, not a failure: a fresh chat
  is created and the record re-stamped, so a deleted chat never leaves a task
  permanently pointing at an id nobody can open. Records are keyed
  `"<id>@<revision>"`, which is why a revised task cannot resume, or even see,
  the previous revision's chat — an engine update never substitutes new
  instructions into a running chat.

A live chat is not the same as a dispatched prompt, so the record's lifecycle
decides what a resume does with one (`_resumed`):

- `failed` — the turn never reached the chat, so the prompt is sent into **that
  same chat** under the lock and the record goes back to `in_progress`. Still one
  chat, still `resumed: true`, and now a prompt that actually left. A retry that
  fails again leaves the record `failed`, so the next start tries again.
- `dismissed` — an operator pressing Start on a task they declined is a reopen,
  so the chat is kept and the record is written `in_progress`. Nothing is
  dispatched: a dismissal carries the chat forward but not whether the turn is
  still in there. (The 409 alternative — "reopen it first" — was available and
  not taken; Reopen already exists and does strictly less.)
- anything else — nothing created, nothing sent, and the digest reported is the
  record's own rather than the one this call computed.

Two ordering rules fall out of that, and both are load-bearing. The record is
written **before** the turn is dispatched, and it is written `failed` — the
honest pre-dispatch value — with `in_progress` written only once `start_stream`
has returned. So a process that dies between minting the chat and stamping the
record cannot orphan it, and one that dies after the stamp leaves a record the
next start can act on: a dispatch that raises, or a crash, raises
`UpdateTaskLaunchError` (which carries the `chat_id`) and the next start sends
the prompt into that same chat instead of reporting a resume that never ran. The
window this cannot close is the one between `start_stream` returning and the
`in_progress` write, where a dead process leaves `failed` for a turn that may
already be running; an orphan chat is worse than a visible duplicate turn, and
`proposal_service.accept_skill_proposal` makes the same trade. And `start_stream`
creates an asyncio task, so `launch_task` is called **on the event loop**, not
through `asyncio.to_thread` like the vault-scanning routes — the same split
`proposal_implement` documents for `accept_skill_proposal`.

A launched chat carries `helper = {"kind": "update_task", "task_id", "revision",
"scope", "prompt_digest"}`, validated fail-closed by
`chat_service._normalize_chat_helper` next to the `memory_pass` and `proposal`
kinds. That helper is the only record of which task a chat is for, so the
launcher builds it and the store re-checks it; if you change one, change the
other in the same commit — `tests/test_update_task_launch.py` pins them against
each other, and `tests/test_memory_pass.py` pins the fail-closed cases.

The launch path runs no detector, no completion check, no model, no `eval`, no
shell and no remote fetch. Applicability is a separate, TTL-cached answer
(`update_tasks.evaluate`) that `GET /api/update-tasks` reports and a start does
not re-ask for: a start is a decision the operator already made.
`dismiss_task`/`reopen_task`/`record_check` are thin wrappers over the
`update_tasks` recorders, so the card and a direct API call cannot produce two
records for one decision. See `tests/test_update_task_launch.py` for the
idempotency cases, each driven against a fake manager over a temp packaged root.

## Change guidelines

- **Doc the change.** After any change to `ciao/`, `web/`, `scripts/`, `deploy/`, or `pyproject.toml`, refresh `docs/ARCHITECTURE.md`, this file, `AGENTS.md`, and `INTEGRATIONS.md` against actual repo state before declaring the task complete. Skip only for pure bugfixes that touch nothing in layout, capabilities, install steps, env vars, endpoints, or commands.
- **New API routes must be documented.** Add the route to `PWA_API.md`; state-changing routes also need an Agent recipe or an allowlist entry in `tests/test_pwa_api_docs.py`. New `CIAO_*` env vars must land in `INTEGRATIONS.md` or the allowlist in `tests/test_env_vars_documented.py`. Both are test-enforced.
- **Never restart the ciao service yourself** from inside the PWA. Apply code changes and ask the operator to hit Deploy.
- **Never commit `.env` or API keys.** `.env` minimum: `PWA_AUTH_TOKEN` (the dashboard password; protection is on unless `PWA_AUTH_REQUIRED=false`).
- **Keep edits minimal and consistent with existing patterns.** Don't refactor unrelated code; if unrelated changes appear, pause and ask.
- **Avoid destructive git** (force push, hard reset on shared branches) unless explicitly asked.
- **Use the branch model in `CONTRIBUTING.md`.** Day-to-day PRs target `develop`; release PRs target `main`.
- **Write tests** for new Python behavior; add to `tests/`. PWA changes verify via `npm run build` typecheck at minimum.
- **Verify UI accessibility.** For PWA layout changes, check keyboard operation, visible focus, browser zoom, and 44px mobile targets at a narrow-phone viewport in addition to the build.
