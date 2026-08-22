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
    # Only the state-shaped rule lands in the region, not the chat event.
    assert "Avoid em dashes; use commas instead." in mem_entries
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
    assert "tags: [person]" in note.read_text(encoding="utf-8")

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
