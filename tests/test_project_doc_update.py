"""Tests for ``ciao.project_doc_update``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ciao import memory_receipts as mr
from ciao import project_doc_update as pdu


_DOC = """---
tags: [project]
---
# Store Intelligence Platform

## Status
Building the ingestion pipeline.

## Open loops
- Pick a queue backend.
"""

_INSIGHTS_WITH_DECISION = """\
## Decisions
- Chose Redis Streams over Kafka because ops overhead. [idx=9]

## Errors
- Build failed once -> retried. [idx=3]
"""

_INSIGHTS_NOISE_ONLY = """\
## Errors
- Build failed once -> retried. [idx=3]

## Reusable snippets
- Rebuild command:
  ```sh
  make build
  ```
"""


def _write_doc(tmp_path: Path) -> Path:
    (tmp_path / "Workspace").mkdir(exist_ok=True)
    doc = tmp_path / "project.md"
    doc.write_text(_DOC, encoding="utf-8")
    return doc


def _patch_oneshot(monkeypatch: pytest.MonkeyPatch, reply: str, calls: list | None = None):
    async def fake_oneshot(prompt, *, system_prompt, model, env=None, timeout_s=120.0, **kwargs):
        if calls is not None:
            calls.append(prompt)
        return reply

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)


def test_trigger_detection() -> None:
    assert pdu.insights_warrant_doc_update(_INSIGHTS_WITH_DECISION)
    assert not pdu.insights_warrant_doc_update(_INSIGHTS_NOISE_ONLY)
    assert not pdu.insights_warrant_doc_update("")
    # A trigger heading with no bullets does not count.
    assert not pdu.insights_warrant_doc_update("## Decisions\n\n## Errors\n- x -> y\n")


def test_noise_only_insights_skip_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _write_doc(tmp_path)
    calls: list = []
    _patch_oneshot(monkeypatch, "SHOULD NOT BE CALLED", calls)

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_NOISE_ONLY, model="m",
    ))

    assert wrote is False
    assert calls == []
    assert doc.read_text(encoding="utf-8") == _DOC


def test_missing_doc_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    _patch_oneshot(monkeypatch, "anything", calls)

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=tmp_path / "nope.md",
        insights_md=_INSIGHTS_WITH_DECISION,
        model="m",
    ))

    assert wrote is False
    assert calls == []


def test_no_changes_sentinel_leaves_doc_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _write_doc(tmp_path)
    _patch_oneshot(monkeypatch, json.dumps({"action": "covered"}))

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
    ))

    assert wrote is False
    assert doc.read_text(encoding="utf-8") == _DOC


def test_material_update_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _write_doc(tmp_path)
    calls: list = []
    _patch_oneshot(
        monkeypatch,
        json.dumps({
            "action": "update",
            "index": 1,
            "text": "- Pick a queue backend. Resolved: Redis Streams.",
        }),
        calls,
    )

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
    ))

    assert wrote is True
    text = doc.read_text(encoding="utf-8")
    # The Open loops bullet is the only change: frontmatter and prose survive.
    assert "- Pick a queue backend. Resolved: Redis Streams.\n" in text
    assert text.replace(
        "- Pick a queue backend. Resolved: Redis Streams.",
        "- Pick a queue backend.",
    ) == _DOC
    # Prompt carried the numbered entry, not a file to return.
    assert "1. section: Open loops" in calls[0]
    assert "- Pick a queue backend." in calls[0]
    assert "complete updated doc" not in calls[0]


def test_add_appends_a_bullet_under_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _write_doc(tmp_path)
    _patch_oneshot(
        monkeypatch,
        json.dumps({
            "action": "add",
            "section": "Decisions",
            "text": "- Chose Redis Streams over Kafka (ops overhead).",
        }),
    )

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
    ))

    assert wrote is True
    text = doc.read_text(encoding="utf-8")
    assert text.startswith("---\ntags: [project]\n---\n")
    assert "- Pick a queue backend.\n" in text
    assert "- Chose Redis Streams over Kafka (ops overhead).\n" in text
    assert text.count("## Decisions") == 1


def test_code_fenced_output_is_unwrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _write_doc(tmp_path)
    reply = json.dumps({
        "action": "add",
        "section": "Decisions",
        "text": "- Chose Redis Streams over Kafka.",
    })
    _patch_oneshot(monkeypatch, f"```json\n{reply}\n```")

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
    ))

    # A reply wrapped in one whole ```json fence is unwrapped and written.
    assert wrote is True
    after = doc.read_text(encoding="utf-8")
    assert "- Chose Redis Streams over Kafka." in after


def test_model_failure_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _write_doc(tmp_path)

    async def boom(prompt, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", boom)

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
    ))

    assert wrote is False
    assert doc.read_text(encoding="utf-8") == _DOC


def test_model_failure_reports_a_reason_through_error_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider failure is distinguishable from a legitimate no-op."""
    doc = _write_doc(tmp_path)

    async def boom(prompt, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", boom)
    errors: list[str] = []

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
        error_out=errors,
    ))

    assert wrote is False
    assert errors and "upstream down" in errors[-1]


