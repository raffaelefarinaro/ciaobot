#!/bin/sh
set -eu

# One-line installer for the Ciaobot engine alone (no Ciaobot.app), #562.
# Everything it installs is verified against the release minisign key
# embedded below before anything runs.

repo=raffaelefarinaro/ciaobot
release_base=${CIAO_RELEASE_BASE_URL:-https://github.com/$repo/releases/download}
release_base=${release_base%/}
UV_VERSION=0.12.17
PYTHON_VERSION=3.13
CRYPTOGRAPHY_PIN="cryptography==50.0.0"
# Same key as ciao/release_manifest.py RELEASE_PUBLIC_KEY (a test keeps them equal).
RELEASE_PUBLIC_KEY="RWSDUnIeQDnpmnNJiTjLmN6XOVFqgn1A0EXvTVG7AJIZXJxhyFN9osxm"
SERVER_LABEL=com.ciao.server
DESKTOP_LABEL=Ciaobot
SHIM_MARKER="# Ciaobot shim (managed by the Ciaobot installer)"
PLISTBUDDY=/usr/libexec/PlistBuddy
# Script variables, not environment variables: the test harness rewrites these
# four lines in its copy of the script, so a test run never talks to the real
# launchd, the running Ciaobot.app, or the operator's `pgrep`.
LAUNCHCTL=launchctl
OSASCRIPT=osascript
PGREP=pgrep
version=
workspace=
no_start=0
migrate=0
as_host=0
as_client=
desktop_live=0
# Filled in by classify_install, empty until then. `migrate_path` is the
# decision: host, client, or skip for an install nothing has to be migrated for.
migrate_kind=
migrate_workspace=
migrate_host_url=
migrate_path=skip
migration_dir="$HOME/.local/state/ciaobot/migration"
migration_started_at=

usage() {
    cat >&2 <<'USAGE'
Usage: install-engine.sh [--version VERSION] [--workspace DIRECTORY] [--no-start]
                         [--migrate [--as-host | --as-client URL]]

Installs the Ciaobot engine for the current user with uv; the PWA is its UI.

  --migrate            Move an engine Ciaobot.app currently manages to this
                       installer. The workspace, its .env and the runtime root
                       are left untouched: only the LaunchAgents, the shim, the
                       uv tool environment and ~/.local/state/ciaobot/ change.
                       Before-images are kept in
                       ~/.local/state/ciaobot/migration/before/, and a failure
                       after the install puts the old engine back.
  --as-host            With --migrate: treat this Mac as the host, even when its
                       node state cannot be read.
  --as-client URL      With --migrate: treat this Mac as a client of URL. Its
                       local engine is disabled and never replaced, so the Mac
                       does not become a second writer.
USAGE
}

fail() {
    echo "Ciaobot engine installer: $*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case $1 in
        --version)
            [ "$#" -ge 2 ] || fail "--version requires a value"
            version=$2
            shift 2
            ;;
        --workspace)
            [ "$#" -ge 2 ] || fail "--workspace requires a value"
            workspace=$2
            shift 2
            ;;
        --no-start) no_start=1; shift ;;
        --migrate) migrate=1; shift ;;
        --as-host) as_host=1; shift ;;
        --as-client)
            [ "$#" -ge 2 ] || fail "--as-client requires a URL"
            as_client=$2
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) usage; fail "unknown option: $1" ;;
    esac
done

# --as-host and --as-client answer a question only the migration asks, and
# they answer it in opposite directions: a URL is not a host, and a host is not
# a URL. Either without --migrate is a plain typo, not a silent no-op.
case "$as_client" in
    http://*|https://*) ;;
    '') ;;
    *) fail "--as-client takes an http:// or https:// URL" ;;
esac
if [ "$as_host" -ne 0 ] && [ -n "$as_client" ]; then
    fail "--as-host and --as-client are mutually exclusive"
fi
if [ "$migrate" -eq 0 ] && { [ "$as_host" -ne 0 ] || [ -n "$as_client" ]; }; then
    fail "--as-host and --as-client require --migrate"
fi

for command in curl shasum mktemp uname awk sw_vers; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done

[ "$(uname -s)" = "Darwin" ] || fail "this installer supports macOS; Linux servers use docs/LINUX.md"

major=$(sw_vers -productVersion | awk -F. '{print $1}')
case "$major" in
    ''|*[!0-9]*) fail "could not determine the macOS version" ;;
