---
name: ciao-dev-install
description: Build the current develop checkout into a self-contained Ciaobot.app and install it on this machine for testing, then watch logs for issues. Trigger on "install develop", "test the develop build", "install the current dev version", "build and install ciaobot", "install dev build", or asking to test new changes locally instead of via the release installer.
---

# Ciaobot develop install

> Contributor/project skill — lives in the repo's workspace `skills/` folder, **not** `ciao/stock/skills/`. It is for people working *on* Ciaobot and is deliberately not packaged or shipped to end-user installs. `ciao sync-skills` mirrors it into the runtime-discovered `.claude/skills/` and `.agents/skills/` catalogs. Don't move it into `ciao/stock/`.

Replaces the end-user one-liner (`curl -fsSL .../install.sh | sh`) with the same outcome built from the **local `develop` checkout**: a self-contained `Ciaobot.app` with the current `ciao/` backend and PWA embedded, installed on this machine, preserving the existing workspace and password. Then watch the engine logs and report anything broken.

Two things make this heavy but faithful: the embedded runtime is rebuilt (it pip-installs the current `ciao/` code and PWA static assets), and the Tauri app is rebuilt from `desktop/`. Expect 10–20+ minutes on a laptop, dominated by the runtime build's two Python downloads and dependency installs.

## Before you start

- The checkout must be on `develop`, clean, and up to date.
- Toolchain must be present: Node 22 (`.nvmrc`), cargo (Rust 1.90.0 via rustup), `swiftc`, `uv`, and a repo `.venv`. If any is missing, install it before building.
- The installed app currently lives at `~/Applications/Ciaobot.app` (`CIAO_APP_DIR`); the workspace and password live in the existing `com.ciao.server.plist` and are preserved.
- Ask the user before installing if the build or install steps would overwrite something unexpected (e.g. a release install they still need), and always confirm the swap because it replaces the running app.

## Steps

### 1. Preflight

```bash
cd /Users/raffaelefarinaro/repos/ciaobot
git switch develop
git pull --ff-only
git status --short   # must be clean, or stop and ask
```

Verify the toolchain:

```bash
command -v cargo swiftc uv node
nvm use               # reads .nvmrc -> Node 22
ls .venv/bin/python
```

### 1b. Check the running engine and active chats

The install boots the engine out mid-swap, which would cut off anything running in it. Check before building:

```bash
.venv/bin/ciao desktop-service status --json
```

From the JSON, note:
- `reachable` — is the currently installed engine serving?
- `active_chat_ids` — **non-empty means live chats/background agents are running.** Ask the user to let them finish (or confirm explicitly) before proceeding; the swap restarts the engine underneath them. `restart`/`stop` in `macos_service.py` enforce the same gate with a `--force` escape hatch — never pass `--force` silently.
- `loaded`/`installed` — sanity check that the engine the skill is about to replace is the one `com.ciao.server.plist` manages.

Re-check right before the swap in step 5 too: a chat started during the long build is just as interruptible.

### 2. Build the PWA

```bash
cd web && npm ci && npm run build
```

Output goes to `ciao/web/static/`, which the runtime build pip-installs as package data. **The PWA build must finish before the runtime build.** If the PWA build fails, stop — a broken static bundle would ship into the installed app.

### 3. Build the embedded runtime

```bash
set -a                # export the pinned vars; the env file does not export them itself
. scripts/pinned-python-runtime.env
set +a
./scripts/build-bundled-runtime.sh desktop/runtime
```

The env file only *sets* variables, it doesn't export them, and the build script runs as a child process — without `set -a` (or explicit exports) it dies at the `CIAO_PYTHON_ARM64_URL is required` guard. (CI avoids this by passing the values via job `env:` instead.)

This downloads both python-build-standalone archives, verifies their SHA-256 against the pinned env, and pip-installs the current repo (backend + PWA assets) into both arch runtimes under `desktop/runtime/`. It is the expensive step; it is also the step that makes the install test the **current** `ciao/` code. It is only skippable when the change being tested touches `desktop/` alone — ask the user before skipping, because a backend or PWA change will not reach the installed app without it.

