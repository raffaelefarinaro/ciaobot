"""Vault answer check: detection, prompt bounds, verdict validation, delivery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ciao import vault_verification as vv
from ciao.models import ResultEvent, ToolUseEvent
from ciao.web import transcript_service
from tests.test_project_chats_queue import _make_manager, _wait_for


def _verdict(claims: list[dict]) -> str:
    return json.dumps({"claims": claims})


def test_vault_read_path_keeps_markdown_inside_the_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "People").mkdir(parents=True)

    note = vault / "People" / "a.md"
    assert vv.vault_read_path(
        "Read", str(note), vault_root=vault, agent_root=tmp_path
    ) == note.resolve()
    assert vv.vault_read_path(
        "read", "vault/x.md", vault_root=vault, agent_root=tmp_path
    ) == (vault / "x.md").resolve()
    assert vv.vault_read_path(
        "Read", str(tmp_path / "other.md"), vault_root=vault, agent_root=tmp_path
    ) is None
    assert vv.vault_read_path(
        "Read", str(vault / "img.png"), vault_root=vault, agent_root=tmp_path
    ) is None
    assert vv.vault_read_path(
        "Bash", str(note), vault_root=vault, agent_root=tmp_path
    ) is None
    assert vv.vault_read_path(
        "Read", str(vault / ".." / "escape.md"), vault_root=vault, agent_root=tmp_path
    ) is None


def test_parse_verification_validates_and_counts() -> None:
    text = "Here you go:\n```json\n" + _verdict([
        {"text": "Ann lives in Rome.", "status": "supported", "source": "People/ann.md"},
        {"text": "Ann owns a boat.", "status": "unsupported", "source": "People/ghost.md"},
        {"text": "Rome is in Italy.", "status": "general", "source": "People/ann.md"},
    ]) + "\n```"

    result = vv.parse_verification(text, {"People/ann.md"})

    assert result is not None
    assert (result["supported"], result["unsupported"], result["general"]) == (1, 1, 1)
    assert [c["source"] for c in result["claims"]] == ["People/ann.md", "", ""]
    assert result["sources"] == ["People/ann.md"]


def test_parse_verification_rejects_garbage() -> None:
    bad = [
        "no json",
        "{}",
        _verdict([{"text": "", "status": "supported"}]),
        _verdict([{"text": "x", "status": "maybe"}]),
    ]
    for text in bad:
        assert vv.parse_verification(text, {"People/ann.md"}) is None, text


def test_parse_verification_caps_claims() -> None:
    claims = [
        {"text": f"Claim {i}.", "status": "general", "source": ""}
        for i in range(vv.MAX_CLAIMS + 1)
    ]
    result = vv.parse_verification(_verdict(claims), set())
    assert result is not None
    assert len(result["claims"]) == vv.MAX_CLAIMS


async def test_verify_answer_caps_notes_and_reads_disk(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault" / "People"
    vault.mkdir(parents=True)
    paths = []
    for i in range(7):
        note = vault / f"n{i}.md"
        note.write_text("x" * 7000, encoding="utf-8")
        paths.append(note)

    prompts: list[str] = []

    async def fake_run_oneshot(prompt, **_kwargs):
        prompts.append(prompt)
        return _verdict([
            {"text": "Ann lives in Rome.", "status": "supported", "source": "People/n0.md"}
        ])

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_run_oneshot)

    result = await vv.verify_answer(
        "Ann lives in Rome.",
        paths,
        vault_root=tmp_path / "vault",
        model="haiku",
        provider="claude",
        cwd=tmp_path,
    )

    assert result is not None
    assert result["supported"] == 1
    assert len(prompts) == 1
    prompt = prompts[0]
    assert prompt.count("### ") == vv.MAX_NOTES
    assert "x" * vv.MAX_NOTE_CHARS in prompt
    assert "x" * (vv.MAX_NOTE_CHARS + 1) not in prompt


async def test_verify_answer_failure_returns_none(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "a.md"
    note.write_text("Ann lives in Rome.", encoding="utf-8")

    async def timing_out(prompt, **_kwargs):
        raise TimeoutError("slow model")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", timing_out)
    assert await vv.verify_answer(
        "Ann lives in Rome.", [note], vault_root=vault,
        model="haiku", provider="claude", cwd=tmp_path,
    ) is None

    calls: list[str] = []

    async def recording(prompt, **_kwargs):
        calls.append(prompt)
        return ""

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", recording)
    assert await vv.verify_answer(
        "Ann lives in Rome.", [vault / "missing.md"], vault_root=vault,
        model="haiku", provider="claude", cwd=tmp_path,
    ) is None
    assert calls == []


# ── Drive loop: detection, scheduling, storage, publish ──


_VERDICT = {
    "supported": 1,
    "unsupported": 0,
    "general": 0,
    "claims": [
        {"text": "Ann lives in Rome.", "status": "supported", "source": "People/ann.md"}
    ],
    "sources": ["People/ann.md"],
}


def _result(text: str, *, is_error: bool = False) -> ResultEvent:
    return ResultEvent(
        type="result",
        result=text,
        session_id="sess-v",
        is_error=is_error,
        effective_model="",
        usage={},
        quota={},
        cost_usd=0.0,
    )


def _read(path: Path) -> ToolUseEvent:
    return ToolUseEvent(type="assistant", tool_name="Read", tool_input=str(path))


async def _drive_turn(pcm, chat_id: str, events: list) -> list[dict]:
    """Run one turn whose provider yields `events`; return what the hub saw."""
    published: list[dict] = []
    real_publish = pcm._events.publish

    def spy(payload):
        published.append(payload)
        return real_publish(payload)

    pcm._events.publish = spy  # type: ignore[method-assign]

    async def fake_stream_chat(_chat_id, _prompt, images=None, **_kwargs):
        for event in events:
            yield event

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    async def consume(stream) -> None:
        async for _ev in stream.subscribe():
            pass

    stream = pcm.start_stream(chat_id, "where does ann live?")
    await asyncio.wait_for(asyncio.create_task(consume(stream)), timeout=5.0)
    return published


def _ann_note(pcm) -> Path:
    note = Path(pcm._config.workspace_vault_root("work")) / "People" / "ann.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("Ann lives in Rome.", encoding="utf-8")
    return note


async def test_turn_that_read_a_vault_note_is_checked_and_published(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("vault-check", workspace="work")
    chat = pcm.create_chat(project.project_id, title="verify-test")
    note = _ann_note(pcm)

    calls: list[tuple[str, list[Path]]] = []

    async def fake_verify(answer, note_paths, **_kwargs):
        calls.append((answer, list(note_paths)))
        return _VERDICT

    monkeypatch.setattr("ciao.vault_verification.verify_answer", fake_verify)

    published = await _drive_turn(
        pcm, chat.chat_id, [_read(note), _result("Ann lives in Rome.")]
    )

    await _wait_for(lambda: bool(calls), timeout=2.0)
    assert calls == [("Ann lives in Rome.", [note.resolve()])]
    await _wait_for(lambda: "0" in pcm._chats[chat.chat_id].turn_verification)
    assert pcm._chats[chat.chat_id].turn_verification["0"] == _VERDICT
    verdicts = [e for e in published if e.get("type") == "turn_verification"]
    assert len(verdicts) == 1
    assert verdicts[0]["turn_index"] == 0
    assert verdicts[0]["verification"] == _VERDICT


async def test_no_check_without_vault_reads_or_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[object] = []

    async def fake_verify(*args, **kwargs):
        calls.append(args)
        return _VERDICT

    monkeypatch.setattr("ciao.vault_verification.verify_answer", fake_verify)

    # (a) a Read outside the vault
    pcm = _make_manager(tmp_path / "outside")
    project = pcm.create_project("no-check", workspace="work")
    chat = pcm.create_chat(project.project_id, title="verify-test")
    other = tmp_path / "outside" / "other.md"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("not the vault", encoding="utf-8")
    await _drive_turn(pcm, chat.chat_id, [_read(other), _result("ok")])

    # (b) a vault read with the setting off
    pcm = _make_manager(tmp_path / "disabled")
    project = pcm.create_project("no-check", workspace="work")
    chat = pcm.create_chat(project.project_id, title="verify-test")
    note = _ann_note(pcm)
    pcm.verify_vault_answers_enabled = lambda: False  # type: ignore[assignment]
    await _drive_turn(pcm, chat.chat_id, [_read(note), _result("ok")])

    # (c) a vault read in a turn that errored
    pcm = _make_manager(tmp_path / "errored")
    project = pcm.create_project("no-check", workspace="work")
    chat = pcm.create_chat(project.project_id, title="verify-test")
    note = _ann_note(pcm)
    await _drive_turn(pcm, chat.chat_id, [_read(note), _result("boom", is_error=True)])

    await asyncio.sleep(0.05)
    assert calls == []


def test_transcript_overlay_attaches_verification_to_last_assistant_row() -> None:
    entries = [
        {"role": "user", "turn_index": 0},
        {"role": "assistant", "text": "first"},
        {"role": "assistant", "text": "last of turn 0"},
        {"role": "user", "turn_index": 1},
        {"role": "assistant", "text": "turn 1"},
    ]
    transcript_service._overlay_turn_verification(entries, {"0": _VERDICT})
    assert ["verification" in e for e in entries] == [False, False, True, False, False]
    assert entries[2]["verification"] == _VERDICT


async def test_steered_turn_keys_the_verdict_on_the_steer_index(
    tmp_path: Path, monkeypatch
) -> None:
    """A steer that lands before the terminal frame owns the final answer row.

    Harness: the fake provider appends to the live ChatStream's
    ``steered_turn_indexes`` (the same list ``project_chats`` fills when a steer
    is delivered) before its successful ResultEvent, so no real steer is sent.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("vault-steer", workspace="work")
    chat = pcm.create_chat(project.project_id, title="steer-verify")
    note = _ann_note(pcm)

    calls: list[tuple[str, list[Path]]] = []

    async def fake_verify(answer, note_paths, **_kwargs):
        calls.append((answer, list(note_paths)))
        return _VERDICT

    monkeypatch.setattr("ciao.vault_verification.verify_answer", fake_verify)

    published: list[dict] = []
    real_publish = pcm._events.publish

    def spy(payload):
        published.append(payload)
        return real_publish(payload)

    pcm._events.publish = spy  # type: ignore[method-assign]

    async def fake_stream_chat(chat_id, _prompt, images=None, **_kwargs):
        yield _read(note)
        pcm.get_active_stream(chat_id).steered_turn_indexes.append(1)
        yield _result("Ann lives in Rome.")

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    async def consume(stream) -> None:
        async for _ev in stream.subscribe():
            pass

    stream = pcm.start_stream(chat.chat_id, "where does ann live?")
    await asyncio.wait_for(asyncio.create_task(consume(stream)), timeout=5.0)

    await _wait_for(lambda: "1" in pcm._chats[chat.chat_id].turn_verification)
    turn_verification = pcm._chats[chat.chat_id].turn_verification
    assert "0" not in turn_verification
    assert turn_verification["1"] == _VERDICT
    verdicts = [e for e in published if e.get("type") == "turn_verification"]
    assert len(verdicts) == 1
    assert verdicts[0]["turn_index"] == 1
    assert calls == [("Ann lives in Rome.", [note.resolve()])]
