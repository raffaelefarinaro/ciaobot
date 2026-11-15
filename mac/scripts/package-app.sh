#!/bin/sh
# Package a local, ad-hoc-signed Ciaobot.app and a ZIP of it for personal
# testing.
#
# The signature is ad-hoc (`codesign --sign -`), the same identity the retired
# Tauri desktop build used (`bundle.macOS.signingIdentity: "-"`). There is no
# Developer ID certificate and no notarization, so this is not a distribution
# build: macOS Gatekeeper still warns on another Mac.
#
# The script never installs the app, never replaces a copy in /Applications,
# never launches it, and never contacts the Ciaobot server, its workspace, or
# the network. It writes only inside --output (plus the ordinary SwiftPM build
# directory when it runs scripts/build-app.sh).
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

usage() {
    cat <<'EOF'
usage: scripts/package-app.sh [--output DIR] [--app PATH] [--no-build]

  --output DIR  write the artifacts into DIR, which must be new or empty
                (default: a fresh mktemp directory)
  --app PATH    package this .app instead of .build/Ciaobot.app
  --no-build    do not run scripts/build-app.sh first

Packaging never replaces anything. An --output directory that already holds
Ciaobot.app, the ZIP, SHA256SUMS or MANIFEST.txt is refused, as is an output
directory that contains the source app or lives inside it. Use a fresh
directory for each run; the default is always fresh.

The bundle is copied into DIR, signed ad-hoc (Sparkle's nested helpers
inside-out first), verified with codesign --verify --deep --strict,
archived without dereferencing symlinks, extracted again and verified,
then described in MANIFEST.txt and SHA256SUMS. The bundle identifier,
version, and architecture are read from the built app, so
scripts/build-app.sh stays their only definition.
EOF
}

die() {
    printf 'package-app: %s\n' "$1" >&2
    exit 1
}

output_dir=
app=
build=1
while [ $# -gt 0 ]; do
    case $1 in
        --output)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            output_dir=$2
            shift 2
            ;;
        --app)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            app=$2
            shift 2
            ;;
        --no-build)
            build=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'package-app: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$build" -eq 1 ]; then
    scripts/build-app.sh
fi

app=${app:-$root/.build/Ciaobot.app}
[ -d "$app" ] || die "no app bundle at $app"
app=$(CDPATH= cd -- "$app" && pwd -P)
plist=$app/Contents/Info.plist
[ -f "$plist" ] || die "no Info.plist at $plist"

identifier=$(plutil -extract CFBundleIdentifier raw -o - "$plist" 2>/dev/null || true)
executable_name=$(plutil -extract CFBundleExecutable raw -o - "$plist" 2>/dev/null || true)
version=$(plutil -extract CFBundleShortVersionString raw -o - "$plist" 2>/dev/null || true)
build_number=$(plutil -extract CFBundleVersion raw -o - "$plist" 2>/dev/null || true)
[ -n "$identifier" ] || die "$plist has no CFBundleIdentifier"
[ -n "$executable_name" ] || die "$plist has no CFBundleExecutable"
[ -n "$version" ] || die "$plist has no CFBundleShortVersionString"
[ -n "$build_number" ] || die "$plist has no CFBundleVersion"
[ -x "$app/Contents/MacOS/$executable_name" ] || die "no executable at $app/Contents/MacOS/$executable_name"

