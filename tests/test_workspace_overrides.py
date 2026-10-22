"""Per-workspace, per-provider overrides of the Providers defaults (#1903).

Order per field: explicit > workspace override for the effective provider >
engine default (Settings -> Providers) > catalog default.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path

import pytest

from ciao import main as ciao_main
from ciao.config import (
    CiaoConfig,
    WorkspaceConfig,
    coerce_provider_overrides,
)
from ciao.control_plane import CiaoControlPlane, McpPrincipal
from ciao.insights import HELPER_MODEL_CLAUDE, resolve_helper_model, resolve_insights_model
from ciao.schedules import ScheduleEntry
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import memory_pass
from ciao.web.project_chats import ProjectChatManager
from ciao.workspace_rename import preview_renamed_config


def _config(tmp_path: Path, overrides: dict | None = None, **kwargs) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "work": WorkspaceConfig(
                name="work",
                vault_root="memory-vault/work",
                provider_overrides=overrides or {},
            ),
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            ),
        },
        **kwargs,
    )


def _manager(tmp_path: Path, config: CiaoConfig) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


# -- coerce_provider_overrides ------------------------------------------------


def test_coerce_provider_overrides_normalizes_and_drops_empties() -> None:
    assert coerce_provider_overrides(None) == {}
    cleaned = coerce_provider_overrides(
        {
            "opencode": {"model": " x/y ", "thinking": ""},
            "claude": {
                "insights_model": "haiku",
                "permission_mode": " manual ",
                "thinking": "high",
                "model": "  ",
            },
        }
    )
    assert cleaned == {
        "claude": {
            "thinking": "high",
            "permission_mode": "manual",
            "insights_model": "haiku",
        },
        "opencode": {"model": "x/y"},
    }
    # Provider order and field order are stable.
    assert list(cleaned) == ["claude", "opencode"]
    assert list(cleaned["claude"]) == ["thinking", "permission_mode", "insights_model"]
    assert coerce_provider_overrides({"claude": {"model": ""}, "opencode": {}}) == {}


@pytest.mark.parametrize(
    "raw, message",
    [
        ([], "provider_overrides must be an object"),
        ({"nope": {}}, "unknown provider 'nope'"),
        ({"claude": []}, "provider_overrides.claude must be an object"),
        ({"claude": {"default_model": "x"}}, "unknown key 'default_model'"),
        ({"claude": {"model": 3}}, "provider_overrides.claude.model must be a string"),
        ({"claude": {"permission_mode": "plan"}}, "must be one of: manual, auto, bypass"),
        ({"claude": {"permission_mode": "normal"}}, "must be one of: manual, auto, bypass"),
        ({"claude": {"thinking": "extreme"}}, "provider_overrides.claude.thinking must be one of"),
        ({"opencode": {"thinking": "xhigh"}}, "provider_overrides.opencode.thinking must be one of"),
    ],
)
def test_coerce_provider_overrides_rejects_unknown_provider_key_and_value(
    raw: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        coerce_provider_overrides(raw)


# -- resolution order ---------------------------------------------------------


def test_model_resolution_order(tmp_path: Path) -> None:
    config = _config(
        tmp_path, {"claude": {"model": "opus"}}, provider_default_models={"claude": "sonnet"}
    )
    manager = _manager(tmp_path, config)
    work = manager.create_project("W", workspace="work")
    personal = manager.create_project("P", workspace="personal")

    assert manager.create_chat(work.project_id).model == "opus"
    # Another workspace keeps the engine default.
    assert manager.create_chat(personal.project_id).model == "sonnet"
    # Explicit wins.
    assert manager.create_chat(work.project_id, model="haiku").model == "haiku"

    config.workspaces["work"].provider_overrides = {}
    assert manager.create_chat(work.project_id).model == "sonnet"

    config.provider_default_models = {}
    assert manager.create_chat(work.project_id).model == config.claude_default_model


def test_override_is_keyed_by_the_effective_provider(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {"opencode": {"model": "x/y"}},
        provider_default_models={"claude": "sonnet"},
    )
    manager = _manager(tmp_path, config)
    work = manager.create_project("W", workspace="work")

    assert manager.create_chat(work.project_id).model == "sonnet"
    assert manager.create_chat(work.project_id, provider="opencode").model == "x/y"

    config.workspaces["work"].provider_overrides = {"claude": {"model": "opus"}}
    config.provider_default_models = {"opencode": "engine/model"}
    assert manager.create_chat(work.project_id, provider="opencode").model == "engine/model"
    assert manager.create_chat(work.project_id).model == "opus"


def test_thinking_resolution_order(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {"claude": {"thinking": "high"}},
        provider_default_thinking={"claude": "low"},
    )
    manager = _manager(tmp_path, config)
    work = manager.create_project("W", workspace="work")
    personal = manager.create_project("P", workspace="personal")

    chat = manager.create_chat(work.project_id)
    assert chat.thinking_level == "high"
    assert manager.create_chat(personal.project_id).thinking_level == "low"

    # A chat's own level (set explicitly later) wins and sticks; a fork or a
    # continued chat copies its source's level and never re-resolves.
    manager.update_chat(chat.chat_id, thinking_level="medium")
    assert manager.get_chat(chat.chat_id).thinking_level == "medium"

    config.workspaces["work"].provider_overrides = {}
    assert manager.create_chat(work.project_id).thinking_level == "low"
    config.provider_default_thinking = {}
    assert manager.create_chat(work.project_id).thinking_level == ""


def test_permission_mode_resolution_order(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {"claude": {"permission_mode": "manual"}},
        provider_default_modes={"claude": "bypass"},
    )
    manager = _manager(tmp_path, config)
    work = manager.create_project("W", workspace="work")
    personal = manager.create_project("P", workspace="personal")

    assert manager.create_chat(work.project_id).mode == "normal"
    assert manager.create_chat(personal.project_id).mode == "bypass"
    assert manager.create_chat(work.project_id, mode="auto").mode == "auto"

    config.workspaces["work"].provider_overrides = {"claude": {"permission_mode": "bypass"}}
    config.provider_default_modes = {"claude": "normal"}
    assert manager.create_chat(work.project_id).mode == "bypass"

    config.workspaces["work"].provider_overrides = {}
    assert manager.create_chat(work.project_id).mode == "normal"
    config.provider_default_modes = {}
    assert manager.create_chat(work.project_id).mode == config.claude_mode


def test_insights_model_resolution_order(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {"claude": {"insights_model": "opus"}},
        provider_insights_models={"claude": "sonnet"},
    )
    assert resolve_insights_model(config, "work", "claude", source_model="haiku") == "opus"
    assert resolve_helper_model(config, "work", "claude", source_model="haiku") == "opus"
    # Another workspace sees the engine's value.
    assert resolve_insights_model(config, "personal", "claude", source_model="haiku") == "sonnet"

    config.workspaces["work"].provider_overrides = {}
    assert resolve_insights_model(config, "work", "claude", source_model="haiku") == "sonnet"

    config.provider_insights_models = {}
    assert resolve_insights_model(config, "work", "claude", source_model="haiku") == "haiku"
    assert resolve_insights_model(config, "work", "claude") == config.default_model_for_workspace(
        "work", "claude"
    )
    # The Claude helper falls to Haiku only when neither override is set.
    assert resolve_helper_model(config, "work", "claude", source_model="opus") == HELPER_MODEL_CLAUDE
    config.workspaces["work"].provider_overrides = {"claude": {"insights_model": "opus"}}
    assert resolve_helper_model(config, "work", "claude") == "opus"


def test_schedule_routing_uses_the_workspace_override(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {"claude": {"model": "opus", "thinking": "high", "permission_mode": "manual"}},
        provider_default_models={"claude": "sonnet"},
    )
    manager = _manager(tmp_path, config)
    work = manager.create_project("W", workspace="work")

    def entry(**kwargs) -> ScheduleEntry:
        return ScheduleEntry(
            schedule_id="sched-1",
            daily_time_utc="08:00",
            prompt="Go.",
            chat_id=0,
            created_at="2026-06-08T00:00:00Z",
            web_project_id=work.project_id,
            workspace="work",
            **kwargs,
        )

    provider, model, workspace = manager.schedule_effective_routing(entry())
    assert (provider, model, workspace) == ("claude", "opus", "work")
    assert manager.schedule_default_model(work.project_id) == "opus"

    # The shape main._resolve_schedule_target gives prepare_schedule_chat.
    mode = config.default_mode_for_provider(provider, workspace)
    assert mode == "normal"
    chat_id = manager.prepare_schedule_chat(entry(), "Go.", model, mode, provider)
    chat = manager.get_chat(chat_id)
    assert (chat.model, chat.thinking_level, chat.mode) == ("opus", "high", "normal")

    # An explicit entry model and mode still win.
    provider, model, _ = manager.schedule_effective_routing(entry(model="haiku"))
    assert model == "haiku"
    explicit = entry(model="haiku", mode="bypass")
    assert (explicit.mode or config.default_mode_for_provider(provider, workspace)) == "bypass"

    source = inspect.getsource(ciao_main)
    assert "provider, model, workspace = pcm.schedule_effective_routing(entry)" in source
    assert "config.default_mode_for_provider(provider, workspace)" in source


def test_memory_pass_chat_uses_the_workspace_insights_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_pass, "MEMORY_PASS_CHATS", True)
    monkeypatch.setattr(ProjectChatManager, "start_stream", lambda self, *a, **k: None)
    config = _config(
        tmp_path,
        {"claude": {"insights_model": "opus"}},
        provider_insights_models={"claude": "sonnet"},
    )
    manager = _manager(tmp_path, config)

    chat_id = manager.enqueue_project_closure(
        workspace="work",
        vault_folder="shipped",
        project_name="Shipped",
        outcome="completed",
        note="",
        recorded_at="2026-10-11T10:00:00+00:00",
    )
    assert chat_id is not None
    assert manager.get_chat(chat_id).model == "opus"

    other = manager.enqueue_project_closure(
        workspace="personal",
        vault_folder="other",
        project_name="Other",
        outcome="completed",
        note="",
        recorded_at="2026-10-11T10:00:00+00:00",
    )
    assert other is not None
    assert manager.get_chat(other).model == "sonnet"


def test_webhook_style_chat_uses_workspace_overrides(tmp_path: Path) -> None:
    """create_chat with no model/mode is the single path for webhook, update-task,
    proposal and delegated chats."""
    config = _config(
        tmp_path,
        {"claude": {"model": "opus", "permission_mode": "bypass", "thinking": "max"}},
    )
    manager = _manager(tmp_path, config)
    work = manager.create_project("W", workspace="work")

    chat = manager.create_chat(work.project_id)
    assert (chat.model, chat.mode, chat.thinking_level) == ("opus", "bypass", "max")


def test_mcp_child_mode_ignores_the_workspace_override(tmp_path: Path) -> None:
    """An override never raises (or lowers) the ceiling of an MCP-spawned chat."""
    config = _config(tmp_path, {"claude": {"permission_mode": "bypass"}})
    manager = _manager(tmp_path, config)
    work = manager.create_project("W", workspace="work")
    parent = manager.create_chat(work.project_id, mode="normal")
    assert parent.mode == "normal"
    plane = CiaoControlPlane(config, project_chat_manager=manager, schedule_manager=None)
    principal = McpPrincipal(
        token_id="t",
        chat_id=parent.chat_id,
        project_id=work.project_id,
        workspace="work",
        provider="claude",
    )

    result = plane.chat_create(principal, None)
    assert manager.get_chat(result["data"]["chat_id"]).mode == "normal"

    asked = plane.chat_create(principal, None, mode="bypass")
    assert manager.get_chat(asked["data"]["chat_id"]).mode == "normal"
    assert asked["data"]["mode_clamped"] is True

    # And the other direction: a manual override does not lower an auto parent.
    config.workspaces["work"].provider_overrides = {"claude": {"permission_mode": "manual"}}
    auto_parent = manager.create_chat(work.project_id, mode="auto")
    auto_principal = McpPrincipal(
        token_id="t",
        chat_id=auto_parent.chat_id,
        project_id=work.project_id,
        workspace="work",
        provider="claude",
    )
    child = plane.chat_create(auto_principal, None)
    assert manager.get_chat(child["data"]["chat_id"]).mode == "auto"


def test_tier_alias_on_non_claude_uses_the_workspace_override(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {"opencode": {"model": "x/y"}},
        provider_default_models={"opencode": "engine/model"},
    )
    manager = _manager(tmp_path, config)

    assert manager._model_for_provider("sonnet", "opencode", "work") == "x/y"
    assert manager._model_for_provider("sonnet", "opencode", "personal") == "engine/model"
    assert manager._model_for_provider("sonnet", "opencode") == "engine/model"


# -- persistence --------------------------------------------------------------


def _load(tmp_path: Path) -> CiaoConfig:
    return CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "t",
            "CIAO_WORKSPACE": str(tmp_path),
            "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        }
    )


def test_workspace_persistence_roundtrip(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    registry = runtime / "workspaces.json"
    registry.write_text(
        json.dumps(
            [
                {"name": "personal", "vault_root": "memory-vault/personal"},
                {
                    "name": "work",
                    "vault_root": "memory-vault/work",
                    "provider_overrides": {"claude": {"model": "opus"}},
                },
                {
                    "name": "bad",
                    "vault_root": "memory-vault/bad",
                    "provider_overrides": {"claude": {"permission_mode": "plan"}},
                },
            ]
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        config = _load(tmp_path)
    assert config.workspace("work").provider_overrides == {"claude": {"model": "opus"}}
    assert config.workspace("personal").provider_overrides == {}
    assert config.workspace("bad").provider_overrides == {}
    assert any("provider_overrides" in r.message and "bad" in r.message for r in caplog.records)

    config.persist_workspace_registry()
    rows = {row["name"]: row for row in json.loads(registry.read_text())}
    assert rows["work"]["provider_overrides"] == {"claude": {"model": "opus"}}
    assert "provider_overrides" not in rows["personal"]
    assert "provider_overrides" not in rows["bad"]


def test_rename_carries_overrides(tmp_path: Path) -> None:
    config = _config(tmp_path, {"claude": {"model": "opus"}})
    renamed = preview_renamed_config(config.workspace("work"), old="work", new="job")
    assert renamed.name == "job"
    assert renamed.provider_overrides == {"claude": {"model": "opus"}}


# -- agent surfaces keep their caller's ceiling (review round 1) --------------


def _agent_world(tmp_path: Path, parent_mode: str):
    from ciao.schedules import ScheduleManager, ScheduleStore

    config = _config(
        tmp_path,
        {"claude": {"permission_mode": "bypass"}},
        vault_root=tmp_path / "memory-vault",
    )
    manager = _manager(tmp_path, config)
    project = manager.create_project("W", workspace="work")
    parent = manager.create_chat(project.project_id, mode=parent_mode)
    schedules = ScheduleManager(
        store=ScheduleStore(tmp_path), dispatch_to_web=lambda *a, **k: None
    )
    plane = CiaoControlPlane(
        config, project_chat_manager=manager, schedule_manager=schedules
    )
    principal = McpPrincipal(
        token_id="t",
        chat_id=parent.chat_id,
        project_id=project.project_id,
        workspace="work",
        provider="claude",
    )
    return plane, manager, principal, project


def test_agent_created_schedule_stores_the_capped_mode(tmp_path: Path) -> None:
    plane, _manager_, principal, _project = _agent_world(tmp_path, "normal")

    created = plane.schedule_create(
        principal, prompt="Check.", daily_time="09:00", timezone="UTC", frequency="weekly"
    )["data"]
    stored = next(
        e for e in plane.schedules.list_entries() if e.schedule_id == created["schedule_id"]
    )
    assert stored.mode == "normal"

    # An explicit stronger mode is clamped too; a weaker one is honoured.
    clamped = plane.schedule_create(
        principal, prompt="Check.", daily_time="09:00", timezone="UTC",
        frequency="weekly", mode="bypass",
    )["data"]
    assert next(
        e for e in plane.schedules.list_entries() if e.schedule_id == clamped["schedule_id"]
    ).mode == "normal"

    updated = plane.schedule_update(principal, created["schedule_id"], mode="bypass")["data"]
    assert next(
        e for e in plane.schedules.list_entries() if e.schedule_id == updated["schedule_id"]
    ).mode == "normal"


def test_legacy_workspaces_env_import_drops_provider_overrides(tmp_path: Path) -> None:
    """The one-time CIAO_WORKSPACES import must not carry a bypass override in."""
    config = _config(tmp_path)
    config.workspaces = {}
    config.legacy_workspaces_env = json.dumps(
        [{"name": "work", "vault_root": "work",
          "provider_overrides": {"claude": {"permission_mode": "bypass"}}}]
    )
    assert config.import_legacy_workspaces_env() == ["work"]
    assert config.workspace("work").provider_overrides == {}
    rows = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    assert "provider_overrides" not in rows[0]


@pytest.mark.parametrize(
    "provider, field, value",
    [
        ("opencode", "model", "x" * 201),
        ("opencode", "model", "vendor/model; rm -rf"),
        ("opencode", "insights_model", "has space"),
        ("opencode", "model", "new\nline"),
        ("claude", "model", "opus$(x)"),
    ],
)
def test_override_model_ids_are_bounded_and_charset_checked(
    provider: str, field: str, value: str
) -> None:
    with pytest.raises(ValueError) as excinfo:
        coerce_provider_overrides({provider: {field: value}})
    assert value not in str(excinfo.value)
    assert f"provider_overrides.{provider}.{field}" in str(excinfo.value)


def test_override_model_ids_accept_real_ids() -> None:
    ok = {"opencode": {"model": "anthropic/claude-sonnet-4.5:beta@2", "insights_model": "a_b-c[1]"}}
    assert coerce_provider_overrides(ok) == ok


def test_invalid_stored_override_warning_does_not_log_the_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "workspaces.json").write_text(
        json.dumps([{"name": "work", "vault_root": "work",
                     "provider_overrides": {"opencode": {"model": "SECRET value!"}}}]),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        config = _load(tmp_path)
    assert config.workspace("work").provider_overrides == {}
    assert any("provider_overrides" in r.getMessage() for r in caplog.records)
    assert "SECRET" not in caplog.text
