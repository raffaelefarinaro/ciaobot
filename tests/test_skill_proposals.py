"""Tests for ``ciao.skill_proposals``, the skill-proposal queue's owner.

The behaviour pinned here is the queue's, not the file format's: one stable
identity per skill, an evidence merge that neither drops what the last pass
found nor double-counts what this pass re-read, a re-run over unchanged evidence
that writes nothing, and a settlement that outlives the row it settles. Every
vault here is a throwaway one under ``tmp_path``.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import replace
from pathlib import Path

import pytest

from ciao import skill_proposals
from ciao import skill_proposals as sp
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.learning_records import LearningRecord, allocate_learning_id
from ciao.memory_proposals import read_decisions

#: Two real minted ids rather than two made-up strings: the point of the
#: workspace scoping is that ``allocate_learning_id`` puts the workspace in the
#: name, and a hand-written uuid would not test that.
STATEMENT = "Blocked pages need a defuddle fallback."
LEARNING_ID = allocate_learning_id("personal", STATEMENT)
OTHER_LEARNING_ID = allocate_learning_id("personal", "Read it back after editing.")
WORK_LEARNING_ID = allocate_learning_id("work", STATEMENT)

#: The revision ``Workspace/Learnings.md`` had when these findings were filed.
LEARNINGS_REVISION = "b" * 64


def _learning(learning_id: str = LEARNING_ID) -> LearningRecord:
    """The learning these findings were filed against."""
    return LearningRecord(
        learning_id=learning_id, key="blocked-pages", text=STATEMENT
    )


def _work_learning() -> LearningRecord:
    return _learning(WORK_LEARNING_ID)


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


def _proposal(
    workspace: str = "personal",
    skill: str = "web-research",
    **overrides: object,
) -> sp.SkillProposal:
    """A record as the pass would build it, with overridable fields."""
    fields: dict[str, object] = {
        "id": sp.proposal_id(workspace, skill),
        "workspace": workspace,
        "skill": skill,
        "canonical_path": "/agent/skills/web-research/SKILL.md",
        "reviewed_revision": "a" * 64,
        "title": "Skill reflection: web-research",
        "problem": "Repeated fetch failures.",
        "change": "Add a defuddle fallback.",
        "rationale": "It handles blocked pages.",
        "sources": (
            sp.SkillEvidence(
                chat_id="sess-a1",
                archive="2026-08-09T10:00:00Z",
                turn="",
                excerpt="outcome=needs_review corrections=1 errors=0 turns=3",
            ),
        ),
        "lifecycle": sp.PENDING,
        "chat_id": "",
        "updated_at": "2026-08-09T10:00:00Z",
    }
    fields.update(overrides)
    return sp.SkillProposal(**fields)  # type: ignore[arg-type]


LEGACY_FILE = """\
---
type: skill-proposal
skill: 2026-05-20-defuddle
status: draft
generated: 2026-05-20T09:00:00Z
trajectories: 2
semantic_check: PRESERVED
---

# Skill reflection: 2026-05-20-defuddle

This is a reviewable suggestion based on repeated recent use. Nothing has been
changed automatically.

## What I noticed

Repeated fetch failures.

## Suggested improvement

Add a defuddle fallback.

## Proposed edit

```diff
+fallback
```

## Why this should help

It handles blocked pages.

## Technical details

- **Skill:** `/agent/skills/defuddle/SKILL.md`

### Source sessions

- 20260520 outcome=needs_review corrections=1 errors=0 turns=3
- 20260521 outcome=needs_review corrections=2 errors=1 turns=4
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- identity --------------------------------------------------------------


def test_identity_is_derived_from_the_workspace_and_the_skill() -> None:
    assert sp.proposal_id("personal", "web-research") == sp.proposal_id(
        "personal", "web-research"
    )
    # Two workspaces holding the same skill are two different proposals: they
    # have separate queues, separate vaults, and separate decisions.
    assert sp.proposal_id("personal", "web-research") != sp.proposal_id(
        "work", "web-research"
    )
    assert sp.proposal_id("personal", "web-research") != sp.proposal_id(
        "personal", "humanizer"
    )


def test_the_queue_lives_where_the_loose_files_already_did(tmp_path: Path) -> None:
    """Unchanged on purpose. A proposal a user already has must not move: the
    review surface, the CLI, the packaged curation skill and the pass all point
    at this path."""
    config = _config(tmp_path)
    assert sp.queue_dir(config, "personal") == (
        tmp_path / "memory-vault" / "personal" / "Workspace" / "Skill-Proposals"
    )
    assert sp.proposal_path(config, "personal", "defuddle") == (
        sp.queue_dir(config, "personal") / "defuddle.md"
    )


def test_a_queue_file_is_named_by_a_plain_skill_name(tmp_path: Path) -> None:
    """A name carrying a separator would address a second directory, and this
    module owns where a queue file may be written."""
    config = _config(tmp_path)
    with pytest.raises(ValueError, match="one proposal file"):
        sp.proposal_path(config, "personal", "../../elsewhere")
    with pytest.raises(ValueError, match="one proposal file"):
        sp.proposal_path(config, "personal", "a/b")
    with pytest.raises(ValueError, match="not a skill proposal name"):
        sp.proposal_path(config, "personal", "..")
    with pytest.raises(ValueError, match="not a skill proposal name"):
        sp.proposal_path(config, "personal", "")


# -- legacy files ----------------------------------------------------------


def test_a_legacy_file_parses_into_a_pending_record_without_a_rewrite(
    tmp_path: Path,
) -> None:
    """The pre-#683 files have no versioned frontmatter. They must read: a
    proposal a user already has is not something an upgrade gets to hide from
    the queue it was filed in, and nothing about reading one may rewrite it."""
    config = _config(tmp_path)
    path = _write(sp.queue_dir(config, "personal") / "2026-05-20-defuddle.md", LEGACY_FILE)
    before = path.read_bytes()

    record = sp.parse_proposal(path, "personal")

    assert record is not None
    assert record.lifecycle == sp.PENDING
    assert record.id == sp.proposal_id("personal", "2026-05-20-defuddle")
    assert record.title == "Skill reflection: 2026-05-20-defuddle"
    # The old headings fold into the record's own fields.
    assert record.problem == "Repeated fetch failures."
    assert record.change.startswith("Add a defuddle fallback.")
    assert "+fallback" in record.change
    assert record.rationale.startswith("It handles blocked pages.")
    # And the session table became evidence, one row per session.
    assert [item.chat_id for item in record.sources] == ["20260520", "20260521"]
    assert path.read_bytes() == before


def test_a_legacy_file_is_still_listed_as_pending(tmp_path: Path) -> None:
    """The queue, not just the parser.

    The old writer is gone for good — the weekly skill-evolution pass that
    produced these files was retired in #697 — so every loose file left in a
    user's queue is permanent. If `read_queue` stopped listing one, the review
    page and the nightly curation worklist would both silently lose a finding
    the user is still being asked about. It has to keep reading, and it has to
    keep reading it as open.
    """
    config = _config(tmp_path)
    _write(sp.queue_dir(config, "personal") / "2026-05-20-defuddle.md", LEGACY_FILE)
    sp.upsert_proposal(config, _proposal(skill="web-research"))

    queued = sp.read_queue(config, "personal")

    assert [item.skill for item in queued] == ["2026-05-20-defuddle", "web-research"]
    assert all(item.lifecycle == sp.PENDING for item in queued)
    assert queued[0].id == sp.proposal_id("personal", "2026-05-20-defuddle")