# Refuse any overlap with the source bundle before creating a directory. The
# copy target is always DIR/Ciaobot.app, so `--output .build` would otherwise
# target the build product itself (and an output inside the app would make
# ditto copy the app into itself), which is exactly how a packaging mistake
# can delete or corrupt the source.
refuse_overlap() {
    case $output_dir/ in
        "$app"/*)
            die "refusing to write inside the source app: $output_dir" ;;
    esac
    case $app in
        "$output_dir"|"$output_dir"/*)
            die "refusing an --output that contains the source app: $output_dir" ;;
    esac
}

# Resolve the physical path of an existing directory, or of a directory that
# does not exist yet, without creating it. This makes the overlap checks work
# through /var -> /private/var and other symlinked parents.
physical_path() (
    path=$1
    suffix=
    while [ ! -d "$path" ]; do
        suffix="/$(basename -- "$path")$suffix"
        path=$(dirname -- "$path")
    done
    printf '%s%s\n' "$(CDPATH= cd -- "$path" && pwd -P)" "$suffix"
)

tmp=${TMPDIR:-/tmp}
tmp=${tmp%/}
if [ -z "$output_dir" ]; then
    output_dir=$(mktemp -d "$tmp/ciaobot-package.XXXXXX")
    output_dir=$(CDPATH= cd -- "$output_dir" && pwd -P)
else
    case $output_dir in
        /*) ;;
        *) output_dir=$PWD/$output_dir ;;
    esac
    output_dir=${output_dir%/}
    [ -n "$output_dir" ] || output_dir=/
    output_dir=$(physical_path "$output_dir")
fi
refuse_overlap
# Packaging is a local artifact step. It must never replace an installed copy,
# so an Applications folder is refused.
case $output_dir/Ciaobot.app in
    /Applications/*|"${HOME:-/nonexistent}"/Applications/*)
        die "refusing to write inside an Applications folder: $output_dir/Ciaobot.app" ;;
esac
if [ -e "$output_dir" ]; then
    [ -d "$output_dir" ] || die "--output is not a directory: $output_dir"
    [ -z "$(ls -A "$output_dir")" ] \
        || die "refusing a non-empty --output directory: $output_dir (packaging never overwrites an existing bundle or artifact; use a fresh directory)"
else
    mkdir -p "$output_dir"
fi
output_dir=$(CDPATH= cd -- "$output_dir" && pwd -P)
bundle=$output_dir/Ciaobot.app
# Re-check after any directory creation, so the actual target path cannot
# differ from the one compared above.
refuse_overlap
for artifact in "$bundle" "$output_dir/SHA256SUMS" "$output_dir/MANIFEST.txt"; do
    [ ! -e "$artifact" ] || die "refusing to overwrite existing artifact: $artifact"
done

# Work on a copy so signing never rewrites the app the build produced (and
# never a copy that is already running from somewhere else).
ditto "$app" "$bundle"
chmod -R u+w "$bundle"
# A downloaded artifact can carry a quarantine flag; it must not be baked into
# the archive we hand out.
xattr -cr "$bundle" 2>/dev/null || true

# Sparkle nests executable helpers inside its framework. Sign them inside-out
# before the app is signed, so the app's seal covers already-valid nested code.
# Ad-hoc mode is deliberate: this app is not hardened, which lets it load an
# ad-hoc Sparkle locally.
sparkle=$bundle/Contents/Frameworks/Sparkle.framework
if [ -d "$sparkle" ]; then
    scripts/sign-sparkle.sh --adhoc "$sparkle"
fi

# Ad-hoc sign the whole bundle, so the signature seals Info.plist and the
# bundled artwork instead of only the linker-signed executable. codesign takes
# the identifier from CFBundleIdentifier; that identity is checked below.
codesign --force --sign - --timestamp=none "$bundle"

# Fixed timestamps make the archive byte-identical for identical file content.
# They are applied after signing because codesign writes _CodeSignature with
# the current time and that timestamp would otherwise change every run.
# Timestamps are not part of the sealed contents, so the signature stays valid.
stamp=${SOURCE_DATE_EPOCH:-1577836800}
case $stamp in
    ''|*[!0-9]*) stamp=1577836800 ;;
esac
[ "$stamp" -ge 315532800 ] || stamp=1577836800
touch_stamp=$(TZ=UTC date -r "$stamp" +%Y%m%d%H%M.%S)
# -h also normalises Sparkle's symlink entries; touching their targets alone
# would leave the symlinks' own mtimes varying between runs.
find "$bundle" -exec touch -h -t "$touch_stamp" {} +

codesign --verify --deep --strict --verbose=2 "$bundle"
signature=$(codesign --display --verbose=4 "$bundle" 2>&1)
printf '%s\n' "$signature" | grep -Fqx 'Signature=adhoc' \
    || die "the bundle is not ad-hoc signed"
printf '%s\n' "$signature" | grep -Fqx "Identifier=$identifier" \
    || die "the signature identity does not match CFBundleIdentifier ($identifier)"
if printf '%s\n' "$signature" | grep -q '^Authority='; then
    die "unexpected signing authority; this script only produces ad-hoc builds"
fi
cdhash=$(printf '%s\n' "$signature" | sed -n 's/^CDHash=//p' | head -n 1)
[ -n "$cdhash" ] || die "no CDHash in the signature"

# Record the executable's real architectures. The build uses the host
# toolchain, so a normal run produces a thin app for this Mac's architecture
# (arm64 on Apple silicon, x86_64 on Intel), not a universal binary.
architectures=unknown
if command -v lipo >/dev/null 2>&1; then
    architectures=$(lipo -archs "$bundle/Contents/MacOS/$executable_name" 2>/dev/null | tr ' ' ',' || true)
fi
[ -n "$architectures" ] || architectures=unknown

# Record what Gatekeeper thinks. An ad-hoc build is expected to be rejected;
# the value is evidence, not a pass/fail condition.
gatekeeper=rejected
if spctl --assess --type execute --verbose=4 "$bundle" >/dev/null 2>&1; then
    gatekeeper=accepted
fi

archive_name="Ciaobot-$version-adhoc.zip"
archive=$output_dir/$archive_name
[ ! -e "$archive" ] || die "refusing to overwrite existing archive: $archive"
# zip -X drops resource forks and extended attributes, which cannot be made
# reproducible (com.apple.provenance is re-added by the system). The code
# signature is inside the files, so it survives. -y stores Sparkle's framework
# symlinks as links; without it zip follows them and duplicates the executable,
# which breaks the framework and its signature.
(cd "$output_dir" && zip -q -r -X -y "$archive_name" Ciaobot.app)

verify_root=$(mktemp -d "$tmp/ciaobot-package-verify.XXXXXX")
trap 'rm -rf "$verify_root"' EXIT INT TERM
ditto -x -k "$archive" "$verify_root"
extracted=$verify_root/Ciaobot.app
[ -d "$extracted" ] || die "the archive does not contain Ciaobot.app at its top level"
codesign --verify --deep --strict --verbose=2 "$extracted"
extracted_signature=$(codesign --display --verbose=4 "$extracted" 2>&1)
extracted_cdhash=$(printf '%s\n' "$extracted_signature" | sed -n 's/^CDHash=//p' | head -n 1)
[ "$extracted_cdhash" = "$cdhash" ] || die "the extracted app has a different signature"
cmp -s "$bundle/Contents/MacOS/$executable_name" "$extracted/Contents/MacOS/$executable_name" \
    || die "the extracted executable differs from the signed one"
extracted_identifier=$(plutil -extract CFBundleIdentifier raw -o - "$extracted/Contents/Info.plist")
[ "$extracted_identifier" = "$identifier" ] || die "the archive changed CFBundleIdentifier"
# An archive that followed Sparkle's symlinks would still unzip and often still
# pass a superficial check, but dyld and codesign expect the versioned layout.
if [ -d "$bundle/Contents/Frameworks/Sparkle.framework" ]; then
    [ -d "$extracted/Contents/Frameworks/Sparkle.framework" ] \
        || die "the archive lost Sparkle.framework"
    [ -L "$extracted/Contents/Frameworks/Sparkle.framework/Versions/Current" ] \
        || die "the archive dereferenced Sparkle's version symlink"
    [ -L "$extracted/Contents/Frameworks/Sparkle.framework/Sparkle" ] \
        || die "the archive dereferenced Sparkle's binary symlink"
    [ -x "$extracted/Contents/Frameworks/Sparkle.framework/Versions/B/Sparkle" ] \
        || die "the archive lost Sparkle's executable"
fi

(
    cd "$output_dir"
    shasum -a 256 "$archive_name" "Ciaobot.app/Contents/MacOS/$executable_name"
) > "$output_dir/SHA256SUMS"

archive_sha=$(shasum -a 256 "$archive" | awk '{print $1}')
executable_sha=$(shasum -a 256 "$bundle/Contents/MacOS/$executable_name" | awk '{print $1}')

cat > "$output_dir/MANIFEST.txt" <<EOF
bundle_identifier=$identifier
bundle_version=$version
bundle_build=$build_number
executable=$executable_name
architectures=$architectures
signature=adhoc
codesign_identifier=$identifier
cdhash=$cdhash
notarized=false
gatekeeper_assessment=$gatekeeper
zip=$archive_name
zip_sha256=$archive_sha
executable_sha256=$executable_sha
source_bundle=local build (path omitted)
created_by=scripts/package-app.sh
EOF

cat <<EOF
Packaged $identifier $version ($build_number), ad-hoc signed.
Architecture: $architectures (this is a thin build, not a universal binary).

  app:      $bundle
  archive:  $archive
  manifest: $output_dir/MANIFEST.txt
  checksums: $output_dir/SHA256SUMS

Verified: codesign --verify --deep --strict (before and after unzipping), a
matching CDHash, and an identical executable in the archive.
Gatekeeper assessment: $gatekeeper. That is expected: this build has no
Developer ID signature and is not notarized, so on another Mac, allow it once
in System Settings > Privacy & Security > Open Anyway, or unquarantine the
extracted copy with: xattr -dr com.apple.quarantine Ciaobot.app
Native notifications and login-item approval are not exercised by this script.
EOF
