"""Tests for ``ciao.skill_proposals.learning_cleanup_eligibility``.

The question this answers is "may this learning be cleaned up?", and every test
here is a way the answer could wrongly be yes. It is the reconciliation hook, not
the reconciliation: nothing in the module it calls removes anything, and the
report is what lets a caller say *why* a learning was kept instead of leaving it
in silence.

A learning is eligible only when every finding filed against it — across every
proposal in its workspace, and through the ids of the records merged into it —
has been verified into the target or explicitly rejected, nothing on those
proposals is unattributable, and **its own line** still reads the way it did when
those findings were filed. The last clause is the one this file is really about:
the compare is per entry, so an unrelated edit elsewhere in the document leaves a
learning exactly as eligible as it was, while a reworded entry cancels it. Every
vault here is a throwaway one under ``tmp_path``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

from ciao import skill_proposals as sp
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.learning_records import (
    LEARNINGS_RELATIVE,
    LearningRecord,
    allocate_learning_id,
    entry_revision,
    parse_learnings,
    render_learning,
)
from ciao.memory_proposals import dismissed_log_path
from ciao.memory_receipts import content_revision, write_queue_atomically

RECEIPT = "mrcpt_0123456789abcdef"

#: The two Active statements every test works with. Two rather than one because
#: the claim under test is about the *other* entry leaving this one alone, and
#: that needs an other entry to exist.
FIRST_TEXT = "Blocked pages need a fallback."
SECOND_TEXT = "Long transcripts need chunking before they are read."

#: The legacy lines the identifiers are minted from, as the parser mints them:
#: over the exact entry text, not over the record. Written out so a reader can
#: see the derivation rather than infer it.
_FIRST_LEGACY = f"- {FIRST_TEXT}"
_SECOND_LEGACY = f"- {SECOND_TEXT}"
_PROMOTED_LEGACY = "- [shipped] already done"
_PROMOTED_TEXT = "An entry that is already done."


def _records(workspace: str) -> tuple[LearningRecord, LearningRecord, LearningRecord]:
    """The three records the fixture document holds, in document order.

    Ids are minted from the *legacy* line, which is what a real migration does
    and what makes the fixture honest: the document below carries the canonical
    lines, so every read returns these very records — same ids, same revisions —
    however many times it is parsed.
    """
    first = LearningRecord(
        learning_id=allocate_learning_id(workspace, _FIRST_LEGACY),
        key="blocked-pages",
        text=FIRST_TEXT,
    )
    second = LearningRecord(
        learning_id=allocate_learning_id(workspace, _SECOND_LEGACY),
        key="long-transcripts",
        text=SECOND_TEXT,
    )
    promoted = LearningRecord(
        learning_id=allocate_learning_id(workspace, _PROMOTED_LEGACY),
        key="shipped",
        text=_PROMOTED_TEXT,
        first_seen=date(2024, 1, 1),
        last_seen=date(2024, 1, 2),
        count=3,
    )
    return first, second, promoted


def _document(workspace: str = "personal") -> str:
    """The fixture document: two canonical Active entries and one promoted.

    Canonical rather than legacy on purpose. A legacy bullet's identifier is
    minted from its own text, so rewording one mints a *new* learning and the
    "the entry changed" tests would be testing the mint instead of the compare.
    A canonical line carries its identifier in the comment, which is exactly the
    property the revision compare depends on.
    """
    first, second, promoted = _records(workspace)
    return (
        "# Learnings\n"
        "\n"
        "## Active\n"
        "\n"
        f"{render_learning(first)}\n"
        f"{render_learning(second)}\n"
        "\n"
        "## Promoted / Resolved\n"
        "\n"
        f"{render_learning(promoted)}\n"
    )


def _config(tmp_path: Path, *names: str) -> CiaoConfig:
    """A registry whose workspaces' vaults live under ``tmp_path``."""
    workspaces = names or ("personal",)
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in workspaces
        },
    )


