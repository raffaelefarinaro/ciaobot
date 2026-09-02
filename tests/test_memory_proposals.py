"""Tests for ``ciao.memory_proposals``."""

from __future__ import annotations

import asyncio
import os
import re
import unittest.mock
from pathlib import Path

from ciao import memory_proposals as mp
from ciao import memory_tool as mt
from ciao import proposal_tracking


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


def test_propose_drops_event_shaped_bullets_without_a_rule() -> None:
    """A transcript-event bullet is not durable state.

    The extraction prompt asks for standing rules, not "User said X -> assistant
    did Y" records; if one slips through, the heuristic drops it so the queue
    does not fill with session noise the curator would only dismiss.
    """
    insights = (
        "## User corrections\n"
        '- User said: "revert that" -> assistant restored the file. [idx=4]\n'
        '- User said: "use commas" -> assistant replaced the em dashes. '
        "Durable rule: Avoid em dashes; use commas instead. [idx=5]\n"
        "## Decisions\n"
        '- Chose inline over split because it was quicker this once. [idx=6]\n'
    )
    proposals = mp.propose_from_insights(insights)
    texts = [p.text for p in proposals]
    # The rule-less event-shaped correction is dropped.
    assert all("revert that" not in t for t in texts)
    # The one carrying a real durable-rule clause survives.
    assert any("use commas instead" in t for t in texts)


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


# ── Auto-apply ────────────────────────────────────────────────────────────
#
# apply_proposals writes straight into the fenced `ciao:memory` /
# `ciao:profile` regions of a CLAUDE.md guide (``guide_path=``). With
# ``vault_root=None`` only region-bound rows are acted on, which is what the
# region tests below want; people/learnings rows stay in ``remaining``.


def test_promote_writes_corrections_and_keeps_the_rest(tmp_path: Path) -> None:
    guide = write_guide(tmp_path / "CLAUDE.md")
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    remaining, promoted = mp.apply_proposals(proposals, guide_path=guide, vault_root=None)

    # Every confidently-addressed region fact applies: the correction into
    # ciao:memory and the operator's self-entry into ciao:profile.
    assert sorted(promoted) == sorted([
        "Avoid em dashes; use commas instead.",
        "person: User Example - the user, product lead.",
    ])
    mem_entries, _diags = mt.read_region(guide, "memory")
    # Only the state-shaped rule lands in the region, not the chat event —
    # stamped with the learned-at date the aging audit reads.
    from ciao.memory_audit import strip_learned_stamp

    stripped = [strip_learned_stamp(entry) for entry in mem_entries]
    assert "Avoid em dashes; use commas instead." in stripped
    assert any(re.search(r"\[\d{4}-\d{2}-\d{2}\]$", entry) for entry in mem_entries)
    assert all("User said" not in entry for entry in mem_entries)
    profile_entries, _diags = mt.read_region(guide, "profile")
    assert any("User Example" in entry for entry in profile_entries)
    # Untagged decisions and non-operator entities are unsure by default and
    # stay reviewable.
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

    remaining, promoted = mp.apply_proposals(proposals, guide_path=guide, vault_root=None)

    # The duplicate correction vanishes from both lists; only the operator
    # self-entry (profile region, still empty) applies.
    assert promoted == ["person: User Example - the user, product lead."]
    # Already remembered: not promoted, not proposed again.
    assert all(p.source_section != "User corrections" for p in remaining)


def test_promote_holds_back_event_shaped_corrections(tmp_path: Path) -> None:
    """A correction with no durable rule is never proposed at all.

    The regions are a state surface and memory-audit flags the
    "User said X -> assistant did Y" shape as rot; writing it verbatim just
    paid a nightly curation run to undo the archive-time write. A bullet with
    no ``Durable rule:`` clause that only records the exchange is the session
    noise the extraction prompt and heuristic both filter, so it is dropped at
    proposal time rather than queued for a curator to rephrase.
    """
    guide = write_guide(tmp_path / "CLAUDE.md")
    insights = (
        "## User corrections\n"
        '- User said: "implementt it directly" -> assistant switched from '
        "drafting an issue to coding the fix. [idx=62]\n"
    )
    proposals = mp.propose_from_insights(insights)
    remaining, promoted = mp.apply_proposals(proposals, guide_path=guide, vault_root=None)

    assert proposals == []
    assert promoted == []
    mem_entries, _diags = mt.read_region(guide, "memory")
    assert mem_entries == []
    assert remaining == []


def test_promote_ignores_echoed_rule_placeholder(tmp_path: Path) -> None:
    """A model echoing the template placeholder must not pollute the region."""
    guide = write_guide(tmp_path / "CLAUDE.md")
    insights = (
        "## User corrections\n"
        '- User said: "use tabs" -> assistant reformatted the file. '
        "Durable rule: <present-tense standing preference, if any>. [idx=3]\n"
    )
    proposals = mp.propose_from_insights(insights)
    remaining, promoted = mp.apply_proposals(proposals, guide_path=guide, vault_root=None)

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
    _remaining, promoted = mp.apply_proposals(proposals, guide_path=guide, vault_root=None)

    assert promoted == ["Prefers terse replies without preamble in code reviews."]


def test_promote_falls_back_to_proposals_when_no_guide(tmp_path: Path) -> None:
    """No ``guide_path`` at all is the simplest fallback path."""
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    remaining, promoted = mp.apply_proposals(proposals, guide_path=None, vault_root=None)

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
    remaining, promoted = mp.apply_proposals(proposals, guide_path=guide, vault_root=None)

    # The memory region refused the write (malformed markers), so the
    # correction stays reviewable; the healthy profile region still took the
    # operator's self-entry.
    assert not any("em dashes" in t for t in promoted)
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
    from ciao.memory_audit import strip_learned_stamp

    assert "Avoid em dashes; use commas instead." in [
        strip_learned_stamp(entry) for entry in mem_entries
    ]
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


def test_promote_holds_back_no_op_rule_clause(tmp_path: Path) -> None:
    """'Durable rule: None.' style fillers never land in a bounded region."""
    guide = write_guide(tmp_path / "CLAUDE.md")
    insights = (
        "## User corrections\n"
        '- User said: "revert that" -> assistant restored the file. '
        "Durable rule: None. [idx=4]\n"
        '- User said: "use x" -> assistant switched the tool. '
        "Durable rule: N/A. [idx=5]\n"
    )
    proposals = mp.propose_from_insights(insights)
    remaining, promoted = mp.apply_proposals(proposals, guide_path=guide, vault_root=None)

    assert promoted == []
    mem_entries, _diags = mt.read_region(guide, "memory")
    assert mem_entries == []
    assert sum(p.source_section == "User corrections" for p in remaining) == 2


