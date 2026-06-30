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


def test_schedule_default_mode_is_auto() -> None:
    """Scheduled runs also go through the classifier."""
    sched = ScheduleEntry(
        schedule_id="s1",
        daily_time_utc="09:00",
        prompt="Test",
        chat_id=0,
        created_at="2026-04-20T00:00:00Z",
    )
    assert sched.mode == "auto"


def test_bridge_config_default_claude_mode_is_auto() -> None:
    """When no env override is set, CiaoConfig.from_env selects auto."""
    config = BridgeConfig.from_env({"PWA_AUTH_TOKEN": "x"})
    assert config.claude_mode == "auto"


def test_state_store_default_mode_is_auto(tmp_path: Path) -> None:
    """The state store hands auto to new ContextState objects by default."""
    store = StateStore(tmp_path / "state.json", tmp_path, tmp_path / "media")
    assert store._default_mode == "auto"


def test_config_env_override_still_wins() -> None:
    """Explicit CLAUDE_PERMISSION_MODE env still takes precedence."""
    config = BridgeConfig.from_env({
        "PWA_AUTH_TOKEN": "x",
        "CLAUDE_PERMISSION_MODE": "bypassPermissions",
    })
    assert config.claude_mode == "bypass"
