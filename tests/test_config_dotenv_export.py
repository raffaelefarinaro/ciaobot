"""How ``CiaoConfig.from_env`` applies a workspace ``.env`` to the process.

The export is deliberate: `.mcp.json` stores credentials as ``${NAME}``
placeholders that the provider CLI resolves from the environment it inherits,
so a Notion or n8n server gets its token only because the workspace ``.env``
reached ``os.environ``. What it must not do is change a caller's environment
without a way back.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import config as ciao_config
from ciao.config import CiaoConfig, reset_exported_dotenv


def _workspace_with_env(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(body, encoding="utf-8")
    return root


def test_export_records_only_the_keys_it_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The export happens, and is undone exactly."""
    workspace = _workspace_with_env(
        tmp_path / "ws",
        "N8N_MCP_TOKEN=from-dotenv\nCIAO_RUNTIME_ROOT=.runtime\n",
    )
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.delenv("N8N_MCP_TOKEN", raising=False)
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)

    CiaoConfig.from_env()

    # The provider subprocess inherits this; that is the whole point.
    assert os.environ["N8N_MCP_TOKEN"] == "from-dotenv"

    reset_exported_dotenv()

    assert "N8N_MCP_TOKEN" not in os.environ
    assert "CIAO_RUNTIME_ROOT" not in os.environ


def test_reset_never_clears_a_value_the_process_already_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key the operator exported themselves is not ours to remove.

    `load_dotenv` does not override an existing value, so the pre-existing one
    is still live; inferring "we set it" from its presence afterwards would
    delete the operator's own variable on reset.
    """
    workspace = _workspace_with_env(
        tmp_path / "ws", "N8N_MCP_TOKEN=from-dotenv\n"
    )
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("N8N_MCP_TOKEN", "from-the-shell")

    CiaoConfig.from_env()
    assert os.environ["N8N_MCP_TOKEN"] == "from-the-shell"

    reset_exported_dotenv()

    assert os.environ["N8N_MCP_TOKEN"] == "from-the-shell"


def test_export_false_reads_the_same_values_without_touching_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only caller must not change its caller's environment."""
    workspace = _workspace_with_env(
        tmp_path / "ws",
        "PWA_AUTH_TOKEN=ws-secret\nN8N_MCP_TOKEN=from-dotenv\n",
    )
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.delenv("N8N_MCP_TOKEN", raising=False)
    monkeypatch.delenv("PWA_AUTH_TOKEN", raising=False)
    before = dict(os.environ)

    config = CiaoConfig.from_env(export=False)

    # The value was still read...
    assert config.pwa_auth_token == "ws-secret"
    # ...but nothing about the process changed.
    assert dict(os.environ) == before
    assert not ciao_config._EXPORTED_DOTENV_KEYS


def test_export_false_keeps_the_process_environment_winning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precedence matches load_dotenv: the process env beats the file."""
    workspace = _workspace_with_env(
        tmp_path / "ws", "PWA_AUTH_TOKEN=from-dotenv\n"
    )
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("PWA_AUTH_TOKEN", "from-the-shell")

    config = CiaoConfig.from_env(export=False)

    assert config.pwa_auth_token == "from-the-shell"


def test_a_leaked_relative_runtime_root_cannot_survive_a_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The concrete harm the autouse fixture exists to stop.

    `.env` carries `CIAO_RUNTIME_ROOT=.runtime`, which is relative. Leaked into
    a later caller whose `CIAO_WORKSPACE` is unset, it resolves against the cwd
    — which is how a CLI run wrote its outcome log into the repository checkout
    instead of its own workspace.
    """
    workspace = _workspace_with_env(
        tmp_path / "ws", "CIAO_RUNTIME_ROOT=.runtime\n"
    )
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)

    CiaoConfig.from_env()
    assert os.environ.get("CIAO_RUNTIME_ROOT") == ".runtime"

    # The autouse fixture runs this at teardown for every test.
    reset_exported_dotenv()
    assert "CIAO_RUNTIME_ROOT" not in os.environ


