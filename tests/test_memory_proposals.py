"""Tests for ``ciao.memory_proposals``."""

from __future__ import annotations

from pathlib import Path

from ciao import memory_proposals as mp


_SAMPLE_INSIGHTS = """
## Errors
- Bash failed: command not found -> installed via brew. [idx=4]

## User corrections
- Raffa said: "no em dashes" -> assistant rewrote with commas. [idx=12]

## New entities
- person: Manager Example - the user's direct manager. [idx=2]
- person: User Example - the user, product lead. [idx=5]
- project: Smart Label Capture - OCR product the user owns. [idx=7]

## Decisions
- Chose Pi over Claude SDK for one-shot insights because cheaper. [idx=18]

## Dead ends
- Tried `gws auth login --profile work`; blocked by missing scopes. [idx=22]
"""


def test_propose_pulls_corrections_and_decisions() -> None:
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    texts = [p.text for p in proposals]
    assert any("no em dashes" in t for t in texts)
    assert any("Chose Pi over Claude SDK" in t for t in texts)


def test_propose_routes_user_self_to_user_md() -> None:
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    user_proposals = [p for p in proposals if p.target == "user"]
    # Only the self-user entry should end up in user.md.
    assert len(user_proposals) == 1
    assert "User Example" in user_proposals[0].text


def test_propose_strips_idx_citations() -> None:
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    for p in proposals:
        assert "[idx=" not in p.text


def test_propose_drops_dead_ends() -> None:
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    assert all("tried" not in p.text.lower() for p in proposals)


def test_propose_handles_empty_input() -> None:
    assert mp.propose_from_insights("") == []
    assert mp.propose_from_insights("## Errors\n\n") == []


def test_append_proposals_writes_bullet_list(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    out = mp.append_proposals(proposals, vault, source_path=Path("/tmp/chat.md"))
    assert out is not None
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "Memory Proposals" in text
    # Each proposal lands as one bullet line.
    for p in proposals:
        assert p.text in text


def test_append_proposals_skips_empty(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert mp.append_proposals([], vault) is None
    assert not (vault / "Workspace" / "Memory-Proposals.md").exists()


def test_proposals_from_archive_extracts_insights_section(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    archive = tmp_path / "chat.md"
    archive.write_text(
        f"# chat\n\nsome turns here.\n\n## Session insights\n{_SAMPLE_INSIGHTS}",
        encoding="utf-8",
    )
    out = mp.proposals_from_archive(archive, vault)
    assert out is not None
    assert out.exists()


def test_proposals_from_archive_returns_none_when_no_insights(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    archive = tmp_path / "chat.md"
    archive.write_text("# chat\n\nonly turns\n", encoding="utf-8")
    out = mp.proposals_from_archive(archive, vault)
    assert out is None
