#!/bin/sh
set -eu
cd -P "$(dirname "$0")/.."

build_dir="$PWD/.build"
# Refuse a symlinked .build before SwiftPM or this script writes through it.
if [ -e "$build_dir" ] || [ -L "$build_dir" ]; then
    [ -d "$build_dir" ] && [ ! -L "$build_dir" ] \
        || { echo "refusing symlinked build directory: $build_dir" >&2; exit 1; }
fi

swift build -c release
binary_dir=$(swift build -c release --show-bin-path)
[ -d "$build_dir" ] && [ ! -L "$build_dir" ] \
    || { echo "refusing symlinked build directory: $build_dir" >&2; exit 1; }

# Build in a private staging tree. Nothing here touches a pre-existing
# .build/Ciaobot.app; a failure can only remove this run's mktemp directory.
staging_root=
iconset_root=
cleanup() {
    [ -z "$staging_root" ] || rm -rf "$staging_root"
    [ -z "$iconset_root" ] || rm -rf "$iconset_root"
}
trap cleanup EXIT INT TERM
staging_root=$(mktemp -d "$build_dir/ciaobot-app.XXXXXX")
bundle="$staging_root/Ciaobot.app"
resources="$bundle/Contents/Resources"
[ ! -e "$bundle" ] && [ ! -L "$bundle" ] \
    || { echo "refusing a pre-existing staging bundle: $bundle" >&2; exit 1; }
mkdir -p "$bundle/Contents/MacOS" "$resources"
[ ! -L "$bundle" ] && [ ! -L "$bundle/Contents" ] \
    && [ ! -L "$bundle/Contents/MacOS" ] && [ ! -L "$resources" ] \
    || { echo "refusing symlinked staged app directory under $bundle" >&2; exit 1; }

cp "$binary_dir/CiaobotMac" "$bundle/Contents/MacOS/CiaobotMac"

# Embed Sparkle exactly as SwiftPM resolved it for this architecture. The
# pinned package's artifact is an xcframework; the helper copies only the
# matching macOS slice with its symlinks and executable helper tools intact.
# The framework is not started by this build: no updater object is created.
artifact_root="$build_dir/artifacts"
xcframework=
xcframework_count=0
while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    xcframework_count=$((xcframework_count + 1))
    xcframework=$candidate
done <<EOF
$(find "$artifact_root" -type d -name Sparkle.xcframework -print 2>/dev/null || true)
EOF
if [ "$xcframework_count" -eq 0 ]; then
    echo "Sparkle.xcframework not found under $artifact_root; run: swift package resolve" >&2
    exit 1
fi
[ "$xcframework_count" -eq 1 ] || {
    echo "ambiguous Sparkle.xcframework under $artifact_root: expected exactly one resolved artifact" >&2
    exit 1
}
frameworks="$bundle/Contents/Frameworks"
mkdir -p "$frameworks"
[ -d "$frameworks" ] && [ ! -L "$frameworks" ] \
    || { echo "Frameworks is not a real directory: $frameworks" >&2; exit 1; }
scripts/install-sparkle-framework.sh "$xcframework" "$(uname -m)" "$frameworks" >/dev/null

command -v otool >/dev/null || { echo "otool is required to verify Sparkle linkage" >&2; exit 1; }
otool -L "$bundle/Contents/MacOS/CiaobotMac" \
    | grep -Fq '@rpath/Sparkle.framework/Versions/B/Sparkle' \
    || { echo "CiaobotMac does not link @rpath/Sparkle.framework" >&2; exit 1; }
otool -l "$bundle/Contents/MacOS/CiaobotMac" \
    | awk '/LC_RPATH/ { in_rpath = 1; next } in_rpath && /path / { print $2; in_rpath = 0 }' \
    | grep -Fxq '@executable_path/../Frameworks' \
    || { echo "CiaobotMac has no @executable_path/../Frameworks runpath" >&2; exit 1; }
