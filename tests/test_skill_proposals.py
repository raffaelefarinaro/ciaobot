"""Tests for ``ciao.skill_proposals``, the skill-proposal queue's owner.

The behaviour pinned here is the queue's, not the file format's: one stable
identity per skill, an evidence merge that neither drops what the last pass
found nor double-counts what this pass re-read, a re-run over unchanged evidence
that writes nothing, and a settlement that outlives the row it settles. Every
vault here is a throwaway one under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import skill_proposals
from ciao import skill_proposals as sp
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.memory_proposals import read_decisions


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
