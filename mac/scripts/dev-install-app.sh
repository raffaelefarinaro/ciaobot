#!/bin/sh
# Dev-install a locally built Ciaobot.app (the ad-hoc output of
# scripts/package-app.sh) over the installed copy on this Mac.
#
# The build scripts never install; this is the safe, testable install step:
# the new bundle is staged under a DIFFERENT name (`.Ciaobot.app.staging`
# beside the installed app), self-checked in place (codesign and the bundled
# engine's --version against the checkout's pyproject version), then the old
# bundle is moved aside (never deleted) and the staged copy is moved into
# place. The live, registered Ciaobot.app name is only ever renamed, never
# written into: writing there is denied ('Operation not permitted').
#
# --dry-run verifies the staged bundle and prints the plan without changing
# anything. If the swap itself is denied (for example an agent sandbox), the
# script restores the previous state, prints a ready-to-run swap script for
# the maintainer, and exits 0: the swap becomes the maintainer's command, not
# a failure. It never builds, signs, launches, or uninstalls anything, never
# deletes an app, and never touches the Ciaobot engine service, its workspace,
# or its .env.
set -eu

usage() {
    cat <<'EOF'
usage: scripts/dev-install-app.sh --staged PATH [--installed PATH] [--dry-run]
                                  [--expect-engine-version X.Y.Z]

  --staged PATH            the app bundle to install, exactly what
                           scripts/package-app.sh produced (required)
  --installed PATH         the installed app to replace
                           (default: "$HOME/Applications/Ciaobot.app"; it
                           must be named Ciaobot.app and must already exist)
  --dry-run                verify the staged bundle and print the plan;
                           change nothing
  --expect-engine-version  compare the staged engine's `bin/ciao --version`
                           with this instead of the checkout's pyproject.toml
                           version

This script stages the bundle under `.Ciaobot.app.staging` in the installed
app's folder, checks it there (codesign --verify --deep --strict; the engine
launcher must report the expected version), moves the installed app aside to
`.Ciaobot.app.prev-<timestamp>`, and moves the staged copy into place. A
leftover staging directory is refused, never overwritten. If the swap cannot
be done here, a ready-to-run swap script is printed on stdout instead of
failing.
EOF
}

die() {
    printf 'dev-install: %s\n' "$1" >&2
    exit 1
}

staged=
installed_app=
dry=0
expect_version=
while [ $# -gt 0 ]; do
    case $1 in
        --staged)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            staged=$2
            shift 2
            ;;
        --installed)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            installed_app=$2
            shift 2
            ;;
        --dry-run)
            dry=1
            shift
            ;;
        --expect-engine-version)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            expect_version=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'dev-install: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

[ -n "$staged" ] || { usage >&2; exit 2; }
[ -n "$installed_app" ] || installed_app="$HOME/Applications/Ciaobot.app"

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)

# The swap script printed on a blocked swap embeds these paths in single
# quotes; refuse a path that would break it rather than emit a broken script.
case $staged$installed_app in
    *"'"*) die "paths with single quotes are not supported: $staged $installed_app" ;;
esac

staged=$(CDPATH= cd -- "$staged" 2>/dev/null && pwd -P) \
    || die "no staged app bundle at $staged"
case $staged in
    *.app) : ;;
    *) die "--staged must be a .app bundle: $staged" ;;
esac

installed_parent=$(dirname -- "$installed_app")
installed_parent=$(CDPATH= cd -- "$installed_parent" 2>/dev/null && pwd -P) \
    || die "the folder of the installed app does not exist: $installed_parent"
installed_name=$(basename -- "$installed_app")
[ "$installed_name" = "Ciaobot.app" ] \
    || die "the installed app must be named Ciaobot.app, got: $installed_name"
installed=$installed_parent/$installed_name
[ -d "$installed" ] \
    || die "no installed app at $installed (this script only replaces a copy that is already installed; install one first)"
if [ -L "$installed" ]; then
    die "the installed app is a symlink: $installed"
fi

# Refuse every overlap before anything is copied: a bundle nested in the
# installed app (or the reverse) is how a mistaken path destroys a build or
# an install. Resolve the physical directory first (like package-app.sh), so
# the checks cannot be led past /var -> /private/var style parents.
[ "$staged" != "$installed" ] \
    || die "refusing to swap the installed app with itself: $staged"
