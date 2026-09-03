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
    if [ "${terminal_output:-0}" -eq 1 ]; then
        printf '\n'
    fi
    echo "Ciaobot installer: $*" >&2
    exit 1
}

terminal_output=0
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    terminal_output=1
fi

if [ "$terminal_output" -eq 1 ]; then
    installer_accent=$(printf '\033[38;5;209m')
    installer_soft=$(printf '\033[38;5;223m')
    installer_ok=$(printf '\033[38;5;114m')
    installer_muted=$(printf '\033[38;5;245m')
    installer_reset=$(printf '\033[0m')
else
    installer_accent=
    installer_soft=
    installer_ok=
    installer_muted=
    installer_reset=
fi

installer_step() {
    percent=$1
    message=$2
    if [ "$terminal_output" -eq 1 ]; then
        printf '\r  %s[%3d%%]%s %s%s%s' \
            "$installer_accent" "$percent" "$installer_reset" \
            "$installer_accent" "$message" "$installer_reset"
    else
        printf '  [%3d%%] %s\n' "$percent" "$message"
    fi
}

installer_done() {
    if [ "$terminal_output" -eq 1 ]; then
        printf ' %s✓%s\n' "$installer_ok" "$installer_reset"
    else
        printf '           ok\n'
    fi
}

installer_header() {
    printf '\n'
    printf '%s╭────────────────────────────────────────────────────────────╮%s\n' \
        "$installer_accent" "$installer_reset"
    printf '%s│%s  %s› ciao%s  %s· first install%s                         %s│%s\n' \
        "$installer_accent" "$installer_reset" "$installer_soft" "$installer_reset" \
        "$installer_muted" "$installer_reset" "$installer_accent" "$installer_reset"
    printf '%s╰────────────────────────────────────────────────────────────╯%s\n' \
        "$installer_accent" "$installer_reset"
    printf '\n'
    printf '  %sWelcome! Let’s get Ciaobot ready for its first hello.%s\n' \
        "$installer_soft" "$installer_reset"
    printf '  %sThe signed app will be installed for this user only.%s\n\n' \
        "$installer_muted" "$installer_reset"
}

installer_greetings() {
    printf '\n'
    printf '  %s✦ ciao%s       %sItalian%s\n' "$installer_accent" "$installer_reset" "$installer_muted" "$installer_reset"
    printf '  %s✦ hello%s      %sEnglish%s\n' "$installer_accent" "$installer_reset" "$installer_muted" "$installer_reset"
    printf '  %s✦ hola%s       %sSpanish%s\n' "$installer_accent" "$installer_reset" "$installer_muted" "$installer_reset"
    printf '  %s✦ salut%s      %sFrench%s\n' "$installer_accent" "$installer_reset" "$installer_muted" "$installer_reset"
    printf '  %s✦ hallo%s      %sGerman%s\n' "$installer_accent" "$installer_reset" "$installer_muted" "$installer_reset"
    printf '  %s✦ olá%s        %sPortuguese%s\n' "$installer_accent" "$installer_reset" "$installer_muted" "$installer_reset"
    printf '  %s✦ こんにちは%s  %sJapanese%s\n' "$installer_accent" "$installer_reset" "$installer_muted" "$installer_reset"
    printf '  %s✦ 안녕하세요%s  %sKorean%s\n' "$installer_accent" "$installer_reset" "$installer_muted" "$installer_reset"
    printf '  %s✦ مرحبا%s      %sArabic%s\n' "$installer_accent" "$installer_reset" "$installer_muted" "$installer_reset"
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

for command in curl tar shasum mktemp mkdir mv find sw_vers awk uname id launchctl pgrep sed grep sysctl; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done

[ "$(uname -s)" = "Darwin" ] || fail "this installer supports macOS only"
# A terminal running under Rosetta reports x86_64 even on Apple Silicon, so an
# x86_64 process is only rejected when the hardware is not arm64 underneath.
case "$(uname -m)" in
    arm64) ;;
    x86_64)
        if [ "$(sysctl -in sysctl.proc_translated 2>/dev/null)" != "1" ]; then
            fail "Ciaobot now requires Apple Silicon (arm64); Intel Macs are no longer supported"
        fi
        ;;
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