### 4. Build the desktop app

```bash
cd desktop
npm ci
npm run tauri build -- --bundles app --config '{"bundle":{"createUpdaterArtifacts":false}}'
```

- `--bundles app` skips the DMG; disabling updater artifacts drops the signing requirement (a dev machine has no `TAURI_SIGNING_PRIVATE_KEY`).
- The `pretauri` hook compiles the Swift voice sidecar automatically.
- Native-arch only (like `ciao/desktop_build.py`'s dev builds), matching the aarch64-only release target.
- Verify the result: `desktop/src-tauri/target/release/bundle/macos/Ciaobot.app` must exist with a runnable `Contents/MacOS/ciaobot-desktop` and `Contents/Resources/ciao-runtime/bin/ciao`.

### 5. Install (staged swap, preserving the workspace)

The install must never delete the running bundle first. Follow `scripts/install.sh`'s choreography: quit the app, boot out the agents, wait for the executable to disappear, then rename the old bundle aside and move the new one in. After the swap, re-render `com.ciao.server.plist` through the new engine's `ciao setup` so the plist names the new runtime paths, write the desktop `Ciaobot.plist`, then bootstrap and kickstart both agents.

```bash
app_dir="$HOME/Applications"
bundle="desktop/src-tauri/target/release/bundle/macos/Ciaobot.app"
uid=$(id -u)

# Gate: refuse to cut the engine loose while chats are active. Same contract
# as `desktop-service stop/restart` (macos_service.py) — never --force silently.
status=$(.venv/bin/ciao desktop-service status --json)
echo "$status" | python3 -c '
import json, sys
data = json.load(sys.stdin)["details"]
if data.get("active_chat_ids"):
    print(f"Active chats still running: {data[\"active_chat_ids\"]}", file=sys.stderr)
    raise SystemExit(1)
'
```

Then the swap, which must never delete the running bundle first (follow `scripts/install.sh`'s choreography: quit the app, boot out the agents, wait for the executable to disappear, rename the old bundle aside, move the new one in):

```bash
osascript -e 'tell application id "local.ciaobot.app" to quit' >/dev/null 2>&1 || true
launchctl bootout "gui/$uid/Ciaobot" >/dev/null 2>&1 || true
launchctl bootout "gui/$uid/com.ciao.server" >/dev/null 2>&1 || true
attempts=0
while pgrep -x ciaobot-desktop >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  [ "$attempts" -lt 30 ] || { echo "Ciaobot is still running; quit it and retry" >&2; exit 1; }
  sleep 1
done

rm -rf "$app_dir/.Ciaobot.app.previous"
mv "$app_dir/Ciaobot.app" "$app_dir/.Ciaobot.app.previous"
ditto --noextattr "$bundle" "$app_dir/Ciaobot.app" \
  || { echo "copy of the new bundle failed; restoring the previous one" >&2
       rm -rf "$app_dir/Ciaobot.app"
       mv "$app_dir/.Ciaobot.app.previous" "$app_dir/Ciaobot.app"
       exit 1; }
rm -rf "$app_dir/.Ciaobot.app.previous"

engine="$app_dir/Ciaobot.app/Contents/Resources/ciao-runtime/bin/ciao"
"$engine" --help >/dev/null || { echo "new runtime self-check failed" >&2; exit 1; }
```

Then re-render the engine plist against the existing workspace (read it from the previous plist, like `install.sh`):

```bash
workspace=$(/usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' \
  "$HOME/Library/LaunchAgents/com.ciao.server.plist" 2>/dev/null || true)
[ -n "$workspace" ] && [ -d "$workspace" ] && [ -f "$workspace/.env" ] \
  || { echo "could not recover the workspace from the existing plist" >&2; exit 1; }

"$engine" setup --workspace "$workspace" --python "$engine" --yes --load-launchd >/dev/null
```

Write the desktop LaunchAgent (same shape as `install.sh`), then load both agents:

```bash
desktop_executable="$app_dir/Ciaobot.app/Contents/MacOS/ciaobot-desktop"
desktop_plist="$HOME/Library/LaunchAgents/Ciaobot.plist"
cat > "$desktop_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>Ciaobot</string>
<key>ProgramArguments</key><array>
<string>$desktop_executable</string>
<string>--background</string></array>
<key>RunAtLoad</key><true/>
</dict></plist>
EOF

launchctl bootstrap "gui/$uid" "$desktop_plist"
launchctl kickstart "gui/$uid/Ciaobot"
```

The `ciao setup --load-launchd` above already unloaded/loaded `com.ciao.server`; kick it to be sure:

```bash
launchctl kickstart "gui/$uid/com.ciao.server"
```

### 6. Watch for issues

The engine logs live in the workspace:

```bash
tail -f "$workspace/.runtime/ciao.stdout.log" "$workspace/.runtime/ciao.stderr.log"
```

Watch for at least a minute. Issues to flag to the user:

- **Engine crash loop** — the engine exits shortly after starting, repeatedly (launchd `KeepAlive` restarts it). Look for tracebacks in `ciao.stderr.log`.
- **Import errors** at startup (`ModuleNotFoundError`, `ImportError`) — usually a runtime build that pip-installed before the PWA build, or a stale `desktop/runtime` when `--skip-runtime` was used.
- **Port already in use** / startup refused because another Ciaobot backend owns the runtime root.
- **PWA not reachable**: `curl -s http://127.0.0.1:8443/` should return HTML. If the desktop app is up but the PWA shows the recovery page, the engine is not serving.
- **Version mismatch** — the served version should match the checkout: `"$engine" --version` vs the installed app's Settings → Home.

Sanity probes:

```bash
launchctl print "gui/$uid/com.ciao.server" | grep -E "state|path" | head
pgrep -f "Ciaobot.app/Contents/MacOS/ciaobot-desktop"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8443/
```

### 7. Report

Summarize: built commit (`git rev-parse --short HEAD`), install path, workspace preserved, engine and PWA status, and any log findings with their source line numbers. If the install succeeded but a backend error appears, create a GitHub issue for it (`gh issue create --repo raffaelefarinaro/ciaobot ...`).

## Notes and traps

- **Never `rm -rf` the installed app before the new one is in place** — use the rename-aside swap; a failed delete can leave a gutted `Ciaobot.app`.
- **`ditto` needs `--noextattr`** — files in a Tauri bundle carry a `com.apple.provenance` xattr that plain `ditto` cannot re-set, so the copy fails with "Operation not permitted" on hundreds of files (including `Contents/Info.plist`), leaving a half-copied, unlaunchable bundle while the script goes on to delete the `.previous` backup. The snippet above both skips xattrs and checks the copy before removing the backup; if it ever fails mid-swap, the previous bundle is at `$app_dir/.Ciaobot.app.previous` — restore it by renaming it back.
- **`tauri-plugin-single-instance` makes `open` focus the running instance** — the quit must happen before the swap, or the new binary never launches.
- **Freshness checks in `ciao/desktop_build.py` do not apply here** — this skill builds unconditionally (it is the point). For shell-only tweaks under `desktop/`, Settings → Restart with `CIAO_DEV_MODE=true` rebuilds just the shell instead; say so when relevant.
- **The bundle's engine resolution is pinned** (`desktop/src-tauri/src/service.rs`): it prefers `Contents/Resources/ciao-runtime/bin/ciao` over everything else, so a `PATH` `ciao` can never shadow the installed engine — no extra config needed after the swap.
- **Backend and PWA changes both require the runtime rebuild** — `ciao.web` static assets ship as package data inside the runtime site-packages. Only a `desktop/`-only change may skip step 3.
- **`ciao setup` preserves the existing `.env`** (its variables win over setup arguments), so the password and vault root survive the re-render.
- If a step fails, do not leave the machine half-installed: the previous bundle is recoverable from `$app_dir/.Ciaobot.app.previous` only if the install stopped before that cleanup — restore it by renaming it back.
