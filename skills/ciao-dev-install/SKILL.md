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
- `reachable` — is the currently installed engine serving?
- `active_chat_ids` — **non-empty means live chats/background agents are
  running.** Ask the user to let them finish (or confirm explicitly) before
  proceeding; the restart happens underneath them. `ciao service restart` and
  `ciao service stop` enforce the same gate with a `--force` escape hatch —
  never pass `--force` silently.
- `loaded`/`installed` — sanity check that the engine you are about to replace
  is the one `com.ciao.server.plist` manages.

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
checkout, not to some other copy on the machine:

```bash
.venv/bin/python -c "import ciao, pathlib; print(ciao.__version__, pathlib.Path(ciao.__file__).resolve())"
```

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
status=$(.venv/bin/ciao service status --json)
echo "$status" | python3 -c '
import json, sys
data = json.load(sys.stdin)["details"]
if data.get("active_chat_ids"):
    print(f"Active chats still running: {data[\"active_chat_ids\"]}", file=sys.stderr)
    raise SystemExit(1)
'
.venv/bin/ciao service restart
```

Never pass `--force` silently; if the user explicitly asked to cut through, say
so out loud in the report.

### 5. Watch for issues

The engine logs live in the workspace:

```bash
tail -f "$workspace/.runtime/ciao.stdout.log" "$workspace/.runtime/ciao.stderr.log"
```

Watch for at least a minute. Issues to flag to the user:

- **Engine crash loop** — the engine exits shortly after starting, repeatedly
  (launchd `KeepAlive` restarts it). Look for tracebacks in `ciao.stderr.log`.
- **Import errors** at startup (`ModuleNotFoundError`, `ImportError`) — usually
  a `pip install` that ran before the PWA build, or an import resolving to a
  different `ciao` on the machine's `sys.path`.
- **Port already in use** / startup refused because another Ciaobot backend
  owns the runtime root.
- **PWA not reachable**: `curl -s http://127.0.0.1:8443/` should return HTML. If
  the engine answers but the PWA shows the recovery page, the static bundle
  behind the running process is stale — re-run step 2 and restart again.
- **Version mismatch** — the served version should match the checkout:
  `.venv/bin/ciao --version` vs Settings → Home.

Sanity probes:

```bash
launchctl print "gui/$(id -u)/com.ciao.server" | grep -E "state|path" | head
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8443/
.venv/bin/python -c "import ciao; print(ciao.__file__)"   # must be the checkout
```

### 6. Report

Summarize: built commit (`git rev-parse --short HEAD`), install type (editable
checkout), workspace preserved, engine and PWA status, and any log findings with
their source line numbers. If the install succeeded but a backend error
appears, create a GitHub issue for it
(`gh issue create --repo raffaelefarinaro/ciaobot ...`).

## Notes and traps

- **A running process is not a working engine.** Check the log for *this* boot
  only, count `Uvicorn running on` to rule out a crash loop, and confirm the PWA
  answers. An auth-required install returns `unauthorized` from
  `/api/startup` — that is a pass, not a failure.
- **Build the PWA last-to-ship, not last overall.** The editable install copies
  package data (`ciao/web/static/`) at install time, so an install that ran
  before `npm run build` bakes in the previous build's assets. When in doubt,
  repeat step 2 then `pip install -e .` then restart.
- **The editable install is the source of truth, not `PATH`.** Two `ciao` on one
  machine is normal in a contributor setup; always drive this skill with
  `.venv/bin/…` so you are talking to the checkout you just built.
- **Never restart from inside a PWA chat.** Apply the change and ask the
  operator to hit Deploy or Restart in Settings.
- **`pytest tests/` is not a smoke test.** The suite mocks the provider, the
  launchd surface and the clock. This skill is what proves the engine boots
  against a real workspace.
- If a step fails, leave the machine in a known state: say which step failed and
  what is still running, rather than reporting a half-finished install as done.