def test_a_legacy_file_keeps_the_words_it_had_no_field_for(tmp_path: Path) -> None:
    """The acceptance test for a readable legacy record: merging into one loses
    none of the words the old file had no field for. The preamble and the
    technical-details block describe the finding, but they are not the finding,
    and there is no field that is them — so they ride along in the rationale
    rather than being dropped on the first write and gone for good on the next."""
    config = _config(tmp_path)
    path = _write(sp.queue_dir(config, "personal") / "2026-05-20-defuddle.md", LEGACY_FILE)
    assert sp.parse_proposal(path, "personal") is not None

    stored = sp.upsert_proposal(
        config,
        # A pass that supplied no rationale of its own, so the stored words are
        # still the ones on record.
        _proposal(
            skill="2026-05-20-defuddle",
            problem="Repeated fetch failures, and then some.",
            rationale="",
            sources=(),
        ),
    )

    rewritten = path.read_text(encoding="utf-8")
    assert "Repeated fetch failures, and then some." in rewritten
    assert "It handles blocked pages." in rewritten
    assert "reviewable suggestion based on repeated recent use" in rewritten
    assert "**Skill:**" in rewritten
    # The old session table became evidence, so its rows are still there.
    assert "outcome=needs_review corrections=2 errors=1 turns=4" in rewritten
    # And the result is a record, so a second read is the same finding.
    again = sp.parse_proposal(path, "personal")
    assert again == stored
    assert again is not None
    assert [item.chat_id for item in again.sources] == ["20260520", "20260521"]


def test_an_unreadable_file_is_no_record_rather_than_an_error(tmp_path: Path) -> None:
    config = _config(tmp_path)
    queue = sp.queue_dir(config, "personal")
    assert sp.parse_proposal(queue / "absent.md", "personal") is None
    _write(queue / "empty.md", "   \n")
    assert sp.parse_proposal(queue / "empty.md", "personal") is None
    (queue / "broken.md").write_bytes(b"---\nskill: broken\n---\n\xff\xfe not utf-8")
    assert sp.parse_proposal(queue / "broken.md", "personal") is None


def test_the_record_round_trips_through_its_own_rendering(tmp_path: Path) -> None:
    """``render_proposal`` is a pure function of the record and ``parse`` reads
    it back: the no-op merge compares rendered bytes with the file, so a renderer
    that lost a field would make every re-run look like a change."""
    config = _config(tmp_path)
    record = _proposal()
    path = sp.proposal_path(config, "personal", "web-research")

    _write(path, sp.render_proposal(record))
    assert sp.parse_proposal(path, "personal") == record


def test_a_record_with_an_unnamed_body_keeps_it(tmp_path: Path) -> None:
    """A model answer that ignored the prompt's headings is still a finding. It
    has no field, so it is carried rather than dropped — dropping it here is how
    the pass's whole output used to vanish."""
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")

    stored = sp.upsert_proposal(
        config, _proposal(problem="", change="", rationale="", sources=())
    )
    assert stored.problem == ""

    _write(path, "# Skill reflection\n\nSuggested edit: explain defuddle.\n")
    record = sp.parse_proposal(path, "personal")
    assert record is not None
    assert record.title == "Skill reflection"
    assert "Suggested edit: explain defuddle." in record.rationale


# -- merging ---------------------------------------------------------------


def test_upsert_merges_new_evidence_without_changing_identity(tmp_path: Path) -> None:
    """The weekly pass re-reads the same skill every run and each run saw a
    different set of sessions. The old writer rendered Markdown and wrote it over
    the last run's, so the queue could not say which sessions had now been seen
    and one run's findings simply vanished."""
    config = _config(tmp_path)
    first = sp.upsert_proposal(config, _proposal())
    second = sp.upsert_proposal(
        config,
        _proposal(
            problem="Also, the blocked-page fallback times out.",
            sources=(
                sp.SkillEvidence(
                    chat_id="sess-b2",
                    archive="2026-08-16T10:00:00Z",
                    turn="",
                    excerpt="outcome=needs_review corrections=2 errors=1 turns=4",
                ),
            ),
            updated_at="2026-08-16T10:00:00Z",
        ),
    )

    assert first.skill == second.skill
    assert first.id == second.id
    assert [item.chat_id for item in second.sources] == ["sess-a1", "sess-b2"]
    assert second.problem == "Also, the blocked-page fallback times out."
    assert second.change == first.change      # unrelated fields untouched
    assert second.rationale == first.rationale


def test_reprocessing_the_same_evidence_is_a_no_op(tmp_path: Path) -> None:
    """Not merely the same result: the file is not rewritten. A pass that saw
    exactly what the last one saw must leave the bytes AND the timestamp alone,
    or every weekly run would touch the queue and a diff could never tell a real
    change from a re-run."""
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")
    sp.upsert_proposal(config, _proposal())
    before = path.read_bytes()
    stamp = path.stat().st_mtime_ns

    again = sp.upsert_proposal(config, _proposal(updated_at="2026-08-30T10:00:00Z"))

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == stamp
    assert again.updated_at == "2026-08-09T10:00:00Z"


def test_the_same_session_located_two_ways_is_one_observation(tmp_path: Path) -> None:
    """The dedupe key is the locator, so re-reading a session adds nothing. Two
    passes over overlapping windows name the same session and must not inflate
    the evidence count."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(
        config,
        _proposal(
            sources=(
                sp.SkillEvidence(
                    chat_id="sess-a1",
                    archive="2026-08-09T10:00:00Z",
                    turn="",
                    excerpt="outcome=needs_review corrections=1 errors=0 turns=3",
                ),
            ),
        ),
    )
    assert len(stored.sources) == 1
    # The same session, quoted differently this time (a longer window sees more
    # turns): still one observation, and the first reading is what is kept.
    stored = sp.upsert_proposal(
        config,
        _proposal(
            sources=(
                sp.SkillEvidence(
                    chat_id="sess-a1",
                    archive="2026-08-09T10:00:00Z",
                    turn="",
                    excerpt="outcome=needs_review corrections=3 errors=2 turns=9",
                ),
            ),
        ),
    )
    assert len(stored.sources) == 1
    assert stored.sources[0].excerpt == (
        "outcome=needs_review corrections=1 errors=0 turns=3"
    )


def test_an_empty_field_from_a_later_run_does_not_blank_the_stored_one(
    tmp_path: Path,
) -> None:
    """The over-cap stub write carries no proposed change. It must not erase the
    change a real proposal recorded for the same skill."""
    config = _config(tmp_path)
    sp.upsert_proposal(config, _proposal())

    stubbed = sp.upsert_proposal(
        config,
        _proposal(problem="No clear improvement found.", change="", rationale="", sources=()),
    )

    assert stubbed.change == "Add a defuddle fallback."
    assert stubbed.rationale == "It handles blocked pages."
    assert stubbed.problem == "No clear improvement found."


def test_an_upsert_records_which_skill_bytes_it_read(tmp_path: Path) -> None:
    """A reader has to be able to tell that the skill has moved on since the
    proposal was written, which is what ``reviewed_revision`` is for."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(
        config, _proposal(reviewed_revision="b" * 64, canonical_path="/elsewhere/SKILL.md")
    )
    assert stored.reviewed_revision == "b" * 64
    assert stored.canonical_path == "/elsewhere/SKILL.md"
    # A later pass that named no path keeps the recorded one rather than
    # clearing it.
    kept = sp.upsert_proposal(
        config, _proposal(reviewed_revision="", canonical_path="", sources=())
    )
    assert kept.reviewed_revision == "b" * 64
    assert kept.canonical_path == "/elsewhere/SKILL.md"


def test_upsert_prunes_the_dated_file_the_old_writer_left(tmp_path: Path) -> None:
    """``YYYY-MM-DD-<skill>.md`` was the old naming, so a pass that wrote one
    leaves a second file for the same finding: two rows the review surface has to
    group to look like one."""
    config = _config(tmp_path)
    queue = sp.queue_dir(config, "personal")
    _write(queue / "2026-05-20-web-research.md", "legacy")
    _write(queue / "2026-05-20-humanizer.md", "a different skill's legacy file")

    sp.upsert_proposal(config, _proposal())

    assert [item.name for item in sorted(queue.glob("*.md"))] == [
        "2026-05-20-humanizer.md",
        "web-research.md",
    ]


