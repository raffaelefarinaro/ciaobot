#!/usr/bin/env bash
# Compatibility wrapper for `ciao dev`.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"${ROOT_DIR}/scripts/ensure-deps.sh" || { echo "Dependency setup failed."; exit 1; }

source "${ROOT_DIR}/.venv/bin/activate"
exec python -m ciao.cli dev --workspace "${ROOT_DIR}" "$@"