def test_promote_survives_multi_index_citation(tmp_path: Path) -> None:
    """A `[idx=12,34]` tag must not block promotion of a well-formed rule.

    Models improvise multi-index citations (memory_audit's own comment and
    fixtures use them); a tag that survives parsing trips the audit's
    transcript-citation pattern and silently held the rule back.
    """
    guide = write_guide(tmp_path / "CLAUDE.md")
    insights = (
        "## User corrections\n"
        '- User said: "no em dashes" -> assistant rewrote with commas. '
        "Durable rule: Avoid em dashes; use commas instead. [idx=12,34]\n"
    )
    proposals = mp.propose_from_insights(insights)
    _remaining, promoted = mp.apply_proposals(proposals, guide_path=guide, vault_root=None)

    assert promoted == ["Avoid em dashes; use commas instead."]


def test_promote_accepts_rule_containing_if_any(tmp_path: Path) -> None:
    """A genuine rule that happens to contain 'if any' still promotes."""
    guide = write_guide(tmp_path / "CLAUDE.md")
    insights = (
        "## User corrections\n"
        '- User said: "do not just run everything" -> assistant asked first. '
        "Durable rule: Ask which tests to run, if any, before starting. [idx=7]\n"
    )
    proposals = mp.propose_from_insights(insights)
    _remaining, promoted = mp.apply_proposals(proposals, guide_path=guide, vault_root=None)

    assert promoted == ["Ask which tests to run, if any, before starting."]


def test_promote_ignores_rule_label_quoted_inside_user_text(tmp_path: Path) -> None:
    """A lowercase in-quote 'durable rule:' fragment is not a rule clause."""
    guide = write_guide(tmp_path / "CLAUDE.md")
    insights = (
        "## User corrections\n"
        '- User said: "write it as a durable rule: prefer pnpm over npm" '
        "and wanted shorter replies. [idx=8]\n"
    )
    proposals = mp.propose_from_insights(insights)
    remaining, promoted = mp.apply_proposals(proposals, guide_path=guide, vault_root=None)

    # No real durable-rule clause and an event-shaped frame: not durable.
    assert proposals == []
    assert promoted == []
    assert remaining == []


def test_durable_rule_label_matches_extraction_prompts() -> None:
    """The prompts and the consumer regex share one label; drift must fail."""
    from ciao import insights as insights_mod

    assert mp.DURABLE_RULE_LABEL in insights_mod._INSIGHTS_SYSTEM_PROMPT
    assert mp.DURABLE_RULE_LABEL in insights_mod._TEXT_MODE_SYSTEM_PROMPT


# ── Destination tags and routing ──────────────────────────────────────────


def test_tagged_bullets_route_to_their_destination() -> None:
    insights = (
        "## User corrections\n"
        "- Prefers pnpm over npm in every repo. [idx=1] [memory]\n"
        "## New entities\n"
        "- person: Mo Salah - the user's coach. [idx=2] [people: Mo Salah]\n"
        "## Decisions\n"
        "- Chose Postgres over SQLite for the app because concurrency. [idx=3] [project]\n"
        "- Chose conventional commits over freeform because tooling. [idx=4] [review]\n"
    )
    proposals = mp.propose_from_insights(insights)
    by_target = {p.target: p for p in proposals}
    assert set(by_target) == {"memory", "people", "project", "review"}
    assert by_target["people"].payload == "Mo Salah"
    # The tag is stripped from the queued text.
    assert "[people" not in by_target["people"].text
    assert "[project]" not in by_target["project"].text


def test_untagged_non_operator_entity_defaults_to_review() -> None:
    """A missing tag is uncertainty: only the old confident paths keep targets."""
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    decision = next(p for p in proposals if "OpenRouter" in p.text)
    manager = next(p for p in proposals if "Manager Example" in p.text)
    product = next(p for p in proposals if "Smart Label Capture" in p.text)
    assert decision.target == "review"
    assert manager.target == "people"
    assert manager.payload == "Manager Example"
    assert product.target == "review"