# -- listing and settlement ------------------------------------------------


def test_read_queue_returns_only_what_is_still_open(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sp.upsert_proposal(config, _proposal(skill="web-research"))
    sp.upsert_proposal(config, _proposal(skill="humanizer"))
    sp.upsert_proposal(config, _proposal(skill="jira-tickets", lifecycle=sp.DISMISSED))

    assert [item.skill for item in sp.read_queue(config, "personal")] == [
        "humanizer",
        "web-research",
    ]


def test_settlement_writes_the_decision_then_flips_the_lifecycle(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")
    stored = sp.upsert_proposal(config, _proposal())

    settled = sp.settle_proposal(
        config, stored.id, sp.DISMISSED, reason="already implemented by hand"
    )

    assert settled is not None
    assert settled.lifecycle == sp.DISMISSED
    on_disk = sp.parse_proposal(path, "personal")
    assert on_disk is not None
    assert on_disk.lifecycle == sp.DISMISSED
    # The finding survives the decision, which is the point: the record is
    # readable, only settled.
    assert on_disk.problem == stored.problem
    assert on_disk.sources == stored.sources
    # And the decision itself is on record, keyed so it cannot be read as a
    # memory fact with the same wording.
    rows = read_decisions(
        tmp_path / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
    )
    assert [row["text"] for row in rows] == ["skill:web-research"]
    assert rows[0]["kind"] == "skill"
    assert rows[0]["action"] == "dismissed"
    assert rows[0]["via"] == "pwa"
    assert rows[0]["outcome"] == "already implemented by hand"


def test_settled_proposals_do_not_come_back(tmp_path: Path) -> None:
    """The whole reason the decision is recorded. A dismissed proposal leaves the
    pending set, and a pass that merges new evidence into it leaves it settled —
    so the same question is not re-asked after it was answered."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())
    sp.settle_proposal(config, stored.id, sp.DISMISSED)
    assert sp.read_queue(config, "personal") == []
    assert stored.id not in sp.enumerate_proposal_ids(config)

    later = sp.upsert_proposal(
        config,
        _proposal(
            sources=(
                sp.SkillEvidence(
                    chat_id="sess-c3",
                    archive="2026-08-23T10:00:00Z",
                    turn="",
                    excerpt="outcome=needs_review corrections=1 errors=0 turns=3",
                ),
            ),
        ),
    )

    assert later.lifecycle == sp.DISMISSED
    assert [item.chat_id for item in later.sources] == ["sess-a1", "sess-c3"]
    assert sp.read_queue(config, "personal") == []


def test_a_decision_outlives_the_file_that_carried_it(tmp_path: Path) -> None:
    """The file can still go — a user clearing their vault, a sync conflict — and
    the decision must not go with it. Reading the sidecar is what makes a
    re-created record settled rather than pending again."""
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")
    stored = sp.upsert_proposal(config, _proposal())
    sp.settle_proposal(config, stored.id, sp.DISMISSED)

    path.unlink()
    re_created = sp.upsert_proposal(config, _proposal())

    assert re_created.lifecycle == sp.DISMISSED
    assert sp.read_queue(config, "personal") == []


def test_an_applied_proposal_is_recorded_as_a_promotion(tmp_path: Path) -> None:
    """``applied`` and ``dismissed`` are the same shape to a reader and opposite
    facts: one says the change landed, the other that it will not. The sidecar
    distinguishes them, so the history does not read a dismissal as an accept."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())

    settled = sp.settle_proposal(
        config, stored.id, sp.APPLIED, chat_id="chat-42", reason="implemented in chat"
    )

    assert settled is not None
    assert settled.lifecycle == sp.APPLIED
    assert settled.chat_id == "chat-42"
    rows = read_decisions(
        tmp_path / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
    )
    assert [row["action"] for row in rows] == ["accepted"]


def test_settling_something_that_is_not_queued_is_not_an_error(tmp_path: Path) -> None:
    """Two tabs racing, or a client retrying after a dropped response. The
    second has nothing left to decide, which is not a failure."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())
    assert sp.settle_proposal(config, stored.id, sp.DISMISSED) is not None
    assert sp.settle_proposal(config, stored.id, sp.DISMISSED) is None
    assert sp.settle_proposal(config, "not-a-real-id", sp.DISMISSED) is None


def test_settlement_refuses_a_lifecycle_that_is_not_a_decision(tmp_path: Path) -> None:
    """A caller inventing one would write a record no reader can place, and it
    would sit in the queue forever with nothing recorded."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())
    with pytest.raises(ValueError, match="not a settled lifecycle"):
        sp.settle_proposal(config, stored.id, "reopened")


def test_a_decision_that_cannot_be_recorded_leaves_the_proposal_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sidecar that could not be written must not leave a record that reads as
    settled: the operator was told nothing, so the proposal is still asking."""
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")
    stored = sp.upsert_proposal(config, _proposal())

    def refuse(*_args: object, **_kwargs: object) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(skill_proposals, "record_dismissal", refuse)

    with pytest.raises(OSError):
        sp.settle_proposal(config, stored.id, sp.DISMISSED)

    on_disk = sp.parse_proposal(path, "personal")
    assert on_disk is not None
    assert on_disk.lifecycle == sp.PENDING
    assert [item.skill for item in sp.read_queue(config, "personal")] == ["web-research"]


# -- workspaces ------------------------------------------------------------


def test_two_workspaces_keep_separate_queues_and_ids(tmp_path: Path) -> None:
    """The same skill name in two workspaces is two proposals with two decisions,
    and one workspace's settlement must not settle the other's."""
    config = _config(tmp_path, "personal", "work")
    personal = sp.upsert_proposal(config, _proposal(workspace="personal"))
    work = sp.upsert_proposal(
        config,
        _proposal(
            workspace="work",
            problem="A different workspace's finding.",
        ),
    )

    assert personal.id != work.id
    assert sp.proposal_path(config, "personal", "web-research") != sp.proposal_path(
        config, "work", "web-research"
    )
    assert sp.read_queue(config, "personal")[0].problem == "Repeated fetch failures."
    assert sp.read_queue(config, "work")[0].problem == "A different workspace's finding."

    sp.settle_proposal(config, personal.id, sp.DISMISSED)

    assert [item.skill for item in sp.read_queue(config, "personal")] == []
    assert [item.skill for item in sp.read_queue(config, "work")] == ["web-research"]


def test_the_enumeration_is_the_pending_set_across_workspaces(tmp_path: Path) -> None:
    """The review listing and the helper-chat archive check both need "which
    proposals are still open". One implementation, so their ids cannot drift —
    two implementations is how a helper chat came to never archive, with no
    error and no log line."""
    config = _config(tmp_path, "personal", "work")
    personal = sp.upsert_proposal(config, _proposal(workspace="personal"))
    work = sp.upsert_proposal(config, _proposal(workspace="work", skill="humanizer"))

    assert sp.enumerate_proposal_ids(config) == {personal.id, work.id}
    sp.settle_proposal(config, work.id, sp.DISMISSED)
    assert sp.enumerate_proposal_ids(config) == {personal.id}


def test_the_enumeration_is_empty_for_an_install_with_no_queue(tmp_path: Path) -> None:
    config = _config(tmp_path, "personal", "work")
    assert sp.read_queue(config, "personal") == []
    assert sp.enumerate_proposal_ids(config) == set()


# -- accepting into a chat: the server owns the lifecycle --------------------
#
# The association used to live in the browser's localStorage, which a reload, a
# second device and the CLI could not see, so the same proposal could be
# implemented twice and nothing recorded that either run happened.


def test_mark_implementing_binds_the_chat_and_keeps_the_row_queued(
    tmp_path: Path,
) -> None:
    """Accepting is a lifecycle transition, not a removal.

    The work is unfinished while it runs, so the record stays in the pending
    set: the review surface keeps showing it, and a resolution helper chat must
    not archive itself against a queue whose question is still open.
    """
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")
    stored = sp.upsert_proposal(config, _proposal())

    marked = sp.mark_implementing(config, stored.id, "chat-1")

    assert marked is not None
    assert marked.lifecycle == sp.IMPLEMENTING
    assert marked.chat_id == "chat-1"
    on_disk = sp.parse_proposal(path, "personal")
    assert on_disk is not None
    assert (on_disk.lifecycle, on_disk.chat_id) == (sp.IMPLEMENTING, "chat-1")
    # Open, so the queue and the archive check both still see it.
    assert [item.id for item in sp.read_queue(config, "personal")] == [stored.id]
    assert stored.id in sp.enumerate_proposal_ids(config)


def test_the_chat_association_survives_a_reload_and_a_later_pass(
    tmp_path: Path,
) -> None:
    """It has to be on the record, not in memory or in a browser.

    A second device reads the same file, and a pass that re-derives the same
    finding must not quietly reset an in-flight record to pending — that would
    drop the row out of "in progress" under a live chat and re-ask a question
    that is already being answered.
    """
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")
    stored = sp.upsert_proposal(config, _proposal())
    sp.mark_implementing(config, stored.id, "chat-1")

    sp.upsert_proposal(config, _proposal(sources=(
        sp.SkillEvidence(chat_id="sess-b2", archive="", turn="", excerpt="again"),
    )))

    reread = sp.parse_proposal(path, "personal")
    assert reread is not None
    assert reread.lifecycle == sp.IMPLEMENTING
    assert reread.chat_id == "chat-1"
    # The new evidence still landed; only the lifecycle was protected.
    assert [item.chat_id for item in reread.sources] == ["sess-a1", "sess-b2"]


def test_a_second_accept_returns_the_live_chat_rather_than_replacing_it(
    tmp_path: Path,
) -> None:
    """Two devices pressing the same button, or a retry after a dropped
    response. Neither is a reason to start a second implementation, and the
    first chat is the one that holds the context."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())
    sp.mark_implementing(config, stored.id, "chat-1")

    again = sp.mark_implementing(config, stored.id, "chat-2")

    assert again is not None
    assert again.chat_id == "chat-1"
    assert again.lifecycle == sp.IMPLEMENTING


def test_a_dead_chat_can_be_superseded_by_the_one_that_replaces_it(
    tmp_path: Path,
) -> None:
    """A re-accept after the chat died has to be able to rebind.

    The idempotency rule above refuses a different chat, which is right while
    the recorded one is live and wrong once it is not: the accept route has
    already established the chat is archived or gone, so the fresh chat is the
    live one and the record has to name it. Naming the dead chat is the whole
    permission — a caller that has not looked at it must not get it.
    """
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")
    stored = sp.upsert_proposal(config, _proposal())
    sp.mark_implementing(config, stored.id, "chat-1")

    rebound = sp.mark_implementing(config, stored.id, "chat-2", supersedes="chat-1")

    assert rebound is not None
    assert (rebound.chat_id, rebound.lifecycle) == ("chat-2", sp.IMPLEMENTING)
    on_disk = sp.parse_proposal(path, "personal")
    assert on_disk is not None
    assert (on_disk.chat_id, on_disk.lifecycle) == ("chat-2", sp.IMPLEMENTING)


def test_superseding_an_unrelated_chat_is_refused(tmp_path: Path) -> None:
    """``supersedes`` replaces the chat it names and no other.

    This function cannot tell a live chat from a dead one, so it does not try:
    a caller that wants to displace ``chat-1`` has to have looked at ``chat-1``.
    """
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())
    sp.mark_implementing(config, stored.id, "chat-1")

    sp.mark_implementing(config, stored.id, "chat-2", supersedes="chat-3")

    kept = sp.parse_proposal(
        sp.proposal_path(config, "personal", "web-research"), "personal"
    )
    assert kept is not None
    assert kept.chat_id == "chat-1"


def test_an_interrupted_record_can_be_rebound_to_a_fresh_chat(tmp_path: Path) -> None:
    """The work stopped and the operator accepted it again. That is the ordinary
    recovery path, and it has to leave the record implementing again."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())
    sp.mark_implementing(config, stored.id, "chat-1")
    sp.mark_outcome(config, stored.id, sp.INTERRUPTED, "ran out of context")

    retried = sp.mark_implementing(config, stored.id, "chat-2", supersedes="chat-1")

    assert retried is not None
    assert (retried.chat_id, retried.lifecycle) == ("chat-2", sp.IMPLEMENTING)
    # And no decision was written by either step, so it is still re-openable.
    assert [item.id for item in sp.read_queue(config, "personal")] == [stored.id]


def test_marking_implementing_is_a_no_op_when_the_chat_has_not_changed(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())
    first = sp.mark_implementing(config, stored.id, "chat-1")
    before = sp.proposal_path(config, "personal", "web-research").read_text("utf-8")

    assert sp.mark_implementing(config, stored.id, "chat-1") == first
    assert sp.proposal_path(config, "personal", "web-research").read_text("utf-8") == before


def test_a_proposal_cannot_be_implementing_without_a_chat(tmp_path: Path) -> None:
    """A row reading "in progress" that nobody is working on is a lie with
    buttons on it, and nothing could ever move it on again."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())

    with pytest.raises(ValueError, match="only be marked implementing with a chat"):
        sp.mark_implementing(config, stored.id, "")
    assert [item.lifecycle for item in sp.read_queue(config, "personal")] == [sp.PENDING]


def test_an_interrupted_run_stays_queued_and_records_no_decision(
    tmp_path: Path,
) -> None:
    """A chat that stopped is not a person who decided.

    The sidecar is what stops the next pass from re-asking, so writing a
    dismissal here would archive an unfinished edit as though it had been
    rejected. The record keeps its evidence, keeps its chat, and goes back in
    the queue so the operator can accept it again.
    """
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")
    stored = sp.upsert_proposal(config, _proposal())
    sp.mark_implementing(config, stored.id, "chat-1")

    stopped = sp.mark_outcome(config, stored.id, sp.INTERRUPTED, "ran out of context")

    assert stopped is not None
    assert stopped.lifecycle == sp.INTERRUPTED
    assert stopped.chat_id == "chat-1"
    on_disk = sp.parse_proposal(path, "personal")
    assert on_disk is not None
    assert on_disk.problem == stored.problem          # the finding is intact
    assert on_disk.sources == stored.sources          # and so is the evidence
    # Still queued: an interrupted implementation is recoverable work.
    assert [item.id for item in sp.read_queue(config, "personal")] == [stored.id]
    assert read_decisions(
        tmp_path / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
    ) == []


def test_an_interrupted_proposal_can_be_accepted_again(tmp_path: Path) -> None:
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())
    sp.mark_implementing(config, stored.id, "chat-1")
    sp.mark_outcome(config, stored.id, sp.INTERRUPTED, "stopped")

    retried = sp.mark_implementing(config, stored.id, "chat-1")

    assert retried is not None
    assert retried.lifecycle == sp.IMPLEMENTING


