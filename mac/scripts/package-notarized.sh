#!/bin/sh
# Build and notarize in a fresh directory; never install or modify an app.
#
# One artifact ships: the notarized, stapled Ciaobot-<version>.dmg. Its volume
# holds the app next to an `Applications` symlink so the app is installed before
# it is launched; a copy run from the download folder is translocated by macOS
# and Sparkle then refuses to update it. The image is also Sparkle's update
# enclosure, so there is one file to download and one file to verify.
#
# CIAOBOT_ALSO_ZIP=1 additionally produces Ciaobot-<version>-notarized.zip for
# anyone who still needs an enclosure archive; the release flow never sets it.
# No .sha256 files are written — the digests are printed for the release notes,
# and GitHub publishes a digest per asset anyway.
#
# Authentication: an App Store Connect API key is preferred, because it needs
# no keychain item and no interactive approval. Set NOTARY_KEY_ID and
# NOTARY_ISSUER (and optionally NOTARY_KEY for the .p8 path; the default is the
# conventional ~/.appstoreconnect/private_keys/AuthKey_<KEY_ID>.p8). A
# notarytool keychain profile is still accepted through NOTARY_PROFILE as a
# fallback. Nothing here reads, prints or stores a password.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$root"
: "${SIGNING_IDENTITY:?Set SIGNING_IDENTITY to a Developer ID Application identity}"

die() {
    printf 'package-notarized: %s\n' "$1" >&2
    exit 1
}

# Resolve one authentication method before doing any work, so a missing
# credential fails before a long build rather than after it.
notary_key=
if [ -n "${NOTARY_KEY_ID:-}" ] && [ -n "${NOTARY_ISSUER:-}" ]; then
    notary_key=${NOTARY_KEY:-$HOME/.appstoreconnect/private_keys/AuthKey_$NOTARY_KEY_ID.p8}
    case $notary_key in
        *.p8) ;;
        *) die "the API key must be a .p8 file: $notary_key" ;;
    esac
    [ -r "$notary_key" ] || die "cannot read the API key at $notary_key"
elif [ -n "${NOTARY_PROFILE:-}" ]; then
    :
else
    die "set NOTARY_KEY_ID and NOTARY_ISSUER (App Store Connect API key), or NOTARY_PROFILE"
fi

submit_for_notarization() {
    notary_artifact=$1
    notary_result=$2
    shift 2
    xcrun notarytool submit "$notary_artifact" "$@" --wait --output-format json \
        > "$notary_result"
    python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print("Notarization:", r.get("status"), "submission:", r.get("id")); sys.exit(0 if r.get("status") == "Accepted" else 1)' "$notary_result"
}

scripts/build-app.sh
output=$(mktemp -d "${TMPDIR:-/tmp}/ciaobot-notarized.XXXXXX")
bundle=$output/Ciaobot.app
ditto .build/Ciaobot.app "$bundle"
chmod -R u+w "$bundle"
xattr -cr "$bundle" 2>/dev/null || true
# Sign Sparkle's nested helpers inside-out before the app. Signing the app
# first would make the framework's later re-signing invalidate the app seal.
sparkle=$bundle/Contents/Frameworks/Sparkle.framework
if [ -d "$sparkle" ]; then
    scripts/sign-sparkle.sh --distribution "$SIGNING_IDENTITY" "$sparkle"
fi
codesign --force --options runtime --timestamp --sign "$SIGNING_IDENTITY" "$bundle"
codesign --verify --deep --strict --verbose=2 "$bundle"
version=$(plutil -extract CFBundleShortVersionString raw -o - "$bundle/Contents/Info.plist")
case $version in ''|*[!0-9.]*) echo 'Invalid bundle version' >&2; exit 1 ;; esac
submission=$output/submission.zip
ditto -c -k --keepParent "$bundle" "$submission"
if [ -n "$notary_key" ]; then
    submit_for_notarization "$submission" "$output/notary-result.json" \
        --key "$notary_key" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER"
else
    submit_for_notarization "$submission" "$output/notary-result.json" \
        --keychain-profile "$NOTARY_PROFILE"
fi
xcrun stapler staple "$bundle"
xcrun stapler validate "$bundle"
spctl --assess --type execute --verbose=4 "$bundle"

# The ZIP is opt-in. When it is asked for it is verified exactly as before; the
# release flow does not ask, because the DMG is the enclosure.
archive=$output/Ciaobot-$version-notarized.zip
if [ "${CIAOBOT_ALSO_ZIP:-0}" = 1 ]; then
    ditto -c -k --keepParent "$bundle" "$archive"
    mkdir "$output/verify"
    ditto -x -k "$archive" "$output/verify"
    codesign --verify --deep --strict --verbose=2 "$output/verify/Ciaobot.app"
    xcrun stapler validate "$output/verify/Ciaobot.app"
    spctl --assess --type execute --verbose=4 "$output/verify/Ciaobot.app"
    printf 'Verified optional update archive: %s\n' "$archive"
fi

# --- The disk image -------------------------------------------------------

# The image is what a person downloads and what Sparkle downloads: its volume
# holds Ciaobot.app next to an Applications symlink, so installing it is the
# obvious action and updates work from /Applications. It is built from the
# stapled bundle, signed with the same Developer ID, notarized on its own with
# the same authentication, and stapled. Its digest is printed afterwards:
# stapling writes the ticket into the file, so a digest taken earlier would
# never match the published image.
dmg=$output/Ciaobot-$version.dmg
scripts/make-dmg.sh --app "$bundle" --output "$dmg" --identity "$SIGNING_IDENTITY"
if [ -n "$notary_key" ]; then
    submit_for_notarization "$dmg" "$output/notary-result-dmg.json" \
        --key "$notary_key" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER"
else
    submit_for_notarization "$dmg" "$output/notary-result-dmg.json" \
        --keychain-profile "$NOTARY_PROFILE"
fi
xcrun stapler staple "$dmg"
xcrun stapler validate "$dmg"
spctl --assess --type open --context context:primary-signature --verbose=4 "$dmg"

printf 'Verified notarized disk image: %s\n' "$dmg"
if [ -f "$archive" ]; then
    printf 'Verified optional update archive: %s\n' "$archive"
fi
printf 'Artifacts (publish exactly one asset, the DMG):\n'
printf '  %s\n' "$dmg"
printf '  SHA-256 %s\n' "$(shasum -a 256 "$dmg" | awk '{print $1}')"
if [ -f "$archive" ]; then
    printf '  %s\n' "$archive"
    printf '  SHA-256 %s\n' "$(shasum -a 256 "$archive" | awk '{print $1}')"
fi
printf 'Record the digest in the release notes; do not upload a .sha256 file.\n'
