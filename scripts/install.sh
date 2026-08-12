#!/bin/sh
set -eu

# This script is intentionally small and POSIX-compatible. It is the only
# first-install path documented for end users. The release workflow replaces
# __VERIFIER_SHA256__ with the checksum of the native verifier built from this
# repository before uploading the script as install.sh.

repo=${CIAO_GITHUB_REPO:-raffaelefarinaro/ciaobot}
# A private mirror can override the release download root for explicit
# versions. Archive signatures are still verified against the embedded public
# key, so changing the transport endpoint does not bypass authenticity checks.
release_base=${CIAO_RELEASE_BASE_URL:-https://github.com/$repo/releases/download}
release_base=${release_base%/}
app_dir=${CIAO_APP_DIR:-$HOME/Applications}
version=
no_start=0
dry_run=0

usage() {
    cat >&2 <<'USAGE'
Usage: install.sh [--version VERSION] [--app-dir DIRECTORY] [--no-start] [--dry-run]

Installs the signed, self-contained Ciaobot.app for the current user.
USAGE
}

fail() {
    echo "Ciaobot installer: $*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case $1 in
        --version)
            [ "$#" -ge 2 ] || fail "--version requires a value"
            version=$2
            shift 2
            ;;
        --app-dir)
            [ "$#" -ge 2 ] || fail "--app-dir requires a value"
            app_dir=$2
            shift 2
            ;;
        --no-start) no_start=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage; fail "unknown option: $1" ;;
    esac
done

for command in curl tar shasum mktemp mkdir mv find sw_vers awk uname id launchctl pgrep sed grep; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done

[ "$(uname -s)" = "Darwin" ] || fail "this installer supports macOS only"
case "$(uname -m)" in
    arm64|x86_64) ;;
    *) fail "unsupported macOS architecture: $(uname -m)" ;;
esac

major=$(sw_vers -productVersion | awk -F. '{print $1}')
case "$major" in
    ''|*[!0-9]*) fail "could not determine the macOS version" ;;
esac
[ "$major" -ge 13 ] || fail "Ciaobot requires macOS 13 or newer"

case "$version" in
    '')
        [ -z "${CIAO_RELEASE_BASE_URL:-}" ] || fail "--version is required with CIAO_RELEASE_BASE_URL"
        latest_url=$(curl -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/$repo/releases/latest")
        version=${latest_url##*/}
        version=${version#v}
        case "$version" in
            ''|*[!0-9A-Za-z.-]*) fail "could not determine the latest release version" ;;
        esac
        base="$release_base/v$version"
        ;;
    v*) version=${version#v}; base="$release_base/v$version" ;;
    *[!0-9A-Za-z.-]*) fail "invalid version: $version" ;;
    *) base="$release_base/v$version" ;;
esac