def _write_learnings(config: CiaoConfig, workspace: str, text: str) -> str:
    """Write the workspace's learnings document; return its *whole-file* revision.

    Whole-file, deliberately: it is the value a caller re-checks a write against
    and the value the cleanup receipt records. Eligibility compares the per-entry
    revision instead, which is what :func:`_read` hands the tests.
    """
    path = Path(config.workspace_vault_root(workspace)).joinpath(LEARNINGS_RELATIVE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return content_revision(text)


def _read(
    config: CiaoConfig, workspace: str, text: str | None = None
) -> LearningRecord:
    """The record the document currently holds for the lesson under test.

    Read through the shared parser, exactly as a caller reconciles it: a record
    the test built by hand would be testing the comparison against a fiction
    rather than against what the file says.
    """
    body = _document(workspace) if text is None else text
    _write_learnings(config, workspace, body)
    document = parse_learnings(body, workspace=workspace)
    for entry in document.entries:
        if entry.record is not None and entry.record.key == "blocked-pages":
            return entry.record
    raise AssertionError("the lesson under test is not in the document")


def _origin(
    learning: LearningRecord,
    finding: str = "add the defuddle fallback",
    *,
    workspace: str = "personal",
    revision: str = "",
    state: str = sp.ORIGIN_PENDING,
    verification: str = "",
) -> sp.SkillOrigin:
    return sp.SkillOrigin(
        workspace=workspace,
        learning_id=learning.learning_id,
        source_revision=revision,
        finding=finding,
        summary="Add the defuddle fallback step.",
        state=state,
        verification=verification,
    )


def _proposal(
    skill: str,
    origins: tuple[sp.SkillOrigin, ...],
    workspace: str = "personal",
    *,
    lifecycle: str = sp.PENDING,
) -> sp.SkillProposal:
    return sp.SkillProposal(
        id=sp.proposal_id(workspace, skill),
        workspace=workspace,
        skill=skill,
        canonical_path=f"/agent/skills/{skill}/SKILL.md",
        reviewed_revision="a" * 64,
        title=f"Skill reflection: {skill}",
        problem="Repeated fetch failures.",
        change="Add a defuddle fallback.",
        rationale="It handles blocked pages.",
        sources=(
            sp.SkillEvidence(
                chat_id="sess-a1",
                archive="2026-08-09T10:00:00Z",
                turn="",
                excerpt="outcome=needs_review corrections=1 errors=0 turns=3",
            ),
        ),
        lifecycle=lifecycle,
        chat_id="",
        updated_at="2026-08-09T10:00:00Z",
        origins=origins,
    )


def _file(config: CiaoConfig, proposal: sp.SkillProposal) -> sp.SkillProposal:
    """Put this record in the queue, decisions and all.

    Written rather than merged, because ``upsert_proposal`` files every origin it
    is handed as ``pending`` — a finding is a question, and only a settlement
    writes a state. The states below are the ones a settlement produces
    (``already_covered``, ``failed``, a receipt-carrying ``applied``), so this is
    the shape a settled record reaches disk in, and it leaves every assertion
    here about the fold rather than about how the record got there.
    """
    write_queue_atomically(
        sp.proposal_path(config, proposal.workspace, proposal.skill),
        sp.render_proposal(proposal),
    )
    return proposal


def _applied(
    learning: LearningRecord,
    revision: str = "",
    finding: str = "add the defuddle fallback",
    *,
    workspace: str = "personal",
) -> sp.SkillOrigin:
    """A finding somebody verified into the target: the one shape that clears a
    learning on its own."""
    return _origin(
        learning,
        finding,
        workspace=workspace,
        revision=revision,
        state=sp.ORIGIN_APPLIED,
        verification=RECEIPT,
    )


def _eligible(
    config: CiaoConfig, learning: LearningRecord, *, current_revision: str = ""
) -> bool:
    """The one answer every test here is about, asked the ordinary way."""
    return bool(
        sp.learning_cleanup_eligibility(
            config, "personal", learning, current_revision=current_revision
        )["eligible"]
    )


# -- the happy path ---------------------------------------------------------


def test_a_learning_with_every_finding_decided_is_eligible(tmp_path: Path) -> None:
    """The only shape that qualifies: both findings answered, one verified into
    the target and one rejected by a person, and the learnings document still
    reading the way it did when they were filed."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    revision = entry_revision(learning)
    stored = _file(
        config,
        _proposal(
            "web-research",
            (
                _applied(learning, revision),
                _origin(
                    learning,
                    "read the file back",
                    revision=revision,
                    state=sp.ORIGIN_DISMISSED,
                ),
            ),
        ),
    )
    assert stored.lifecycle == sp.PENDING

    report = sp.learning_cleanup_eligibility(config, "personal", learning)

    assert report["eligible"] is True
    assert report["learning_id"] == learning.learning_id
    assert report["workspace"] == "personal"
    assert [origin["state"] for origin in report["origins"]] == [
        sp.ORIGIN_APPLIED,
        sp.ORIGIN_DISMISSED,
    ]
    assert report["origins"][0]["verification"] == RECEIPT


def test_the_whole_file_revision_is_still_readable_and_still_the_files(
    tmp_path: Path,
) -> None:
    """``learnings_revision`` keeps its old job, and it is not this hook's job.

    A caller re-checking the write it is about to make needs the revision of the
    document it is replacing, which is the whole file. Keeping the function and
    narrowing only the comparison is what lets an install that already filed
    whole-file revisions keep them readable without anything rewriting them.
    """
    config = _config(tmp_path)
    learning = _read(config, "personal")
    _file(config, _proposal("web-research", (_applied(learning, entry_revision(learning)),)))

    whole = sp.learnings_revision(config, "personal")

    assert whole == content_revision(_document("personal"))
    assert whole != entry_revision(learning)
    # An origin that recorded the whole-file revision — the shape every install
    # has on disk today — matches no entry and is therefore kept.
    _file(
        config,
        _proposal(
            "web-research",
            (_applied(learning, whole),),
        ),
    )
    report = sp.learning_cleanup_eligibility(config, "personal", learning)
    assert report["eligible"] is False
    assert "changed since" in report["reason"]


def test_a_caller_may_pass_the_revision_it_read_under_its_own_lock(
    tmp_path: Path,
) -> None:
    """The hook answers against the bytes the caller is holding, not a re-read.

    ``apply_cleanup`` parses the file *inside* the write lock and then asks this
    question, so the revision it compares against is the one it is about to write
    over. If the hook re-read the vault instead, that check would be against a
    document the caller never held and a concurrent write would slip between the
    two."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    _file(config, _proposal("web-research", (_applied(learning, entry_revision(learning)),)))

    # The file now says something else entirely, but the caller is holding these
    # bytes and the answer has to be about them.
    _write_learnings(config, "personal", "# Learnings\n\n## Active\n\n- unrelated\n")

    report = sp.learning_cleanup_eligibility(
        config, "personal", learning, current_revision=entry_revision(learning)
    )

    assert report["eligible"] is True


# -- every way the answer can wrongly be yes --------------------------------


def test_one_outstanding_finding_keeps_the_learning(tmp_path: Path) -> None:
    """A learning split across two findings is dealt with only when both are.
    Anything else retires a lesson on half the evidence."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    revision = entry_revision(learning)
    _file(
        config,
        _proposal(
            "web-research",
            (
                _applied(learning, revision),
                _origin(learning, "read the file back", revision=revision),
            ),
        ),
    )

    report = sp.learning_cleanup_eligibility(config, "personal", learning)

    assert report["eligible"] is False
    assert "not settled" in report["reason"]


def test_a_learning_split_across_two_proposals_needs_both(tmp_path: Path) -> None:
    """Two records, one learning, one answer each. The fold has to read the
    settled one too — a learning is not dealt with because the row that happened
    to still be queued was dealt with."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    revision = entry_revision(learning)
    first = _file(config, _proposal("web-research", (_applied(learning, revision),)))
    sp.settle_proposal(config, first.id, sp.APPLIED, verification=RECEIPT)
    _file(
        config,
        _proposal(
            "humanizer",
            (_origin(learning, "and here too", revision=revision),),
        ),
    )

    assert not _eligible(config, learning)

    sp.settle_proposal(
        config,
        sp.proposal_id("personal", "humanizer"),
        sp.DISMISSED,
    )
    report = sp.learning_cleanup_eligibility(config, "personal", learning)
    assert report["eligible"] is True
    assert {origin["skill"] for origin in report["origins"]} == {
        "web-research",
        "humanizer",
    }


def test_a_reworded_entry_cancels_eligibility(tmp_path: Path) -> None:
    """The learning itself moved after the finding was filed, so the finding was
    written against bytes that are no longer there. Re-read it before doing
    anything to it."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    filed_against = entry_revision(learning)
    _file(config, _proposal("web-research", (_applied(learning, filed_against),)))
    reworded = replace(learning, text=f"{FIRST_TEXT} of some kind.")
    moved = _read(
        config,
        "personal",
        _document("personal").replace(
            render_learning(learning), render_learning(reworded)
        ),
    )
    # The identifier survives the rewording — it lives in the comment — so this
    # is the same learning with a different line, not a different learning. The
    # fold therefore finds the finding, and only the compare can keep the lesson.
    assert moved.learning_id == learning.learning_id
    assert entry_revision(moved) != filed_against

    report = sp.learning_cleanup_eligibility(config, "personal", moved)

    assert report["eligible"] is False
    assert "changed since" in report["reason"]


def test_an_unrelated_edit_does_not_cancel_eligibility(tmp_path: Path) -> None:
    """The whole point of hashing the entry rather than the file.

    A learnings document is one workspace's running notes. A new lesson filed
    below this one, a neighbour reworded, a finding filed against a *different*
    learning, the cleanup pass splicing out an unrelated line — every one of those
    rewrites the file and leaves this entry exactly as it was, so every one of
    them has to leave this learning exactly as eligible as it was. Comparing the
    whole file is what made a busy document permanently ineligible, and it is also
    what made two findings filed at different moments unable to both match.
    """
    config = _config(tmp_path)
    learning = _read(config, "personal")
    _file(config, _proposal("web-research", (_applied(learning, entry_revision(learning)),)))
    # Same entry, byte for byte. Everything around it is different.
    _, second, _ = _records("personal")
    extra = LearningRecord(
        learning_id=allocate_learning_id("personal", "- Rate limits are per-endpoint."),
        key="rate-limits",
        text="Rate limits are per-endpoint.",
    )
    grown = _document("personal").replace(
        f"{render_learning(second)}\n",
        f"{render_learning(second)}\n{render_learning(extra)}\n",
    )
    assert _read(config, "personal", grown) == learning

    report = sp.learning_cleanup_eligibility(config, "personal", learning)

    assert sp.learnings_revision(config, "personal") != content_revision(
        _document("personal")
    )
    assert report["eligible"] is True
    assert report["learning_id"] == learning.learning_id


def test_editing_a_neighbouring_entry_does_not_cancel_eligibility(
    tmp_path: Path,
) -> None:
    """The other half of the same claim, with the neighbour *edited* rather than
    appended to. A reworded neighbour gets a new id, so this also pins that the
    fold is not reaching across entries by key or by position."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    _file(config, _proposal("web-research", (_applied(learning, entry_revision(learning)),)))
    _, neighbour, _ = _records("personal")
    reworded_neighbour = replace(neighbour, text=f"{SECOND_TEXT} first.")
    body = _document("personal").replace(
        render_learning(neighbour), render_learning(reworded_neighbour)
    )
    _read(config, "personal", body)

    assert entry_revision(reworded_neighbour) != entry_revision(neighbour)
    assert sp.learning_cleanup_eligibility(config, "personal", learning)["eligible"]


def test_an_origin_with_no_recorded_revision_is_ineligible(tmp_path: Path) -> None:
    """Nothing can show what was filed against is unchanged, and "cannot show" is
    the same answer as "changed" for a question about deleting somebody's notes."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    _file(config, _proposal("web-research", (_applied(learning),)))

    report = sp.learning_cleanup_eligibility(config, "personal", learning)

    assert report["eligible"] is False
    assert "recorded no revision" in report["reason"]


def test_an_unattributable_finding_keeps_every_learning_on_the_record(
    tmp_path: Path,
) -> None:
    """An origin naming no learning could be the other half of a learning the
    record does name. Counting the one we can read and ignoring the one we cannot
    is how a learning is retired on the strength of half its findings."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    revision = entry_revision(learning)
    _file(
        config,
        _proposal(
            "web-research",
            (
                _applied(learning, revision),
                sp.SkillOrigin(finding="something this queue could not read"),
            ),
        ),
    )

    report = sp.learning_cleanup_eligibility(config, "personal", learning)

    assert report["eligible"] is False
    assert "name no learning" in report["reason"]


def test_a_learning_nothing_links_is_ineligible(tmp_path: Path) -> None:
    """The legacy case, and the reason an unlinked proposal is safe: a record
    filed before origins existed has nothing to fold, so a learning behind it was
    never covered and never got dealt with."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    _file(config, _proposal("web-research", ()))
    _file(config, _proposal("humanizer", ()))

    report = sp.learning_cleanup_eligibility(config, "personal", learning)

    assert report["eligible"] is False
    assert report["origins"] == []
    assert "no proposal links" in report["reason"]


def test_a_reopened_finding_cancels_eligibility(tmp_path: Path) -> None:
    """A record that is asking again is asking again, whatever the decision said
    last time. The fold reads the records, not the decision sidecar, so a
    question that came back is visible to it — and the queue's own rule is that
    a settled record stays settled until the decision itself is gone, which is
    the case exercised here."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    revision = entry_revision(learning)
    stored = _file(config, _proposal("web-research", (_applied(learning, revision),)))
    sp.settle_proposal(config, stored.id, sp.APPLIED, verification=RECEIPT)
    assert _eligible(config, learning)

    # The record and its decision both go: a user clearing their vault, a sync
    # conflict. What comes back is a pending record, and a pending record is a
    # finding nobody has answered.
    sp.proposal_path(config, "personal", "web-research").unlink()
    dismissed_log_path(
        tmp_path / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
    ).unlink()
    _file(
        config,
        _proposal(
            "web-research",
            (_origin(learning, revision=revision),),
        ),
    )

    report = sp.learning_cleanup_eligibility(config, "personal", learning)
    assert report["eligible"] is False
    assert "not settled" in report["reason"]


def test_a_chat_working_on_the_finding_cancels_eligibility(tmp_path: Path) -> None:
    """Accepting a row starts one chat over its findings. While it runs, the
    record says ``implementing`` and the learning stays Active — a chat having
    started is not the lesson being in the skill."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    revision = entry_revision(learning)
    stored = _file(
        config,
        _proposal(
            "web-research",
            (_origin(learning, revision=revision),),
        ),
    )

    sp.mark_implementing(config, stored.id, "chat-9")

    report = sp.learning_cleanup_eligibility(config, "personal", learning)
    assert report["eligible"] is False
    assert "implementing" in report["reason"]


def test_an_unconfirmed_already_covered_keeps_the_learning(tmp_path: Path) -> None:
    """"The skill already says this" is a claim about the target that only
    somebody reading the target can confirm. It is the state most likely to be
    recorded by a well-meaning pass, and the one that must not clear a lesson."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    revision = entry_revision(learning)
    _file(
        config,
        _proposal(
            "web-research",
            (_origin(learning, revision=revision, state=sp.ORIGIN_ALREADY_COVERED),),
        ),
    )

    report = sp.learning_cleanup_eligibility(config, "personal", learning)

    assert report["eligible"] is False
    assert "already_covered" in report["reason"]


# -- aliases ----------------------------------------------------------------


def test_a_merged_learning_is_covered_by_the_links_filed_against_both_ids(
    tmp_path: Path,
) -> None:
    """``aliases`` are the ids of the records merged into this one. A finding
    filed against an id that has since been absorbed is still a finding about
    this learning, and dropping it would let the merge retire a lesson on the
    strength of the other half."""
    config = _config(tmp_path)
    learning = _read(config, "personal")
    absorbed = allocate_learning_id("personal", _SECOND_LEGACY)
    revision = entry_revision(learning)
    _file(config, _proposal("web-research", (_applied(learning, revision),)))
    _file(
        config,
        _proposal(
            "humanizer",
            (
                _origin(
                    replace(learning, learning_id=absorbed),
                    "and here too",
                    revision=revision,
                ),
            ),
        ),
    )
    merged = replace(learning, aliases=(absorbed,))

    # Without the alias the second proposal's link is invisible, which is the
    # point: the merge is what tells the fold that the absorbed id was this
    # learning, and until it says so the other half of the story is missing.
    assert _eligible(config, learning)
    assert not _eligible(config, merged)

    sp.settle_proposal(
        config, sp.proposal_id("personal", "humanizer"), sp.DISMISSED
    )
    report = sp.learning_cleanup_eligibility(config, "personal", merged)
    assert len(report["origins"]) == 2
    # Both findings are now answered, and the learning is still kept: a merge is
    # itself a change to the entry. `aliases` live in the canonical line, so the
    # merged record has a new revision and every finding filed before the merge
    # reads as "changed since". That is the fail-safe direction, and it is the
    # right one — a merge is a decision about what this learning *is*, and the
    # finding behind it was written against the other half. Re-filing the finding
    # against the merged record is what settles it, and nothing is backfilled to
    # avoid that.
    assert report["eligible"] is False
    assert "changed since" in report["reason"]

    # Re-filed against the merged record, the same findings clear it with no
    # other change: the revision is the only thing that was holding it.
    now = entry_revision(merged)
    _file(
        config,
        _proposal("web-research", (_applied(merged, now),)),
    )
    _file(
        config,
        _proposal(
            "humanizer",
            (
                _origin(
                    merged,
                    "and here too",
                    revision=now,
                    state=sp.ORIGIN_DISMISSED,
                ),
            ),
        ),
    )
    assert sp.learning_cleanup_eligibility(config, "personal", merged)["eligible"]


def test_an_alias_from_another_workspace_is_not_folded_in(tmp_path: Path) -> None:
    """Ids are workspace-scoped, so an alias is only ever another id from the
    same workspace. A link filed under a same-spelled learning in a second
    workspace is not one of this one's findings, and the second workspace's
    answer says nothing about the first."""
    config = _config(tmp_path, "personal", "work")
    personal = _read(config, "personal")
    work = _read(config, "work")
    revision = entry_revision(work)
    _file(
        config,
        _proposal(
            "web-research",
            (_applied(work, revision, workspace="work"),),
            workspace="work",
        ),
    )
    folded = replace(personal, aliases=(work.learning_id,))

    assert not sp.learning_cleanup_eligibility(config, "personal", folded)["eligible"]
    assert sp.learning_cleanup_eligibility(config, "work", work)["eligible"] is True
