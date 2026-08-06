#!/usr/bin/env bash
# Update Formula/ciaobot.rb and Casks/ciaobot-desktop.rb in a checked-out tap.
#
# Usage:
#   ./scripts/update-homebrew-tap.sh <version> <wheel-sha256> <dmg-sha256> [tap-root]
#
# Example:
#   ./scripts/update-homebrew-tap.sh 0.6.1 33efb3... 8c61aa... /tmp/homebrew-ciaobot
#
# When tap-root is omitted, updates deploy/homebrew/ciaobot.rb in the repo root
# (useful for review before pushing to the tap repository).

set -euo pipefail

VERSION="${1:?version required, e.g. 0.4.5}"
SHA256="${2:?wheel sha256 required}"
DMG_SHA256="${3:?desktop DMG sha256 required}"
TAP_ROOT="${4:-}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "$TAP_ROOT" ]]; then
  TAP_ROOT="$REPO_ROOT/deploy/homebrew"
  mkdir -p "$TAP_ROOT"
  FORMULA_PATH="$TAP_ROOT/ciaobot.rb"
  CASK_PATH="$TAP_ROOT/ciaobot-desktop.rb"
else
  mkdir -p "$TAP_ROOT/Formula"
  mkdir -p "$TAP_ROOT/Casks"
  FORMULA_PATH="$TAP_ROOT/Formula/ciaobot.rb"
  CASK_PATH="$TAP_ROOT/Casks/ciaobot-desktop.rb"
fi

WHEEL_URL="https://github.com/raffaelefarinaro/ciaobot/releases/download/v${VERSION}/ciaobot-${VERSION}-py3-none-any.whl"

cat >"$FORMULA_PATH" <<EOF
class Ciaobot < Formula
  include Language::Python::Virtualenv

  desc "Local-first personal assistant server"
  homepage "https://github.com/raffaelefarinaro/ciaobot"
  url "${WHEEL_URL}"
  version "${VERSION}"
  sha256 "${SHA256}"
  license "Apache-2.0"

  depends_on "python@3.12"

  def install
    python = Formula["python@3.12"].opt_bin/"python3.12"
    virtualenv_create(libexec, python)
    # Install only the app wheel here; it is pure Python, so Homebrew's
    # install-linkage step finds no Mach-O files to rewrite. The dependency
    # tree is installed in post_install: prebuilt wheels such as jiter ship
    # dylibs with @rpath install names and no Mach-O header padding, and the
    # linkage fixer aborts on them ("Failed to fix install linkage").
    system libexec/"bin/python", "-m", "pip", "install", "--no-deps",
           buildpath.glob("ciaobot-*.whl").first
    bin.install_symlink Dir[libexec/"bin/ciao*"]
  end

  def post_install
    # Resolve the app's pinned dependency tree from PyPI now, after the
    # install-linkage step has run, so dependency wheels keep their dylib
    # install names as built (wheels are self-contained and need no rewrite).
    # The app itself is already installed, so pip only adds what is missing.
    system libexec/"bin/python", "-m", "pip", "install", "ciaobot==#{version}"

    # Setup cannot run here: Homebrew's post-install sandbox blocks launchctl
    # and fakes HOME. Point the user at the browser setup wizard instead.
    puts <<~BANNER

      ##############################################################
      #                                                            #
      #   Ciaobot is installed! To finish setup, run:              #
      #                                                            #
      #       ciao run                                             #
      #                                                            #
      #   then open http://localhost:8443 in your browser and      #
      #   follow the setup wizard.                                 #
      #                                                            #
      ##############################################################

    BANNER
  end

  def caveats
    <<~CAVEATS
      Finish setup with \`ciao run\`, then open http://localhost:8443 and
      follow the wizard: it asks for a workspace folder and a model
      provider, then installs the background engine and Ciaobot.app
      (the native window, menu bar, and notifications).

      The app install verifies the release signature and needs no
      Gatekeeper approval. If it is skipped or fails, add it later with:

        ciao desktop install

      Scripted or headless setups can skip the wizard:

        ciao setup --workspace <dir>
    CAVEATS
  end

  test do
    assert_match "usage:", shell_output("#{bin}/ciao --help")
  end
end
EOF

echo "Updated ${FORMULA_PATH}"

cat >"$CASK_PATH" <<EOF
cask "ciaobot-desktop" do
  version "${VERSION}"
  sha256 "${DMG_SHA256}"

  url "https://github.com/raffaelefarinaro/ciaobot/releases/download/v#{version}/Ciaobot_#{version}_universal.dmg"
  name "Ciaobot"
  desc "Native macOS shell for the local-first Ciaobot assistant"
  homepage "https://github.com/raffaelefarinaro/ciaobot"

  depends_on formula: "ciaobot"
  depends_on macos: :ventura
  auto_updates true

  app "Ciaobot.app"

  uninstall quit: "local.ciaobot.app"

  caveats <<~EOS
    \`ciao desktop install\` installs the same app without this first-launch
    block, because Homebrew quarantines what it downloads and a command-line
    download does not. Prefer it unless you specifically want the cask.

    Ciaobot is ad-hoc signed and is not notarized, so macOS blocks the first
    launch with "Apple could not verify Ciaobot is free of malware".

    To allow it: open Ciaobot.app once, then go to System Settings -> Privacy &
    Security, scroll to Security, and click "Open Anyway" next to the Ciaobot
    message. Authenticate, launch the app again, and confirm Open.

    Control-clicking the app and choosing Open does not clear this dialog --
    Apple removed that bypass in macOS 15. The "Open Anyway" button only appears
    for about an hour after a blocked launch. Do not disable Gatekeeper.
  EOS
end
EOF

echo "Updated ${CASK_PATH}"