esac
[ "$major" -ge 13 ] || fail "the Ciaobot engine requires macOS 13 or newer"

case "$version" in
    '')
        [ -z "${CIAO_RELEASE_BASE_URL:-}" ] || fail "--version is required with CIAO_RELEASE_BASE_URL"
        latest_url=$(curl -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/$repo/releases/latest")
        version=${latest_url##*/}
        version=${version#v}
        case "$version" in
            ''|*[!0-9A-Za-z.-]*) fail "could not determine the latest release version" ;;
        esac
        ;;
    *) version=${version#v} ;;
esac
# The version reaches both a download URL and the manifest verifier, so reject
# anything that is not a plain release tag - in the v-prefixed spelling the
# release uses and in the plain one the rest of this script carries.
case "v$version" in
    *[!0-9A-Za-z.-]*) fail "invalid version: $version" ;;
esac
case "$version" in
    *[!0-9A-Za-z.-]*) fail "invalid version: $version" ;;
esac
base="$release_base/v$version"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/ciaobot-engine.XXXXXX")
chmod 700 "$tmp"
trap 'rm -rf "$tmp"' EXIT
# A signal handler that only cleaned up would fall through into the next step
# with the temp dir already deleted; exit runs the EXIT trap, so cleanup stays.
trap 'exit 130' HUP INT TERM

download() {
    curl -fsSL --retry 3 --connect-timeout 15 "$1" -o "$2"
}

find_uv() {
    # No `local` anywhere in this script: every function writes globals on
    # purpose, so the value found here is what the steps below use.
    if command -v uv >/dev/null 2>&1; then
        uv=$(command -v uv)
    elif [ -x "$HOME/.local/bin/uv" ]; then
        uv="$HOME/.local/bin/uv"
    else
        # Pinned version and official installer, and the caller's shell
        # profile is left alone: PATH changes are the user's call.
        download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-installer.sh" \
            "$tmp/uv-installer.sh" || fail "could not download the uv installer"
        UV_NO_MODIFY_PATH=1 UV_INSTALL_DIR="$HOME/.local/bin" sh "$tmp/uv-installer.sh" >/dev/null \
            || fail "could not install uv"
        uv="$HOME/.local/bin/uv"
    fi
    [ -x "$uv" ] || fail "uv is not available at $uv"
}

