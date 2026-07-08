class Ciaobot < Formula
  include Language::Python::Virtualenv

  desc "Local-first personal assistant server"
  homepage "https://github.com/raffaelefarinaro/ciaobot"
  url "https://github.com/raffaelefarinaro/ciaobot/releases/download/v0.4.7/ciaobot-0.4.7-py3-none-any.whl"
  version "0.4.7"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
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
      To finish setting up Ciaobot, run:

        ciao run

      and open http://localhost:8443 in your browser. The setup wizard asks
      for a workspace folder (default ~/ciaobot) and a model provider, then
      writes the config and installs the Ciaobot menu bar app and background
      server. Afterwards, open Ciaobot anytime from the menu bar icon or
      /Applications/Ciaobot.app.

      For scripted or headless setups, skip the wizard with:

        ciao setup --workspace <dir>
    CAVEATS
  end

  test do
    assert_match "usage:", shell_output("#{bin}/ciao --help")
  end
end