def test_an_applied_outcome_is_recorded_as_a_promotion(tmp_path: Path) -> None:
    """``applied`` and ``dismissed`` are the same shape to a reader and opposite
    facts, and only the caller that verified the change may assert the first."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())
    sp.mark_implementing(config, stored.id, "chat-1")

    settled = sp.mark_outcome(config, stored.id, sp.APPLIED, "verified in the skill")

    assert settled is not None
    assert settled.lifecycle == sp.APPLIED
    assert settled.chat_id == "chat-1"
    rows = read_decisions(
        tmp_path / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
    )
    assert [row["action"] for row in rows] == ["accepted"]
    assert rows[0]["outcome"] == "verified in the skill"


def test_a_pass_can_never_record_an_outcome_for_an_implementation(
    tmp_path: Path,
) -> None:
    """``mark_outcome`` is the resolution writer, and it only speaks about work
    a chat did. A lifecycle it does not name would be a value no reader of the
    queue can place."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())
    with pytest.raises(ValueError, match="is not an outcome"):
        sp.mark_outcome(config, stored.id, "reopened")


def test_a_settled_proposal_has_no_outcome_left_to_record(tmp_path: Path) -> None:
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())
    sp.settle_proposal(config, stored.id, sp.DISMISSED)

    assert sp.mark_outcome(config, stored.id, sp.APPLIED) is None
    assert sp.mark_implementing(config, stored.id, "chat-1") is None


