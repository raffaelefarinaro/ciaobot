#!/bin/sh
# Build a signed disk image from one already notarized Ciaobot.app.
#
# usage: scripts/make-dmg.sh --app PATH --output PATH --identity IDENTITY
#
# The image is the human download: its volume holds Ciaobot.app next to an
# `Applications` symlink, so the expected action is to drag the app into
# Applications before launching it. A copy launched from the download folder is
# translocated by macOS, and Sparkle then refuses to update it.
#
# The app must already be notarized and stapled (submitted by
# scripts/package-notarized.sh) and signed by this project's Developer ID team;
# the image itself is signed with the same identity. Notarizing and stapling the
# image changes its bytes, so its checksum is written by the caller after that
# step, never here.
#
# The script never installs, launches or contacts anything. It writes only
# inside the --output directory (a fresh hidden staging directory beside the
# image, removed on every exit path), refuses any existing output, and refuses
# to touch an Applications folder. It mounts the image read-only for
# inspection and always detaches it again, including on failure.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$root"

usage() {
    cat <<'EOF'
usage: scripts/make-dmg.sh --app PATH --output PATH --identity IDENTITY

  --app PATH        a notarized, stapled, Developer ID signed Ciaobot.app
  --output PATH     the .dmg to create; it must not exist yet
  --identity IDENTITY
                    the Developer ID Application identity that signs the image

The app must be a real directory (not a symlink), carry CFBundleIdentifier
local.ciaobot.mac, pass `xcrun stapler validate`, and be signed by the team in
release/update-config.env. The image is staged from a copy of the app plus an
`Applications -> /Applications` symlink, created with hdiutil, signed with the
identity, verified, mounted read-only and inspected, then detached and moved to
--output. Nothing is written to an Applications folder and an existing output is
never replaced.

Notarize, staple and checksum the image afterwards; scripts/package-notarized.sh
does that for a release.
EOF
}

die() {
    printf 'make-dmg: %s\n' "$1" >&2
    exit 1
}

usage_error() {
    usage >&2
    exit 2
}

app=
output=
identity=
while [ $# -gt 0 ]; do
    case $1 in
        --app)
            [ $# -ge 2 ] || usage_error
            app=$2
            shift 2
            ;;
        --output)
            [ $# -ge 2 ] || usage_error
            output=$2
            shift 2
            ;;
        --identity)
            [ $# -ge 2 ] || usage_error
            identity=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'make-dmg: unknown argument: %s\n' "$1" >&2
            usage_error
            ;;
    esac
done
[ -n "$app" ] && [ -n "$output" ] && [ -n "$identity" ] || usage_error

# An ad-hoc signature is not a distribution signature, and the image is handed
# to other people. Refuse it here rather than after a notarization rejection.
case $identity in
    -|--*)
        die "refusing an ad-hoc signing identity; --identity must be a Developer ID Application identity" ;;
esac

