#!/usr/bin/env bash
# Build the native macOS sidecar (ciaobot-native) for Tauri to bundle.
#
# Tauri's externalBin looks for `<name>-<target-triple>` next to the configured
# path, so every triple a build might ask for gets its own copy: the two native
# ones for a dev build, plus the universal binary release builds use. They are
# ~150 KB each, so shipping all three costs less than getting the naming wrong.
#
# Deployment target is 13.0, matching tauri.conf.json's minimumSystemVersion.
# The binary therefore loads on every Mac the app supports; the subcommands that
# need newer frameworks (`hear`, `respond`) report exit code 65 on anything older
# than macOS 26, which the engine turns into a clear message instead of a crash.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$HERE/../src-tauri/binaries"
DEPLOYMENT_TARGET="13.0"
NAME="ciaobot-native"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "swiftc not found. Install the Xcode Command Line Tools: xcode-select --install" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

build_slice() {
  local arch="$1"
  swiftc \
    -parse-as-library \
    -O \
    -target "${arch}-apple-macosx${DEPLOYMENT_TARGET}" \
    -o "$WORK/$arch" \
    "$HERE/main.swift"
}

build_slice arm64
build_slice x86_64

lipo -create -output "$WORK/universal" "$WORK/arm64" "$WORK/x86_64"

# One copy per triple Tauri may ask for. aarch64 is the Rust spelling of arm64.
cp "$WORK/universal" "$OUT_DIR/${NAME}-universal-apple-darwin"
cp "$WORK/arm64" "$OUT_DIR/${NAME}-aarch64-apple-darwin"
cp "$WORK/x86_64" "$OUT_DIR/${NAME}-x86_64-apple-darwin"
chmod +x "$OUT_DIR/${NAME}-"*

echo "Built ${NAME} into ${OUT_DIR}:"
lipo -info "$OUT_DIR/${NAME}-universal-apple-darwin"
