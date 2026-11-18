---
name: ciao-dev-install
description: Build the branch you are working on into what runs on this machine for testing — the Ciaobot for Mac app (bundle with the engine built from HEAD) and/or the engine service (wheel + PWA) — reinstall it, and watch the logs for issues. Trigger on "install develop", "test the develop build", "install the current dev version", "build and install ciaobot", "install dev build", "build and install the mac app", "install the app from my branch", or asking to test new changes locally instead of via the release installer.
---

# Ciaobot develop install

> Contributor/project skill — lives in the repo's workspace `skills/` folder, **not** `ciao/stock/skills/`. It is for people working *on* Ciaobot and is deliberately not packaged or shipped to end-user installs. `ciao sync-skills` mirrors it into the runtime-discovered `.claude/skills/` catalog. Don't move it into `ciao/stock/`.

Replaces the end-user install (`curl -fsSL .../install.sh | sh` for the engine,
the released DMG for the app) with the same outcome built from the **local
checkout you are standing in** — whichever branch you are on, not only develop.
Two install surfaces, built as they are needed:

- **The Mac app** (`~/Applications/Ciaobot.app` by default): the app bundle with
  the engine built from `HEAD` embedded at `Contents/Resources/engine`,
  including the current PWA inside that engine, ad-hoc signed by
  `mac/scripts/package-app.sh`. This is how app behaviour is tested from any
  branch, and it is the flow below.
- **The engine service** (the `com.ciao.server.plist` LaunchAgent): an editable
  install of the checkout's `ciao/` engine plus current PWA into the checkout's
  `.venv`, serving the existing workspace and password. This is how engine
  changes reach the running PWA before the app runs the bundled engine as its
  daily driver.

Then watch the logs and report anything broken. Expect a few minutes; the app
build dominates, and its engine part (npm, vite and pip inside
`mac/scripts/engine_build.py`) reruns on every build.

## Before you start

- The source is the worktree or checkout the maintainer is working in, on any
  branch: no `git switch`, no `git pull`, no develop requirement. Name the
  branch and commit in the report.
- **Dirty tree warning for the app flow.** `mac/scripts/build-app.sh` builds
  the bundled engine from `HEAD` and refuses a tree with uncommitted tracked
  changes (`engine build failed: commit or stash tracked changes; the engine
  is built from HEAD`) — so warn the user and let them pick, do not refuse on
  your own:
  - commit first (the branch they are testing should carry the change anyway),
    or
  - build from a temporary worktree of `HEAD` — nothing uncommitted can reach
    that build:

    ```bash
    git -c core.fsmonitor=false worktree add --detach "$SCRATCH" HEAD
    ```

  A worktree needs node/npm on `PATH` and network on a cold engine cache.
- **The engine-service flow is editable by design**: it installs the tree
  exactly as it is, uncommitted changes included. A dirty tree is the point
  there, not a warning; just say what state the tree is in.
- Toolchain for the app flow (from `mac/AGENTS.md`): Xcode or Command Line
  Tools via `DEVELOPER_DIR`, node and npm on `PATH`, network on a cold engine
  cache — **and a bare `python3` of 3.12 or newer on `PATH` for
  `scripts/package-app.sh` itself** (any version from 3.12 up is fine —
  3.13, 3.14 and later included): after the build it signs and checks the
  bundled engine with plain `python3 -I -S` and dies with `python3 3.12 or
  newer is required to sign the bundled engine` otherwise. The 3.12-or-3.13-only
  limit belongs to the engine build (`mac/scripts/build-app.sh`: the first of
  `python3.12`, `python3.13` on `PATH`, else `python3` only when it is 3.12 or
  3.13; anything newer is refused there) and does not apply to
  `package-app.sh`. The engine build's own `python3.12`/`python3.13` pick does
  not cover the packaging step, and on a Mac the default `python3` (the
  Command Line Tools one) is usually older. Satisfy it with a PATH shim for
  the packaging run, e.g.
  `mkdir -p "$HOME/.local/bin" && ln -s "$(command -v python3.12)"
  "$HOME/.local/bin/python3"` and prepend `"$HOME/.local/bin"` to `PATH`.
  Toolchain for the service flow: Node 22 (`.nvmrc`), `uv`, Python 3.12+, and
  a repo `.venv` with `pip install -e '.[test]'` already run. Install anything
  missing before building.