archive_name=${CIAO_ARCHIVE_NAME:-Ciaobot_${version}_aarch64.app.tar.gz}
signature_name=${archive_name}.sig
verifier_name=${CIAO_VERIFIER_NAME:-ciaobot-installer-verify_aarch64}
tmp=$(mktemp -d "${TMPDIR:-/tmp}/ciaobot-install.XXXXXX")
# The staging bundle cannot live under $tmp: it has to sit next to the install
# target so the final move is a rename on one filesystem rather than a ~350 MB
# copy. That put it outside this cleanup, so every failed install (a bad
# archive, a runtime self-check failure, Ciaobot still running, ^C during the
# unpack) abandoned a full staging copy in the user's Applications directory
# forever. `stage` is empty until the unpack step claims a path, and is cleared
# again once the bundle has been moved into place, so the trap only ever
# removes a directory this run created and still owns.
stage=
trap 'rm -rf "$tmp"; [ -z "${stage:-}" ] || rm -rf "${stage:-}"' EXIT HUP INT TERM

# The archive is hundreds of MB; a silent fetch froze the step line at its
# checkpoint percentage with nothing on screen moving. On a TTY, poll the
# partial file against the Content-Length and redraw the step line with a
# live percentage, size, and rate. Piped output keeps the quiet -fsSL form
# so logs and CI don't fill with bar redraws.
download_live() {
    url=$1
    target=$2
    percent=$3
    message=$4
    total=
    total=$(curl -fsSI -L --connect-timeout 15 "$url" 2>/dev/null |
        awk 'tolower($1) == "content-length:" { gsub("\r", "", $2); print $2; exit }')
    curl -fsSL --retry 3 --connect-timeout 15 "$url" -o "$target" &
    pid=$!
    last_size=0
    last_epoch=0
    while kill -0 "$pid" 2>/dev/null; do
        size=0
        if [ -e "$target" ]; then
            size=$(wc -c < "$target" 2>/dev/null || echo 0)
        fi
        if [ "$size" -ne "$last_size" ]; then
            epoch=$(date +%s)
            rate=0
            if [ "$epoch" -gt "$last_epoch" ] && [ "$last_epoch" -gt 0 ]; then
                rate=$(((size - last_size) / (epoch - last_epoch)))
            fi
            last_size=$size
            last_epoch=$epoch
            redraw_download "$size" "$rate" "$total" "$percent" "$message"
        fi
        sleep 0.2
    done
    wait "$pid"
    status=$?
    size=0
    if [ -e "$target" ]; then
        size=$(wc -c < "$target" 2>/dev/null || echo 0)
    fi
    redraw_download "$size" 0 "$total" "$percent" "$message"
    return "$status"
}

redraw_download() {
    size=$1
    rate=$2
    total=$3
    percent_label=$4
    message=$5
    if [ -n "$total" ] && [ "$total" -gt 0 ] 2>/dev/null; then
        percent=$((size * 100 / total))
        [ "$percent" -gt 100 ] && percent=100
        rate_mib=$(awk -v rate="$rate" 'BEGIN { printf "%.1f", rate / 1048576 }')
        printf '\r  %s[%3d%%]%s %s%s%s %s(%d MiB of %d MiB · %s MiB/s)%s\033[K' \
            "$installer_accent" "$percent" "$installer_reset" \
            "$installer_soft" "$message" "$installer_reset" \
            "$installer_muted" "$((size / 1048576))" "$((total / 1048576))" \
            "$rate_mib" "$installer_reset"
    else
        rate_mib=$(awk -v rate="$rate" 'BEGIN { printf "%.1f", rate / 1048576 }')
        printf '\r  %s[%3d%%]%s %s%s%s %s(%d MiB · %s MiB/s)%s\033[K' \
            "$installer_accent" "$percent_label" "$installer_reset" \
            "$installer_soft" "$message" "$installer_reset" \
            "$installer_muted" "$((size / 1048576))" "$rate_mib" "$installer_reset"
    fi
}

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
    installer_header
    installer_step 20 "checking macOS and architecture"
    installer_done
    installer_step 45 "would download the signed release"
    installer_done
    installer_step 70 "would verify and unpack the app"
    installer_done
    installer_step 100 "would install into $app_dir/Ciaobot.app"
    installer_done
    installer_greetings
    printf '\n  %sDry run complete — no files were changed.%s\n' "$installer_soft" "$installer_reset"
    exit 0
fi

archive="$tmp/$archive_name"
signature="$tmp/$signature_name"
verifier="$tmp/$verifier_name"
installer_header
installer_step 8 "checking macOS and architecture"
installer_done
installer_step 25 "downloading the signed release"
if [ "$terminal_output" -eq 1 ]; then
    download_live "$base/$archive_name" "$archive" 25 "downloading the signed release" \
        || fail "could not download $archive_name"
