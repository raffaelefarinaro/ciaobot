"""Default-mode regression tests.

Pins the switch from ``bypass`` to ``auto`` as the new default across every
place where a fresh context / chat / schedule is created without an explicit
mode. Skipping this would silently re-introduce bypass-by-default and defeat
the Auto-mode safety net for any code path that relies on the dataclass
default (scheduled runs, new web chats, fresh SDK sessions, etc.).
"""

from __future__ import annotations

from pathlib import Path

from ciao.config import CiaoConfig, BridgeConfig
from ciao.models import ContextState
from ciao.schedules import ScheduleEntry
from ciao.sessions import StateStore
from ciao.web.project_chats import ChatInfo


def test_context_state_default_mode_is_auto() -> None:
    """Fresh ContextState inherits 'auto' so the first SDK call is gated."""
    assert ContextState().mode == "auto"


def test_chat_info_default_mode_is_auto() -> None:
    """New PWA chats default to auto-mode."""
    # Only project_id and chat_id are required.
    chat = ChatInfo(chat_id="c1", project_id="p1")
    assert chat.mode == "auto"


def test_schedule_default_mode_inherits() -> None:
    """A schedule pins no mode: "" resolves to the operator's per-provider pin
    at dispatch, the same setting a hand-opened chat on that provider obeys.

    It used to default to "auto", which is truthy and therefore an override,
    not a default — a routine on a provider pinned to `bypass` still stamped
    its chat `auto`, and the inheritance fallback behind it was dead code."""
    sched = ScheduleEntry(
        schedule_id="s1",
        daily_time_utc="09:00",
        prompt="Test",
        chat_id=0,
        created_at="2026-04-20T00:00:00Z",
    )
    assert sched.mode == ""


def test_bridge_config_default_claude_mode_is_auto() -> None:
    """CiaoConfig.from_env always selects auto; there is no env override."""
    config = BridgeConfig.from_env({"PWA_AUTH_TOKEN": "x"})
    assert config.claude_mode == "auto"


def test_state_store_default_mode_is_auto(tmp_path: Path) -> None:
    """The state store hands auto to new ContextState objects by default."""
    store = StateStore(tmp_path / "state.json", tmp_path, tmp_path / "media")
    assert store._default_mode == "auto"


def test_config_ignores_legacy_mode_env() -> None:
    """Legacy CLAUDE_EXECUTION_MODE / CLAUDE_PERMISSION_MODE env is ignored."""
    config = BridgeConfig.from_env({
        "PWA_AUTH_TOKEN": "x",
        "CLAUDE_EXECUTION_MODE": "bypassPermissions",
        "CLAUDE_PERMISSION_MODE": "bypassPermissions",
    })
    assert config.claude_mode == "auto"


def test_schedule_chat_is_stamped_with_the_providers_pinned_mode(tmp_path) -> None:
    """The end of the chain the empty default unblocks.

    A routine chat is created with the entry's mode, so a hardcoded "auto" on
    the entry was stamped onto the chat and the operator's Settings -> Providers
    pin never applied. The chat then disagreed with its own unattended run
    (which `_effective_mode_for_chat` forces to bypass), and on opencode a reply
    to that chat rotated the session — a session's permission rules are fixed
    when it is created.
    """
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager

    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    config.provider_default_modes = {"opencode": "bypass", "claude": "auto"}
    pcm = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("General", "personal")

    inherited = pcm.create_chat(
        project.project_id, title="routine", mode="", provider="opencode"
    )
    assert inherited.mode == "bypass"

    explicit = pcm.create_chat(
        project.project_id, title="pinned", mode="plan", provider="opencode"
    )
    assert explicit.mode == "plan", "an explicit mode still wins"
