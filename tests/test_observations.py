"""Counted observations (#1338): ``Workspace/Observations.md`` and its writers."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from ciao import observations as ob
from ciao.learning_records import (
    SECTION_ACTIVE,
    SECTION_PROMOTED,
    LearningObservation,
    LearningRecord,
    allocate_learning_id,
    learning_key,
    parse_learnings,
    render_learning,
)
from ciao.memory_proposals import dismiss_proposal_by_substring, record_dismissal
from ciao.proposal_kinds import BULLET_RE

WORKSPACE = "personal"
TODAY = date(2026, 10, 12)
TEXT = "prefers a one-line summary before any detail"
STATEMENT = f"About the user: {TEXT}"


@pytest.fixture(autouse=True)
def _isolated_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "locks"))


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    return vault


def _add(vault: Path, source: str, *, about: str = ob.USER_SUBJECT, text: str = TEXT,
         today: date = TODAY) -> str:
    return ob.add_observation(
        vault, workspace=WORKSPACE, about=about, text=text, source=source, today=today
    )


def _entries(vault: Path) -> list[ob.Observation]:
    path = ob.observations_path(vault)
    return ob.parse_observations(path.read_text(encoding="utf-8"), workspace=WORKSPACE)


def _x3(vault: Path, *, about: str = ob.USER_SUBJECT, text: str = TEXT) -> str:
    for chat in ("chat-a", "chat-b", "chat-c"):
        _add(vault, chat, about=about, text=text)
    return next(e for e in _entries(vault) if e.about == about).record.learning_id


def _queue(vault: Path) -> str:
    path = vault / "Workspace" / "Memory-Proposals.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _hand_written(statement: str, chats: tuple[str, ...], *, last: date, count: int | None = None,
                  first: date | None = None, key: str = "") -> LearningRecord:
    return LearningRecord(
        learning_id=allocate_learning_id(WORKSPACE, f"hand:{statement}:{chats}:{last}"),
        key=key or learning_key(statement),
        text=statement,
        first_seen=first or last,
        last_seen=last,
        count=len(chats) if count is None else count,
        observations=tuple(LearningObservation(source=c) for c in chats),
    )


def _write_active(vault: Path, *records: LearningRecord, newline: str = "\n") -> None:
    body = "".join(f"{render_learning(r)}\n" for r in records)
    text = ob.OBSERVATIONS_STUB + "\n" + body
    ob.observations_path(vault).write_bytes(text.replace("\n", newline).encode("utf-8"))


def test_add_creates_file_from_stub_with_search_false(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    assert _add(vault, "chat-a") == "add"
    text = ob.observations_path(vault).read_text(encoding="utf-8")
    assert text.startswith(ob.OBSERVATIONS_STUB)
    assert text.splitlines()[1] == "search: false"
    (entry,) = _entries(vault)
    assert entry.record.text.startswith("About the user: ")
    assert entry.section == SECTION_ACTIVE


def test_same_chat_twice_is_a_noop(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _add(vault, "chat-a")
    before = ob.observations_path(vault).read_bytes()
    assert _add(vault, "chat-a", today=TODAY + timedelta(days=3)) == "none"
    assert ob.observations_path(vault).read_bytes() == before


def test_new_chat_counts(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    for chat in ("chat-a", "chat-b", "chat-c"):
        _add(vault, chat)
    (entry,) = _entries(vault)
    assert entry.record.count == 3
    assert "(x3)" in ob.observations_path(vault).read_text(encoding="utf-8")
    assert entry.chats == ("chat-a", "chat-b", "chat-c")
    assert ob.due_for_proposal(entry)


def test_same_text_different_subject_stays_separate(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _add(vault, "chat-a")
    _add(vault, "chat-a", about="Anna Rossi")
    assert sorted(e.about for e in _entries(vault)) == ["Anna Rossi", "the user"]


def test_add_requires_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _add(_vault(tmp_path), "  ")


@pytest.mark.parametrize("about", ["Anna: Rossi", "Anna\nRossi", "", "  "])
def test_subject_statement_rejects_colon_and_newline_in_about(about: str) -> None:
    with pytest.raises(ValueError):
        ob.subject_statement(about, "likes tea")


def test_subject_statement_flattens_text() -> None:
    assert ob.subject_statement("Anna Rossi", "likes\n  tea") == "About Anna Rossi: likes tea"


def test_propose_user_observation_queues_profile_and_resolves(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    obs_id = _x3(vault)
    result = ob.propose_observation(vault, workspace=WORKSPACE, observation_id=obs_id)
    assert result["queued"] is True and result["destination"] == "profile"
    queue = _queue(vault)
    bullet = next(line for line in queue.splitlines() if line.startswith("- [profile]"))
    assert f"] {TEXT}  _(from: observations chat-a, chat-b, chat-c)_" in bullet
    assert BULLET_RE.match(bullet)
    (entry,) = _entries(vault)
    assert entry.section == SECTION_PROMOTED
    assert not ob.due_for_proposal(entry)


def test_propose_person_observation_queues_people_with_payload(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    text = "usually reschedules Friday meetings"
    obs_id = _x3(vault, about="Anna Rossi", text=text)
    ob.propose_observation(vault, workspace=WORKSPACE, observation_id=obs_id)
    assert f"- [people Anna Rossi] {text}" in _queue(vault)


def test_propose_refuses_below_threshold(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _add(vault, "chat-a")
    _add(vault, "chat-b")
    obs_id = _entries(vault)[0].record.learning_id
    with pytest.raises(ValueError):
        ob.propose_observation(vault, workspace=WORKSPACE, observation_id=obs_id)
    assert _queue(vault) == ""


def test_propose_refuses_resolved_entry(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    obs_id = _x3(vault)
    ob.propose_observation(vault, workspace=WORKSPACE, observation_id=obs_id)
    with pytest.raises(ValueError):
        ob.propose_observation(vault, workspace=WORKSPACE, observation_id=obs_id)


def test_propose_after_dismissal_does_not_requeue(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    obs_id = _x3(vault)
    ob.propose_observation(vault, workspace=WORKSPACE, observation_id=obs_id)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    record_dismissal(queue, text=TEXT, kind="profile", outcome="dismissed")
    assert dismiss_proposal_by_substring(queue, TEXT)
    queue_before = _queue(vault)

    twin = _hand_written(STATEMENT, ("chat-d", "chat-e", "chat-f"), last=TODAY, key="twin")
    path = ob.observations_path(vault)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "## Active\n", f"## Active\n{render_learning(twin)}\n", 1
        ),
        encoding="utf-8",
    )
    result = ob.propose_observation(
        vault, workspace=WORKSPACE, observation_id=twin.learning_id
    )
    assert result["queued"] is False and result["dismissed_before"] is True
    assert _queue(vault) == queue_before
    moved = next(e for e in _entries(vault) if e.record.learning_id == twin.learning_id)
    assert moved.section == SECTION_PROMOTED


def test_resolved_entry_records_new_sighting_in_place(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    obs_id = _x3(vault)
    ob.propose_observation(vault, workspace=WORKSPACE, observation_id=obs_id)
    assert _add(vault, "chat-d") == "update"
    (entry,) = _entries(vault)
    assert entry.section == SECTION_PROMOTED and entry.record.count == 4
    assert not [e for e in _entries(vault) if e.section == SECTION_ACTIVE]


def test_merge_unions_chats_and_removes_drop(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    keep = _hand_written("About the user: likes short answers", ("chat-a", "chat-b"),
                         last=TODAY, first=date(2026, 10, 2))
    drop = _hand_written("About the user: wants brief replies", ("chat-b", "chat-c"),
                         last=date(2026, 10, 8), first=date(2026, 10, 1))
    _write_active(vault, keep, drop)
    result = ob.merge_observations(
        vault, workspace=WORKSPACE, keep_id=keep.learning_id, drop_id=drop.learning_id
    )
    assert result == {"kept": keep.learning_id, "dropped": drop.learning_id, "chats": 3}
    (entry,) = _entries(vault)
    assert entry.chats == ("chat-a", "chat-b", "chat-c")
    assert entry.record.count == 3
    assert drop.learning_id in entry.record.aliases
    assert entry.record.first_seen == date(2026, 10, 1)
    assert entry.record.last_seen == TODAY
    assert "wants brief replies" not in ob.observations_path(vault).read_text(encoding="utf-8")


def test_merge_refuses_different_subjects(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    keep = _hand_written("About the user: likes tea", ("chat-a",), last=TODAY)
    drop = _hand_written("About Anna Rossi: likes tea", ("chat-b",), last=TODAY)
    _write_active(vault, keep, drop)
    before = ob.observations_path(vault).read_bytes()
    with pytest.raises(ValueError):
        ob.merge_observations(
            vault, workspace=WORKSPACE, keep_id=keep.learning_id, drop_id=drop.learning_id
        )
    assert ob.observations_path(vault).read_bytes() == before


def test_prune_archives_old_x1_only(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    old = _hand_written("About the user: old thing", ("chat-a",), last=TODAY - timedelta(days=61))
    recent = _hand_written("About the user: recent thing", ("chat-b",), last=TODAY - timedelta(days=59))
    twice = _hand_written("About the user: seen twice", ("chat-c", "chat-d"),
                          last=TODAY - timedelta(days=90))
    _write_active(vault, old, recent, twice)
    result = ob.prune_observations(vault, workspace=WORKSPACE, today=TODAY)
    assert result == {"archived": [old.learning_id]}
    assert {e.record.learning_id for e in _entries(vault)} == {
        recent.learning_id, twice.learning_id
    }
    archive = (vault / ob.OBSERVATIONS_ARCHIVE_RELATIVE).read_text(encoding="utf-8")
    assert "search: false" in archive and "## Archived" in archive
    assert "old thing" in archive and "recent thing" not in archive


def test_prune_without_candidates_writes_no_archive(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _add(vault, "chat-a")
    assert ob.prune_observations(vault, workspace=WORKSPACE, today=TODAY) == {"archived": []}
    assert not (vault / ob.OBSERVATIONS_ARCHIVE_RELATIVE).exists()


def test_crlf_file_stays_crlf(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    first = _hand_written("About the user: likes tea", ("chat-a", "chat-b"), last=TODAY)
    _write_active(vault, first, newline="\r\n")
    _add(vault, "chat-c", text="likes tea")
    ob.propose_observation(vault, workspace=WORKSPACE, observation_id=first.learning_id)
    raw = ob.observations_path(vault).read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert b"## Promoted / Resolved\r\n" in raw
    (entry,) = _entries(vault)
    assert entry.section == SECTION_PROMOTED and entry.record.count == 3


def test_non_observation_lines_are_ignored(tmp_path: Path) -> None:
    text = ob.OBSERVATIONS_STUB + "- a plain bullet with no subject\n"
    assert ob.parse_observations(text, workspace=WORKSPACE) == []
    assert parse_learnings(text, workspace=WORKSPACE).entries
