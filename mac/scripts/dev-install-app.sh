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
# script restores the previous state from the exact backup path it recorded,
# removes the staging copy it created, prints a ready-to-run swap script for
# the maintainer, and exits 0: the swap becomes the maintainer's command, not
# a failure. If even the restore is denied, it exits non-zero and names the
# exact backup path instead of printing a script. It never builds, signs,
# launches, or uninstalls anything, never deletes an app, and never touches
# the Ciaobot engine service, its workspace, or its .env.
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
a free `.Ciaobot.app.prev-<timestamp>` name (a -N counter disambiguates a
name taken in the same second), and moves the staged copy into place. A
leftover staging directory is refused, never overwritten. Paths containing
quotes, backticks, dollar signs, backslashes or control characters are
refused. If the swap cannot be done here, a ready-to-run swap script is
printed on stdout instead of failing.
EOF
}

die() {
    printf 'dev-install: %s\n' "$1" >&2
    exit 1
}

# A path carrying quoting, expansion or control characters is a mistake
# waiting to happen in an mv that swaps a live app, and in the swap script
# printed on a blocked swap. Refuse every one of these characters, not only
# the single quote.
shell_unsafe() {
    case $1 in
        *[\'\"\$\`\\]*) return 0 ;;
        *[[:cntrl:]]*) return 0 ;;
    esac
    return 1
}

# Quote a value for shell use inside single quotes, spelling an embedded
# single quote as the '\'' idiom. The path checks refuse most of these
# characters; the helper stays general anyway.
shq() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

staged=
installed_app=
dry=0
expect_version=
staging_ours=
swap_state=
swap_backup=
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

for unsafe_candidate in "$staged" "$installed_app"; do
    if shell_unsafe "$unsafe_candidate"; then
        printf 'dev-install: paths with quotes, backticks, dollar signs, backslashes or control characters are not supported: %s\n' "$unsafe_candidate" >&2
        exit 2
    fi
done

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

# The ready-to-run swap script handed to the maintainer on a blocked swap.
# Every value is emitted with the quoting helper; nothing is expanded through
# an unquoted heredoc, so what is printed is exactly what runs. The script
# stages a fresh copy from the bundle --staged named (this run removed its
# own staging copy), refuses a staging directory already in the way, refuses
# to overwrite a backup (its backup name carries the date and its own pid),
# and rolls the previous app back if its final move fails.
print_swap_script() {
    printf '%s\n' '#!/bin/sh'
    printf '%s\n' '# Ready-to-run dev-install swap, generated by scripts/dev-install-app.sh after'
    printf '%s\n' '# this environment (agent sandbox / macOS) denied the move. Running it as the'
    printf '%s\n' '# maintainer re-stages, self-checks and swaps exactly the bundle that run'
    printf '%s\n' '# planned. Read it, then run it where the denial does not apply.'
    printf '%s\n' 'set -eu'
    printf 'staged=%s\n' "$(shq "$staged")"
    printf 'installed=%s\n' "$(shq "$installed")"
    printf 'expected_version=%s\n' "$(shq "$expected_version")"
    printf 'staging=%s\n' "$(shq "$staging")"
    printf 'installed_parent=%s\n' "$(shq "$installed_parent")"
    printf '%s\n' '[ -d "$staged" ] || { echo "no staged bundle: $staged" >&2; exit 1; }'
    printf '%s\n' '[ "$(basename -- "$installed")" = "Ciaobot.app" ] \'
    printf '%s\n' '    || { echo "the installed app must be named Ciaobot.app, got: $(basename -- "$installed")" >&2; exit 1; }'
    printf '%s\n' 'if [ -e "$staging" ]; then'
    printf '%s\n' '    echo "a staging directory is in the way: $staging (inspect or remove it first; nothing was changed)" >&2'
    printf '%s\n' '    exit 1'
    printf '%s\n' 'fi'
    printf '%s\n' 'ditto "$staged" "$staging"'
    printf '%s\n' 'chmod -R u+w "$staging"'
    printf '%s\n' 'xattr -cr "$staging" 2>/dev/null || :'
    printf '%s\n' 'codesign --verify --deep --strict "$staging" \'
    printf '%s\n' '    || { echo "could not verify the staged bundle" >&2; exit 1; }'
    printf '%s\n' '[ -f "$staging/Contents/Resources/engine/bin/ciao" ] \'
    printf '%s\n' '    || { echo "no bundled engine launcher in the staged bundle" >&2; exit 1; }'
    printf '%s\n' '[ "$(env -i "$staging/Contents/Resources/engine/bin/ciao" --version)" = "ciao $expected_version" ] \'
    printf '%s\n' '    || { echo "the staged engine does not report ciao $expected_version" >&2; exit 1; }'
    printf '%s\n' 'backup="$installed_parent/.Ciaobot.app.prev-$(date +%Y%m%d%H%M%S)-$$"'
    printf '%s\n' 'if [ -e "$backup" ]; then'
    printf '%s\n' '    echo "a backup already sits at the computed backup name: $backup (inspect or remove it first)" >&2'
    printf '%s\n' '    exit 1'
    printf '%s\n' 'fi'
    printf '%s\n' 'if [ -e "$installed" ]; then'
    printf '%s\n' '    mv "$installed" "$backup"'
    printf '%s\n' 'fi'
    printf '%s\n' 'if ! mv "$staging" "$installed"; then'
    printf '%s\n' '    echo "the swap failed" >&2'
    printf '%s\n' '    if [ -e "$backup" ]; then'
    printf '%s\n' '        if mv "$backup" "$installed"; then'
    printf '%s\n' '            echo "the previous app was put back in place from $backup" >&2'
    printf '%s\n' '        else'
    printf '%s\n' '            echo "the previous app could NOT be moved back; it sits at: $backup" >&2'
    printf '%s\n' '        fi'
    printf '%s\n' '    else'
    printf '%s\n' '        echo "no backup had been moved aside, so there is nothing to put back" >&2'
    printf '%s\n' '    fi'
    printf '%s\n' '    exit 1'
    printf '%s\n' 'fi'
    printf '%s\n' 'echo "swapped into place: $installed (the previous copy is in $backup, and any earlier .prev-* beside it)"'
}

