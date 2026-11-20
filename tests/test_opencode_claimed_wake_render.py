"""A claimed wake renders as started, the notice card, then resumed: each once, live and after reload.

The fixture is a sanitised copy of a real OpenCode session journal (the chat and call ids replaced,
the turn-0 prompt trimmed). It also carries the record the claim now writes at the continuation's
first text: the notice, with ``turn_origin``, before the continuation's reply.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ciao.web.transcript_service import _render_acp_journal

FIXTURE = Path(__file__).parent / "fixtures" / "acp" / "opencode_claimed_wake_journal.jsonl"


def _render() -> list[dict]:
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    chat = SimpleNamespace(
        user_turn_origin={}, user_turn_images={}, user_turn_unattended={}, user_turn_sent_at={},
        user_turn_timings={}, user_turn_meta={}, user_turn_usage={}, user_turn_quota={},
        user_turn_model={}, user_turn_mode={}, user_turn_effort={}, turn_index_base=0,
        title="t", model="m", provider="opencode", session_id="", mode="bypass", user_turn_count=1,
    )
    return _render_acp_journal(records, chat, start_user_idx=0, workspace_root=Path("/tmp"), agent_root=Path("/tmp"))


def test_a_claimed_wake_renders_started_then_the_card_then_resumed_each_once() -> None:
    rows = _render()
    assert [row["role"] for row in rows] == ["user", "system", "assistant", "system", "assistant"]
    assert rows[0]["content"].startswith("Wake test 2")
    assert rows[1]["content"] == "sleep 25; echo shell-done-marker"
    assert rows[2]["content"] == "started"
    assert rows[3]["origin"] == "background_run"
    assert rows[3]["content"].startswith("[Ciaobot] 1 background run finished.")
    assert rows[4]["content"] == "resumed"


def test_the_first_reply_keeps_its_own_text_and_the_card_is_not_a_user_turn() -> None:
    rows = _render()
    card = next(row for row in rows if row.get("origin") == "background_run")
    assert "exit status unknown" in card["content"]
    assert "turn_index" not in card  # a wake is not a turn the person sent
    assert [row["content"] for row in rows if row["role"] == "assistant"] == ["started", "resumed"]