def test_find_proposal_answers_for_the_whole_registry(tmp_path: Path) -> None:
    """One walk, so the accept route and the outcome writer cannot disagree
    about which row an id names."""
    config = _config(tmp_path, "personal", "work")
    personal = sp.upsert_proposal(config, _proposal(workspace="personal"))
    work = sp.upsert_proposal(
        config, _proposal(workspace="work", skill="humanizer")
    )

    assert sp.find_proposal(config, work.id) == work
    assert sp.find_proposal(config, personal.id) == personal
    assert sp.find_proposal(config, "nope") is None


# -- the improvement prompt --------------------------------------------------


def test_the_prompt_improves_the_existing_skill(tmp_path: Path) -> None:
    """The prompt IS the acceptance, and the browser used to own it — where it
    had drifted into telling the chat to CREATE a skill. The finding is about a
    skill that already exists, so create-a-skill wording asks for a different
    task and leaves the queue with nothing it asked for."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())

    prompt = sp.render_improvement_prompt(stored)

    # Names the EXISTING canonical source, and says so.
    assert "/agent/skills/web-research/SKILL.md" in prompt
    assert "already exists" in prompt
    assert "Improve the existing `web-research` skill" in prompt
    # And never asks for a new one.
    lowered = prompt.lower()
    for phrase in ("create it", "create a new skill", "create the skill"):
        assert phrase not in lowered, phrase


def test_the_prompt_carries_the_proposal_its_findings_and_its_revision(
    tmp_path: Path,
) -> None:
    """Without the id, the evidence and the reviewed revision the chat cannot
    tell whether the skill still reads the way the reviewer saw it, nor which
    question its resolution is answering."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())

    prompt = sp.render_improvement_prompt(stored)

    assert stored.id in prompt
    assert "Repeated fetch failures." in prompt
    assert "Add a defuddle fallback." in prompt
    assert "It handles blocked pages." in prompt
    assert "sess-a1" in prompt
    assert stored.reviewed_revision[:12] in prompt
    # The resolution, through the CLI that owns the settlement, and the
    # interrupted escape that keeps an unfinished edit recoverable.
    assert "ciao skill-proposal-remove web-research --applied" in prompt
    assert "--interrupted" in prompt
    assert "Read the current skill first" in prompt
    assert "ciao sync-skills" in prompt


def test_the_prompt_runs_no_command_it_does_not_mean(tmp_path: Path) -> None:
    """A prompt is instructions, so every command in it has to do what it says.

    Three ways it did not. ``sync-skills --workspace`` takes a PATH, and the chat's
    working directory is already the owning agent root, so passing the workspace
    NAME resolved to ``<root>/work`` and seeded a stray catalog there instead of
    syncing the real one. And the prompt told the chat to settle a finding "as not
    applicable" while the CLI exposed no way to record that — so the one branch a
    chat that concluded the finding had expired could not answer for itself.
    ``--workspace`` on ``skill-proposal-remove`` is the install-root path too, so
    ``--workspace .`` from the agent root resolved a queue that does not exist.
    """
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal(workspace="work"))

    prompt = sp.render_improvement_prompt(stored)

    assert "sync-skills --workspace" not in prompt
    assert "ciao sync-skills" in prompt
    assert "ciao skill-proposal-remove web-research --not-applicable" in prompt
    assert "skill-proposal-remove web-research --workspace" not in prompt


def test_every_outcome_the_prompt_names_is_one_the_cli_can_record(
    tmp_path: Path,
) -> None:
    """The prompt and the command have to agree, and the command is the contract.

    Parsed out of the rendered prompt rather than hard-coded, so a future edit to
    the wording cannot quietly reintroduce an outcome no flag produces.
    """
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())

    parser = _skill_proposal_remove_parser()

    prompt = sp.render_improvement_prompt(stored)
    named = set(re.findall(r"skill-proposal-remove[^\n]*?(--applied|--not-applicable|--interrupted)", prompt))
    assert named == {"--applied", "--not-applicable", "--interrupted"}
    for flag in named:
        assert flag in parser._option_string_actions, flag


def _skill_proposal_remove_parser() -> argparse.ArgumentParser:
    """The real parser for ``ciao skill-proposal-remove``."""
    from ciao.cli import build_parser

    parser = build_parser()
    for action in parser._subparsers._actions:
        if isinstance(action, argparse._SubParsersAction):
            sub = action.choices.get("skill-proposal-remove")
            if sub is not None:
                return sub
    raise AssertionError("skill-proposal-remove is not registered")


