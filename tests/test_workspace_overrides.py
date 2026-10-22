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
from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal
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


def _operator_schedule(plane, project, mode: str):
    """A routine the operator made (store write), with a saved mode."""
    return plane.schedules.create(
        daily_time_utc="09:00",
        prompt="Original text.",
        model="",
        mode=mode,
        chat_id=0,
        web_project_id=project.project_id,
        web_project_name=project.name,
        workspace="work",
    )


def _stored(plane, schedule_id: str):
    return next(e for e in plane.schedules.list_entries() if e.schedule_id == schedule_id)


def test_saved_schedule_mode_is_a_ceiling_at_dispatch() -> None:
    """Lowering the workspace default later lowers an agent routine's saved mode."""
    from ciao.execution_modes import MODE_RANK, cap_schedule_mode

    assert cap_schedule_mode("", "auto") == "auto"
    assert cap_schedule_mode("bypass", "normal") == "normal"
    assert cap_schedule_mode("bypass", "auto") == "auto"
    # A saved weaker mode stays weaker when the default rises.
    assert cap_schedule_mode("normal", "bypass") == "normal"
    assert cap_schedule_mode("auto", "auto") == "auto"
    # A value that is not a mode cannot raise anything.
    assert cap_schedule_mode("junk", "normal") == "normal"

    # control_plane and main share one rank object.
    from ciao import control_plane as cp_module

    assert cp_module._MODE_RANK is MODE_RANK


def test_schedule_run_mode_resolves_through_the_real_dispatch_function(tmp_path: Path) -> None:
    """main._resolve_schedule_target calls schedule_run_mode; exercise it with a real config."""
    config = _config(
        tmp_path,
        {"claude": {"permission_mode": "bypass"}},
        provider_default_modes={"claude": "auto"},
    )
    assert ciao_main.schedule_run_mode(config, "", "claude", "work") == "bypass"
    assert ciao_main.schedule_run_mode(config, "normal", "claude", "work") == "normal"
    # Another workspace has the engine default (auto): a saved bypass drops to it.
    assert ciao_main.schedule_run_mode(config, "bypass", "claude", "personal") == "auto"
    # Lowering the workspace override lowers a saved higher mode with it.
    config.workspaces["work"].provider_overrides = {"claude": {"permission_mode": "manual"}}
    assert ciao_main.schedule_run_mode(config, "bypass", "claude", "work") == "normal"
    assert ciao_main.schedule_run_mode(config, "", "claude", "work") == "normal"
    # The closure in create_app goes through this function, not its own copy.
    assert "schedule_run_mode(config, entry.mode, provider, workspace)" in " ".join(
        inspect.getsource(ciao_main).split()
    )


def _operator_schedule(plane, project, mode: str):
    """A routine the operator made (store write), with a saved mode."""
    return plane.schedules.create(
        daily_time_utc="09:00",
        prompt="Original text.",
        model="",
        mode=mode,
        chat_id=0,
        web_project_id=project.project_id,
        web_project_name=project.name,
        workspace="work",
    )


def _stored(plane, schedule_id: str):
    return next(e for e in plane.schedules.list_entries() if e.schedule_id == schedule_id)


def test_saved_schedule_mode_is_a_ceiling_at_dispatch() -> None:
    """Lowering the workspace default later lowers an agent routine's saved mode."""
    from ciao.execution_modes import MODE_RANK, cap_schedule_mode

    assert cap_schedule_mode("", "auto") == "auto"
    assert cap_schedule_mode("bypass", "normal") == "normal"
    assert cap_schedule_mode("bypass", "auto") == "auto"
    # A saved weaker mode stays weaker when the default rises.
    assert cap_schedule_mode("normal", "bypass") == "normal"
    assert cap_schedule_mode("auto", "auto") == "auto"
    # A value that is not a mode cannot raise anything.
    assert cap_schedule_mode("junk", "normal") == "normal"

    # control_plane and main share one rank object, and main uses the cap.
    from ciao import control_plane as cp_module

    assert cp_module._MODE_RANK is MODE_RANK


