#!/usr/bin/env bash
#
# Evil-merge detector.
#
# A merge commit is supposed to be the automatic 3-way merge of its parents.
# Where it isn't, someone edited during conflict resolution — and that is where
# work silently disappears: the resolution takes one side wholesale, a feature's
# other half goes with it, and because the resolution usually deletes the
# feature's tests in the same stroke, CI stays green over the hole.
#
# This replays the automatic merge of every merge commit in a range and reports
# where the committed tree differs. A non-empty delta is not automatically a
# bug — a real conflict has to be resolved by hand — but every delta is a hand
# edit that no diff review covers, so each one needs a human look.
#
# v0.12.0 shipped this failure: merge ed04d1e9 reverted the entire frontend
# half of the subagent-subchat feature and committed a 3533-line
# ProjectSidebar.vue.tmp scratch file, while its backend half stayed live. The
# PR was green. This script found it in seconds.
#
# Usage: scripts/check-merges.sh [range]     (default: <last tag>..develop)

set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1

range="${1:-$(git describe --tags --abbrev=0)..develop}"
found=0

echo "Replaying merges in ${range}…"
echo

for m in $(git rev-list --merges "$range"); do
  # shellcheck disable=SC2046
  set -- $(git log -1 --format='%P' "$m")
  # Octopus merges have no single automatic result to compare against.
  [ $# -ne 2 ] && continue

  auto=$(git merge-tree --write-tree "$1" "$2" 2>/dev/null | head -1)
  if [ -z "$auto" ]; then
    echo "== $m  $(git log -1 --format='%s' "$m")"
    echo "     (parents conflict; resolution cannot be replayed — review by hand)"
    echo
    found=1
    continue
  fi

  delta=$(git diff --stat "$auto" "$m^{tree}" 2>/dev/null)
  if [ -n "$delta" ]; then
    echo "== $m  $(git log -1 --format='%s' "$m")"
    printf '%s\n' "$delta" | sed 's/^/     /'
    echo
    found=1
  fi
done

if [ "$found" -eq 0 ]; then
  echo "No hand edits in any merge in ${range}."
  exit 0
fi

cat <<'EOF'
Each block above is a hand edit made while resolving a merge. For every one,
confirm the resolution kept what both sides intended:

  git diff $(git merge-tree --write-tree <parent1> <parent2> | head -1) <merge>^{tree}

Watch for a resolution that took one side wholesale — especially one that
deleted a test file along with the code it covered, which is how this class of
regression reaches a green PR.
EOF
exit 1
