from __future__ import annotations

from pathlib import Path

from ciao.models import ChatContext
from ciao.sessions import StateStore

CTX = ChatContext(chat_id=1)
CTX2 = ChatContext(chat_id=2)


def test_state_store_updates_model_and_sessions(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json", tmp_path, tmp_path / ".runtime")
    store.set_active_model("opus", CTX)
    store.set_mode("auto", CTX)
    store.update_session("sess-1", CTX)

    reloaded = StateStore(tmp_path / "state.json", tmp_path, tmp_path / ".runtime")
    ctx_state = reloaded.get_context(CTX)
    assert ctx_state.active_model == "opus"
    assert reloaded.get_mode(CTX) == "auto"
    assert reloaded.get_session_id(CTX) == "sess-1"


def test_state_store_reload_refreshes_in_memory_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json", tmp_path, tmp_path / ".runtime")
    store.set_active_model("sonnet", CTX)

    external = StateStore(tmp_path / "state.json", tmp_path, tmp_path / ".runtime")
    external.set_active_model("opus", CTX)
    external.set_mode("auto", CTX)

    assert store.get_context(CTX).active_model == "sonnet"
    store.reload()
    assert store.get_context(CTX).active_model == "opus"
    assert store.get_selected_model(CTX) == "opus"
    assert store.get_mode(CTX) == "auto"


def test_context_isolation(tmp_path: Path) -> None:
    """Two different contexts don't share session/model state."""
    store = StateStore(tmp_path / "state.json", tmp_path, tmp_path / ".runtime")
    store.set_active_model("haiku", CTX)
    store.update_session("sess-1", CTX)

    store.set_active_model("opus", CTX2)
    store.update_session("sess-2", CTX2)

    assert store.get_context(CTX).active_model == "haiku"
    assert store.get_context(CTX2).active_model == "opus"
    assert store.get_session_id(CTX) == "sess-1"
    assert store.get_session_id(CTX2) == "sess-2"


def test_v1_migration(tmp_path: Path) -> None:
    """V1 state.json loads correctly and produces a 'default' context."""
    import json

    v1_data = {
        "bot_state": {
            "active_provider": "claude",
            "active_model": "opus",
            "last_effective_model": "opus",
            "workspace_root": str(tmp_path),
            "media_root": str(tmp_path / ".runtime/telegram_media"),
            "selected_models": {"claude": "opus"},
            "provider_modes": {"claude": "bypass"},
            "provider_usage": {"claude": {}},
            "provider_quota": {"claude": {}},
            "provider_costs": {"claude": 1.5},
        },
        "session_state": {
            "provider_sessions": {
                "claude": {"session_id": "old-sess", "message_count": 5},
            }
        },
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(v1_data))

    store = StateStore(state_path, tmp_path, tmp_path / ".runtime")

    assert store.bot_state.cost == 1.5

    ctx_state = store.get_context(CTX)
    assert ctx_state.active_model == "opus"
    assert ctx_state.session.session_id == "old-sess"
    assert ctx_state.session.message_count == 5

    assert "default" not in store._contexts
    assert CTX.key in store._contexts


def test_v2_migration(tmp_path: Path) -> None:
    """V2 state.json (per-provider dicts) loads correctly as v3."""
    import json

    v2_data = {
        "version": 2,
        "bot_state": {
            "workspace_root": str(tmp_path),
            "media_root": str(tmp_path / ".runtime/telegram_media"),
            "provider_usage": {"claude": {"input_tokens": "100"}},
            "provider_quota": {"claude": {"status": "ok"}},
            "provider_costs": {"claude": 2.5},
        },
        "contexts": {
            "1": {
                "active_provider": "claude",
                "active_model": "sonnet",
                "last_effective_model": "sonnet",
                "selected_models": {"claude": "opus"},
                "provider_modes": {"claude": "auto"},
                "sessions": {
                    "claude": {"session_id": "sess-v2", "message_count": 3},
                },
                "context_mode": "personal",
            }
        },
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(v2_data))

    store = StateStore(state_path, tmp_path, tmp_path / ".runtime")

    assert store.bot_state.cost == 2.5
    assert store.bot_state.usage == {"input_tokens": "100"}
    assert store.bot_state.quota == {"status": "ok"}

    ctx_state = store.get_context(CTX)
    assert ctx_state.active_model == "opus"
    assert ctx_state.mode == "auto"
    assert ctx_state.session.session_id == "sess-v2"
    assert ctx_state.context_mode == "personal"
