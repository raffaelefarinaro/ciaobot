"""The injected subagent-synthesis nudge must not read as a user message.

The server pokes the parent turn with `SUBAGENT_SYNTHESIS_NUDGE` when its
background agents finish (ciao/web/project_chats.py). The CLI writes that poke
into the session JSONL as an ordinary user record, so /messages has to collapse
it into a status line — otherwise the transcript shows a paragraph the user
never typed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.subagent_tracking import SUBAGENT_SYNTHESIS_NUDGE
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_messages


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


@pytest.mark.asyncio
async def test_synthesis_nudge_renders_as_system_line_and_keeps_turn_indexes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Synthesis nudge", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="nudge-render-test")
    chat.session_id = "sess-nudge-render"
    # Images belong to the second *human* turn; the nudge in between must not
    # claim the bucket.
    chat.user_turn_images = {"1": ["second.png"]}

    fake_sdk = SimpleNamespace(
        get_session_messages=lambda session_id, directory: [
            _Msg("user", "kick off the audit"),
            _Msg("assistant", "Dispatched two agents. I'll report back."),
            _Msg("user", f'[Chat ID: "{chat.chat_id}"]\n\n{SUBAGENT_SYNTHESIS_NUDGE}'),
            _Msg("assistant", "Here is the consolidated report."),
            _Msg("user", "thanks, now do the other half"),
        ]
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    messages = json.loads(response.body)

    # The prompt text itself never reaches the transcript...
    assert "post your consolidated final report" not in response.body.decode()
    # ...it is replaced by a one-line system status entry, in place.
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "kick off the audit"),
        ("assistant", "Dispatched two agents. I'll report back."),
        ("system", "\U0001F916 Background agents finished — asked for a consolidated report"),
        ("assistant", "Here is the consolidated report."),
        ("user", "thanks, now do the other half"),
    ]
    # The nudge did not consume a user-turn slot: the second human turn keeps
    # index 1 and its image bucket.
    assert messages[0]["turn_index"] == 0
    assert messages[4]["turn_index"] == 1
    assert messages[4]["images"] == ["second.png"]
    assert "turn_index" not in messages[2]
