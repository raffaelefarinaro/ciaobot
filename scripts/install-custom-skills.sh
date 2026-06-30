#!/usr/bin/env bash
# Compatibility wrapper. The implementation lives in the packaged CLI.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
exec python3 -m ciao.cli sync-skills --workspace "$REPO_ROOT" "$@"
