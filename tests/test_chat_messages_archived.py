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
type: telegram-transcript
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

# Telegram Transcript (claude)

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