- Never apply a develop-only migration to the live workspace. Neither flow
  runs migrations against the daily-driver workspace; if a branch needs new
  workspace state tested, test it in a throwaway workspace, never the real
  one.
- The installed engine and app each carry state: the workspace and password
  live in the plist's workspace `.env`, and both flows preserve them. Ask the
  user before installing if a step would overwrite something unexpected, and
  always confirm the switch because it cuts off whatever the running
  app/engine is doing.

## 1. Check the running engine and active chats

The install cuts off anything the running engine is doing — a service restart
in the engine flow, a relaunch on the bundled engine in the app flow. Check
before building:

```bash
.venv/bin/ciao service status --json
```

From the JSON, note:
- `reachable` — is the currently installed engine serving? **This says nothing
  about whether your code is being served** — see `python_path`.
- `python_path` — the interpreter the plist actually runs. If this is *not* your
  checkout's `.venv` (a `~/.local/bin/ciao` uv-tool release install is the usual
  culprit), the service is running a *copy*, so none of your edits are live and
  `reachable: true` tells you nothing useful. Say so before installing: Flow B
  repoints the plist, which switches the daily-driver service over to your
  branch. Back the old plist up
  (`cp …/com.ciao.server.plist …plist.bak.devinstall`) so the release install
  is one command away.
- `restart_blockers` — a list of `{chat_id, reasons}`. **Non-empty means a
  switch would interrupt chats** (working, or waiting on an open
  question/approval card); `null` means the engine could not be asked, which
  should be treated as blocked. Ask the user to let them finish (or confirm
  explicitly) before proceeding. `ciao service restart` and
  `ciao service stop` enforce the same gate with a `--force` escape hatch —
  never pass `--force` silently.
- `loaded`/`installed` — sanity check that the engine you are about to replace
  is the one `com.ciao.server.plist` manages.

If `.venv/bin/ciao` dies with `bad interpreter: …/some-other-repo/.venv/bin/python`,
the console script was generated before this checkout was renamed or moved. B2's
`pip install -e` regenerates it, but step 1 needs the CLI *now*, so fall
back to the module entry point (`python -m ciao` does **not** work — `ciao` has
no `__main__`):

```bash
.venv/bin/python -c "from ciao.cli import main; main()" service status --json
```

Re-check right before the switch in either flow: a chat started during the long
build is just as interruptible.

## Flow A — build and install the Mac app from this branch

This is the flow for "test the app from my branch". It builds the app (engine
from `HEAD`, current PWA inside it), installs it over the installed copy, and
leaves the engine service and workspace exactly as they are.

### A1. Build the bundle

From `<checkout>/mac`:

```bash
DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer scripts/package-app.sh
```

- Ad-hoc signing is fine for local testing; there is no Developer ID and no
  notarization in this build.
- Output is a fresh directory holding the sealed, ad-hoc signed `Ciaobot.app`,
  `Ciaobot-<version>-adhoc.zip`, `MANIFEST.txt` and `SHA256SUMS`. Pass
  `--output DIR` (a new or empty directory) to fix the path — for example
  `--output "$out"`, so the dev-install step below can name
  `"$out"/Ciaobot.app` — or read the `app:` line the script prints at the end.
  `MANIFEST.txt` records the `engine_version`.
- The DMG is not the dev artifact while the engine has no distribution signing:
  `make-dmg.sh` refuses an engine-bearing app, and every app this flow builds
  carries the engine. Install the app `package-app.sh` produced; the
  `make-dmg.sh` route returns when distribution signing lands.
