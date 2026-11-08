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
    COMMAND_MAX_BYTES,
    ChatActionError,
    _VALIDATORS,
    normalize_command,
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


# ── The text contract ─────────────────────────────────────────────────────


def test_a_command_of_exactly_the_byte_bound_is_accepted() -> None:
    """4096 bytes is inside the bound; the limit is not off by one."""
    command = "a" * COMMAND_MAX_BYTES

    assert normalize_command(command) == command


def test_one_byte_over_the_bound_is_refused() -> None:
    with pytest.raises(ChatActionError):
        normalize_command("a" * (COMMAND_MAX_BYTES + 1))


def test_the_bound_counts_bytes_not_characters() -> None:
    """A multibyte command is measured as it is stored, not as it is typed.

    1365 three-byte characters are 4095 bytes and fit; 1366 are 4098 and do
    not, even though both are well under 4096 characters.
    """
    assert normalize_command("€" * 1365) == "€" * 1365

    with pytest.raises(ChatActionError):
        normalize_command("€" * 1366)


def test_the_raw_bound_applies_before_the_text_is_trimmed() -> None:
    """A command that arrives huge is refused even when it trims to nothing.

    The bound is on what arrived, not on what would be kept: 4096 spaces
    around "ls" is 4098 raw bytes, and the text that would be stored is two
    characters. One space fewer is exactly the bound, and it is accepted.
    """
    with pytest.raises(ChatActionError) as excinfo:
        normalize_command(" " * 4096 + "ls")
    assert excinfo.value.reason == "too_long"

    assert normalize_command(" " * 4094 + "ls") == "ls"

    # The same in bytes rather than characters: 1365 three-byte characters
    # are 4095 bytes and fit on their own, so two spaces around them are
    # already over the raw bound even though the text that would be kept
    # still fits.
    with pytest.raises(ChatActionError) as excinfo:
        normalize_command("  " + "€" * 1365)
    assert excinfo.value.reason == "too_long"


@pytest.mark.parametrize(
    "value",
    [None, 7, 3.5, True, b"ls -la", ["ls"], {"command": "ls"}, object()],
    ids=[
        "none",
        "int",
        "float",
        "bool",
        "bytes",
        "list",
        "dict",
        "object",
    ],
)
def test_a_value_that_is_not_text_is_refused(value: object) -> None:
    with pytest.raises(ChatActionError):
        normalize_command(value)


def test_a_lone_surrogate_is_refused() -> None:
    """A surrogate is not text: it has no UTF-8 encoding at all.

    The card copies the command into a shell, so a value that cannot be
    encoded has to be refused rather than mangled.
    """
    with pytest.raises(ChatActionError):
        normalize_command("ls -la \ud800")


@pytest.mark.parametrize(
    "character",
    [
        "\x00",  # NUL
        "\x07",  # BEL
        "\x0b",  # vertical tab
        "\x1b",  # ESC
        "\x7f",  # DEL
        "\x85",  # C1 NEL
        "\x9b",  # C1 CSI
        "\u202a",  # RLO
        "\u202e",  # PDF
        "\u2066",  # LRI
        "\u2069",  # PDI
        "\u200b",  # zero-width space
        "\u2028",  # line separator
        "\u2029",  # paragraph separator
    ],
    ids=[
        "nul",
        "bel",
        "vt",
        "esc",
        "del",
        "c1-nel",
        "c1-csi",
        "bidi-rlo",
        "bidi-pdf",
        "bidi-lri",
        "bidi-pdi",
        "zero-width-space",
        "line-separator",
        "paragraph-separator",
    ],
)
def test_a_control_or_invisible_character_is_refused(character: str) -> None:
    """Invisible characters are refused, not stripped.

    A right-to-left override or a zero-width space inside a command is
    invisible on the card and changes what the user copies into their shell,
    so it never reaches the state at all.
    """
    with pytest.raises(ChatActionError):
        normalize_command(f"ls{character}-la")


def test_line_feeds_and_tabs_are_allowed() -> None:
    """A multi-line command is ordinary: a shell script is several lines."""
    assert normalize_command("cd /tmp\n\tls -la") == "cd /tmp\n\tls -la"


def test_carriage_returns_become_line_feeds() -> None:
    """CRLF and a bare CR are line endings, not control characters."""
    assert normalize_command("ls\r\n-la\r") == "ls\n-la"


def test_outer_whitespace_is_stripped() -> None:
    assert normalize_command("  ls -la  ") == "ls -la"
    assert normalize_command("\n ls -la \n") == "ls -la"


@pytest.mark.parametrize("value", ["", "   ", "\n", "\t\n "], ids=["empty", "spaces", "newline", "mixed"])
def test_a_command_with_nothing_left_in_it_is_refused(value: str) -> None:
    with pytest.raises(ChatActionError):
        normalize_command(value)


#: A credential-shaped value used to prove that a refusal never carries the
#: text it refused. It is not a real credential.
_SENTINEL = "sk-live-0123456789abcdef"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        pytest.param(None, "invalid_type", id="not-text"),
        pytest.param(
            f"{_SENTINEL}\ud800", "invalid_encoding", id="lone-surrogate"
        ),
        pytest.param(_SENTINEL * 200, "too_long", id="too-long-ascii"),
        pytest.param("€" * 1366 + _SENTINEL, "too_long", id="too-long-multibyte"),
        pytest.param(" " * 4096 + _SENTINEL, "too_long", id="too-long-raw-only"),
        pytest.param(" \t\n ", "empty", id="whitespace-only"),
        pytest.param(f"{_SENTINEL}\x00", "invalid_characters", id="nul"),
        pytest.param(f"{_SENTINEL}\u202e", "invalid_characters", id="bidi-override"),
    ],
)
def test_a_refusal_names_its_exact_static_reason(
    value: object, reason: str
) -> None:
    """Every refusal has one closed reason, and never the text.

    The reason is a fixed word, so a caller can map it to a status without
    parsing prose. The rejected text is model-authored and can name a
    credential, and the refusal ends up in a log line and a telemetry
    field, so the text is nowhere in it.
    """
    with pytest.raises(ChatActionError) as excinfo:
        normalize_command(value)

    assert excinfo.value.reason == reason
    assert str(excinfo.value) == reason
    assert _SENTINEL not in str(excinfo.value)
    assert _SENTINEL not in excinfo.value.reason
    # The error carries the reason and nothing else: one argument, and no
    # cause. A refused command is model-authored, so the error must not
    # carry it along in a traceback either.
    assert excinfo.value.args == (reason,)
    assert excinfo.value.__cause__ is None
    assert (
        excinfo.value.__context__ is None
        or excinfo.value.__suppress_context__ is True
    )
    assert _SENTINEL not in repr(excinfo.value)




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
