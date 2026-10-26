"""Tests for archived-chat transcript fallback in /messages."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_messages


_TRANSCRIPT = """---
provider: claude
context: archived fallback test
selected_model: opus
active_model: opus
last_effective_model: opus
session_id: sess-archived
started: 2026-06-19T10:00:00Z
ended: 2026-06-19T10:05:00Z
turn_count: 2
---

# Chat Transcript (claude)

## Turn 1

- Time: 2026-06-19T10:00:00Z
- Input kind: text
- Mode: auto
- Effective model: opus
- Images: 0

### User

```text
[CIAO_CONTEXT_BEGIN]
[Project context: test]
[CIAO_CONTEXT_END]

hello archived chat
```

### Assistant

```text
reply from archived chat
```

## Turn 2

- Time: 2026-06-19T10:05:00Z
- Input kind: text
- Mode: auto
- Effective model: opus
- Images: 0

### User

```text
follow-up question
```

### Assistant

```text
second reply
```
"""


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _request(pcm: ProjectChatManager, config: CiaoConfig, chat_id: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/chats/{chat_id}/messages",
        "headers": [],
        "path_params": {"chat_id": chat_id},
        "app": SimpleNamespace(
            state=SimpleNamespace(project_chat_manager=pcm, config=config),
        ),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_archived_chat_falls_back_to_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the SDK session blob is gone, archived chats render the vault transcript."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Archive fallback", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="archived-fallback")
    chat.session_id = "sess-archived"

    # Write a fake archived transcript in the vault
    chats_dir = tmp_path / "memory-vault" / "Logs" / "Chats" / chat.chat_id
    chats_dir.mkdir(parents=True, exist_ok=True)
    claude_dir = chats_dir / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = claude_dir / "2026-06-19T10-00-00Z-sess-archived.md"
    transcript_path.write_text(_TRANSCRIPT, encoding="utf-8")

    # Mark chat as archived and point it at the transcript
    chat.archived = True
    chat.archive_path = str(transcript_path.relative_to(tmp_path))
    pcm._save()

    # Ensure the SDK import succeeds but the session file is missing.
    fake_sdk = SimpleNamespace(
        get_session_messages=lambda session_id, directory: (_ for _ in ()).throw(FileNotFoundError("gone"))
    )
    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", fake_sdk)

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    payload = response.body.decode()

    assert '"content":"hello archived chat"' in payload
    assert '"content":"reply from archived chat"' in payload
    assert '"content":"follow-up question"' in payload
    assert '"content":"second reply"' in payload
    # Context block is stripped
    assert "[CIAO_CONTEXT_BEGIN]" not in payload


@pytest.mark.asyncio
async def test_archived_chat_returns_handover_when_transcript_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the transcript file is missing too, we still return handover messages."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Archive missing", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="missing-transcript")
    chat.session_id = "sess-missing"
    chat.archived = True
    chat.archive_path = "memory-vault/Logs/Chats/nope.md"
    chat.handover_messages = [{"role": "user", "content": "seed"}]
    pcm._save()

    fake_sdk = SimpleNamespace(
        get_session_messages=lambda session_id, directory: (_ for _ in ()).throw(FileNotFoundError("gone"))
    )
    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", fake_sdk)

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    payload = response.body.decode()

    assert '"content":"seed"' in payload


@pytest.mark.asyncio
async def test_archived_chat_falls_back_when_sdk_session_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty SDK session must not hide the durable archived transcript."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Archive empty SDK", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="empty-sdk-session")
    chat.session_id = "sess-empty"

    chats_dir = tmp_path / "memory-vault" / "Logs" / "Chats" / chat.chat_id
    claude_dir = chats_dir / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = claude_dir / "2026-06-19T10-00-00Z-sess-empty.md"
    transcript_path.write_text(_TRANSCRIPT, encoding="utf-8")
    chat.archived = True
    chat.archive_path = str(transcript_path.relative_to(tmp_path))
    pcm._save()

    fake_sdk = SimpleNamespace(get_session_messages=lambda session_id, directory: [])
    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", fake_sdk)

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    payload = response.body.decode()

    assert '"content":"hello archived chat"' in payload
    assert '"content":"reply from archived chat"' in payload


_CODEX_TRANSCRIPT = """---
provider: codex
context: archived fallback test
selected_model: gpt-test
active_model: gpt-test
last_effective_model: gpt-test
session_id: thread-archived
started: 2026-06-19T10:00:00Z
ended: 2026-06-19T10:05:00Z
turn_count: 1
---

# Chat Transcript (codex)

## Turn 1

- Time: 2026-06-19T10:00:00Z
- Input kind: text
- Mode: auto
- Effective model: gpt-test
- Images: 0

### User

```text
hello archived codex
```

### Assistant

```text
reply from archived codex
```
"""


@pytest.mark.asyncio
async def test_archived_legacy_provider_chat_falls_back_to_transcript(
    tmp_path: Path
) -> None:
    """Archived chats from a removed provider render the durable transcript."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Codex archive fallback", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="codex-archived-fallback")
    chat.provider = "codex"
    chat.session_id = "thread-archived"

    chats_dir = tmp_path / "memory-vault" / "Logs" / "Chats" / chat.chat_id / "codex"
    chats_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = chats_dir / "2026-06-19T10-00-00Z-thread-archived.md"
    transcript_path.write_text(_CODEX_TRANSCRIPT, encoding="utf-8")

    chat.archived = True
    chat.archive_path = str(transcript_path.relative_to(tmp_path))
    pcm._save()

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    payload = response.body.decode()

    assert '"content":"hello archived codex"' in payload
    assert '"content":"reply from archived codex"' in payload


