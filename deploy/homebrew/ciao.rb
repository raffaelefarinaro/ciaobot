require "language/python/virtualenv"

class Ciao < Formula
  include Language::Python::Virtualenv

  desc "Local-first personal assistant server"
  homepage "https://github.com/raffaelefarinaro/ciaobot"
  license "Apache-2.0"
  url "https://github.com/raffaelefarinaro/ciaobot/releases/download/v0.2.1/ciao-0.2.1.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  head "https://github.com/raffaelefarinaro/ciaobot.git", branch: "main"

  depends_on "python@3.12"

  def install
    python = Formula["python@3.12"].opt_bin/"python3.12"
    venv = virtualenv_create(libexec, python)
    venv.pip_install_and_link buildpath
    # Menu bar companion (rumps/pyobjc); optional extra kept out of the base wheel.
    venv.pip_install "#{buildpath}[menubar]"
  end

  def post_install
    workspace = ENV.fetch("CIAO_WORKSPACE", File.expand_path("~/ciao"))
    setup_command = "#{bin}/ciao setup --workspace #{workspace}"

    unless ciao_gui_session?
      opoo "Ciao installed. Open Terminal.app and run `#{setup_command}` to finish."
      return
    end

    system bin/"ciao",
      "setup",
      "--workspace", workspace,
      "--python", "#{libexec}/bin/python",
      "--load-launchd"
  rescue StandardError => e
    opoo "Ciao installed, but automatic setup did not complete: #{e.message}"
    opoo "Open Terminal.app and run `#{setup_command}` to finish."
  end

  def ciao_gui_session?
    return false if ENV["CI"]
    return false if ENV["SSH_CONNECTION"] || ENV["SSH_TTY"]
    return false if ENV["HOMEBREW_CIAO_SKIP_SETUP"]

    system "/bin/launchctl", "print", "gui/#{Process.uid}",
      out: File::NULL,
      err: File::NULL
  end

  test do
    assert_match "usage:", shell_output("#{bin}/ciao --help")
  end
end