def test_no_changes_sentinel_reports_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The NO_CHANGES no-op must not look like a failure to the caller."""
    doc = _write_doc(tmp_path)
    _patch_oneshot(monkeypatch, "NO_CHANGES")
    errors: list[str] = []

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
        error_out=errors,
    ))

    assert wrote is False
    assert errors == []


# ── fold_fact_into_person_note ───────────────────────────────────────────

_NOTE = """---
tags: [person]
---
# Laurene Racine

**Role:** Product Manager
"""


def _write_note(tmp_path: Path) -> Path:
    (tmp_path / "Workspace").mkdir(exist_ok=True)
    note = tmp_path / "Laurene-Racine.md"
    note.write_text(_NOTE, encoding="utf-8")
    return note


def test_person_fold_writes_the_merged_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _write_note(tmp_path)
    calls: list = []
    _patch_oneshot(
        monkeypatch,
        json.dumps({
            "action": "add",
            "section": "Notes",
            "text": "- Q4 2026 capacity is committed to Handoff Intelligence.",
        }),
        calls,
    )

    wrote = asyncio.run(pdu.fold_fact_into_person_note(
        note_path=note, fact="Her Q4 2026 capacity is committed to Handoff Intelligence.", model="m",
    ))

    assert wrote is True
    text = note.read_text(encoding="utf-8")
    # The prose note keeps its prose and gains a bullet under ## Notes.
    assert "**Role:** Product Manager" in text
    assert "- Q4 2026 capacity is committed to Handoff Intelligence.\n" in text
    assert text.count("## Notes") == 1
    assert "Handoff Intelligence" in calls[0]


def test_person_fold_no_changes_leaves_the_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _write_note(tmp_path)
    _patch_oneshot(monkeypatch, json.dumps({"action": "covered"}))
    errors: list[str] = []

    wrote = asyncio.run(pdu.fold_fact_into_person_note(
        note_path=note, fact="Product Manager.", model="m", error_out=errors,
    ))

    assert wrote is False
    assert errors == ["the fold reported no changes; dismiss instead"]
    assert note.read_text(encoding="utf-8") == _NOTE


def test_person_fold_keeps_an_edit_made_during_the_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _write_note(tmp_path)
    edited = _NOTE + "\nHand edit while the model ran.\n"
    entry_calls: list[str] = []

    async def slow(prompt, **kwargs):
        note.write_text(edited, encoding="utf-8")
        return json.dumps(
            {"action": "add", "section": "Notes", "text": "- New fact."}
        )

    def _spy_apply(**kwargs):
        entry_calls.append("apply")
        raise AssertionError("must not be called after a concurrent edit")

    def _spy_append(**kwargs):
        entry_calls.append("append")
        raise AssertionError("must not be called after a concurrent edit")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", slow)
    monkeypatch.setattr("ciao.note_receipts.apply_entry_edit", _spy_apply)
    monkeypatch.setattr("ciao.note_receipts.append_list_item", _spy_append)
    errors: list[str] = []

    wrote = asyncio.run(pdu.fold_fact_into_person_note(
        note_path=note, fact="New fact.", model="m", error_out=errors,
    ))

    assert wrote is False
    assert note.read_text(encoding="utf-8") == edited
    assert errors and "changed during the fold" in errors[-1]
    assert entry_calls == []


def test_person_fold_reports_a_model_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _write_note(tmp_path)

    async def boom(*args, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", boom)
    errors: list[str] = []

    wrote = asyncio.run(pdu.fold_fact_into_person_note(
        note_path=note, fact="New fact.", model="m", error_out=errors,
    ))

    assert wrote is False
    assert errors and "upstream down" in errors[-1]
    assert note.read_text(encoding="utf-8") == _NOTE


# ── R1: fold entry invariants ────────────────────────────────────────────
#
# A fold is not a verification and reads exact bytes: CRLF notes fold
# without a phantom conflict and undo byte for byte, a changed or new
# fact cannot gain or keep a `[verified:]` stamp, an unchanged fact
# cannot be re-dated, and an update that changes nothing is a
# dismiss-instead no-change in both folds.

_CRLF_DOC = b"## Open loops\r\n- Pick a queue backend.\r\n"
_CRLF_NOTE = b"## Notes\r\n- Lives in Porto.\r\n"

_STAMPED_NOTE = (
    "# Mo\n\n## Notes\n"
    "- Lives in Porto [verified: 2024-03-01]\n"
    "- Likes tea.\n"
)


def _write_bytes(tmp_path: Path, name: str, data: bytes) -> Path:
    (tmp_path / "Workspace").mkdir(exist_ok=True)
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _undo_fold(vault: Path) -> None:
    rows = mr.read_receipts(mr.journal_path(vault, None))
    applied = [
        row for row in rows
        if row.get("kind") == "note_apply" and row.get("status") == mr.APPLIED
    ]
    assert applied, "the fold must journal exactly one applied note receipt"
    undone = mr.undo_receipt(applied[-1]["id"], vault_root=vault)
    assert undone["status"] == mr.UNDONE


def test_project_fold_crlf_update_and_undo_are_byte_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _write_bytes(tmp_path, "project.md", _CRLF_DOC)
    _patch_oneshot(
        monkeypatch,
        json.dumps({
            "action": "update",
            "index": 1,
            "text": "- Pick a queue backend. Resolved: Redis Streams.",
        }),
    )

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
    ))

    assert wrote is True
    assert doc.read_bytes() == (
        b"## Open loops\r\n"
        b"- Pick a queue backend. Resolved: Redis Streams.\r\n"
    )
    _undo_fold(tmp_path)
    assert doc.read_bytes() == _CRLF_DOC


def test_project_fold_crlf_add_and_undo_are_byte_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _write_bytes(tmp_path, "project.md", _CRLF_DOC)
    _patch_oneshot(
        monkeypatch,
        json.dumps({
            "action": "add",
            "section": "Decisions",
            "text": "- Chose Redis Streams over Kafka.",
        }),
    )

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
    ))

    assert wrote is True
    after = doc.read_bytes()
    assert b"\r\n" in after and b"- Chose Redis Streams over Kafka.\r\n" in after
    assert after.startswith(_CRLF_DOC)
    _undo_fold(tmp_path)
    assert doc.read_bytes() == _CRLF_DOC


def test_person_fold_crlf_update_and_undo_are_byte_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _write_bytes(tmp_path, "Mo.md", _CRLF_NOTE)
    _patch_oneshot(
        monkeypatch,
        json.dumps({
            "action": "update",
            "index": 1,
            "text": "- Lives in Lisbon.",
        }),
    )

    wrote = asyncio.run(pdu.fold_fact_into_person_note(
        note_path=note, fact="Lives in Lisbon.", model="m",
    ))

    assert wrote is True
    assert note.read_bytes() == b"## Notes\r\n- Lives in Lisbon.\r\n"
    _undo_fold(tmp_path)
    assert note.read_bytes() == _CRLF_NOTE


def test_person_fold_crlf_add_and_undo_are_byte_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _write_bytes(tmp_path, "Mo.md", _CRLF_NOTE)
    _patch_oneshot(
        monkeypatch,
        json.dumps({
            "action": "add",
            "section": "Notes",
            "text": "- Based in Lisbon.",
        }),
    )

    wrote = asyncio.run(pdu.fold_fact_into_person_note(
        note_path=note, fact="Based in Lisbon.", model="m",
    ))

    assert wrote is True
    assert note.read_bytes() == _CRLF_NOTE + b"- Based in Lisbon.\r\n"
    _undo_fold(tmp_path)
    assert note.read_bytes() == _CRLF_NOTE


@pytest.mark.parametrize("writer", ["project", "people"])
def test_folds_cut_a_stamp_kept_on_a_changed_fact(
    writer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reworded fact is unverified, even when the reply keeps the stamp."""
    if writer == "project":
        doc = _write_bytes(
            tmp_path, "project.md",
            b"## Open loops\n- Lives in Porto [verified: 2024-03-01]\n- Likes tea.\n",
        )
        _patch_oneshot(
            monkeypatch,
            json.dumps({
                "action": "update",
                "index": 1,
                "text": "- Lives in Lisbon [verified: 2024-03-01]",
            }),
        )
        wrote = asyncio.run(pdu.update_project_doc(
            doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
        ))
        after = doc.read_bytes()
    else:
        note = _write_bytes(tmp_path, "Mo.md", _STAMPED_NOTE.encode("utf-8"))
        _patch_oneshot(
            monkeypatch,
            json.dumps({
                "action": "update",
                "index": 1,
                "text": "- Lives in Lisbon [verified: 2024-03-01]",
            }),
        )
        wrote = asyncio.run(pdu.fold_fact_into_person_note(
            note_path=note, fact="Lives in Lisbon.", model="m",
        ))
        after = note.read_bytes()

    assert wrote is True
    # The new words land without the verification claim, and the untouched
    # neighbor keeps its exact bytes.
    assert b"- Lives in Lisbon\n" in after
    assert b"[verified:" not in after.split(b"- Likes tea.\n")[0]
    assert after.endswith(b"- Likes tea.\n")


