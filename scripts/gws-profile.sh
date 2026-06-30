#!/usr/bin/env bash
# GWS dual-account profile wrapper.
# Usage: gws-profile <work|personal> <gws-args...>
# Or: GWS_PROFILE=work gws-profile <gws-args...>
#
# Profiles:
#   personal → ~/.config/gws-personal/
#   work     → ~/.config/gws/

PROFILE="${1:-${GWS_PROFILE:-personal}}"

# If first arg looks like a profile name, consume it; otherwise treat all args as gws args
case "$PROFILE" in
  work|personal)
    shift
    ;;
  *)
    PROFILE="${GWS_PROFILE:-personal}"
    ;;
esac

# Resolve repo root (where this script lives)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

case "$PROFILE" in
  work)
    export GOOGLE_WORKSPACE_CLI_CONFIG_DIR="${REPO_ROOT}/secrets/gws"
    ;;
  *)
    export GOOGLE_WORKSPACE_CLI_CONFIG_DIR="${REPO_ROOT}/secrets/gws-personal"
    ;;
esac

# If the config directory is missing credentials, attempt to restore from env
if [ ! -f "${GOOGLE_WORKSPACE_CLI_CONFIG_DIR}/credentials.json" ] || [ ! -f "${GOOGLE_WORKSPACE_CLI_CONFIG_DIR}/client_secret.json" ]; then
  if [ -f "${REPO_ROOT}/scripts/gws-secrets.py" ]; then
    python3 "${REPO_ROOT}/scripts/gws-secrets.py" restore "$PROFILE"
  fi
fi

# Unset GOOGLE_APPLICATION_CREDENTIALS because the repo .env stores it as a
# base64 string (used by BigQuery runner), but gws expects a file path.
# GWS must use its own OAuth token cache, not a service account.
unset GOOGLE_APPLICATION_CREDENTIALS

exec gws "$@"