[ -x "$frameworks/Sparkle.framework/Versions/B/Sparkle" ] \
    || { echo "embedded Sparkle framework is not executable" >&2; exit 1; }
[ -L "$frameworks/Sparkle.framework/Versions/Current" ] \
    || { echo "embedded Sparkle framework lost its version symlink" >&2; exit 1; }

# Package the artwork with the app. The build fails here if SwiftPM did not
# produce the resource bundle, rather than shipping a title without it. This
# tree is fresh, so there is nothing to delete first.
resource_bundle="CiaobotMac_CiaobotMac.bundle"
cp -R "$binary_dir/$resource_bundle" "$resources/$resource_bundle"

# Flat copies are also resolved by AppAssets inside a .app.
cp "Sources/CiaobotMac/Resources/app-icon.png" "$resources/app-icon.png"
cp "Sources/CiaobotMac/Resources/mark_template.png" "$resources/mark_template.png"
cp "Sources/CiaobotMac/Resources/ciaobot-wordmark.png" "$resources/ciaobot-wordmark.png"
cp "Sources/CiaobotMac/Resources/ciaobot-wordmark-light.png" "$resources/ciaobot-wordmark-light.png"
# The running icon repaints the cream field behind this ink mask. It is a
# separate resource: AppIcon.icns below is still generated from the unchanged
# app-icon.png.
cp "Sources/CiaobotMac/Resources/dock-icon-ink.png" "$resources/dock-icon-ink.png"
cp "Sources/CiaobotMac/Resources/dock-icon-dots.png" "$resources/dock-icon-dots.png"

# Native Finder/Dock icon. It is generated from the brand artwork snapshot in
# Sources/CiaobotMac/Resources by scripts/make-icons.swift, never read from the
# engine checkout, so the bundle build needs no running server and no
# third-party tooling. The icon has transparent rounded corners (macOS shows a
# native .icns as-is); fail here rather than ship a full-bleed square tile.
command -v sips >/dev/null || { echo "sips is required to build AppIcon.icns" >&2; exit 1; }
command -v iconutil >/dev/null || { echo "iconutil is required to build AppIcon.icns" >&2; exit 1; }
iconset_root=$(mktemp -d "${TMPDIR:-/tmp}/ciaobot-iconset.XXXXXX")
iconset="$iconset_root/AppIcon.iconset"
mkdir -p "$iconset"
icon_source="Sources/CiaobotMac/Resources/app-icon.png"
if [ "$(sips -g hasAlpha "$icon_source" | awk '/hasAlpha/ { print $2 }')" != "yes" ]; then
    echo "app-icon.png must have transparent rounded corners" >&2
    exit 1
fi
for size in 16 32 128 256 512; do
    double=$((size * 2))
    sips -z "$size" "$size" "$icon_source" --out "$iconset/icon_${size}x${size}.png" >/dev/null
    sips -z "$double" "$double" "$icon_source" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$resources/AppIcon.icns"

