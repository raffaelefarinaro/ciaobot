"""Target isolation and routing tests for declarative live evaluations."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ciao.eval_runner import (
    ChatObservation,
    IsolatedChatServer,
    _assistant_result,
)
from ciao.evals import (
    EvalAssertions,
    EvalDefaults,
    EvalRunOverrides,
    EvalScenario,
    EvalTarget,
    EvalTargetError,
    resolve_routing,
    run_eval_scenario,
    stage_eval_target,
)


def _scenario(kind: str, name: str) -> EvalScenario:
    return EvalScenario(
        name=f"{kind}-{name}",
        description="target test",
        target=EvalTarget(kind=kind, name=name),  # type: ignore[arg-type]
        prompt="Complete the requested work.",
        provider=None,
        model=None,
        surface=None,
        assertions=EvalAssertions(output_contains=("done",)),
    )


def _observation(prompt: str) -> ChatObservation:
    return ChatObservation(
        scenario="scenario",
        selected_model="model",
        effective_model="model",
        final_text=prompt,
        error="",
        elapsed_ms=1,
        provider_duration_ms=1,
        usage={},
        tokens=None,
        provider_tools=(),
        mcp_tools=(),
        mcp_errors=0,
    )


def test_skill_target_stages_only_requested_skill(tmp_path: Path) -> None:
    source = tmp_path / "source"
    isolated = tmp_path / "isolated"
    for name in ("wanted", "other"):
        skill = source / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        (skill / "reference.txt").write_text(name, encoding="utf-8")
    sync_calls: list[tuple[Path, bool]] = []

    staged = stage_eval_target(
        source,
        isolated,
        EvalTarget(kind="skill", name="wanted"),
        sync=lambda root, *, refresh_upstream: sync_calls.append(
            (Path(root), refresh_upstream)
        ),
    )

    assert staged.definition_path == isolated / "skills" / "wanted" / "SKILL.md"
    assert (isolated / "skills" / "wanted" / "reference.txt").read_text() == "wanted"
    assert not (isolated / "skills" / "other").exists()
    assert sync_calls == [(isolated.resolve(), False)]


@pytest.mark.parametrize(
    ("target", "relative_definition"),
    [
        (
            EvalTarget(kind="skill", name="ciao-capabilities"),
            Path("skills/ciao-capabilities/SKILL.md"),
        ),
        (
            EvalTarget(kind="subagent", name="researcher"),
            Path("subagents/researcher.md"),
        ),
    ],
)
def test_target_falls_back_to_packaged_stock_assets(
    tmp_path: Path,
    target: EvalTarget,
    relative_definition: Path,
) -> None:
    isolated = tmp_path / "isolated"

    staged = stage_eval_target(
        tmp_path / "empty-source",
        isolated,
        target,
        sync=lambda _root, *, refresh_upstream: None,
    )

    assert staged.definition_path == isolated / relative_definition
    assert staged.definition_path.is_file()


def test_real_sync_exposes_only_the_selected_skill(tmp_path: Path) -> None:
    source = tmp_path / "source"
    definition = source / "skills" / "wanted" / "SKILL.md"
    definition.parent.mkdir(parents=True)
    definition.write_text(
        "---\nname: wanted\ndescription: selected test skill\n---\n\n# Wanted\n",
        encoding="utf-8",
    )
    isolated = tmp_path / "isolated"

    stage_eval_target(
        source,
        isolated,
        EvalTarget(kind="skill", name="wanted"),
    )

    assert {path.name for path in (isolated / ".claude" / "skills").iterdir()} == {
        "wanted"
    }
    assert {path.name for path in (isolated / ".agents" / "skills").iterdir()} == {
        "wanted"
    }
    assert list((isolated / ".claude" / "agents").iterdir()) == []
    assert list((isolated / ".codex" / "agents").iterdir()) == []
    assert list((isolated / "commands").iterdir()) == []
    assert list((isolated / ".claude" / "commands").iterdir()) == []
    assert not (isolated / "CLAUDE.md").exists()
    assert not (isolated / "AGENTS.md").exists()


def test_subagent_target_stages_only_definition_and_companion_assets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    isolated = tmp_path / "isolated"
    subagents = source / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "researcher.md").write_text("# Researcher\n", encoding="utf-8")
    (subagents / "other.md").write_text("# Other\n", encoding="utf-8")
    companion = subagents / "researcher"
    (companion / "scripts").mkdir(parents=True)
    (companion / "resources").mkdir()
    (companion / "scripts" / "normalize.py").write_text("VALUE = 1\n")
    (companion / "resources" / "policy.md").write_text("# Policy\n")
    (companion / "notes.tmp").write_text("not a defined companion asset")

    staged = stage_eval_target(
        source,
        isolated,
        EvalTarget(kind="subagent", name="researcher"),
        sync=lambda _root, *, refresh_upstream: None,
    )

    assert staged.definition_path == isolated / "subagents" / "researcher.md"
    assert staged.companion_path == isolated / "subagents" / "researcher"
    assert (staged.companion_path / "scripts" / "normalize.py").is_file()
    assert (staged.companion_path / "resources" / "policy.md").is_file()
    assert not (staged.companion_path / "notes.tmp").exists()
    assert not (isolated / "subagents" / "other.md").exists()


def test_real_sync_exposes_only_the_selected_subagent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    definition = source / "subagents" / "researcher.md"
    definition.parent.mkdir(parents=True)
    definition.write_text(
        "---\ndescription: selected test role\n---\n\n# Researcher\n",
        encoding="utf-8",
    )
    isolated = tmp_path / "isolated"

    stage_eval_target(
        source,
        isolated,
        EvalTarget(kind="subagent", name="researcher"),
    )

    assert {path.name for path in (isolated / ".claude" / "agents").iterdir()} == {
        "researcher.md"
    }
    assert {path.name for path in (isolated / ".agents" / "skills").iterdir()} == {
        "ciao-agent-researcher"
    }
    assert {path.name for path in (isolated / ".codex" / "agents").iterdir()} == {
        "researcher.toml"
    }
    config = (isolated / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert '[agents."researcher"]' in config
    assert '[agents."memory"]' not in config
    assert list((isolated / "commands").iterdir()) == []
    assert list((isolated / ".claude" / "commands").iterdir()) == []
    assert not (isolated / "CLAUDE.md").exists()
    assert not (isolated / "AGENTS.md").exists()


@pytest.mark.parametrize("kind", ["skill", "subagent"])
def test_missing_target_fails_before_server_start(tmp_path: Path, kind: str) -> None:
    starts = 0

    class Server:
        is_running = False

        def start(self) -> None:
            nonlocal starts
            starts += 1

    with pytest.raises(EvalTargetError, match="not found"):
        run_eval_scenario(
            _scenario(kind, "missing"),
            defaults=EvalDefaults("claude", "sonnet", "mcp"),
            source_workspace=tmp_path / "source",
            isolated_root=tmp_path / "isolated",
            server_factory=lambda **_kwargs: Server(),
            sync=lambda _root, *, refresh_upstream: None,
        )

    assert starts == 0


def test_ambiguous_target_fails_before_server_start(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for root in ("skills", ".claude/skills"):
        skill = source / root / "duplicate"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {root}\n")
    starts = 0

    class Server:
        is_running = False

        def start(self) -> None:
            nonlocal starts
            starts += 1

    with pytest.raises(EvalTargetError, match="ambiguous"):
        run_eval_scenario(
            _scenario("skill", "duplicate"),
            defaults=EvalDefaults("claude", "sonnet", "mcp"),
            source_workspace=source,
            isolated_root=tmp_path / "isolated",
            server_factory=lambda **_kwargs: Server(),
            sync=lambda _root, *, refresh_upstream: None,
        )

    assert starts == 0


def test_source_workspace_is_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    skill = source / "skills" / "wanted"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# wanted\n")
    (source / "CLAUDE.md").write_text("# source instructions\n")
    (source / "AGENTS.md").symlink_to("CLAUDE.md")
    before = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }

    stage_eval_target(
        source,
        tmp_path / "isolated",
        EvalTarget(kind="skill", name="wanted"),
        sync=lambda _root, *, refresh_upstream: None,
    )

    after = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert (source / "CLAUDE.md").read_text() == "# source instructions\n"
    assert (source / "AGENTS.md").is_symlink()


def test_cli_override_beats_scenario_then_suite_default() -> None:
    defaults = EvalDefaults("claude", "suite-model", "legacy")
    scenario = replace(
        _scenario("skill", "wanted"),
        provider="codex",
        model="scenario-model",
        surface="mcp",
    )

    assert resolve_routing(defaults, scenario) == EvalDefaults(
        "codex", "scenario-model", "mcp"
    )
    assert resolve_routing(
        defaults,
        scenario,
        EvalRunOverrides(
            provider="claude",
            model="cli-model",
            surface="legacy",
        ),
    ) == EvalDefaults("claude", "cli-model", "legacy")
    assert resolve_routing(
        defaults,
        scenario,
        EvalRunOverrides(provider="claude"),
    ) == EvalDefaults("claude", "scenario-model", "mcp")


@pytest.mark.parametrize(
    ("kind", "expected_prefix"),
    [
        ("skill", "Use the wanted skill for this task."),
        (
            "subagent",
            "Delegate this task to the wanted subagent and wait for its final result.",
        ),
    ],
)
def test_run_prefixes_prompt_and_uses_fresh_chat_runner(
    tmp_path: Path,
    kind: str,
    expected_prefix: str,
) -> None:
    source = tmp_path / "source"
    if kind == "skill":
        definition = source / "skills" / "wanted" / "SKILL.md"
    else:
        definition = source / "subagents" / "wanted.md"
    definition.parent.mkdir(parents=True)
    definition.write_text("# wanted\n")
    seen: dict[str, Any] = {}

    def server_factory(**kwargs: Any) -> object:
        seen["server"] = kwargs
        return object()

    def run_turn(server: object, spec: Any) -> ChatObservation:
        seen["runner_server"] = server
        seen["spec"] = spec
        return _observation(spec.prompt)

    observation = run_eval_scenario(
        _scenario(kind, "wanted"),
        defaults=EvalDefaults("claude", "sonnet", "mcp"),
        source_workspace=source,
        isolated_root=tmp_path / "isolated",
        server_factory=server_factory,
        run_turn=run_turn,
        sync=lambda _root, *, refresh_upstream: None,
    )

    assert seen["server"]["install_packaged_assets"] is False
    assert seen["server"]["require_subagent_synthesis"] is (kind == "subagent")
    assert seen["spec"].prompt == f"{expected_prefix}\n\nComplete the requested work."
    assert observation.final_text.startswith(expected_prefix)


def test_subagent_run_waits_for_background_completion_and_synthesis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = IsolatedChatServer(
        root=tmp_path,
        surface="mcp",
        provider="claude",
        workspace_name="personal",
        startup_timeout=1,
        require_subagent_synthesis=True,
    )
    server.base_url = "http://example.test"
    server.process = type("Process", (), {"poll": lambda self: None})()  # type: ignore[assignment]
    active = iter(
        [
            {"active_chat_ids": ["chat"]},  # parent response
            {"active_chat_ids": []},  # transition to watcher
            {"active_chat_ids": ["chat"]},  # background subagent
            {"active_chat_ids": []},  # watcher exits after completion
            {"active_chat_ids": []},  # synthesis has not started
            {"active_chat_ids": []},  # still no synthesis
            {"active_chat_ids": ["chat"]},  # synthesis starts
            {"active_chat_ids": []},
        ]
    )
    interim = [{"role": "assistant", "content": "delegated", "duration_ms": 1}]
    completed_without_synthesis = [
        *interim,
        {"role": "system", "content": "🤖 Subagent completed: result ready"},
    ]
    messages = iter(
        [
            interim,
            interim,
            completed_without_synthesis,
            completed_without_synthesis,
            completed_without_synthesis,
            completed_without_synthesis,
            [
                *completed_without_synthesis,
                {"role": "assistant", "content": "final synthesis"},
            ],
            [
                *completed_without_synthesis,
                {"role": "assistant", "content": "final synthesis"},
            ],
        ]
    )
    subagents = iter(
        [
            [],  # dispatch has not reached the discovery endpoint yet
            [{"agent_id": "agent-1"}],  # metadata merge lags discovery
            [{"agent_id": "agent-1", "is_async": True, "status": "running"}],
            [{"agent_id": "agent-1", "is_async": True, "status": "completed"}],
            [{"agent_id": "agent-1", "is_async": True, "status": "completed"}],
            [{"agent_id": "agent-1", "is_async": True, "status": "completed"}],
            [{"agent_id": "agent-1", "is_async": True, "status": "completed"}],
            [{"agent_id": "agent-1", "is_async": True, "status": "completed"}],
        ]
    )

    def request(_base_url: str, path: str, **_kwargs: Any) -> Any:
        if path == "/api/active-chats":
            return next(active)
        if path == "/api/chats/chat/subagents":
            return next(subagents)
        return next(messages)

    monkeypatch.setattr("ciao.eval_runner._json_request", request)
    monkeypatch.setattr("ciao.eval_runner.time.sleep", lambda _seconds: None)

    result = server.wait_for_turn("chat", timeout=2)

    assert result[-1]["content"] == "final synthesis"
    assert _assistant_result(result)[0] == "final synthesis"


def test_subagent_wait_returns_stable_terminal_parent_when_nothing_delegated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = IsolatedChatServer(
        root=tmp_path,
        surface="mcp",
        provider="claude",
        workspace_name="personal",
        startup_timeout=1,
        require_subagent_synthesis=True,
        subagent_discovery_polls=3,
    )
    server.base_url = "http://example.test"
    server.process = type("Process", (), {"poll": lambda self: None})()  # type: ignore[assignment]
    terminal = [{"role": "assistant", "content": "I did not delegate", "duration_ms": 2}]
    calls = {"active": 0}

    def request(_base_url: str, path: str, **_kwargs: Any) -> Any:
        if path == "/api/active-chats":
            calls["active"] += 1
            return {"active_chat_ids": []}
        if path.endswith("/subagents"):
            return []
        return terminal

    monkeypatch.setattr("ciao.eval_runner._json_request", request)
    monkeypatch.setattr("ciao.eval_runner.time.sleep", lambda _seconds: None)

    result = server.wait_for_turn("chat", timeout=2)

    assert calls["active"] == 3
    assert result == terminal


def test_subagent_wait_accepts_explicit_claude_foreground_delegation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = IsolatedChatServer(
        root=tmp_path,
        surface="mcp",
        provider="claude",
        workspace_name="personal",
        startup_timeout=1,
        require_subagent_synthesis=True,
    )
    server.base_url = "http://example.test"
    server.process = type("Process", (), {"poll": lambda self: None})()  # type: ignore[assignment]
    active = iter([{"active_chat_ids": ["chat"]}, {"active_chat_ids": []}])
    final = [{"role": "assistant", "content": "foreground synthesis", "duration_ms": 2}]
    foreground = [
        {
            "agent_id": "agent-1",
            "is_async": False,
            "status": "completed",
        }
    ]

    def request(_base_url: str, path: str, **_kwargs: Any) -> Any:
        if path == "/api/active-chats":
            return next(active)
        if path.endswith("/subagents"):
            return foreground
        return final

    monkeypatch.setattr("ciao.eval_runner._json_request", request)
    monkeypatch.setattr("ciao.eval_runner.time.sleep", lambda _seconds: None)

    assert server.wait_for_turn("chat", timeout=2) == final


def test_subagent_wait_uses_codex_collaboration_status_as_completion_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = IsolatedChatServer(
        root=tmp_path,
        surface="mcp",
        provider="codex",
        workspace_name="personal",
        startup_timeout=1,
        require_subagent_synthesis=True,
    )
    server.base_url = "http://example.test"
    server.process = type("Process", (), {"poll": lambda self: None})()  # type: ignore[assignment]
    active = iter([{"active_chat_ids": ["chat"]}, {"active_chat_ids": []}])
    messages = iter(
        [
            [],
            [{"role": "assistant", "content": "codex synthesis", "duration_ms": 3}],
        ]
    )
    subagents = iter(
        [
            [{"agent_id": "child", "is_async": True, "status": "running"}],
            [{"agent_id": "child", "is_async": True, "status": "completed"}],
        ]
    )

    def request(_base_url: str, path: str, **_kwargs: Any) -> Any:
        if path == "/api/active-chats":
            return next(active)
        if path.endswith("/subagents"):
            return next(subagents)
        return next(messages)

    monkeypatch.setattr("ciao.eval_runner._json_request", request)
    monkeypatch.setattr("ciao.eval_runner.time.sleep", lambda _seconds: None)

    result = server.wait_for_turn("chat", timeout=2)

    assert _assistant_result(result)[0] == "codex synthesis"


def test_rejects_source_and_destination_overlap(tmp_path: Path) -> None:
    source = tmp_path / "source"
    skill = source / "skills" / "wanted"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# wanted\n")

    for isolated in (skill / "eval", tmp_path):
        with pytest.raises(EvalTargetError, match="overlap"):
            stage_eval_target(
                source,
                isolated,
                EvalTarget(kind="skill", name="wanted"),
                sync=lambda _root, *, refresh_upstream: None,
            )


def test_rejects_top_level_skill_symlink_escape(tmp_path: Path) -> None:
    source = tmp_path / "source"
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# outside\n")
    (source / "skills").mkdir(parents=True)
    (source / "skills" / "wanted").symlink_to(outside, target_is_directory=True)

    with pytest.raises(EvalTargetError, match="escapes"):
        stage_eval_target(
            source,
            tmp_path / "isolated",
            EvalTarget(kind="skill", name="wanted"),
            sync=lambda _root, *, refresh_upstream: None,
        )


def test_rejects_nested_skill_symlink_escape(tmp_path: Path) -> None:
    source = tmp_path / "source"
    skill = source / "skills" / "wanted"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# wanted\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (skill / "secret.txt").symlink_to(outside)

    with pytest.raises(EvalTargetError, match="escapes"):
        stage_eval_target(
            source,
            tmp_path / "isolated",
            EvalTarget(kind="skill", name="wanted"),
            sync=lambda _root, *, refresh_upstream: None,
        )


def test_safe_internal_skill_symlink_is_materialized(tmp_path: Path) -> None:
    source = tmp_path / "source"
    skill = source / "skills" / "wanted"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# wanted\n")
    (skill / "policy.md").write_text("policy")
    (skill / "policy-link.md").symlink_to("policy.md")

    stage_eval_target(
        source,
        tmp_path / "isolated",
        EvalTarget(kind="skill", name="wanted"),
        sync=lambda _root, *, refresh_upstream: None,
    )

    copied = tmp_path / "isolated" / "skills" / "wanted" / "policy-link.md"
    assert copied.read_text() == "policy"
    assert not copied.is_symlink()


def test_rejects_subagent_definition_symlink_escape(tmp_path: Path) -> None:
    source = tmp_path / "source"
    outside = tmp_path / "outside.md"
    outside.write_text("# outside\n")
    (source / "subagents").mkdir(parents=True)
    (source / "subagents" / "researcher.md").symlink_to(outside)

    with pytest.raises(EvalTargetError, match="escapes"):
        stage_eval_target(
            source,
            tmp_path / "isolated",
            EvalTarget(kind="subagent", name="researcher"),
            sync=lambda _root, *, refresh_upstream: None,
        )


@pytest.mark.parametrize("escape_kind", ["asset-dir", "nested-file"])
def test_rejects_subagent_companion_symlink_escape(
    tmp_path: Path, escape_kind: str
) -> None:
    source = tmp_path / "source"
    subagents = source / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "researcher.md").write_text("# researcher\n")
    companion = subagents / "researcher"
    companion.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    if escape_kind == "asset-dir":
        (companion / "scripts").symlink_to(outside, target_is_directory=True)
    else:
        resources = companion / "resources"
        resources.mkdir()
        (resources / "secret.txt").symlink_to(outside / "secret.txt")

    with pytest.raises(EvalTargetError, match="escapes"):
        stage_eval_target(
            source,
            tmp_path / "isolated",
            EvalTarget(kind="subagent", name="researcher"),
            sync=lambda _root, *, refresh_upstream: None,
        )


def test_safe_internal_subagent_companion_symlink_is_materialized(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    subagents = source / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "researcher.md").write_text("# researcher\n")
    resources = subagents / "researcher" / "resources"
    resources.mkdir(parents=True)
    (resources / "policy.md").write_text("policy")
    (resources / "policy-link.md").symlink_to("policy.md")

    stage_eval_target(
        source,
        tmp_path / "isolated",
        EvalTarget(kind="subagent", name="researcher"),
        sync=lambda _root, *, refresh_upstream: None,
    )

    copied = (
        tmp_path
        / "isolated"
        / "subagents"
        / "researcher"
        / "resources"
        / "policy-link.md"
    )
    assert copied.read_text() == "policy"
    assert not copied.is_symlink()
