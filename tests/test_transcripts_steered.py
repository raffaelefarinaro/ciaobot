"""A message steered into a running Claude turn survives a transcript reload.

The CLI records a mid-turn steer as a ``queued_command`` attachment, not as a
``user`` record, so the stock visibility filter drops it and the steered bubble
would vanish after a reload.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_queued_command_attachment_is_a_user_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_agent_sdk._internal.sessions import project_key_for_directory

    from ciao.transcripts import get_session_messages_full

    session_id = "22222222-3333-4444-5555-666666666666"
    key = project_key_for_directory(str(tmp_path))
    project_dir = tmp_path / ".claude" / "projects" / key
    project_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "claude_agent_sdk._internal.sessions._get_projects_dir",
        lambda: tmp_path / ".claude" / "projects",
    )

    entries = [
        {
            "type": "user", "uuid": "u1", "parentUuid": None, "sessionId": session_id,
            "message": {"role": "user", "content": "Run sleep 20 && echo A"},
        },
        {
            "type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": session_id,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": "sleep 20 && echo A"}},
            ]},
        },
        {"type": "queue-operation", "operation": "enqueue", "sessionId": session_id,
         "content": "Also end with PINEAPPLE."},
        {
            "type": "user", "uuid": "u2", "parentUuid": "a1", "sessionId": session_id,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "A"},
            ]},
            "toolUseResult": {"stdout": "A"},
        },
        {
            "type": "attachment", "uuid": "q1", "parentUuid": "u2", "sessionId": session_id,
            "timestamp": "2026-10-15T10:00:00.000Z",
            "attachment": {
                "type": "queued_command", "prompt": "Also end with PINEAPPLE.",
                "source_uuid": "steer-1", "commandMode": "prompt",
            },
        },
        {"type": "queue-operation", "operation": "remove", "sessionId": session_id,
         "reason": "absorbed_mid_turn", "commandUuid": "steer-1"},
        {
            "type": "assistant", "uuid": "a2", "parentUuid": "q1", "sessionId": session_id,
            "message": {"role": "assistant", "content": "A\n\nPINEAPPLE"},
        },
    ]
    (project_dir / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )

    messages = get_session_messages_full(session_id, directory=str(tmp_path))

    assert [m.type for m in messages] == ["user", "assistant", "user", "user", "assistant"]
    assert messages[3].message["content"] == "Also end with PINEAPPLE."
    assert messages[3].uuid == "q1"
