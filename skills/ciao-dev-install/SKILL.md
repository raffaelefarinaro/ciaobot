---
name: ciao-dev-install
description: Build the current develop checkout into an installed engine (wheel + PWA) and run it on this machine for testing, then watch logs for issues. Trigger on "install develop", "test the develop build", "install the current dev version", "build and install ciaobot", "install dev build", or asking to test new changes locally instead of via the release installer.
---

# Ciaobot develop install

> Contributor/project skill — lives in the repo's workspace `skills/` folder, **not** `ciao/stock/skills/`. It is for people working *on* Ciaobot and is deliberately not packaged or shipped to end-user installs. `ciao sync-skills` mirrors it into the runtime-discovered `.claude/skills/` catalog. Don't move it into `ciao/stock/`.

Replaces the end-user one-liner (`curl -fsSL .../install.sh | sh`) with the same
outcome built from the **local `develop` checkout**: the current `ciao/` engine
plus the current PWA, installed into this machine's own Python environment,
serving the existing workspace and password. Then watch the engine logs and
report anything broken.

There is no app bundle to assemble any more (`#656`) — the release is the
engine, so this skill's whole job is: build the PWA, install the Python package,
restart the service, watch the log. Expect a few minutes, dominated by the
`npm ci` dependency install.

## Before you start

- The checkout must be on `develop`, clean, and up to date.
- Toolchain must be present: Node 22 (`.nvmrc`), `uv`, Python 3.12+, and a repo
  `.venv` with `pip install -e '.[test]'` already run. If any is missing,
  install it before building.
- The installed engine is the one `com.ciao.server.plist` manages, and the
  workspace and password live in that plist's workspace `.env`; both are
  preserved. Ask the user before installing if the steps would overwrite
  something unexpected, and always confirm the restart because it cuts off
  whatever the running engine is doing.

## Steps

### 1. Preflight

```bash
cd <path to your ciaobot checkout>
git switch develop
git pull --ff-only
git status --short   # must be clean, or stop and ask
```

Verify the toolchain:

```bash
command -v uv node
nvm use               # reads .nvmrc -> Node 22
ls .venv/bin/python
```

### 1b. Check the running engine and active chats

The install restarts the engine, which cuts off anything running in it. Check
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
  `reachable: true` tells you nothing useful. Say so before installing: this
  skill repoints the plist, which switches the daily-driver service over to
  `develop`. Back the old plist up (`cp …/com.ciao.server.plist …plist.bak.devinstall`)
  so the release install is one command away.
- `active_chat_ids` — **non-empty means live chats/background agents are
  running.** Ask the user to let them finish (or confirm explicitly) before
  proceeding; the restart happens underneath them. `ciao service restart` and
  `ciao service stop` enforce the same gate with a `--force` escape hatch —
  never pass `--force` silently.
- `loaded`/`installed` — sanity check that the engine you are about to replace
  is the one `com.ciao.server.plist` manages.

If `.venv/bin/ciao` dies with `bad interpreter: …/some-other-repo/.venv/bin/python`,
the console script was generated before this checkout was renamed or moved. Step
3's `pip install -e` regenerates it, but step 1b needs the CLI *now*, so fall
back to the module entry point (`python -m ciao` does **not** work — `ciao` has
no `__main__`):

```bash
.venv/bin/python -c "from ciao.cli import main; main()" service status --json
```

Re-check right before the restart in step 4 too: a chat started during the long
build is just as interruptible.

### 2. Build the PWA

```bash
cd web && npm ci && npm run build
```

Output goes to `ciao/web/static/`, which ships as package data inside the
wheel. **Build the PWA before the install**, or the installed engine serves the
previous build's assets.

### 3. Install the current checkout

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
arguments), so the password and vault root survive the re-render.

### 4. Restart the service

Gate on active chats first, then restart:

```bash
tmp=$(mktemp)
.venv/bin/ciao service status --json > "$tmp"
python3 - "$tmp" <<'EOF'
import json, sys
data = json.loads(open(sys.argv[1]).read())["details"]
active = data.get("active_chat_ids")
if active:
    raise SystemExit(f"Active chats still running: {active}")
print("no active chats - clear to restart")
EOF
[ $? -eq 0 ] && .venv/bin/ciao service restart
```

`ciao service restart` reloads an edited plist (bootout and bootstrap) when the loaded definition differs from the one on disk, and kickstarts otherwise.

Two things bite here. `status=$(…)` is a **zsh read-only variable** and aborts the
whole snippet, so use a neutral name; and backslash-escaping quotes inside a
`python3 -c '…'` f-string does not survive the shell, so feed the script on
stdin instead. `raise SystemExit(msg)` prints to stderr and exits non-zero, which
is the gate you want.

Never pass `--force` silently; if the user explicitly asked to cut through, say
so out loud in the report.

### 5. Watch for issues

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
  the loop. `ciao setup --load-launchd` (step 3) bootstraps the plist, so the
  restart in step 4 legitimately produces **two** boots a few seconds apart —
  that is not a loop.
- **Import errors** at startup (`ModuleNotFoundError`, `ImportError`) — usually
  a `pip install` that ran before the PWA build, or an import resolving to a
  different `ciao` on the machine's `sys.path`.
- **Port already in use** / startup refused because another Ciaobot backend
  owns the runtime root.
- **PWA not reachable**: `curl -s http://127.0.0.1:8443/` should return HTML. If
  the engine answers but the PWA shows the recovery page, the static bundle
  behind the running process is stale — re-run step 2 and restart again.
  Confirm you are serving *this* build by diffing the asset hash the served
  `index.html` names against the one on disk:

  ```bash
  grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' ciao/web/static/index.html
  curl -s http://127.0.0.1:8443/ | grep -oE 'assets/index-[A-Za-z0-9_-]+\.js'
  ```
- **Version mismatch** — the served version should match the checkout:
  `.venv/bin/ciao --version` vs Settings → Home.

Sanity probes:

```bash
launchctl print "gui/$(id -u)/com.ciao.server" | grep -E "state|pid" | head
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8443/
(cd ~ && /path/to/ciaobot/.venv/bin/python -c "import ciao; print(ciao.__file__)")
```

### 6. Report

Summarize: built commit (`git rev-parse --short HEAD`), install type (editable
checkout), workspace preserved, engine and PWA status, and any log findings with
their source line numbers.

Also report what changed underneath the user: if the plist was pointing at
something other than this checkout, say plainly that the service moved from a
release install to `develop`, and where the old plist backup is.

If the install succeeded but a backend error appears, create a GitHub issue for
it (`gh issue create --repo raffaelefarinaro/ciaobot ...`) — but only for
genuine defects. Check the by-design log lines in the traps above first, and
prefer telling the user a finding is a workspace-config concern over filing
noise against a working engine.

## Notes and traps

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
  repeat step 2 then `pip install -e .` then restart.
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
  what is still running, rather than reporting a half-finished install as done.
