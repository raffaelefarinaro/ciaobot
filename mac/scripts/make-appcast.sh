#!/bin/sh
# Generate the Sparkle appcast for one already packaged Ciaobot archive.
#
# usage: scripts/make-appcast.sh --archive PATH --download-url PREFIX --output PATH
#
# The archive is the notarized, Developer ID signed, arm64-only
# Ciaobot-<version>.dmg that release publishes. A notarized ZIP is still
# accepted for the older feed. The app inside either archive must be signed by
# this project's Developer ID team. This script only reads the archive: it
# mounts a DMG read-only (or unzips a ZIP) into a scratch directory, verifies
# identity, version, architecture, notarization and signing team, and then runs
# Sparkle's own generate_appcast on the archive itself so archive signatures are
# produced by the pinned tool and never hand-written. The private EdDSA key is
# read from the operator's keychain by generate_appcast; it is never mentioned
# here.
#
# A DMG is always detached, including when a check fails; a left-mounted volume
# breaks every later build on the machine.
#
# --download-url is the *prefix* the archives are published under, and it must
# end with "/": generate_appcast appends each archive's file name. Publishing
# the resulting appcast is a separate, deliberate step; this script never
# uploads, installs, launches or contacts the update feed.
#
# CIAOBOT_GENERATE_APPCAST points at an alternate generate_appcast binary. The
# test suite uses it to run a recording double instead of the pinned tool.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$root"

usage() {
    cat <<'EOF'
usage: scripts/make-appcast.sh --archive PATH --download-url PREFIX --output PATH

Generate the Sparkle appcast for one packaged Ciaobot archive.

  --archive PATH        the published, notarized, Developer ID signed,
                        arm64-only Ciaobot-<version>.dmg; a
                        Ciaobot-<version>-<flavour>.zip from an older release
                        is still accepted
  --download-url PREFIX the https URL the archives are published under; must
                        end with "/" because archive names are appended
  --output PATH         the appcast file to write; an existing non-empty file
                        is refused

Reads the archive only: it never publishes, installs, launches or contacts the
update feed. A DMG is mounted read-only for inspection and detached on every
exit path. An archive another team signed, without a team identifier, or
without a Developer ID Application authority is refused. The EdDSA private key
stays in the operator's keychain and is read by Sparkle's own generate_appcast;
signatures are never written by hand.
EOF
}

die() {
    printf 'make-appcast: %s\n' "$1" >&2
    exit 1
}

usage_error() {
    usage >&2
    exit 2
}

archive=
download_url=
output=
while [ $# -gt 0 ]; do
    case $1 in
        --archive)
            [ $# -ge 2 ] || usage_error
            archive=$2
            shift 2
            ;;
        --download-url)
            [ $# -ge 2 ] || usage_error
            download_url=$2
            shift 2
            ;;
        --output)
            [ $# -ge 2 ] || usage_error
            output=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'make-appcast: unknown argument: %s\n' "$1" >&2
            usage_error
            ;;
    esac
done

[ -n "$archive" ] || usage_error
[ -n "$download_url" ] || usage_error
[ -n "$output" ] || usage_error

# --- The archive ----------------------------------------------------------

[ -f "$archive" ] && [ ! -L "$archive" ] \
    || die "archive is missing, not a regular file, or a symlink: $archive"
[ -s "$archive" ] || die "archive is empty: $archive"

archive_name=$(basename "$archive")
case $archive_name in
    *.zip)
        archive_kind=zip
        # Ciaobot-<version>-<flavour>.zip. The version in the name is the
        # version the appcast enclosure advertises, so it has to be the version
        # inside the bundle.
        archive_version=$(printf '%s' "$archive_name" | sed -n 's/^Ciaobot-\([0-9][0-9.]*\)-.*\.zip$/\1/p')
        [ -n "$archive_version" ] \
            || die "archive name must be Ciaobot-<version>-<flavour>.zip, got: $archive_name"
        ;;
    *.dmg)
        archive_kind=dmg
        # Release publishes exactly one image, so its name is fully determined:
        # an enclosure named any other way would advertise a download that does
        # not exist.
        archive_version=$(printf '%s' "$archive_name" | sed -n 's/^Ciaobot-\([0-9][0-9.]*\)\.dmg$/\1/p')
        [ -n "$archive_version" ] \
            || die "archive name must be Ciaobot-<version>.dmg, got: $archive_name"
        ;;
    *)
        die "archive must be the published Ciaobot-<version>.dmg (or an older Ciaobot-<version>-<flavour>.zip), got: $archive_name"
        ;;