@pytest.mark.parametrize("writer", ["project", "people"])
def test_folds_cut_a_stamp_on_a_new_fact(
    writer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new bullet carries no verification, even when the reply stamps it."""
    reply = json.dumps({
        "action": "add",
        "section": "Notes",
        "text": "- Based in Lisbon [verified: 2024-03-01]",
    })
    if writer == "project":
        doc = _write_doc(tmp_path)
        _patch_oneshot(monkeypatch, reply)
        wrote = asyncio.run(pdu.update_project_doc(
            doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
        ))
        # Newline-agnostic: on Windows the fixture lands as CRLF bytes and
        # the appended bullet takes the note's own spelling.
        after = doc.read_bytes().replace(b"\r\n", b"\n")
        assert wrote is True
        assert b"- Based in Lisbon\n" in after
        assert b"[verified:" not in after
        assert b"- Pick a queue backend.\n" in after
    else:
        note = _write_note(tmp_path)
        _patch_oneshot(monkeypatch, reply)
        wrote = asyncio.run(pdu.fold_fact_into_person_note(
            note_path=note, fact="Based in Lisbon.", model="m",
        ))
        after = note.read_bytes().replace(b"\r\n", b"\n")
        assert wrote is True
        assert b"- Based in Lisbon\n" in after
        assert b"[verified:" not in after
        assert b"**Role:** Product Manager" in after


@pytest.mark.parametrize("writer", ["project", "people"])
def test_folds_do_not_redate_an_unchanged_fact(
    writer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-dating the same words is a no-change, not an update."""
    reply = json.dumps({
        "action": "update",
        "index": 1,
        "text": "- Lives in Porto [verified: 2025-01-01]",
    })
    if writer == "project":
        before = b"## Open loops\n- Lives in Porto [verified: 2024-03-01]\n"
        doc = _write_bytes(tmp_path, "project.md", before)
        _patch_oneshot(monkeypatch, reply)
        wrote = asyncio.run(pdu.update_project_doc(
            doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
        ))
        assert wrote is False
        assert doc.read_bytes() == before
    else:
        before = b"## Notes\n- Lives in Porto [verified: 2024-03-01]\n"
        note = _write_bytes(tmp_path, "Mo.md", before)
        _patch_oneshot(monkeypatch, reply)
        errors: list[str] = []
        wrote = asyncio.run(pdu.fold_fact_into_person_note(
            note_path=note, fact="Lives in Porto.", model="m",
            error_out=errors,
        ))
        assert wrote is False
        assert errors == ["the fold reported no changes; dismiss instead"]
        assert note.read_bytes() == before


@pytest.mark.parametrize("writer", ["project", "people"])
def test_noop_update_returns_no_change(
    writer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An update restating the entry writes nothing and dismisses instead."""
    reply = json.dumps({
        "action": "update",
        "index": 1,
        "text": "- Lives in Porto.",
    })
    if writer == "project":
        before = b"## Open loops\n- Lives in Porto.\n"
        doc = _write_bytes(tmp_path, "project.md", before)
        _patch_oneshot(monkeypatch, reply)
        wrote = asyncio.run(pdu.update_project_doc(
            doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
        ))
        assert wrote is False
        assert doc.read_bytes() == before
    else:
        before = b"## Notes\n- Lives in Porto.\n"
        note = _write_bytes(tmp_path, "Mo.md", before)
        _patch_oneshot(monkeypatch, reply)
        errors: list[str] = []
        wrote = asyncio.run(pdu.fold_fact_into_person_note(
            note_path=note, fact="Lives in Porto.", model="m",
            error_out=errors,
        ))
        assert wrote is False
        assert errors == ["the fold reported no changes; dismiss instead"]
        assert note.read_bytes() == before


_FOLD_ADD = json.dumps({
    "action": "add",
    "section": "Notes",
    "text": "- Based in Lisbon",
})


@pytest.mark.parametrize("writer", ["project", "people"])
@pytest.mark.parametrize("reply", [
    _FOLD_ADD,
    f"```json\n{_FOLD_ADD}\n```",
    f"```\n{_FOLD_ADD}\n```",
    f"  ```json\n{_FOLD_ADD}\n```\n",
    f"```json \n{_FOLD_ADD}\n```",
    f"```json\t\n{_FOLD_ADD}\n```",
    f"```\t\n{_FOLD_ADD}\n```",
    f"```json\r\n{_FOLD_ADD}\r\n```",
])
def test_fold_reply_parses_bare_or_fenced(
    writer: str, reply: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply is accepted bare or wrapped in one whole code fence."""
    _patch_oneshot(monkeypatch, reply)
    if writer == "project":
        doc = _write_doc(tmp_path)
        wrote = asyncio.run(pdu.update_project_doc(
            doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
        ))
        after = doc.read_bytes().replace(b"\r\n", b"\n")
    else:
        note = _write_note(tmp_path)
        wrote = asyncio.run(pdu.fold_fact_into_person_note(
            note_path=note, fact="Based in Lisbon.", model="m",
        ))
        after = note.read_bytes().replace(b"\r\n", b"\n")
    assert wrote is True
    assert b"- Based in Lisbon\n" in after


@pytest.mark.parametrize("writer", ["project", "people"])
@pytest.mark.parametrize("reply", [
    f"Here you go:\n```json\n{_FOLD_ADD}\n```",
    f"```json\n{_FOLD_ADD}\n```\nLet me know if that works.",
    f"{_FOLD_ADD}\n{_FOLD_ADD}",
    f"```json\n{_FOLD_ADD}\n{_FOLD_ADD}\n```",
])
def test_fold_reply_with_junk_or_two_objects_is_refused(
    writer: str, reply: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text around a fence, or more than one object, writes nothing."""
    _patch_oneshot(monkeypatch, reply)
    if writer == "project":
        doc = _write_doc(tmp_path)
        before = doc.read_bytes()
        wrote = asyncio.run(pdu.update_project_doc(
            doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
        ))
        assert wrote is False
        assert doc.read_bytes() == before
    else:
        note = _write_note(tmp_path)
        before = note.read_bytes()
        errors: list[str] = []
        wrote = asyncio.run(pdu.fold_fact_into_person_note(
            note_path=note, fact="Based in Lisbon.", model="m",
            error_out=errors,
        ))
        assert wrote is False
        assert errors == ["the fold reply was not a single entry edit"]
        assert note.read_bytes() == before
