"""Tests for ``ciao.memory_proposals``."""

from __future__ import annotations

from pathlib import Path

from ciao import memory_proposals as mp


_SAMPLE_INSIGHTS = """
## Errors
- Bash failed: command not found -> installed via brew. [idx=4]

## User corrections
- User said: "no em dashes" -> assistant rewrote with commas. [idx=12]

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
    # Without a source_path with known frontmatter, defaults to "personal" workspace.
    out = mp.append_proposals(proposals, vault, source_path=None)
    assert out is not None
    assert out.exists()
    assert out == vault / "personal" / "Workspace" / "Memory-Proposals.md"
    text = out.read_text(encoding="utf-8")
    assert "Memory Proposals" in text
    # Each proposal lands as one bullet line.
    for p in proposals:
        assert p.text in text


def test_append_proposals_skips_empty(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert mp.append_proposals([], vault) is None
    assert not (vault / "personal" / "Workspace" / "Memory-Proposals.md").exists()


def test_append_proposals_routes_by_workspace(tmp_path: Path) -> None:
    """Proposals from a 'work' chat archive go to vault/work/Workspace/Memory-Proposals.md."""
    vault = tmp_path / "vault"
    (vault / "work").mkdir(parents=True)
    archive = tmp_path / "chat.md"
    archive.write_text(
        "---\ntype: transcript\ncontext: work\n---\n# chat\n\n## Session insights\n" + _SAMPLE_INSIGHTS,
        encoding="utf-8",
    )
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    out = mp.append_proposals(proposals, vault, source_path=archive)
    assert out is not None
    assert out == vault / "work" / "Workspace" / "Memory-Proposals.md"


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


# ── Auto-promotion of user corrections ────────────────────────────────────


def test_promote_writes_corrections_and_keeps_the_rest(tmp_path: Path) -> None:
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    remaining, promoted = mp.promote_user_corrections(proposals, memory_dir=tmp_path)

    assert len(promoted) == 1
    assert "no em dashes" in promoted[0]
    mem_text = (tmp_path / "memory.md").read_text(encoding="utf-8")
    assert "no em dashes" in mem_text
    # Decisions and entities are untouched and still reviewable.
    remaining_texts = [p.text for p in remaining]
    assert any("Chose Pi over Claude SDK" in t for t in remaining_texts)
    assert all("no em dashes" not in t for t in remaining_texts)


def test_promote_drops_exact_duplicates(tmp_path: Path) -> None:
    from ciao import memory_tool as mt

    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    correction = next(p for p in proposals if p.source_section == "User corrections")
    mt.add_entry(tmp_path / "memory.md", correction.text, char_limit=2200)

    remaining, promoted = mp.promote_user_corrections(proposals, memory_dir=tmp_path)

    assert promoted == []
    # Already remembered: not promoted, not proposed again.
    assert all(p.source_section != "User corrections" for p in remaining)


def test_promote_falls_back_to_proposals_when_memory_full(
    tmp_path: Path, monkeypatch,
) -> None:
    from ciao import memory_tool as mt

    filler = "x" * 500
    mt.add_entry(tmp_path / "memory.md", filler, char_limit=2200)
    monkeypatch.setenv("CIAO_MEMORY_CHAR_LIMIT", str(len(filler) + 10))

    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    remaining, promoted = mp.promote_user_corrections(proposals, memory_dir=tmp_path)

    assert promoted == []
    # The correction stays reviewable instead of being lost.
    assert any(p.source_section == "User corrections" for p in remaining)


def test_proposals_from_archive_auto_promotes_corrections(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    memory_dir = tmp_path / "ciao-home"
    archive = tmp_path / "chat.md"
    archive.write_text(
        f"# chat\n\nturns.\n\n## Session insights\n{_SAMPLE_INSIGHTS}",
        encoding="utf-8",
    )

    out = mp.proposals_from_archive(
        archive, vault, auto_promote_memory=True, memory_dir=memory_dir
    )

    mem_text = (memory_dir / "memory.md").read_text(encoding="utf-8")
    assert "no em dashes" in mem_text
    # The promoted correction is not duplicated into the proposals file.
    assert out is not None
    proposals_text = out.read_text(encoding="utf-8")
    assert "no em dashes" not in proposals_text
    assert "Chose Pi over Claude SDK" in proposals_text


def test_proposals_from_archive_default_leaves_memory_untouched(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    memory_dir = tmp_path / "ciao-home"
    archive = tmp_path / "chat.md"
    archive.write_text(
        f"# chat\n\nturns.\n\n## Session insights\n{_SAMPLE_INSIGHTS}",
        encoding="utf-8",
    )

    out = mp.proposals_from_archive(archive, vault, memory_dir=memory_dir)

    assert out is not None
    assert not (memory_dir / "memory.md").exists()
    assert "no em dashes" in out.read_text(encoding="utf-8")
