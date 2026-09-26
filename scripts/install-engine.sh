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
# lines in its copy of the script, so a test run never talks to the real
# launchd, the running Ciaobot.app, the operator's `pgrep`, a real `sleep`, or a
# real engine answering the health check on some port of this Mac.
LAUNCHCTL=launchctl
OSASCRIPT=osascript
PGREP=pgrep
CURL=curl
SLEEP=sleep
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
install_receipt="$HOME/.local/state/ciaobot/install-receipt.json"
migration_started_at=
# Set once this run has started a migration transaction, so a signal handler can
# tell "nothing has been touched yet" from "this stopped halfway".
migration_active=0
# Set when this transaction has taken the app's own LaunchAgent out of launchd,
# which is not the same fact as having removed its plist: an agent that was
# booted out and never loaded again is an unloaded one, and an enabled label with
# no job behind it never starts. A rollback reloads and starts that agent
# whenever this is 1, and re-enables it alone when it is 0.
desktop_bootout_done=0
# Recorded in the migration receipt once the transaction has begun retiring the
# app's own agent, so a run that stops in the middle of that cannot be resumed
# as though launchd still had the job.
retiring_desktop=0
# The before-images, as paths in the migration receipt. An empty one is a fact
# about this Mac ("there was nothing here"), not a missing variable: a rollback
# acts on the difference instead of inventing a file.
before_server_plist=
before_desktop_plist=
before_shim=
before_install_receipt=
before_tool_env=
# The last validated read of the migration receipt, and why it was rejected.
receipt_valid=0
receipt_phase=
receipt_reason=none
receipt_kind=
receipt_installed_version=
receipt_started_at=
receipt_workspace=
receipt_retiring_desktop=0
receipt_before_server_plist=
receipt_before_desktop_plist=
receipt_before_shim=
receipt_before_install_receipt=
receipt_before_tool_env=
# Set when the receipt says a host migration stopped while it was taking the
# app's own agent out of launchd. `ciao setup` has already repointed the engine
# plist at the new engine by then, so the classifier reads that Mac as an
# ordinary installer-managed install; it is not one, and this is the flag that
# says so.
resume_retiring=0
# Why a settled receipt is not believed, when it names a phase this script
# settles but the install on this Mac does not match it.
settled_mismatch=

