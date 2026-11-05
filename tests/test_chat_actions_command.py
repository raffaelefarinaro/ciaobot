"""The `command` needs-you kind: its validator, its handler and the CLI routing.

A `command` card asks the user to run a suggested shell command. The engine
validates the text and records the user's progress. It never opens a terminal,
writes to a PTY or runs anything: the PWA types the text itself (E2).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import agent_cli
from ciao.chat_actions import (
    ChatActionError,
    _VALIDATORS,
    normalize_note,
    validate_params,
)
from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.chat_actions_base import ActionContext
from ciao.web.chat_actions_runtime import ACTION_HANDLERS, CommandActionHandler
from ciao.web.project_chats import ProjectChatManager


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


# ── The validator ──────────────────────────────────────────────────────────


def test_a_command_is_normalized() -> None:
    assert validate_params("command", {"command": "  ls -la\r\n"}) == {"command": "ls -la"}


def test_a_note_is_kept_normalized() -> None:
    params = validate_params("command", {"command": "ls", "note": "Lists files"})
    assert params == {"command": "ls", "note": normalize_note("Lists files")}


def test_an_unknown_param_is_refused() -> None:
    with pytest.raises(ChatActionError) as excinfo:
        validate_params("command", {"command": "ls", "chat_id": "x"})
    assert excinfo.value.reason == "unknown_param"


@pytest.mark.parametrize("raw", [{}, {"command": 12345}])
def test_a_missing_or_non_text_command_is_refused(raw: dict[str, Any]) -> None:
    with pytest.raises(ChatActionError) as excinfo:
        validate_params("command", raw)
    assert excinfo.value.reason == "invalid_type"
    assert "12345" not in str(excinfo.value)


def test_an_escape_can_never_reach_a_paste() -> None:
    """The guard at the seam: a command cannot close or open a bracketed paste."""
    for hostile in ("echo hi\x1b[201~rm -rf ~", "\x1b[200~ls"):
        with pytest.raises(ChatActionError) as excinfo:
            validate_params("command", {"command": hostile})
        assert excinfo.value.reason == "invalid_characters"


def test_the_byte_bound_holds() -> None:
    assert validate_params("command", {"command": "a" * 4096}) == {"command": "a" * 4096}
    with pytest.raises(ChatActionError) as excinfo:
        validate_params("command", {"command": "a" * 4097})
    assert excinfo.value.reason == "too_long"


def test_an_empty_command_is_refused() -> None:
    with pytest.raises(ChatActionError) as excinfo:
        validate_params("command", {"command": "   \n"})
    assert excinfo.value.reason == "empty"


def test_a_kind_without_a_validator_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(_VALIDATORS, "command")
    with pytest.raises(ChatActionError) as excinfo:
        validate_params("command", {"command": "ls"})
    assert excinfo.value.reason == "unsupported_kind"


# ── The handler ────────────────────────────────────────────────────────────


def _command_action(tmp_path: Path) -> tuple[ProjectChatManager, Any, str]:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Commands", workspace="work")
    chat = manager.create_chat(project.project_id, title="Run it", provider="claude")
    action = manager.raise_action(
        chat.chat_id, "command", {"command": "ls -la"}, origin="agent"
    )
    assert action is not None
    return manager, chat, str(action["id"])


def _context(manager: ProjectChatManager, chat_id: str) -> ActionContext:
    chat = manager.get_chat(chat_id)
    assert chat is not None
    return ActionContext(pcm=manager, chat=chat, provider=None, is_loopback=True)


def _state(manager: ProjectChatManager, chat_id: str, action_id: str) -> str:
    action = manager.get_action(chat_id, action_id)
    assert action is not None
    return action.state


@pytest.mark.asyncio
async def test_typed_moves_the_action_to_working(tmp_path: Path) -> None:
    manager, chat, action_id = _command_action(tmp_path)
    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    await ACTION_HANDLERS["command"].start(
        _context(manager, chat.chat_id), action, {"step": "typed"}
    )
    assert _state(manager, chat.chat_id, action_id) == "working"


@pytest.mark.asyncio
async def test_ran_moves_the_action_to_done(tmp_path: Path) -> None:
    handler = ACTION_HANDLERS["command"]

    manager, chat, action_id = _command_action(tmp_path / "from_open")
    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    await handler.start(_context(manager, chat.chat_id), action, {"step": "ran"})
    assert _state(manager, chat.chat_id, action_id) == "done"

    manager, chat, action_id = _command_action(tmp_path / "from_working")
    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    await handler.start(_context(manager, chat.chat_id), action, {"step": "typed"})
    assert _state(manager, chat.chat_id, action_id) == "working"
    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    await handler.start(_context(manager, chat.chat_id), action, {"step": "ran"})
    assert _state(manager, chat.chat_id, action_id) == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"step": "run"}, {"step": "typed", "command": "x"}])
async def test_a_bad_body_is_refused(tmp_path: Path, body: dict[str, object]) -> None:
    manager, chat, action_id = _command_action(tmp_path)
    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    with pytest.raises(ChatActionError) as excinfo:
        await ACTION_HANDLERS["command"].start(
            _context(manager, chat.chat_id), action, body
        )
    assert excinfo.value.reason == "invalid_body"
    assert _state(manager, chat.chat_id, action_id) == "open"


@pytest.mark.asyncio
async def test_a_finished_action_refuses_another_step(tmp_path: Path) -> None:
    manager, chat, action_id = _command_action(tmp_path)
    runtime = manager.actions_runtime
    await runtime.start(chat.chat_id, action_id, {"step": "ran"}, is_loopback=True)
    with pytest.raises(ChatActionError) as excinfo:
        await runtime.start(chat.chat_id, action_id, {"step": "typed"}, is_loopback=True)
    assert excinfo.value.reason == "invalid_state"


@pytest.mark.asyncio
async def test_a_typed_command_is_never_polled(tmp_path: Path) -> None:
    manager, chat, action_id = _command_action(tmp_path)
    await manager.actions_runtime.start(
        chat.chat_id, action_id, {"step": "typed"}, is_loopback=True
    )
    assert action_id not in manager.actions_runtime._polls


@pytest.mark.asyncio
async def test_the_handler_never_touches_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ciao.host_terminal
    import ciao.terminal_sessions

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the command handler reached a terminal or a subprocess")

    async def refuse_async(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the command handler reached a terminal or a subprocess")

    monkeypatch.setattr(ciao.terminal_sessions.TerminalSessionManager, "create", refuse)
    monkeypatch.setattr(ciao.terminal_sessions.TerminalSessionManager, "write_input", refuse)
    monkeypatch.setattr(ciao.host_terminal, "launch", refuse, raising=False)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr("asyncio.create_subprocess_exec", refuse_async)

    manager, chat, action_id = _command_action(tmp_path)
    await manager.actions_runtime.start(
        chat.chat_id, action_id, {"step": "typed"}, is_loopback=True
    )
    assert _state(manager, chat.chat_id, action_id) == "working"
    await manager.actions_runtime.start(
        chat.chat_id, action_id, {"step": "ran"}, is_loopback=True
    )
    assert _state(manager, chat.chat_id, action_id) == "done"


def test_the_wake_prompt_says_the_exit_is_unknown() -> None:
    action: Any = SimpleNamespace(params={"command": "ls -la"})
    prompt = CommandActionHandler().wake_prompt(action)
    assert "exit status unknown" in prompt
    assert "ls -la" not in prompt


# ── The CLI routing ────────────────────────────────────────────────────────


def _resolve(argv: list[str]) -> tuple[str, dict[str, Any]] | None:
    return agent_cli.resolve(agent_cli.build_parser().parse_args(argv))


def test_the_cli_routes_only_the_kinds_flags(tmp_path: Path) -> None:
    command_file = tmp_path / "command.txt"
    command_file.write_text("ls -la\n", encoding="utf-8")

    assert _resolve(["chat", "needs-action", "gws_auth", "--profile", "work"]) == (
        "chat_needs_action",
        {"kind": "gws_auth", "params": {"profile": "work"}},
    )
    with pytest.raises(agent_cli.UsageError, match="--profile does not apply to needs-action command."):
        _resolve(["chat", "needs-action", "command", "--command-file", str(command_file), "--profile", "x"])
    with pytest.raises(agent_cli.UsageError, match="needs-action command requires --command-file."):
        _resolve(["chat", "needs-action", "command"])
    with pytest.raises(SystemExit) as excinfo:
        agent_cli.build_parser().parse_args(["chat", "needs-action", "limit"])
    assert excinfo.value.code == 2
