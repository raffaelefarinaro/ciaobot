"""The hidden code-mode continuation never shows as a user row and never shifts turn_index."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ciao.cli_envelopes import CODE_MODE_CONTINUE_PROMPT
from ciao.config import CiaoConfig
from ciao.models import ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_messages
from ciao.web.transcript_service import _render_opencode_thread

CAPSULE = "[CIAO_CONTEXT_BEGIN]\nctx\n[CIAO_CONTEXT_END]\n\n"


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


class _Msg:
    def __init__(self, type_: str, content: str) -> None:
        self.type = type_
        self.message = {"role": type_, "content": [{"type": "text", "text": content}]}


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


async def _drive(pcm: ProjectChatManager, chat_id: str, prompt: str) -> None:
    stream = pcm.start_stream(chat_id, prompt)

    async def consume() -> None:
        async for _ in stream.subscribe():
            pass

    await asyncio.wait_for(asyncio.create_task(consume()), timeout=5.0)


@pytest.mark.asyncio
async def test_hidden_continuation_keeps_code_mode_markers_aligned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = _make_manager(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    project = pcm.create_project("Linked", workspace="work")
    project.repo_path = str(repo)
    chat = pcm.create_chat(project.project_id, title="code-markers")
    cid = chat.chat_id
    calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        calls.append(prompt)
        if prompt == "initial":
            pcm.set_code_mode(cid, True, actor="agent")
            assert pcm.arm_code_mode_continuation(cid) is True
        elif prompt == "second":
            pcm.set_code_mode(cid, False, actor="agent")
        yield ResultEvent(
            type="result",
            result="answer",
            session_id="sess-x",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    await _drive(pcm, cid, "initial")
    assert calls == ["initial", CODE_MODE_CONTINUE_PROMPT]
    await _drive(pcm, cid, "second")
    assert chat.user_turn_count == 2

    chat.session_id = "sess-markers"
    fake_sdk = SimpleNamespace(
        get_session_messages=lambda session_id, directory: [
            _Msg("user", "initial"),
            _Msg("assistant", "a1"),
            _Msg("user", CAPSULE + CODE_MODE_CONTINUE_PROMPT),
            _Msg("assistant", "a2"),
            _Msg("user", "second"),
            _Msg("assistant", "a3"),
        ]
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    response = await chat_messages(_request(pcm, pcm._config, cid))
    messages = json.loads(response.body)

    assert CODE_MODE_CONTINUE_PROMPT not in response.body.decode()
    users = [(m["content"], m["turn_index"]) for m in messages if m["role"] == "user"]
    assert users == [("initial", 0), ("second", 1)]
    assert chat.code_mode_events[0]["after_turn_index"] == 0
    assert chat.code_mode_events[-1]["after_turn_index"] == 1


def test_opencode_render_skips_the_continuation_without_shifting_turn_index(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("OC", workspace="work")
    chat = pcm.create_chat(project.project_id, title="oc-continue")

    def user(text: str) -> dict:
        return {"info": {"role": "user"}, "parts": [{"type": "text", "text": text}]}

    rows = _render_opencode_thread(
        {"info": {}, "messages": [user("a"), user(CAPSULE + CODE_MODE_CONTINUE_PROMPT), user("b")]},
        chat,
    )
    users = [(r["content"], r["turn_index"]) for r in rows if r["role"] == "user"]
    assert users == [("a", 0), ("b", 1)]


def test_archived_transcript_drops_the_continuation_user_row(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    text = f"""---
provider: claude
session_id: sess-archived
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
{CAPSULE}{CODE_MODE_CONTINUE_PROMPT}
```

### Assistant

```text
continued reply
```
"""
    rows = pcm._parse_transcript_messages(text)
    assert [(r["role"], r["content"]) for r in rows] == [("assistant", "continued reply")]