usage() {
    cat >&2 <<'USAGE'
Usage: install-engine.sh [--version VERSION] [--workspace DIRECTORY] [--no-start]
                         [--migrate [--as-host | --as-client URL]]

Installs the Ciaobot engine for the current user with uv; the PWA is its UI.

  --migrate            Move an engine Ciaobot.app currently manages to this
                       installer. The workspace, its .env and the runtime root
                       are left untouched: only the LaunchAgents, the shim, the
                       uv tool environment and ~/.local/state/ciaobot/ change.
                       Before-images of all of those are kept in
                       ~/.local/state/ciaobot/migration/before/, and any failure
                       after the install puts the old engine back. If Ciaobot.app
                       is still running 20s after it is asked to quit, nothing
                       is changed at all.
  --as-host            With --migrate: treat this Mac as the host, even when its
                       node state cannot be read or trusted.
  --as-client URL      With --migrate: treat this Mac as a client of URL, which
                       has to be an http:// or https:// address with a host name.
                       Its local engine is disabled and never replaced, so the
                       Mac does not become a second writer.
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
# a URL. Either without --migrate is a plain typo, not a silent no-op. The
# scheme check here is a fast answer for an obvious typo, before anything is
# downloaded; whether the value is an *address* is asked of the verified wheel
# later, in validate_client_url, before anything on this Mac is touched.
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
        latest_url=$("$CURL" -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/$repo/releases/latest")
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
trap on_signal HUP INT TERM

download() {
    "$CURL" -fsSL --retry 3 --connect-timeout 15 "$1" -o "$2"
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

snapshot_file() {
    # <source> <name>: copy `source` into the immutable before-image directory
    # and print the path recorded for it - or print nothing, meaning "this was
    # not here". Absence is written down rather than left out, so a rollback
    # knows the difference between "restore this" and "take away what the
    # migration created", and never invents a file the user never had.
    [ -f "$1" ] || return 0
    copy_file "$1" "$migration_dir/before/$2" || return 1
    printf '%s\n' "$migration_dir/before/$2"
}

snapshot_tool_env() {
    # The uv tool environment a forced reinstall is about to replace, or
    # nothing at all when there is none - which is the common hand-over from
    # Ciaobot.app, since the app does not install the engine as a uv tool. A
    # rollback then removes the tool environment this transaction created
    # instead of restoring one. Copying a real engine environment is expensive,
    # so it is only copied when one is already there.
    [ -n "$tool_dir" ] || return 0
    [ -d "$tool_dir/ciaobot" ] || return 0
    rm -rf "$migration_dir/before/tool-env"
    cp -pR "$tool_dir/ciaobot" "$migration_dir/before/tool-env" || return 1
    printf '%s\n' "$migration_dir/before/tool-env"
}

backup_before() {
    # Everything this migration replaces, copied aside before the first of them
    # is written, and kept for the whole transaction: the originals are the only
    # way back to the engine that is running now. Nothing under the workspace or
    # the runtime root is ever copied or touched: that data is what the
    # migration is preserving in place.
    mkdir -p "$migration_dir/before"
    chmod 700 "$migration_dir" "$migration_dir/before"
    if [ "$receipt_valid" -ne 0 ]; then
        # A receipt that parsed, whose schema is this script's and whose
        # before-images are all still on disk describes a transaction already in
        # progress. Its snapshots *are* the originals, so they are reused as
        # they are: re-taking them now would copy the uv entry point and the
        # rewritten plist over the desktop shim, and after that a rollback
        # could not bring the app's engine back.
        before_server_plist=$receipt_before_server_plist
        before_desktop_plist=$receipt_before_desktop_plist
        before_shim=$receipt_before_shim
        before_install_receipt=$receipt_before_install_receipt
        before_tool_env=$receipt_before_tool_env
        migration_started_at=$receipt_started_at
        return 0
    fi
    before_server_plist=$(snapshot_file \
        "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" "$SERVER_LABEL.plist") \
        || fail "could not copy $migration_dir/before/$SERVER_LABEL.plist aside; nothing has been changed"
    before_desktop_plist=$(snapshot_file \
        "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" "$DESKTOP_LABEL.plist") \
        || fail "could not copy $migration_dir/before/$DESKTOP_LABEL.plist aside; nothing has been changed"
    before_shim=$(snapshot_file "$HOME/.local/bin/ciao" ciao) \
        || fail "could not copy $migration_dir/before/ciao aside; nothing has been changed"
    before_install_receipt=$(snapshot_file "$install_receipt" install-receipt.json) \
        || fail "could not copy $migration_dir/before/install-receipt.json aside; nothing has been changed"
    before_tool_env=$(snapshot_tool_env) \
        || fail "could not copy $migration_dir/before/tool-env aside; nothing has been changed"
    migration_started_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
}

migration_receipt() {
    # Rewritten at every phase, so a migration that dies mid-flight says how far
    # it got. 0600 in a 0700 directory, written through a temp file so a reader
    # never sees half a receipt. Returns non-zero instead of exiting: a receipt
    # written after the tool is installed is a post-install failure like any
    # other, and the caller has to roll back rather than call `fail` from here.
    migration_phase=$1
    migration_error=${2:-}
    "$uv" run --quiet --no-project --python "$PYTHON_VERSION" python -c '
# migration-receipt-write: writes ~/.local/state/ciaobot/migration/receipt.json
import json, os, pathlib, sys, tempfile
phase, kind, workspace, host_url, error, started_at, path, version = sys.argv[1:9]
server_plist, desktop_plist, shim, install_receipt, tool_env = sys.argv[9:14]
retiring_desktop = sys.argv[14]
payload = {
    "schema": 1,
    "kind": kind,
    "phase": phase,
    "workspace": workspace,
    "host_url": host_url,
    "version": version,
    # Whether the app agent has been, or is being, taken out of launchd. A
    # rollback that stopped caring about this leaves a Mac whose desktop engine
    # never starts again while it reports the engine restored.
    "retiring_desktop": retiring_desktop == "1",
    "before": {
        "server_plist": server_plist,
        "desktop_plist": desktop_plist,
        "shim": shim,
        "install_receipt": install_receipt,
        "tool_env": tool_env,
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
        "$version" \
        "${before_server_plist:-}" "${before_desktop_plist:-}" "${before_shim:-}" \
        "${before_install_receipt:-}" "${before_tool_env:-}" "$retiring_desktop"
}

load_migration_receipt() {
    # The existing migration receipt, read as a whole and checked field by
    # field. `receipt_valid` is 1 only when the file parsed, the schema is this
    # script's, the phase is one this script writes, and all five before-images
    # it has to carry are recorded, are the ones this installer writes and are
    # still on disk: anything less is a state to refuse rather than a state to
    # resume from. The old `grep '"phase":' ` answer is not good enough here - it
    # is satisfied by a truncated file, and a truncated file on an unmigrated Mac
    # reads as "already done".
    receipt_valid=0
    receipt_phase=
    receipt_reason=none
    receipt_kind=
    receipt_installed_version=
    receipt_started_at=
    receipt_workspace=
    receipt_retiring_desktop=0
    receipt_before_server_plist=
    receipt_before_desktop_plist=
    receipt_before_shim=
    receipt_before_install_receipt=
    receipt_before_tool_env=
    resume_retiring=0
    [ -f "$migration_dir/receipt.json" ] || return 0
    if ! "$uv" run --quiet --no-project --python "$PYTHON_VERSION" python -c '
# migration-receipt-read: validates an existing receipt.json, one field per line
import json
import pathlib
import sys

# Every phase this script writes. A receipt naming anything else was written by
# something that is not this installer, and its before-images mean nothing here.
PHASES = (
    "started",
    "installed_no_start",
    "retiring",
    "migrated",
    "migrated_client",
    "rolled_back",
    "interrupted",
)
IMAGES = {
    "server_plist": "com.ciao.server.plist",
    "desktop_plist": "Ciaobot.plist",
    "shim": "ciao",
    "install_receipt": "install-receipt.json",
    "tool_env": "tool-env",
}

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    print("reason=unreadable")
    raise SystemExit(0)
if not isinstance(data, dict) or data.get("schema") != 1:
    print("reason=schema")
    raise SystemExit(0)
phase, kind = data.get("phase"), data.get("kind")
if not isinstance(phase, str) or phase not in PHASES or not isinstance(kind, str) or not kind:
    print("reason=phase")
    raise SystemExit(0)
before = data.get("before")
if not isinstance(before, dict):
    print("reason=before")
    raise SystemExit(0)


def image(name):
    # A key that is not there at all is not a record of "this Mac had no such
    # file": it is a receipt that does not say what it replaced, and a rollback
    # that read the omission as absence deletes a file the user had before the
    # migration started. So all five keys have to be present, they have to be
    # strings, and a path has to be the before-image this installer writes under
    # that name - and still be on disk, since a snapshot that is gone cannot roll
    # anything back and must not be reported as one that can.
    if name not in before or not isinstance(before[name], str):
        raise KeyError(name)
    value = before[name]
    if value == "":
        return ""
    path = pathlib.Path(value)
    if path.parent != pathlib.Path(sys.argv[2]) or path.name != IMAGES[name]:
        raise ValueError(name)
    # A tool environment is a directory and the rest are files, so this is the
    # one shape both of them pass.
    if not path.exists():
        raise ValueError(name)
    return value


try:
    images = {name: image(name) for name in IMAGES}
except KeyError:
    # An incomplete receipt is not a state to resume from and not one to read
    # as an absence either: there is nothing here to roll back *to*.
    print("reason=incomplete-image")
    raise SystemExit(0)
except ValueError:
    print("reason=missing-image")
    raise SystemExit(0)
print("valid=1")
print("phase=" + phase)
print("kind=" + kind)
print("version=" + str(data.get("version") or ""))
print("started_at=" + str(data.get("started_at") or ""))
print("workspace=" + str(data.get("workspace") or ""))
print("retiring_desktop=" + ("1" if data.get("retiring_desktop") is True else "0"))
for name in IMAGES:
    print("before_" + name + "=" + images[name])
' "$migration_dir/receipt.json" "$migration_dir/before" > "$tmp/receipt-fields.txt" 2>/dev/null; then
        printf '%s\n' "reason=unreadable" > "$tmp/receipt-fields.txt"
    fi
    # Read with `case` rather than `IFS=`, so a before-image path with a space
    # in it survives as one value.
    while IFS= read -r field || [ -n "$field" ]; do
        case "$field" in
            valid=1) receipt_valid=1 ;;
            phase=*) receipt_phase=${field#phase=} ;;
            kind=*) receipt_kind=${field#kind=} ;;
            version=*) receipt_installed_version=${field#version=} ;;
            started_at=*) receipt_started_at=${field#started_at=} ;;
            workspace=*) receipt_workspace=${field#workspace=} ;;
            retiring_desktop=1) receipt_retiring_desktop=1 ;;
            before_server_plist=*) receipt_before_server_plist=${field#before_server_plist=} ;;
            before_desktop_plist=*) receipt_before_desktop_plist=${field#before_desktop_plist=} ;;
            before_shim=*) receipt_before_shim=${field#before_shim=} ;;
            before_install_receipt=*) receipt_before_install_receipt=${field#before_install_receipt=} ;;
            before_tool_env=*) receipt_before_tool_env=${field#before_tool_env=} ;;
            reason=*) receipt_reason=${field#reason=} ;;
        esac
    done < "$tmp/receipt-fields.txt"
    if [ "$receipt_valid" -eq 0 ]; then
        receipt_phase=
    elif [ "$receipt_phase" = retiring ] && [ "$receipt_retiring_desktop" -ne 0 ]; then
        # The transaction this receipt came from had already started taking the
        # app's own agent out of launchd, so launchd may already have dropped the
        # job on this Mac. Two things follow, and both are about not lying about
        # that agent: this run's rollback has to load it again, and this run is
        # finishing a retirement rather than installing over the top of it.
        desktop_bootout_done=1
        resume_retiring=1
    fi
}

refuse_unusable_receipt() {
    # A receipt this script cannot trust is not a state to carry on from: the
    # next step would be to replace an engine whose before-images are unknown.
    # Every answer here says what to do about the file, because deleting the one
    # record of a rollback by hand is the user's decision, not this script's.
    case "$receipt_reason" in
        none) return 0 ;;
        unreadable)
            fail "the migration receipt at $migration_dir/receipt.json cannot be read, and it is the only record of the before-images this migration keeps. Move it aside (mv $migration_dir/receipt.json $migration_dir/receipt.json.bak) and re-run with --migrate"
            ;;
        schema)
            fail "the migration receipt at $migration_dir/receipt.json was written by a different version of this installer, so its before-images cannot be trusted. Move it aside (mv $migration_dir/receipt.json $migration_dir/receipt.json.bak) and re-run with --migrate"
            ;;
        phase)
            fail "the migration receipt at $migration_dir/receipt.json does not name a phase this installer wrote. Move it aside (mv $migration_dir/receipt.json $migration_dir/receipt.json.bak) and re-run with --migrate"
            ;;
        before)
            fail "the migration receipt at $migration_dir/receipt.json has no before-images recorded, so this migration cannot be rolled back from it. Move it aside (mv $migration_dir/receipt.json $migration_dir/receipt.json.bak) and re-run with --migrate"
            ;;
        missing-image)
            fail "the migration receipt at $migration_dir/receipt.json refers to before-images that are no longer in $migration_dir/before, so a failed migration could no longer be rolled back. Finish the install by hand: ciao setup --workspace <workspace> --python <ciao> && ciao service start"
            ;;
        incomplete-image)
            fail "the migration receipt at $migration_dir/receipt.json does not record every before-image this migration keeps, so this run cannot tell a file this Mac never had from one it was not told about, and refuses to replace anything. Its snapshots are still in $migration_dir/before: put back what you need from there and finish by hand (ciao setup --workspace <workspace> --python <ciao> && ciao service start), or move the receipt aside (mv $migration_dir/receipt.json $migration_dir/receipt.json.bak) once this Mac is in the state you want and re-run with --migrate"
            ;;
    esac
}