def test_the_prompt_falls_back_to_the_skills_directory_when_no_path_was_recorded(
    tmp_path: Path,
) -> None:
    """A legacy record names no resolved source. It still has to say WHICH
    skill to improve, or the chat is left guessing between a stock copy, a
    provider mirror and the workspace's own catalog."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal(canonical_path="", reviewed_revision=""))

    prompt = sp.render_improvement_prompt(stored)

    assert "skills/web-research/SKILL.md" in prompt
    assert "revision" not in prompt


# -- splitting the model's answer ------------------------------------------


def test_split_findings_maps_the_prompts_headings() -> None:
    problem, change, rationale = sp.split_findings(
        "## What I noticed\nFetch failures.\n\n"
        "## Suggested improvement\nA fallback.\n\n"
        "## Proposed edit\n```diff\n+fallback\n```\n\n"
        "## Why this should help\nIt handles blocked pages."
    )
    assert problem == "Fetch failures."
    assert change.startswith("A fallback.")
    assert "+fallback" in change
    assert rationale == "It handles blocked pages."


def test_split_findings_keeps_a_heading_inside_a_code_block_as_content() -> None:
    """The finding is usually a diff, and a diff that adds a Markdown heading to
    a SKILL.md contains a line that looks exactly like a section boundary. Read
    as one, the heading ends the section and the rest of the diff lands in
    whichever field came next."""
    problem, change, rationale = sp.split_findings(
        "## What I noticed\nFetch failures.\n\n"
        "## Proposed edit\n\n```diff\n+# Usage\n+\nRun it.\n```\n"
    )
    assert problem == "Fetch failures."
    assert "# Usage" in change
    assert "Run it." in change
    assert rationale == ""


def test_split_findings_keeps_an_answer_with_no_headings() -> None:
    problem, change, rationale = sp.split_findings("Suggested edit: explain defuddle.")
    assert (problem, change) == ("", "")
    assert rationale == "Suggested edit: explain defuddle."


def _eligible(config: CiaoConfig, learning: LearningRecord) -> bool:
    """Whether the cleanup hook would clear this learning, revision unchanged."""
    return bool(
        sp.learning_cleanup_eligibility(
            config, "personal", learning, current_revision=LEARNINGS_REVISION
        )["eligible"]
    )


# -- origins ----------------------------------------------------------------


def _origin(
    learning_id: str = LEARNING_ID,
    finding: str = "add a defuddle fallback",
    **overrides: object,
) -> sp.SkillOrigin:
    """One learning link, as a pass that routed a finding would file it."""
    fields: dict[str, object] = {
        "workspace": "personal",
        "learning_id": learning_id,
        "source_revision": LEARNINGS_REVISION,
        "finding": finding,
        "summary": "Add the defuddle fallback step.",
        "state": sp.ORIGIN_PENDING,
        "verification": "",
    }
    fields.update(overrides)
    return sp.SkillOrigin(**fields)  # type: ignore[arg-type]


def _linked(
    config: CiaoConfig,
    *origins: sp.SkillOrigin,
    skill: str = "web-research",
    workspace: str = "personal",
) -> sp.SkillProposal:
    """File a record whose findings are linked to learnings.

    The links are written into one workspace's queue, so an origin filed
    without naming one belongs to the workspace being written to.
    """
    return sp.upsert_proposal(
        config,
        _proposal(
            workspace=workspace,
            skill=skill,
            origins=tuple(
                replace(origin, workspace=origin.workspace or workspace)
                for origin in origins
            ),
        ),
    )


def _decided(
    config: CiaoConfig,
    *origins: sp.SkillOrigin,
    skill: str = "web-research",
    workspace: str = "personal",
) -> sp.SkillProposal:
    """Write a record whose findings already carry a state, as a settled one is.

    Straight to the queue rather than through ``upsert_proposal``, which files
    every origin it is handed as ``pending``: a pass that routed a learning into
    a finding cannot know whether the lesson landed or whether anybody rejected
    the finding, so a filing cannot answer one. These states are what a decision
    leaves behind.
    """
    record = _proposal(
        workspace=workspace,
        skill=skill,
        origins=tuple(
            replace(origin, workspace=origin.workspace or workspace)
            for origin in origins
        ),
    )
    _write(sp.proposal_path(config, workspace, skill), sp.render_proposal(record))
    return record


def test_origins_round_trip_through_render_and_parse(tmp_path: Path) -> None:
    """The link is the whole point, so the file has to hold it in a shape that
    reads back identically: the no-op merge compares rendered bytes with the
    stored file, and a lost field here would make every re-run look like a
    change while quietly unlinking the learning."""
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")
    record = _proposal(
        origins=(
            _origin(),
            _origin(
                learning_id=OTHER_LEARNING_ID,
                finding="name the source revision in the readback",
            ),
        )
    )

    _write(path, sp.render_proposal(record))
    back = sp.parse_proposal(path, "personal")

    assert back == record
    assert "## Origins" in path.read_text(encoding="utf-8")
    assert back is not None and back.origins[0].learning_id == LEARNING_ID


def test_an_origin_survives_a_rewrite_of_the_file_it_lives_in(tmp_path: Path) -> None:
    """Two full round trips produce identical bytes. A renderer that reordered
    keys, or a parser that filled a blank in, would make the queue churn on
    every pass even though nothing about the finding changed."""
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")
    record = _proposal(origins=(_origin(),))

    first = sp.render_proposal(record)
    _write(path, first)
    parsed = sp.parse_proposal(path, "personal")
    assert parsed is not None
    second = sp.render_proposal(parsed)

    assert second == first
    assert sp.upsert_proposal(config, parsed) == parsed


def test_upsert_merges_a_new_origin_and_keeps_the_one_it_had(tmp_path: Path) -> None:
    """A re-observation of the same finding is the same origin and adds nothing;
    a finding on a second learning is a second origin even though the record is
    still one row for the skill. Replacing the list instead of merging it is how
    the previous run's links used to disappear."""
    config = _config(tmp_path)
    _linked(config, _origin())
    merged = sp.upsert_proposal(
        config,
        _proposal(
            origins=(
                _origin(),
                _origin(
                    learning_id=OTHER_LEARNING_ID,
                    finding="name the source revision in the readback",
                ),
            )
        ),
    )

    assert [origin.learning_id for origin in merged.origins] == [
        LEARNING_ID,
        OTHER_LEARNING_ID,
    ]


def test_a_reworded_finding_is_the_same_origin_not_a_second_one(tmp_path: Path) -> None:
    """The dedupe key is the normalized finding, so a pass that re-words the same
    finding on its next run does not split one finding in two — which would leave
    a learning half-settled for ever, with no row saying the other half existed."""
    config = _config(tmp_path)
    _linked(config, _origin())
    merged = sp.upsert_proposal(
        config, _proposal(origins=(_origin(finding="Add a defuddle fallback!"),))
    )

    assert len(merged.origins) == 1
    assert merged.origins[0].finding == "add a defuddle fallback"


def test_a_merge_does_not_take_a_decision_back(tmp_path: Path) -> None:
    """First-seen wins, exactly as the evidence merge does. A later pass
    re-deriving the same finding arrives as pending, and taking that would
    silently reopen a finding a person had already rejected."""
    config = _config(tmp_path)
    _linked(config, _origin())
    sp.settle_proposal(
        config,
        sp.find_proposal(config, sp.proposal_id("personal", "web-research")).id,
        sp.DISMISSED,
    )

    merged = sp.upsert_proposal(config, _proposal(origins=(_origin(),)))

    assert [origin.state for origin in merged.origins] == [sp.ORIGIN_DISMISSED]


def test_a_legacy_proposal_without_origins_parses_and_links_nothing(
    tmp_path: Path,
) -> None:
    """Backward compatibility, and the reason it is safe: an empty tuple is not
    the same as a settled learning. A record filed before origins existed has
    nothing to fold, so every learning it might have covered stays exactly as
    Active as it was."""
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")
    _write(path, LEGACY_FILE)

    record = sp.parse_proposal(path, "personal")
    assert record is not None
    assert record.origins == ()

    # Rewriting it must not invent a link, and must not lose a word either.
    stored = sp.upsert_proposal(config, record)
    assert stored.origins == ()
    rewritten = sp.parse_proposal(
        sp.proposal_path(config, "personal", "web-research"), "personal"
    )
    assert rewritten is not None and rewritten.origins == ()
    assert "reviewable suggestion" in rewritten.rationale


def test_an_origin_line_this_cannot_read_is_kept_and_names_no_learning(
    tmp_path: Path,
) -> None:
    """Fail closed. An origin nobody can attribute could be the other half of a
    learning somebody is about to declare dealt with, so a line in a shape this
    parser refuses becomes an unlinked origin rather than being dropped — and an
    unlinked origin is kept as text, because the record must not lose a finding
    because it was formatted differently than expected."""
    config = _config(tmp_path)
    path = sp.proposal_path(config, "personal", "web-research")
    _write(
        path,
        sp.render_proposal(_proposal())
        + "\n## Origins\n\n```json\n"
        + "not json at all\n"
        + '{"schema":1,"workspace":"personal","learning_id":"'
        + LEARNING_ID
        + '","source_revision":"'
        + LEARNINGS_REVISION
        + '","finding":"read it back","state":"pending"}\n```\n',
    )

    record = sp.parse_proposal(path, "personal")
    assert record is not None
    assert [origin.linked for origin in record.origins] == [False, True]
    assert record.origins[0].finding == "not json at all"


# -- per-finding settlement -------------------------------------------------


def test_settling_one_finding_leaves_its_siblings_asking(tmp_path: Path) -> None:
    """The failure this whole shape exists to prevent. A record is one row per
    skill, so accepting the row used to accept every finding on it — and a person
    who agreed with one of them had no way to say they did not agree with the
    other two."""
    config = _config(tmp_path)
    stored = _linked(
        config,
        _origin(),
        _origin(finding="read the file back after editing"),
    )

    settled = sp.settle_proposal(
        config,
        stored.id,
        sp.DISMISSED,
        selectors=[sp.OriginRef(LEARNING_ID, "add a defuddle fallback")],
    )

    assert settled is not None
    assert [(origin.finding, origin.state) for origin in settled.origins] == [
        ("add a defuddle fallback", sp.ORIGIN_DISMISSED),
        ("read the file back after editing", sp.ORIGIN_PENDING),
    ]
    # The record is still asking, so the row is still on the review surface.
    assert settled.lifecycle == sp.PENDING
    assert [item.skill for item in sp.read_queue(config, "personal")] == ["web-research"]


