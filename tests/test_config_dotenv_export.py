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
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
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