def test_agent_edit_of_a_routines_text_is_capped_at_the_callers_mode(tmp_path: Path) -> None:
    plane, _manager_, principal, project = _agent_world(tmp_path, "normal")

    # Operator routine with no saved mode: an agent rewrite pins it to the caller's ceiling.
    inherits = _operator_schedule(plane, project, "")
    plane.schedule_update(principal, inherits.schedule_id, prompt="New text.")
    assert _stored(plane, inherits.schedule_id).mode == "normal"

    # Operator routine saved at bypass: a rewrite cannot keep it there.
    high = _operator_schedule(plane, project, "bypass")
    plane.schedule_update(principal, high.schedule_id, prompt="New text.")
    assert _stored(plane, high.schedule_id).mode == "normal"

    # Naming the mode key: stronger is clamped, "" cannot clear a saved mode.
    auto = _operator_schedule(plane, project, "bypass")
    plane.schedule_update(principal, auto.schedule_id, mode="")
    assert _stored(plane, auto.schedule_id).mode == "normal"

    # An update that touches neither text nor mode leaves the saved mode alone.
    untouched = _operator_schedule(plane, project, "bypass")
    plane.schedule_update(principal, untouched.schedule_id, enabled=False)
    assert _stored(plane, untouched.schedule_id).mode == "bypass"


def _webhook(plane, tmp):
    created = plane.workspace_webhook_create("work", name="hook", instructions="Do the thing.")
    return created["trigger"] if "trigger" in created else created


def test_agent_below_the_workspace_default_cannot_enable_edit_or_rotate_a_webhook(
    tmp_path: Path,
) -> None:
    """A webhook chat starts at the workspace default, so an agent below it may not
    switch a trigger on, change what an enabled one runs, or rotate its secret."""
    plane, _manager_, principal, _project = _agent_world(tmp_path, "normal")
    trigger = _webhook(plane, tmp_path)
    tid = trigger["trigger_id"]

    def refused(**kwargs):
        with pytest.raises(ControlPlaneError) as excinfo:
            plane.webhook_update(principal, tid, expected_revision=current["revision"], **kwargs)
        assert excinfo.value.code == "webhook_requires_operator"

    current = trigger
    # Disabled trigger: enabling is refused, and so is rewriting its instructions
    # (it would run them the moment the operator switches it on).
    refused(enabled=True)
    refused(instructions="Run something else.")
    # A name change and disabling stay open.
    current = plane.webhook_update(
        principal, tid, expected_revision=current["revision"], name="renamed"
    )["data"]
    current = plane.webhook_update(
        principal, tid, expected_revision=current["revision"], enabled=False
    )["data"]

    # The operator enables it; now rewriting and rotating are refused too.
    current = plane.workspace_webhook_update(
        "work", tid, expected_revision=current["revision"], enabled=True
    )
    assert current["enabled"] is True
    refused(instructions="Run something else.")
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.webhook_rotate(principal, tid, expected_revision=current["revision"])
    assert excinfo.value.code == "webhook_requires_operator"
    # Disabling an enabled one is still allowed.
    plane.webhook_update(principal, tid, expected_revision=current["revision"], enabled=False)


def test_agent_at_the_workspace_default_may_manage_a_webhook(tmp_path: Path) -> None:
    plane, _m, principal, _p = _agent_world(tmp_path, "bypass")
    trigger = _webhook(plane, tmp_path)
    tid = trigger["trigger_id"]
    edited = plane.webhook_update(
        principal, tid, expected_revision=trigger["revision"],
        instructions="Run something else.", enabled=True,
    )["data"]
    assert edited["enabled"] is True
    plane.webhook_rotate(principal, tid, expected_revision=edited["revision"])


def test_override_warning_clips_a_long_key(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    huge = "k" * 5000
    with pytest.raises(ValueError) as excinfo:
        coerce_provider_overrides({"claude": {huge: "x"}})
    assert len(str(excinfo.value)) < 200
    with pytest.raises(ValueError) as excinfo:
        coerce_provider_overrides({huge: {}})
    assert len(str(excinfo.value)) < 200