server_plist_program() {
    # ProgramArguments:0 out of the engine LaunchAgent, read raw by the same
    # interpreter that reads the migration receipt: the plist is what a
    # migration is judged on, and a program read out of a shell word would be a
    # different answer on a file this script did not write. Prints nothing when
    # the file is missing or is not a plist, which is a fact the caller has to
    # hear about rather than a program it can compare.
    "$uv" run --quiet --no-project --python "$PYTHON_VERSION" python -c '
# engine-plist-program: ProgramArguments:0 out of a LaunchAgent plist
import plistlib
import sys

try:
    with open(sys.argv[1], "rb") as handle:
        payload = plistlib.load(handle)
except Exception:
    raise SystemExit(0)
arguments = payload.get("ProgramArguments") if isinstance(payload, dict) else None
if isinstance(arguments, (list, tuple)) and arguments:
    print(str(arguments[0] or ""))
' "$1" 2>/dev/null
}

migration_matches_install() {
    # "Already migrated" is only true when the receipt and what is actually
    # installed agree. A receipt is a file on disk, and a Mac that was restored
    # from a backup, or that had a later install written over it, still carries
    # one - and a stale success that exits 0 is a Mac with no engine and a
    # message saying it has one. So the version, the service role, the retired
    # desktop agent, the tool the install receipt names and - for a host - the
    # engine LaunchAgent itself all have to line up with the receipt.
    settled_mismatch=
    case "$receipt_phase" in
        migrated) expected_backend=launchd; expected_label=$SERVER_LABEL ;;
        migrated_client) expected_backend=none; expected_label= ;;
        *) return 1 ;;
    esac
    [ -n "$receipt_installed_version" ] || return 1
    [ -f "$install_receipt" ] || return 1
    installed_version=$(awk -F'"' '/^[[:space:]]*"version":/ {print $4; exit}' \
        "$install_receipt" 2>/dev/null || true)
    [ "$installed_version" = "$receipt_installed_version" ] || return 1
    installed_backend=$(awk -F'"' '/^[[:space:]]*"service_backend":/ {print $4; exit}' \
        "$install_receipt" 2>/dev/null || true)
    [ "$installed_backend" = "$expected_backend" ] || return 1
    installed_label=$(awk -F'"' '/^[[:space:]]*"service_label":/ {print $4; exit}' \
        "$install_receipt" 2>/dev/null || true)
    [ "$installed_label" = "$expected_label" ] || return 1
    # The app's own agent is retired by both migrating paths, so a plist still
    # sitting in LaunchAgents means the retirement never happened.
    [ ! -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" ] || return 1
    # A receipt outlives the install it recorded: a `uv tool uninstall`, a wiped
    # state directory or a half-removed tool environment all leave the file
    # behind, and a "migrated" Mac whose `ciao` cannot run is a Mac with no
    # engine whatever the receipt says.
    expected_executable=$(awk -F'"' '/^[[:space:]]*"executable":/ {print $4; exit}' \
        "$install_receipt" 2>/dev/null || true)
    expected_interpreter=$(awk -F'"' '/^[[:space:]]*"python":/ {print $4; exit}' \
        "$install_receipt" 2>/dev/null || true)
    if [ -z "$expected_executable" ] || [ ! -x "$expected_executable" ]; then
        settled_mismatch="the ciao entry point it names (${expected_executable:-none}) is not there"
        return 1
    fi
    if [ -z "$expected_interpreter" ] || [ ! -x "$expected_interpreter" ]; then
        settled_mismatch="the engine it was installed into (${expected_interpreter:-none}) is not there"
        return 1
    fi
    if [ "$expected_backend" != launchd ]; then
        # A client runs no engine of its own, and its plist is left in place
        # pointing inside Ciaobot.app on purpose: that is the agent that was
        # disabled, and the host is where that engine went. There is nothing here
        # to compare a program against.
        return 0
    fi
    # A host's engine is the com.ciao.server agent, so the plist has to be the
    # one this install wrote: a program inside Ciaobot.app is the desktop's
    # engine, which is exactly what a settled migration replaced, and a missing
    # plist is a Mac with nothing running.
    server_plist="$HOME/Library/LaunchAgents/$SERVER_LABEL.plist"
    installed_program=$(server_plist_program "$server_plist")
    if [ -z "$installed_program" ]; then
        settled_mismatch="there is no $SERVER_LABEL.plist in $HOME/Library/LaunchAgents"
        return 1
    fi
    if [ "$installed_program" != "$expected_executable" ]; then
        settled_mismatch="$SERVER_LABEL.plist still points at $installed_program"
        return 1
    fi
    return 0
}

