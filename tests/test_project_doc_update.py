"""Tests for ``ciao.project_doc_update``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

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

    # A fenced JSON reply is a parse failure and does not write.
    assert wrote is False
    assert doc.read_text(encoding="utf-8") == _DOC


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