[ ! -e "$staging" ] \
    || die "a staging bundle is already in the way: $staging (inspect or remove it first; nothing was changed)"

if [ "$dry" -eq 1 ]; then
    check_bundle "$staged"
    printf 'Dry run: nothing was changed.\n'
    printf 'would stage:  %s -> %s\n' "$staged" "$staging"
    printf 'would swap:   %s -> %s<timestamp>[-N] (moved aside, not deleted)\n' \
        "$installed" "$backup_prefix"
    printf 'then move:    %s -> %s\n' "$staging" "$installed"
    exit 0
fi

# The staging refusal must happen before the trap below is armed, so a
# directory someone else keeps at that name is never touched. From here the
# staging copy is this run's own artifact and the only thing this run ever
# removes: the EXIT trap deletes it if ditto or the self-check fails, or on
# the blocked-swap path below. Nothing else is ever removed.
[ ! -e "$staging" ] \
    || die "a staging bundle is already in the way: $staging (inspect or remove it first; nothing was changed)"

if [ "$dry" -eq 1 ]; then
    check_bundle "$staged"
    printf 'Dry run: nothing was changed.\n'
    printf 'would stage:  %s -> %s\n' "$staged" "$staging"
    printf 'would swap:   %s -> %s<timestamp>[-N] (moved aside, not deleted)\n' \
        "$installed" "$backup_prefix"
    printf 'then move:    %s -> %s\n' "$staging" "$installed"
    exit 0
fi

cleanup_staging() {
    if [ -n "$staging_ours" ] && [ -d "$staging" ]; then
        if rm -rf "$staging"; then
            printf 'dev-install: removed the staging copy this run created: %s\n' "$staging" >&2
        else
            printf 'dev-install: could not remove the staging copy this run created: %s (remove it by hand)\n' "$staging" >&2
        fi
    fi
}

staging_ours=1
trap cleanup_staging EXIT

ditto "$staged" "$staging"
chmod -R u+w "$staging"
xattr -cr "$staging" 2>/dev/null || :
check_bundle "$staging"

# Swap by rename only. The backup path is recorded exactly, and if the staged
# copy cannot be moved into place, the previous app is restored from that
# recorded path — never from a `.prev-*` glob, which could pick up an older
# backup from a previous run instead of the previous app.
do_swap() {
    swap_state=failed
    swap_backup=
    if [ -e "$installed" ]; then
        stamp=$(date +%Y%m%d%H%M%S)
        swap_backup=$backup_prefix$stamp
        n=2
        while [ -e "$swap_backup" ]; do
            swap_backup=$backup_prefix$stamp-$n
            n=$((n + 1))
            [ "$n" -le 1000 ] \
                || die "could not choose a free backup name beside $installed (refusing to overwrite any .prev-* copy)"
        done
        if ! mv "$installed" "$swap_backup"; then
            swap_state=stayed
            swap_backup=
            return 1
        fi
    fi
    if mv "$staging" "$installed"; then
        swap_state=ok
        return 0
    fi
    if [ -n "$swap_backup" ]; then
        if mv "$swap_backup" "$installed"; then
            swap_state=restored
        else
            swap_state=restore_failed
        fi
    else
        swap_state=stayed
    fi
    return 1
}

if do_swap; then
    printf 'dev-install: swapped into place: %s\n' "$installed"
    if [ -n "$swap_backup" ]; then
        printf 'dev-install: the previous bundle was moved aside to %s\n' "$swap_backup"
        printf 'dev-install: nothing was deleted here; remove that backup once you no longer want it\n'
    fi
    exit 0
fi

case $swap_state in
    restore_failed)
        # The staged copy could not be moved in AND the previous app could
        # not be restored: stop loudly instead of handing over a script that
        # would swap from a half-moved state. The EXIT trap removes this
        # run's staging copy; the backup stays exactly where it was recorded.
        printf 'dev-install: the swap failed and the previous app could NOT be moved back.\n' >&2
        printf 'dev-install: its backup sits exactly at: %s\n' "$swap_backup" >&2
        printf 'dev-install: %s is missing; move the backup back there by hand before swapping again.\n' \
            "$installed" >&2
        exit 1
        ;;
esac

# The swap was denied in this environment (an agent sandbox, or macOS
# refusing the rename of a running, registered app). The previous app is back
# in place (or never left it) and the EXIT trap removes this run's staging
# copy: the maintainer's script stages a fresh copy from the --staged bundle.
# Print that script instead of failing: the maintainer runs it where the
# sandbox does not apply.
printf 'dev-install: SWAP BLOCKED in this environment.\n' >&2
case $swap_state in
    restored)
        printf 'dev-install: the previous app was put back in place at %s.\n' "$installed" >&2
        ;;
    stayed)
        printf 'dev-install: the previous app stayed in place at %s.\n' "$installed" >&2
        ;;
esac
printf 'dev-install: a ready-to-run swap script follows. Save it (for example to\n' >&2
printf 'dev-install: ~/swap-ciaobot.sh), read it, and run it yourself as the maintainer:\n' >&2
print_swap_script
exit 0