else
    download "$base/$archive_name" "$archive" || fail "could not download $archive_name"
fi
installer_done
installer_step 32 "downloading the release signature"
download "$base/$signature_name" "$signature" || fail "could not download $signature_name"
installer_done
installer_step 39 "downloading the native verifier"
download "$base/$verifier_name" "$verifier" || fail "could not download $verifier_name"
installer_done

expected_verifier_sha=__VERIFIER_SHA256__
placeholder=__VERIFIER_SHA"256__"
installer_step 47 "checking the verifier checksum"
[ "$expected_verifier_sha" != "$placeholder" ] || fail "installer verifier checksum was not embedded"
actual_verifier_sha=$(shasum -a 256 "$verifier" | awk '{print $1}')
[ "$actual_verifier_sha" = "$expected_verifier_sha" ] || fail "installer verifier checksum mismatch"
chmod 755 "$verifier"
installer_done
installer_step 55 "verifying the release signature"
"$verifier" "$archive" "$signature" >/dev/null || fail "release signature verification failed"
installer_done

installer_step 66 "unpacking the bundled runtime"
mkdir -p "$app_dir"
destination="$app_dir/Ciaobot.app"
stage="$app_dir/.Ciaobot.app.new.$$"
# Older installers preserved the previous bundle as .Ciaobot.app.previous.
# Nothing restores it anymore, and a leftover copy would keep ~350 MB of the
# user's disk forever, so clear it whenever it shows up.
rm -rf "$app_dir/.Ciaobot.app.previous"
rm -rf "$stage"
mkdir "$stage"
tar -xzf "$archive" -C "$stage" || fail "could not extract the release archive"
extracted="$stage/Ciaobot.app"
[ -x "$extracted/Contents/MacOS/ciaobot-desktop" ] || fail "archive does not contain a usable Ciaobot.app"
[ -x "$extracted/Contents/Resources/ciao-runtime/bin/ciao" ] || fail "archive does not contain Ciaobot's bundled runtime"
"$extracted/Contents/Resources/ciao-runtime/bin/ciao" --help >/dev/null \
    || fail "bundled Ciaobot runtime self-check failed"
installer_done

# The engine is a second LaunchAgent, and its executable lives inside the
# bundle this install is about to replace. Booting it out used to happen only as
# a side effect of `ciao setup --load-launchd` further down, which runs only
# when a workspace was recovered *and* --no-start was not passed -- so on every
# other path the new app came up talking to the previous engine build, still
# running from a bundle that no longer exists on disk. Stop it here instead,
# unconditionally and before the swap: setup re-bootstraps it below when it
# runs, and when it does not, the app starts the engine itself on next launch.
uid=$(id -u)
launchctl bootout "gui/$uid/com.ciao.server" >/dev/null 2>&1 || true

if [ -e "$destination" ]; then
    # A manually launched app is not necessarily owned by the LaunchAgent, so
    # booting that agent out alone would leave the old single-instance process
    # alive across the bundle swap. Ask the app to quit, then wait for the
    # executable to disappear before replacing the old bundle. The previous
    # version is not preserved: keeping a full .previous copy would double the
    # ~350 MB disk cost of every update for a rollback nothing ever performs.
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
    rm -rf "$destination"
fi
installer_step 82 "installing Ciaobot.app"
if ! mv "$extracted" "$destination"; then
    [ ! -e "$destination" ] || rm -rf "$destination"
    fail "could not install Ciaobot.app"
fi
rm -rf "$stage"
# The bundle is in place, so there is no staging directory left to clean up.
# Clearing the variable keeps the exit trap from touching $app_dir again.
stage=
installer_done

engine="$destination/Contents/Resources/ciao-runtime/bin/ciao"

# The engine lives inside the app bundle, so a terminal has no `ciao` at all
# unless the install puts one there -- yet the docs, the setup wizard and every
# support answer hand out bare `ciao ...` commands. Users who had an unrelated
# `ciao` on PATH got its error message instead ("Unknown command 'auth'"), and
# users who had none got "command not found".
#
# A shim, not a symlink: the bundled launcher derives its runtime root from
# `dirname "$0"`, which through a symlink resolves to the link's directory and
# breaks. Never overwrite a `ciao` we did not write -- it may be someone's own
# tool -- so the marker line is what makes replacement safe on updates.
shim_dir="$HOME/.local/bin"
shim="$shim_dir/ciao"
shim_marker="# Ciaobot shim (managed by the Ciaobot installer)"
shim_installed=0
if { [ -e "$shim" ] || [ -L "$shim" ]; } && ! grep -qF "$shim_marker" "$shim" 2>/dev/null; then
    echo "Ciaobot installer: $shim exists and was not created by Ciaobot; leaving it alone" >&2
    echo "Ciaobot installer: run the CLI as $engine" >&2
