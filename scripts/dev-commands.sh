#!/usr/bin/env bash
set -euo pipefail

# Helper script for Ciaobot dev quality gates: typechecking, coverage, linting, and security audits.

function usage() {
  echo "Usage: $0 {test|coverage|typecheck|audit|lint|all}"
  exit 1
}

if [ $# -lt 1 ]; then
  usage
fi

COMMAND="$1"

case "$COMMAND" in
  test)
    echo "=== Running Pytest ==="
    pytest tests/
    ;;
  coverage)
    echo "=== Running Pytest with Coverage ==="
    pytest --cov=ciao --cov-report=term-missing tests/
    ;;
  typecheck)
    echo "=== Running Mypy Typecheck ==="
    mypy ciao
    ;;
  audit)
    echo "=== Running Dependency Security Audits ==="
    echo "[1/2] Python pip-audit:"
    pip-audit --desc || true
    echo "[2/2] Frontend npm audit:"
    (cd web && npm audit --audit-level=high) || true
    ;;
  lint)
    echo "=== Running Frontend Lint ==="
    (cd web && npm run lint)
    ;;
  all)
    echo "=== Running All Dev Quality Checks ==="
    "$0" typecheck
    "$0" coverage
    "$0" lint
    "$0" audit
    ;;
  *)
    usage
    ;;
esac
