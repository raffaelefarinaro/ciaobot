"""Loop/schedule ticks must be distinguishable from turns the user typed.

Before this, a loop's prompt landed as an ordinary user bubble: the reader
could not tell it apart from their own message, and neither could the model,
which replied "even though you're actively messaging me" to its own loop
prompt.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import _extract_assistant_blocks, _failed_tool_use_ids


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


def test_unattended_prefix_tells_the_model_nobody_is_watching(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("loops", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")

    interactive = pcm._build_prompt_prefix(chat)
    assert "[Unattended run:" not in interactive

    tick = pcm._build_prompt_prefix(chat, unattended=True)
    assert "[Unattended run:" in tick
    # Must land inside the block the history renderer strips, so the marker
    # never shows up in the transcript the user reads.
    assert tick.index("[Unattended run:") < tick.index("[CIAO_CONTEXT_END]")


def test_unattended_turn_runs_in_bypass(tmp_path: Path) -> None:
    """Nobody can answer an approval prompt on a loop tick.

    Every escalating mode resolves to an unanswerable card that `_drive`
    auto-denies, so the automation fails while reporting success.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("loops", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")

    chat.mode = "auto"
    assert pcm._effective_mode_for_chat(chat) == "auto"
    assert pcm._effective_mode_for_chat(chat, unattended=True) == "bypass"

    # `plan` cannot escalate — it only proposes — so forcing bypass would turn
    # a read-only tick into a writing one.
    chat.mode = "plan"
    assert pcm._effective_mode_for_chat(chat, unattended=True) == "plan"


def test_build_agent_request_forwards_unattended_to_the_mode(tmp_path: Path) -> None:
    """The flag has to survive the trip, or the bypass above is dead code."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("loops", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")
    chat.mode = "auto"

    interactive = pcm.build_agent_request(chat, prompt="hi")
    assert interactive.mode == "auto"

    tick = pcm.build_agent_request(chat, prompt="hi", unattended=True)
    assert tick.mode == "bypass"


def test_unattended_turn_flag_is_persisted_and_reloaded(tmp_path: Path) -> None:
    """The ↻ marker has to survive a reload: /messages reads the flag back
    because the SDK session file records no sender for a turn."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("loops", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")
    chat.user_turn_unattended["3"] = True
    pcm._save()

    stored = json.loads((tmp_path / ".runtime" / "web_projects.json").read_text())
    assert stored["chats"][chat.chat_id]["user_turn_unattended"] == {"3": True}

    # Re-hydrate through the same loader the server uses at startup.
    pcm._chats.clear()
    pcm._load()
    reloaded = pcm.get_chat(chat.chat_id)
    assert reloaded is not None
    assert reloaded.user_turn_unattended == {"3": True}
    # Absent key = interactive, so pre-feature chats are unaffected.
    assert reloaded.user_turn_unattended.get("0") is None


def test_failed_tool_calls_are_collected_from_their_results() -> None:
    class _Msg:
        def __init__(self, type_: str, message: dict) -> None:
            self.type = type_
            self.message = message

    msgs = [
        _Msg("assistant", {"content": [
            {"type": "tool_use", "id": "toolu-denied", "name": "Write",
             "input": {"file_path": "snapshot.md", "content": "x"}},
        ]}),
        _Msg("user", {"content": [
            {"type": "tool_result", "tool_use_id": "toolu-denied", "is_error": True,
             "content": "Scheduled runs cannot wait for interactive approval."},
        ]}),
        _Msg("assistant", {"content": [
            {"type": "tool_use", "id": "toolu-ok", "name": "Write",
             "input": {"file_path": "real.md", "content": "y"}},
        ]}),
        _Msg("user", {"content": [
            {"type": "tool_result", "tool_use_id": "toolu-ok", "content": "ok"},
        ]}),
    ]

    assert _failed_tool_use_ids(msgs) == {"toolu-denied"}
    # Raw JSONL dicts flow through the subagent renderer, so both shapes work.
    dict_msgs = [{"type": m.type, "message": m.message} for m in msgs]
    assert _failed_tool_use_ids(dict_msgs) == {"toolu-denied"}


def test_tool_use_blocks_keep_their_id_for_result_matching() -> None:
    blocks = _extract_assistant_blocks({"content": [
        {"type": "tool_use", "id": "toolu-1", "name": "Write",
         "input": {"file_path": "notes.md", "content": "hi"}},
    ]})
    assert blocks[0]["id"] == "toolu-1"
    assert blocks[0]["file_touch"]["file_path"] == "notes.md"


def test_in_place_container_mutations_reach_disk(tmp_path: Path) -> None:
    """Regression: the save baseline used to hold the live container objects,
    so `_merge_local_map`'s field diff saw `before == value` for every in-place
    mutation and dropped it. Per-turn send times, image refs, and the
    unattended flag silently never persisted when they were the only change."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("persist", workspace="personal")
    chat = pcm.create_chat(project.project_id)
    state_file = tmp_path / ".runtime" / "web_projects.json"

    chat.user_turn_timings["0"] = {"sent_at": "2026-07-28T10:00:00Z"}
    chat.user_turn_images["0"] = ["shot.png"]
    chat.user_turn_unattended["0"] = True
    pcm._save()

    record = json.loads(state_file.read_text())["chats"][chat.chat_id]
    assert record["user_turn_timings"] == {"0": {"sent_at": "2026-07-28T10:00:00Z"}}
    assert record["user_turn_images"] == {"0": ["shot.png"]}
    assert record["user_turn_unattended"] == {"0": True}

    # Nested mutation too: the turn-end path writes completed_at into the
    # existing sub-dict rather than replacing it.
    chat.user_turn_timings["0"]["duration_ms"] = 4200
    pcm._save()
    record = json.loads(state_file.read_text())["chats"][chat.chat_id]
    assert record["user_turn_timings"]["0"]["duration_ms"] == 4200