def _turn(n: int, user: str, reply: str, *, model: str = "opus", usage: str = "") -> str:
    return (
        f"\n## Turn {n}\n\n- Time: 2026-06-19T10:{n % 60:02d}:00Z\n"
        f"- Input kind: text\n- Mode: auto\n- Effective model: {model}\n- Images: 0\n\n"
        f"### User\n\n```text\n{user}\n```\n\n### Assistant\n\n```text\n{reply}\n```\n"
        f"{usage}"
    )


_HEADER = "---\nprovider: claude\nsession_id: sess-archived\n---\n\n# Chat Transcript (claude)\n"


async def _archived_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    **chat_fields,
) -> list[dict]:
    import json

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Archive meta", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="archived-meta")
    chat.session_id = "sess-archived"
    path = tmp_path / "memory-vault" / "Logs" / "Chats" / chat.chat_id / "claude" / "a.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HEADER + body, encoding="utf-8")
    chat.archived = True
    chat.archive_path = str(path.relative_to(tmp_path))
    for key, value in chat_fields.items():
        setattr(chat, key, value)
    pcm._save()
    fake_sdk = SimpleNamespace(
        get_session_messages=lambda session_id, directory: (_ for _ in ()).throw(FileNotFoundError("gone"))
    )
    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", fake_sdk)
    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    payload = json.loads(response.body.decode())
    return payload["messages"] if isinstance(payload, dict) else payload


@pytest.mark.asyncio
async def test_archived_fallback_preserves_usage_model_and_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = "\n### Usage\n\n- input_tokens: 14\n- output_tokens: 0\n"
    body = _turn(1, "q1", "r1", usage=usage) + _turn(2, "q2", "r2")
    rows = await _archived_rows(
        tmp_path,
        monkeypatch,
        body,
        user_turn_count=2,
        user_turn_timings={"0": {"completed_at": "2026-06-19T10:00:04Z", "duration_ms": 4000}},
    )
    users = [r for r in rows if r["role"] == "user"]
    assistants = [r for r in rows if r["role"] == "assistant"]
    assert [u["turn_index"] for u in users] == [0, 1]
    assert assistants[0]["usage"] == {"input_tokens": "14", "output_tokens": "0"}
    assert assistants[0]["effective_model"] == "opus"
    assert assistants[0]["duration_ms"] == 4000


@pytest.mark.asyncio
async def test_archived_display_is_not_truncated_to_handover_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "".join(_turn(n, f"question {n}", f"answer {n}") for n in range(1, 91))
    rows = await _archived_rows(tmp_path, monkeypatch, body, user_turn_count=90)
    assert len(rows) == 180
    assert rows[0]["content"] == "question 1"


@pytest.mark.asyncio
async def test_hidden_archive_turns_do_not_shift_timing_indices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli_envelopes import RELOAD_CONTINUE_PROMPT

    body = (
        _turn(1, "first", "r1")
        + _turn(2, RELOAD_CONTINUE_PROMPT, "hidden reply")
        + _turn(3, "second", "r2")
    )
    rows = await _archived_rows(
        tmp_path,
        monkeypatch,
        body,
        user_turn_count=2,
        user_turn_timings={"1": {"completed_at": "2026-06-19T10:03:09Z", "duration_ms": 9000}},
    )
    second = next(i for i, r in enumerate(rows) if r.get("content") == "second")
    assert rows[second]["turn_index"] == 1
    assert rows[second + 1]["duration_ms"] == 9000


@pytest.mark.asyncio
async def test_steered_index_is_skipped_in_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _turn(1, "a", "ra") + _turn(2, "b", "rb")
    rows = await _archived_rows(
        tmp_path,
        monkeypatch,
        body,
        user_turn_count=3,
        user_turn_steered={"1": True},
    )
    assert [r["turn_index"] for r in rows if r["role"] == "user"] == [0, 2]


@pytest.mark.asyncio
async def test_archive_rows_from_a_previous_provider_stint_get_no_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _turn(1, "a", "ra") + _turn(2, "b", "rb") + _turn(3, "c", "rc")
    rows = await _archived_rows(
        tmp_path,
        monkeypatch,
        body,
        user_turn_count=3,
        turn_index_base=2,
    )
    users = [r for r in rows if r["role"] == "user"]
    assert ["turn_index" in r for r in users] == [False, False, True]
    assert users[-1]["turn_index"] == 2


@pytest.mark.asyncio
async def test_archive_without_historical_metadata_does_not_fabricate_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = await _archived_rows(
        tmp_path, monkeypatch, _turn(1, "q", "r", model="-"), user_turn_count=1
    )
    assistant = next(r for r in rows if r["role"] == "assistant")
    for key in ("usage", "effective_model", "duration_ms"):
        assert key not in assistant