# Sparkle updater keys are opt-in. Without CIAOBOT_RELEASE_UPDATES=1 the
# bundle carries no feed and no public key, so ClientUpdater refuses to start
# and the client never contacts the update feed. The values are public and
# committed in release/update-config.env; the EdDSA private key is never read
# or written here, because it lives only in the operator's keychain.
update_plist=
minimum_system_version=13.0
if [ "${CIAOBOT_RELEASE_UPDATES:-0}" = "1" ]; then
    update_config=release/update-config.env
    [ -f "$update_config" ] && [ ! -L "$update_config" ] \
        || { echo "release/update-config.env is missing; refusing to build an update-enabled app" >&2; exit 1; }
    # shellcheck disable=SC1090
    . "$update_config"
    : "${UPDATE_FEED_URL:?update-config.env has no UPDATE_FEED_URL}"
    : "${UPDATE_PUBLIC_ED_KEY:?update-config.env has no UPDATE_PUBLIC_ED_KEY}"
    : "${UPDATE_MIN_SYSTEM_VERSION:?update-config.env has no UPDATE_MIN_SYSTEM_VERSION}"
    # Only a credential-free https URL without XML-hostile characters may be
    # embedded in the plist.
    printf '%s' "$UPDATE_FEED_URL" \
        | grep -Eq '^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]+)?(/[A-Za-z0-9._~%+-]+)+/?$' \
        || { echo "UPDATE_FEED_URL must be a credential-free https URL, got: $UPDATE_FEED_URL" >&2; exit 1; }
    # A Sparkle EdDSA public key is the 32-byte key in base64.
    printf '%s' "$UPDATE_PUBLIC_ED_KEY" \
        | grep -Eq '^[A-Za-z0-9+/]{43}=$' \
        || { echo "UPDATE_PUBLIC_ED_KEY must be a base64 EdDSA public key" >&2; exit 1; }
    # The public minimum system version has to be the one the bundle ships,
    # or the appcast and the app would disagree about who can install it.
    [ "$UPDATE_MIN_SYSTEM_VERSION" = "$minimum_system_version" ] \
        || { echo "UPDATE_MIN_SYSTEM_VERSION must match LSMinimumSystemVersion $minimum_system_version, got: $UPDATE_MIN_SYSTEM_VERSION" >&2; exit 1; }
    update_plist="<key>SUFeedURL</key><string>$UPDATE_FEED_URL</string>
<key>SUPublicEDKey</key><string>$UPDATE_PUBLIC_ED_KEY</string>
<key>SUEnableAutomaticChecks</key><true/>
<key>SUAutomaticallyUpdate</key><true/>
<key>SUAllowsAutomaticUpdates</key><true/>
<key>SUScheduledCheckInterval</key><integer>3600</integer>
<key>SUVerifyUpdateBeforeExtraction</key><true/>
<key>SURequireSignedFeed</key><false/>
<key>SUEnableSystemProfiling</key><false/>"
fi

cat > "$bundle/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>CiaobotMac</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>CFBundleIdentifier</key><string>local.ciaobot.mac</string>
<key>CFBundleName</key><string>Ciaobot</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.5.0</string>
<key>CFBundleVersion</key><string>14</string>
<key>LSMinimumSystemVersion</key><string>$minimum_system_version</string>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
<key>NSHighResolutionCapable</key><true/>
<key>NSPrincipalClass</key><string>NSApplication</string>
$update_plist
</dict></plist>
PLIST
plutil -lint "$bundle/Contents/Info.plist"
test -s "$resources/AppIcon.icns"

# Full validation is complete. Publish by renaming the previous entry into a
# fresh quarantine directory (mv never follows it and deletes nothing), then
# rename this run's validated staging bundle into place.
[ -d "$bundle" ] && [ ! -L "$bundle" ] \
    || { echo "staged app is not a real directory: $bundle" >&2; exit 1; }
[ -f "$bundle/Contents/Info.plist" ] && [ ! -L "$bundle/Contents/Info.plist" ] \
    || { echo "staged Info.plist is not a regular file: $bundle/Contents/Info.plist" >&2; exit 1; }
published="$build_dir/Ciaobot.app"
if [ -e "$published" ] || [ -L "$published" ]; then
    quarantine_root=$(mktemp -d "$build_dir/ciaobot-quarantine.XXXXXX")
    [ -d "$quarantine_root" ] && [ ! -L "$quarantine_root" ] \
        || { echo "could not create quarantine directory: $quarantine_root" >&2; exit 1; }
    mv "$published" "$quarantine_root/Ciaobot.app"
    printf 'Quarantined previous app: %s\n' "$quarantine_root/Ciaobot.app"
fi
mv "$bundle" "$published"
[ -d "$published" ] && [ ! -L "$published" ] \
    || { echo "published app is not a real directory: $published" >&2; exit 1; }
printf 'Built %s\n' "$published"
