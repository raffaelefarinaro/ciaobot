#!/usr/bin/env bash
# The desktop gate: everything CI's build-desktop job would catch, run locally.
#
# `pytest tests/` and the two `npm run build`s never touch Rust, the Swift
# sidecar, or Tauri bundling, so a change under desktop/ could pass every local
# check and still fail on GitHub — where the failure costs a release. This runs
# the same commands CI does, in the same order, against the same pinned
# toolchain (desktop/rust-toolchain.toml).
#
# Usage:
#   ./scripts/check-desktop.sh            # fmt, clippy, cargo test, sidecar, bundle
#   ./scripts/check-desktop.sh --fast     # skip the bundle build (~1 min instead of ~3)
#
# The bundle step is what catches externalBin/sidecar mistakes: a wrong
# target-triple suffix only shows up when Tauri actually assembles the .app.
# Skip it only when you have not touched desktop/native/ or tauri.conf.json.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP="$REPO_ROOT/desktop"
FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

if [[ ! -d "$DESKTOP/src-tauri" ]]; then
  echo "No Tauri project at $DESKTOP/src-tauri; nothing to check." >&2
  exit 0
fi

# rustup installs into ~/.cargo/bin, which a non-login shell (and launchd) does
# not have on PATH. Mirrors desktop_build.ensure_cargo_on_path.
for candidate in "$HOME/.cargo/bin" /opt/homebrew/opt/rustup/bin /usr/local/opt/rustup/bin; do
  [[ -x "$candidate/cargo" ]] && PATH="$candidate:$PATH"
done
export PATH

missing=""
command -v cargo >/dev/null 2>&1 || missing="cargo (install: brew install rustup && rustup default 1.90.0)"
command -v swiftc >/dev/null 2>&1 || missing="${missing:+$missing; }swiftc (install: xcode-select --install)"
if [[ -n "$missing" ]]; then
  echo "Cannot run the desktop gate — missing: $missing" >&2
  exit 1
fi

# Tauri resolves every bundle.resources path while running the build script, so
# an absent desktop/runtime aborts `cargo fmt`/`clippy`/`test` before a single
# check runs — the gate is unusable on a fresh checkout. CI builds the real
# runtime first (.github/workflows/ci.yml), but that downloads a pinned CPython
# and takes minutes, which is more than this gate is for. A placeholder
# satisfies the path lookup; none of the Rust checks read its contents.
RUNTIME="$DESKTOP/runtime"
STUBBED_RUNTIME=0
if [[ ! -d "$RUNTIME" ]]; then
  mkdir -p "$RUNTIME"
  cat > "$RUNTIME/README.txt" <<'EOF'
Placeholder created by scripts/check-desktop.sh so Tauri can resolve the
`../runtime` resource path. The real bundled Python runtime is built by
scripts/build-bundled-runtime.sh. An app bundled against this placeholder has
no engine inside it: resolve_ciao() finds no Contents/Resources/ciao-runtime/
bin/ciao and reports "Ciaobot engine unavailable".
EOF
  STUBBED_RUNTIME=1
fi

runtime_note() {
  [[ "$STUBBED_RUNTIME" -eq 1 ]] || return 0
  echo
  echo "Note: desktop/runtime was missing, so a placeholder was created. The"
  echo "Rust checks above are unaffected, but any .app built from this tree has"
  echo "no engine bundled. Run scripts/build-bundled-runtime.sh for a real one."
}

step() { printf '\n=== %s ===\n' "$1"; }

step "sidecar build (swiftc, aarch64)"
"$DESKTOP/native/build.sh"

step "cargo fmt --check"
(cd "$DESKTOP/src-tauri" && cargo fmt --check)

step "cargo clippy --all-targets -- -D warnings"
(cd "$DESKTOP/src-tauri" && cargo clippy --all-targets -- -D warnings)

step "cargo test"
(cd "$DESKTOP/src-tauri" && cargo test)

if [[ "$FAST" -eq 1 ]]; then
  echo
  echo "Desktop gate passed (--fast: bundle build skipped)."
  runtime_note
  exit 0
fi

step "tauri build (app bundle, aarch64)"
# aarch64 is what release CI builds, and it resolves a different sidecar
# filename than a native build, so this is the shape worth checking. Updater
# artifacts are disabled: signing them needs TAURI_SIGNING_PRIVATE_KEY, which
# only CI holds.
(cd "$DESKTOP" && npm run tauri build -- \
  --target aarch64-apple-darwin \
  --bundles app \
  --config '{"bundle":{"createUpdaterArtifacts":false}}')

APP="$DESKTOP/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/Ciaobot.app"
SIDECAR="$APP/Contents/MacOS/ciaobot-native"

step "bundle contents"
[[ -x "$SIDECAR" ]] || { echo "FAIL: $SIDECAR missing — externalBin did not bundle the sidecar" >&2; exit 1; }
codesign -v "$APP" || { echo "FAIL: bundle signature invalid" >&2; exit 1; }
"$SIDECAR" probe >/dev/null \
  || { echo "FAIL: bundled sidecar does not run" >&2; exit 1; }

echo
echo "Desktop gate passed: sidecar bundled, aarch64, signed, and runnable."
runtime_note
