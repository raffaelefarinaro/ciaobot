"""The shipped ACP agent specs (#3139): Codex's mode table and its login command."""

from __future__ import annotations

from typing import get_args

import pytest

from ciao.models import BridgeMode
from ciao.providers.acp import agents
from ciao.providers.acp.agents import CODEX, CodexAcpProvider


def test_codex_mode_table_covers_every_bridge_mode() -> None:
    assert set(CODEX.modes) == set(get_args(BridgeMode))
    assert all(choices for choices in CODEX.modes.values())


def test_codex_spec_names_its_adapter_and_config_ids() -> None:
    assert CODEX.id == "codex"
    assert CODEX.binary == "codex-acp"
    assert CODEX.model_config_id == "model"
    assert CODEX.thought_config_id == "reasoning_effort"
    assert CodexAcpProvider.spec is CODEX


def test_codex_auth_command_builds_login_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents, "codex_binary", lambda: "/opt/tools/codex")
    assert agents.codex_auth_command() == ["/opt/tools/codex", "login"]
    assert agents.codex_auth_command(device_auth=True) == [
        "/opt/tools/codex",
        "login",
        "--device-auth",
    ]


def test_codex_auth_command_raises_when_codex_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents, "codex_binary", lambda: None)
    with pytest.raises(FileNotFoundError):
        agents.codex_auth_command()