refuse_desktop_engine() {
    # Ciaobot.app owns the engine through a plist whose program arguments
    # point inside the bundle. Two launch agents must never fight over
    # com.ciao.server, so a live desktop engine is refused - unless --migrate
    # was asked for, which is the one path that takes the hand-over on (#576).
    plist="$HOME/Library/LaunchAgents/$SERVER_LABEL.plist"
    if [ -f "$plist" ] && [ -x "$PLISTBUDDY" ]; then
        program=$("$PLISTBUDDY" -c 'Print :ProgramArguments:0' "$plist" 2>/dev/null || true)
        case "$program" in
            *.app/*)
                if [ -e "$program" ]; then
                    desktop_live=1
                    if [ "$migrate" -eq 0 ]; then
                        fail "Ciaobot.app manages the engine on this Mac (#576); to move it to the terminal engine, re-run with --migrate"
                    fi
                fi
                ;;
        esac
    fi
    # The desktop installer writes that shim before onboarding creates the
    # plist, so in that window the plist check above sees nothing while a live
    # Ciaobot.app still owns the engine. A shim whose target is gone (the app
    # was deleted) is stale and this script may replace it.
    shim="$HOME/.local/bin/ciao"
    if [ -f "$shim" ] && grep -qF "$SHIM_MARKER" "$shim" 2>/dev/null; then
        shim_target=$(awk -F'"' '/^exec "/ {print $2; exit}' "$shim" 2>/dev/null || true)
        if [ -n "$shim_target" ] && [ -x "$shim_target" ]; then
            desktop_live=1
            if [ "$migrate" -eq 0 ]; then
                fail "Ciaobot.app manages the engine on this Mac (#576); to move it to the terminal engine, re-run with --migrate"
            fi
        fi
    fi
}

read_manifest() {
    # Verifies the manifest signature with the embedded key, then the schema,
    # the requested version and the single wheel entry, and prints that entry
    # as "<filename> <sha256> <size>". Nothing outside this heredoc is
    # trusted, and nothing from the manifest is ever executed.
    "$uv" run --quiet --no-project --python "$PYTHON_VERSION" --with "$CRYPTOGRAPHY_PIN" \
        python - "$tmp/ciaobot-engine-manifest.json" "$tmp/ciaobot-engine-manifest.json.sig" "$version" "$RELEASE_PUBLIC_KEY" <<'PY'
# --- embedded verifier (keep in sync with ciao/release_manifest.py) ---
import base64, hashlib, json, sys
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

manifest_path, sig_path, expected_version, public_key = sys.argv[1:5]
def die(msg):
    print(msg, file=sys.stderr); sys.exit(1)
raw_key = base64.b64decode(public_key.strip().splitlines()[-1], validate=True)
if len(raw_key) != 42 or raw_key[:2] != b"Ed": die("bad embedded public key")
key_id, key = raw_key[2:10], raw_key[10:]
text = open(sig_path, encoding="utf-8").read().strip()
if not text.startswith("untrusted comment:"):
    text = base64.b64decode(text, validate=True).decode("utf-8")
lines = [l.rstrip("\r") for l in text.splitlines() if l.strip()]
if len(lines) != 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith("trusted comment: "):
    die("malformed manifest signature")
blob = base64.b64decode(lines[1], validate=True)
if len(blob) != 74: die("malformed manifest signature")
alg, sig_key_id, signature = blob[:2], blob[2:10], blob[10:]
if sig_key_id != key_id: die("manifest signed by a different key")
data = open(manifest_path, "rb").read()
if alg == b"ED": message = hashlib.blake2b(data, digest_size=64).digest()
elif alg == b"Ed": message = data
else: die("unsupported signature algorithm")
trusted = lines[2][len("trusted comment: "):]
pk = Ed25519PublicKey.from_public_bytes(key)
try:
    pk.verify(signature, message)
    pk.verify(base64.b64decode(lines[3], validate=True), signature + trusted.encode("utf-8"))
except InvalidSignature:
    die("manifest signature does not match")
manifest = json.loads(data)
if manifest.get("schema") != 1 or manifest.get("version") != expected_version:
    die("manifest is not for version " + expected_version)
wheels = [a for a in manifest.get("artifacts", []) if a.get("kind") == "wheel"]
if len(wheels) != 1: die("manifest must list exactly one wheel")
w = wheels[0]
name, digest, size = str(w.get("filename", "")), str(w.get("sha256", "")), w.get("size")
if "/" in name or "\\" in name or not name.endswith(".whl") or len(digest) != 64 or not isinstance(size, int):
    die("manifest wheel entry is malformed")
print(name, digest, size)
PY
}

copy_file() {
    # `cp -p` where it exists, plain `cp` where it does not: the before-images
    # want their mode and timestamps kept, but a copy that preserves nothing is
    # still better than no backup at all.
    cp -p "$1" "$2" 2>/dev/null || cp "$1" "$2"
}

backup_before() {
    # The three things this migration replaces, copied aside before the first of
    # them is written. Nothing under the workspace or the runtime root is ever
    # copied or touched: the data is what the migration is preserving in place.
    mkdir -p "$migration_dir/before"
    chmod 700 "$migration_dir" "$migration_dir/before"
    before_server_plist=
    before_desktop_plist=
    before_shim=
    if [ -f "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" ]; then
        copy_file "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" \
            "$migration_dir/before/$SERVER_LABEL.plist"
        before_server_plist="$migration_dir/before/$SERVER_LABEL.plist"
    fi
    if [ -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" ]; then
        copy_file "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" \
            "$migration_dir/before/$DESKTOP_LABEL.plist"
        before_desktop_plist="$migration_dir/before/$DESKTOP_LABEL.plist"
    fi
    if [ -f "$HOME/.local/bin/ciao" ]; then
        copy_file "$HOME/.local/bin/ciao" "$migration_dir/before/ciao"
        before_shim="$migration_dir/before/ciao"
    fi
    migration_started_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
}

migration_receipt() {
    # Rewritten at every phase, so a migration that dies mid-flight says how far
    # it got. 0600 in a 0700 directory, written through a temp file so a reader
    # never sees half a receipt.
    migration_phase=$1
    migration_error=${2:-}
    "$uv" run --quiet --no-project --python "$PYTHON_VERSION" python -c '
import json, os, pathlib, sys, tempfile
phase, kind, workspace, host_url, error, started_at, path = sys.argv[1:8]
server_plist, desktop_plist, shim = sys.argv[8:11]
payload = {
    "schema": 1,
    "kind": kind,
    "phase": phase,
    "workspace": workspace,
    "host_url": host_url,
    "before": {
        "server_plist": server_plist,
        "desktop_plist": desktop_plist,
        "shim": shim,
    },
    "started_at": started_at,
}
if error:
    payload["error"] = error
target = pathlib.Path(path)
target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(target.parent, 0o700)
handle, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".receipt.")
with os.fdopen(handle, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, target)
' "$migration_phase" "$migrate_kind" "$migrate_workspace" "$migrate_host_url" \
        "$migration_error" "$migration_started_at" "$migration_dir/receipt.json" \
        "${before_server_plist:-}" "${before_desktop_plist:-}" "${before_shim:-}" \
        || fail "could not write the migration receipt"
}

migration_phase_of() {
    # The phase in an existing receipt, or "" when there is no receipt to read.
    # Read with grep/awk rather than a JSON parser because this runs before uv is
    # known to work on this Mac, and the `"phase": "…"` pair is picked out
    # wherever it sits: a receipt is a state file a hand edit or a later release
    # may have rewritten on one line, and missing it would re-run a migration
    # that already happened.
    [ -f "$migration_dir/receipt.json" ] || return 0
    grep -o '"phase"[[:space:]]*:[[:space:]]*"[^"]*"' "$migration_dir/receipt.json" \
        2>/dev/null | head -1 | awk -F'"' '{print $4}' || true
}

quit_desktop_app() {
    # The app owns the workspace's files, so it is asked to quit before
    # anything is replaced. A missing app, or one that ignores the request,
    # is not an error: the wait below is bounded either way.
    "$OSASCRIPT" -e 'tell application id "local.ciaobot.app" to quit' >/dev/null 2>&1 || true
    waited=0
    while [ "$waited" -lt 20 ] && "$PGREP" -x ciaobot-desktop >/dev/null 2>&1; do
        waited=$((waited + 1))
        sleep 1
    done
}

retire_desktop_agent() {
    # Only after the new engine has answered with its own version: the app's
    # agent is bootout *and* disabled, so a relaunch cannot bring it back
    # alongside the agent that replaced it. The plist is kept, next to its
    # before-image.
    "$LAUNCHCTL" bootout "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || true
    "$LAUNCHCTL" disable "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist"
}

rollback_host() {
    # The before-images go back, and the app's engine agent is loaded again, so
    # a Mac that handed its engine over gets it back even if the new engine
    # never came up. Best effort by design: a rollback that cannot restore
    # something must still restore everything else.
    if [ -n "${before_server_plist:-}" ] && [ -f "$before_server_plist" ]; then
        copy_file "$before_server_plist" "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" \
            || true
    fi
    if [ -n "${before_shim:-}" ] && [ -f "$before_shim" ]; then
        mkdir -p "$HOME/.local/bin"
        copy_file "$before_shim" "$HOME/.local/bin/ciao" || true
    fi
    "$LAUNCHCTL" bootout "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || true
    if [ -f "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" ]; then
        "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" \
            2>/dev/null || true
    fi
    "$LAUNCHCTL" kickstart -k "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || true
    migration_receipt rolled_back "$1"
}

install_step() {
    # One step of the install, with the failure message it reports. In a
    # migration the app's engine is already stopped by the time the first of
    # these runs, so a failure there puts it back before the script exits.
    step_error=$1
    shift
    "$@" || abort_install "$step_error"
}

abort_install() {
    if [ "$migrate_path" != skip ]; then
        rollback_host "$1"
        fail "migration failed and Ciaobot.app's engine was restored: $1"
    fi
    fail "$1"
}

classify_install() {
    # Reads the state through the *verified* wheel - the only code that has been
    # checked against the signed manifest at this point - and prints one JSON
    # object. It writes nothing: it runs before the first change this script
    # makes, and it has to be safe to run twice.
    classification=$("$uv" run --quiet --no-project --python "$PYTHON_VERSION" \
        --with "$wheel" python -I -m ciao.engine_migration classify --json \
        --launch-agents-dir "$HOME/Library/LaunchAgents") \
        || fail "could not classify this install"
    printf '%s' "$classification" | "$uv" run --quiet --no-project \
        --python "$PYTHON_VERSION" python -c '
import json, sys
state = json.load(sys.stdin)
print(state.get("kind", ""))
print(state.get("workspace", ""))
print(state.get("host_url", ""))
' > "$tmp/classification.txt" || fail "could not classify this install"
    # Read line by line rather than with `set --`: a workspace path with a space
    # in it is one value, not two words.
    field_number=0
    while IFS= read -r field || [ -n "$field" ]; do
        field_number=$((field_number + 1))
        case "$field_number" in
            1) migrate_kind=$field ;;
            2) migrate_workspace=$field ;;
            3) migrate_host_url=$field ;;
        esac
    done < "$tmp/classification.txt"
    case "$migrate_kind" in
        desktop_host)
            migrate_path=host
            ;;
        desktop_client)
            migrate_path=client
            ;;
        desktop_invalid)
            # Nobody can say what this Mac writes, so nothing is decided for
            # the user: the override is the decision, and it is recorded.
            if [ "$as_host" -ne 0 ]; then
                migrate_path=host
            elif [ -n "$as_client" ]; then
                migrate_path=client
                migrate_host_url=$as_client
            else
                fail "Ciaobot could not tell whether this Mac is the host or a client (node state unreadable). Re-run with --migrate --as-host, or --migrate --as-client https://your-host"
            fi
            ;;
        desktop_stale|engine|none)
            # Nothing live to migrate: a deleted app, an engine this script
            # already placed, or a Mac that never ran Ciaobot.app. --migrate is
            # a no-op and the ordinary install runs.
            migrate_path=skip
            ;;
        *)
            fail "could not classify this install: unknown kind: $migrate_kind"
            ;;
    esac
    if [ "$desktop_live" -ne 0 ] && [ "$migrate_path" = skip ]; then
        # A live Ciaobot.app was found before verification, and the state it
        # lives in says there is nothing to take over. Installing anyway would
        # be the refusal this script exists to avoid, so it stops here with
        # nothing changed and asks again.
        fail "Ciaobot.app is running but its engine state could not be read; re-run with --migrate --as-host, or --migrate --as-client https://your-host"
    fi
}

refuse_desktop_engine
find_uv

download "$base/ciaobot-engine-manifest.json" "$tmp/ciaobot-engine-manifest.json" \
    || fail "could not download the release manifest"
download "$base/ciaobot-engine-manifest.json.sig" "$tmp/ciaobot-engine-manifest.json.sig" \
    || fail "could not download the release manifest signature"

manifest_info=$(read_manifest) || fail "release manifest verification failed"
set -- $manifest_info
wheel_name=${1:-}
wheel_sha=${2:-}
wheel_size=${3:-}
if [ -z "$wheel_name" ] || [ -z "$wheel_sha" ] || [ -z "$wheel_size" ]; then
    fail "release manifest verification failed"
fi

wheel="$tmp/$wheel_name"
download "$base/$wheel_name" "$wheel" || fail "could not download $wheel_name"
actual_sha=$(shasum -a 256 "$wheel" | awk '{print $1}')
[ "$actual_sha" = "$wheel_sha" ] || fail "downloaded wheel does not match the signed manifest"
actual_size=$(wc -c < "$wheel" | tr -d ' ')
[ "$actual_size" = "$wheel_size" ] || fail "downloaded wheel does not match the signed manifest"

# Past this point the release bytes are known to be the ones the maintainer
# signed, and not one byte of this Mac has been touched yet. Everything below
# may change the install; everything above may not.
if [ "$migrate" -ne 0 ]; then
    previous_phase=$(migration_phase_of)
    case "$previous_phase" in
        migrated|migrated_client)
            echo "This Mac was already migrated ($previous_phase); nothing to do."
            exit 0
            ;;
    esac
    classify_install
    case "$migrate_path" in
        host)
            # Stop the app and keep a way back before the first change: from
            # here on the engine this Mac is running is the one being replaced.
            backup_before
            migration_receipt started
            quit_desktop_app
            ;;
        client)
            backup_before
            migration_receipt started
            quit_desktop_app
            # This Mac stops being a writer here. The server plist is left on
            # disk (it is backed up) but bootout *and* disable, so nothing
            # restarts it and no second engine ever comes up beside the host.
            "$LAUNCHCTL" bootout "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || true
            "$LAUNCHCTL" disable "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || true
            "$LAUNCHCTL" bootout "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || true
            "$LAUNCHCTL" disable "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || true
            rm -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist"
            ;;
    esac
fi

bin_dir=$("$uv" tool dir --bin)
tool_dir=$("$uv" tool dir)
target="$bin_dir/ciao"
if [ -e "$target" ] || [ -L "$target" ]; then
    # `ciao` is a common enough name to collide (Ciao Prolog ships one), and
    # clobbering someone else's program is not this script's call. Ours is
    # either a uv tool entry point or the shim the desktop installer wrote.
    if ! grep -qF "$SHIM_MARKER" "$target" 2>/dev/null &&
        ! "$uv" tool list 2>/dev/null | grep -q '^ciaobot '; then
        fail "$target exists and was not installed by Ciaobot; move it away and re-run"
    fi
fi

# Best effort: keep whatever the last install put there, so the engine can
# still answer "which release replaced me" after a runtime swap.
previous_version=
previous_executable=
receipt="$HOME/.local/state/ciaobot/install-receipt.json"
if [ -f "$receipt" ]; then
    previous_version=$(awk -F'"' '/^[[:space:]]*"version":/ {print $4; exit}' "$receipt" 2>/dev/null || true)
    previous_executable=$(awk -F'"' '/^[[:space:]]*"executable":/ {print $4; exit}' "$receipt" 2>/dev/null || true)
fi

install_step "uv tool install failed" \
    "$uv" tool install --force --python "$PYTHON_VERSION" "$wheel" >/dev/null
ciao="$bin_dir/ciao"
tool_python="$tool_dir/ciaobot/bin/python"
[ -x "$ciao" ] || abort_install "the installed ciao entry point is missing: $ciao"
[ -x "$tool_python" ] || abort_install "the installed engine interpreter is missing: $tool_python"

# Absolute paths only: the receipt is read by a process that has no idea which
# directory the installer ran from. A client is recorded as owning no service,
# because that is the whole point of its path: this Mac runs no engine.
receipt_backend=launchd
receipt_label=$SERVER_LABEL
if [ "$migrate_path" = client ]; then
    receipt_backend=none
    receipt_label=
fi
install_step "could not write the install receipt" \
    "$tool_python" -m ciao.install_receipt write \
    --version "$version" \
    --executable "$ciao" \
    --python "$tool_python" \
    --service-backend "$receipt_backend" \
    --service-label "$receipt_label" \
    --previous-version "$previous_version" \
    --previous-executable "$previous_executable" \
    >/dev/null

# A client stops here on purpose. It gets no workspace resolved, no setup, no
# service, no agent: the one thing it must not become is a second writer for a
# runtime root a host owns.
if [ "$migrate_path" != client ]; then
    # --yes turns off every guard in `ciao setup` (a LaunchAgent macOS TCC will
    # block, a health check that never comes up, a service silently re-pointed
    # at a different workspace), so it is passed only for the workspace this
    # script detected the engine already running in - the same rule install.sh
    # applies.
    setup_yes=
    if [ -z "$workspace" ]; then
        workspace=
        if [ "$migrate_path" = host ] && [ -n "$migrate_workspace" ]; then
            # Read out of the plist by the verified wheel: this is the workspace
            # the engine being replaced runs in, so it is the one the new engine
            # keeps. Nothing inside it is written.
            workspace=$migrate_workspace
            setup_yes=1
        fi
        if [ -z "$workspace" ] && [ -f "$plist" ] && [ -x "$PLISTBUDDY" ]; then
            existing=$("$PLISTBUDDY" -c 'Print :WorkingDirectory' "$plist" 2>/dev/null || true)
            if [ -n "$existing" ] && [ -d "$existing" ] && [ -f "$existing/.env" ]; then
                workspace=$existing
                setup_yes=1
            fi
        fi
        [ -n "$workspace" ] || workspace="$HOME/Ciaobot"
    fi
    mkdir -p "$workspace"
    # CDPATH would send `cd` looking for a matching directory elsewhere and print
    # the path it found, so it is cleared for this one command.
    workspace=$(CDPATH= cd -- "$workspace" && pwd -P)

    # Idempotent, and it preserves an existing .env and its password. stderr is
    # left visible so setup's own guard message reaches the user.
    set -- --workspace "$workspace" --python "$ciao"
    [ -z "$setup_yes" ] || set -- "$@" --yes
    # Reload the agent so launchd drops any job definition it already holds
    # (another engine's), otherwise `service start` restarts the old one.
    [ "$no_start" -ne 0 ] || set -- "$@" --load-launchd
    install_step "ciao setup failed" "$ciao" setup "$@" >/dev/null
fi

if [ "$no_start" -eq 0 ] && [ "$migrate_path" != client ]; then
    install_step "could not start the engine; try: $ciao service status" \
        "$ciao" service start --workspace "$workspace" --json >/dev/null
    port=$(awk -F= '/^PWA_PORT=/{print $2}' "$workspace/.env" 2>/dev/null | tr -d '"' | tail -1)
    [ -n "$port" ] || port=8443
    # A slow first boot is not a failed install: the plist is written, so say
    # so instead of failing an install that did everything it promised. A
    # migration is the exception - the app's own agent is about to be retired,
    # and only an engine answering with the version just installed proves the
    # hand-over worked.
    health_attempts=60
    if [ "$migrate_path" = host ]; then
        health_attempts=90
    fi
    attempt=0
    healthy=0
    while [ "$attempt" -lt "$health_attempts" ]; do
        status=$(curl -fsS "http://localhost:$port/api/startup-status" 2>/dev/null || true)
        if [ -n "$status" ]; then
            if [ "$migrate_path" != host ]; then
                healthy=1
                break
            fi
            reported=$(printf '%s\n' "$status" | awk -F'"' '/"version"/{print $4; exit}')
            if [ "$reported" = "$version" ]; then
                healthy=1
                break
            fi
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    if [ "$healthy" -eq 0 ]; then
        if [ "$migrate_path" = host ]; then
            abort_install "the new engine did not answer on http://localhost:$port within ${health_attempts}s; check: $ciao service status"
        fi
        echo "Ciaobot engine installer: the engine is still starting; check: ciao service status" >&2
    fi
fi

if [ "$migrate_path" = client ]; then
    migration_receipt migrated_client
    echo "Ciaobot engine $version installed. This Mac runs no engine."
    echo "  ciao: $ciao"
    echo "This Mac was a client of $migrate_host_url. Open that address in your browser and sign in there; this Mac no longer runs its own engine."
    if [ -t 1 ] && command -v open >/dev/null 2>&1; then
        open "$migrate_host_url" >/dev/null 2>&1 || true
    fi
    exit 0
fi

if [ "$migrate_path" = host ]; then
    if [ "$no_start" -eq 0 ]; then
        # The new engine answered with this version, so the app's own agent is
        # retired now and not a second earlier. The bundle stays: the user
        # removes it when they are ready.
        retire_desktop_agent
        migration_receipt migrated
    else
        # --no-start promised no service, and an engine that was never started
        # cannot retire anything. The receipt says so; re-running without
        # --no-start is what completes the hand-over.
        migration_receipt installed_no_start
    fi
fi

echo "Ciaobot engine $version installed."
echo "  ciao:      $ciao"
echo "  workspace: $workspace"
url=
if [ "$no_start" -eq 0 ]; then
    # Printed to the terminal and nowhere else: the URL is a one-time
    # credential that signs the person at this keyboard in. A failure must not
    # reach the user as "Open Ciaobot: " with nothing after it.
    url_output=$("$ciao" setup-url --workspace "$workspace") \
        || fail "could not create the sign-in link; run: $ciao setup-url --workspace $workspace"
    url=$(printf '%s\n' "$url_output" | tail -1)
    case "$url" in
        http://*) ;;
        *) fail "could not create the sign-in link; run: $ciao setup-url --workspace $workspace" ;;
    esac
    echo "Open Ciaobot: $url"
    echo "This link signs you in once. Do not share it."
fi
# Only once the app's own agent is retired: while it is still loaded Ciaobot.app
# is not "no longer needed". The bundle itself is never deleted here.
if [ "$migrate_path" = host ] && [ "$no_start" -eq 0 ]; then
    echo "Ciaobot.app is no longer needed; remove it with: ciao desktop uninstall"
fi
case ":${PATH:-}:" in
    *":$bin_dir:"*) ;;
    *) echo "Add ciao to your PATH: $uv tool update-shell" ;;
esac
if [ -t 1 ] && [ "$no_start" -eq 0 ] && command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
fi