esac

# --- The download URL -----------------------------------------------------

case $download_url in
    https://*) ;;
    *) die "download URL must be https, got: $download_url" ;;
esac
rest=${download_url#https://}
case $rest in
    */*) authority=${rest%%/*} ;;
    *) die "download URL must be a prefix that ends with /: $download_url" ;;
esac
case $authority in
    ''|*@*) die "download URL must not carry credentials: $download_url" ;;
esac
case $download_url in
    *[[:space:]]*|*\?*|*\#*) die "download URL must be a plain path prefix without a query or fragment: $download_url" ;;
    */) ;;
    *) die "download URL must end with / so archive names append cleanly: $download_url" ;;
esac

# --- The output -----------------------------------------------------------

output_dir=$(dirname -- "$output")
[ -d "$output_dir" ] && [ ! -L "$output_dir" ] \
    || die "output directory does not exist: $output_dir"
[ ! -d "$output" ] || die "output is a directory, not an appcast file: $output"
[ ! -L "$output" ] || die "output must not be a symlink: $output"
if [ -e "$output" ] && [ -s "$output" ]; then
    die "refusing a non-empty output: $output"
fi

# --- The signing team -----------------------------------------------------

# Validity is not ownership: a notarized app another developer signed would pass
# every check below and could still be signed with this project's Sparkle key.
# The expected team identifier is public and committed next to the feed and key.
update_config=release/update-config.env
[ -f "$update_config" ] && [ ! -L "$update_config" ] \
    || die "release/update-config.env is missing; cannot verify the signing team"
# shellcheck disable=SC1090
. "$update_config"
[ -n "${UPDATE_TEAM_ID:-}" ] || die "release/update-config.env has no UPDATE_TEAM_ID"
case $UPDATE_TEAM_ID in
    *[!A-Z0-9]*) die "UPDATE_TEAM_ID must be a plain Apple team identifier: $UPDATE_TEAM_ID" ;;
esac

# --- The pinned tool ------------------------------------------------------

if [ -n "${CIAOBOT_GENERATE_APPCAST:-}" ]; then
    generate_appcast=$CIAOBOT_GENERATE_APPCAST
else
    generate_appcast="$root/.build/artifacts/sparkle/Sparkle/bin/generate_appcast"
fi
[ -x "$generate_appcast" ] \
    || die "generate_appcast not found; run: swift package resolve ($generate_appcast)"

# --- Inspect the archive --------------------------------------------------

scratch=$(mktemp -d "${TMPDIR:-/tmp}/ciaobot-appcast.XXXXXX")
mount_point=
cleanup() {
    if [ -n "${mount_point:-}" ]; then
        hdiutil detach "$mount_point" >/dev/null 2>&1 || true
        mount_point=
    fi
    [ -z "${scratch:-}" ] || rm -rf "$scratch"
}
trap cleanup EXIT INT TERM

if [ "$archive_kind" = dmg ]; then
    # Mount inside the scratch directory, read-only and out of Finder, so the
    # published bytes are what gets inspected. The trap detaches it on every
    # failure path.
    mount_point=$scratch/mounted
    mkdir "$mount_point"
    hdiutil attach -readonly -nobrowse -mountpoint "$mount_point" "$archive" >/dev/null \
        || die "could not mount the disk image read-only: $archive"
    app=$mount_point/Ciaobot.app
else
    ditto -x -k "$archive" "$scratch/unpacked" \
        || die "could not extract the archive: $archive"
    app=$scratch/unpacked/Ciaobot.app
fi
[ -d "$app" ] && [ ! -L "$app" ] \
    || die "archive does not contain a real Ciaobot.app directory"
plist=$app/Contents/Info.plist
[ -f "$plist" ] && [ ! -L "$plist" ] || die "archive has no Ciaobot.app/Contents/Info.plist"