elif mkdir -p "$shim_dir" 2>/dev/null; then
    shim_tmp="$shim.tmp.$$"
    if {
        printf '%s\n' '#!/bin/sh'
        printf '%s\n' "$shim_marker"
        printf 'exec "%s" "$@"\n' "$engine"
    } > "$shim_tmp" 2>/dev/null && chmod 755 "$shim_tmp" && mv "$shim_tmp" "$shim"; then
        shim_installed=1
    else
        rm -f "$shim_tmp"
        echo "Ciaobot installer: could not create $shim" >&2
    fi
else
    echo "Ciaobot installer: could not create $shim_dir" >&2
fi

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
installer_step 89 "preserving the local workspace"

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
installer_done

if [ "$no_start" -eq 0 ]; then
    installer_step 96 "starting the menu-bar app"
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
        # Check before acting, not after: if the loaded job already names the
        # new executable there is nothing to do, and tearing it down first
        # would mean a mis-detection (a future launchctl print format, an
        # escaped path) repeatedly booting out an agent that was working.
        if launchctl print "gui/$uid/Ciaobot" 2>/dev/null \
            | grep -qF "$desktop_executable"; then
            break
        fi
        if [ "$attempts" -ge 15 ]; then
            echo "Ciaobot installer: the menu-bar LaunchAgent did not load; open Ciaobot.app manually" >&2
            break
        fi
        attempts=$((attempts + 1))
        launchctl bootout "gui/$uid/Ciaobot" >/dev/null 2>&1 || true
        launchctl bootstrap "gui/$uid" "$desktop_plist" >/dev/null 2>&1 || true
        sleep 1
    done
    launchctl kickstart "gui/$uid/Ciaobot" >/dev/null 2>&1 || true
    # The engine agent was booted out before the bundle swap, and only
    # `setup --load-launchd` re-registers it — which runs only when a workspace
    # was recovered. The app re-registers it itself as a further fallback, but
    # the guarantee belongs here: an updated install must not leave the engine
    # unloaded, and a missing registration is verifiable without any engine
    # binary at all.
    if [ -f "$plist" ] && ! launchctl print "gui/$uid/com.ciao.server" >/dev/null 2>&1; then
        launchctl enable "gui/$uid/com.ciao.server" >/dev/null 2>&1 || true
        launchctl bootstrap "gui/$uid" "$plist" >/dev/null 2>&1 || true
        launchctl kickstart -k "gui/$uid/com.ciao.server" >/dev/null 2>&1 || true
        if ! launchctl print "gui/$uid/com.ciao.server" >/dev/null 2>&1; then
            echo "Ciaobot installer: the engine LaunchAgent did not load; open Ciaobot to start the engine" >&2
        fi
    fi
    installer_done
else
    installer_step 96 "leaving the app stopped (--no-start)"
    installer_done
fi

installer_step 100 "finishing the first hello"
installer_done
installer_greetings
printf '\n'
echo "Ciaobot installed at $destination"
if [ -n "$workspace" ]; then
    echo "Workspace: $workspace"
else
    echo "Workspace: first-run onboarding will ask where to create or adopt one"
fi
if [ "$shim_installed" -eq 1 ]; then
    # Installing the shim is not enough on its own: ~/.local/bin is not on a
    # default macOS PATH, and when it is, an unrelated `ciao` earlier in the
    # PATH still wins. Say which of the two happened instead of leaving the
    # user to discover it through a confusing error.
    case ":${PATH:-}:" in
        *":$shim_dir:"*)
            resolved=$(command -v ciao || true)
            if [ -n "$resolved" ] && [ "$resolved" != "$shim" ]; then
                echo "Note: another 'ciao' comes first on your PATH ($resolved)."
                echo "      Run Ciaobot's CLI as $shim"
            fi
            ;;
        *)
            echo "Add ~/.local/bin to your PATH to use the 'ciao' command:"
            echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
            ;;
    esac
fi
