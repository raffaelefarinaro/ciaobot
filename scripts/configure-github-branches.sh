#!/usr/bin/env bash
# Configure GitHub branch protection for the develop/main release model.
# Run from the repo root with an authenticated `gh` session and admin access.
set -euo pipefail

repo="${1:-raffaelefarinaro/ciaobot}"

# develop: the checks a develop PR runs (the macOS `test` job does not run
# there). Admins stay exempt so release-on-main.yml can merge its
# sync-develop PR with `gh pr merge --admin`.
develop_json() {
  cat <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "contexts": ["linux-server", "linux-web", "windows"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
}

# main: release PRs only, and they run the full matrix (Windows suites and the
# macOS job), so every check is required, for admins too.
main_json() {
  cat <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "contexts": ["linux-server", "linux-web", "windows", "test"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
}

echo "Setting default branch to develop on ${repo}..."
gh api "repos/${repo}" -X PATCH -f default_branch=develop

echo "Protecting develop..."
develop_json | gh api "repos/${repo}/branches/develop/protection" -X PUT --input -
echo "Protecting main..."
main_json | gh api "repos/${repo}/branches/main/protection" -X PUT --input -

echo "Done. Feature PRs should target develop; release PRs target main."