archive_name=${CIAO_ARCHIVE_NAME:-Ciaobot_${version}_universal.app.tar.gz}
signature_name=${archive_name}.sig
verifier_name=${CIAO_VERIFIER_NAME:-ciaobot-installer-verify_universal}
tmp=$(mktemp -d "${TMPDIR:-/tmp}/ciaobot-install.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

download() {
    curl -fsSL --retry 3 --connect-timeout 15 "$1" -o "$2"
}

xml_escape() {
    # The path is XML character data, so these three characters are the only
    # ones that can change the meaning of the plist document.
    printf '%s' "$1" | sed \
        -e 's/&/\&amp;/g' \
        -e 's/</\&lt;/g' \
        -e 's/>/\&gt;/g'
}

if [ "$dry_run" -eq 1 ]; then
    echo "Would download: $base/$archive_name"
    echo "Would install into: $app_dir/Ciaobot.app"
    exit 0
fi

archive="$tmp/$archive_name"
signature="$tmp/$signature_name"
verifier="$tmp/$verifier_name"
download "$base/$archive_name" "$archive" || fail "could not download $archive_name"
download "$base/$signature_name" "$signature" || fail "could not download $signature_name"
download "$base/$verifier_name" "$verifier" || fail "could not download $verifier_name"

expected_verifier_sha=__VERIFIER_SHA256__
placeholder=__VERIFIER_SHA"256__"
[ "$expected_verifier_sha" != "$placeholder" ] || fail "installer verifier checksum was not embedded"
actual_verifier_sha=$(shasum -a 256 "$verifier" | awk '{print $1}')
[ "$actual_verifier_sha" = "$expected_verifier_sha" ] || fail "installer verifier checksum mismatch"
chmod 755 "$verifier"
"$verifier" "$archive" "$signature" >/dev/null || fail "release signature verification failed"

mkdir -p "$app_dir"
destination="$app_dir/Ciaobot.app"
stage="$app_dir/.Ciaobot.app.new.$$"
backup="$app_dir/.Ciaobot.app.previous"
rm -rf "$stage"
mkdir "$stage"
tar -xzf "$archive" -C "$stage" || fail "could not extract the release archive"
extracted="$stage/Ciaobot.app"
[ -x "$extracted/Contents/MacOS/ciaobot-desktop" ] || fail "archive does not contain a usable Ciaobot.app"
[ -x "$extracted/Contents/Resources/ciao-runtime/bin/ciao" ] || fail "archive does not contain Ciaobot's bundled runtime"
"$extracted/Contents/Resources/ciao-runtime/bin/ciao" --help >/dev/null \
    || fail "bundled Ciaobot runtime self-check failed"

if [ -e "$destination" ]; then
    # A manually launched app is not necessarily owned by the LaunchAgent, so
    # booting that agent out alone would leave the old single-instance process
    # alive across the bundle swap. Ask the app to quit, then wait for the
    # executable to disappear before moving the old bundle aside.
    if command -v osascript >/dev/null 2>&1; then
        osascript -e 'tell application id "local.ciaobot.app" to quit' \
            >/dev/null 2>&1 || true
    fi
    uid=$(id -u)
    launchctl bootout "gui/$uid/Ciaobot" >/dev/null 2>&1 || true
    attempts=0
    while pgrep -x ciaobot-desktop >/dev/null 2>&1; do
        attempts=$((attempts + 1))
        [ "$attempts" -lt 30 ] || fail "Ciaobot is still running; quit it and retry"
        sleep 1
    done
    rm -rf "$backup"
    mv "$destination" "$backup" || fail "could not move the existing Ciaobot.app aside"
fi
if ! mv "$extracted" "$destination"; then
    [ ! -e "$destination" ] || rm -rf "$destination"
    [ ! -e "$backup" ] || mv "$backup" "$destination"
    fail "could not install Ciaobot.app"
fi
rm -rf "$stage"

engine="$destination/Contents/Resources/ciao-runtime/bin/ciao"
plist="$HOME/Library/LaunchAgents/com.ciao.server.plist"
desktop_plist="$HOME/Library/LaunchAgents/Ciaobot.plist"
workspace=
if [ -f "$plist" ] && command -v /usr/libexec/PlistBuddy >/dev/null 2>&1; then
    existing_workspace=$(/usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' "$plist" 2>/dev/null || true)
    if [ -n "$existing_workspace" ] && [ -d "$existing_workspace" ] && [ -f "$existing_workspace/.env" ]; then
        workspace=$existing_workspace
    fi
fi

mkdir -p "$HOME/Library/LaunchAgents"

# The engine and the menu-bar app are separate processes. Keep the app in the
# same per-user LaunchAgent model as the in-app "start at login" setting so a
# service-only launch cannot leave the user without a tray icon. The app owns
# this plist too, so the menu action can still disable it later.
desktop_executable="$destination/Contents/MacOS/ciaobot-desktop"
desktop_executable_xml=$(xml_escape "$desktop_executable")
desktop_plist_tmp="$desktop_plist.tmp.$$"
{
    printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>'
    printf '%s\n' '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    printf '%s\n' '<plist version="1.0"><dict>'
    printf '%s\n' '<key>Label</key><string>Ciaobot</string>'
    printf '%s\n' '<key>ProgramArguments</key><array>'
    printf '<string>%s</string>\n' "$desktop_executable_xml"
    printf '%s\n' '<string>--background</string></array>'
    printf '%s\n' '<key>RunAtLoad</key><true/>'
    printf '%s\n' '</dict></plist>'
} > "$desktop_plist_tmp"
mv "$desktop_plist_tmp" "$desktop_plist"

if [ -n "$workspace" ]; then
    if [ "$no_start" -eq 0 ]; then
        "$engine" setup \
            --workspace "$workspace" \
            --python "$engine" \
            --yes \
            --load-launchd \
            >/dev/null
    else
        "$engine" setup \
            --workspace "$workspace" \
            --python "$engine" \
            --yes \
            >/dev/null
    fi
fi

if [ "$no_start" -eq 0 ]; then
    uid=$(id -u)
    # Reload the app agent after an update so launchd does not retain the old
    # executable path. kickstart without -k starts it only when it is absent.
    # bootout is asynchronous. A bootstrap that lands before it finishes either
    # fails outright or finds the stale job still registered -- and that job
    # still names the bundle this install just moved aside, so kickstart would
    # try to run a path that no longer exists. Both failures were previously
    # swallowed, leaving an updated install with no menu-bar app. Retry until
    # the loaded job names the new executable, and say so if it never does.
    attempts=0
    while :; do
        launchctl bootout "gui/$uid/Ciaobot" >/dev/null 2>&1 || true
        launchctl bootstrap "gui/$uid" "$desktop_plist" >/dev/null 2>&1 || true
        if launchctl print "gui/$uid/Ciaobot" 2>/dev/null \
            | grep -qF "$desktop_executable"; then
            break
        fi
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 15 ]; then
            echo "Ciaobot installer: the menu-bar LaunchAgent did not load; open Ciaobot.app manually" >&2
            break
        fi
        sleep 1
    done
    launchctl kickstart "gui/$uid/Ciaobot" >/dev/null 2>&1 || true
fi

echo "Ciaobot installed at $destination"
if [ -n "$workspace" ]; then
    echo "Workspace: $workspace"
else
    echo "Workspace: first-run onboarding will ask where to create or adopt one"
fi
if [ -e "$backup" ]; then
    echo "Previous app preserved at $backup"
fi
