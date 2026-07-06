from __future__ import annotations

from pathlib import Path


def _formula_text() -> str:
    return (Path(__file__).parents[1] / "deploy" / "homebrew" / "ciao.rb").read_text(
        encoding="utf-8"
    )


def test_homebrew_formula_installs_python312_virtualenv_package() -> None:
    text = _formula_text()

    assert 'require "language/python/virtualenv"' in text
    assert "include Language::Python::Virtualenv" in text
    assert 'depends_on "python@3.12"' in text
    assert 'Formula["python@3.12"].opt_bin/"python3.12"' in text
    assert "virtualenv_create(libexec, python)" in text
    assert "venv.pip_install_and_link buildpath" in text
    assert 'bin/"ciao"' in text


def test_homebrew_formula_postinstall_runs_setup_or_prints_headless_fallback() -> None:
    text = _formula_text()

    assert "def post_install" in text
    assert 'ENV.fetch("CIAO_WORKSPACE", File.expand_path("~/ciao"))' in text
    assert '"setup",' in text
    assert '"--workspace", workspace' in text
    assert '"--python", "#{libexec}/bin/python"' in text
    assert '"--load-launchd"' in text
    assert "ciao_gui_session?" in text
    assert "Open Terminal.app and run" in text
    assert "HOMEBREW_CIAO_SKIP_SETUP" in text
    assert "SSH_CONNECTION" in text
    assert 'launchctl", "print", "gui/#{Process.uid}"' in text


CANONICAL_REPO = "https://github.com/raffaelefarinaro/ciaobot"


def test_homebrew_formula_points_at_canonical_public_repo() -> None:
    text = _formula_text()

    assert f'homepage "{CANONICAL_REPO}"' in text
    assert f'url "{CANONICAL_REPO}/archive/refs/tags/v' in text
    assert f'head "{CANONICAL_REPO}.git", branch: "main"' in text
    # Every GitHub reference must be the canonical public repo.
    for line in text.splitlines():
        if "github.com" in line:
            assert CANONICAL_REPO in line, line


def test_homebrew_formula_has_no_private_distribution_markers() -> None:
    text = _formula_text()

    forbidden = (
        "scan" + "dit",
        "sdc" + "-labs",
        "/Users/",
    )
    lowered = text.lower()
    for marker in forbidden:
        assert marker.lower() not in lowered
