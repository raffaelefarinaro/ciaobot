from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_installer_script_is_posix_shell() -> None:
    result = subprocess.run(
        ["sh", "-n", str(REPO_ROOT / "scripts" / "install.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_installer_requires_native_verification_before_extraction() -> None:
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "__VERIFIER_SHA256__" in script
    assert 'placeholder=__VERIFIER_SHA"256__"' in script
    assert '"$verifier" "$archive" "$signature"' in script
    assert script.index('"$verifier" "$archive" "$signature"') < script.index(
        'tar -xzf "$archive"'
    )
    assert "ciao-runtime/bin/ciao" in script
    assert 'ciao-runtime/bin/ciao" --help' in script
    assert 'CIAO_RELEASE_BASE_URL' in script


def test_installer_starts_and_persists_the_menu_bar_agent() -> None:
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert 'desktop_plist="$HOME/Library/LaunchAgents/Ciaobot.plist"' in script
    assert '<string>--background</string>' in script
    assert 'launchctl bootstrap "gui/$uid" "$desktop_plist"' in script
    assert 'launchctl kickstart "gui/$uid/Ciaobot"' in script


def test_installer_does_not_swallow_existing_workspace_setup_failure() -> None:
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    setup_block = script[script.index('if [ -n "$workspace" ]; then') :]
    assert '"$engine" setup' in setup_block
    assert "--load-launchd" in setup_block
    assert '"$engine" setup \\\n            --workspace "$workspace"' in setup_block
    assert '>/dev/null || true' not in setup_block


def test_installer_retries_until_the_menu_bar_agent_names_the_new_app() -> None:
    # bootout is asynchronous, so a bootstrap issued immediately after it can
    # fail outright, or appear to succeed while launchd still holds the stale
    # job — which names the bundle this install just moved aside. Swallowing
    # either outcome leaves an updated install with no menu-bar app, and that
    # is invisible until the user looks for the tray icon.
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    reload_block = script[script.index('if [ "$no_start" -eq 0 ]; then') :]
    assert 'grep -qF "$desktop_executable"' in reload_block
    assert "the menu-bar LaunchAgent did not load" in reload_block
    # Verify before acting: a job that already names the new executable must not
    # be torn down, or a mis-detection would boot a working agent out on every
    # one of the retries.
    assert reload_block.index('grep -qF "$desktop_executable"') < (
        reload_block.index('launchctl bootout "gui/$uid/Ciaobot"')
    )
    assert reload_block.index('launchctl bootout "gui/$uid/Ciaobot"') < (
        reload_block.index('launchctl bootstrap "gui/$uid" "$desktop_plist"')
    )
    assert "grep" in script[: script.index("[ \"$(uname -s)\" = \"Darwin\" ]")]


def test_fresh_install_defers_workspace_creation_to_onboarding() -> None:
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    # A configured workspace is recovered from the existing LaunchAgent. With
    # no valid workspace, the app must start in its built-in bootstrap mode;
    # setup must not invent a password in a new ~/Ciaobot directory first.
    assert 'workspace="$HOME/Ciaobot"' not in script
    assert '[ -f "$existing_workspace/.env" ]' in script
    assert 'if [ -n "$workspace" ]; then' in script
    assert 'first-run onboarding will ask where to create or adopt one' in script


def test_release_workflows_do_not_publish_removed_install_channels() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml")
    )

    assert "update-homebrew-tap" not in workflows
    assert "pypi:" not in workflows
    assert ".dmg" not in workflows.lower()
    assert "brew install" not in workflows
    assert "mapfile" not in workflows


def test_release_smoke_only_runs_with_a_published_version() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release-smoke.yml").read_text(
        encoding="utf-8"
    )

    # Pull requests do not populate workflow_call/workflow_dispatch inputs, so
    # triggering this release-only smoke test there would normalize an empty
    # version and fail before the installer is exercised.
    assert "workflow_call:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert 'LaunchAgents/com.ciao.server.plist")' not in workflow


def _shim_block() -> str:
    """The installer's ``ciao`` shim logic, runnable on its own.

    The surrounding script downloads and verifies a signed release, so the
    block is exercised in isolation with ``engine`` supplied by the caller.
    """
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    start = script.index('shim_dir="$HOME/.local/bin"')
    end = script.index('\nplist="$HOME/Library/LaunchAgents', start)
    return script[start:end]