- The build verifies itself: codesign before and after archiving, and the
  engine's `bin/ciao --version` against the committed `pyproject.toml` version.
  Trust but re-verify at the staging step below.

### A2. Stage, self-check, swap — through the dev-install script

The installed `Ciaobot.app` is a registered app name: **writing into it is
denied** (`ditto` reports `Operation not permitted`), so the new bundle must be
staged under a different name, checked there, and swapped in by rename. Never
copy into the live name, and never delete it. `mac/scripts/dev-install-app.sh`
does exactly that, small and testable:

```bash
<checkout>/mac/scripts/dev-install-app.sh --staged "$out/Ciaobot.app" --dry-run
```

`--dry-run` runs the self-check and prints the plan without changing anything.
It defaults to replacing `$HOME/Applications/Ciaobot.app`; pass
`--installed /Applications/Ciaobot.app` for an install living there. Run the
script from the SAME checkout/worktree that was built — it compares the
engine version against the `pyproject.toml` two levels above the script — or
pass `--expect-engine-version` when you must call it from elsewhere. What it
does (and what you should be able to narrate before confirming):

1. Stages the bundle into `<installed folder>/.Ciaobot.app.staging` — the
   hidden, differently named sibling the live name is protected from.
2. Self-checks the **staging copy in place**: `codesign --verify --deep
   --strict`, and the bundled engine's `bin/ciao --version` must equal the
   checkout's `pyproject.toml` version (`--expect-engine-version` to pin it).
   This catches a stale or wrong bundle *before* anything is touched.
3. Moves the installed app aside to a free `.Ciaobot.app.prev-<timestamp>`
   name (kept, never deleted — the maintainer removes stale `.prev-*` copies
   once satisfied) and moves the staged copy into place; if that second move
   is blocked, the previous app is restored and the handoff script of A4 is
   printed. A signal between the two moves puts the previous app back the
   same way (never nesting the backup) and stops non-zero instead.

The script does not enforce any confirmation or the active-chat check: it
swaps as soon as it runs. The confirmation and the section 1 check are the
agent's job (A3) and happen before you invoke the real, non-`--dry-run`
swap — do them, then call it.

Confirm before replacing, then swap:

```bash
<checkout>/mac/scripts/dev-install-app.sh --staged "$out/Ciaobot.app"
```

### A3. Confirm before replacing the running app/engine

- Re-run the section 1 chat check right before the swap.
- Ask the user to quit the running Ciaobot app and to say yes to the swap.
  State plainly what will be cut off and where the previous bundle will be.
- Never swap while a blocker list is non-empty and unconfirmed, and never
  without an explicit go from the user.
- The dev-install script does not enforce these gates — it swaps whatever
  bundle it is pointed at the moment it runs, with no knowledge of chats.
  They bind the agent's decision to invoke it.

### A4. If the swap is blocked, hand the maintainer the script

An agent sandbox may deny the rename even though nothing else failed. The
dev-install script handles it instead of failing: it rolls the previous app
back from the exact backup it recorded, removes the staging copy it created,
exits 0, and **prints a ready-to-run swap script on stdout**. If even the
restore is denied, it exits non-zero and names the exact backup path instead
of printing a script — report where the backup sits and stop. The printed
script re-stages from the `--staged` bundle, so leave the `package-app.sh`
output directory where it is; it refuses a staging directory that is already
in the way, refuses to overwrite any `.prev-*` backup, and rolls the previous
app back if its own move into place fails. Give the printed script to the
maintainer — save it (for example as `~/swap-ciaobot.sh`), have them read it,
and let them run it themselves where the sandbox does not apply. Do not retry
the swap from the agent context, and do not edit the printed script.

### A5. What the app flow must never touch

- The workspace, its `.env` (password), vaults: untouched. The swap moves only
  the app bundle.
- The engine service plist (`com.ciao.server.plist`): untouched, never
  re-rendered here. If the checkout also needs the service, that is flow B
  with its own confirmation.