# Any directory named Applications is somebody's app folder, including a mounted
# image and a scratch tree. The DMG must never write into one.
in_applications() {
    case $1 in
        /Applications|/Applications/*) return 0 ;;
    esac
    case $1 in
        "${HOME:-/nonexistent}"/Applications|"${HOME:-/nonexistent}"/Applications/*) return 0 ;;
    esac
    case $1 in
        */Applications/*) return 0 ;;
    esac
    return 1
}

# --- The app --------------------------------------------------------------

[ -d "$app" ] || die "no app bundle at $app"
[ ! -L "$app" ] || die "refusing a symlinked app bundle: $app"
app=$(CDPATH= cd -- "$app" && pwd -P)
case $app in
    *.app) ;;
    *) die "not an .app bundle: $app" ;;
esac
if in_applications "$app"; then
    die "refusing an app inside an Applications folder: $app"
fi
app_name=$(basename -- "$app")

plist=$app/Contents/Info.plist
[ -f "$plist" ] && [ ! -L "$plist" ] || die "no Info.plist at $plist"
identifier=$(plutil -extract CFBundleIdentifier raw -o - "$plist" 2>/dev/null || true)
[ "$identifier" = "local.ciaobot.mac" ] \
    || die "not the Ciaobot app: CFBundleIdentifier is '${identifier:-missing}', expected local.ciaobot.mac"

# An unstapled app would run from the image without its notarization ticket and
# Gatekeeper would block it. The image is only as trustworthy as the bundle in
# it, so refuse before any image work.
staple_result=$(xcrun stapler validate "$app" 2>&1) \
    || die "app is not notarized: xcrun stapler validate failed for $app: $staple_result"

# Validity is not ownership: the image must carry this project's code, exactly
# as the appcast refuses an update another team signed. The expected team is
# public and committed next to the feed and key.
update_config=release/update-config.env
[ -f "$update_config" ] && [ ! -L "$update_config" ] \
    || die "release/update-config.env is missing; cannot verify the signing team"
# shellcheck disable=SC1090
. "$update_config"
[ -n "${UPDATE_TEAM_ID:-}" ] || die "release/update-config.env has no UPDATE_TEAM_ID"
case $UPDATE_TEAM_ID in
    *[!A-Z0-9]*) die "UPDATE_TEAM_ID must be a plain Apple team identifier: $UPDATE_TEAM_ID" ;;
esac

signature=$(codesign -d --verbose=4 "$app" 2>&1) \
    || die "could not read the app's code signature: $app"
case $signature in
    *"Authority=Developer ID Application"*) ;;
    *) die "app is not signed with a Developer ID Application certificate: $app" ;;
esac
signed_team=$(printf '%s\n' "$signature" | sed -n 's/^TeamIdentifier=//p' | head -n 1)
case $signed_team in
    ''|"not set") die "app has no TeamIdentifier; refusing an image of a build that was not signed by team $UPDATE_TEAM_ID" ;;
esac
[ "$signed_team" = "$UPDATE_TEAM_ID" ] \
    || die "app was signed by team $signed_team, not $UPDATE_TEAM_ID; refusing another developer's build"

# --- The output -----------------------------------------------------------

case $output in
    *.dmg) ;;
    *) die "output must be a .dmg file: $output" ;;
esac
[ ! -L "$output" ] && [ ! -e "$output" ] || die "refusing to overwrite existing output: $output"
output_dir=$(dirname -- "$output")
[ -d "$output_dir" ] || die "output directory does not exist: $output_dir"
output_dir=$(CDPATH= cd -- "$output_dir" && pwd -P)
output=$output_dir/$(basename -- "$output")
case $output in
    "$app"/*) die "refusing to write inside the source app: $output" ;;
esac
if in_applications "$output"; then
    die "refusing to write inside an Applications folder: $output"
fi

# Build beside the output so publishing is a rename on one volume: nothing
# appears at --output until the image has been created, signed, verified and
# inspected. The staging tree is removed on every exit path.
work_root=$(mktemp -d "$output_dir/.ciaobot-dmg.XXXXXX")
stage=$work_root/stage
pending=$work_root/image.dmg
mount_point=
cleanup() {
    if [ -n "${mount_point:-}" ]; then
        hdiutil detach "$mount_point" >/dev/null 2>&1 || true
        mount_point=
    fi
    [ -z "${work_root:-}" ] || rm -rf "$work_root"
}
trap cleanup EXIT INT TERM

mkdir "$stage"
ditto "$app" "$stage/$app_name"
ln -s /Applications "$stage/Applications"

hdiutil create -volname Ciaobot -srcfolder "$stage" -fs HFS+ -format UDZO "$pending"
codesign --force --timestamp --sign "$identity" "$pending"

image_signature=$(codesign -d --verbose=4 "$pending" 2>&1) \
    || die "could not read the image's code signature: $pending"
case $image_signature in
    *"Authority=Developer ID Application"*) ;;
    *) die "image is not signed with a Developer ID Application certificate" ;;
esac
image_team=$(printf '%s\n' "$image_signature" | sed -n 's/^TeamIdentifier=//p' | head -n 1)
[ "$image_team" = "$UPDATE_TEAM_ID" ] \
    || die "image was signed by team '${image_team:-unknown}', not $UPDATE_TEAM_ID"

hdiutil verify "$pending" >/dev/null

# Prove the image the way a user meets it: mount read-only and look for the app
# and the Applications link. The mount point is always detached below or by the
# trap, so a failed inspection can never leave a volume behind.
mount_point=$work_root/mounted
mkdir "$mount_point"
hdiutil attach -readonly -nobrowse -mountpoint "$mount_point" "$pending" >/dev/null
mounted_app=$mount_point/$app_name
[ -d "$mounted_app" ] && [ ! -L "$mounted_app" ] \
    || die "the image does not contain a real $app_name"
mounted_identifier=$(plutil -extract CFBundleIdentifier raw -o - "$mounted_app/Contents/Info.plist" 2>/dev/null || true)
[ "$mounted_identifier" = "$identifier" ] \
    || die "the image's app has CFBundleIdentifier '${mounted_identifier:-missing}'"
[ -L "$mount_point/Applications" ] \
    || die "the image has no Applications symlink; the app could only be run from the download folder"
link_target=$(readlink "$mount_point/Applications")
[ "$link_target" = "/Applications" ] \
    || die "the image's Applications link points at '$link_target' instead of /Applications"
codesign --verify --deep --strict --verbose=2 "$mounted_app"
hdiutil detach "$mount_point" >/dev/null
mount_point=

[ ! -e "$output" ] && [ ! -L "$output" ] \
    || die "the output appeared while the image was being built: $output"
mv "$pending" "$output"

printf 'Built %s\n' "$output"
printf 'Signed with %s (team %s).\n' "$identity" "$UPDATE_TEAM_ID"
printf 'Notarize, staple and checksum it before publishing; scripts/package-notarized.sh does all three.\n'