def test_a_settled_finding_does_not_strand_its_siblings_on_the_next_pass(
    tmp_path: Path,
) -> None:
    """The bug this per-finding shape existed to prevent, arriving by the back
    door. A settlement writes one sidecar row per finding it answered, keyed by
    the whole skill — the same synthetic text a whole-record settlement writes —
    so the next pass found that row, read it as a decision about the *record*, and
    closed it: one finding applied, one still pending, and a row that had left
    the review queue with an unanswered finding nobody could reach.

    A record that links learnings takes its lifecycle from its findings, so a
    decision row speaks only for a record that has no findings to speak of.
    """
    config = _config(tmp_path)
    stored = _linked(
        config,
        _origin(),
        _origin(finding="read the file back after editing"),
    )
    sp.settle_proposal(
        config,
        stored.id,
        sp.APPLIED,
        selectors=[sp.OriginRef(LEARNING_ID, "add a defuddle fallback")],
        verification="mrcpt_abc123",
    )
    # The decision row is on disk, keyed by the whole skill. That it is there is
    # correct: it is the history. What it must not be is the record's lifecycle.
    assert [
        row["text"] for row in read_decisions(
            tmp_path / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
        )
    ] == [sp.decision_text("web-research")]

    merged = sp.upsert_proposal(
        config,
        _proposal(
            origins=(
                _origin(),
                _origin(finding="read the file back after editing"),
            )
        ),
    )

    assert merged.lifecycle == sp.PENDING
    assert [origin.state for origin in merged.origins] == [
        sp.ORIGIN_APPLIED,
        sp.ORIGIN_PENDING,
    ]
    # Still on the review surface, which is the only place the outstanding
    # finding can be answered at all.
    assert [item.id for item in sp.read_queue(config, "personal")] == [stored.id]


def test_a_settled_record_a_new_finding_reaches_reopens(tmp_path: Path) -> None:
    """The same read from the other side. A record whose every finding has been
    answered is settled, and a later pass linking one more finding to it has
    produced something nobody has looked at — so the record is asking again
    rather than carrying an unanswered question out of the queue. This is the
    eligibility rule's other half: an origin back in ``pending`` is a question
    being asked again, and the learning behind it stays Active."""
    config = _config(tmp_path)
    stored = _linked(config, _origin())
    sp.settle_proposal(config, stored.id, sp.APPLIED, verification="mrcpt_abc123")
    assert sp.read_queue(config, "personal") == []
    assert _eligible(config, _learning())

    merged = sp.upsert_proposal(
        config,
        _proposal(origins=(_origin(), _origin(finding="read the file back"))),
    )

    assert merged.lifecycle == sp.PENDING
    assert [item.id for item in sp.read_queue(config, "personal")] == [stored.id]
    assert not _eligible(config, _learning())


def test_a_filing_cannot_file_a_finding_as_already_answered(tmp_path: Path) -> None:
    """A payload is model-authored prose, so a ``state`` in one is a claim rather
    than a fact, and the two claims that matter both retire a lesson: ``applied``
    skips the verification that makes it mean anything, and ``dismissed`` stands
    in for a rejection nobody made. The merge therefore files every incoming
    origin ``pending`` with no verification, whatever it was handed, so the only
    way a learning clears is somebody settling the finding."""
    config = _config(tmp_path)

    merged = sp.upsert_proposal(
        config,
        _proposal(
            origins=(
                _origin(state=sp.ORIGIN_APPLIED, verification="mrcpt_forged"),
                _origin(finding="and this one is rejected", state=sp.ORIGIN_DISMISSED),
            )
        ),
    )

    assert [(origin.state, origin.verification) for origin in merged.origins] == [
        (sp.ORIGIN_PENDING, ""),
        (sp.ORIGIN_PENDING, ""),
    ]
    assert sp.learning_settlement(config, "personal", _learning()).settled is False
    assert not _eligible(config, _learning())


def test_a_finding_selector_cannot_reverse_a_settlement(tmp_path: Path) -> None:
    """A selector says *which* finding a decision is about, so naming one that
    has already been answered is a caller bug rather than a decision worth
    recording — silently flipping ``applied`` to ``dismissed`` would turn a
    lesson somebody verified into the target into a finding a person is supposed
    to have rejected on its own. Refused by name, the way a selector matching
    nothing is."""
    config = _config(tmp_path)
    stored = _linked(
        config,
        _origin(),
        _origin(finding="read the file back after editing"),
    )
    sp.settle_proposal(
        config,
        stored.id,
        sp.APPLIED,
        selectors=[sp.OriginRef(LEARNING_ID, "add a defuddle fallback")],
        verification="mrcpt_abc123",
    )

    with pytest.raises(ValueError, match="not a way to un-decide it"):
        sp.settle_proposal(
            config,
            stored.id,
            sp.DISMISSED,
            selectors=[sp.OriginRef(LEARNING_ID, "add a defuddle fallback")],
        )

    on_disk = sp.parse_proposal(
        sp.proposal_path(config, "personal", "web-research"), "personal"
    )
    assert on_disk is not None
    assert [(origin.finding, origin.state) for origin in on_disk.origins] == [
        ("add a defuddle fallback", sp.ORIGIN_APPLIED),
        ("read the file back after editing", sp.ORIGIN_PENDING),
    ]


def test_a_learning_with_one_applied_and_one_pending_finding_is_not_settled(
    tmp_path: Path,
) -> None:
    """A learning split across two findings is dealt with only when both are.
    Anything else retires a lesson on the strength of half the evidence."""
    config = _config(tmp_path)
    stored = _linked(
        config,
        _origin(),
        _origin(finding="read the file back after editing"),
    )
    sp.settle_proposal(
        config,
        stored.id,
        sp.APPLIED,
        selectors=[sp.OriginRef(LEARNING_ID, "add a defuddle fallback")],
        verification="mrcpt_abc123",
    )

    settlement = sp.learning_settlement(config, "personal", _learning())
    assert settlement.settled is False
    assert "not settled" in settlement.reason
    assert not _eligible(config, _learning())


def test_applying_a_linked_finding_needs_a_verification(tmp_path: Path) -> None:
    """"Chat started" and "the queue row is gone" are the two things this queue
    can see for itself, and neither is the lesson being in the target. So an
    applied with no receipt and no readback is refused rather than recorded."""
    config = _config(tmp_path)
    stored = _linked(config, _origin())

    with pytest.raises(ValueError, match="needs a verification"):
        sp.settle_proposal(config, stored.id, sp.APPLIED)

    on_disk = sp.parse_proposal(
        sp.proposal_path(config, "personal", "web-research"), "personal"
    )
    assert on_disk is not None
    assert [origin.state for origin in on_disk.origins] == [sp.ORIGIN_PENDING]
    assert sp.read_queue(config, "personal") != []


def test_a_verified_finding_and_a_rejected_one_settle_the_learning(
    tmp_path: Path,
) -> None:
    """The two answers that do clear a learning: the lesson is in the target and
    somebody proved it, or a person rejected that finding outright."""
    config = _config(tmp_path)
    stored = _linked(
        config,
        _origin(),
        _origin(finding="read the file back after editing"),
    )
    sp.settle_proposal(
        config,
        stored.id,
        sp.APPLIED,
        selectors=[sp.OriginRef(LEARNING_ID, "add a defuddle fallback")],
        verification="read SKILL.md back: the fallback step is there",
    )
    settled = sp.settle_proposal(
        config,
        stored.id,
        sp.DISMISSED,
        selectors=[sp.OriginRef(LEARNING_ID, "read the file back after editing")],
    )

    assert settled is not None
    assert settled.lifecycle == sp.APPLIED
    assert settled.origins[0].verification == (
        "read SKILL.md back: the fallback step is there"
    )
    settlement = sp.learning_settlement(config, "personal", _learning())
    assert settlement.settled is True
    assert len(settlement.origins) == 2
    assert _eligible(config, _learning())