def _run_shim_block(home: Path, engine: Path) -> subprocess.CompletedProcess[str]:
    program = f'engine="{engine}"\n' + _shim_block() + '\necho "shim_installed=$shim_installed"\n'
    return subprocess.run(
        ["sh", "-c", program],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        check=False,
    )


def test_installer_puts_ciao_on_path_as_a_shim_not_a_symlink(tmp_path: Path) -> None:
    """The engine lives inside Ciaobot.app, so without this a terminal has no
    `ciao` at all and every documented `ciao ...` command fails -- with
    "command not found", or with an unrelated `ciao`'s error message.

    A symlink would not do: the bundled launcher derives its runtime root from
    `dirname "$0"`, which resolves to the link's own directory.
    """
    engine = tmp_path / "Ciaobot.app" / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"
    engine.parent.mkdir(parents=True)
    engine.write_text('#!/bin/sh\necho "engine ran: $*"\n', encoding="utf-8")
    engine.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()

    result = _run_shim_block(home, engine)
    assert result.returncode == 0, result.stderr
    assert "shim_installed=1" in result.stdout

    shim = home / ".local" / "bin" / "ciao"
    assert shim.is_file() and not shim.is_symlink()
    assert os.access(shim, os.X_OK)
    forwarded = subprocess.run(
        [str(shim), "auth", "claude"], capture_output=True, text=True, check=False
    )
    assert forwarded.stdout.strip() == "engine ran: auth claude"


def test_installer_replaces_its_own_shim_but_not_a_foreign_ciao(tmp_path: Path) -> None:
    """`ciao` is a common enough name to collide (Ciao Prolog ships one), and
    clobbering someone else's binary is not the installer's call. Its own
    marker is what makes replacement on update safe."""
    engine = tmp_path / "Ciaobot.app" / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"
    engine.parent.mkdir(parents=True)
    engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    engine.chmod(0o755)
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    foreign = bin_dir / "ciao"
    foreign.write_text("#!/bin/sh\necho other project\n", encoding="utf-8")

    result = _run_shim_block(home, engine)
    assert result.returncode == 0, result.stderr
    assert "shim_installed=0" in result.stdout
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\necho other project\n"
    assert "leaving it alone" in result.stderr

    # A shim the installer wrote itself is replaced, so updates re-point it at
    # the new bundle instead of failing shut.
    foreign.unlink()
    assert "shim_installed=1" in _run_shim_block(home, engine).stdout
    assert "shim_installed=1" in _run_shim_block(home, engine).stdout
    assert str(engine) in (bin_dir / "ciao").read_text(encoding="utf-8")


def test_uninstall_parses_the_shim_the_installer_actually_writes(tmp_path) -> None:
    """Run install.sh's own shim-writing lines and parse the result.

    The marker assertion below pins one literal; this pins the whole format.
    `shim_exec_target` matches `exec "<path>" "$@"`, and that line lives in
    shell, so nothing but this test connects the writer to the reader — a
    change to either side would otherwise orphan every installed shim silently
    (uninstall stops recognising them and leaves a dead `ciao` on PATH).
    """
    import re
    import subprocess

    from ciao import desktop_install

    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    block = re.search(r'if \{\n(?:\s*printf .*\n)+\s*\} > "\$shim_tmp"', script)
    assert block is not None, "install.sh's shim-writing block moved; update this test"
    printfs = "\n".join(
        line.strip()
        for line in block.group(0).splitlines()
        if line.strip().startswith("printf")
    )

    engine = (
        tmp_path / "Ciaobot.app" / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"
    )
    written = subprocess.run(
        ["sh", "-s", desktop_install.SHIM_MARKER, str(engine)],
        input=f'shim_marker="$1"\nengine="$2"\n{printfs}\n',
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert desktop_install.SHIM_MARKER in written
    assert desktop_install.shim_exec_target(written) == engine


def test_uninstall_and_the_installer_agree_on_the_shim_marker() -> None:
    """The marker is how uninstall recognises a shim it may delete.

    `scripts/install.sh` writes it and `ciao/desktop_install.py` matches it,
    with nothing but this test tying the two literals together. If they drift,
    uninstall silently stops recognising every shim already on disk and leaves
    a dead `ciao` on PATH — the exact failure the marker exists to prevent.
    """
    from ciao import desktop_install

    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert desktop_install.SHIM_MARKER in script
