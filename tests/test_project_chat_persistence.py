from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

from ciao import entity_types
from ciao.config import CiaoConfig
from ciao.models import ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import (
    AgentSurfaceUnavailableError,
    ProjectChatManager,
    _StreamOutcome,
)

from tests.conftest import attach_stub_mcp


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    manager = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    # The MCP control plane is mandatory for a turn, so every manager that
    # builds one needs a service; the one test that asserts the missing-server
    # failure clears it explicitly.
    return attach_stub_mcp(manager)


def _persisted_chats(tmp_path: Path) -> dict[str, dict]:
    payload = json.loads(
        (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    )
    assert payload["revision"] > 0
    return payload["chats"]


def _seeded_welcome(manager: ProjectChatManager, title: str) -> tuple[str, str]:
    """The seeded welcome's own prompt and its visible first message, by title."""
    chat = next(chat for chat in manager._chats.values() if chat.title == title)
    prompt, welcome = chat.handover_messages[0], chat.handover_messages[1]
    return str(prompt["content"]), str(welcome["content"])


def _write_registry(root: Path, overrides: str) -> None:
    """The `<vault>/entity-types.yaml` the Categories API reads, plus a cache reset.

    The cache is process-wide, so a registry written here has to be visible to
    the manager built next; a stale entry would answer from stock and the test
    would pass for the wrong reason.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / entity_types.VAULT_FILENAME).write_text(overrides, encoding="utf-8")
    entity_types.clear_entity_types_cache()


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


def test_onboarding_names_effective_enabled_non_hidden_categories(
    tmp_path: Path,
) -> None:
    """The welcome names the categories the Categories page would list (#979).

    Read through the registry the owner edits — the AGENT vault root — which on
    an install that has not re-rooted is a different directory from the notes
    root the welcome names. Reading the notes root would report the shipped list
    while the owner's own page shows their edits, so both files are seeded here
    with different labels and the welcome has to pick the right one.
    """
    registry_root = tmp_path / "memory-vault"          # config.agent_vault_root("personal")
    notes_root = registry_root / "personal"            # config.workspace_vault_root("personal")
    _write_registry(
        registry_root,
        "- id: person\n"
        "  label: Human\n"
        "- id: idea\n"
        "  enabled: false\n"
        "- id: client\n"
        "  label: Client account\n"
        "  kind: entity\n"
        "  folder: Clients\n"
        "  description: An account the user works with.\n"
        "  aliases: []\n"
        "  stale_after_days: 0\n"
        "  enabled: true\n",
    )
    _write_registry(
        notes_root,
        "- id: notebook\n"
        "  label: Notes root only\n"
        "  kind: note\n"
        "  folder: Notebook\n"
        "  aliases: []\n"
        "  stale_after_days: 0\n"
        "  enabled: true\n",
    )

    manager = _make_manager(tmp_path)
    _, welcome = _seeded_welcome(manager, "Welcome to Ciaobot! 👋")

    # A renamed builtin and a custom one, in registry order.
    assert "Human, Project, Place, Resource, Journal, Note, Client account" in welcome
    # A category the owner turned off is not promoted into a first impression…
    assert "Idea" not in welcome
    # …and neither is one the agent writes itself (`hidden`).
    assert "Skill proposal" not in welcome
    assert "Workspace," not in welcome
    # The source is this workspace's own registry, not a list seeded beside it.
    assert "Notes root only" not in welcome
    # And naming them created none: seeding writes nothing into the vault.
    assert not (registry_root / "People").exists()
    assert not (registry_root / "Clients").exists()
    entity_types.clear_entity_types_cache()


def test_both_welcome_shapes_explain_memory_and_link_existing_categories(
    tmp_path: Path, monkeypatch
) -> None:
    """Scratch and existing-vault onboarding both teach the two memory layers."""
    monkeypatch.setenv("CIAO_VAULT_MODE", "existing")
    existing = _make_manager(tmp_path / "existing")
    monkeypatch.setenv("CIAO_VAULT_MODE", "scratch")
    scratch = _make_manager(tmp_path / "scratch")

    for manager, title in (
        (existing, "Connect Existing Vault 👋"),
        (scratch, "Welcome to Ciaobot! 👋"),
    ):
        _, welcome = _seeded_welcome(manager, title)

        # The two layers, and the one editor that changes the second one.
        assert "**Profile & preferences**" in welcome
        assert "**Notes, by category**" in welcome
        assert "[See or customize memory categories](/memory/categories)" in welcome
        # A category is a filing rule for future notes, not a standing order to
        # remember everything.
        assert "**future**" in welcome
        # What the memory pass really does, and where a person reviews it.
        assert "proposal" in welcome
        assert "**Memory · To decide**" in welcome
        assert "**Memory · History**" in welcome
        # Undo is offered where the receipt is, and only as long as the note is
        # still as the pass left it — a note edited since is refused.
        assert "undo it while the note is still as the pass left it" in welcome
        # No promise that past conversations are read, imported or filed.
        assert "import" not in welcome.lower()
        # The interview still opens the chat, and memory comes before it.
        assert "What is your name" in welcome
        assert welcome.index("memory is organized") < welcome.index("What is your name")

        # The agent is told to explain it early, once, without a second round of
        # questions — and never to go looking for conversations to file.
        prompt, _ = _seeded_welcome(manager, title)
        assert "**Explain memory early**" in prompt
        assert "Do not run a second interview round about categories" in prompt
        assert "do not scan past chats" in prompt
        assert "Ask the user 2-3 important questions" in prompt

    # The existing-vault preservation and routing instructions are untouched.
    existing_prompt, _ = _seeded_welcome(existing, "Connect Existing Vault 👋")
    assert "Preserve before reorganizing" in existing_prompt
    assert "Never delete or overwrite them" in existing_prompt
    assert "Workspace/Memory-Proposals.md" in existing_prompt
    scratch_prompt, _ = _seeded_welcome(scratch, "Welcome to Ciaobot! 👋")
    assert "Onboarding interview and curation" in scratch_prompt
    assert "Do not create `personal/`, `work/`, or `Templates/`" in scratch_prompt


def test_custom_category_label_is_plain_text_in_welcome(tmp_path: Path) -> None:
    """A label is user-typed text: it is named, never rendered as markup (#979).

    The welcome is Markdown, so a category label carrying a link or a tag would
    otherwise become one — and the one link the message offers would not be the
    only one in it.
    """
    _write_registry(
        tmp_path / "memory-vault",
        "- id: sneaky\n"
        "  label: Read [more](https://evil.example/steal) <img src=x onerror=alert(1)>\n"
        "  kind: note\n"
        "  folder: Sneaky\n"
        "  aliases: []\n"
        "  stale_after_days: 0\n"
        "  enabled: true\n",
    )

    manager = _make_manager(tmp_path)
    _, welcome = _seeded_welcome(manager, "Welcome to Ciaobot! 👋")

    # The Categories link is still the one real link in the message.
    assert welcome.count("](/memory/categories)") == 1
    # The label's own text survives, escaped rather than dropped…
    assert "Read \\[more\\]" in welcome
    assert "onerror" in welcome
    # …and it is not a link target, a tag, or anything a click would follow.
    assert "](https://evil.example/steal)" not in welcome
    assert "](https" not in welcome
    # The tag is readable text (an escaped `<`), never markup a renderer sees.
    assert "\\<img src\\=x onerror\\=alert\\(1\\)\\>" in welcome
    assert "<img src=x onerror=alert(1)>" not in welcome
    entity_types.clear_entity_types_cache()


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


def test_proposal_helper_metadata_persists_and_new_session_clears_it(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Reviews", workspace="work")
    helper = {
        "kind": "proposal",
        "intent": "resolve",
        "proposal_ids": ["proposal-1"],
        "archive_policy": "when_resolved",
    }
    chat = manager.create_chat(project.project_id, title="Merge fact", helper=helper)

    restored = _make_manager(tmp_path).get_chat(chat.chat_id)
    assert restored is not None
    assert restored.helper == helper
    assert restored.to_dict()["helper"] == helper

    manager.new_session(chat.chat_id)
    assert manager.get_chat(chat.chat_id).helper == {}


def test_resolution_helper_archives_only_after_target_leaves_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Reviews", workspace="work")
    chat = manager.create_chat(
        project.project_id,
        helper={
            "kind": "proposal",
            "intent": "resolve",
            "proposal_ids": ["proposal-1"],
            "archive_policy": "when_resolved",
        },
    )
    chat.last_response = "Merged the fact."
    chat.last_response_status = "success"
    monkeypatch.setattr(
        "ciao.proposal_tracking.pending_proposal_ids", lambda _config: {"proposal-1"}
    )
    assert asyncio.run(manager._maybe_archive_proposal_helper(chat.chat_id)) is False

    monkeypatch.setattr(
        "ciao.proposal_tracking.pending_proposal_ids", lambda _config: set()
    )

    async def archive(chat_id: str):
        manager.get_chat(chat_id).archived = True
        return None

    archive_mock = AsyncMock(side_effect=archive)
    monkeypatch.setattr(manager, "archive_chat", archive_mock)
    assert asyncio.run(manager._maybe_archive_proposal_helper(chat.chat_id)) is True
    archive_mock.assert_awaited_once_with(chat.chat_id)


def test_review_helper_never_auto_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Reviews", workspace="work")
    chat = manager.create_chat(
        project.project_id,
        helper={
            "kind": "proposal",
            "intent": "review",
            "proposal_ids": ["proposal-1"],
            "archive_policy": "manual",
        },
    )
    chat.last_response = "Here is my recommendation."
    chat.last_response_status = "success"
    archive_mock = AsyncMock()
    monkeypatch.setattr(manager, "archive_chat", archive_mock)

    assert asyncio.run(manager._maybe_archive_proposal_helper(chat.chat_id)) is False
    archive_mock.assert_not_awaited()


def test_build_agent_request_fails_without_an_agent_surface(tmp_path: Path) -> None:
    from ciao.agent_surface import AGENT_TOKEN_ENV

    # The Ciaobot agent surface is the only control surface: with no service
    # there is nothing to degrade to, so the turn must fail loudly instead of
    # dispatching an agent that cannot reach Ciaobot.
    manager = _make_manager(tmp_path)
    manager._mcp_service = None
    project = manager.create_project("Fallback", workspace="work")
    chat = manager.create_chat(project.project_id)

    with pytest.raises(AgentSurfaceUnavailableError):
        manager.build_agent_request(chat, prompt="hi")

    transcript_request = manager.build_agent_request(
        chat, prompt="hi", require_mcp=False
    )
    assert AGENT_TOKEN_ENV not in transcript_request.extra_env


def test_build_agent_request_attaches_the_agent_credentials(tmp_path: Path) -> None:
    from ciao.agent_surface import AGENT_TOKEN_ENV, AGENT_URL_ENV

    manager = _make_manager(tmp_path)
    project = manager.create_project("Attached", workspace="work")
    chat = manager.create_chat(project.project_id)

    request = manager.build_agent_request(chat, prompt="hi")
    # Every chat is CLI since S6: the token+URL reach the foreground shell so
    # `ciao <noun> <verb>` can call the control plane.
    assert request.extra_env[AGENT_URL_ENV] == "http://127.0.0.1:8443/agent/v1/"
    assert request.extra_env[AGENT_TOKEN_ENV] == "tok-test"
    assert request.control_token == "tok-test"


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
