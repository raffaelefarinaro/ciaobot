#!/bin/sh
# Sign Sparkle's nested helpers inside-out, then the framework itself.
#
# usage: scripts/sign-sparkle.sh --adhoc FRAMEWORK
#        scripts/sign-sparkle.sh --distribution IDENTITY FRAMEWORK
#
# Sparkle 2.10.1 ships Installer.xpc, Downloader.xpc, Autoupdate and
# Updater.app inside Sparkle.framework/Versions/B. Signing order follows
# https://sparkle-project.org/documentation/sandboxing/#code-signing: each
# helper first, then the enclosing framework. --deep is never used to sign:
# it would apply the Downloader XPC's entitlement policy to every nested
# binary. The framework itself is never re-laid-out here; callers are expected
# to have copied the resolved artifact with symlinks intact.
#
# Trust boundary: the caller supplies the app bundle path and is trusted to name
# the location it means to sign. Every component below that bundle is checked,
# so a link placed inside the bundle cannot redirect codesign elsewhere. A caller
# that reaches the bundle through an ancestor symlink of its own choosing has
# selected that physical app; the build and packaging scripts pass fresh paths.
set -eu

usage() {
    cat <<'EOF'
usage: scripts/sign-sparkle.sh --adhoc FRAMEWORK
       scripts/sign-sparkle.sh --distribution IDENTITY FRAMEWORK

FRAMEWORK must be APP/Contents/Frameworks/Sparkle.framework inside a real app
bundle. Every enclosing component is checked before codesign runs.

  --adhoc                 re-sign ad-hoc for local packaging. The app is not
                          hardened, so it can load the framework locally.
  --distribution IDENTITY sign with a Developer ID identity, hardened runtime
                          and a secure timestamp for notarization.

Never installs, launches or contacts the network.
EOF
}

die() {
    printf 'sign-sparkle: %s\n' "$1" >&2
    exit 1
}

[ $# -ge 2 ] || { usage >&2; exit 2; }
mode=$1
shift
case $mode in
    --adhoc)
        [ $# -eq 1 ] || { usage >&2; exit 2; }
        identity=
        ;;
    --distribution)
        [ $# -eq 2 ] || { usage >&2; exit 2; }
        identity=$1
        shift
        [ -n "$identity" ] || die "an empty signing identity is not a distribution identity"
        ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
framework_arg=$1
framework_arg=${framework_arg%/}
case $framework_arg in
    */Contents/Frameworks/Sparkle.framework) ;;
    *) die "refusing a framework outside an app's Contents/Frameworks: $framework_arg" ;;
esac
bundle_arg=${framework_arg%/Contents/Frameworks/Sparkle.framework}
[ -n "$bundle_arg" ] || die "cannot derive the app bundle from $framework_arg"
[ -d "$bundle_arg" ] || die "no app bundle at $bundle_arg"
[ -L "$bundle_arg" ] && die "refusing a symlinked app bundle: $bundle_arg"
[ -d "$bundle_arg/Contents" ] || die "missing app Contents: $bundle_arg/Contents"
[ -L "$bundle_arg/Contents" ] && die "refusing a symlinked app Contents: $bundle_arg/Contents"
[ -d "$bundle_arg/Contents/Frameworks" ] \
    || die "missing app Frameworks directory: $bundle_arg/Contents/Frameworks"
[ -L "$bundle_arg/Contents/Frameworks" ] \
    && die "refusing a symlinked app Frameworks directory: $bundle_arg/Contents/Frameworks"
[ -d "$framework_arg" ] || die "no framework at $framework_arg"
[ -L "$framework_arg" ] && die "refusing a symlinked framework path: $framework_arg"
framework=$(CDPATH= cd -- "$framework_arg" && pwd -P)
bundle=$(CDPATH= cd -- "$bundle_arg" && pwd -P)
[ "$framework" = "$bundle/Contents/Frameworks/Sparkle.framework" ] \
    || die "framework does not resolve to the app's own Frameworks directory: $framework"
