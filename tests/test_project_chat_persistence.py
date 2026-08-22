from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ciao import native_sidecar
from ciao.config import CiaoConfig
from ciao.models import ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.chat_broker import ChatStream
from ciao.web.project_chats import (
    ProjectChatManager,
    _StreamOutcome,
    _cap_reentry_summary,
    _reentry_transcript_text,
)


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


def _persisted_chats(tmp_path: Path) -> dict[str, dict]:
    payload = json.loads(
        (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    )
    assert payload["revision"] > 0
    return payload["chats"]


def test_existing_vault_onboarding_uses_current_layout_and_workspace_name(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CIAO_VAULT_MODE", "existing")
    manager = _make_manager(tmp_path)

    onboarding = next(
        chat for chat in manager._chats.values()
        if chat.title == "Connect Existing Vault 👋"
    )
    prompt = onboarding.handover_messages[0]["content"]

    assert "logical workspace **personal**" in prompt
    assert "projects/active/" in prompt
    assert "Workspace/Memory-Proposals.md" in prompt
    assert "`Templates/` and `personal/`/`work/` are not required" in prompt
    assert "Create Directory Structure" not in prompt

    monkeypatch.setenv("CIAO_VAULT_MODE", "scratch")
    fresh_manager = _make_manager(tmp_path / "fresh")
    fresh_onboarding = next(
        chat for chat in fresh_manager._chats.values()
        if chat.title == "Welcome to Ciaobot! 👋"
    )
    fresh_prompt = fresh_onboarding.handover_messages[0]["content"]
    assert "logical workspace **personal**" in fresh_prompt
    assert "projects/active/" in fresh_prompt
    assert "Do not create `personal/`, `work/`, or `Templates/`" in fresh_prompt
    assert "Create Directory Structure" not in fresh_prompt


def test_stale_manager_does_not_drop_chat_created_by_other_process(tmp_path: Path) -> None:
    first = _make_manager(tmp_path)
    project = first.create_project("Shared", workspace="work")
    stale = _make_manager(tmp_path)

    first_chat = first.create_chat(project.project_id, title="Created by first")
    stale_chat = stale.create_chat(project.project_id, title="Created by stale")

    chats = _persisted_chats(tmp_path)
    assert first_chat.chat_id in chats
    assert stale_chat.chat_id in chats


def test_concurrent_field_updates_to_one_chat_are_merged(tmp_path: Path) -> None:
    first = _make_manager(tmp_path)
    project = first.create_project("Shared", workspace="work")
    chat = first.create_chat(project.project_id, title="Original")
    stale = _make_manager(tmp_path)

    first.update_chat(chat.chat_id, title="Renamed")
    stale_chat = stale.get_chat(chat.chat_id)
    assert stale_chat is not None
    stale_chat.last_read_at = "2026-07-14T12:00:00Z"
    stale._save()

    persisted = _persisted_chats(tmp_path)[chat.chat_id]
    assert persisted["title"] == "Renamed"
    assert persisted["last_read_at"] == "2026-07-14T12:00:00Z"


def test_stale_manager_does_not_resurrect_concurrently_deleted_chat(tmp_path: Path) -> None:
    first = _make_manager(tmp_path)
    project = first.create_project("Shared", workspace="work")
    deleted = first.create_chat(project.project_id, title="Delete me")
    survivor = first.create_chat(project.project_id, title="Keep me")
    stale = _make_manager(tmp_path)

    assert first.delete_chat(deleted.chat_id) is True
    stale.update_chat(survivor.chat_id, title="Still here")

    chats = _persisted_chats(tmp_path)
    assert deleted.chat_id not in chats
    assert chats[survivor.chat_id]["title"] == "Still here"


def test_vault_project_identity_is_stable_after_registry_rebuild(tmp_path: Path) -> None:
    folder = tmp_path / "memory-vault" / "work" / "projects" / "active" / "rossmann-mvp"
    folder.mkdir(parents=True)
    (folder / "README.md").write_text(
        "---\ntitle: Rossmann MVP\ndescription: Shelf recognition\n---\n",
        encoding="utf-8",
    )

    first = _make_manager(tmp_path)
    first_project = next(
        project for project in first.list_projects("work")
        if project.vault_folder == "rossmann-mvp"
    )
    (tmp_path / ".runtime" / "web_projects.json").unlink()

    rebuilt = _make_manager(tmp_path)
    rebuilt_project = next(
        project for project in rebuilt.list_projects("work")
        if project.vault_folder == "rossmann-mvp"
    )
    assert rebuilt_project.project_id == first_project.project_id


def test_registry_audit_records_chat_create_and_delete(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Audited", workspace="work")
    chat = manager.create_chat(project.project_id, title="Temporary")
    assert manager.delete_chat(chat.chat_id) is True

    audit_path = tmp_path / ".runtime" / "web_projects.audit.jsonl"
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert any(chat.chat_id in event["chats"]["added"] for event in events)
    assert any(
        event["reason"] == "user_chat_delete"
        and chat.chat_id in event["chats"]["deleted"]
        for event in events
    )


def test_build_agent_request_falls_back_to_legacy_without_mcp_service(
    tmp_path: Path,
) -> None:
    # The default control surface is mcp, but a manager with no MCP service must
    # degrade gracefully to legacy instead of raising, so the app stays usable.
    manager = _make_manager(tmp_path)
    assert manager._config.control_surface == "mcp"
    assert manager._mcp_service is None
    project = manager.create_project("Fallback", workspace="work")
    chat = manager.create_chat(project.project_id)
    assert chat.control_surface == ""  # inherits the server default

    request = manager.build_agent_request(chat, prompt="hi")
    assert request.control_surface == "legacy"
    assert request.mcp_required is False
    assert request.mcp_url == ""
    assert request.mcp_token == ""


@pytest.mark.asyncio
async def test_context_marker_waits_for_provider_session(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Context", workspace="personal")
    chat = manager.create_chat(project.project_id)

    first = manager.build_agent_request(chat, prompt="first")
    retry = manager.build_agent_request(chat, prompt="retry after failure")

    assert first.context_digest
    assert chat.context_digest == ""
    assert chat.context_session_id == ""
    assert "[CIAO_CONTEXT_BEGIN]" in retry.prompt

    class _Provider:
        current_session_id = None

        async def execute_streaming(self, _request):
            self.current_session_id = "native-session"
            yield ResultEvent(type="result", result="ok")

    manager._providers[chat.chat_id] = _Provider()  # type: ignore[assignment]
    outcome = _StreamOutcome(effective_model=chat.model)
    events = [
        event
        async for event in manager._drive_stream(
            chat_id=chat.chat_id, request=first, outcome=outcome
        )
    ]

    assert len(events) == 1
    assert chat.context_digest == first.context_digest
    assert chat.context_session_id == "native-session"


@pytest.mark.asyncio
async def test_opencode_effective_model_is_persisted_for_model_less_chat(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("OpenCode model", workspace="personal")
    chat = manager.create_chat(
        project.project_id, title="OpenCode model", provider="opencode"
    )
    chat.model = ""
    request = manager.build_agent_request(chat, prompt="hello")

    class _Provider:
        current_session_id = None

        async def execute_streaming(self, _request):
            yield ResultEvent(
                type="result",
                result="ok",
                effective_model="opencode/big-pickle",
            )

    manager._providers[chat.chat_id] = _Provider()  # type: ignore[assignment]
    outcome = _StreamOutcome()
    _ = [
        event
        async for event in manager._drive_stream(
            chat_id=chat.chat_id, request=request, outcome=outcome
        )
    ]

    assert chat.model == "opencode/big-pickle"
    assert _persisted_chats(tmp_path)[chat.chat_id]["model"] == "opencode/big-pickle"


def test_chat_control_surface_round_trips_through_registry(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("MCP evaluation", workspace="personal")
    chat = manager.create_chat(project.project_id)
    chat.control_surface = "mcp"
    chat.user_turn_count = 1
    manager._save(reason="test_control_surface")

    persisted = _persisted_chats(tmp_path)[chat.chat_id]
    assert persisted["control_surface"] == "mcp"

    reloaded = _make_manager(tmp_path).get_chat(chat.chat_id)
    assert reloaded is not None
    assert reloaded.control_surface == "mcp"
    assert reloaded.to_dict()["control_surface"] == "mcp"


def test_reentry_summary_is_cached_bounded_and_invalidated_by_queue(
    tmp_path: Path, monkeypatch,
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Summary cache", workspace="personal")
    chat = manager.create_chat(project.project_id, title="Summary cache chat")
    chat.session_id = "session-summary"
    manager._save()

    monkeypatch.setattr(native_sidecar, "apple_model_available", lambda: True)
    monkeypatch.setattr(
        manager._transcripts,
        "current_filtered_jsonl",
        lambda *_args: '{"type":"user","content":"keep working"}',
    )
    calls = 0

    async def fake_respond(*_args, **_kwargs) -> str:
        nonlocal calls
        calls += 1
        return "\n".join(
            f"- point {index} " + ("x" * 130)
            for index in range(6)
        )

    monkeypatch.setattr(native_sidecar, "respond", fake_respond)

    first = asyncio.run(manager.generate_reentry_summary(chat.chat_id))
    second = asyncio.run(manager.generate_reentry_summary(chat.chat_id))

    assert first == second
    assert calls == 1
    assert len(first) <= 600
    assert len(first.splitlines()) <= 4

    reloaded = _make_manager(tmp_path).get_chat(chat.chat_id)
    assert reloaded is not None
    assert reloaded.reentry_summary == first

    stream = ChatStream(prompt_text="new message")
    manager._broker.register(chat.chat_id, stream)
    assert manager.queue_message(chat.chat_id, "new message") is True
    assert chat.reentry_summary == ""
    manager._broker.clear(chat.chat_id, stream)

    after_message = _make_manager(tmp_path).get_chat(chat.chat_id)
    assert after_message is not None
    assert after_message.reentry_summary == ""


def test_reentry_summary_humanizes_fenced_json_and_repairs_cached_bullets() -> None:
    generated = """```json
{
  "repo_source": "insights.py",
  "crash_timeline": "checked around crash time",
  "next_step": "review the failing path"
}
```"""

    normalized = _cap_reentry_summary(generated)
    assert normalized == (
        "• Repo source: insights.py\n"
        "• Crash timeline: checked around crash time\n"
        "• Next step: review the failing path"
    )
    assert "```" not in normalized
    assert "{" not in normalized

    cached = "\n".join(f"• {line}" for line in generated.splitlines())
    assert _cap_reentry_summary(cached) == normalized


def test_reentry_summary_drops_unparseable_json_instead_of_bulleting_it() -> None:
    # Apple mirrors the shape of what it is handed and answered with a JSON
    # envelope of its own, cut off mid-object. Every line is structure, so the
    # note has nothing to say and must not render.
    truncated = """{
  "type": "event",
  "event_id": "e5a77d9b",
  "description": {"""

    assert _cap_reentry_summary(truncated) == ""
    # And the same residue already cached from an earlier run stays gone.
    assert _cap_reentry_summary("\n".join(f"• {line}" for line in truncated.splitlines())) == ""


def test_reentry_summary_drops_metadata_only_json_envelope() -> None:
    envelope = '{"type": "event", "event_id": "e5a77d9b", "session": "abc"}'

    assert _cap_reentry_summary(envelope) == ""


def test_reentry_summary_keeps_real_fields_beside_metadata_keys() -> None:
    mixed = '{"type": "event", "next_step": "review the failing path"}'

    assert _cap_reentry_summary(mixed) == "• Next step: review the failing path"


def test_reentry_transcript_text_flattens_records_to_prose() -> None:
    filtered = "\n".join(
        [
            json.dumps(
                {"idx": 0, "type": "user", "content": [{"type": "text", "text": "fix the crash"}]}
            ),
            json.dumps(
                {
                    "idx": 1,
                    "type": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file": "x.py"}},
                        {"type": "text", "text": "Found it in insights.py"},
                    ],
                }
            ),
            "not json at all",
        ]
    )

    assert _reentry_transcript_text(filtered) == (
        "User: fix the crash\nAssistant: Found it in insights.py"
    )
    assert "tool_use" not in _reentry_transcript_text(filtered)


def test_reentry_summary_regenerates_when_cached_value_is_residue(
    tmp_path: Path, monkeypatch,
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Residue", workspace="personal")
    chat = manager.create_chat(project.project_id, title="Residue chat")
    chat.reentry_summary = '• {\n• "type": "event",\n• "event_id": "e5a77d9b",'
    manager._save()

    monkeypatch.setattr(native_sidecar, "apple_model_available", lambda: True)
    monkeypatch.setattr(
        manager._transcripts,
        "current_filtered_jsonl",
        lambda *_args: json.dumps(
            {"type": "user", "content": [{"type": "text", "text": "keep working"}]}
        ),
    )

    seen: list[str] = []

    async def fake_respond(prompt: str, **_kwargs) -> str:
        seen.append(prompt)
        return "Picked up the crash fix"

    monkeypatch.setattr(native_sidecar, "respond", fake_respond)

    assert asyncio.run(manager.generate_reentry_summary(chat.chat_id)) == (
        "• Picked up the crash fix"
    )
    # The model is handed prose, not the JSON records that triggered the echo.
    assert "User: keep working" in seen[0]
    assert '"content"' not in seen[0]