warn_settled_mismatch() {
    # Not fatal, and not silent: the originals are still in before/, and
    # re-running the migration from them is what puts this Mac back into the
    # state the receipt claims. What is not acceptable is a settled receipt
    # quietly re-migrating a Mac, or one that was believed and was not true.
    echo "Ciaobot engine installer: the migration receipt says $receipt_phase, but $settled_mismatch, so it does not describe this Mac and is not believed. Re-running the migration from the before-images in $migration_dir/before" >&2
}

quit_desktop_app() {
    # The app owns the workspace's files, so it is asked to quit before
    # anything is replaced. The wait is a hard deadline, not a formality: an app
    # that is still there when it expires is a live writer, and replacing its
    # engine underneath it is the one outcome this script exists to prevent. So
    # the answer is "stop, with the old engine exactly as it was", which is why
    # this runs before the first thing is disabled, booted out or overwritten.
    # A missing app, or one that ignores the request, is not an error here.
    "$OSASCRIPT" -e 'tell application id "local.ciaobot.app" to quit' >/dev/null 2>&1 || true
    waited=0
    while [ "$waited" -lt 20 ] && "$PGREP" -x ciaobot-desktop >/dev/null 2>&1; do
        waited=$((waited + 1))
        "$SLEEP" 1
    done
    if "$PGREP" -x ciaobot-desktop >/dev/null 2>&1; then
        return 1
    fi
}

