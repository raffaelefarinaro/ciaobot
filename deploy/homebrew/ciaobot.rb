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
    # Install the wheel *with* its dependency tree. Homebrew's
    # `virtualenv` `pip_install` helpers pass --no-deps (they expect every
    # dependency vendored as a `resource`); this app has none, so use pip
    # directly and let it resolve the pinned deps from PyPI at install time.
    system libexec/"bin/python", "-m", "pip", "install",
           buildpath.glob("ciaobot-*.whl").first
    bin.install_symlink Dir[libexec/"bin/ciao*"]
  end

  def post_install
    workspace = ENV.fetch("CIAO_WORKSPACE", File.expand_path("~/ciaobot"))
    setup_command = "#{bin}/ciao setup --workspace #{workspace}"

    unless ciao_gui_session?
      ohai "Ciaobot installed. Open Terminal and run `#{setup_command}` to finish."
      return
    end

    system bin/"ciao",
           "setup",
           "--workspace", workspace,
           "--python", "#{libexec}/bin/python",
           "--load-launchd"
  rescue StandardError => e
    opoo "Ciaobot installed, but automatic setup did not complete: #{e.message}"
    opoo "Open Terminal and run `#{setup_command}` to finish."
  end

  def ciao_gui_session?
    return false if ENV["CI"]
    return false if ENV["SSH_CONNECTION"] || ENV["SSH_TTY"]
    return false if ENV["HOMEBREW_CIAOBOT_SKIP_SETUP"]

    system "/bin/launchctl", "print", "gui/#{Process.uid}",
           out: File::NULL,
           err: File::NULL
  end

  test do
    assert_match "usage:", shell_output("#{bin}/ciao --help")
  end
end