def test_a_whitespace_only_workspace_does_not_resolve_to_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`export CIAO_WORKSPACE=" "` must not silently mean "here".

    Three decisions keyed off the raw value and only some stripped it, so a
    stray space in a shell profile was truthy enough to skip LaunchAgent
    discovery and to clear `bootstrap_mode`, and then `Path("  ").resolve()`
    became the current directory — the CLI created `.runtime` and minted a
    session secret under wherever the operator was standing.
    """
    from ciao.config import reset_reroot_cache

    stood_here = tmp_path / "some-unrelated-dir"
    stood_here.mkdir()
    monkeypatch.chdir(stood_here)
    for name in ("CIAO_RUNTIME_ROOT", "CIAO_VAULT_ROOT", "CIAO_BOOTSTRAP_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CIAO_WORKSPACE", "   ")
    (tmp_path / "home").mkdir()

    reset_reroot_cache()
    try:
        config = CiaoConfig.from_env()
    finally:
        reset_reroot_cache()

    # Without the strip this resolved to `<cwd>/"   "` — a directory literally
    # NAMED three spaces, sitting in whatever directory the operator was in.
    assert stood_here.resolve() not in config.workspace_root.parents
    assert config.workspace_root.name.strip(), config.workspace_root
    # An unusable value means "no workspace configured", which is bootstrap.
    assert config.workspace_root == (tmp_path / "home" / ".ciao" / "bootstrap").resolve()
    assert not any(p.name.strip() == "" for p in stood_here.iterdir())


def test_a_blank_runtime_root_falls_back_to_the_workspace_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank ``CIAO_RUNTIME_ROOT=`` means unset, not the workspace root.

    ``installed_workspace_env`` overlays the install ``.env`` and keeps its empty
    values, so a hand-written blank reached ``from_env`` as an empty string and
    ``Path("")`` resolved to the workspace root itself — putting ``state.json``
    and ``workspaces.json`` beside the notes instead of under ``<ws>/.runtime``.
    The ``vault_root`` line just above already treats blank as unset; this makes
    the runtime root agree.
    """
    workspace = _workspace_with_env(tmp_path / "ws", "CIAO_RUNTIME_ROOT=\n")
    (workspace / "memory-vault").mkdir()
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)

    config = CiaoConfig.from_env(export=False)

    assert config.state_path.parent == (workspace / ".runtime").resolve()
    assert config.state_path.parent != config.workspace_root


def test_installed_workspace_env_pins_the_workspace_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty caller workspace is still pinned to the installed root.

    Discovery is entered for an exported empty ``CIAO_WORKSPACE=`` as well as an
    unset one, and the caller's environment wins the merge — so the pin has to be
    the final word rather than the overlay's. A caller that already names a
    workspace is still returned untouched, which
    ``test_installed_workspace_env_leaves_a_named_workspace_alone`` covers.
    """
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(
            workspace=str(install), runtime_root=str(install / ".runtime")
        ),
    )

    for base in ({"FOO": "1"}, {"FOO": "1", "CIAO_WORKSPACE": ""}):
        merged = ciao_config.installed_workspace_env(base)
        assert merged["CIAO_WORKSPACE"] == str(install)
        assert merged["FOO"] == "1"

    named = {"CIAO_WORKSPACE": "/x", "FOO": "1"}
    assert ciao_config.installed_workspace_env(named) == named


def test_installed_workspace_env_leaves_a_named_workspace_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that names a workspace has already answered the question.

    Discovery exists for the bare shell, which has no answer to give. A caller's
    own ``CIAO_WORKSPACE`` outranks whatever the LaunchAgent points at, so the
    helper must not even ask — the explicit-env callers that pass one are the
    ones this refactor exists to keep exact.
    """

    def _must_not_be_called(**_: object) -> None:
        raise AssertionError("discovery must not run for a named workspace")

    monkeypatch.setattr("ciao.macos_service.discover_runtime", _must_not_be_called)
    base = {"CIAO_WORKSPACE": "/x", "FOO": "1"}

    assert ciao_config.installed_workspace_env(base) == base


def test_installed_workspace_env_overlays_the_installed_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare shell reads the install's ``.env``, and the process still wins.

    The settings an install keeps only in its workspace ``.env`` — a custom
    vault root, a custom runtime root — used to reach ``from_env`` from a bare
    shell and not the explicit-env callers, because the inline copies of this
    discovery skipped the overlay.
    """
    install = tmp_path / "install"
    install.mkdir()
    (install / ".env").write_text(
        "CIAO_VAULT_ROOT=memory-vault\nFOO=from-file\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(
            workspace=str(install), runtime_root=str(install / ".runtime")
        ),
    )
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)

    merged = ciao_config.installed_workspace_env({"FOO": "from-shell"})

    assert merged["CIAO_WORKSPACE"] == str(install)
    assert merged["CIAO_VAULT_ROOT"] == "memory-vault"
    assert merged["FOO"] == "from-shell", "the process environment wins, as in load_dotenv"
    # Read-only: the value lives in the returned mapping, never in the process.
    assert os.environ.get("CIAO_VAULT_ROOT") is None


def test_installed_workspace_env_without_an_install_returns_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No installed server to ask: the caller's environment is all there is."""
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(workspace="", runtime_root=""),
    )
    base = {"FOO": "1"}

    assert ciao_config.installed_workspace_env(base) == base


