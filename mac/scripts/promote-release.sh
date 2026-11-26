#!/bin/sh
# Mark a finished release as the latest one.
#
# usage: scripts/promote-release.sh --tag vX.Y.Z [--repo OWNER/NAME]
#
# release-on-main creates every version release as a pre-release that is not
# latest, because the signed DMG is attached by hand after publish.yml. This is
# the one step that makes it latest. It refuses unless the release is a
# published pre-release that already holds what a download page needs: the
# stamped install.sh, the engine wheel and Ciaobot-<version>.dmg. It changes
# nothing else: no tag, no upload, no feed.
set -eu

usage() {
    cat <<'EOF'
usage: scripts/promote-release.sh --tag vX.Y.Z [--repo OWNER/NAME]

  --tag TAG          the version release to promote, for example v2.0.0
  --repo OWNER/NAME  repository to edit; defaults to the current checkout
EOF
}

die() {
    printf 'promote-release: %s\n' "$1" >&2
    exit 1
}

usage_error() {
    usage >&2
    exit 2
}

tag=
repo=
while [ $# -gt 0 ]; do
    case $1 in
        --tag)
            [ $# -ge 2 ] || usage_error
            tag=$2
            shift 2
            ;;
        --repo)
            [ $# -ge 2 ] || usage_error
            repo=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'promote-release: unknown argument: %s\n' "$1" >&2
            usage_error
            ;;
    esac
done

[ -n "$tag" ] || usage_error

# Plain vX.Y.Z only: the `updates` feed release must stay a pre-release.
printf '%s' "$tag" | grep -Eq '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
    || die "tag must look like v2.0.0, got: $tag"
version=${tag#v}

if [ -n "$repo" ]; then
    case $repo in
        */*) ;;
        *) die "repository must be OWNER/NAME, got: $repo" ;;
    esac
fi

command -v gh >/dev/null 2>&1 || die "gh is not installed; install the GitHub CLI"

# Run gh against the chosen repository; arguments are passed through untouched.
gh_repo() {
    if [ -n "$repo" ]; then
        gh "$@" --repo "$repo"
    else
        gh "$@"
    fi
}

state=$(gh_repo release view "$tag" --json isDraft,isPrerelease \
    --jq '"\(.isDraft) \(.isPrerelease)"') || die "cannot read release $tag"
case $state in
    "true "*) die "$tag is a draft; publish.yml never ran for it" ;;
    "false true") ;;
    "false false") die "$tag is not a pre-release: it is already published" ;;
    *) die "unexpected release state for $tag: $state" ;;
esac

assets=$(gh_repo release view "$tag" --json assets --jq '.assets[].name') \
    || die "cannot list the assets of $tag"
for want in install.sh "ciaobot-$version-py3-none-any.whl" "Ciaobot-$version.dmg"; do
    printf '%s\n' "$assets" | grep -Fxq "$want" \
        || die "$tag has no $want; wait for publish.yml and attach the DMG first"
done

gh_repo release edit "$tag" --prerelease=false --latest \
    || die "gh release edit failed"
printf 'Promoted %s to the latest release\n' "$tag"