- No migrations: a develop-only migration must never be applied to a live
  workspace.

### A6. Verify the install

```bash
plutil -p "$HOME/Applications/Ciaobot.app/Contents/Info.plist" | /usr/bin/grep -E 'CFBundleShortVersionString'
env -i "$HOME/Applications/Ciaobot.app/Contents/Resources/engine/bin/ciao" --version
/usr/bin/sed -n 's/^version = "\(.*\)"$/\1/p' <checkout>/pyproject.toml
```

The engine's `--version` line must name the pyproject version the checkout
prints, and the app's `CFBundleShortVersionString` the one `build-app.sh`
defines. Launch the app and drive the feature the branch is about. The
previous app sits at `.Ciaobot.app.prev-<timestamp>` beside it if you need to
go back — a manual `mv` back restores it, nothing else does.

## Flow B — reinstall the engine service from this branch

This is the flow for "test the engine/PWA from my branch" (and what
`/ciao-release` calls before cutting a candidate). It is an editable install:
whatever the tree holds is what runs.

### B1. Build the PWA

```bash
cd web && npm ci && npm run build
```

Output goes to `ciao/web/static/`, which ships as package data inside the
editable install. **Build the PWA before the install**, or the installed engine
serves the previous build's assets.

### B2. Install the current checkout

```bash
cd <path to your ciaobot checkout>
.venv/bin/python -m pip install -e '.[test]'
```

A plain editable install of the checkout is the whole point: it is what makes
`ciao` run the code you are editing. Confirm the import resolves to the
checkout, not to some other copy on the machine — **and run it from outside the
checkout**:

```bash
(cd ~ && /path/to/ciaobot/.venv/bin/python \
  -c "import ciao, pathlib; print(ciao.__version__, pathlib.Path(ciao.__file__).resolve())")
```

The `cd` is not optional. Run from the repo and a *broken* editable install still
prints the right answer: `''` is on `sys.path`, so the local `ciao/` package
shadows whatever the `__editable__` finder points at. A finder left mapping to a
renamed-away checkout (`…/ciaobot-rel/ciao`) imports clean from inside the repo
and raises `ModuleNotFoundError` from everywhere else. Only the neutral-cwd run
tells you whether the install is real.

On macOS, re-render the LaunchAgent against the existing workspace so it names
the current interpreter and code (read the workspace from the installed plist):

```bash
workspace=$(/usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' \
  "$HOME/Library/LaunchAgents/com.ciao.server.plist" 2>/dev/null || true)
[ -n "$workspace" ] && [ -d "$workspace" ] && [ -f "$workspace/.env" ] \
  || { echo "could not recover the workspace from the existing plist" >&2; exit 1; }

.venv/bin/ciao setup --workspace "$workspace" --python "$(pwd)/.venv/bin/python" --yes --load-launchd >/dev/null
```

`ciao setup` preserves the existing `.env` (its variables win over setup
arguments), so the password and vault root survive the re-render. This is the
workspace handling the app flow deliberately does not duplicate.

**Hosted service (`Ciaobot Server.app`).** When the plist's `ProgramArguments`
start with `…/Ciaobot Server.app/Contents/MacOS/Ciaobot Server serve --python
<interpreter>`, `ciao setup` deliberately keeps that live hosted definition
byte-for-byte (`_existing_hosted_definition_serves` in `ciao/cli.py`), so the
command above exits 0 and changes nothing: the service keeps running the old
interpreter. Point the hosted definition at this checkout instead; the restart
in B3 reloads it:

```bash
plist="$HOME/Library/LaunchAgents/com.ciao.server.plist"
if [ "$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:1' "$plist")" = "serve" ]; then
  cp "$plist" "$plist.bak.devinstall"
  /usr/libexec/PlistBuddy -c "Set :ProgramArguments:3 $(pwd)/.venv/bin/python" "$plist"
  plutil -lint "$plist"
fi
```

### B3. Restart the service

Gate on restart blockers first, then restart:

