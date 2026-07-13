"""Tests for ``ciao.project_doc_update``."""

from __future__ import annotations

import asyncio
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
    _patch_oneshot(monkeypatch, "NO_CHANGES")

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
    ))

    assert wrote is False
    assert doc.read_text(encoding="utf-8") == _DOC


def test_material_update_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _write_doc(tmp_path)
    updated = _DOC.replace(
        "- Pick a queue backend.",
        "- ~~Pick a queue backend~~ Resolved: Redis Streams (ops overhead vs Kafka).",
    ).strip()
    calls: list = []
    _patch_oneshot(monkeypatch, updated, calls)

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
    ))

    assert wrote is True
    text = doc.read_text(encoding="utf-8")
    assert "Redis Streams" in text
    assert text.startswith("---")
    # Prompt carried both the current doc and the insights.
    assert "Store Intelligence Platform" in calls[0]
    assert "Chose Redis Streams" in calls[0]


def test_code_fenced_output_is_unwrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _write_doc(tmp_path)
    updated = _DOC.strip() + "\n\n## Decisions\n- Redis Streams over Kafka."
    _patch_oneshot(monkeypatch, f"```markdown\n{updated}\n```")

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
    ))

    assert wrote is True
    text = doc.read_text(encoding="utf-8")
    assert "```" not in text
    assert text.startswith("---")


def test_dropped_frontmatter_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _write_doc(tmp_path)
    _patch_oneshot(
        monkeypatch,
        "# Store Intelligence Platform\n\nRewritten without frontmatter but "
        "otherwise long enough to pass the size guard easily, with plenty of "
        "extra words to make sure length is not the failing check here.",
    )

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
    ))

    assert wrote is False
    assert doc.read_text(encoding="utf-8") == _DOC


def test_truncated_rewrite_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _write_doc(tmp_path)
    _patch_oneshot(monkeypatch, "---\ntags: [project]\n---\n# Stub")

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc, insights_md=_INSIGHTS_WITH_DECISION, model="m",
    ))

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