retire_desktop_agent() {
    # Only after the new engine has answered with its own version: the app's
    # agent is bootout *and* disabled, so a relaunch cannot bring it back
    # alongside the agent that replaced it. Every one of the three is checked.
    # A relaunch that came back anyway, or a plist removed while its agent is
    # still loaded, is a desktop agent running next to the engine that was
    # supposed to replace it - which is not a migration that worked, so the
    # caller rolls back instead of reporting success.
    "$LAUNCHCTL" bootout "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || return 1
    # Recorded the moment launchd has dropped the job, which is a different fact
    # from having removed the plist: from here the app's agent is unloaded, and a
    # rollback that only re-enables its label leaves a desktop engine that never
    # starts again.
    desktop_bootout_done=1
    "$LAUNCHCTL" disable "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || return 1
    rm -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" || return 1
    return 0
}

disable_local_engine() {
    # What a client path does before it installs anything: this Mac stops being
    # a writer. The server plist is left on disk (it is backed up) but bootout
    # *and* disable, so nothing restarts it and no second engine ever comes up
    # beside the host. A launchctl that refuses is not shrugged off: the whole
    # point of this path is that the local engine stops.
    "$LAUNCHCTL" bootout "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || return 1
    "$LAUNCHCTL" disable "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || return 1
    # The app's own agent is held to the same standard as the engine's, and the
    # order matters. A client that stopped the engine but left the app's agent
    # loaded comes back on the next relaunch with a desktop engine running next
    # to the host it was just handed to, so neither bootout nor disable is
    # ignored - and the refusal happens before the plist is removed, which is
    # what the rollback restores it from.
    "$LAUNCHCTL" bootout "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || return 1
    desktop_bootout_done=1
    "$LAUNCHCTL" disable "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || return 1
    rm -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" || return 1
    return 0
}

