# Ciaobot Development Guide

Setup, dev workflow, testing, and change guidelines. For the system design, read `docs/ARCHITECTURE.md` first.

Provider-settings regressions cover saved defaults and effort options even when
catalog refresh fails. Automatic Session insights inherits the source chat model
for archive memory, titles and schedule attention; an explicit per-provider
insights model wins. History imports without a recorded model use the
workspace/provider default.

Chat file-link regressions cover separate agent roots, exact identities across
text/image/binary/HTML previews, failed pins preserving the modal, and stale pin
completions not dismissing a newer file (#1167).

`ChatPanelUserMessage.test.ts` covers chat-reference link eligibility against
the same openable-chat list used by the mention picker, and preserves the
link-map identity when a chat-list poll changes only activity time (#1807).

Page-load behavior is covered by `tests/test_static_asset_compression.py` and
`web/src/lib/chatComponents.test.ts`, plus `AppEngineOffline.test.ts` for the
bounded startup request and display delay. Static gzip is limited to hashed
JavaScript/CSS, while chat code is prefetched only for chat deep links.
The Home-to-chat rise is covered by `ChatLayoutAsyncChunk.test.ts` (held chunk),
`ChatLayoutRiseLifecycle.test.ts` (ordering, cancellation, same-id replacement,
refused navigation), `ChatPanelRiseContract.test.ts` (the production panel's
instance-owned class), and the held-chunk cases in `web/e2e/specs/chat-motion.spec.ts`
(page clock, reduced motion) (#3086).

`tests/test_is_session_local.py` covers batched chat-list availability, including
session creation/deletion and newly used working directories between requests.
`tests/test_claude_session_lookup.py` covers the independent single-session
lookup freshness contract. Large real catalogs need measurement alongside the
small browser fixture; asset readiness alone does not measure Home's data wait.

## Server install

Linux production and every installer-managed macOS install restart the engine
from Settings through `POST /api/admin/restart`; developer mode (Settings →
Advanced → Developer, with the source checkout set there) on a source checkout
retains the source deploy workflow.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
ciao setup --workspace /tmp/ciao-workspace
ciao run
```

`python -m ciao <command>` runs the same CLI as the `ciao` console script, from any interpreter that has the package installed.

`ciao setup` is idempotent and defers Git history unless explicitly requested with `--initialize-git`. That option creates local history and an initial commit of nonignored workspace and external-vault contents; it configures no remote and uploads nothing. `--yes` is not Git consent. Existing repositories still receive missing ignore guards. It writes the initial `.env` (including the selected port), seeds stock workspace files, copies the editable `AGENTS.md` workspace guide (both providers discover it natively; nothing writes a `CLAUDE.md` any more) and copies `CIAO_CUSTOMIZATION.md`. On macOS it also renders the server plist under `~/Library/LaunchAgents/` and removes retired launcher bundles. Existing custom `AGENTS.md` files and configuration values are preserved. By default setup does not load launchd; add `--load-launchd` on macOS to run `launchctl`. Linux setup creates no desktop/service files unless an explicit `--launch-agents-dir` requests an offline plist export. See [Linux hosting](LINUX.md) for the Linux install, the systemd unit and HTTPS deployment.

For a new installation, `ciao setup --workspace ~/Ciaobot --notes-folder ~/Notes` selects an existing, accessible notes directory separate from the operational folder. The first-run browser setup offers the same choice. Ciaobot stores credentials, runtime data and its database in the operational folder; the notes remain in the selected directory. Setup does not create files in the separate notes folder or initialize its Git history, and refuses `--initialize-git` with this option. A cloud client manages synchronization, and Ciaobot does not report whether cloud synchronization is complete. This option does not rebind an existing installation.

Onboarding lifecycle is recorded separately in the owner-private `.runtime/onboarding.json` receipt; it is not the provisioning timestamp or welcome-password flag. A fresh supported setup starts `in_progress`; an existing receipt is always preserved. Receipt-less established installs are marked `legacy_exempt`, based on setup registry/legacy workspace evidence or durable runtime state—not notes-folder contents, unrelated `.env` files, or bootstrap auth/lock files. Startup creates a pending receipt for a fresh configured install before importing legacy workspace configuration. Invalid existing receipts fail closed and are never overwritten.
A genuinely new install also writes `backup_enabled: false` into its app settings before the engine starts: adopting a folder that already sits inside a repository with an `origin` must not begin unattended commits or uploads. Existing installs, explicit choices and unreadable settings files are left untouched, and local history (`--initialize-git`) is not consent to backup. An operator turns backup on later from Settings → Memory (the backup settings route) or by running the guided backup setup, both of which are explicit actions.

`ciao service status|start|stop|restart` drives that background service on
macOS (the LaunchAgent). `start --workspace <dir>` registers a service that is not installed yet, and refuses to
repoint one that already serves a different workspace; use `ciao setup --workspace
<dir> --load-launchd --yes` to repoint it. `stop` and `restart`
refuse while a chat would be interrupted (working, or holding an open question/approval card) unless `--force` is given.

`ciao service start` on macOS reports two different things, and its exit code
tells them apart. The launchd submission (`enable`, `bootstrap`, `kickstart`)
is what `start_service()` does and what the move and update workers consume:
it returns as soon as launchd accepted the job. The CLI then waits for the
engine it started to answer *as itself*: the runtime's `server.lock` must name
a new run (a fresh `startup_id`, this install's port and runtime directory,
status `running`) and `/api/startup-status` on the port must report that same
`startup_id`. A bare 2xx from whatever listens on the port, the run that was
there before the kickstart, or another Ciaobot runtime do not count. The wait
ends early on a recorded bind failure or a workspace-access denial written
since the kickstart, and reports those. Reaching the deadline
(`START_SETTLE_TIMEOUT_S`, 30 s) is `"start_outcome": "timeout"` with exit
code 1 — an unconfirmed start, never "started". The deadline rests on a
measurement: a cold `python -m ciao run` from a checkout served its port after
4.1 s and was ready at 4.7 s (2026-10-23, Apple Silicon), so the previous 2 s
wait expired before some engines had bound at all; a launchd-spawned install
adds interpreter start-up and first import on top. A verified start carries
`"verified": true` with the `startup_id` and `pid` it confirmed.

The weekly dependency-changelog review is an operator-owned routine, not part of the public app install. In a maintainer workspace it lives at `scripts/dependency_review.py` and invokes this checkout for the DAG/runtime; public release preparation uses only the generic helpers in `scripts/dependency_updates.py`.

A fresh first logical workspace and workspaces added later in Settings live at
`<CIAO_VAULT_ROOT>/<workspace-name>/`. Their registry path is read-only in the
PWA. Existing-folder setup preserves the selected notes in place so the
onboarding agent can inspect them; `ciao os-audit` then offers a model-guided,
backed-up migration into the standard named folder. Settings → Overview → All workspaces →
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
ciao os-audit --json
ciao create-chat --prompt "Start here" --workspace default
ciao cleanup-sdk-blobs --workspace .       # dry-run by default
python scripts/label_hygiene.py --json      # audit issue labels, dry-run by default
ciao dev                                   # backend :8543 + Vite :5173
python scripts/public_release.py export . /tmp/ciao-public-export
python scripts/public_release.py scan /tmp/ciao-public-export --private-patterns /tmp/private-patterns.txt
python scripts/package_smoke.py --skip-frontend
ciao auth claude --print-only              # show terminal OAuth command
ciao auth opencode --print-only            # show opencode login command
ciao auth opencode                         # run provider login helper
```

### Existing-folder vault selection

Setup decides where the vault lives by reading, and writing nothing, in this order:

1. a nonblank `.env` `CIAO_VAULT_ROOT`;
2. `.runtime/workspaces.json`, when it exists. An unreadable file, invalid JSON or no valid entry refuses the setup;
3. a pending legacy `CIAO_WORKSPACES` (previewed here, and imported once by the existing importer, which then writes its marker);
4. an explicit `vault_root` argument;
5. for an existing folder only, bounded discovery.

Established configuration (1 to 3) is authoritative and is never reinterpreted: a registered `"."` stays `"."`, and an explicit argument never replaces registered targets. The inspection endpoint and the finish endpoint call the same selection, so the wizard reports the location the finish will write.

Discovery looks at the folder and its visible immediate children, never deeper. A real `memory-vault/` directory directly in the folder wins. Otherwise the candidates are the folder itself (when it holds `entity-types.yaml`), each child that holds its own `entity-types.yaml`, and each child's real `memory-vault/`. One candidate is adopted. No candidate adopts the folder as its own vault, as before. More than one is refused with HTTP 400, listing every candidate sorted by path, before any receipt, `.env`, registry, import, scaffold, Git or service write. Symlinks never count as evidence and a symlinked child is skipped entirely. An explicitly chosen vault, including a symlink target, is used as given.

To use one candidate, set the main Ciaobot folder to that vault folder itself (for example `<clone>/work/memory-vault`), inspect it and finish there. The application data (`.env`, `.runtime/`, the agent assets) then lives inside that folder. To keep the parent as the main folder, use the recovery procedure below instead of a fresh setup. The setup wizard's location stage shows a generic inspection message on a refusal; the server's full message, with the candidates, is in the API response.

### Recovering an incorrectly adopted vault

Setup recorded the vault at the wrong place: for example `.env` has `CIAO_VAULT_ROOT=.` and the notes live at `work/memory-vault`, with generated defaults at the install root. Discovery does not repair an established `"."`. Repair by hand, with the install stopped, as follows. The paths below are relative to the installed operational root `<install>`, not to a source checkout.

1. Finish any active work. Stop the installed engine with its normal controls: `ciao service stop` on a supported desktop service, the configured systemd unit on Linux, or by stopping the foreground process. Do not stop or restart a live engine from an agent session.
2. Make private backups outside any indexed vault folder of `<install>/.env` and `<install>/.runtime/workspaces.json`. Keep file permissions and ownership. Do not paste secrets into an issue.
3. In `<install>/.env`, change the one existing `CIAO_VAULT_ROOT` assignment to `CIAO_VAULT_ROOT=work/memory-vault`. Keep every unrelated line and do not add a second assignment.
4. In `<install>/.runtime/workspaces.json`, find the affected logical workspace by its `name`, not by its position or folder basename. Change only that entry's `vault_root` to `"work/memory-vault"`. In the list shape, that is the object whose `"name"` matches. In the supported legacy mapping shape, it is that workspace's mapping value. Keep every other entry and field (provider, tools, filesystem scope, account settings). Do not replace the whole registry with a one-entry example.
5. Only if the registry is absent and a genuine legacy `CIAO_WORKSPACES` is still pending import, correct the affected entry's `vault_root` inside that `.env` value and leave the other entries alone. Do not delete or fabricate the import marker (`.runtime/workspaces-env-imported.json`). Startup performs the existing import.
6. Validate the edited JSON. Confirm that `work/memory-vault` contains the original notes and its custom `entity-types.yaml`. Do not run `ciao setup` over the affected install as a repair step.
7. Start the stopped engine through its normal controls. Configuration is read at process startup, so editing the files does not hot-reload them. If the fix needs a newer engine release, deploy that release from Settings before the stop, edit and start sequence.
8. Verify workspace health, the expected notes, the indexing target and the custom categories. Leave every previously generated root default in place: it may hold user edits.

For an absolute external location, put the same absolute path in both the global assignment and the affected registry entry. Do not move, merge or delete data.

### macOS venv workarounds

On recent macOS, `scripts/run-ciao.sh` and the `scripts/dev.sh` wrapper source `scripts/ensure-deps.sh`, which injects two self-healing workarounds into `.venv/bin/activate`:

- A `DYLD_LIBRARY_PATH` pointing at Homebrew's `libexpat`. macOS 26+ ships a system `libexpat` missing a symbol Homebrew Python's `pyexpat` needs, which otherwise crashes pip and venv creation with `ImportError: ... Symbol not found`.
- An `SSL_CERT_FILE` pointing at the venv's certifi CA bundle. The python.org Python build ships no CA bundle, so the bare `urllib` calls in the server (e.g. OAuth token refresh in `ciao/web/auth.py`) fail with `SSLCertVerificationError`.

No manual step is needed; `ensure-deps.sh` handles both. If you set up the venv by hand and hit either error, run `scripts/ensure-deps.sh` once to repair `activate`.

### End-user distribution

Two install paths ship, and both install the same engine wheel. The documented one-liner is

```bash
curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh
```

It is the Linux installer; on macOS it refuses and names the app. Install paths:

- **macOS:** the Ciaobot app (DMG from the latest GitHub release). The app carries the engine. `scripts/install.sh` refuses macOS and names the app.
- **Linux:** `scripts/install.sh`, run as the account that will own Ciaobot (`sh install.sh`). It installs the release wheel with uv for the invoking user, runs `ciao setup`, and installs the systemd unit through `ciao service install` (asks for sudo). Flags: `--workspace DIR`, `--no-service`, `--replace-unit`, `--wheel PATH`. It refuses to run as root and refuses to run from a source checkout, naming the release asset instead. See [Linux hosting](LINUX.md).

**Engine updates** no longer run through the engine. On macOS, Ciaobot for Mac updates the bundled engine with the app (Sparkle). On Linux, `ciao update` re-runs the latest stamped installer (`linux_service.run_update`; see [Linux hosting](LINUX.md)). `update_package()` (`ciao/package_version.py`) answers the Settings hint for an installer-managed engine that it no longer updates itself, and for a `uv_tool` engine (a Linux one-line install) with `ciao update`. What is left of the transaction is the receiving half that a 1.x install hops through: `ciao update status`, and the hidden `run-apply` and `run-recover` verbs its detached updater job runs (`ciao/engine_update.py`, `update_host.MacUpdateHost` on launchd). `ciao update` on Linux sends everything except those three verbs to the installer. `current_update_host()` raises `UnsupportedPlatformError` on other platforms. `ciao update stage` and `ciao update apply` are gone and exit 2.

Machines still running the retired macOS app are cleaned up by hand (see INTEGRATIONS.md, Upgrading from the macOS app): the `Ciaobot.app` bundle and its LaunchAgents are removed manually. The app's own updater, the native verifier and the signed `.app` feed are not published.

The engine release carries the wheel and the stamped `install.sh` that pins it. The signed manifest (`ciaobot-engine-manifest.json` and `.sig`) is attached to 2.0.0 only, for 1.x engines. `python -m ciao.release_manifest verify` checks the manifest against the embedded minisign key; see Publish under Branching and releases. The server host archive is not built or attached.

## Branching and releases

- **`develop`** is the integration branch. Feature and fix PRs target `develop`.
- **`main`** is release-only. Direct pushes and merges to `main` are blocked; only release PRs land there.
- **CI** (`.github/workflows/ci.yml`) runs on pull requests into `develop` or `main` and manual dispatch.
  Every PR runs three Linux jobs in parallel: `linux-server` (`mypy ciao`,
  `pytest -n auto tests/`, about 4 minutes), `linux-web` (`npm test`,
  `npm run build` and a package smoke test of the built wheel, about 3
  minutes) and `linux-install-smoke`, which builds the PWA and the engine
  wheel this checkout would publish and proves the Linux one-line installer
  by installing that wheel with `scripts/install.sh` inside a clean
  `ubuntu:24.04` container as an unprivileged account (`--no-service`; the
  planned service unit is only dry-run in the container and
  `systemd-analyze verify` re-checks it on the host, #3131). For a PR into
  `develop` whose three-dot diff from its base changes
  `web/`, `linux-e2e` runs alongside `linux-web`, after the fast `web-scope`
  detector: it installs Chromium on Ubuntu
  and runs `npm run test:e2e` from the PR merge checkout. That command's npm
  prehook builds both the PWA and terminal harness from the tested checkout.
  PRs without a `web/` change run only the scope-validation step. A detector
  failure fails the stable `linux-e2e` check rather than silently skipping it.
  Failed Linux browser runs upload
  `linux-playwright-traces` when Playwright produced traces.
  `linux-server-latest-python` repeats the `linux-server` gates on
  Python 3.14, the newest interpreter `requires-python` admits, on release PRs
  and manual full runs. The checked-in `scripts/configure-github-branches.sh`
  declares it a required check on `main`. The
  full macOS `test` job (coverage, browser tests, and the engine wheel this
  branch would publish, installed into a throwaway venv and cold-started — the
  launchd, installer, engine-update and migration coverage — about 23-32
  minutes) runs on release PRs and manual full runs, not ordinary develop PRs.
  Its browser steps reuse the PWA build the job already made
  (`npm run build:harness` then `npx playwright test`) instead of running the
  `test:e2e` prehook a second time. Every CI job and every pytest step has a
  timeout, listed under "CI time bounds" below. The Swift
  Mac app in `mac/` is built by its own `mac-app` job (`swift test`,
  `swift build`, `scripts/build-app.sh`, which builds the bundled engine, the
  asset test and a plist lint on `macos-15`), which runs on develop PRs whose
  three-dot diff changes `mac/`, `pyproject.toml`, `uv.lock` or
  `web/src/lib/workspaceColors.ts`, the engine colour table it pins (decided by
  the fast `mac-scope` detector), and on every release PR and manual
  run; other develop PRs run only its scope check, on Linux. Changes under `ciao/` or
  `web/` alone therefore do not trigger `mac-app` on a develop PR (#3128 M4); the
  engine build of such changes runs on release PRs and manual dispatch. The engine
  build needs Python 3.12 or 3.13 (the job installs 3.12; a developer machine whose
  `python3` is newer needs `python3.12` or `python3.13` on `PATH`) and Node. The engine's wheelhouse
  is cached (`actions/cache`, keyed by the four engine input hashes); the nightly
  dispatch on `develop` seeds a cache every develop PR can read, while release PRs
  into `main` cannot read it and build cold. Web changes in develop PRs get the
  Linux browser check before merge; the macOS browser run is the release PR.
  A full macOS or Python 3.14 failure therefore surfaces on the release PR, and `/ciao-release` merges into
  `main` only when that PR's `linux-server-latest-python`, `test` and `mac-app` checks are green.
  Superseded PR runs are cancelled by PR number; manual full runs are independent.
  A nightly dispatcher starts full CI on the current `develop` at 02:23 UTC.
  GitHub requires both workflow files on its default branch (`main`) before
  nightly or manual dispatch becomes available, so this starts after release.
  Until then, use a release PR for full platform checks.
  The `discover-agents` workflow (`.github/workflows/discover-agents.yml`, manual
  plus weekly, never on a PR) records where Claude Code and OpenCode keep their
  files (#696, D-04). On `macos-latest` it installs both
  tools, runs one tiny real turn each in three workspaces whose paths contain a
  space, a dot and an underscore, and uploads `agent-discovery-macos`
  (new-file list, Claude project folder to workspace
  map, tool locations, turn logs). The Claude turns need the `ANTHROPIC_API_KEY`
  repository secret and are skipped without it; OpenCode uses the free
  `opencode/big-pickle` model. Run it with `gh workflow run discover-agents.yml`
  and fetch the result with `gh run download <run-id>`. Dispatch and the weekly
  schedule only work once a release has put the file on `main`; before that, run
  it from a throwaway branch with a temporary `push` trigger.
- **Release prep:** from a clean checkout, run:

```bash
scripts/prepare-release --apply --create-pr --ready
```

  That cuts `release/vX.Y.Z` from `develop`, aligns the Python and PWA versions
  and lockfiles, refreshes `CHANGELOG.md`, runs release
  checks, and opens a PR into `main`. Use
  `--bump minor` or `--version X.Y.Z` when needed.

- **Publish:** merging the release PR into `main` triggers `.github/workflows/release-on-main.yml`, which creates the `vX.Y.Z` tag and a GitHub release that is a pre-release and not latest (`--prerelease --latest=false`; a pre-release still fires `release: published`, a draft would not). `releases/latest` feeds the download page, the `install.sh` one-liner and the 1.x updater, so the release stays off it until the operator has attached the signed DMG by hand and run `mac/scripts/promote-release.sh --tag vX.Y.Z` (the runbook, appcast order and rollback are in `skills/release-mac/SKILL.md` sections 6 and Rollback). `publish.yml` then builds the PWA and the engine wheel, verifies the wheel in a clean environment, and signs the engine manifest with the release minisign key (`ciaobot-engine-manifest.json` + `.sig`, gated by `ciao.release_manifest verify`), which lists the wheel alone. It attaches the stamped `install.sh` and the wheel it pins on every release. The signed manifest and its signature are attached to 2.0.0 only, the one release 1.x engines update to (#3131 Q3); before upload a 1.x engine must stage that release through `scripts/legacy-update-gate.py`. The server host archive and the installer scripts are no longer built, listed or attached. Cutting a patch release before 2.0.0 strands 1.x engines, because the manifest is 2.0.0-only. `release-smoke.yml` installs the published Linux `install.sh`, re-runs it as a same-version reinstall (the service must restart under a new main PID), and (for the 2.0.0 release only) runs the 1.x update gate against the published assets. After the upload it re-downloads the attached files and fails unless the installer pins this version and the attached wheel's sha256, and, for 2.0.0, `ciao.release_manifest verify --asset` matches the wheel to its signed entry. Since #653 it publishes no app, no `latest.json` feed, no native verifier and no bundled runtime, and its signer is `@tauri-apps/cli` run standalone, so it never depended on the now-deleted `desktop/` tree. It does not publish PyPI, Homebrew, or DMG artifacts. A follow-up job merges `main` back into `develop`.

- **Release rehearsal:** `.github/workflows/release-rehearsal.yml` builds the same artifact set a release would publish (the engine wheel, the stamped `install.sh`, the signed engine manifest and its `.sig`) without creating a release. Dispatch it from `develop` or `main` only; other refs are refused:

```bash
gh workflow run release-rehearsal.yml --ref develop
```

  GitHub offers `workflow_dispatch` only for workflow files on the default branch, which is `main` (`git symbolic-ref refs/remotes/origin/HEAD`). Until `release-rehearsal.yml` is merged to `main`, the command above cannot start it. The acceptance proof is one green run dispatched from `develop` after that merge, with its artifact list recorded in the issue or PR.

  The `engine` job uploads `rehearsal-<sha>-<run_attempt>` (the wheel, `install.sh`, the manifest and its signature) and runs the 1.x update gate against those files through `file://`. The gate runs only for 2.0.0, the one release that ships the 1.x manifest; other versions skip it with a notice. The `linux` job installs the rehearsal wheel with the service and runs the same service asserts as `release-smoke.yml`. The `mac-adhoc` job packages the ad-hoc Mac app with `mac/scripts/package-app.sh` and uploads the archive, its `MANIFEST.txt` and `SHA256SUMS`. The signed and notarized DMG is not built here; it comes from `mac-app-signing-rehearsal.yml` (#3128 G) once the credentials exist. The `engine` job runs in the `release-signing` GitHub Environment, and the manifest signing secret is read by one step only. The maintainer must move `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` into that Environment and restrict its deployment branches to `develop` and `main`; until then the in-file ref guard is only a convenience. Nothing in the run has write access to the repository, and nothing is uploaded to a release.

One-time GitHub setup for a fresh clone or repo admin:

```bash
./scripts/configure-github-branches.sh
```

When run by an admin, the script sets `develop` as the default branch and
applies protection to both branches. The checked-in declaration adds
`linux-e2e` to develop's required contexts; GitHub skips that job on develop
PRs without a `web/` change. `mac-app` is required on both branches the same
way: it passes after the scope check on PRs that do not change `mac/` or
`web/src/lib/workspaceColors.ts`, and
activating it live is the same separate admin action, after it has reported on
a PR. `linux-install-smoke` is likewise required on both branches, with no
scope condition: GitHub requires it once the workflow has reported on a PR,
and activating its context live is the same admin action. Admins remain
exempt on `develop` so the
post-release sync PR can merge. The checked-in script continues to declare
`linux-server`, `linux-web`, `linux-install-smoke`, `linux-server-latest-python`, macOS `test` and
`mac-app` required on `main`, including for admins.
This source change does not update live branch protection: activating the new
required context needs separate maintainer approval and an admin action after
the workflow is present and `linux-e2e` has reported on a PR.

### CI time bounds

Run 45669361072 (PR #3635) hung at 99% in `linux-server` after every test had
passed, leaving orphan pytest processes. `faulthandler_timeout` is per test
and cannot fire once the suite has finished, so each job and each pytest step
now has a `timeout-minutes` bound, sized at roughly 2-3x the slowest
successful run measured on 2026-11-26 (14 recent PR runs and 10 manual full
runs of `ci.yml`; wall-clock minutes of the slowest success):

| Job | Slowest success | Job bound | Step bound |
| --- | --- | --- | --- |
| `linux-server` | 6.9 (tests 6.1) | 20 | 15 |
| `linux-server-latest-python` | 5.5 (tests 4.8) | 15 | 12 |
| `linux-web` | 5.8 | 15 | |
| `linux-install-smoke` | 2.1 | 10 | |
| `linux-e2e` | 9.9 | 30 | |
| `mac-app` | 8.5 | 25 | |
| `test` (macOS) | 32.5 (pytest 13.3, browser 9.6) | 75 | 30 (pytest) |
| `web-scope`, `mac-scope` | 0.3 | 5 | |

After each pytest step an `if: always()` step lists leftover pytest processes,
sends them SIGABRT (pytest enables `faulthandler`, so every thread's stack is
printed) and then SIGKILL. The Linux pytest steps pass `--durations=15` so the
log records the real slowest tests. `tests/test_ci_parallelism.py` asserts the
bounds are present, at least twice the measured maximum, and below 90 minutes.

`faulthandler_timeout = 600` in `pyproject.toml` is deliberately far above any
real test. A local full run on macOS (17,937 tests, 243 s with `-n auto`) had
the slowest test at 35 s (`test_host_stop_escalates_stalled_child`), then 25 s
and 15 s; earlier CI logs print no durations, so the Linux per-test figures
arrive with the first run of this change.

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

### The pinned uv

One uv version writes `uv.lock`: `[tool.uv] required-version = "==X.Y.Z"` in
`pyproject.toml`. Every `astral-sh/setup-uv` step in `.github/workflows/` sets
`version:` to the same number, and `tests/test_release.py` fails when they
differ. A different uv can rewrite the lock header (0.12.17 turned revision 5
into 3, #3188), and uv itself refuses to run against the pin. `prepare-release
--apply` checks `uv --version` against the pin first and stops with the
version to install. To bump uv, change the pin and every workflow `version:`
together, run `uv lock` once and commit any lock change in the same PR. This is
separate from the uv that `scripts/install.sh` installs for users.

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
npm test             # full frontend suite under web/src
```

The build's TypeScript check rejects unused locals and parameters. Remove dead
code; prefix an intentionally unused callback parameter with `_` when its
position is required by the callback contract.

## macOS engine development

The macOS app is retired and its source tree is gone (`#656`): the release is the
engine, and the one-line installer installs that. Nothing under `desktop/` is
built, so there is no Rust or native-shell step to run here.

A `Ciaobot.app` an older install left behind is removed by hand, as INTEGRATIONS.md
describes; no command does it.

After PWA changes, rebuild and either restart the service or use the **Deploy** button in PWA Settings. **Never restart the ciao service from inside a PWA chat** (you'd sever your own session); ask the operator to deploy.

Restart requests made through the running server enter a drain phase: existing chats finish before shutdown, and new turns are not admitted during that window. Directly killing the process bypasses this protection. A drain that is cancelled (an update whose drain timed out) replays the background-run and CLI-task wakes it deferred instead of losing them, since no restart follows to deliver them.

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
- The login page's *Sign in on this computer* (`POST /api/auth/host-sign-in`) cannot be exercised through `:5173`: the Vite proxy forwards `Host: localhost:5173` to `:8543`, and the route refuses a Host port that differs from its listening socket (the guard against redirecting the one-time token to another local port), so it answers 403 `not_on_host`. Check it against a built engine (`npm run build`, then `http://localhost:8543`). Do not relax that check for dev.
- A development backend refuses to start when another Ciaobot backend already owns the same runtime directory. Stop the launchd service first, or use a separate `CIAO_RUNTIME_ROOT`; changing only the port is not sufficient because both servers would otherwise mutate the same project/chat registry.

## Testing


Claude slash-invocation regression coverage lives in `tests/test_providers.py`
(`-k 'slash or non_commands_keep or exact_trusted or forged'`). The SDK wire
message must start with the native command, while the shared context capsule
arrives exactly once through `UserPromptSubmit.additionalContext`; it must never
become command arguments. Capsules travel with images and arguments, and the
image manifest is part of the prompt the submit hook matches. Trust is
`AgentRequest.injected_context_prefix`, set by `build_agent_request` and extended
by `prepend_stable_context`: a forged second capsule block, a capsule-shaped
prompt with no recorded prefix, or mismatched metadata stays inline as ordinary
user text. Request-construction provenance is covered in
`tests/test_project_chat_persistence.py`.

```bash
source .venv/bin/activate
pytest tests/                  # Python backend tests
pytest tests/test_schedule_workspace_routing.py  # Workspace/provider/model inheritance for schedules
pytest tests/test_dag.py       # DAG runner only (Node/Edge/run, per-node timing)
python scripts/public_release.py scan <export-root> --private-patterns <file> # Public export private-data preflight
python scripts/public_release.py export . /tmp/ciao-public-export # Copy allowlisted public tree
python scripts/package_smoke.py --skip-frontend # Wheel install smoke test
ciao vault-index --workspace default --format json  # Query the vault index
ciao vault-search "keyword" --limit 5 # FTS search over the configured vault
ciao vault-lint --vault-root memory-vault # Vault hygiene lint
ciao os-audit --json # Strict AI OS setup and context-hygiene audit
ciao memory-audit --json # Bounded-memory rot only (regions; add --with-vault for note aging)
cd web && npm test             # Frontend unit tests
cd web && npm run build        # Typecheck + Vite build (frontend smoke test)
```

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
Settings → Advanced shows a compact online backup state and last successful upload;
the configured scope, repository, and raw diagnostic are in a native "Backup details"
disclosure ("Review details" when attention is needed). A coverage gap — paths git
already tracks that the scope refuses to commit, so a repository that is also a
checkout is only partly covered — is a neutral note under whatever the state says,
driven by the status's `coverage_gap` count rather than by parsing the `reason`
sentence, and it never reddens a backed-up install (#733). The five-minute cadence is
in the section description. There is no separate backup guide.

For chat rendering changes, verify the compact `Activity` disclosure, `Outputs` placement, readable token labels, keyboard operation, and 44px touch targets at both desktop and narrow-phone widths. Markdown tables should shrink-wrap on desktop and keep readable first-column labels inside a horizontally scrollable table viewport on narrow screens.

For whole-install workspace moves, `tests/test_workspace_move.py` uses a temporary source, target, service definition and operation state; never test by moving the operator's live workspace. The detached worker probes destination write/delete access before draining, and macOS may deny that access without showing a prompt. When destination startup exits with 78 or records a fresh access denial, the operation saves its diagnosis before rolling back; a successful source restart may clear the shared denial notice without erasing that operation evidence. In app mode (Ciaobot for Mac) the engine performs the move itself in the gap of its own exec restart: `ciao.workspace_move_app.before_boot` moves the folder and repoints the pointer before the engine boots, and any boot finding an interrupted move rolls it back before booting the old folder; `tests/test_workspace_move_app.py` covers that side, with real folders and records under a temporary home. It injects record-write failures after the rename (the data must stay under a folder the pointer names), and checks every refusal of a malformed or hostile record with real temporary directories; mutate a guard in `workspace_move_app.py` and one of them must fail. The app-mode request side is `workspace_move_app.record` — the only module that writes an `app=true` record (a source-scan test's `_APP_RECORD_WRITERS` allows exactly that one) — and `drain_then_restart`, which drains chats and scheduled work, writes `stopping` and restarts the engine in place. `verify_after_boot`, started by the booted engine, writes `done` only after `overall_ready` — never before; on a timeout it records `rolling_back` and requests the restart whose pre-boot pass rolls the move back, so an ordinary restart can never read a finished move as unfinished. Run `tests/test_workspace_move.py`, `tests/test_workspace_move_app.py` and `tests/test_macos_service.py` for changes to this flow.

For the receiving half of an engine update, `tests/test_engine_update.py` and `tests/test_engine_update_hop_contract.py` are the contract: launchd, uv, the service starter, the engine's HTTP surface, the clock and sleep are all doubles, so nothing in it may start a real service, move a real environment or open a socket. The durable `com.ciao.recover` agent that the 1.x updater installs before it stops the engine is answered by `recover_apply` (`run-recover`): it takes the lock without waiting, stands down while a swap holds it, never posts, and retires its own job once there is nothing to recover or the rollback has settled. Do not run `run-apply`, `run-recover` or real `launchctl` against your own install to check any of it.

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
and the lane status sentence both ask, and it is false for every pass, blocked
or not; the row's own blocking phase carries that signal), and that a declared
pass never reappears in `activeChatsAll`, `projectChats`, `totalUnread`, the
sidebar or a schedule target. A pass is silent on every notification surface
too — toasts, the notification inbox, the bell and OS badge, and the menu bar —
even when blocked; `isMemoryPassChat` takes the project as its fallback so a
malformed helper in a Memory project cannot start notifying again. That
malformed-helper pass is the exception in the display filters: because it has
no memory-insight row, it must stay in `activeChatsAll` and `projectChats` so its
pending question remains reachable, while `projectNeedsInput`, unread and every
notification predicate still exclude it by project kind. The project kind is a
signal the app owns, so the engine keeps it true: **the Memory project holds
the engine's own passes and nothing else.** `create_chat` refuses an ordinary
chat there (`MemoryProjectRefused`), `update_chat` refuses to move one in, a
fork or a continuation of a pass is placed in the workspace's General project
instead, the create route refuses a client-supplied `memory_pass` helper, and
the agent surface reports the refusal as `invalid_project`. Without that, a
client could silence any chat it created by moving it into the Memory project
or by labelling it a pass. A routine is a target like any other, so the same
rule reaches the scheduler: `prepare_schedule_chat` refuses a target the app
will not run in, and the refusal is raised rather than swallowed — the caller
has to record it, because a run that never happened is not a run and a
one-shot entry that never ran must not be consumed. The scheduler's tick
catches it, records the entry as failed and carries on with the other due
schedules (one entry that cannot run must not take the rest of the tick down
with it); the interval path disables the entry with the reason. The dispatch
checks the target before a routine's command runs, and the schedule routes
refuse such a target with a 400 that names the reason — while still letting
an existing invalid routine be paused or retargeted. A user project called
"Memory" is not the app's own: only the kind is the discriminator, and a
schedule whose project id decayed recovers to the user's project. A pass's own archive is
not a second row: `memoryInsights` skips any chat
that is a pass when reading archive state.
For Chat details changes, verify the docked rail and narrow-pane drawer together: both render `AgentContextSection.vue`. The × closes the surface; the ⓘ tab reopens it, restoring focus between the rail heading and chat-body tab. Project context is readable prose with file mentions linked in place, resolving the canonical filename to its full path without a separate link row; token estimates count the sent capsule.
For task-board layout changes, preserve To do, In progress, In review and Done on every width: fixed 310px lanes side by side that scroll sideways when they do not fit, with Done behind a tab at the end of the row. A status filter selects one group; horizontal drag and Shift+Left/Right movement are only available while columns are side by side. Earlier done tasks remain behind Show all. Card and lane reordering runs on pointer events and `setPointerCapture`, so a test of it drives the grip with real mouse or touch input (`page.mouse` down, move, up, as in `web/e2e/specs/task-board-reorder.spec.ts`) or with the keyboard; a synthetic `dragstart` or `draggable` check never reaches the engine. For any grip, lane or motion change, also check the 44px coarse-pointer targets, reduced motion (the placeholder stays, the travel goes) and browser zoom. Run these specs against the fixture server that `web/e2e/support/app.ts` boots, never against a live engine.
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
drain and the local feed) is gated on the TCP peer address plus a loopback `Host` and a same-origin `Origin`/`Referer` when present; the peer alone never grants it. Do not add a capability or an origin exception for a page the model or
a remote browser can influence. Auth/origin coverage is in
`tests/test_auth_security.py`; hostile artifact coverage is in
`tests/test_workspace_html.py`. This does not yet confine broad file endpoints,
isolate browser storage/service workers, or replace the host password protocol
with revocable per-device credentials; those remain follow-up work for #546.

The completed-reply mention route (`/api/chats/{chat_id}/file-paths/resolve`, #2118) is a security-sensitive surface and needs a PR-scoped security review before merge. Its focused regression command is `PYTHONPATH=$PWD .venv/bin/python -m pytest tests/test_chat_file_mentions.py -q`. Root-isolation tests must build real config layouts (legacy and migrated receipt) and must not monkeypatch `agent_root` or `workspace_vault_root`, because a stub can agree with itself about a layout the engine never produces.

## Quality gates

For task progress/history changes, run `tests/test_task_updates.py`,
`tests/test_task_log.py`, `tests/test_task_resolution.py`,
`tests/test_task_markdown.py` and `tests/test_task_board_store.py`. Progress-note
coverage must preserve literal section markers, encoded marker text and old
unversioned items through add and edit operations.

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

A vault can list generated files in a `.ciaoignore` at its root: one glob per line, `#` comments, gitignore-style (`*` and `?` stay within one path segment, `**` spans directories, a pattern without `/` matches at any depth, a trailing `/` matches a directory, case-insensitive, no `!` negation). Matching files are skipped by `vault-lint`, `os-audit`, the orphan and duplicate checks, `INDEX.md` and the Memory Map, but stay valid link targets. Search indexing does not read it.

Use `--vault-root` to choose a vault. Otherwise it uses `CIAO_VAULT_ROOT` or
`./memory-vault`. A clean scan exits 0. Findings, a missing vault root, or an
incomplete traversal exit 1. It never reports a clean vault when it could not
inspect every path.

### Proposal files and the Unsure list

Memory is written directly and Git is the record, so there is no review queue. What the agent is unsure of is one line on `Workspace/Unsure.md` (`ciao/unsure_list.py`: `append_unsure` takes the file lock from `ciao/file_kernel.py`, skips an item already on the list and refuses credential-shaped text). A skill edit, guide edit, upstream issue or note retirement is a proposal file `Workspace/Proposals/<slug>.md`; `ciao proposal-check FILE [--json]` (`ciao/proposal_files.py`) prints every problem in it at once and never edits it. The line format, the table of what a ticked line means and the proposal front matter are in the stock memory guide (`references/unsure-and-proposals.md`), and `tests/test_proposal_files.py` and `tests/test_unsure_list.py` pin both. The legacy queue is drained by the care run (`references/drain-legacy-queues.md`).

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

The daily `system-memory-curation` schedule is presented as **Workspace care**. Its packaged prompt (`ciao/stock/schedules.json`) is a short orchestrator over a dynamic checklist, not a procedure. The run: (0) a preflight loop checks that the installed `ciao` has every subcommand and key flag the run uses and reports one clear error if not; (1) an old report with an open `- [ ]` line marks an interrupted run and becomes a needs-you item; cheap read-only commands decide which steps are due (Unsure ticks, legacy queues, region `pct` of 85 or more, learnings at x3, and weekly stale notes, hygiene, guide review and decision rules) and write `Workspace/Care-Report.md` fresh, one `- [ ] <step id>: <what> (why due)` line per due step; (2) each judgement step runs in a fresh subagent, one at a time, whose prompt only points at the shared `step-preamble.md` and that step's file under `ciao/stock/skills/ciao-capabilities/references/memory-operations/references/` (each file starts with `# Step <id>:` and has Inputs, Procedure and Done when), while hygiene runs as plain `ciao` commands in the orchestrator; (3) the orchestrator runs the step file's `Verify:` command itself and ticks `[x]` only with evidence plus the verify output (a subagent's `done:` alone is not enough), otherwise blocked `[!]` after one re-dispatch; a provider with no subagent tool runs the step file inline; (4) the run does not end while a `- [ ]` line remains; (5) a completion check re-reads the report and the reply starts with `Care complete: N/N` or `Care incomplete: N/M, blocked: <ids>`. The weekly cadence is carried by a `Weekly done: YYYY-MM-DD` line in the report, copied forward because the report itself is rewritten nightly, and advanced only on a night the full weekly batch ran and every weekly line is `[x]` or `[!]`. A weekly step still `[!]` after its retry is recorded in a `Weekly retry: <id>=<nights>` header line; on the following nights only those ids get a weekly line again (not the whole batch), until the step succeeds or has been blocked 3 nights in a row, when it stays `[!]`, is listed under What needs you with its reason and leaves the retry line. Sections after the checklist (such as `## Hygiene`) use plain bullets, so the app counts only the checklist's checkbox lines. `tests/test_stock_package.py` and `tests/test_cli_docs_drift.py` pin the step ids, the line format, the no-tick-without-evidence rule and that every named file and command exists. Packaged system definitions are read from the wheel, so an installed schedule row picks the new prompt up on update with no row change.

### Bounded-memory rot audit

`ciao/memory_audit.py` checks whether the content of the always-loaded
`ciao:memory` / `ciao:profile` regions has rotted, as opposed to the mechanical
checks (caps, expiry, exact duplicates) that `audit_memory` already ran. It rests
on one rule: a remembered fact is either **state**, a current value that gets
replaced when it changes, or an **event**, a thing that happened which gets
appended to a log and never edited. The regions are a state surface.

The memory write policy is described in `docs/ARCHITECTURE.md` under "Memory
write policy"; `ciao/memory_policy.py` holds the capsule's unattended guidance. Two rules matter for
any change here: the region cap is **advisory** on every path (a write goes
through and reports `over_cap`; consolidation, not refusal, bounds a region),
and an **unattended run defers** approval-requiring work and reports it instead
of asking or routing around the missing reviewer. For a scheduled run the ACP broker
is the backstop: an approval it cannot wait for is denied at once and the run is
recorded as `error` naming the denied tool (`tests/test_schedule_dispatch_boundary.py`). `tests/test_memory_policy.py`
pins the stock assets and the docs to the capsule, so a copy that contradicts it
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
its review horizon is a review candidate; logs and journals record events,
which never go stale. One predicate, `memory_audit.note_verification`, ages each note from
frontmatter `updated:` (a deliberate "I re-checked this" claim) or file mtime,
against one horizon for every note, `memory_audit.NOTE_REVIEW_DAYS`: a note is flagged at 30 days or more, not before;
types are resolved through the vault's alias table; `log`, `journal` and `workspace`
exempt — flagging an inbox for being an inbox is noise). `find_stale_notes`,
the Memory Map's `stale` flag and the vault-review `unverified` signal all call
it, so the three cannot disagree. The map additionally leaves `stale` off notes that `memory_audit.never_aged` names (`Workspace/` paths, templates, `projects/completed/`). Like `superseded_state_candidates` these findings are informational: they surface in `os-audit`'s memory section, the Memory Map, and the weekly `system-memory-curation` re-check.

### Creating a note: `note_create`

`ciao note create --type T --title T` is the one way an agent makes a vault note
(`ciao/note_create.py`, `control_plane.note_create`, the `note_create` operation,
`_WRITE` so plan mode refuses it). The type is checked against the vault's
category registry (`load_entity_types` on the agent vault root); the note lands
under the workspace vault root in the category's folder, or in `--path`, a
vault-relative folder, which a category with no folder requires. The description
and body come from files inside the workspace (`--description-file`,
`--body-file`), confined by `_safe_relative` and capped at
`MAX_NOTE_FILE_BYTES`, so prose never travels as an argument. The file is opened
exclusively: an existing note is `note_exists`, never overwritten. The search
index is not refreshed; `vault_search` indexes on every query. Tests:
`tests/test_note_create.py`, `tests/test_agent_surface.py`.

`ciao memory-audit` reads one file and skips the vault scan, so the daily
`system-memory-curation` schedule can afford to call it and fix what it finds.
`--with-vault` adds the note-aging pass (still informational, never changes the
exit code). Exit 0 clean, 1 findings, 2 a region could not be read.

### The entry-level detector

`memory_audit.note_entry_coverage` is the one function that decides which list item inside a note is overdue (each item ages from its own `[verified: YYYY-MM-DD]` stamp, the note's date is only the fallback). `os-audit` and the Memory Map both read it, so they cannot disagree about which fact is due; a paragraph, table or quote is reported as not measurable rather than as clean. The tests are `tests/test_memory_audit.py` and `tests/test_vault_graph.py`.

## Skills, subagents, and slash commands

Packaged generic skills live in `ciao/stock/skills/` and are installed into every workspace's `.claude/skills/` by `ciao sync-skills` on startup. This includes Ciaobot-specific skills (`ciao-capabilities`, `workspace-authoring`, …); Ciaobot troubleshooting is a reference under `ciao-capabilities`. In a **workspace**, user-owned skills live in `skills/`, project agents in `subagents/`, and slash commands in `commands/`; `ciao sync-skills` mirrors them into the generated `.claude/` directories. A workspace skill with the same name as a packaged one overrides it. A workspace-owned `ciao-capabilities` override replaces the installed skill and its references. Durable-memory procedures are loaded separately from the engine-managed package, so the override does not need to copy them.

No subagents are packaged by default. Attended vault writes and proposal handling use the durable-memory guide under `ciao-capabilities`; the Workspace care schedule carries its own unattended rules and runs vault writes sequentially in its own chat. Reading tasks use the provider's own web-fetch. Startup sync prunes retired stock-managed `memory`, `researcher`, and `secretary` from generated Claude/OpenCode inventories; custom subagents and unmanaged agent files are left alone.

### Provider context and native memory

Normal chats send one compact provider-neutral context capsule containing the
active workspace/project, canonical document, date, retrieval hint, entity
matches, and unattended-turn marker. Stable routing facts are sent once per
provider session; handovers use a separate bounded excerpt. Claude and OpenCode
receive the same compact Ciaobot core, while their native
The `AGENTS.md` guide loaders remain the only source of bounded memory.
`memory_tool.py` prunes valid expired entries before provider startup and
exposes `memory_status`/`memory_update` without creating a second memory store.

#### Shared-brain recall acceptance (opt-in)

Run this only against a disposable checkout of the candidate and a disposable
workspace. Never restart or reconfigure the operator engine, reuse its runtime,
or use a real workspace or vault. Give the candidate checkout its own approved
HTTPS/SSH origin identity for the linked brain. Use a free port and keep setup,
runtime data, memory index, and launch-agent export under one temporary root.
For example, from a shell with the candidate checkout at `$CANDIDATE_CHECKOUT`:

```bash
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ciao-brain-acceptance.XXXXXX")"
python3 -m venv "$TEST_ROOT/venv"
"$TEST_ROOT/venv/bin/python" -m pip install -e "$CANDIDATE_CHECKOUT"
export CIAO_RUNTIME_ROOT="$TEST_ROOT/workspace/.runtime"
export CIAO_MEMORY_DIR="$TEST_ROOT/memory"
export CIAO_LAUNCH_AGENTS_DIR="$TEST_ROOT/LaunchAgents"
"$TEST_ROOT/venv/bin/ciao" setup \
  --workspace "$TEST_ROOT/workspace" --port 18543 \
  --launch-agents-dir "$CIAO_LAUNCH_AGENTS_DIR"
cd "$TEST_ROOT/workspace"
"$TEST_ROOT/venv/bin/python" -c 'import json; from pathlib import Path; p = Path(".runtime/app_settings.json"); settings = json.loads(p.read_text()); settings["pwa_host"] = "127.0.0.1"; p.write_text(json.dumps(settings))'
"$TEST_ROOT/venv/bin/ciao" run
```

Confirm the selected port is free first; substitute it in setup and use only
the matching loopback URL. The app setting is required because the engine
defaults to listening on every interface. `ciao setup` generates the separate
PWA password; use it only for this session. Do not pass `--load-launchd`. Keep the provider
CLIs' normal user authentication in place; do not copy credentials into the
test workspace or its environment. Record the candidate SHA and host, and
verify each configured CLI and ACP adapter version before testing.

Create a disposable Git checkout linked in Settings as a shared brain named
`team`, with `decisions/rollout.md` containing a newly generated random
identifier. Commit the file, then remove any repository-local commit identity
settings (`user.name`, `user.email`) before linking it; shared-brain checkouts
reject repository settings outside the safe allowlist. Keep the identifier out
of the question, the Ciaobot workspace/vault and any model-visible file or
directory name. For each
provider in `ciao/providers/acp/agents.py` (`CLAUDE`, `CODEX`, `OPENCODE`), use
a new ordinary project chat with normal permissions and ask exactly: “What
rollout identifier did the team choose, and where is that recorded?” Record a
pass only when the final answer includes both the exact identifier and
`team:decisions/rollout.md`; also record the provider's file-read/tool evidence
when exposed. An answer without its source fails.

Then use a fresh identifier in a fresh committed checkout and a new chat for
each provider. Deny access to that file either with a verified OS-level read
denial for the provider's effective OS identity, or by denying the provider's
file-read request at its normal permission prompt. Verify the denial under
that same effective identity or at that prompt before asking the same
question. Restore ordinary permissions after the case. Pass only when the
answer explicitly says it cannot access the source and gives neither the
identifier nor an invented attribution. If the host cannot enforce and verify
the denial, mark the case blocked; do not count a missing file or an unverified
permission setting as a denial.

Keep each provider to these two cases. Record one redacted row per case on the
issue: candidate SHA, host/OS, provider and CLI version, ACP adapter/version,
allow or deny result, exact attribution (or `none`), whether tool-read or
denial evidence was observed, and a short failure/blocker reason. Do not post
the identifier, prompts from real workspaces, credentials, or private paths.
Remove the disposable engine, workspace, checkouts, and temporary identity
afterward, and restore normal file permissions. Stop the foreground engine
with Ctrl-C, verify `TEST_ROOT` is the acceptance temp directory, then remove
that directory. Preserve
`tests/test_chat_shared_brain.py::test_agent_request_carries_no_extra_directory`;
the linked brain remains a file-tool recall path, not an additional provider
directory. Unit coverage is a focused check, not a substitute for the three
providers' allow and deny cases.

Edit canonical sources, not the generated `.claude/` or `.agents/` dirs. Do not run `npx skills update` ad-hoc (it re-expands the lockfile and repopulates bloat).

OpenCode runs as `opencode acp` (`ciao/providers/acp/agents.py`), and its start gate refuses
any version below 2.3.2. Provider changes replay the ACP fixtures under `tests/fixtures/acp/`
and run one tiny real turn against the installed `opencode acp`. Keep V2 internal
credential resources covered in both absolute and workspace-relative spellings
when a custom runtime root is configured. Keep form constraints and explicit
empty-versus-cancel semantics in the provider/PWA contract. Permission cards
may choose only an unambiguous `allow_once`; shell approvals display the
exact validated command the provider submitted, bounded at 4096 characters.
Native form and
permission cards stay mounted until a matching `(chat_id, session_id,
request_id)` response is acknowledged. Permission and form requests from
task/subagent child sessions (known from `session.created.parentID`, or the
server's active-session map) surface with the child's own session id; every
other child event stays filtered. The PWA keeps complete response frames
in memory only (never localStorage), queues them across closed or failed
sockets, and retries them on reconnect; a timeout makes the card retryable but
does not discard the queued frame. Backend state is cleared only after a typed
V2 success or an authoritative stale result. Resolution events are ephemeral
for attached tabs, while the persisted chat snapshot reconciles tabs that were
disconnected. `background_shell_wake.jsonl` and `background_subagent_wake.jsonl` pin OpenCode's own follow-up executions after background work (#1391): inbox delivery ids decide which execution a turn owns, and `drain_events` streams the rest.

`ScheduleManager.catch_up()` runs once at server startup on the host; It dispatches only the latest missed occurrence for each enabled schedule, leaves the prompt unchanged, and records the missed occurrence's local date so a later slot on the startup day can still fire normally. Cover changes to this behavior in `tests/test_schedules.py`. Packaged system routines are excluded when the startup falls inside the post-setup grace window (`ciao/setup_marker.py`, 24h from a first-time setup): a brand-new install is greeted by its onboarding chat, and the routines fire at their next regular tick instead of all replaying missed runs in parallel. Cover that in `tests/test_setup_catch_up_grace.py`.

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
is the point of the CLI surface (D-01), not a leak. Do not add the token to
telemetry arguments.

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

## Change guidelines

- **Doc the change.** After any change to `ciao/`, `web/`, `scripts/`, `deploy/`, or `pyproject.toml`, refresh `docs/ARCHITECTURE.md`, this file, `AGENTS.md`, and `INTEGRATIONS.md` against actual repo state before declaring the task complete. Skip only for pure bugfixes that touch nothing in layout, capabilities, install steps, env vars, endpoints, or commands.
- **New API routes must be documented.** Add the route to `PWA_API.md`; state-changing routes also need an Agent recipe or an allowlist entry in `tests/test_pwa_api_docs.py`. New `CIAO_*` env vars must land in `INTEGRATIONS.md` or the allowlist in `tests/test_env_vars_documented.py`. Both are test-enforced.
- **Never restart the ciao service yourself** from inside the PWA. Apply code changes and ask the operator to hit Deploy.
- **Never commit `.env` or API keys.** `.env` minimum: `PWA_AUTH_TOKEN` (the dashboard password; protection is always on).
- **Keep edits minimal and consistent with existing patterns.** Don't refactor unrelated code; if unrelated changes appear, pause and ask.
- **Avoid destructive git** (force push, hard reset on shared branches) unless explicitly asked.
- **Use the branch model in `CONTRIBUTING.md`.** Day-to-day PRs target `develop`; release PRs target `main`.
- **Write tests** for new Python behavior; add to `tests/`. PWA changes verify via `npm run build` typecheck at minimum.
- **Name the encoding.** Every text `open()`, `read_text()`, `write_text()`, `os.fdopen()` and every `subprocess` call that decodes (`text=True`) passes `encoding="utf-8"` — with `errors="replace"` only for tool output that is displayed or logged, never for output that is parsed. The locale's default encoding is not guaranteed to be UTF-8, and nothing may rely on Python's UTF-8 mode. `tests/test_explicit_encoding.py` fails on a new call without one.
- **Verify UI accessibility.** For PWA layout changes, check keyboard operation, visible focus, browser zoom, and 44px mobile targets at a narrow-phone viewport in addition to the build.
- Keyboard shortcut changes also need coverage for shared `/ws/events` updates, custom-binding conflicts, IME composition, picker precedence, and Enter behavior on touch layouts.