def test_installed_workspace_env_ignores_a_stale_launch_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plist naming a deleted directory is stale, and must not be pinned.

    The operator removed `~/Ciaobot` but never unloaded the agent. Pinning the
    path it names is how the directory came back: `from_env` takes the
    non-bootstrap branch (a workspace is set, no token), finds no token and calls
    `_read_or_create_secret(<deleted>/.runtime/session-secret)`, whose `mkdir`
    resurrects what was deleted. `scripts/install-engine.sh` likewise refuses to
    trust a plist workspace that is not an existing directory. A live install
    without a `.env` is still trusted — the LaunchAgent server's own `from_env`
    treats it as a workspace, so pinning matches what the server sees.
    """
    gone = tmp_path / "gone"
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(
            workspace=str(gone), runtime_root=str(gone / ".runtime")
        ),
    )
    base = {"FOO": "1"}

    assert ciao_config.installed_workspace_env(base) == base
    assert not gone.exists(), "a read-only lookup must not recreate the workspace"


def test_windows_bare_shell_discovers_the_task_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows task definition is the install a bare shell must adopt.

    Before the platform seam `installed_workspace_env` asked only the macOS
    LaunchAgent, so on Windows a bare-shell read-only command found nothing,
    fell back to the cwd and could mint a bootstrap secret beside it. The
    engine already records its workspace as the task's ``WorkingDirectory``, so
    a bare shell reads that definition and overlays its ``.env`` — and mints
    nothing while doing it.
    """
    from ciao import install_discovery, windows_service

    install = tmp_path / "install"
    install.mkdir()
    (install / ".env").write_text("PWA_AUTH_TOKEN=ws-secret\n", encoding="utf-8")
    local = tmp_path / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    # The seam reads the LIVE task dir, so `CIAO_LAUNCH_AGENTS_DIR` is cleared:
    # discovery reads what the engine wrote, not a test override.
    monkeypatch.delenv("CIAO_LAUNCH_AGENTS_DIR", raising=False)
    definition = local / "Ciaobot" / "service" / windows_service.TASK_FILE_NAME
    definition.parent.mkdir(parents=True, exist_ok=True)
    # A definition shaped like the renderer's, written by hand: `render_task_xml`
    # refuses a POSIX workspace path (its `PureWindowsPath` check), and this test
    # runs on every OS by forcing the branch it exercises.
    definition.write_bytes(
        (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            f'<Task version="1.2" xmlns="{windows_service.TASK_NS[1:-1]}">\n'
            "  <Actions Context=\"Author\">\n    <Exec>\n"
            f"      <WorkingDirectory>{install}</WorkingDirectory>\n"
            "    </Exec>\n  </Actions>\n</Task>\n"
        ).encode("utf-16")
    )
    monkeypatch.setattr(install_discovery, "_platform", lambda: "win32")
    monkeypatch.delenv("PWA_AUTH_TOKEN", raising=False)

    merged = ciao_config.installed_workspace_env({"FOO": "1"})

    assert merged["CIAO_WORKSPACE"] == str(install)
    assert merged["FOO"] == "1"
    assert merged["PWA_AUTH_TOKEN"] == "ws-secret", "the install's .env is overlaid"
    # Read-only: the value lives in the returned mapping, never in the process.
    assert os.environ.get("PWA_AUTH_TOKEN") is None
    # And nothing was manufactured beside the install.
    assert not (tmp_path / "bootstrap").exists()


def test_windows_bare_shell_without_a_task_mints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No task definition means no install to discover, which is an honest None.

    A missing file is not an error and must not become a fallback: pinning a
    workspace nobody recorded is how `_read_or_create_secret` mkdirs a bootstrap
    runtime root beside the shell. Discovery falls through to the preserved
    macOS read, which on a real Windows machine finds no plist either, so the
    mapping is returned unchanged.
    """
    from ciao import install_discovery

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("CIAO_LAUNCH_AGENTS_DIR", raising=False)
    monkeypatch.setattr(install_discovery, "_platform", lambda: "win32")
    base = {"FOO": "1"}

    assert ciao_config.installed_workspace_env(base) == base
    assert not (tmp_path / "Local").exists(), "a read-only lookup creates nothing"


def test_installed_workspace_env_still_discovers_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam keeps the macOS branch, and the patch target the suite relies on.

    The existing tests patch ``ciao.macos_service.discover_runtime``; the seam
    must keep calling it, and force the macOS branch exactly as the real
    platform would, or the refactor would have silently moved discovery behind
    a platform check the suite never satisfied.
    """
    from ciao import install_discovery

    install = tmp_path / "install"
    install.mkdir()
    (install / ".env").write_text("FOO=from-file\n", encoding="utf-8")
    monkeypatch.setattr(install_discovery, "_platform", lambda: "darwin")
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(
            workspace=str(install), runtime_root=str(install / ".runtime")
        ),
    )

    merged = ciao_config.installed_workspace_env({"FOO": "from-shell"})

    assert merged["CIAO_WORKSPACE"] == str(install)
    assert merged["FOO"] == "from-shell", "the process environment wins, as before"
