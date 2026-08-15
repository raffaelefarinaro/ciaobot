"""Tests for ``ciao.memory_proposals``."""

from __future__ import annotations

from pathlib import Path

from ciao import memory_proposals as mp
from ciao import memory_tool as mt


def write_guide(
    path: Path,
    memory_entries: list[str] | None = None,
    profile_entries: list[str] | None = None,
    body: str = "# Guide\n\n",
) -> Path:
    """Seed a workspace guide with bounded-memory regions for a test."""
    path.write_text(body, encoding="utf-8")
    mt.ensure_regions(path)
    if memory_entries:
        mt.write_region(path, "memory", memory_entries)
    if profile_entries:
        mt.write_region(path, "profile", profile_entries)
    return path


_SAMPLE_INSIGHTS = """
## Errors
- Bash failed: command not found -> installed via brew. [idx=4]

## User corrections
- User said: "no em dashes" -> assistant rewrote with commas. Durable rule: Avoid em dashes; use commas instead. [idx=12]

## New entities
- person: Manager Example - the user's direct manager. [idx=2]
- person: User Example - the user, product lead. [idx=5]
- project: Smart Label Capture - OCR product the user owns. [idx=7]

## Decisions
- Chose OpenRouter over Anthropic for one-shot insights because cheaper. [idx=18]

## Dead ends
- Tried `gws auth login --profile work`; blocked by missing scopes. [idx=22]
"""


def test_propose_pulls_corrections_and_decisions() -> None:
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    texts = [p.text for p in proposals]
    assert any("no em dashes" in t for t in texts)
    assert any("Chose OpenRouter over Anthropic" in t for t in texts)


def test_propose_routes_user_self_to_profile_region() -> None:
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    profile_proposals = [p for p in proposals if p.target == "profile"]
    # Only the self-user entry should end up in the ciao:profile region.
    assert len(profile_proposals) == 1
    assert "User Example" in profile_proposals[0].text


def test_propose_strips_idx_citations() -> None:
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    for p in proposals:
        assert "[idx=" not in p.text


def test_propose_drops_dead_ends() -> None:
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    assert all("tried" not in p.text.lower() for p in proposals)


def test_append_proposals_uses_the_resolved_workspace_vault(tmp_path: Path) -> None:
    """The caller supplies the registry-owned root; this module never guesses."""
    vault = tmp_path / "vault"
    personal = vault / "personal"

    out = mp.append_proposals(
        mp.propose_from_insights(_SAMPLE_INSIGHTS),
        personal,
        source_path=None,
    )

    assert out == personal / "Workspace" / "Memory-Proposals.md"


def test_propose_handles_empty_input() -> None:
    assert mp.propose_from_insights("") == []
    assert mp.propose_from_insights("## Errors\n\n") == []