case $framework in
    /Applications/*|"${HOME:-/nonexistent}"/Applications/*)
        die "refusing to sign a framework inside an Applications folder: $framework" ;;
esac
[ -d "$framework/Versions/B" ] || die "$framework has no Versions/B"

installer=$framework/Versions/B/XPCServices/Installer.xpc
downloader=$framework/Versions/B/XPCServices/Downloader.xpc
autoupdate=$framework/Versions/B/Autoupdate
updater=$framework/Versions/B/Updater.app
for path in "$installer" "$downloader" "$autoupdate" "$updater"; do
    [ -e "$path" ] || die "missing Sparkle helper: $path"
done

# Everything below runs before the first codesign invocation. The copied
# framework is allowed to keep Sparkle's documented top-level symlinks, but
# every nested bundle, enclosing directory and target must stay inside the
# physical framework root. This prevents signing an external bundle through
# Versions/B, an XPC bundle or any other symlinked component.
root=$framework

require_real_dir() {
    path=$1
    case $path in
        "$root"/*) ;;
        *) die "path escapes the framework: $path" ;;
    esac
    [ -d "$path" ] || die "missing directory: $path"
    [ -L "$path" ] && die "refusing symlinked directory: $path"
    physical=$(CDPATH= cd -- "$path" && pwd -P) || die "cannot resolve $path"
    [ "$physical" = "$path" ] \
        || die "refusing symlinked enclosing component for $path (resolved to $physical)"
}

require_real_file() {
    path=$1
    case $path in
        "$root"/*) ;;
        *) die "path escapes the framework: $path" ;;
    esac
    [ -f "$path" ] || die "missing file: $path"
    [ -L "$path" ] && die "refusing symlinked file: $path"
    directory=$(CDPATH= cd -- "$(dirname -- "$path")" && pwd -P) \
        || die "cannot resolve the directory for $path"
    [ "$directory/$(basename -- "$path")" = "$path" ] \
        || die "refusing symlinked enclosing component for $path"
}

check_expected_link() {
    relative=$1
    expected_target=$2
    link=$root/$relative
    [ -L "$link" ] || die "missing expected symbolic link: $relative"
    actual=$(readlink "$link") || die "cannot read symbolic link: $relative"
    [ "$actual" = "$expected_target" ] \
        || die "unexpected symbolic link target: $relative -> $actual"
    target_directory=$(CDPATH= cd -- "$(dirname -- "$link")" \
        && CDPATH= cd -- "$(dirname -- "$actual")" && pwd -P) \
        || die "broken symbolic link: $relative"
    target=$target_directory/$(basename -- "$actual")
    [ -e "$target" ] || die "symbolic link target is missing: $relative -> $target"
    case $target in
        "$root"/*) ;;
        *) die "symbolic link escapes Sparkle.framework: $relative -> $target" ;;
    esac
}

# Reject any symlink outside Sparkle's exact pinned layout. That layout has
# one symlink per top-level product plus Versions/Current -> B; anything else
# could redirect signing to a foreign file.
links=$(find "$root" -type l -print 2>/dev/null) || die "cannot inspect symbolic links in $root"
while IFS= read -r link; do
    [ -n "$link" ] || continue
    relative=${link#"$root"/}
    [ "$relative" != "$link" ] || die "symbolic link outside framework root: $link"
    case $relative in
        Autoupdate|Headers|Modules|PrivateHeaders|Resources|Sparkle|Updater.app|XPCServices|Versions/Current) ;;
        *) die "unexpected symbolic link in Sparkle.framework: $relative" ;;
    esac
done <<EOF
$links
EOF

check_expected_link Autoupdate Versions/Current/Autoupdate
check_expected_link Headers Versions/Current/Headers
check_expected_link Modules Versions/Current/Modules
check_expected_link PrivateHeaders Versions/Current/PrivateHeaders
check_expected_link Resources Versions/Current/Resources
check_expected_link Sparkle Versions/Current/Sparkle
check_expected_link Updater.app Versions/Current/Updater.app
check_expected_link XPCServices Versions/Current/XPCServices
check_expected_link Versions/Current B

require_real_dir "$root/Versions"
require_real_dir "$root/Versions/B"
require_real_dir "$root/Versions/B/XPCServices"
require_real_file "$root/Versions/B/Sparkle"
require_real_dir "$installer"
require_real_dir "$installer/Contents"
require_real_dir "$installer/Contents/MacOS"
require_real_file "$installer/Contents/MacOS/Installer"
require_real_dir "$downloader"
require_real_dir "$downloader/Contents"
require_real_dir "$downloader/Contents/MacOS"
require_real_file "$downloader/Contents/MacOS/Downloader"
require_real_file "$autoupdate"
require_real_dir "$updater"
require_real_dir "$updater/Contents"
require_real_dir "$updater/Contents/MacOS"
require_real_file "$updater/Contents/MacOS/Updater"

command -v codesign >/dev/null || die "codesign is required"

sign() {
    path=$1
    if [ "$mode" = "--distribution" ]; then
        codesign --force --sign "$identity" --options runtime --timestamp "$path"
    else
        codesign --force --sign - --timestamp=none "$path"
    fi
    codesign --verify --strict --verbose=2 "$path"
}

# Inside-out, exactly the order Sparkle documents. Installer.xpc carries no
# entitlements of its own; Downloader.xpc must keep the ones Sparkle shipped
# with it (an optional sandbox profile can depend on them). Updater.app is
# signed after its executable is already sealed by its own bundle signature.
sign "$installer"
if [ "$mode" = "--distribution" ]; then
    codesign --force --sign "$identity" --options runtime --timestamp \
        --preserve-metadata=entitlements "$downloader"
    codesign --verify --strict --verbose=2 "$downloader"
else
    sign "$downloader"
fi
sign "$autoupdate"
sign "$updater"
sign "$framework"

if [ "$mode" = "--distribution" ]; then
    # A hardened-runtime distribution helper with get-task-allow would let a
    # debugger attach to the installed updater. Refuse that combination
    # instead of shipping it.
    for path in "$installer" "$downloader" "$autoupdate" "$updater" "$framework"; do
        case $path in
            "$installer") label=Installer.xpc ;;
            "$downloader") label=Downloader.xpc ;;
            "$autoupdate") label=Autoupdate ;;
            "$updater") label=Updater.app ;;
            *) label=Sparkle.framework ;;
        esac
        if codesign -d --entitlements - "$path" 2>/dev/null | grep -q 'com.apple.security.get-task-allow'; then
            die "$label has com.apple.security.get-task-allow; refusing a distribution signature"
        fi
        info=$(codesign -d --verbose=4 "$path" 2>&1)
        printf '%s\n' "$info" | grep -q 'flags=.*(.*runtime.*)' \
            || die "$label is not signed with the hardened runtime"
    done
fi

printf 'Signed %s (%s)\n' "$framework" "$mode"
