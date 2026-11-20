#!/bin/sh
# Copy the resolved Sparkle framework slice for one architecture into an app
# bundle's Contents/Frameworks. Sparkle ships as an xcframework in SwiftPM's
# artifact cache; its framework contains symlinks and executable helper
# tools, so a plain recursive copy of the xcframework is not sufficient.
#
# usage: scripts/install-sparkle-framework.sh XCFRAMEWORK ARCH FRAMEWORKS_DIR
#
# Refuses an ambiguous or missing architecture slice and an existing
# destination. It never downloads anything, signs anything, launches anything,
# or writes outside FRAMEWORKS_DIR/Sparkle.framework.
#
# Trust boundary: FRAMEWORKS_DIR is chosen by the caller and is trusted only for
# its own location. Every path component from the app bundle down is checked for
# symlinks, so a link placed inside that tree cannot redirect the copy. A caller
# that reaches the bundle through a symlink it created itself has simply chosen
# that physical destination; the build passes a fresh physical .build path.
set -eu

usage() {
    printf 'usage: %s XCFRAMEWORK ARCH FRAMEWORKS_DIR\n' "$0" >&2
    exit 2
}

[ $# -eq 3 ] || usage
xcframework=$1
arch=$2
frameworks_dir=$3
[ -n "$xcframework" ] && [ -n "$arch" ] && [ -n "$frameworks_dir" ] || usage

case $arch in
    ''|*[!A-Za-z0-9_]*) printf 'install-sparkle-framework: invalid architecture: %s\n' "$arch" >&2; exit 1 ;;
esac
case $xcframework in
    /*) ;;
    *) xcframework=$PWD/$xcframework ;;
esac
[ -d "$xcframework" ] || {
    printf 'install-sparkle-framework: no Sparkle.xcframework at %s\n' "$xcframework" >&2
    exit 1
}
xcframework=$(CDPATH= cd -- "$xcframework" && pwd -P)
info=$xcframework/Info.plist
[ -f "$info" ] || {
    printf 'install-sparkle-framework: no Info.plist in %s\n' "$xcframework" >&2
    exit 1
}
plist_buddy=/usr/libexec/PlistBuddy
[ -x "$plist_buddy" ] || {
    printf 'install-sparkle-framework: %s is required\n' "$plist_buddy" >&2
    exit 1
}

# Select exactly one macOS library whose SupportedArchitectures contains the
# exact requested architecture. Any two matches are ambiguous: the app would
# silently link a framework built for the wrong platform or slice.
selected=
selected_identifier=
index=0
while identifier=$("$plist_buddy" -c "Print :AvailableLibraries:$index:LibraryIdentifier" "$info" 2>/dev/null); do
    platform=$("$plist_buddy" -c "Print :AvailableLibraries:$index:SupportedPlatform" "$info" 2>/dev/null || true)
    if [ "$platform" = "macos" ]; then
        architectures=$("$plist_buddy" -c "Print :AvailableLibraries:$index:SupportedArchitectures" "$info" 2>/dev/null || true)
        if printf '%s\n' "$architectures" | tr -d ' \t' | grep -qx "$arch"; then
            case $identifier in
                ''|.|..|*[!A-Za-z0-9_.-]*)
                    printf 'install-sparkle-framework: invalid library identifier: %s\n' "$identifier" >&2
                    exit 1
                    ;;
            esac
            library_path=$("$plist_buddy" -c "Print :AvailableLibraries:$index:LibraryPath" "$info" 2>/dev/null || true)
            binary_path=$("$plist_buddy" -c "Print :AvailableLibraries:$index:BinaryPath" "$info" 2>/dev/null || true)
            case $library_path in
                ''|.|..|*/*|*[!A-Za-z0-9_.-]*)
                    printf 'install-sparkle-framework: invalid LibraryPath for %s\n' "$identifier" >&2
                    exit 1
                    ;;
            esac
            case $binary_path in
                ''|/*|..|../*|*/..|*/../*) printf 'install-sparkle-framework: invalid BinaryPath for %s\n' "$identifier" >&2; exit 1 ;;
            esac
            [ -d "$xcframework/$identifier/$library_path" ] || {
                printf 'install-sparkle-framework: %s has no %s/%s\n' "$identifier" "$identifier" "$library_path" >&2
                exit 1
            }
            [ -f "$xcframework/$identifier/$binary_path" ] || {
                printf 'install-sparkle-framework: %s has no executable at %s/%s\n' "$identifier" "$identifier" "$binary_path" >&2
                exit 1
            }
            if [ -n "$selected" ]; then
                printf 'install-sparkle-framework: ambiguous macOS Sparkle slices for %s: %s and %s\n' \
                    "$arch" "$selected_identifier" "$identifier" >&2
                exit 1
            fi
            selected=$xcframework/$identifier/$library_path
            selected_identifier=$identifier
        fi
    fi
    index=$((index + 1))
done

if [ -z "$selected" ]; then
    printf 'install-sparkle-framework: no macOS Sparkle slice supports %s in %s\n' "$arch" "$xcframework" >&2
    exit 1
fi

case $frameworks_dir in
    /*) ;;
    *) frameworks_dir=$PWD/$frameworks_dir ;;
esac
frameworks_dir=${frameworks_dir%/}
case $frameworks_dir in
    */Contents/Frameworks) ;;
    *)
        printf 'install-sparkle-framework: Frameworks directory must be APP/Contents/Frameworks: %s\n' "$frameworks_dir" >&2
        exit 1
        ;;