def test_append_proposals_writes_bullet_list(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    # No source frontmatter and no workspace directories in the vault yet, so
    # the queue lands at the vault root rather than under a guessed name.
    out = mp.append_proposals(proposals, vault, source_path=None)
    assert out is not None
    assert out.exists()
    assert out == vault / "Workspace" / "Memory-Proposals.md"
    text = out.read_text(encoding="utf-8")
    assert "Memory Proposals" in text
    # Each proposal lands as one bullet line.
    for p in proposals:
        assert p.text in text


def test_append_proposals_skips_empty(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert mp.append_proposals([], vault) is None
    assert not (vault / "personal" / "Workspace" / "Memory-Proposals.md").exists()


def test_append_proposals_dedups_against_existing_file(tmp_path: Path) -> None:
    """A proposal already recorded in the file is not stacked up again."""
    vault = tmp_path / "vault"
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)

    first = mp.append_proposals(proposals, vault, source_path=None)
    assert first is not None
    after_first = first.read_text(encoding="utf-8")

    # Re-proposing the identical batch writes nothing new and returns None.
    second = mp.append_proposals(proposals, vault, source_path=None)
    assert second is None
    assert first.read_text(encoding="utf-8") == after_first

    # A genuinely new proposal still gets appended; the dup alongside it does not.
    fresh = mp.MemoryProposal(
        target="memory", text="A brand new durable fact.", source_section="Decisions"
    )
    third = mp.append_proposals([proposals[0], fresh], vault, source_path=None)
    assert third is not None
    text = third.read_text(encoding="utf-8")
    assert "A brand new durable fact." in text
    # The recurring proposal appears exactly once, not once per batch.
    assert text.count(proposals[0].text) == 1


def test_append_proposals_routes_by_workspace(tmp_path: Path) -> None:
    """A transcript label cannot redirect a registry-resolved queue."""
    vault = tmp_path / "vault"
    work = vault / "work"
    archive = tmp_path / "chat.md"
    archive.write_text(
        "---\ntype: transcript\ncontext: personal\n---\n# chat\n\n## Session insights\n"
        + _SAMPLE_INSIGHTS,
        encoding="utf-8",
    )
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    out = mp.append_proposals(proposals, work, source_path=archive)
    assert out is not None
    assert out == vault / "work" / "Workspace" / "Memory-Proposals.md"
    assert not (
        vault / "personal" / "Workspace" / "Memory-Proposals.md"
    ).exists()


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


def test_proposals_from_archive_reports_a_count_via_stats(tmp_path: Path) -> None:
    """The archived chat says "3 memory proposals"; a path cannot express that."""
    vault = tmp_path / "vault"
    archive = tmp_path / "chat.md"
    archive.write_text(
        f"# chat\n\nsome turns here.\n\n## Session insights\n{_SAMPLE_INSIGHTS}",
        encoding="utf-8",
    )
    stats: dict[str, int] = {}
    out = mp.proposals_from_archive(archive, vault, stats=stats)
    assert out is not None
    assert stats["proposed"] > 0


def test_proposals_stats_stay_zero_when_nothing_is_filed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    archive = tmp_path / "chat.md"
    archive.write_text("# chat\n\nonly turns\n", encoding="utf-8")
    stats: dict[str, int] = {}
    assert mp.proposals_from_archive(archive, vault, stats=stats) is None
    assert stats.get("proposed", 0) == 0


# ── Auto-promotion of user corrections ────────────────────────────────────
#
# Promotion now writes straight into the fenced `ciao:memory` / `ciao:profile`
# regions of a CLAUDE.md guide (``guide_path=``), not a legacy `memory.md`.
# There is no write-time cap enforcement any more — the only fallback path is
# a guide with missing/malformed region markers.


def test_promote_writes_corrections_and_keeps_the_rest(tmp_path: Path) -> None:
    guide = write_guide(tmp_path / "CLAUDE.md")
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    remaining, promoted = mp.promote_user_corrections(proposals, guide_path=guide)

    assert promoted == ["Avoid em dashes; use commas instead."]
    mem_entries, _diags = mt.read_region(guide, "memory")
    # Only the state-shaped rule lands in the region, not the chat event.
    assert "Avoid em dashes; use commas instead." in mem_entries
    assert all("User said" not in entry for entry in mem_entries)
    # Decisions and entities are untouched and still reviewable.
    remaining_texts = [p.text for p in remaining]
    assert any("Chose OpenRouter over Anthropic" in t for t in remaining_texts)
    assert all("no em dashes" not in t for t in remaining_texts)


def test_promote_drops_exact_duplicates(tmp_path: Path) -> None:
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    # The region holds the state-shaped rule the bullet would promote to.
    guide = write_guide(
        tmp_path / "CLAUDE.md",
        memory_entries=["Avoid em dashes; use commas instead."],
    )

    remaining, promoted = mp.promote_user_corrections(proposals, guide_path=guide)

    assert promoted == []
    # Already remembered: not promoted, not proposed again.
    assert all(p.source_section != "User corrections" for p in remaining)


def test_promote_holds_back_event_shaped_corrections(tmp_path: Path) -> None:
    """A correction with no durable rule never lands in a bounded region.

    The regions are a state surface and memory-audit flags the
    "User said X -> assistant did Y" shape as rot; writing it verbatim just
    paid a nightly curation run to undo the archive-time write.
    """
    guide = write_guide(tmp_path / "CLAUDE.md")
    insights = (
        "## User corrections\n"
        '- User said: "implementt it directly" -> assistant switched from '
        "drafting an issue to coding the fix. [idx=62]\n"
    )
    proposals = mp.propose_from_insights(insights)
    remaining, promoted = mp.promote_user_corrections(proposals, guide_path=guide)

    assert promoted == []
    mem_entries, _diags = mt.read_region(guide, "memory")
    assert mem_entries == []
    # Still reviewable: the curator rephrases it on the next pass.
    assert any(p.source_section == "User corrections" for p in remaining)


def test_promote_ignores_echoed_rule_placeholder(tmp_path: Path) -> None:
    """A model echoing the template placeholder must not pollute the region."""
    guide = write_guide(tmp_path / "CLAUDE.md")
    insights = (
        "## User corrections\n"
        '- User said: "use tabs" -> assistant reformatted the file. '
        "Durable rule: <present-tense standing preference, if any>. [idx=3]\n"
    )
    proposals = mp.propose_from_insights(insights)
    remaining, promoted = mp.promote_user_corrections(proposals, guide_path=guide)

    assert promoted == []
    mem_entries, _diags = mt.read_region(guide, "memory")
    assert mem_entries == []
    assert any(p.source_section == "User corrections" for p in remaining)


def test_promote_state_shaped_correction_without_rule_clause(tmp_path: Path) -> None:
    """A bullet already phrased as standing state promotes verbatim."""
    guide = write_guide(tmp_path / "CLAUDE.md")
    insights = (
        "## User corrections\n"
        "- Prefers terse replies without preamble in code reviews. [idx=9]\n"
    )
    proposals = mp.propose_from_insights(insights)
    _remaining, promoted = mp.promote_user_corrections(proposals, guide_path=guide)

    assert promoted == ["Prefers terse replies without preamble in code reviews."]


def test_promote_falls_back_to_proposals_when_no_guide(tmp_path: Path) -> None:
    """No ``guide_path`` at all is the simplest fallback path."""
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    remaining, promoted = mp.promote_user_corrections(proposals, guide_path=None)

    assert promoted == []
    assert remaining == proposals


def test_promote_falls_back_to_proposals_when_markers_malformed(tmp_path: Path) -> None:
    """A guide with duplicated markers refuses the write; the fact stays reviewable."""
    guide = tmp_path / "CLAUDE.md"
    guide.write_text(
        "<!-- ciao:memory:start cap=2200 -->\n"
        "<!-- ciao:memory:start cap=2200 -->\n"
        "<!-- ciao:memory:end -->\n"
        "<!-- ciao:profile:start cap=1375 -->\n"
        "<!-- ciao:profile:end -->\n",
        encoding="utf-8",
    )

    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    remaining, promoted = mp.promote_user_corrections(proposals, guide_path=guide)

    assert promoted == []
    # The correction stays reviewable instead of being lost.
    assert any(p.source_section == "User corrections" for p in remaining)


def test_proposals_from_archive_auto_promotes_corrections(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    guide = write_guide(tmp_path / "CLAUDE.md")
    archive = tmp_path / "chat.md"
    archive.write_text(
        f"# chat\n\nturns.\n\n## Session insights\n{_SAMPLE_INSIGHTS}",
        encoding="utf-8",
    )

    out = mp.proposals_from_archive(
        archive, vault, auto_promote_memory=True, guide_path=guide
    )

    mem_entries, _diags = mt.read_region(guide, "memory")
    assert "Avoid em dashes; use commas instead." in mem_entries
    # The promoted correction is not duplicated into the proposals file.
    assert out is not None
    proposals_text = out.read_text(encoding="utf-8")
    assert "no em dashes" not in proposals_text
    assert "Chose OpenRouter over Anthropic" in proposals_text


def test_extract_ignores_quoted_marker_mid_transcript(tmp_path: Path) -> None:
    """A transcript that quotes the header is not mistaken for the section.

    Curation chats quote insights sections verbatim; the old first-occurrence
    match re-ingested the quoted (already reviewed) bullets as fresh proposals.
    """
    vault = tmp_path / "vault"
    archive = tmp_path / "chat.md"
    archive.write_text(
        "# chat\n\n## Turn 1\n\nquoting a prior archive:\n\n"
        "## Session insights\n\n## Decisions\n"
        "- Chose already-reviewed thing over alternative because reviewed. [idx=1]\n\n"
        "## Turn 2\n\nmore discussion.\n",
        encoding="utf-8",
    )

    out = mp.proposals_from_archive(archive, vault)

    assert out is None


def test_extract_prefers_appended_section_over_quoted_marker(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    archive = tmp_path / "chat.md"
    archive.write_text(
        "# chat\n\n## Turn 1\n\nquoting a prior archive:\n\n"
        "## Session insights\n\n## Decisions\n"
        "- Chose already-reviewed thing over alternative because reviewed. [idx=1]\n\n"
        "## Turn 2\n\nmore discussion.\n\n"
        "<!-- ciao:session-insights -->\n## Session insights\n\n## Decisions\n"
        "- Chose the fresh decision over the alternative because it is new. [idx=7]\n",
        encoding="utf-8",
    )

    out = mp.proposals_from_archive(archive, vault)

    assert out is not None
    text = out.read_text(encoding="utf-8")
    assert "fresh decision" in text
    assert "already-reviewed thing" not in text


def test_proposals_from_archive_default_leaves_memory_untouched(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    guide = write_guide(tmp_path / "CLAUDE.md")
    archive = tmp_path / "chat.md"
    archive.write_text(
        f"# chat\n\nturns.\n\n## Session insights\n{_SAMPLE_INSIGHTS}",
        encoding="utf-8",
    )

    out = mp.proposals_from_archive(archive, vault, guide_path=guide)

    assert out is not None
    mem_entries, _diags = mt.read_region(guide, "memory")
    assert mem_entries == []
    assert "no em dashes" in out.read_text(encoding="utf-8")