case $staged/ in
    "$installed"/*) die "refusing a --staged bundle inside the installed app: $staged" ;;
esac
case $installed/ in
    "$staged"/*) die "refusing an --installed app inside the staged bundle: $installed" ;;
esac

if [ -n "$expect_version" ]; then
    expected_version=$expect_version
else
    expected_version=$(/usr/bin/sed -n \
        '/^\[project\]/,/^\[/s/^version = "\([^"]*\)"$/\1/p' \
        "$root/pyproject.toml" | head -n 1)
    [ -n "$expected_version" ] \
        || die "could not read the project version from pyproject.toml (pass --expect-engine-version)"
fi

staging=$installed_parent/.Ciaobot.app.staging
backup_prefix=$installed_parent/.Ciaobot.app.prev-

# The self-check: prove the bundle (or its staging copy) is signed and that
# its engine reports the expected version.
check_bundle() {
    bundle=$1
    if [ -L "$bundle/Contents/Resources/engine" ]; then
        die "the bundled engine must be a real directory, not a symlink: $bundle/Contents/Resources/engine"
    fi
    codesign --verify --deep --strict --verbose=2 "$bundle" \
        || die "could not verify the code signature of $bundle"
    [ -f "$bundle/Contents/Resources/engine/bin/ciao" ] \
        || die "no bundled engine launcher at $bundle/Contents/Resources/engine/bin/ciao"
    reported=$(env -i "$bundle/Contents/Resources/engine/bin/ciao" --version) \
        || die "the bundled engine launcher did not run: $bundle/Contents/Resources/engine/bin/ciao"
    [ "$reported" = "ciao $expected_version" ] \
        || die "the engine reports '$reported', expected 'ciao $expected_version'"
    printf 'dev-install: engine check passed (%s)\n' "$reported" >&2
}

[ ! -e "$staging" ] \
    || die "a staging bundle is already in the way: $staging (inspect or remove it first; nothing was changed)"

if [ "$dry" -eq 1 ]; then
    check_bundle "$staged"
    printf 'Dry run: nothing was changed.\n'
    printf 'would stage:  %s -> %s\n' "$staged" "$staging"
    printf 'would swap:   %s -> %s<timestamp> (moved aside, not deleted)\n' \
        "$installed" "$backup_prefix"
    printf 'then move:    %s -> %s\n' "$staging" "$installed"
    exit 0
fi

# From here a failure before the swap must leave the machine untouched: the
# staging copy is this run's own artifact and the only thing cleaned up.
ditto "$staged" "$staging"
chmod -R u+w "$staging"
xattr -cr "$staging" 2>/dev/null || :
check_bundle "$staging"

swap_ok() {
    if [ -e "$installed" ]; then
        mv "$installed" "$backup_prefix$(date +%Y%m%d%H%M%S)" || return 1
    fi
    mv "$staging" "$installed" || return 1
}

if swap_ok; then
    printf 'dev-install: swapped into place: %s\n' "$installed"
    printf 'dev-install: the previous bundle was moved aside to %s*\n' "$backup_prefix"
    printf 'dev-install: nothing was deleted here; remove the .prev-* copy you no longer want\n'
    exit 0
fi

# The swap was denied in this environment (an agent sandbox, or macOS
# refusing the rename of a running, registered app). Put the previous state
# back, then print a ready-to-run swap script instead of failing: the
# maintainer runs it where the sandbox does not apply.
restored=none
if [ -e "$installed" ]; then
    restored=left
else
    for candidate in "$backup_prefix"*; do
        [ -d "$candidate" ] || continue
        if [ -e "$installed" ]; then
            break
        fi
        if mv "$candidate" "$installed"; then
            restored=yes
        else
            restored=blocked
        fi
        break
    done
fi

printf 'dev-install: SWAP BLOCKED in this environment.\n' >&2
case $restored in
    yes)
        printf 'dev-install: the previous app was put back in place at %s.\n' "$installed" >&2
        ;;
    blocked)
        printf 'dev-install: the previous app could NOT be moved back: it sits at %s* and %s is missing.\n' \
            "$backup_prefix" "$installed" >&2
        ;;
    left)
        printf 'dev-install: the previous app stayed in place at %s.\n' "$installed" >&2
        ;;
    *)
        printf 'dev-install: the machine state could not be determined; nothing was deleted.\n' >&2
        ;;
esac
[ -d "$staging" ] \
    || die "the swap was denied and the staging copy is gone; run scripts/package-app.sh again and retry"
printf 'dev-install: a ready-to-run swap script follows. Save it (for example to\n' >&2
printf 'dev-install: ~/swap-ciaobot.sh), read it, and run it yourself as the maintainer:\n' >&2
cat <<SCRIPT
#!/bin/sh
# Ready-to-run dev-install swap, generated by scripts/dev-install-app.sh after
# this environment (agent sandbox / macOS) denied the move. Running it as the
# maintainer stages, verifies and swaps exactly what that run planned.
set -eu
staged='$staged'
installed='$installed'
expected_version='$expected_version'
staging="\$(dirname -- "$installed")/.Ciaobot.app.staging"
[ -d "$staged" ] || { echo "no staged bundle: $staged" >&2; exit 1; }
[ "\$(basename -- "$installed")" = "Ciaobot.app" ] \\
    || { echo "the installed app must be named Ciaobot.app, got: \$(basename -- "$installed")" >&2; exit 1; }
rm -rf "\$staging"
ditto "\$staged" "\$staging"
chmod -R u+w "\$staging"
xattr -cr "\$staging" 2>/dev/null || :
codesign --verify --deep --strict "\$staging" \\
    || { echo "could not verify the staged bundle" >&2; exit 1; }
[ -f "\$staging/Contents/Resources/engine/bin/ciao" ] \\
    || { echo "no bundled engine launcher in the staged bundle" >&2; exit 1; }
[ "\$(env -i "\$staging/Contents/Resources/engine/bin/ciao" --version)" = "ciao \$expected_version" ] \\
    || { echo "the staged engine does not report ciao \$expected_version" >&2; exit 1; }
backup="\$(dirname -- "$installed")/.Ciaobot.app.prev-\$(date +%Y%m%d%H%M%S)"
if [ -e "$installed" ]; then mv "$installed" "\$backup"; fi
mv "\$staging" "$installed"
echo "swapped into place: $installed (the previous copy is in \$backup, and any earlier .prev-* beside it)"
SCRIPT
exit 0
