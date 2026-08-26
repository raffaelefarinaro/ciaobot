#!/usr/bin/env bash
# Back-compat shim for the old GWS profile wrapper.
#
# The canonical entry point is now `ciao gws` (which ships inside the installed
# app). This thin shim forwards to it so dev checkouts and any external
# references keep working unchanged.
exec ciao gws "$@"
