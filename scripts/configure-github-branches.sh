#!/usr/bin/env bash
# Configure GitHub branch protection for the develop/main release model.
# Run from the repo root with an authenticated `gh` session and admin access.
set -euo pipefail

repo="${1:-raffaelefarinaro/ciaobot}"

protection_json() {
  cat <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["test"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
}

echo "Setting default branch to develop on ${repo}..."
gh api "repos/${repo}" -X PATCH -f default_branch=develop

for branch in develop main; do
  echo "Protecting ${branch}..."
  protection_json | gh api "repos/${repo}/branches/${branch}/protection" -X PUT --input -
done

echo "Done. Feature PRs should target develop; release PRs target main."