restore_before_state() {
    # Every file this transaction replaced goes back to its before-image, and
    # everything it created for itself is taken away again. An empty before-image
    # means "this was not here", so the file the migration made is removed
    # instead - and nothing this transaction did not create is touched.
    if [ -n "$before_server_plist" ]; then
        copy_file "$before_server_plist" \
            "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" || return 1
    else
        rm -f "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" || return 1
    fi
    if [ -n "$before_desktop_plist" ]; then
        copy_file "$before_desktop_plist" \
            "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" || return 1
    else
        rm -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" || return 1
    fi
    if [ -n "$before_shim" ]; then
        mkdir -p "$HOME/.local/bin"
        copy_file "$before_shim" "$HOME/.local/bin/ciao" || return 1
    else
        rm -f "$HOME/.local/bin/ciao" || return 1
    fi
    # The install receipt is what a later `ciao setup` or a runtime swap reads
    # to answer "which release installed me", so a rollback that left the new
    # engine's receipt in place would report the migration as successful after
    # putting the old engine back.
    if [ -n "$before_install_receipt" ]; then
        mkdir -p "$HOME/.local/state/ciaobot"
        copy_file "$before_install_receipt" "$install_receipt" || return 1
    else
        rm -f "$install_receipt" || return 1
    fi
    if [ -n "$tool_dir" ]; then
        if [ -n "$before_tool_env" ]; then
            rm -rf "$tool_dir/ciaobot" || return 1
            cp -pR "$before_tool_env" "$tool_dir/ciaobot" || return 1
        else
            rm -rf "$tool_dir/ciaobot" || return 1
        fi
    fi
    return 0
}

restore_desktop_agent() {
    # The app's own LaunchAgent, if this transaction took it out of launchd. Its
    # plist comes back from the before-image - restore_before_state has already
    # put it back, or taken it away again if this Mac never had one - and the
    # agent is loaded and started, because a host handed back to Ciaobot.app
    # with its agent still unloaded is a Mac whose engine never comes back on its
    # own. bootstrap and kickstart are checked, so a rollback that could not put
    # the app's engine back says so instead of claiming it did. When this
    # transaction never booted the agent out, or the Mac had no app agent to
    # begin with, there is nothing to load: launchd already holds the job, and
    # bootstrapping a label it holds is an error that would report a complete
    # rollback as incomplete.
    "$LAUNCHCTL" enable "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || return 1
    if [ "$desktop_bootout_done" -ne 0 ] &&
        [ -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" ]; then
        "$LAUNCHCTL" bootstrap "gui/$(id -u)" \
            "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" 2>/dev/null || return 1
        "$LAUNCHCTL" kickstart -k "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || return 1
    fi
    # The app's agent is enabled and loaded again, so this transaction is no
    # longer one that took it out of launchd. The receipt the rollback writes
    # says so, or the run after it would reload a job launchd already holds.
    desktop_bootout_done=0
    retiring_desktop=0
    return 0
}

rollback_host() {
    # The Mac gets its engine back: the app's own plist and shim, the install
    # receipt and tool environment the previous install left, and its agent
    # loaded and started. bootstrap and kickstart are checked, because a
    # rollback that reports success while launchd silently kept a job
    # definition for an engine that is no longer on disk is worse than one that
    # says what it could not put back.
    failure=
    restore_before_state || failure=yes
    "$LAUNCHCTL" bootout "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || true
    if [ -f "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" ]; then
        "$LAUNCHCTL" bootstrap "gui/$(id -u)" \
            "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" 2>/dev/null || failure=yes
        "$LAUNCHCTL" kickstart -k "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || failure=yes
    fi
    restore_desktop_agent || failure=yes
    if [ -n "$failure" ]; then
        migration_receipt rolled_back "$1 (rollback incomplete: $failure)" || true
        fail "migration failed and Ciaobot.app's engine could NOT be fully restored: $1. See $migration_dir/receipt.json, and check: $HOME/.local/bin/ciao service status"
    fi
    # `|| true` on the write that records the rollback: this runs because
    # something already went wrong, and `set -e` taking the shell down here
    # would leave the user with no message at all.
    migration_receipt rolled_back "$1" || true
    fail "migration failed and Ciaobot.app's engine was restored: $1"
}

rollback_client() {
    # A client had no engine of its own to replace, so what goes back is the
    # state it was in before: both launchd labels enabled and loaded again, the
    # desktop plist that was removed, the shim, the install receipt and the tool
    # environment. The message never claims an engine was restored - there was
    # no engine here - and it says which parts could not be put back.
    failure=
    restore_before_state || failure=yes
    "$LAUNCHCTL" enable "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || failure=yes
    if [ -f "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" ]; then
        "$LAUNCHCTL" bootstrap "gui/$(id -u)" \
            "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" 2>/dev/null || failure=yes
        "$LAUNCHCTL" kickstart -k "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || failure=yes
    fi
    restore_desktop_agent || failure=yes
    if [ -n "$failure" ]; then
        migration_receipt rolled_back "$1 (rollback incomplete: $failure)" || true
        fail "migration failed and this Mac's local engine could NOT be fully restored: $1. See $migration_dir/receipt.json, and check: $HOME/.local/bin/ciao service status"
    fi
    migration_receipt rolled_back "$1" || true
    fail "migration failed and this Mac's local engine was put back as it was: $1"
}

on_signal() {
    # An interrupted install must not leave a receipt claiming more than
    # happened, and it must not try to roll back from inside a signal handler:
    # half an undo driven by a signal is not an undo. What it can do truthfully
    # is record that the transaction never finished, so the next run resumes
    # from the originals instead of guessing what state this Mac is in.
    if [ "$migration_active" -ne 0 ]; then
        migration_receipt interrupted "the installer was interrupted before the migration finished" \
            || true
    fi
    exit 130
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
    # Every post-install failure has to undo what this transaction changed, and
    # the undo that fits is the one for the path it took: a client has nothing to
    # restore but its own previous state, a host has an engine to give back.
    case "$migrate_path" in
        host) rollback_host "$1" ;;
        client) rollback_client "$1" ;;
    esac
    fail "$1"
}

