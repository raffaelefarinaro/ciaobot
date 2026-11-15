#!/bin/sh
# Publish one release asset: the notarized Ciaobot-<version>.dmg.
#
# usage: scripts/publish-release.sh --tag TAG --asset PATH --notes PATH
#                                   [--title TITLE] [--repo OWNER/NAME]
#                                   [--draft] [--prerelease]
#
# A release page is a download page. One obvious file is the whole point, so
# this script accepts exactly one --asset and refuses anything that is not a
# Ciaobot-<version>.dmg. The appcast enclosure is that same DMG; there is no
# second archive and no .sha256 asset. Record the printed digest in the release
# notes instead.
#
# This is the only script in the release flow that talks to GitHub. It never
# tags, pushes or edits the appcast: publishing the feed stays a separate,
# deliberate step.
set -eu

usage() {
    cat <<'EOF'
usage: scripts/publish-release.sh --tag TAG --asset PATH --notes PATH
                                  [--title TITLE] [--repo OWNER/NAME]
                                  [--draft] [--prerelease]

  --tag TAG          the release tag, for example v0.1.2
  --asset PATH       the one file to publish; must be Ciaobot-<version>.dmg
  --notes PATH       a file with the release notes, including the DMG digest
  --title TITLE      release title; defaults to the tag
  --repo OWNER/NAME  repository to publish to; defaults to the current checkout
  --draft            create the release as a draft
  --prerelease       mark the release as a pre-release

Exactly one asset may be attached. A ZIP and a .sha256 file are refused here:
the DMG is the download and Sparkle's enclosure, and the digest belongs in the
notes.
EOF
}

die() {
    printf 'publish-release: %s\n' "$1" >&2
    exit 1
}

usage_error() {
    usage >&2
    exit 2
}

tag=
asset=
asset_count=0
notes=
title=
repo=
draft=0
prerelease=0
while [ $# -gt 0 ]; do
    case $1 in
        --tag)
            [ $# -ge 2 ] || usage_error
            tag=$2
            shift 2
            ;;
        --asset)
            [ $# -ge 2 ] || usage_error
            asset=$2
            asset_count=$((asset_count + 1))
            shift 2
            ;;
        --notes)
            [ $# -ge 2 ] || usage_error
            notes=$2
            shift 2
            ;;
        --title)
            [ $# -ge 2 ] || usage_error
            title=$2
            shift 2
            ;;
        --repo)
            [ $# -ge 2 ] || usage_error
            repo=$2
            shift 2
            ;;
        --draft)
            draft=1
            shift
            ;;
        --prerelease)
            prerelease=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'publish-release: unknown argument: %s\n' "$1" >&2
            usage_error
            ;;
    esac
done

[ -n "$tag" ] || usage_error
[ -n "$asset" ] || usage_error
[ -n "$notes" ] || usage_error

# One download, or none. A second asset would make the page ambiguous again.
[ "$asset_count" -eq 1 ] \
    || die "a release publishes exactly one asset, the DMG; got $asset_count --asset arguments"

case $tag in
    *[!A-Za-z0-9._-]*) die "tag must be a plain release tag, got: $tag" ;;
esac

[ -f "$asset" ] && [ ! -L "$asset" ] \
    || die "asset is missing, not a regular file, or a symlink: $asset"
[ -s "$asset" ] || die "asset is empty: $asset"
asset_name=$(basename "$asset")
case $asset_name in
    *.zip) die "refusing a ZIP: the release publishes Ciaobot-<version>.dmg, which is also Sparkle's enclosure: $asset_name" ;;
    *.sha256) die "refusing a checksum asset: put the digest in the notes instead: $asset_name" ;;
    *.dmg) ;;
    *) die "asset must be Ciaobot-<version>.dmg, got: $asset_name" ;;
esac
printf '%s' "$asset_name" | grep -Eq '^Ciaobot-[0-9][0-9.]*\.dmg$' \
    || die "asset must be Ciaobot-<version>.dmg, got: $asset_name"

[ -f "$notes" ] && [ ! -L "$notes" ] \
    || die "notes file is missing, not a regular file, or a symlink: $notes"

if [ -n "$repo" ]; then
    case $repo in
        */*) ;;
        *) die "repository must be OWNER/NAME, got: $repo" ;;
    esac
fi

command -v gh >/dev/null 2>&1 \
    || die "gh is not installed; install the GitHub CLI to publish a release"

set -- release create "$tag" "$asset"
if [ -n "$title" ]; then
    set -- "$@" --title "$title"
else
    set -- "$@" --title "$tag"
fi
set -- "$@" --notes-file "$notes"
[ "$draft" = 0 ] || set -- "$@" --draft
[ "$prerelease" = 0 ] || set -- "$@" --prerelease
if [ -n "$repo" ]; then
    set -- "$@" --repo "$repo"
fi

gh "$@" || die "gh release create failed"

printf 'Published %s as the only asset of %s\n' "$asset_name" "$tag"
