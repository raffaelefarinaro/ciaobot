class Ciaobot < Formula
  include Language::Python::Virtualenv

  desc "Local-first personal assistant server"
  homepage "https://github.com/raffaelefarinaro/ciaobot"
  url "https://github.com/raffaelefarinaro/ciaobot/releases/download/v0.5.2/ciaobot-0.5.2-py3-none-any.whl"
  version "0.5.2"
  sha256 "d1589d4cdff9ebd02788edbabeeb8500335e47abbe940b53b6f79b32e2cc22cb"
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
      Finish setup with `ciao run`, then open http://localhost:8443 and
      follow the wizard: it asks for a workspace folder and a model
      provider, then installs Ciaobot.app and the background server.
      Afterwards, open Ciaobot.app.

      Scripted or headless setups can skip the wizard:

        ciao setup --workspace <dir>
    CAVEATS
  end

  test do
    assert_match "usage:", shell_output("#{bin}/ciao --help")
  end
end