@pytest.mark.parametrize(
    ("state", "because"),
    [
        (sp.ORIGIN_ALREADY_COVERED, "needs the target confirmed"),
        (sp.ORIGIN_NOT_APPLICABLE, "needs a person to judge it"),
        (sp.ORIGIN_UNCLEAR, "needs a person to judge it"),
        (sp.ORIGIN_INTERRUPTED, "the run stopped"),
        (sp.ORIGIN_FAILED, "the run broke"),
        (sp.ORIGIN_IMPLEMENTING, "a chat is working on it"),
        (sp.ORIGIN_PENDING, "nobody has looked at it"),
    ],
)
def test_no_state_but_applied_or_dismissed_settles_a_learning(
    tmp_path: Path, state: str, because: str
) -> None:
    """Each of these is an answer-shaped string that is not an answer. Applied
    means the lesson is in the target; dismissed means a person said no. The rest
    all leave the learning Active, which is the only safe default for a lesson
    somebody is relying on."""
    config = _config(tmp_path)
    stored = _decided(config, _origin(finding="add a defuddle fallback", state=state))

    settlement = sp.learning_settlement(config, "personal", _learning())

    assert settlement.settled is False, because
    assert settlement.origins[0].proposal_id == stored.id
    assert settlement.origins[0].state == state
    assert not _eligible(config, _learning())


def test_interrupting_a_run_leaves_the_finding_and_the_learning_alone(
    tmp_path: Path,
) -> None:
    """The absence of an answer is not an answer, so an interrupted run writes no
    decision, re-opens nothing, and leaves the learning Active — which is the
    whole reason the origin states are not booleans."""
    config = _config(tmp_path)
    stored = _linked(config, _origin())
    sp.mark_implementing(config, stored.id, "chat-42")
    assert sp.learning_settlement(config, "personal", _learning()).settled is False

    stopped = sp.mark_outcome(config, stored.id, sp.INTERRUPTED, "ran out of context")

    assert stopped is not None
    assert [origin.state for origin in stopped.origins] == [sp.ORIGIN_INTERRUPTED]
    decisions = read_decisions(
        tmp_path / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
    )
    assert decisions == []


def test_accepting_moves_the_findings_to_implementing_without_deciding_them(
    tmp_path: Path,
) -> None:
    """Accepting a row starts one chat over all of its findings. Recording that
    as a decision is exactly the bug: the lesson is in the skill only once
    somebody reads the skill back."""
    config = _config(tmp_path)
    stored = _linked(config, _origin())

    accepted = sp.mark_implementing(config, stored.id, "chat-42")

    assert accepted is not None
    assert accepted.lifecycle == sp.IMPLEMENTING
    assert [origin.state for origin in accepted.origins] == [sp.ORIGIN_IMPLEMENTING]
    assert sp.learning_settlement(config, "personal", _learning()).settled is False


def test_interrupting_one_finding_leaves_the_others_in_flight(tmp_path: Path) -> None:
    """A selector narrows an interrupt exactly as it narrows a settlement: a run
    that fell over on one finding did not decide the ones beside it."""
    config = _config(tmp_path)
    stored = _linked(
        config,
        _origin(finding="add a defuddle fallback"),
        _origin(finding="read the file back"),
    )
    sp.mark_implementing(config, stored.id, "chat-42")

    stopped = sp.mark_outcome(
        config,
        stored.id,
        sp.INTERRUPTED,
        "ran out of context on the first one",
        selectors=[sp.OriginRef(LEARNING_ID, "add a defuddle fallback")],
    )

    assert stopped is not None
    assert [(origin.finding, origin.state) for origin in stopped.origins] == [
        ("add a defuddle fallback", sp.ORIGIN_INTERRUPTED),
        ("read the file back", sp.ORIGIN_IMPLEMENTING),
    ]
    assert sp.learning_settlement(config, "personal", _learning()).settled is False


def test_a_settlement_names_the_finding_it_was_about(tmp_path: Path) -> None:
    """One sidecar row per finding, so the decision history can say which lesson
    was answered. A single row keyed only by ``skill:<name>`` reads as "the skill
    was dealt with", which is the claim that was never true."""
    config = _config(tmp_path)
    stored = _linked(
        config,
        _origin(),
        _origin(finding="read the file back after editing"),
    )

    sp.settle_proposal(
        config,
        stored.id,
        sp.APPLIED,
        selectors=[sp.OriginRef(LEARNING_ID, "read the file back after editing")],
        verification="mrcpt_abc123",
    )

    rows = read_decisions(
        tmp_path / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
    )
    assert [(row["action"], row["finding"], row["learning_id"]) for row in rows] == [
        ("accepted", "read the file back after editing", LEARNING_ID)
    ]
    # Still the synthetic skill text, so a skill decision can never be read as a
    # memory fact with the same wording.
    assert {row["text"] for row in rows} == {"skill:web-research"}


def test_a_selector_naming_no_finding_is_refused(tmp_path: Path) -> None:
    """A caller settling a finding this record does not carry has a bug, and the
    bug is worth hearing about rather than quietly settling the whole record."""
    config = _config(tmp_path)
    stored = _linked(config, _origin())

    with pytest.raises(ValueError, match="has no origin for"):
        sp.settle_proposal(
            config, stored.id, sp.DISMISSED, selectors=[sp.OriginRef("nonesuch")]
        )

    on_disk = sp.parse_proposal(
        sp.proposal_path(config, "personal", "web-research"), "personal"
    )
    assert on_disk is not None
    assert on_disk.origins[0].state == sp.ORIGIN_PENDING


def test_a_selector_on_a_record_with_no_links_is_refused(tmp_path: Path) -> None:
    """A legacy record links nothing, so there is no finding to settle and no
    learning that could be retired by pretending otherwise."""
    config = _config(tmp_path)
    stored = sp.upsert_proposal(config, _proposal())

    with pytest.raises(ValueError, match="links no learning"):
        sp.settle_proposal(
            config, stored.id, sp.DISMISSED, selectors=[sp.OriginRef(LEARNING_ID)]
        )
    # The same decision without a selector is the ordinary whole-row one and
    # still works, because a person dismissing a row has answered it.
    assert sp.settle_proposal(config, stored.id, sp.DISMISSED) is not None


def test_two_workspaces_with_the_same_key_cannot_clear_each_other(
    tmp_path: Path,
) -> None:
    """A learning id is minted from the workspace, so the same statement in two
    workspaces is two learnings. The fold reads one workspace's queue and matches
    only that workspace's ids, so applying the first one says nothing about the
    second — which is the same rule that keeps a ``key`` from being an identity."""
    config = _config(tmp_path, "personal", "work")
    personal = _linked(config, _origin(), skill="web-research")
    _linked(
        config,
        _origin(
            learning_id=WORK_LEARNING_ID,
            workspace="work",
            finding="the same sentence",
        ),
        skill="web-research",
        workspace="work",
    )
    work = sp.find_proposal(config, sp.proposal_id("work", "web-research"))
    assert work is not None
    sp.settle_proposal(config, personal.id, sp.APPLIED, verification="mrcpt_abc123")

    assert sp.learning_settlement(config, "personal", _learning()).settled is True
    assert sp.learning_settlement(config, "work", _work_learning()).settled is False
    assert len(sp.learning_settlement(config, "work", _work_learning()).origins) == 1


def test_a_link_pointing_out_of_its_workspace_is_not_a_link(tmp_path: Path) -> None:
    """A queue is one workspace's, so a link naming another workspace is a claim
    this queue cannot make. Counting it would let a workspace retire a learning
    by asserting a finding on someone else's."""
    config = _config(tmp_path)
    _linked(config, _origin(workspace="work"))

    settlement = sp.learning_settlement(config, "personal", _learning())

    assert settlement.origins == ()
    assert settlement.settled is False