```bash
tmp=$(mktemp)
.venv/bin/ciao service status --json > "$tmp"
python3 - "$tmp" <<'EOF'
import json, sys
data = json.loads(open(sys.argv[1]).read())["details"]
blockers = data.get("restart_blockers")
if blockers is None or blockers:
    raise SystemExit(f"Restart would interrupt: {blockers}")
print("no restart blockers - clear to restart")
EOF
[ $? -eq 0 ] && .venv/bin/ciao service restart
```

`ciao service restart` reloads an edited plist (bootout and bootstrap) when the
loaded definition differs from the one on disk, and kickstarts otherwise. Confirm
the switch took with `ciao service status --json`: `loaded_python_path` is the
interpreter launchd is actually running and `plist_matches_loaded` must be
`true`. (`python_path` is read from the plist file, so on its own it reports the
new interpreter even while the old process is still serving.)

Two things bite here. `status=$(…)` is a **zsh read-only variable** and aborts the
whole snippet, so use a neutral name; and backslash-escaping quotes inside a
`python3 -c '…'` f-string does not survive the shell, so feed the script on
stdin instead. `raise SystemExit(msg)` prints to stderr and exits non-zero, which
is the gate you want.

Never pass `--force` silently; if the user explicitly asked to cut through, say
so out loud in the report.

### B4. Watch for issues

The engine logs live in the workspace:

```bash
tail -f "$workspace/.runtime/ciao.stdout.log" "$workspace/.runtime/ciao.stderr.log"
```

Watch for at least a minute. Issues to flag to the user:

- **Engine crash loop** — the engine exits shortly after starting, repeatedly
  (launchd `KeepAlive` restarts it). Look for tracebacks in `ciao.stderr.log`,
  and count boots rather than trusting a single healthy-looking process:

  ```bash
  grep -c "Starting Ciaobot server" "$workspace/.runtime/server_debug.log"
  ```

  One boot for this restart is the pass; the count climbing across a minute is
  the loop. `ciao setup --load-launchd` (step B2) bootstraps the plist, so the
  restart in B3 legitimately produces **two** boots a few seconds apart —
  that is not a loop.
- **Import errors** at startup (`ModuleNotFoundError`, `ImportError`) — usually
  a `pip install` that ran before the PWA build, or an import resolving to a
  different `ciao` on the machine's `sys.path`.
- **Port already in use** / startup refused because another Ciaobot backend
  owns the runtime root.
- **PWA not reachable**: `curl -s http://127.0.0.1:8443/` should return HTML. If
  the engine answers but the PWA shows the recovery page, the static bundle
  behind the running process is stale — re-run B1 and restart again.
  Confirm you are serving *this* build by diffing the asset hash the served
  `index.html` names against the one on disk:

  ```bash
  grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' ciao/web/static/index.html
  curl -s http://127.0.0.1:8443/ | grep -oE 'assets/index-[A-Za-z0-9_-]+\.js'
  ```
- **Version mismatch** — the served version should match the checkout:
  `.venv/bin/ciao --version` vs Settings → General.

Sanity probes:

```bash
launchctl print "gui/$(id -u)/com.ciao.server" | grep -E "state|pid" | head
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8443/
(cd ~ && /path/to/ciaobot/.venv/bin/python -c "import ciao; print(ciao.__file__)")
```

## Report

Summarize: the branch and built commit (`git rev-parse --short HEAD`), which
flow ran, the version checks (app `CFBundleShortVersionString` and the engine's
`bin/ciao --version` against the checkout's `pyproject.toml` version for flow
A; `ciao --version` vs Settings for flow B), the workspace preserved, the
staging/backup paths (flow A) or plist backup (flow B), engine and PWA status,
and any log findings with their source line numbers.

Also report what changed underneath the user: if the plist was pointing at
something other than this checkout, say plainly that the service moved from a
release install to the branch, and where the old plist backup is; if the app
was swapped, say which bundle was moved aside and where it sits now.

