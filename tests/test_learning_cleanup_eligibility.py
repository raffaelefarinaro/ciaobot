"""Tests for ``ciao.skill_proposals.learning_cleanup_eligibility``.

The question this answers is "may this learning be cleaned up?", and every test
here is a way the answer could wrongly be yes. It is the reconciliation hook, not
the reconciliation: nothing in the module it calls removes anything, and the
report is what lets a caller say *why* a learning was kept instead of leaving it
in silence.

A learning is eligible only when every finding filed against it — across every
proposal in its workspace, and through the ids of the records merged into it —
has been verified into the target or explicitly rejected, nothing on those
proposals is unattributable, and the ``Learnings.md`` they were filed against
still reads the way it did. Every vault here is a throwaway one under
``tmp_path``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ciao import skill_proposals as sp
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.learning_records import (
    LEARNINGS_RELATIVE,
    LearningRecord,
    allocate_learning_id,
)
from ciao.memory_proposals import dismissed_log_path
from ciao.memory_receipts import content_revision, write_queue_atomically

RECEIPT = "mrcpt_0123456789abcdef"


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
    """Write the workspace's learnings document; return its revision."""
    path = Path(config.workspace_vault_root(workspace)).joinpath(LEARNINGS_RELATIVE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return content_revision(text)


def _learning(workspace: str = "personal") -> LearningRecord:
    """One learning, with a genuinely workspace-scoped identifier."""
    return LearningRecord(
        learning_id=allocate_learning_id(workspace, "Blocked pages need a fallback."),
        key="blocked-pages",
        text="Blocked pages need a fallback.",
    )


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
    learning = _learning()
    revision = _write_learnings(config, "personal", "# Learnings\n\n- one\n")
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


def test_the_revision_is_read_from_the_vault_when_the_caller_does_not_pass_it(
    tmp_path: Path,
) -> None:
    """A caller that has not read the document should not have to: the hook
    answers the same question either way, so one implementation of "unchanged
    since filing" is what a report can rely on."""
    config = _config(tmp_path)
    learning = _learning()
    revision = _write_learnings(config, "personal", "# Learnings\n\n- one\n")
    _file(config, _proposal("web-research", (_applied(learning, revision),)))

    assert sp.learnings_revision(config, "personal") == revision
    assert sp.learning_cleanup_eligibility(config, "personal", learning)["eligible"]


# -- every way the answer can wrongly be yes --------------------------------


def test_one_outstanding_finding_keeps_the_learning(tmp_path: Path) -> None:
    """A learning split across two findings is dealt with only when both are.
    Anything else retires a lesson on half the evidence."""
    config = _config(tmp_path)
    learning = _learning()
    revision = _write_learnings(config, "personal", "# Learnings\n\n- one\n")
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
    learning = _learning()
    revision = _write_learnings(config, "personal", "# Learnings\n\n- one\n")
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


def test_a_changed_source_revision_cancels_eligibility(tmp_path: Path) -> None:
    """The learning moved after the finding was filed, so the finding was
    written against bytes that are no longer there. Re-read it before doing
    anything to it."""
    config = _config(tmp_path)
    learning = _learning()
    filed_against = _write_learnings(config, "personal", "# Learnings\n\n- one\n")
    _file(config, _proposal("web-research", (_applied(learning, filed_against),)))
    _write_learnings(config, "personal", "# Learnings\n\n- one\n- two\n")

    report = sp.learning_cleanup_eligibility(config, "personal", learning)

    assert report["eligible"] is False
    assert "changed since" in report["reason"]


def test_an_origin_with_no_recorded_revision_is_ineligible(tmp_path: Path) -> None:
    """Nothing can show what was filed against is unchanged, and "cannot show" is
    the same answer as "changed" for a question about deleting somebody's notes."""
    config = _config(tmp_path)
    learning = _learning()
    _write_learnings(config, "personal", "# Learnings\n\n- one\n")
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
    learning = _learning()
    revision = _write_learnings(config, "personal", "# Learnings\n\n- one\n")
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
    learning = _learning()
    _write_learnings(config, "personal", "# Learnings\n\n- one\n")
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
    learning = _learning()
    revision = _write_learnings(config, "personal", "# Learnings\n\n- one\n")
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
    learning = _learning()
    revision = _write_learnings(config, "personal", "# Learnings\n\n- one\n")
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
    learning = _learning()
    revision = _write_learnings(config, "personal", "# Learnings\n\n- one\n")
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
    learning = _learning()
    absorbed = allocate_learning_id("personal", "Blocked pages need a fallback!")
    revision = _write_learnings(config, "personal", "# Learnings\n\n- one\n")
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
    assert report["eligible"] is True
    assert len(report["origins"]) == 2


def test_an_alias_from_another_workspace_is_not_folded_in(tmp_path: Path) -> None:
    """Ids are workspace-scoped, so an alias is only ever another id from the
    same workspace. A link filed under a same-spelled learning in a second
    workspace is not one of this one's findings, and the second workspace's
    answer says nothing about the first."""
    config = _config(tmp_path, "personal", "work")
    personal = _learning("personal")
    work = _learning("work")
    revision = _write_learnings(config, "personal", "# Learnings\n\n- one\n")
    _write_learnings(config, "work", "# Learnings\n\n- one\n")
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