validate_client_url() {
    # `--as-client` is the one input this migration never read from a state
    # file: it is the user's answer to a question this Mac could not answer, and
    # it is printed back at them as the address to open. A prefix check accepts
    # `https://`, which opens nothing - taking the client path with it would
    # disable this Mac's own engine and then hand it over to no host at all. So
    # the override goes through the same parsed check the classifier applies to
    # a host_url, asked of the verified wheel, and the answer replaces the value
    # the rest of this run uses.
    [ -n "$as_client" ] || return 0
    if ! client_url=$("$uv" run --quiet --no-project --python "$PYTHON_VERSION" \
        --with "$wheel" python -I -m ciao.engine_migration check-client-url \
        "$as_client" 2>/dev/null); then
        fail "--as-client takes an address this Mac can be handed over to, and \"$as_client\" is not one: an http:// or https:// URL with a host name - a prefix, a bare path, credentials or whitespace in it is not an address. Re-run with --migrate --as-client https://your-host"
    fi
    as_client=$client_url
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
    if [ "$resume_retiring" -ne 0 ]; then
        # The last run stopped while it was retiring the app's own agent, which
        # means it had already installed the new engine and repointed the engine
        # plist at it. The classifier reads that as an installer-managed engine -
        # correctly, on its own - and the ordinary install would leave the app's
        # agent behind forever, in a receipt that never settles. So this run
        # finishes the transaction the receipt describes, with the kind and the
        # workspace that transaction was started with.
        migrate_kind=$receipt_kind
        [ -z "$receipt_workspace" ] || migrate_workspace=$receipt_workspace
        migrate_path=host
        return 0
    fi
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
    # An override the user typed is checked before the receipt is read and before
    # anything is touched: a client path taken with an address that opens nothing
    # stops this Mac's engine and then hands the user nowhere to sign in.
    validate_client_url
    # Read before anything else, because a migration in progress is resumed
    # from its receipt rather than started again from whatever this Mac looks
    # like now - and a receipt that cannot be trusted stops the run outright.
    load_migration_receipt
    refuse_unusable_receipt
    if migration_matches_install; then
        echo "This Mac was already migrated ($receipt_phase); nothing to do."
        exit 0
    fi
    # A settled receipt that does not describe this Mac is re-run from the
    # before-images it kept, and says so, rather than either being believed or
    # quietly starting a second migration.
    [ -z "$settled_mismatch" ] || warn_settled_mismatch
    classify_install
fi

# --- preflight: everything that can be decided without changing anything ----
#
# These checks read. They run before the migration touches a single label or
# file, because a collision discovered after `com.ciao.server` has been booted
# out and `Ciaobot.plist` has been deleted is a collision discovered too late to
# be a refusal: the user would be told to move a file away while their Mac has
# no engine and no agent.
bin_dir=$("$uv" tool dir --bin)
tool_dir=$("$uv" tool dir)
target="$bin_dir/ciao"
if [ -e "$target" ] || [ -L "$target" ]; then
    # `ciao` is a common enough name to collide (Ciao Prolog ships one), and
    # clobbering someone else's program is not this script's call. Ours is
    # either a uv tool entry point or the shim the desktop installer wrote.
    # The listing is indented output (`- ciaobot v1.2.3` under a heading), so
    # the name is matched where uv prints it: a re-run of this script - which is
    # what a retry after a failed or interrupted migration is - has to
    # recognise the entry point its own previous run left behind.
    if ! grep -qF "$SHIM_MARKER" "$target" 2>/dev/null &&
        ! "$uv" tool list 2>/dev/null | grep -qE '^[[:space:]]*-[[:space:]]+ciaobot([[:space:]]|$)'; then
        fail "$target exists and was not installed by Ciaobot; move it away and re-run"
    fi
fi

# Best effort: keep whatever the last install put there, so the engine can
# still answer "which release replaced me" after a runtime swap.
previous_version=
previous_executable=
if [ -f "$install_receipt" ]; then
    previous_version=$(awk -F'"' '/^[[:space:]]*"version":/ {print $4; exit}' "$install_receipt" 2>/dev/null || true)
    previous_executable=$(awk -F'"' '/^[[:space:]]*"executable":/ {print $4; exit}' "$install_receipt" 2>/dev/null || true)
fi

# --- the migration --------------------------------------------------------
#
# From `migration_active=1` on, this run may have changed the install, so every
# failure after this point has to undo it and a signal has to say so in the
# receipt. Before it, nothing has been touched and a refusal is free.
if [ "$migrate" -ne 0 ]; then
    case "$migrate_path" in
        host|client)
            # Stop the app and keep a way back before the first change: from
            # here on the engine this Mac is running is the one being replaced.
            backup_before
            migration_receipt started \
                || fail "could not record the migration state; nothing on this Mac has been changed"
            # Still before anything is disabled, booted out or overwritten: a
            # desktop that will not quit is a live writer, and the honest
            # answer is to stop with its engine exactly as it was.
            if ! quit_desktop_app; then
                migration_receipt started "Ciaobot.app was still running 20s after it was asked to quit; nothing was changed" \
                    || true
                fail "Ciaobot.app is still running 20s after it was asked to quit, so nothing on this Mac has been changed. Quit it (or run: osascript -e 'tell application id \"local.ciaobot.app\" to quit') and re-run with --migrate"
            fi
            migration_active=1
            ;;
    esac
    if [ "$migrate_path" = client ]; then
        disable_local_engine || abort_install "the local engine could not be stopped, so this Mac would keep writing"
    fi
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
        status=$("$CURL" -fsS "http://localhost:$port/api/startup-status" 2>/dev/null || true)
        if [ -n "$status" ]; then
            if [ "$migrate_path" != host ]; then
                healthy=1
                break
            fi
            # The version is picked out of the whole response, not counted out
            # of it: the endpoint's payload has keys before `version`, so
            # splitting the document on quotes and taking the fourth field
            # reads whichever key happens to come first. `"version":` cannot
            # match inside `"latest_version":` - the quote in front of `version`
            # is a character in that name, not one - so this is the engine's own
            # version and nothing else.
            reported=$(printf '%s\n' "$status" \
                | sed -n 's/.*"version":[[:space:]]*"\([^"]*\)".*/\1/p' \
                | head -1)
            if [ "$reported" = "$version" ]; then
                healthy=1
                break
            fi
        fi
        attempt=$((attempt + 1))
        "$SLEEP" 1
    done
    if [ "$healthy" -eq 0 ]; then
        if [ "$migrate_path" = host ]; then
            abort_install "the new engine did not answer on http://localhost:$port with version $version within ${health_attempts}s; check: $ciao service status"
        fi
        echo "Ciaobot engine installer: the engine is still starting; check: ciao service status" >&2
    fi
