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
SHIM_MARKER="# Ciaobot shim (managed by the Ciaobot installer)"
PLISTBUDDY=/usr/libexec/PlistBuddy
version=
workspace=
no_start=0

usage() {
    cat >&2 <<'USAGE'
Usage: install-engine.sh [--version VERSION] [--workspace DIRECTORY] [--no-start]

Installs the Ciaobot engine for the current user with uv; the PWA is its UI.
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
        -h|--help) usage; exit 0 ;;
        *) usage; fail "unknown option: $1" ;;
    esac
done

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
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

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
        UV_NO_MODIFY_PATH=1 sh "$tmp/uv-installer.sh" >/dev/null || fail "could not install uv"
        uv="$HOME/.local/bin/uv"
    fi
    [ -x "$uv" ] || fail "uv is not available at $uv"
}

refuse_desktop_engine() {
    # Ciaobot.app owns the engine through a plist whose program arguments
    # point inside the bundle. Two launch agents must never fight over
    # com.ciao.server, and this script cannot migrate that install (#576).
    plist="$HOME/Library/LaunchAgents/$SERVER_LABEL.plist"
    if [ -f "$plist" ] && [ -x "$PLISTBUDDY" ]; then
        program=$("$PLISTBUDDY" -c 'Print :ProgramArguments:0' "$plist" 2>/dev/null || true)
        case "$program" in
            *.app/*)
                fail "Ciaobot.app manages the engine on this Mac. Migrating it to the terminal engine is not supported yet (#576); keep using Ciaobot.app or uninstall it first with: ciao desktop uninstall"
                ;;
        esac
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

find_uv
refuse_desktop_engine

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

"$uv" tool install --force --python "$PYTHON_VERSION" "$wheel" >/dev/null \
    || fail "uv tool install failed"
ciao="$bin_dir/ciao"
tool_python="$tool_dir/ciaobot/bin/python"
[ -x "$ciao" ] || fail "the installed ciao entry point is missing: $ciao"
[ -x "$tool_python" ] || fail "the installed engine interpreter is missing: $tool_python"

# Absolute paths only: the receipt is read by a process that has no idea which
# directory the installer ran from.
"$tool_python" -m ciao.install_receipt write \
    --version "$version" \
    --executable "$ciao" \
    --python "$tool_python" \
    --service-backend launchd \
    --service-label "$SERVER_LABEL" \
    --previous-version "$previous_version" \
    --previous-executable "$previous_executable" \
    >/dev/null || fail "could not write the install receipt"

if [ -z "$workspace" ]; then
    workspace=
    if [ -f "$plist" ] && [ -x "$PLISTBUDDY" ]; then
        existing=$("$PLISTBUDDY" -c 'Print :WorkingDirectory' "$plist" 2>/dev/null || true)
        if [ -n "$existing" ] && [ -d "$existing" ] && [ -f "$existing/.env" ]; then
            workspace=$existing
        fi
    fi
    [ -n "$workspace" ] || workspace="$HOME/Ciaobot"
fi
mkdir -p "$workspace"
workspace=$(cd "$workspace" && pwd -P)

# Idempotent, and it preserves an existing .env and its password.
"$ciao" setup --workspace "$workspace" --python "$ciao" --yes >/dev/null \
    || fail "ciao setup failed"

if [ "$no_start" -eq 0 ]; then
    "$ciao" service start --workspace "$workspace" --json >/dev/null \
        || fail "could not start the engine; try: $ciao service status"
    port=$(awk -F= '/^PWA_PORT=/{print $2}' "$workspace/.env" 2>/dev/null | tr -d '"' | tail -1)
    [ -n "$port" ] || port=8443
    # A slow first boot is not a failed install: the plist is written, so say
    # so instead of failing an install that did everything it promised.
    attempt=0
    while [ "$attempt" -lt 60 ]; do
        if curl -fsS -o /dev/null "http://localhost:$port/api/startup-status" 2>/dev/null; then
            break
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    if [ "$attempt" -ge 60 ]; then
        echo "Ciaobot engine installer: the engine is still starting; check: ciao service status" >&2
    fi
fi

echo "Ciaobot engine $version installed."
echo "  ciao:      $ciao"
echo "  workspace: $workspace"
url=
if [ "$no_start" -eq 0 ]; then
    # Printed to the terminal and nowhere else: the URL is a one-time
    # credential that signs the person at this keyboard in.
    url=$("$ciao" setup-url --workspace "$workspace" | tail -1)
    echo "Open Ciaobot: $url"
    echo "This link signs you in once. Do not share it."
fi
case ":${PATH:-}:" in
    *":$bin_dir:"*) ;;
    *) echo "Add ciao to your PATH: $uv tool update-shell" ;;
esac
if [ -t 1 ] && [ "$no_start" -eq 0 ] && command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
fi