If the install succeeded but a backend error appears, create a GitHub issue for
it (`gh issue create --repo raffaelefarinaro/ciaobot ...`) — but only for
genuine defects. Check the by-design log lines in the traps below first, and
prefer telling the user a finding is a workspace-config concern over filing
noise against a working engine.

## Notes and traps

- **The live app name is write-protected: rename, never write.** Writing into
  the registered, live `Ciaobot.app` name is denied (`Operation not permitted`),
  which is why the flow is stage → self-check → move aside → move into place.
  Never `cp`/`ditto` directly into the installed name, and never delete the
  previous bundle: the `.prev-` copy is the only way back.
- **A staging name already there is a stop, not a target.** A leftover
  `.Ciaobot.app.staging` from an earlier run (or anything the maintainer keeps
  at that dot-name) is refused by the dev-install script, never overwritten.
  Inspect it, then remove it by hand if it is the old staging copy.
- **The swap cut-off is a confirmation, not a detail.** Relaunching the app
  with a new bundle interrupts whatever the running app/engine is serving.
  Section 1's chat check and the user's explicit yes come before every swap and
  every service restart.
- **A running process is not a working engine.** Confirm the PWA answers and
  that the boot count in `server_debug.log` is not climbing. An auth-required
  install returns `unauthorized` from `/api/startup` — that is a pass, not a
  failure.
- **Don't diff this boot's log by line offset.** Startup runs
  `startup_triage: Capped oversized service logs`, which truncates and rewrites
  `ciao.stderr.log` *during* the very boot you are trying to measure, so an
  offset you captured in preflight silently points at the wrong region. Filter
  `server_debug.log` by timestamp instead — it is not capped.
- **`Uvicorn running on` goes to stderr, not stdout.** Grepping only
  `ciao.stdout.log` for it returns 0 and reads like a failed boot. The
  app's own markers are `Starting Ciaobot server on 0.0.0.0:8443` and
  `Started server process [pid]`.
- **Build the PWA last-to-ship, not last overall.** The editable install copies
  package data (`ciao/web/static/`) at install time, so an install that ran
  before `npm run build` bakes in the previous build's assets. When in doubt,
  repeat B1 then `pip install -e .` then restart. Flow A does not have this
  trap: its engine build always includes the current PWA, at the cost of
  rerunning npm/vite/pip every build.
- **The editable install is the source of truth, not `PATH`.** Two `ciao` on one
  machine is normal in a contributor setup; always drive this skill with
  `.venv/bin/…` so you are talking to the checkout you just built.
- **Never trust `import ciao` run from inside the checkout.** `''` is on
  `sys.path` when cwd is the repo, so the local `ciao/` package shadows the
  `__editable__` finder and a totally broken install still prints a healthy
  version and path. Every import check here must run from a neutral directory
  (`(cd ~ && …)`), and the answer to give is the path — not the version, which
  will look right either way.
- **Two log lines are by design, not defects.** Do not file a GitHub issue for
  either without first reading its source:
  - `startup_triage: Startup found runtime errors but a triage chat ran at …;
    waiting out the cooldown` — `TRIAGE_COOLDOWN_S` is 12h
    (`ciao/startup_triage.py`). The suppression is working.
  - `backup_service: Memory backup failed: … N tracked path(s) outside the
    backup scope would not be backed up` — by design; its own docstring says
    "a developer checkout reports its whole application source"
    (`ciao/backup_service.py`). The backup still commits and pushes. A workspace
    that is itself a git repo will always trip this.
- **Never restart from inside a PWA chat.** Apply the change and ask the
  operator to hit Deploy or Restart in Settings.
- **`pytest tests/` is not a smoke test.** The suite mocks the provider, the
  launchd surface and the clock. This skill is what proves the engine boots
  against a real workspace.
- If a step fails, leave the machine in a known state: say which step failed and
  what is still running — including whether the app was replaced, where the
  `.prev-` copy sits, and whether the printed swap script is still pending —
  rather than reporting a half-finished install as done.