fi

if [ "$migrate_path" = client ]; then
    install_step "could not write the migration receipt" migration_receipt migrated_client
    migration_active=0
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
        # A durable phase a crash can resume from: the new engine is proven, and
        # the app's own agent is not retired yet. `retiring_desktop` goes in with
        # it because from this line on launchd may be about to lose that job, and
        # a run that stops in the middle of the retirement has to be resumed as
        # though it did - otherwise its rollback re-enables a label with no job
        # behind it and reports the engine restored. Only once the retirement
        # itself has succeeded does this become `migrated`.
        retiring_desktop=1
        install_step "could not write the migration receipt" migration_receipt retiring
        migration_active=0
        if retire_desktop_agent; then
            retiring_desktop=0
            install_step "could not write the migration receipt" migration_receipt migrated
        else
            # The new engine is up, but the app's agent is still loaded: a
            # desktop relaunch would bring it back next to the engine that was
            # supposed to replace it. That is not a migration that worked, so
            # the app's engine is put back and the failure is reported.
            migration_active=1
            abort_install "Ciaobot.app's own agent could not be retired: launchctl refused to boot it out or disable it"
        fi
    else
        # --no-start promised no service, and an engine that was never started
        # cannot retire anything. The receipt says so; re-running without
        # --no-start is what completes the hand-over.
        install_step "could not write the migration receipt" migration_receipt installed_no_start
        migration_active=0
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