def test_apply_creates_people_note_only_when_absent(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    proposal = mp.MemoryProposal(
        target="people", text="Mo Salah - the user's coach.",
        source_section="New entities", payload="Mo Salah",
    )
    remaining, applied = mp.apply_proposals([proposal], vault_root=vault)
    assert applied == ["Mo Salah - the user's coach."]
    assert not remaining
    note = vault / "People" / "Mo Salah.md"
    assert note.exists()
    text = note.read_text(encoding="utf-8")
    assert "tags: [person]" in text
    # Creation counts as the first verification, so the stub is born dated
    # and can age out visibly instead of relying on mtime.
    assert re.search(r"^updated: \d{4}-\d{2}-\d{2}$", text, re.MULTILINE)

    # An existing note is a merge decision, so the fact stays queued.
    remaining, applied = mp.apply_proposals([proposal], vault_root=vault)
    assert applied == []
    assert len(remaining) == 1


def test_apply_appends_learning_under_active(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    proposal = mp.MemoryProposal(
        target="learnings",
        text="defuddle extracts clean article text where readability fails.",
        source_section="Decisions",
    )
    remaining, applied = mp.apply_proposals([proposal], vault_root=vault)
    assert applied and not remaining
    text = (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")
    assert "## Active" in text
    assert "defuddle extracts" in text

    # A second append lands under the existing section, not a new one.
    mp.apply_proposals(
        [mp.MemoryProposal(target="learnings", text="Second lesson.",
                           source_section="Decisions")],
        vault_root=vault,
    )
    text = (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")
    assert text.count("## Active") == 1
    assert "Second lesson." in text


def test_project_facts_are_dropped_when_the_fold_consumed_them(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    archive = tmp_path / "chat.md"
    archive.write_text(
        "# chat\n\nturns.\n\n## Session insights\n"
        "## Decisions\n"
        "- Chose Postgres over SQLite because concurrency. [idx=1] [project]\n",
        encoding="utf-8",
    )
    stats: dict[str, int] = {}
    out = mp.proposals_from_archive(
        archive, vault,
        project_doc_path="doc.md", project_fold_wrote=True, stats=stats,
    )
    # Consumed by the fold: nothing queued, nothing reported.
    assert out is None
    assert stats.get("proposed", 0) == 0


def test_already_applied_guard_does_not_match_contradictions_or_shared_words(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "note.md"
    destination.write_text(
        "Do not use Python for production tooling.\n"
        "Deploy production services through the managed release pipeline.\n",
        encoding="utf-8",
    )

    assert not mp._is_already_in_file(
        destination, "Use Python for production tooling."
    )
    assert not mp._is_already_in_file(
        destination,
        "Deploy production databases through the managed backup pipeline.",
    )
    destination.write_text(
        "Do not deploy production services through the managed release pipeline.\n",
        encoding="utf-8",
    )
    assert not mp._is_already_in_file(
        destination,
        "Deploy production services through the managed release pipeline.",
    )


def test_already_applied_guard_keeps_negation_over_repeated_matches(tmp_path: Path) -> None:
    destination = tmp_path / "note.md"
    destination.write_text(
        "Do not deploy X or deploy X during freezes.\n", encoding="utf-8"
    )

    assert not mp._is_already_in_file(destination, "Deploy X during freezes.")


def test_already_applied_guard_matches_short_exact_facts(tmp_path: Path) -> None:
    destination = tmp_path / "note.md"
    destination.write_text("Use PostgreSQL, not SQLite.\n", encoding="utf-8")

    assert mp._is_already_in_file(destination, "Use PostgreSQL, not SQLite.")


def test_pending_duplicate_proposal_keeps_base_identity_after_first_is_removed(tmp_path: Path) -> None:
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir()
    queue.write_text(
        "- [memory] Same fact — sources: one\n"
        "- [memory] Same fact — sources: one\n",
        encoding="utf-8",
    )

    class Config:
        def workspace_names(self):
            return ["personal"]

        def workspace_vault_root(self, _workspace):
            return tmp_path

    ids = proposal_tracking.pending_proposal_ids(Config())
    base = next(item for item in ids if ":" not in item)
    assert base in ids


def test_unconsumed_project_facts_queue_addressed_to_their_doc(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    archive = tmp_path / "chat.md"
    archive.write_text(
        "# chat\n\nturns.\n\n## Session insights\n"
        "## Decisions\n"
        "- Chose Postgres over SQLite because concurrency. [idx=1] [project]\n",
        encoding="utf-8",
    )
    out = mp.proposals_from_archive(
        archive, vault,
        project_doc_path="projects/x/doc.md", project_fold_wrote=False,
    )
    assert out is not None
    from ciao.proposal_kinds import parse_bullet
    bullets = [
        parse_bullet(line) for line in out.read_text(encoding="utf-8").splitlines()
    ]
    rows = [b for b in bullets if b is not None]
    assert len(rows) == 1
    assert rows[0].kind == "project"
    assert rows[0].target == "projects/x/doc.md"


def test_project_fact_from_general_chat_is_review_not_project_specific(
    tmp_path: Path,
) -> None:
    """A project tag without a canonical chat doc must not create an unroutable row."""
    vault = tmp_path / "vault"
    archive = tmp_path / "chat.md"
    archive.write_text(
        "# chat\n\nturns.\n\n## Session insights\n"
        "## Decisions\n"
        "- Chose Postgres over SQLite because concurrency. [idx=1] [project]\n",
        encoding="utf-8",
    )

    out = mp.proposals_from_archive(archive, vault)

    assert out is not None
    rows = mp.list_proposals(out)
    assert len(rows) == 1
    assert rows[0]["kind"] == "review"
    assert rows[0]["target"] == ""


def test_project_fact_uses_canonical_doc_even_when_model_supplies_a_path(
    tmp_path: Path,
) -> None:
    """Extraction output cannot redirect a project fact to another document."""
    vault = tmp_path / "vault"
    archive = tmp_path / "chat.md"
    archive.write_text(
        "# chat\n\nturns.\n\n## Session insights\n"
        "## Decisions\n"
        "- Chose Postgres over SQLite because concurrency. [idx=1] "
        "[project: guessed/other.md]\n",
        encoding="utf-8",
    )

    out = mp.proposals_from_archive(
        archive, vault, project_doc_path="projects/canonical/doc.md"
    )

    assert out is not None
    rows = mp.list_proposals(out)
    assert rows[0]["kind"] == "project"
    assert rows[0]["target"] == "projects/canonical/doc.md"


def test_queue_bullet_round_trips_payload() -> None:
    proposal = mp.MemoryProposal(
        target="people", text="Alba - a collaborator.",
        source_section="New entities", payload="Alba",
    )
    line = proposal.as_bullet()
    assert line.startswith("- [people Alba] Alba - a collaborator.")
    from ciao.proposal_kinds import parse_bullet
    bullet = parse_bullet(line)
    assert bullet is not None
    assert bullet.kind == "people"
    assert bullet.target == "Alba"


# --- CLI: memory-proposal-add -----------------------------------------------


def _add_args(tmp_path: Path, **overrides):
    import argparse

    defaults = {
        "workspace": tmp_path,
        "vault_root": tmp_path / "memory-vault",
        "text": "A workstream invisible to the vault scores zero everywhere.",
        "kind": "memory",
        "payload": "",
        "source": "chat-824cd4ec",
        "json": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_add_command_queues_a_reviewable_fact(
    tmp_path: Path,
    capsys,
) -> None:
    """A curator-discovered fact lands in the machine queue, not just prose.

    Archive-time routing only sees chats that grew a session-insights
    section; facts the nightly curation run finds by reading a transcript in
    full must get the same review path (list, promote, dismiss).
    """
    from ciao.cli import _memory_proposal_add_command

    exit_code = _memory_proposal_add_command(_add_args(tmp_path))

    assert exit_code == 0
    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    rows = mp.list_proposals(queue)
    assert len(rows) == 1
    assert rows[0]["kind"] == "memory"
    assert "scores zero everywhere" in rows[0]["text"]
    assert rows[0]["source"] == "chat-824cd4ec"
    out = capsys.readouterr().out
    assert "Queued [memory] proposal" in out


def test_add_command_dedupes_identical_text(tmp_path: Path) -> None:
    """Re-filing what an earlier run queued is a no-op, not a duplicate.

    The nightly run re-reads the same transcripts until the user promotes or
    dismisses; a queue that grows one copy per night stops being readable.
    """
    from ciao.cli import _memory_proposal_add_command

    first = _memory_proposal_add_command(_add_args(tmp_path))
    second = _memory_proposal_add_command(_add_args(tmp_path))

    assert first == second == 0
    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    assert len(mp.list_proposals(queue)) == 1


def test_add_command_rejects_an_unknown_kind(tmp_path: Path) -> None:
    """A kind outside DESTINATIONS is refused, not written through.

    ``MemoryProposal.as_bullet`` is deliberately total for archive batches —
    one odd proposal must not fail a whole archive — but the CLI is an agent
    command where a typo'd kind would queue an unroutable bullet.
    """
    from ciao.cli import _memory_proposal_add_command

    exit_code = _memory_proposal_add_command(_add_args(tmp_path, kind="memories"))

    assert exit_code == 2
    assert not (
        tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    ).exists()


def test_add_command_requires_payload_for_addressed_kinds(tmp_path: Path) -> None:
    """`people`/`project` without a payload would queue an unroutable bullet.

    The PWA accept handlers refuse such rows ("the bullet names no person" /
    "the bullet names no project doc"), so a successful CLI invocation must
    not be able to create one.
    """
    from ciao.cli import _memory_proposal_add_command

    for kind in ("people", "project"):
        exit_code = _memory_proposal_add_command(
            _add_args(tmp_path, kind=kind, payload="")
        )

        assert exit_code == 2, kind
        assert not (
            tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
        ).exists()

    # Naming the target unblocks both kinds.
    assert (
        _memory_proposal_add_command(
            _add_args(tmp_path, kind="people", payload="Mo Salah")
        )
        == 0
    )


def test_add_command_json_serializes_an_explicit_workspace(
    tmp_path: Path,
    capsys,
) -> None:
    """--json with an explicit --workspace emits parseable JSON.

    argparse supplies the workspace as a Path, which json.dump refuses to
    serialize; the command reports the resolved root instead so a caller
    never sees a TypeError after the queue was already modified.
    """
    import json as json_module

    from ciao.cli import _memory_proposal_add_command

    exit_code = _memory_proposal_add_command(
        _add_args(tmp_path, json=True, workspace=tmp_path)
    )

    assert exit_code == 0
    payload = json_module.loads(capsys.readouterr().out)
    assert payload["queued"] is True
    assert payload["workspace"] == str(tmp_path)


def test_add_command_queues_verbatim_text_from_file(tmp_path: Path) -> None:
    """A fact filed via --text-file lands byte-for-byte, shell hazards and all.

    The curator reads transcripts full of `$(...)`, backticks, `$VARS`, and
    quotes. Passing such text as a shell argument executes or mangles it;
    the file path is the non-interpolated input route, so what reaches the
    queue must equal what the agent wrote.
    """
    from ciao.cli import _memory_proposal_add_command

    hazardous = (
        'Deploy tag is "$(cat VERSION)" on `runner-2`; $CI_ENV says "staging"'
    )
    fact_file = tmp_path / "fact.txt"
    fact_file.write_text(hazardous + "\n", encoding="utf-8")

    exit_code = _memory_proposal_add_command(
        _add_args(tmp_path, text="", text_file=str(fact_file))
    )

    assert exit_code == 0
    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    rows = mp.list_proposals(queue)
    assert len(rows) == 1
    assert rows[0]["text"] == hazardous


def test_add_command_rejects_ambiguous_fact_inputs(tmp_path: Path) -> None:
    """Both or neither of text/--text-file is a usage error, not a guess."""
    from ciao.cli import _memory_proposal_add_command

    fact_file = tmp_path / "fact.txt"
    fact_file.write_text("A fact.", encoding="utf-8")

    both = _memory_proposal_add_command(
        _add_args(tmp_path, text="A fact.", text_file=str(fact_file))
    )
    neither = _memory_proposal_add_command(
        _add_args(tmp_path, text="", text_file="")
    )
    missing = _memory_proposal_add_command(
        _add_args(tmp_path, text="", text_file=str(tmp_path / "absent.txt"))
    )

    assert both == neither == missing == 2
    assert not (tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md").exists()


def test_add_command_flattens_a_multiline_fact_file(tmp_path: Path) -> None:
    """Embedded newlines become one single-line bullet that dedupes cleanly.

    The queue is line-oriented Markdown: a raw multiline fact would parse as
    a truncated first line, strand the continuation lines (or spawn phantom
    bullets), and never match itself on re-filing because no parsed bullet
    carries the full original text.
    """
    from ciao.cli import _memory_proposal_add_command

    multiline = "Deploy froze on Tuesday.\n- [memory] injected-looking line\n"
    fact_file = tmp_path / "fact.txt"
    fact_file.write_text(multiline, encoding="utf-8")

    exit_code = _memory_proposal_add_command(
        _add_args(tmp_path, text="", text_file=str(fact_file))
    )

    assert exit_code == 0
    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    rows = mp.list_proposals(queue)
    assert len(rows) == 1
    assert rows[0]["text"] == "Deploy froze on Tuesday. - [memory] injected-looking line"

    # The flattened form is what dedupe sees, so a re-file of the same file
    # is the documented no-op rather than a second copy.
    again = _memory_proposal_add_command(
        _add_args(tmp_path, text="", text_file=str(fact_file))
    )
    assert again == 0
    assert len(mp.list_proposals(queue)) == 1


def test_add_command_flattens_source_and_payload(tmp_path: Path) -> None:
    """Provenance fields cannot split a bullet or spawn a phantom proposal.

    ``--source`` carries a chat identifier and ``--payload`` a person name, but
    both are free text on the way in. A newline in either splits the written
    line, so the queue parses a truncated first bullet plus whatever the
    continuation looks like — and the real text never appears as one parsed
    bullet, so re-filing it dodges dedupe.
    """
    from ciao.cli import _memory_proposal_add_command

    exit_code = _memory_proposal_add_command(
        _add_args(
            tmp_path,
            text="Release trains freeze on Tuesdays.",
            kind="people",
            payload="Mo Salah\n- [memory] phantom payload row",
            source="chat-1\n- [memory] phantom source row",
        )
    )

    assert exit_code == 0
    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    rows = mp.list_proposals(queue)
    # One bullet in, one bullet out: no truncation, no injected extra rows.
    assert len(rows) == 1
    assert rows[0]["text"] == "Release trains freeze on Tuesdays."
    assert rows[0]["kind"] == "people"

    # The single written line still parses, so dedupe recognises a re-file.
    again = _memory_proposal_add_command(
        _add_args(
            tmp_path,
            text="Release trains freeze on Tuesdays.",
            kind="people",
            payload="Mo Salah",
            source="chat-1",
        )
    )
    assert again == 0
    assert len(mp.list_proposals(queue)) == 1


def test_bullet_keeps_its_delimiters_out_of_free_text_fields() -> None:
    """A `]` in the payload or a `)` in the source must not end its own slot.

    ``as_bullet`` owns the queue's line grammar: the destination head closes on
    the first `]` and the provenance tail on the first `)`, so an unescaped one
    inside either field makes the whole bullet unparseable — the row then
    exists in the file but is invisible to the review UI and to dedupe.
    """
    proposal = mp.MemoryProposal(
        target="people",
        text="Prefers async standups.",
        source_section="chat-1 (imported)",
        payload="Alex] Rivera",
    )

    bullet = proposal.as_bullet()

    assert bullet.count("]") == 1
    assert bullet.count(")") == 1
    assert mp._existing_proposal_texts(bullet) == {"Prefers async standups."}


def test_dismissed_facts_are_not_refiled_by_the_next_run(tmp_path: Path) -> None:
    """A dismissal outlives its row: dedupe consults the decision history.

    Removing the bullet is not enough — the nightly run re-reads the same
    transcript while it counts as recent and would re-file identical text,
    resurrecting a decision the user already made.
    """
    from ciao.cli import _memory_proposal_add_command

    text = "The release train freezes every second Tuesday."
    assert _memory_proposal_add_command(_add_args(tmp_path, text=text)) == 0

    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    removed = mp.remove_proposal_by_substring(queue, "release train")
    assert removed is not None
    kind, removed_text = removed
    mp.record_dismissal(queue, text=removed_text, kind=kind)

    # The next nightly pass re-files what the transcript still supports.
    exit_code = _memory_proposal_add_command(_add_args(tmp_path, text=text))

    assert exit_code == 0
    assert mp.list_proposals(queue) == []
    log = queue.with_suffix(".dismissed.jsonl")
    assert "release train" in log.read_text(encoding="utf-8")


def test_add_command_targets_the_active_workspace_vault(tmp_path: Path) -> None:
    """A scheduled run files into the logical workspace's queue, not the shared vault.

    Scheduled chats export ``CIAO_VAULT_ROOT`` at the install-wide shared
    vault while re-rooting is still pending; appending to that raw value
    strands the fact in a stray file the review UI never reads. The active
    workspace name must win and resolve through the registry — the same
    authority the PWA's ``workspace_vault_root`` reads with.
    """
    import argparse
    import json as json_module

    from ciao.cli import _memory_proposal_add_command

    root = tmp_path / "install"
    runtime = root / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "workspaces.json").write_text(
        json_module.dumps([
            {"name": "personal", "vault_root": "memory-vault/personal"},
            {"name": "client", "vault_root": "memory-vault/client"},
        ]),
        encoding="utf-8",
    )
    shared = tmp_path / "shared-vault"
    shared.mkdir()
    monkeypatch_env = {
        "CIAO_WORKSPACE": str(root),
        "CIAO_ACTIVE_WORKSPACE": "client",
        "CIAO_VAULT_ROOT": str(shared),
    }

    args = argparse.Namespace(
        workspace=None,
        vault_root=None,
        text="A client-scoped fact.",
        kind="memory",
        payload="",
        source="chat-abc",
        json=False,
    )
    with unittest.mock.patch.dict(os.environ, monkeypatch_env):
        exit_code = _memory_proposal_add_command(args)

    assert exit_code == 0
    queue = root / "memory-vault" / "client" / "Workspace" / "Memory-Proposals.md"
    assert queue.is_file()
    assert not (shared / "Workspace" / "Memory-Proposals.md").exists()

    # An explicit --workspace is a manual invocation: it keeps winning over
    # the ambient active-workspace name. With no explicit vault root and no
    # exported one either, the queue lands under that folder's own default.
    explicit_dir = tmp_path / "manual"
    manual_env = {k: v for k, v in monkeypatch_env.items() if k != "CIAO_VAULT_ROOT"}
    explicit_args = argparse.Namespace(
        workspace=explicit_dir,
        vault_root=None,
        text="A manual fact.",
        kind="memory",
        payload="",
        source="chat-abc",
        json=False,
    )
    with unittest.mock.patch.dict(os.environ, manual_env):
        assert _memory_proposal_add_command(explicit_args) == 0
    assert (explicit_dir / "memory-vault" / "Workspace" / "Memory-Proposals.md").is_file()


def test_removing_last_bullet_sweeps_its_batch_header(tmp_path: Path) -> None:
    """Dismissing a batch's only bullet removes the batch header too.

    Before the sweep, every fully-dismissed batch left its ``## <timestamp>``
    header behind; a real queue accumulated 29 empty batches out of 30.
    """
    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    proposals = [
        mp.MemoryProposal(target="review", text="lone fact one", source_section="Decisions"),
    ]
    out = mp.append_proposals(proposals, vault, source_path=None)
    assert out is not None
    out2 = mp.append_proposals(
        [mp.MemoryProposal(target="review", text="second batch fact", source_section="Decisions")],
        vault,
        source_path=None,
    )
    assert out2 is not None

    removed = mp.remove_proposal_by_substring(out, "lone fact one")
    assert removed is not None

    text = out.read_text(encoding="utf-8")
    # One batch header remains (the second batch still has its bullet).
    assert len(re.findall(r"^## \d{4}-", text, re.MULTILINE)) == 1
    assert "second batch fact" in text


def test_sweep_keeps_batches_with_bullets_or_prose(tmp_path: Path) -> None:
    """The sweep only removes headers over all-blank sections.

    A timestamped section that carries prose (agents have appended notes into
    the queue) was written by someone else and is not the sweep's to delete.
    """
    lines = [
        "# Memory Proposals",
        "",
        "## 2026-08-01T00:00:00+00:00 — from `a.md`",
        "",
        "- [review] pending fact  _(from: Decisions)_",
        "",
        "## 2026-08-02T00:00:00+00:00 — from `b.md`",
        "",
        "",
        "## 2026-08-03T00:00:00+00:00 — from `c.md`",
        "",
        "Some hand-written note under a timestamp.",
        "",
    ]
    swept = mp._sweep_empty_batches(lines)
    text = "\n".join(swept)
    assert "2026-08-01" in text  # has a bullet
    assert "2026-08-02" not in text  # empty: swept
    assert "2026-08-03" in text  # has prose: kept
    assert "Some hand-written note" in text


def test_record_dismissal_ignores_empty_and_junk_lines(tmp_path: Path) -> None:
    """Blank decisions record nothing; unreadable sidecar lines never crash."""
    from ciao.cli import _memory_proposal_add_command
    from ciao.memory_proposals import dismissed_log_path

    assert _memory_proposal_add_command(_add_args(tmp_path)) == 0
    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"

    assert mp.record_dismissal(queue, text="   ") is False

    log = dismissed_log_path(queue)
    log.write_text("{not json}\n\n", encoding="utf-8")
    assert mp._dismissed_texts(queue) == set()
    # A well-formed entry alongside junk still contributes its text.
    log.write_text(
        '{not json}\n{"text": "kept fact", "kind": "memory"}\n',
        encoding="utf-8",
    )
    assert mp._dismissed_texts(queue) == {"kept fact"}
    # The pre-rename .log sidecar is read too, so history written before the
    # extension change keeps protecting across the upgrade.
    legacy = queue.with_suffix(".dismissed.log")
    legacy.write_text('{"text": "legacy fact", "kind": "memory"}\n', encoding="utf-8")
    assert mp._dismissed_texts(queue) == {"kept fact", "legacy fact"}


def test_promote_dedupes_across_learned_stamps(tmp_path: Path) -> None:
    """The same fact promoted on a different day is still a duplicate."""
    proposals = mp.propose_from_insights(_SAMPLE_INSIGHTS)
    guide = write_guide(
        tmp_path / "CLAUDE.md",
        memory_entries=["Avoid em dashes; use commas instead. [2026-01-15]"],
    )

    remaining, promoted = mp.apply_proposals(proposals, guide_path=guide, vault_root=None)

    assert promoted == ["person: User Example - the user, product lead."]
    mem_entries, _diags = mt.read_region(guide, "memory")
    assert len(mem_entries) == 1  # no second copy with a fresh stamp


# ---- Write-time reconcile (ADD / UPDATE / COVERED) -----------------------


def test_parse_reconcile_reply_shapes() -> None:
    good = '[{"action": "add"}, {"action": "update", "index": 2, "text": "merged"}, {"action": "covered"}]'
    rows = mp._parse_reconcile_reply(good, 3)
    assert rows == [
        {"action": "add"},
        {"action": "update", "index": 2, "text": "merged"},
        {"action": "covered"},
    ]
    # Fenced replies are unwrapped.
    assert mp._parse_reconcile_reply(f"```json\n{good}\n```", 3) is not None
    # Wrong length or non-array: discarded whole.
    assert mp._parse_reconcile_reply('[{"action": "add"}]', 2) is None
    assert mp._parse_reconcile_reply('{"action": "add"}', 1) is None
    assert mp._parse_reconcile_reply("not json", 1) is None
    # Per-row junk degrades to the safe plain add.
    rows = mp._parse_reconcile_reply(
        '[{"action": "update"}, {"action": "delete"}, 42]', 3
    )
    assert rows == [{"action": "add"}, {"action": "add"}, {"action": "add"}]


def test_promote_update_decision_replaces_and_logs_undo(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    guide = write_guide(
        tmp_path / "CLAUDE.md",
        memory_entries=[
            "Insights model is deepseek-flash. [2026-01-01]",
            "Unrelated standing rule.",
        ],
    )
    proposal = mp.MemoryProposal(
        target="memory",
        text="Insights model is sonnet since the Ollama quota 429s.",
        source_section="Decisions",
    )
    decisions = {
        mp._decision_key(
            "memory", "Insights model is sonnet since the Ollama quota 429s."
        ): {
            "action": "update",
            "index": 1,
            "text": "Insights model is sonnet (moved off deepseek-flash: quota 429s).",
        }
    }
    remaining, promoted = mp.apply_proposals(
        [proposal], guide_path=guide, vault_root=vault, region_decisions=decisions
    )
    assert promoted and not remaining
    entries, _diags = mt.read_region(guide, "memory")
    assert len(entries) == 2  # replaced, not appended
    assert any("moved off deepseek-flash" in e for e in entries)
    assert all("Insights model is deepseek-flash" not in e for e in entries)
    undo = (vault / "Workspace" / "Memory-Consolidations.md").read_text(encoding="utf-8")
    assert "Insights model is deepseek-flash" in undo
    assert "ciao:memory" in undo


def test_promote_covered_decision_drops_the_fact(tmp_path: Path) -> None:
    guide = write_guide(
        tmp_path / "CLAUDE.md",
        memory_entries=["Prefers direct implementation over proposals."],
    )
    proposal = mp.MemoryProposal(
        target="memory",
        text="When the fix is clear, code it instead of drafting an issue.",
        source_section="User corrections",
    )
    decisions = {mp._decision_key("memory", proposal.text): {"action": "covered"}}
    remaining, promoted = mp.apply_proposals(
        [proposal], guide_path=guide, vault_root=tmp_path, region_decisions=decisions
    )
    # Covered: neither promoted nor left queued — same as an exact duplicate.
    assert promoted == [] and remaining == []
    entries, _diags = mt.read_region(guide, "memory")
    assert len(entries) == 1


def test_promote_malformed_update_degrades_to_append(tmp_path: Path) -> None:
    guide = write_guide(tmp_path / "CLAUDE.md", memory_entries=["Only entry."])
    proposal = mp.MemoryProposal(
        target="memory", text="A brand new durable fact.", source_section="Decisions"
    )
    decisions = {
        mp._decision_key("memory", proposal.text): {
            "action": "update",
            "index": 9,
            "text": "merged",
        }
    }
    remaining, promoted = mp.apply_proposals(
        [proposal], guide_path=guide, vault_root=tmp_path, region_decisions=decisions
    )
    assert promoted and not remaining
    entries, _diags = mt.read_region(guide, "memory")
    assert len(entries) == 2  # appended; nothing replaced
    assert "Only entry." in entries


def test_plan_region_reconcile_maps_facts_to_decisions(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio

    guide = write_guide(
        tmp_path / "CLAUDE.md",
        memory_entries=["Insights model is deepseek-flash."],
    )
    archive = tmp_path / "chat.md"
    archive.write_text(
        "# chat\n\nturns.\n\n## Session insights\n\n"
        "## User corrections\n"
        "- User said switch models. Durable rule: Insights model is sonnet. [idx=3] [memory]\n",
        encoding="utf-8",
    )

    async def fake_run_oneshot(prompt: str, **kwargs: object) -> str:
        assert "Insights model is deepseek-flash." in prompt
        assert "Insights model is sonnet." in prompt
        return '[{"action": "update", "index": 1, "text": "Insights model is sonnet."}]'

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_run_oneshot)

    decisions = asyncio.run(
        mp.plan_region_reconcile(archive, guide, model="sonnet")
    )
    assert decisions == {
        mp._decision_key("memory", "Insights model is sonnet."): {
            "action": "update",
            "index": 1,
            "text": "Insights model is sonnet.",
            # Snapshotted at plan time so the apply step can refuse the
            # update if the entry changed during the model call.
            "old": "Insights model is deepseek-flash.",
        }
    }


def test_plan_region_reconcile_failure_returns_none(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio

    guide = write_guide(
        tmp_path / "CLAUDE.md", memory_entries=["Existing entry."]
    )
    archive = tmp_path / "chat.md"
    archive.write_text(
        "# chat\n\nturns.\n\n## Session insights\n\n"
        "## User corrections\n"
        "- Durable rule: A new standing rule. [idx=3] [memory]\n",
        encoding="utf-8",
    )

    async def broken_run_oneshot(prompt: str, **kwargs: object) -> str:
        raise RuntimeError("backend down")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", broken_run_oneshot)

    assert asyncio.run(mp.plan_region_reconcile(archive, guide, model="sonnet")) is None


# ---- Structured learnings -------------------------------------------------


def test_append_learning_writes_structured_entry(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert mp.append_learning(
        vault, "Airtable sort param returns 400; filter by field ID.", source="chat-a1"
    )
    text = (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if l.startswith("- ["))
    match = mp._LEARNING_LINE_RE.match(line)
    assert match is not None
    assert match.group("count") == "1"
    assert match.group("first") == match.group("last")
    assert match.group("sources") == "chat-a1"


def test_append_learning_recurrence_increments_instead_of_duplicating(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    fact = "Airtable sort param returns 400; filter by field ID."
    assert mp.append_learning(vault, fact, source="chat-a1")
    assert mp.append_learning(vault, fact, source="chat-b2")
    # Whitespace/case variations still count as the same learning.
    assert mp.append_learning(vault, fact.upper(), source="chat-b2")

    text = (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.startswith("- [")]
    assert len(lines) == 1
    match = mp._LEARNING_LINE_RE.match(lines[0])
    assert match is not None
    assert match.group("count") == "3"
    assert match.group("sources") == "chat-a1, chat-b2"  # dedup'd source


def test_append_learning_leaves_legacy_bullets_alone(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = vault / "Workspace" / "Learnings.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# Learnings\n\n## Active\n- legacy plain learning bullet\n",
        encoding="utf-8",
    )
    assert mp.append_learning(vault, "legacy plain learning bullet")
    text = path.read_text(encoding="utf-8")
    assert text.count("legacy plain learning bullet") == 1  # exact dup short-circuit

    assert mp.append_learning(vault, "A brand new structured learning.", source="chat-x")
    text = path.read_text(encoding="utf-8")
    assert "- legacy plain learning bullet" in text
    assert "(x1) A brand new structured learning." in text


def test_already_applied_guard_scopes_negation_to_its_own_bullet(
    tmp_path: Path,
) -> None:
    """One "Never ..." bullet must not negate every fact below it.

    `_normalize_for_match` collapses the whole file to a single line, so the
    newline the guard used as its sentence boundary was gone by the time it
    looked — and a markdown bullet rarely ends in `.!?`. Every fact after the
    first negated bullet therefore read as negated, `_is_already_in_file`
    returned False for facts plainly present, and the proposals they came from
    were queued straight back into Review.
    """
    destination = tmp_path / "note.md"
    destination.write_text(
        "- Never commit secrets to the repository\n"
        "- The deploy script lives at scripts/deploy.sh and runs nightly\n"
        "- Staging mirrors production on the first Monday of the month\n",
        encoding="utf-8",
    )

    assert mp._is_already_in_file(
        destination, "The deploy script lives at scripts/deploy.sh and runs nightly"
    )
    assert mp._is_already_in_file(
        destination, "Staging mirrors production on the first Monday of the month"
    )
    # The negated bullet itself still reads as negated.
    assert not mp._is_already_in_file(
        destination, "Commit secrets to the repository"
    )


# ── accept_region_fact: the UI accept path's guards ───────────────────────
#
# Accepting a queued fact used to call `update_region(action="add")` directly,
# which skipped every guard the archive-time path applies. These pin what a
# click now gets — and, just as importantly, what it must NOT have acquired.


def _guide_with(tmp_path: Path, region: str, entries: list[str]) -> Path:
    from ciao.memory_tool import ensure_regions, write_region

    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Guide\n", encoding="utf-8")
    ensure_regions(guide)
    if entries:
        write_region(guide, region, entries)
    return guide


def _entries(guide: Path, region: str) -> list[str]:
    from ciao.memory_audit import strip_learned_stamp
    from ciao.memory_tool import read_region

    entries, _diags = read_region(guide, region)
    return [strip_learned_stamp(e) for e in entries]


def test_accept_refuses_event_shaped_text(tmp_path):
    """The guard that matters most: always-loaded context must hold rules.

    An event-shaped bullet used to be written verbatim, which is exactly the
    rot the shipped memory audit flags.
    """
    guide = _guide_with(tmp_path, "memory", [])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="The user asked me to check the logs and I found the bug.",
        vault_root=tmp_path,
    )
    assert outcome == "unshaped"
    assert _entries(guide, "memory") == []


def test_accept_writes_a_state_shaped_fact_with_a_stamp(tmp_path):
    guide = _guide_with(tmp_path, "memory", [])
    outcome, promotable = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers tabs over spaces.",
        vault_root=tmp_path,
    )
    assert outcome == "written"
    assert promotable == "Prefers tabs over spaces."
    assert _entries(guide, "memory") == ["Prefers tabs over spaces."]
    # The stamp the aging audit reads. Without it a UI-accepted fact was
    # invisible to re-verification forever.
    from ciao.memory_tool import read_region

    raw, _ = read_region(guide, "memory")
    assert re.search(r"\[\d{4}-\d{2}-\d{2}\]$", raw[0])


def test_accept_drops_a_stamp_stripped_duplicate(tmp_path):
    """The same fact accepted twice is one entry, whatever day it was."""
    guide = _guide_with(tmp_path, "memory", ["Prefers tabs over spaces. [2020-01-01]"])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers tabs over spaces.",
        vault_root=tmp_path,
    )
    assert outcome == "duplicate"
    assert _entries(guide, "memory") == ["Prefers tabs over spaces."]


def test_accept_still_writes_past_the_advisory_cap(tmp_path):
    """The cap is ADVISORY and must stay that way.

    `update_region` says so outright: enforcing it "made the accept button dead
    for 67 of 130 queued proposals" on a real vault, and
    tests/test_memory_tool.py pins that. Routing accept through the guarded
    write must not quietly reinstate the wall.
    """
    guide = _guide_with(tmp_path, "memory", ["x" * 5000])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers tabs over spaces.",
        vault_root=tmp_path,
    )
    assert outcome == "written"
    assert "Prefers tabs over spaces." in _entries(guide, "memory")


def test_accept_makes_no_model_call(tmp_path, monkeypatch):
    """A click must not block on a provider.

    Reconciliation would be welcome here, but one `run_oneshot` per row is a
    120s timeout each and the batch endpoint accepts rows sequentially inside a
    single request — 30 rows would be up to an hour. It stays out of this path.
    """
    async def must_not_run(prompt, **kwargs):
        raise AssertionError("the accept path must not call a model")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", must_not_run)
    guide = _guide_with(tmp_path, "memory", ["Prefers tabs over spaces. [2020-01-01]"])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers spaces over tabs.",
        vault_root=tmp_path,
    )
    assert outcome == "written"


def test_accept_applies_a_reconcile_decision_it_is_handed(tmp_path):
    """The seam a future reconcile pass writes through."""
    guide = _guide_with(tmp_path, "memory", ["Prefers tabs over spaces. [2020-01-01]"])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers spaces over tabs.",
        vault_root=tmp_path,
        decision={
            "action": "update",
            "index": 1,
            "text": "Prefers spaces over tabs.",
            "old": "Prefers tabs over spaces. [2020-01-01]",
        },
    )
    assert outcome == "written"
    assert _entries(guide, "memory") == ["Prefers spaces over tabs."]


def test_a_covered_verdict_with_no_vault_appends_rather_than_dropping(tmp_path):
    """Nothing is dropped without a trace — the standing contract.

    `covered` is a model verdict, not a provable match. With no vault to log it
    to, honour the contract instead of the verdict: a duplicate is visible and
    removable, a silently dropped fact is neither.
    """
    guide = _guide_with(tmp_path, "memory", ["Prefers tabs over spaces. [2020-01-01]"])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers spaces over tabs.",
        vault_root=None,
        decision={"action": "covered"},
    )
    assert outcome == "written"
    assert "Prefers spaces over tabs." in _entries(guide, "memory")


# -- Decision recording is append-only, except where it must be idempotent ----


def test_record_promotion_once_does_not_append_a_duplicate(tmp_path: Path) -> None:
    """The archive-time suppression verdict is re-derived on every pass.

    ``_is_already_applied`` recognises the same applied fact each time the same
    archive is re-processed, and recording per pass grew the decision history
    without recording anything new.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)

    assert mp.record_promotion(
        queue, text="A known fact.", kind="memory", via="auto",
        source="chat-1", outcome="suppressed", once=True,
    ) is True
    assert mp.record_promotion(
        queue, text="A known fact.", kind="memory", via="auto",
        source="chat-1", outcome="suppressed", once=True,
    ) is False

    rows = mp.read_decisions(queue)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "suppressed"


def test_record_promotion_once_still_records_a_different_outcome(tmp_path: Path) -> None:
    """A real promotion of a fact previously logged as "already known" is news."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)

    mp.record_promotion(
        queue, text="A fact.", kind="memory", via="auto", outcome="suppressed", once=True,
    )
    assert mp.record_promotion(
        queue, text="A fact.", kind="memory", via="pwa", once=True,
    ) is True

    assert [r["outcome"] for r in mp.read_decisions(queue)] == ["suppressed", ""]


def test_record_promotion_defaults_to_appending(tmp_path: Path) -> None:
    """Operator decisions are fresh events every time; only ``once`` dedupes."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)

    for _ in range(2):
        assert mp.record_promotion(queue, text="A fact.", kind="memory", via="pwa") is True

    assert len(mp.read_decisions(queue)) == 2


def test_read_decisions_numbers_rows_for_id_disambiguation(tmp_path: Path) -> None:
    """Two byte-identical rows must still get distinct history ids."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    log = mp.dismissed_log_path(queue)
    log.write_text('{"kind": "memory", "text": "Same."}\n' * 2, encoding="utf-8")

    rows = mp.read_decisions(queue)

    assert len({r["seq"] for r in rows}) == 2
    assert mp.history_row_id(rows[0], "personal") != mp.history_row_id(rows[1], "personal")
    # And the same row in two workspaces is two ids.
    assert mp.history_row_id(rows[0], "personal") != mp.history_row_id(rows[0], "work")


def test_legacy_row_ids_survive_a_new_decision(tmp_path: Path) -> None:
    """Appending to the current sidecar must not renumber the legacy log.

    ``seq`` used to be the row's position across the concatenated current +
    legacy sidecars, with the current file read first, so every new decision
    shifted the ids of all legacy rows.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    legacy = queue.with_suffix(".dismissed.log")
    legacy.write_text(
        '{"kind": "memory", "text": "Old one."}\n{"kind": "memory", "text": "Old two."}\n',
        encoding="utf-8",
    )

    before = {
        r["text"]: mp.history_row_id(r, "personal")
        for r in mp.read_decisions(queue)
    }
    assert mp.record_dismissal(queue, text="Brand new.", kind="memory", via="pwa") is True
    after = {
        r["text"]: mp.history_row_id(r, "personal")
        for r in mp.read_decisions(queue)
    }

    assert before["Old one."] == after["Old one."]
    assert before["Old two."] == after["Old two."]