identifier=$(plutil -extract CFBundleIdentifier raw -o - "$plist" 2>/dev/null || true)
[ "$identifier" = "local.ciaobot.mac" ] \
    || die "archive is not the Ciaobot app: CFBundleIdentifier is '${identifier:-missing}'"

short_version=$(plutil -extract CFBundleShortVersionString raw -o - "$plist" 2>/dev/null || true)
case $short_version in
    ''|*[!0-9.]*) die "archive has no dotted numeric CFBundleShortVersionString: '${short_version:-missing}'" ;;
esac
[ "$short_version" = "$archive_version" ] \
    || die "version mismatch: archive name says $archive_version, the bundle says $short_version"
build_version=$(plutil -extract CFBundleVersion raw -o - "$plist" 2>/dev/null || true)
case $build_version in
    ''|*[!0-9]*) die "archive has no numeric CFBundleVersion: '${build_version:-missing}'" ;;
esac

executable=$(plutil -extract CFBundleExecutable raw -o - "$plist" 2>/dev/null || true)
[ -n "$executable" ] && [ -f "$app/Contents/MacOS/$executable" ] \
    || die "archive has no Contents/MacOS/${executable:-<CFBundleExecutable>}"
architectures=$(lipo -archs "$app/Contents/MacOS/$executable" 2>/dev/null || true)
[ "$architectures" = "arm64" ] \
    || die "archive must be a thin arm64 build, got: '${architectures:-unknown}'"

# An update the client cannot launch is not an update. The signature has to
# verify inside the archive and the notarization ticket has to be stapled, so
# Sparkle's verification and Gatekeeper agree on the same code.
codesign --verify --deep --strict "$app" \
    || die "archive is not validly signed after unzipping"
xcrun stapler validate "$app" \
    || die "archive is not notarized (no stapled ticket)"
spctl --assess --type execute "$app" \
    || die "Gatekeeper does not accept the archived app"

# ...and it has to be this project's code. A notarized archive from another
# developer would satisfy everything above; only this project's Developer ID
# Application certificate may issue an update for this bundle.
# codesign -d writes its description to stderr.
description=$(codesign -d --verbose=4 "$app" 2>&1) \
    || die "could not read the archive's code signature"
case $description in
    *"Authority=Developer ID Application"*) ;;
    *) die "archive is not signed with a Developer ID Application certificate; refusing an update that another certificate issued" ;;
esac
signed_team=$(printf '%s\n' "$description" | sed -n 's/^TeamIdentifier=//p' | head -n 1)
case $signed_team in
    ''|"not set") die "archive has no TeamIdentifier; refusing an update that team $UPDATE_TEAM_ID did not sign" ;;
esac
[ "$signed_team" = "$UPDATE_TEAM_ID" ] \
    || die "archive was signed by team $signed_team, not $UPDATE_TEAM_ID; refusing another developer's build"

# The volume has done its job. Detach before the feed is written so a failure in
# generate_appcast can never leave a volume mounted.
if [ -n "${mount_point:-}" ]; then
    hdiutil detach "$mount_point" >/dev/null \
        || die "could not detach the disk image: $mount_point"
    mount_point=
fi

# --- Generate the appcast -------------------------------------------------

# generate_appcast reads a directory of archives and writes the appcast beside
# them, so stage this one archive under its published name. For a DMG that is
# the image itself: Sparkle signs the bytes that ship, never a repack.
stage=$scratch/feed
mkdir "$stage"
cp "$archive" "$stage/$archive_name"
appcast_name=appcast.xml
# generate_appcast resolves a relative -o against the process working
# directory, not against the archives directory, so pass the absolute path the
# script then checks. A relative name silently wrote the feed into the caller's
# directory instead.
generated=$stage/$appcast_name
"$generate_appcast" \
    --download-url-prefix "$download_url" \
    -o "$generated" \
    "$stage" \
    || die "generate_appcast failed"

[ -f "$generated" ] && [ -s "$generated" ] \
    || die "generate_appcast did not write $generated"

mv "$generated" "$output"
[ -s "$output" ] || die "appcast output is empty: $output"
printf 'Generated %s\n' "$output"
