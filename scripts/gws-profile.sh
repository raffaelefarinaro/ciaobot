#!/usr/bin/env bash
# GWS account profile wrapper.
# Usage: gws-profile <profile> <gws-args...>
# Or: GWS_PROFILE=<profile> gws-profile <gws-args...>
#
# Credential directories (under the workspace root):
#   personal → secrets/gws-personal/   (legacy name, kept for existing installs)
#   work     → secrets/gws/            (legacy name, kept for existing installs)
#   <other>  → secrets/gws-<other>/
#
# Profiles are whatever the user added in Settings → Workspaces; this wrapper
# does not keep a list of its own.

PROFILE="${1:-${GWS_PROFILE:-}}"

# A first arg that starts with `-` or names a gws service is not a profile, so
# fall back to the environment and pass every arg through to gws. Settings
# rejects these service names as account slugs because this positional syntax
# cannot otherwise distinguish the profile from the service argument.
case "$PROFILE" in
  ""|-*|gmail|calendar|drive|docs|sheets|slides|tasks|contacts|forms|auth)
    PROFILE="${GWS_PROFILE:-}"
    ;;
  *)
    shift
    ;;
esac

if [ -z "$PROFILE" ]; then
  echo "gws-profile: no profile given and GWS_PROFILE is unset" >&2
  exit 2
fi

# Resolve repo root (where this script lives)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Same slug rule as ciao.gws_auth.slugify_profile: lowercase, and anything
# outside [a-z0-9_-] collapses to a dash.
SLUG="$(printf '%s' "$PROFILE" | tr '[:upper:]' '[:lower:]' | sed -e 's/[^a-z0-9_-]\{1,\}/-/g' -e 's/^-*//' -e 's/-*$//')"

case "$SLUG" in
  work)
    export GOOGLE_WORKSPACE_CLI_CONFIG_DIR="${REPO_ROOT}/secrets/gws"
    ;;
  "")
    echo "gws-profile: '$PROFILE' is not a usable profile name" >&2
    exit 2
    ;;
  *)
    export GOOGLE_WORKSPACE_CLI_CONFIG_DIR="${REPO_ROOT}/secrets/gws-${SLUG}"
    ;;
esac

# Unset GOOGLE_APPLICATION_CREDENTIALS because the repo .env stores it as a
# base64 string (used by BigQuery runner), but gws expects a file path.
# GWS must use its own OAuth token cache, not a service account.
unset GOOGLE_APPLICATION_CREDENTIALS

exec gws "$@"