esac
[ ! -L "$frameworks_dir" ] || {
    printf 'install-sparkle-framework: refusing a symlinked Frameworks directory: %s\n' "$frameworks_dir" >&2
    exit 1
}
destination=$frameworks_dir/Sparkle.framework
if [ -e "$destination" ] || [ -L "$destination" ]; then
    printf 'install-sparkle-framework: refusing to replace %s\n' "$destination" >&2
    exit 1
fi
app_bundle=$(dirname -- "$(dirname -- "$frameworks_dir")")
[ ! -L "$app_bundle" ] || {
    printf 'install-sparkle-framework: refusing a symlinked app bundle: %s\n' "$app_bundle" >&2
    exit 1
}
[ ! -L "$app_bundle/Contents" ] || {
    printf 'install-sparkle-framework: refusing a symlinked app Contents: %s\n' "$app_bundle/Contents" >&2
    exit 1
}
mkdir -p "$frameworks_dir"

# ditto preserves symlinks, executable bits and the framework's versioned
# layout. The Sparkle framework is distributed with a Versions/B directory and
# top-level symlinks (Sparkle, Resources, Updater.app, XPCServices, ...) that
# codesign and dyld both expect.
ditto "$selected" "$destination"

# Prove the copy is still a versioned framework. A dereferenced symlink would
# also pass a plain directory check but break the bundle's seal and runtime.
for link in Autoupdate Headers Modules PrivateHeaders Resources Sparkle Updater.app XPCServices Versions/Current; do
    [ -L "$destination/$link" ] || {
        printf 'install-sparkle-framework: copied framework lost symlink %s\n' "$link" >&2
        exit 1
    }
done
[ -x "$destination/Versions/B/Sparkle" ] || {
    printf 'install-sparkle-framework: copied Sparkle binary is not executable\n' >&2
    exit 1
}
for helper in Versions/B/Autoupdate Versions/B/Updater.app/Contents/MacOS/Updater \
    Versions/B/XPCServices/Installer.xpc/Contents/MacOS/Installer \
    Versions/B/XPCServices/Downloader.xpc/Contents/MacOS/Downloader; do
    [ -x "$destination/$helper" ] || {
        printf 'install-sparkle-framework: copied helper is missing or not executable: %s\n' "$helper" >&2
        exit 1
    }
done

printf '%s\n' "$destination"
