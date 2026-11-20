#!/bin/sh
set -eu
cd -P "$(dirname "$0")/.."

# The bundled engine is built from HEAD by scripts/engine_build.py, so its
# prerequisites are checked first: a late failure would waste the Swift build.
# The interpreter is the first of python3.12 and python3.13 on PATH, else
# python3 when it is 3.12 or 3.13; the driver pins the same allowlist.
engine_python=
for candidate in python3.12 python3.13; do
    if engine_python=$(command -v "$candidate" 2>/dev/null); then
        break
    fi
    engine_python=
done
if [ -z "$engine_python" ]; then
    found=none
    if command -v python3 >/dev/null 2>&1; then
        found=$(python3 -I -S -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || found=unknown
        case $found in
            3.12|3.13) engine_python=$(command -v python3) ;;
        esac
    fi
    [ -n "$engine_python" ] \
        || { echo "engine build needs Python 3.12 or 3.13 (found $found)" >&2; exit 1; }
fi
command -v node >/dev/null && command -v npm >/dev/null \
    || { echo "the engine build needs node and npm on PATH" >&2; exit 1; }
# Git runs with its location and configuration variables scrubbed, as the driver does.
engine_git() {
    env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_CONFIG_COUNT -u GIT_CONFIG_PARAMETERS \
        -u GIT_CONFIG_GLOBAL -u GIT_CONFIG_SYSTEM -u GIT_CONFIG_NOSYSTEM \
        /usr/bin/git -c core.fsmonitor=false "$@"
}
engine_dirty=$(engine_git status --porcelain --untracked-files=no) \
    || { echo "could not read the git status of the checkout" >&2; exit 1; }
[ -z "$engine_dirty" ] \
    || { echo "engine build failed: commit or stash tracked changes; the engine is built from HEAD" >&2; exit 1; }
engine_hidden=$(engine_git ls-files -v | awk '/^[a-zS] / { print $2 }') \
    || { echo "could not list the tracked files of the checkout" >&2; exit 1; }
[ -z "$engine_hidden" ] \
    || { echo "engine build failed: tracked files are marked skip-worktree or assume-unchanged; the engine is built from HEAD" >&2; exit 1; }

build_dir="$PWD/.build"
# Refuse a symlinked .build before SwiftPM or this script writes through it.
if [ -e "$build_dir" ] || [ -L "$build_dir" ]; then
    [ -d "$build_dir" ] && [ ! -L "$build_dir" ] \
        || { echo "refusing symlinked build directory: $build_dir" >&2; exit 1; }
fi

# The app's versions come from the committed project version in
# ../pyproject.toml. CFBundleVersion is major*10000 + minor*100 + patch, so
# 2.0.0 is build 20000, above the build 14 that 0.5.0 shipped. Minor and patch
# must stay below 100 or two releases could share a build number, and any
# other shape is refused here, before the Swift build.
[ -f ../pyproject.toml ] && [ ! -L ../pyproject.toml ] \
    || { echo "pyproject.toml is missing or a symlink next to the mac/ directory" >&2; exit 1; }
version_lines=$(grep -c '^version = ' ../pyproject.toml || true)
[ "$version_lines" = 1 ] \
    || { echo "pyproject.toml must have exactly one 'version = ' line, found $version_lines" >&2; exit 1; }
short_version=$(sed -n 's/^version = "\(.*\)"$/\1/p' ../pyproject.toml)
printf '%s' "$short_version" \
    | grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
    || { echo "pyproject.toml version must be MAJOR.MINOR.PATCH, got: $short_version" >&2; exit 1; }
version_major=${short_version%%.*}
version_rest=${short_version#*.}
version_minor=${version_rest%%.*}
version_patch=${version_rest#*.}
[ "$version_minor" -lt 100 ] && [ "$version_patch" -lt 100 ] \
    || { echo "pyproject.toml version $short_version: minor and patch must each be below 100" >&2; exit 1; }
bundle_build=$((version_major * 10000 + version_minor * 100 + version_patch))

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
<key>CFBundleShortVersionString</key><string>$short_version</string>
<key>CFBundleVersion</key><string>$bundle_build</string>
<key>LSMinimumSystemVersion</key><string>$minimum_system_version</string>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
<key>NSHighResolutionCapable</key><true/>
<key>NSPrincipalClass</key><string>NSApplication</string>
$update_plist
</dict></plist>
PLIST
plutil -lint "$bundle/Contents/Info.plist"
test -s "$resources/AppIcon.icns"

# The bundled engine: a relocatable CPython 3.12 prefix with this commit's
# ciaobot wheel and PWA, built only from the committed pins by
# scripts/engine_build.py (network on a cold cache). Its prerequisites were
# checked at the top of this script. There is no option to skip it.
engine_cache="$build_dir/engine-cache"
mkdir -p "$engine_cache"
[ -d "$engine_cache" ] && [ ! -L "$engine_cache" ] \
    || { echo "refusing a symlinked engine cache: $engine_cache" >&2; exit 1; }
chmod 700 "$engine_cache"
engine_build="$staging_root/engine-build"
"$engine_python" -I -S scripts/engine_build.py --cache "$engine_cache" --destination "$engine_build"
mv "$engine_build/engine" "$resources/engine"
# The moved tree must still equal the assembly receipt, and its version must be
# the committed project version (never the engine's own provenance).
engine_expected=$("$engine_python" -I -S scripts/engine_build.py --verify "$resources/engine" \
    "$engine_build/assembly-receipt.json") \
    || { echo "bundled engine does not match its assembly receipt" >&2; exit 1; }
engine_actual=$(env -i "$resources/engine/bin/ciao" --version)
[ "$engine_actual" = "$engine_expected" ] \
    || { echo "bundled engine reports '$engine_actual', expected '$engine_expected'" >&2; exit 1; }
"$engine_python" -I -S -c 'import json, sys; t = json.load(open(sys.argv[1]))["totals"]; print("Engine: %d files (%d Mach-O), %d bytes" % (t["files"], t["macho_files"], t["bytes"]))' \
    "$engine_build/assembly-receipt.json"

# Full validation is complete. Publish by renaming the previous entry into a
# fresh quarantine directory (mv never follows it and deletes nothing), then
# rename this run's validated staging bundle into place.
[ -d "$bundle" ] && [ ! -L "$bundle" ] \
    || { echo "staged app is not a real directory: $bundle" >&2; exit 1; }
[ -f "$bundle/Contents/Info.plist" ] && [ ! -L "$bundle/Contents/Info.plist" ] \
    || { echo "staged Info.plist is not a regular file: $bundle/Contents/Info.plist" >&2; exit 1; }
[ -x "$bundle/Contents/Resources/engine/bin/ciao" ] && [ ! -L "$bundle/Contents/Resources/engine" ] \
    || { echo "staged app has no bundled engine" >&2; exit 1; }
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
