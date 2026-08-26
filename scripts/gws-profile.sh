#!/usr/bin/env bash
# Back-compat shim for the old GWS profile wrapper.
#
# The canonical entry point is now `ciao gws` (which ships inside the installed
# app). This thin shim forwards to it so dev checkouts and any external
# references keep working unchanged.
#
# When invoked from a dev checkout, expose the checkout as the workspace root so
# `ciao gws` resolves the credential directory there (platform-independent; the
# macOS LaunchAgent plist is not present on every host). Only sets it when the
# caller did not already export one.
if [ -z "${CIAO_WORKSPACE:-}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  export CIAO_WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
exec ciao gws "$@"
