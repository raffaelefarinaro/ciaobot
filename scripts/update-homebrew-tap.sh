#!/usr/bin/env bash
# Update Formula/ciaobot.rb in a checked-out homebrew-ciaobot tap.
#
# Usage:
#   ./scripts/update-homebrew-tap.sh <version> <wheel-sha256> [tap-root]
#
# Example:
#   ./scripts/update-homebrew-tap.sh 0.4.5 33efb3... /tmp/homebrew-ciaobot
#
# When tap-root is omitted, updates deploy/homebrew/ciaobot.rb in the repo root
# (useful for review before pushing to the tap repository).

set -euo pipefail

VERSION="${1:?version required, e.g. 0.4.5}"
SHA256="${2:?wheel sha256 required}"
TAP_ROOT="${3:-}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "$TAP_ROOT" ]]; then
  TAP_ROOT="$REPO_ROOT/deploy/homebrew"
  mkdir -p "$TAP_ROOT"
  FORMULA_PATH="$TAP_ROOT/ciaobot.rb"
else
  mkdir -p "$TAP_ROOT/Formula"
  FORMULA_PATH="$TAP_ROOT/Formula/ciaobot.rb"
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
    # Install the wheel *with* its dependency tree. Homebrew's
    # \`virtualenv\` \`pip_install\` helpers pass --no-deps (they expect every
    # dependency vendored as a \`resource\`); this app has none, so use pip
    # directly and let it resolve the pinned deps from PyPI at install time.
    system libexec/"bin/python", "-m", "pip", "install",
           buildpath.glob("ciaobot-*.whl").first
    bin.install_symlink Dir[libexec/"bin/ciao*"]
  end

  def post_install
    workspace = ENV.fetch("CIAO_WORKSPACE", File.expand_path("~/ciaobot"))
    setup_command = "ciao setup --workspace #{workspace}"

    unless ciao_gui_session?
      ohai "Next step: run \`#{setup_command}\` to finish setup (see \`brew info ciaobot\`)."
      return
    end

    system bin/"ciao",
           "setup",
           "--workspace", workspace,
           "--python", "#{libexec}/bin/python",
           "--load-launchd"
  rescue StandardError => e
    opoo "Automatic setup did not complete: #{e.message}"
    opoo "Run \`#{setup_command}\` to finish setup."
  end

  def ciao_gui_session?
    return false if ENV["CI"]
    return false if ENV["SSH_CONNECTION"] || ENV["SSH_TTY"]
    return false if ENV["HOMEBREW_CIAOBOT_SKIP_SETUP"]

    system "/bin/launchctl", "print", "gui/#{Process.uid}",
           out: File::NULL,
           err: File::NULL
  end

  def caveats
    <<~CAVEATS
      To finish setting up Ciaobot, run:

        ciao setup --workspace ~/ciaobot

      This scaffolds the workspace, installs the Ciaobot menu bar app and
      background server, starts it, and prints a link to open Ciaobot at
      http://localhost:8443 (macOS may run this automatically on install).

      Afterwards, open Ciaobot anytime from the menu bar icon or
      /Applications/Ciaobot.app. To use a different folder (for example an
      existing notes folder), change the --workspace path above.
    CAVEATS
  end

  test do
    assert_match "usage:", shell_output("#{bin}/ciao --help")
  end
end
EOF

echo "Updated ${FORMULA_PATH}"
