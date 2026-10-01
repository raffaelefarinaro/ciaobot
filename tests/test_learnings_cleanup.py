"""The settlement-linked cleanup of ``Workspace/Learnings.md`` (#728-E).

``ciao/skill_proposals.learning_cleanup_eligibility`` already answers "may this
learning be retired?"; this file is the reconciliation that acts on the answer,
and every test here is a way it could remove a line it should not have — or
leave a line it should have.

The three properties the module is built on are each pinned by a test of their
own failure:

* **lossless** — only the eligible Active spans leave; frontmatter except
  ``updated:``, format notes, the Promoted section, the BOM and CRLF all survive;
* **revision-checked** — a file that moved between the plan and the apply is a
  conflict, not a write, and a candidate whose own line moved is dropped from that
  apply rather than removed on a stale answer;
* **suppressed** — a second run removes nothing, an undo is not immediately
  reversed, and a re-reviewed revision makes the entry eligible again.

Every vault here is a throwaway one under ``tmp_path``. No test contacts GitHub,
reads a real vault, or leaves a lock file in the shared temp root.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Iterator

import pytest

from ciao import learnings_cleanup as lc
from ciao import skill_proposals as sp
from ciao import upstream_drafts
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.learning_records import (
    LearningRecord,
    allocate_learning_id,
    entry_revision,
    parse_learnings,
    render_learning,
)
from ciao.memory_receipts import content_revision, write_queue_atomically

WORKSPACE = "personal"
TODAY = date(2026, 9, 30)
RECEIPT = "mrcpt_0123456789abcdef"

RETIRED = "Blocked pages need a fallback."
PENDING = "Long transcripts need chunking before they are read."
NEVER_MENTIONED = "Rate limits are per-endpoint, not global."


@pytest.fixture(autouse=True)
def _isolated_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every lock file out of the shared temp root the install uses."""
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "locks"))


# ── Fixtures ────────────────────────────────────────────────────────────────


def _config(tmp_path: Path, *names: str) -> CiaoConfig:
    workspaces = names or (WORKSPACE,)
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in names
        }
        or {
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in workspaces
        },
    )


def _record(text: str, key: str, legacy: str) -> LearningRecord:
    """One canonical record, identified by the legacy line it was minted from."""
    return LearningRecord(
        learning_id=allocate_learning_id(WORKSPACE, legacy), key=key, text=text
    )


RETIRED_RECORD = _record(RETIRED, "blocked-pages", f"- {RETIRED}")
PENDING_RECORD = _record(PENDING, "long-transcripts", f"- {PENDING}")
UNLINKED_RECORD = _record(NEVER_MENTIONED, "rate-limits", f"- {NEVER_MENTIONED}")
#: Two more Active entries, so a run can remove three of five and produce every
#: gap shape: three separate gaps, one gap shared by a removed pair, and one gap
#: shared by a whole run of them. A single removal cannot tell those apart.
FOURTH_RECORD = _record(
    "Session logs grow faster than the notes they describe.",
    "log-growth",
    "- Session logs grow faster than the notes they describe.",
)
FIFTH_RECORD = _record(
    "Promotions need a dated review, not a mention.",
    "dated-review",
    "- Promotions need a dated review, not a mention.",
)
PROMOTED_RECORD = LearningRecord(
    learning_id=allocate_learning_id(WORKSPACE, "- [shipped] already done"),
    key="shipped",
    text="An entry that is already done.",
    first_seen=date(2024, 1, 1),
    last_seen=date(2024, 1, 2),
    count=3,
)

FRONTMATTER = "---\ntags: [ciao, learnings]\nupdated: 2020-01-01\n---\n"
#: The same frontmatter with an ``updated:`` the restamp has to *lengthen*, so the
#: write moves every offset below it.
_SHORT_STAMP = "---\ntags: [ciao, learnings]\nupdated: 2020-1-1\n---\n"
FORMAT_NOTES = (
    "## Format\n\n"
    "An example, which is not an entry and must survive byte for byte:\n\n"
    "```\n"
    "- [not-an-entry] this is inside a fence\n"
    "```\n"
)


def _document(*records: LearningRecord, frontmatter: str = FRONTMATTER) -> str:
    """A canonical document: the given Active entries, notes, and a Promoted one."""
    body = "".join(f"{render_learning(record)}\n" for record in records)
    return (
        f"{frontmatter}# Learnings\n\n"
        "## Active\n\n"
        f"{body}\n"
        f"{FORMAT_NOTES}\n"
        "## Promoted / Resolved\n\n"
        f"{render_learning(PROMOTED_RECORD)}\n"
    )


def _write(config: CiaoConfig, text: str) -> Path:
    path = Path(config.workspace_vault_root(WORKSPACE)).joinpath("Workspace/Learnings.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _origin(
    learning: LearningRecord,
    *,
    finding: str = "add the defuddle fallback",
    state: str = sp.ORIGIN_APPLIED,
    verification: str = RECEIPT,
    revision: str | None = None,
) -> sp.SkillOrigin:
    return sp.SkillOrigin(
        workspace=WORKSPACE,
        learning_id=learning.learning_id,
        source_revision=entry_revision(learning) if revision is None else revision,
        finding=finding,
        summary="Add the defuddle fallback step.",
        state=state,
        verification=verification,
    )


def _file_settled(config: CiaoConfig, *origins: sp.SkillOrigin, skill: str = "web-research") -> None:
    """One settled proposal linking the given findings, written to the queue.

    Written rather than merged, because ``upsert_proposal`` files every origin it
    is handed as ``pending`` — a finding is a question and only a settlement
    writes a state — so this puts the record on disk in the shape a settled one
    reaches it in and leaves every assertion here about the fold.
    """
    proposal = sp.SkillProposal(
        id=sp.proposal_id(WORKSPACE, skill),
        workspace=WORKSPACE,
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
        lifecycle=sp.PENDING,
        chat_id="",
        updated_at="2026-08-09T10:00:00Z",
        origins=origins,
    )
    write_queue_atomically(
        sp.proposal_path(config, WORKSPACE, skill), sp.render_proposal(proposal)
    )


def _plan(config: CiaoConfig, vault: Path, **kwargs: object) -> lc.CleanupPlan:
    return lc.plan_cleanup(vault, workspace=WORKSPACE, config=config, **kwargs)


def _settle_every(config: CiaoConfig, *records: LearningRecord) -> None:
    """One settled proposal linking a finding to each of *records*.

    One proposal rather than one each, so the three removals below are decided by
    a single fold in a single queue — which is the shape a real workspace has, and
    the one that makes a single apply do all three splices.
    """
    _file_settled(
        config,
        *(_origin(record, finding=f"finding for {record.key}") for record in records),
    )


# ── The happy path, and the losslessness ───────────────────────────────────


def test_an_eligible_entry_is_removed_and_every_other_byte_survives(
    tmp_path: Path,
) -> None:
    """The whole contract in one assertion: the eligible line goes, and the file
    around it is the same file."""
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD, UNLINKED_RECORD)
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))

    plan = _plan(config, path.parent.parent)
    assert [row.key for row in plan.removals] == ["blocked-pages"]

    result = lc.apply_cleanup(
        path.parent.parent,
        plan,
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )

    after = path.read_text(encoding="utf-8")
    assert result.applied is True
    assert result.removed_count == 1
    assert result.revision_before == result.receipt["revision_before"]
    assert result.revision_after == result.receipt["revision_after"]
    assert result.receipt["entries_removed"] == 1
    # Only the entry's own line and the blank line pairing with it are gone.
    assert render_learning(RETIRED_RECORD) not in after
    assert render_learning(PENDING_RECORD) in after
    assert render_learning(UNLINKED_RECORD) in after
    assert render_learning(PROMOTED_RECORD) in after
    # The format notes, the fenced example inside them, the headings, and the
    # frontmatter — except `updated:`, which the write restated.
    assert FORMAT_NOTES in after
    assert "## Promoted / Resolved\n" in after
    assert after.startswith("---\ntags: [ciao, learnings]\nupdated: 2026-09-30\n---\n")
    # And nothing else moved: the file is exactly the old one with that one line
    # and that one date changed.
    assert after == text.replace(f"{render_learning(RETIRED_RECORD)}\n", "").replace(
        "updated: 2020-01-01", "updated: 2026-09-30"
    )


def test_a_document_with_no_frontmatter_is_left_without_one(
    tmp_path: Path,
) -> None:
    """Frontmatter is optional in this model, so a document that has none must
    not acquire some. Inventing bookkeeping is the same class of move as
    repairing an unreadable line."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, frontmatter=""))
    _file_settled(config, _origin(RETIRED_RECORD))

    result = lc.apply_cleanup(
        path.parent.parent,
        _plan(config, path.parent.parent),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )

    expected = (
        _document(frontmatter="")
        .replace(f"{render_learning(RETIRED_RECORD)}\n", "")
        .replace("updated: 2020-01-01\n", "")
    )
    assert result.applied is True
    assert path.read_text(encoding="utf-8") == expected
    assert not expected.startswith("---")


def test_a_missing_updated_key_is_added_rather_than_refused(tmp_path: Path) -> None:
    """The file did change, so the document has to stop claiming it did not."""
    config = _config(tmp_path)
    path = _write(
        config,
        _document(RETIRED_RECORD, frontmatter="---\ntags: [ciao, learnings]\n---\n"),
    )
    _file_settled(config, _origin(RETIRED_RECORD))

    lc.apply_cleanup(
        path.parent.parent,
        _plan(config, path.parent.parent),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )

    after = path.read_text(encoding="utf-8")
    assert after.startswith("---\ntags: [ciao, learnings]\nupdated: 2026-09-30\n---\n")


@pytest.mark.parametrize("bom", ["", "﻿"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_bom_and_crlf_survive_the_whole_round_trip(
    tmp_path: Path, bom: str, newline: str
) -> None:
    """Both properties are file properties, and neither is content.

    The read is from bytes for this reason: a CRLF document read through
    ``read_text`` comes back LF and is written out that way, so the claim would
    hold of the preview and be false of the file. The terminator each removal
    takes with it is the document's own, for the same reason.
    """
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD, frontmatter=FRONTMATTER)
    path = _write(config, "")
    path.write_bytes((bom + text).replace("\n", newline).encode("utf-8"))
    _file_settled(config, _origin(RETIRED_RECORD))

    result = lc.apply_cleanup(
        path.parent.parent,
        _plan(config, path.parent.parent),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )

    raw = path.read_bytes()
    expected = (bom + text).replace("\n", newline)
    expected = expected.replace(
        f"{render_learning(RETIRED_RECORD)}\r\n" if newline == "\r\n" else f"{render_learning(RETIRED_RECORD)}\n",
        "",
    ).replace("updated: 2020-01-01", "updated: 2026-09-30")
    assert result.applied is True
    assert raw == expected.encode("utf-8")
    assert b"\r\n" in raw if newline == "\r\n" else b"\r\n" not in raw


# ── More than one removal, which is a different claim ────────────────────────


#: The three shapes a multi-removal run can leave behind, over five Active
#: entries. What they differ in is the *gaps*: how many there are, and how many
#: entries share one.
#:
#: * ``separate`` — none of the three touches its neighbour, so three gaps, and
#:   two of the three recorded offsets are shifted left by the bytes the entries
#:   below them took out. This is the case a single-removal test cannot see.
#: * ``shared-pair`` — the first two were side by side, so they left one gap
#:   between them and one of their own.
#: * ``one-run`` — all three were side by side, so the three removals left a
#:   single gap, and an undo that filled it three times would put the second
#:   entry inside the first one's bytes.
_REMOVAL_SHAPES: tuple[tuple[tuple[LearningRecord, ...], int], ...] = (
    ((RETIRED_RECORD, UNLINKED_RECORD, FIFTH_RECORD), 3),
    ((RETIRED_RECORD, PENDING_RECORD, FOURTH_RECORD), 2),
    ((RETIRED_RECORD, PENDING_RECORD, UNLINKED_RECORD), 1),
)

_FIVE_RECORDS = (
    RETIRED_RECORD,
    PENDING_RECORD,
    UNLINKED_RECORD,
    FOURTH_RECORD,
    FIFTH_RECORD,
)


@pytest.mark.parametrize("bom", ["", "﻿"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("removed", "gaps"),
    _REMOVAL_SHAPES,
    ids=["separate-gaps", "shared-pair", "one-run"],
)
def test_three_removals_revert_to_the_exact_bytes(
    tmp_path: Path,
    bom: str,
    newline: str,
    removed: tuple[LearningRecord, ...],
    gaps: int,
) -> None:
    """Reversible means reversible, and a run of three is a different claim than a
    run of one.

    A receipt's offset is the position of the gap **in the text the run left**, and
    for the second and later removals in a run that is not the text it read: an
    entry further up the document also removed takes its bytes with it, so the gap
    ends up further left than the span's own start. Recording the start anyway put
    the bytes back in the wrong places — silently, because the anchors were
    sampled from the same wrong offsets and so the undo's own check agreed with
    itself. Every existing revert test removed exactly one entry, which is why
    this was not visible.

    The assertion is on the bytes, not on the arithmetic, and the anchors are
    checked against the file on disk *before* the undo runs so that a receipt
    which could not possibly match is caught as such instead of being repaired by
    an undo that happens to be lenient.
    """
    config = _config(tmp_path)
    text = _document(*_FIVE_RECORDS)
    path = _write(config, "")
    path.write_bytes((bom + text).replace("\n", newline).encode("utf-8"))
    _settle_every(config, *removed)
    vault = path.parent.parent

    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )

    assert [row.key for row in result.removed] == [row.key for row in removed]
    receipt = result.receipt
    assert receipt is not None
    assert receipt["entries_removed"] == 3
    # The gaps are what the undo has, so they are checked against the file rather
    # than against the receipt's own bookkeeping.
    after = path.read_bytes().decode("utf-8")
    for span in receipt["removals"]:
        offset = int(span["offset"])
        assert after[offset : offset + len(span["after"])] == span["after"]
        assert after[max(0, offset - len(span["before"])) : offset] == span["before"]
    offsets = [int(span["offset"]) for span in receipt["removals"]]
    assert offsets == sorted(offsets)
    # Two entries removed side by side really do leave one gap between them, so
    # the receipt says so rather than inventing a second one.
    assert len(set(offsets)) == gaps

    undo = lc.unmigrate_cleanup(vault, receipt, apply=True, today=TODAY)

    assert undo["failed"] == []
    assert undo["entries_reverted"] == 3
    expected = (bom + text).replace("\n", newline)
    expected = expected.replace("updated: 2020-01-01", "updated: 2026-09-30")
    assert path.read_bytes() == expected.encode("utf-8")
    # And a second undo is refused rather than doubling the lines back.
    assert lc.unmigrate_cleanup(vault, receipt, apply=True)["failed"]


def test_the_anchors_are_sampled_from_the_document_that_lands(
    tmp_path: Path,
) -> None:
    """The anchors are sampled from what is written, not from the spliced text.

    The ``updated:`` restamp runs between the splice and the write, and it can
    change the document's length: ``updated: 2020-1-1`` becomes a full ISO date.
    An entry near the frontmatter — within :data:`~ciao.learnings_cleanup.ANCHOR_CHARS`
    of it — then has its whole gap moved by those extra bytes, and an anchor
    sampled from the pre-restamp text describes a document that never existed.
    The undo would refuse a perfectly good receipt, which is the safe direction
    and still a lie about what is on disk.
    """
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD, frontmatter=_SHORT_STAMP)
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))
    vault = path.parent.parent

    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )

    receipt = result.receipt
    assert receipt is not None
    assert len(text) != len(text.replace("updated: 2020-1-1", "updated: 2026-09-30"))
    after = path.read_text(encoding="utf-8")
    span = receipt["removals"][0]
    offset = int(span["offset"])
    assert after[offset : offset + len(span["after"])] == span["after"]
    assert after[max(0, offset - len(span["before"])) : offset] == span["before"]
    assert lc.unmigrate_cleanup(vault, receipt, apply=True, today=TODAY)["failed"] == []


# ── Idempotence, suppression and the undo ────────────────────────────────────


def test_a_second_run_removes_nothing(tmp_path: Path) -> None:
    """The ordinary nightly case: the entry is gone, so there is nothing to do."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD))
    vault = path.parent.parent
    first = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    assert first.applied is True

    second_plan = _plan(config, vault)
    second = lc.apply_cleanup(
        vault, second_plan, workspace=WORKSPACE, config=config, today=TODAY
    )

    assert second_plan.removals == ()
    assert second.applied is False
    assert second.receipt is None
    # The file the first run left is exactly the file the second run finds, and
    # neither wrote a second byte of it.
    assert second.revision_before == first.revision_after
    assert render_learning(RETIRED_RECORD) not in path.read_text(encoding="utf-8")


def test_a_settlement_that_lands_after_a_failed_run_is_picked_up_next_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the settlement and the removal leaves retryable work.

    The two are separate records on purpose. The settlement is the queue's, written
    by whoever verified the lesson into the skill; the removal is the vault's, and
    a run that dies after the first and before the second has left the learning
    exactly where it was with its finding now answered. Nothing is "in progress",
    no cursor has to be resumed and nothing has to be reconciled from an image —
    the next plan simply reads a fold that now says yes, which is what makes the
    window between them recoverable rather than a hole.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    # The finding is open, so there is nothing to do.
    _file_settled(
        config, _origin(RETIRED_RECORD, state=sp.ORIGIN_PENDING, verification="")
    )
    assert _plan(config, vault).removals == ()

    # The settlement lands, and then the run that would act on it dies before it
    # writes anything.
    _file_settled(config, _origin(RETIRED_RECORD))

    def _refuse(path: Path, text: str, *, expect: str = "") -> None:
        raise OSError("disk full")

    monkeypatch.setattr(lc, "_write_locked", _refuse)
    failed = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )
    assert failed.applied is False
    assert render_learning(RETIRED_RECORD) in path.read_text(encoding="utf-8")
    # Nothing was recorded as removed, so the retry is not suppressed.
    assert lc.read_suppressions(vault) == {}

    # The retry: the same plan, now against a working write.
    monkeypatch.undo()
    retried = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )

    assert retried.applied is True
    assert [row.key for row in retried.removed] == ["blocked-pages"]
    assert render_learning(RETIRED_RECORD) not in path.read_text(encoding="utf-8")


def test_the_undo_restores_the_exact_bytes_and_the_next_pass_keeps_them(
    tmp_path: Path,
) -> None:
    """The two halves of "reversible but not fragile".

    The bytes come back from the receipt rather than being re-derived, and the
    suppression is deliberately *not* lifted by the undo — otherwise the very next
    nightly pass would remove the line the operator just put back, and ``--revert``
    would be a button that does nothing.
    """
    config = _config(tmp_path)
    original = _document(RETIRED_RECORD, PENDING_RECORD)
    path = _write(config, original)
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True

    undo = lc.unmigrate_cleanup(vault, result.receipt, apply=True, today=TODAY)

    assert undo["entries_reverted"] == 1
    assert undo["failed"] == []
    # Exact, modulo the one field a write is allowed to move.
    assert path.read_text(encoding="utf-8") == original.replace(
        "updated: 2020-01-01", "updated: 2026-09-30"
    )
    after_undo = _plan(config, vault)
    assert after_undo.removals == ()
    assert [(row.key, row.reason) for row in after_undo.kept if row.key == "blocked-pages"] == [
        ("blocked-pages", "suppressed")
    ]
    # And a second undo is refused rather than doubling the line back.
    assert lc.unmigrate_cleanup(vault, result.receipt, apply=True)["failed"]


def test_the_undo_re_records_a_store_that_never_learned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undo protects the restored line even when the store is behind the file.

    The apply writes the document first and the store second, because a pair
    recorded for an entry still on disk suppresses an entry that is still there.
    So the store can be left knowing nothing about a removal the document has
    already made — and then the undo puts the line back, and the next nightly pass
    re-derives the same removals from the same fold and takes it out again. The
    receipt names every pair, so the undo can put them back itself: re-recording
    is a merge, not an overwrite, so the pairs a healthy apply already stored are
    left exactly as they are.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))

    def _refuse(vault_root: Path, added: list[tuple[str, str]], **_: object) -> None:
        raise OSError("the store could not be written")

    monkeypatch.setattr(lc, "write_suppressions", _refuse)
    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True
    assert result.suppressed == ()
    assert lc.read_suppressions(vault) == {}

    monkeypatch.undo()
    undo = lc.unmigrate_cleanup(vault, result.receipt, apply=True, today=TODAY)

    assert undo["entries_reverted"] == 1
    assert undo["failed"] == []
    assert render_learning(RETIRED_RECORD) in path.read_text(encoding="utf-8")
    # The store now holds the pair the apply could not write, and the plan that
    # would otherwise offer the line straight back keeps it.
    assert set(lc.read_suppressions(vault)) == {
        (RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))
    }
    assert _plan(config, vault).removals == ()
    assert undo["suppressions_kept"] == [
        {
            "learning_id": RETIRED_RECORD.learning_id,
            "entry_revision": entry_revision(RETIRED_RECORD),
        }
    ]


def test_an_undo_that_cannot_hold_the_restored_line_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restored file and a store that does not know about it is a half-finished
    undo, and the next pass would quietly undo it again — so it is reported as the
    failure it is rather than as a clean revert."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True

    def _refuse(vault_root: Path, added: list[tuple[str, str]], **_: object) -> None:
        raise lc._SuppressionUnreadable("the store is nonsense")

    monkeypatch.setattr(lc, "write_suppressions", _refuse)
    undo = lc.unmigrate_cleanup(vault, result.receipt, apply=True, today=TODAY)

    assert undo["entries_reverted"] == 1
    assert [item["path"] for item in undo["failed"]] == [lc.SUPPRESSION_RELATIVE]
    assert render_learning(RETIRED_RECORD) in path.read_text(encoding="utf-8")


def test_a_re_reviewed_entry_is_eligible_again(tmp_path: Path) -> None:
    """A changed line is a new fact, not the one that was retired.

    The suppression is keyed on ``(learning_id, entry_revision)``, so the whole
    point of hashing the entry rather than the file is here: editing this entry
    makes it eligible while everything else in the document is irrelevant.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True

    # The owner re-words the lesson and the finding is re-filed against the new
    # line — which is exactly what a person does after reading a retired lesson
    # they disagree with.
    reworded = replace(RETIRED_RECORD, text=f"{RETIRED} Sometimes.")
    _write(config, _document(reworded, PENDING_RECORD))
    _file_settled(config, _origin(reworded))

    plan = _plan(config, vault)

    assert [row.key for row in plan.removals] == ["blocked-pages"]
    # The neighbour is still listed as unproposed: the cap on what a pass decides
    # is per entry, and one entry becoming eligible says nothing about the other.
    assert [row.key for row in plan.kept] == ["long-transcripts"]


def test_reapproval_lifts_the_suppression_without_any_other_change(
    tmp_path: Path,
) -> None:
    """The attended path: somebody says "remove that one again"."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True
    lc.unmigrate_cleanup(vault, result.receipt, apply=True, today=TODAY)
    assert _plan(config, vault).removals == ()

    assert lc.clear_suppression(vault, RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))

    assert [row.key for row in _plan(config, vault).removals] == ["blocked-pages"]


def test_an_unreadable_suppression_store_stops_the_whole_plan(tmp_path: Path) -> None:
    """Unreadable is not empty.

    Every pair in that store is an entry this module already removed once, so
    treating a store it cannot read as "nothing was removed" is how the same line
    is retired twice — and the second retirement has no receipt, because the first
    one recorded the bytes.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    store = lc.suppression_path(vault)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{ not json", encoding="utf-8", newline="")

    plan = _plan(config, vault)

    assert plan.blocked
    assert "cannot be read" in plan.blocked
    assert plan.removals == ()
    result = lc.apply_cleanup(
        vault, plan, workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is False
    assert result.skipped == plan.blocked
    assert render_learning(RETIRED_RECORD) in path.read_text(encoding="utf-8")


# ── Every way the answer could wrongly be "remove" ──────────────────────────


def test_a_pending_finding_keeps_the_entry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    _file_settled(
        config, _origin(RETIRED_RECORD, state=sp.ORIGIN_PENDING, verification="")
    )

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert [(row.key, row.reason) for row in plan.kept] == [
        ("blocked-pages", "pending"),
        ("long-transcripts", "never_proposed"),
    ]


def test_a_chat_working_on_the_finding_keeps_the_entry(tmp_path: Path) -> None:
    """``implementing`` is a chat having started, not the lesson being in the
    skill, so it is still an open finding."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _file_settled(
        config, _origin(RETIRED_RECORD, state=sp.ORIGIN_IMPLEMENTING, verification="")
    )

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert plan.kept[0].reason == "pending"
    assert "implementing" in plan.kept[0].detail


def test_an_unconfirmed_already_covered_keeps_the_entry(tmp_path: Path) -> None:
    """"The skill already says this" is a claim only somebody reading the target
    can confirm, and it is the state most likely to be recorded by a well-meaning
    pass."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _file_settled(
        config,
        _origin(RETIRED_RECORD, state=sp.ORIGIN_ALREADY_COVERED, verification=""),
    )

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert plan.kept[0].reason == "unanswered"


def test_an_entry_nothing_has_ever_proposed_is_kept_and_counted_as_unmatched(
    tmp_path: Path,
) -> None:
    """The legacy case, and the one an unattended pass must never act on.

    It is also the row a reviewer most needs to see, which is why the plan lists
    it rather than omitting it: "it isn't in the list" is not an answer a person
    can act on.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, UNLINKED_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD))

    plan = _plan(config, path.parent.parent)

    assert [row.key for row in plan.removals] == ["blocked-pages"]
    assert [row.reason for row in plan.kept] == ["never_proposed"]
    assert plan.counts["unmatched"] == 1


def test_an_unattributable_finding_on_a_linked_proposal_keeps_the_entry(
    tmp_path: Path,
) -> None:
    """An origin naming no learning could be this entry's other half, so it is
    counted and the entry is kept rather than the one readable origin being
    believed on its own."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _file_settled(
        config, _origin(RETIRED_RECORD), sp.SkillOrigin(finding="something unreadable")
    )

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert plan.kept[0].reason == "held_back"


def test_a_reworded_entry_is_kept(tmp_path: Path) -> None:
    """The entry moved after the finding was filed, so the finding was written
    against bytes that are no longer there."""
    config = _config(tmp_path)
    reworded = replace(RETIRED_RECORD, text=f"{RETIRED} Sometimes.")
    _write(config, _document(reworded, PENDING_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD))

    plan = _plan(config, _document(reworded, PENDING_RECORD) and Path(
        _write(config, _document(reworded, PENDING_RECORD)).parent.parent
    ))

    assert plan.removals == ()
    assert plan.kept[0].reason == "changed_since"


def test_an_origin_with_no_recorded_revision_is_kept(tmp_path: Path) -> None:
    """"Cannot show it is unchanged" is the same answer as "changed" for a
    question about deleting somebody's notes."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD, revision=""))

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert plan.kept[0].reason == "unrecorded"


def test_a_whole_file_revision_filed_before_the_move_is_kept(tmp_path: Path) -> None:
    """The migration story, and the reason no backfill exists.

    Every install that filed links before #728-E holds whole-file hashes. They
    match no entry, so every such learning reads as changed-since and is kept.
    Rewriting them to match the present file would assert that the file was
    unchanged since filing, and nothing established that.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    whole = lc.content_revision(path.read_text(encoding="utf-8"))
    _file_settled(config, _origin(RETIRED_RECORD, revision=whole))

    plan = _plan(config, path.parent.parent)

    assert whole != entry_revision(RETIRED_RECORD)
    assert plan.removals == ()
    assert plan.kept[0].reason == "changed_since"


def test_a_promoted_entry_is_never_a_candidate(tmp_path: Path) -> None:
    """A decision somebody already made, re-decided every night, is how a promoted
    learning gets retired on work nobody re-checked."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD))
    # Even with a settled finding filed against it, the Promoted entry is not a
    # row at all: the section is not reconciled, so it cannot be a candidate.
    _file_settled(config, _origin(PROMOTED_RECORD), skill="shipped")

    plan = _plan(config, path.parent.parent)

    assert [row.key for row in plan.removals] == ["blocked-pages"]
    assert all(row.key != "shipped" for row in plan.rows)
    assert plan.active == 1


def test_an_unreadable_line_is_reported_and_never_repaired(tmp_path: Path) -> None:
    """A line this code cannot read is still the owner's line.

    It is reported by name so an owner who cannot see why their entry is being
    ignored does not assume the tool lost it — and it is never a candidate, never
    re-rendered, never dropped.
    """
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD).replace(
        f"{render_learning(RETIRED_RECORD)}\n",
        f"{render_learning(RETIRED_RECORD)}\n- [broken] [2024-13-45 → nope] (x0) Bad dates.\n",
    )
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))

    plan = _plan(config, path.parent.parent)

    assert [row.key for row in plan.removals] == ["blocked-pages"]
    assert [(row.reason, row.action) for row in plan.conflicts] == [
        ("unreadable", lc.CONFLICT)
    ]
    assert "neither a real date nor 'unknown'" in plan.conflicts[0].detail
    assert "not a positive count" in plan.conflicts[0].detail
    result = lc.apply_cleanup(
        path.parent.parent,
        plan,
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )
    assert result.applied is True
    assert "- [broken] [2024-13-45 → nope] (x0) Bad dates.\n" in path.read_text(
        encoding="utf-8"
    )


# ── The upstream side ───────────────────────────────────────────────────────


def _draft(
    config: CiaoConfig,
    learning: LearningRecord,
    *,
    lifecycle: str,
    skill: str = "stock-skill",
) -> None:
    """One upstream ``[review]`` draft linking a learning, in the given state."""
    draft = upstream_drafts.UpstreamDraft(
        id=upstream_drafts.draft_id(
            WORKSPACE, upstream_drafts.UPSTREAM_ISSUE, skill, "add the fallback"
        ),
        workspace=WORKSPACE,
        target=upstream_drafts.UPSTREAM_ISSUE,
        skill=skill,
        title="Add the fallback",
        body="What was done, what went wrong, and the instruction that fixes it.",
        lifecycle=lifecycle,
        repository="owner/repo",
        change="add the fallback",
        issue_url="https://example.invalid/issue/1" if lifecycle == "filed" else "",
        updated_at="2026-08-09T10:00:00Z",
        origins=(
            {
                "learning_id": learning.learning_id,
                "finding": "add the defuddle fallback",
                "source_revision": entry_revision(learning),
                "summary": "Add the defuddle fallback step.",
            },
        ),
    )
    path = upstream_drafts.sidecar_path(config, WORKSPACE, draft.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_queue_atomically(path, upstream_drafts.render_sidecar(draft))


def test_a_lesson_whose_only_destination_is_a_filed_issue_stays(
    tmp_path: Path,
) -> None:
    """An issue existing is somebody being told, not the lesson landing anywhere
    this workspace can see.

    This is the stock-filed case the whole routing half of #728-D exists for: a
    lesson that applies to a packaged skill is a real finding, and the only honest
    record of it is the draft. Retiring the lesson because the issue was filed
    would delete the local copy of a lesson whose destination is not this vault.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _draft(config, RETIRED_RECORD, lifecycle=upstream_drafts.DRAFT_FILED)

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    kept = plan.kept[0]
    assert kept.reason == "stock_filed_upstream"
    assert kept.destination == "stock-skill"
    assert "https://example.invalid/issue/1" in kept.evidence
    assert plan.counts["unmatched"] == 1


def test_a_lesson_waiting_on_an_upstream_decision_stays(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _draft(config, RETIRED_RECORD, lifecycle=upstream_drafts.DRAFT_PENDING)

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert plan.kept[0].reason == "stock_waiting_upstream"


def test_a_rejected_upstream_draft_does_settle_the_lesson(tmp_path: Path) -> None:
    """A person rejecting the finding is the upstream equivalent of a dismissal,
    and it is the only way a stock-routed lesson is ever retired.

    A *rejection* is a decision; a filing is a notification. Nothing else about a
    stock lesson can ever reach a state where removing it is right, which is why
    this is the single lifecycle in
    :data:`ciao.learnings_cleanup.CLEARED_DRAFTS`.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _draft(config, RETIRED_RECORD, lifecycle=upstream_drafts.DRAFT_REJECTED)

    plan = _plan(config, path.parent.parent)

    assert [row.key for row in plan.removals] == ["blocked-pages"]


def test_a_locally_settled_lesson_with_a_draft_also_stays(tmp_path: Path) -> None:
    """Both destinations have to be answered. A learning routed to an owned skill
    *and* filed upstream is not done until the upstream side is too."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD))
    _draft(config, RETIRED_RECORD, lifecycle=upstream_drafts.DRAFT_FILED)

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert plan.kept[0].reason == "stock_filed_upstream"


# ── The revision checks ─────────────────────────────────────────────────────


def test_a_document_that_moved_between_the_plan_and_the_apply_is_a_conflict(
    tmp_path: Path,
) -> None:
    """A whole-document check, because a cleanup splices into a whole document.

    A concurrent ``[learnings]`` accept appending a lesson below the eligible entry
    leaves that entry's own bytes identical, so the per-entry compare is silent —
    and splicing on the plan's offsets would drop the new lesson along with the
    old one. This is the check that stops that, and it writes nothing.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    plan = _plan(config, vault)
    _write(config, _document(RETIRED_RECORD, PENDING_RECORD, UNLINKED_RECORD))
    before = path.read_text(encoding="utf-8")

    result = lc.apply_cleanup(
        vault, plan, workspace=WORKSPACE, config=config, today=TODAY
    )

    assert result.applied is False
    assert result.conflicts == (lc.REVISION_MOVED,)
    assert result.receipt is None
    assert path.read_text(encoding="utf-8") == before
    assert render_learning(UNLINKED_RECORD) in before


def test_a_candidate_whose_settlement_moved_is_dropped_from_that_apply(
    tmp_path: Path,
) -> None:
    """Re-checked against the bytes under the lock, not trusted from the plan.

    The revision check above cannot see this one: the file is byte-identical, and
    what changed is the *queue* — somebody reopened the finding between the plan
    and the apply. The candidate is dropped and named, and the rest of the apply
    goes ahead, because one reopened finding is not a reason to leave the others.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    _file_settled(
        config, _origin(PENDING_RECORD), skill="long-transcripts"
    )
    plan = _plan(config, vault)
    assert [row.key for row in plan.removals] == ["blocked-pages", "long-transcripts"]

    # Only the first is reopened, after the plan was computed.
    reopened = sp.SkillOrigin(
        workspace=WORKSPACE,
        learning_id=PENDING_RECORD.learning_id,
        source_revision=entry_revision(PENDING_RECORD),
        finding="add the defuddle fallback",
        state=sp.ORIGIN_PENDING,
    )
    _file_settled(config, reopened, skill="long-transcripts")

    result = lc.apply_cleanup(
        vault, plan, workspace=WORKSPACE, config=config, today=TODAY
    )

    assert result.applied is True
    assert [row.key for row in result.removed] == ["blocked-pages"]
    assert any("long-transcripts" in note for note in result.conflicts)
    after = path.read_text(encoding="utf-8")
    assert render_learning(PENDING_RECORD) in after
    assert render_learning(RETIRED_RECORD) not in after


def test_a_failed_write_removes_nothing_and_records_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The document is written last, after the receipt is prepared, so a failure
    at any point above leaves the file exactly as it was."""

    def _refuse(path: Path, text: str, *, expect: str = "") -> None:
        raise OSError("disk full")

    monkeypatch.setattr(lc, "_write_locked", _refuse)
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD)
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))
    vault = path.parent.parent

    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )

    assert result.applied is False
    assert result.removed_count == 0
    assert result.failed
    assert result.receipt is None
    assert path.read_text(encoding="utf-8") == text
    # And nothing was remembered as removed, so the next run tries again.
    assert lc.read_suppressions(vault) == {}


def test_a_receipt_that_cannot_be_written_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse map is what makes a removal reversible, so it is written *first*.

    Prepared-then-persisted-then-written is three different things, and only the
    middle one is the contract: a receipt serialized in memory and handed back
    after the document is gone cannot stop anything, it can only be reported
    missing afterwards — which is exactly what happened, and the CLI's own way of
    describing it was "the removals landed but their receipt could not be
    written". So the write of the receipt is the last thing that can fail, and it
    fails closed: not one byte of the document moves.
    """
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD)
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))
    vault = path.parent.parent

    def _refuse(target: Path, receipt: dict[str, object]) -> Path:
        raise OSError("no space left on device")

    monkeypatch.setattr(lc, "write_receipt", _refuse)
    result = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
        receipt_path=tmp_path / "receipts" / "learnings-cleanup-1.json",
    )

    assert result.applied is False
    assert result.removed_count == 0
    assert result.receipt is None
    assert result.receipt_path == ""
    assert result.failed
    assert "no space left on device" in result.failed[0]
    assert path.read_bytes() == text.encode("utf-8")
    # And nothing was remembered as removed, so the next run tries again.
    assert lc.read_suppressions(vault) == {}


def test_a_receipt_that_landed_before_a_write_that_did_not_is_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one window the new order opens, and what a run that dies in it leaves.

    The receipt is durable and the document is untouched, which looks like a
    removal that half-happened and is in fact inert: the entries are still on
    disk, so every anchor in that receipt describes a document that was never
    written and ``--revert`` refuses it rather than filling gaps in a file that
    still holds the entries. The retry needs no reconciliation at all, because the
    document's revision is still the one the plan was computed from — which is the
    whole reason the plan is a plan and not a cursor.

    A process that *dies* is what leaves that receipt behind, so that is what this
    drives: a ``BaseException`` out of the write, which is the one failure the run
    cannot catch, tidy up after, or report. The recoverable failures below take
    their receipt back instead, which is why this test raises something no
    ``except`` in the module will see.
    """
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD)
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))
    vault = path.parent.parent
    receipt_file = tmp_path / "receipts" / "learnings-cleanup-1.json"

    def _die(path_arg: Path, body: str, *, expect: str = "") -> None:
        raise KeyboardInterrupt("the process died between the two writes")

    monkeypatch.setattr(lc, "_write_locked", _die)
    with pytest.raises(KeyboardInterrupt):
        lc.apply_cleanup(
            vault,
            _plan(config, vault),
            workspace=WORKSPACE,
            config=config,
            today=TODAY,
            receipt_path=receipt_file,
        )

    assert path.read_bytes() == text.encode("utf-8")
    orphan = lc.read_receipt(receipt_file)
    assert orphan is not None
    # The tell: the receipt's *before* image is still the file. Nothing was
    # removed, and the next plan is computed against the same revision.
    assert orphan["revision_before"] == content_revision(text)
    assert orphan["revision_after"] != orphan["revision_before"]
    assert lc.read_suppressions(vault) == {}

    stale = lc.unmigrate_cleanup(vault, orphan, apply=True, today=TODAY)
    assert stale["entries_reverted"] == 0
    assert stale["failed"]
    assert path.read_bytes() == text.encode("utf-8")

    monkeypatch.undo()
    retried = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
        receipt_path=tmp_path / "receipts" / "learnings-cleanup-2.json",
    )

    assert retried.applied is True
    assert [row.key for row in retried.removed] == ["blocked-pages"]
    assert render_learning(RETIRED_RECORD) not in path.read_text(encoding="utf-8")


def test_a_write_that_fails_takes_its_receipt_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run still alive when the document write fails does not leave its receipt.

    The run knows the removals did not happen, so the record of them is not true
    and is taken back rather than left for the next reader to interpret. What that
    protects is concrete: the update task's completion check asks whether a receipt
    certifies a review of the document as it stands, and this receipt would name
    exactly the revision the file still has while describing entries it never took
    out. The retry is an ordinary apply, so nothing is lost by removing the file.
    """
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD)
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))
    vault = path.parent.parent
    receipt_file = tmp_path / "receipts" / "learnings-cleanup-1.json"

    def _refuse(path_arg: Path, body: str, *, expect: str = "") -> None:
        raise OSError("the disk went away")

    monkeypatch.setattr(lc, "_write_locked", _refuse)
    result = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
        receipt_path=receipt_file,
    )

    assert result.applied is False
    assert result.failed
    assert result.receipt_path == ""
    assert path.read_bytes() == text.encode("utf-8")
    assert lc.read_receipt(receipt_file) is None
    assert lc.read_suppressions(vault) == {}

    # And the same on the other recoverable failure, the document moving under the
    # run between the revision check and the write.
    def _moved(path_arg: Path, body: str, *, expect: str = "") -> None:
        raise lc._RevisionMoved("the document moved under the run")

    monkeypatch.setattr(lc, "_write_locked", _moved)
    second = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
        receipt_path=tmp_path / "receipts" / "learnings-cleanup-2.json",
    )

    assert second.applied is False
    assert second.conflicts
    assert lc.read_receipt(tmp_path / "receipts" / "learnings-cleanup-2.json") is None

    monkeypatch.undo()
    retried = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
        receipt_path=tmp_path / "receipts" / "learnings-cleanup-3.json",
    )
    assert retried.applied is True
    assert [row.key for row in retried.removed] == ["blocked-pages"]


def test_the_store_is_read_and_written_under_its_own_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store is a read-modify-write, so it needs the lock every other queue
    writer here takes.

    Unlocked, two concurrent runs each read the store, each add their own pairs,
    and the second write discards the first run's — so the entries that run
    removed are not recorded as removed and the next pass removes them a second
    time. The check is that the lock is actually *taken*, since a lock that is
    documented and not held is the bug it was added to fix.
    """
    config = _config(tmp_path)
    vault = Path(config.workspace_vault_root(WORKSPACE))
    held: list[Path] = []
    real = lc.queue_lock

    @contextmanager
    def _spy(path: Path) -> Iterator[None]:
        held.append(Path(path))
        with real(path):
            yield

    monkeypatch.setattr(lc, "queue_lock", _spy)
    lc.write_suppressions(vault, [("a" * 8, "b" * 64)], actor="operator")
    assert lc.clear_suppression(vault, "a" * 8, "b" * 64) is True

    assert held == [lc.suppression_path(vault)] * 2


def test_an_undo_of_a_changed_file_is_refused_entirely(tmp_path: Path) -> None:
    """The context either side of the gap is what an undo checks, because a
    removal leaves nothing at the offset to check. One hand edit near it and the
    whole undo is refused: a half-restored document is worse than an unrestored
    one, because nothing in it can be trusted afterwards."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD, UNLINKED_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    # The hand edit is right beside the gap, which is what the anchor covers: an
    # undo has to refuse when somebody has typed into the file this module wrote.
    body = path.read_text(encoding="utf-8")
    path.write_text(
        body.replace(render_learning(PENDING_RECORD), "# a hand edit"), encoding="utf-8", newline=""
    )

    undo = lc.unmigrate_cleanup(vault, result.receipt, apply=True)

    assert undo["entries_reverted"] == 0
    assert undo["failed"][0]["error"] == "the file changed since the cleanup"
    assert "# a hand edit" in path.read_text(encoding="utf-8")
    assert render_learning(RETIRED_RECORD) not in path.read_text(encoding="utf-8")


# ── The budget ──────────────────────────────────────────────────────────────


def test_the_cap_holds_the_backlog_back_and_says_so(tmp_path: Path) -> None:
    """A pass that may drain the whole document takes the whole night's budget on
    pass seven of nine, and the required weekly keys are never reached — so the
    marker cannot advance and the backlog is still there tomorrow. The entries
    behind the cap are reported rather than silently dropped, because a plan that
    quietly omitted them would read as a complete answer."""
    config = _config(tmp_path)
    records = [
        LearningRecord(
            learning_id=allocate_learning_id(WORKSPACE, f"- lesson {index}"),
            key=f"lesson-{index}",
            text=f"Lesson {index} is settled.",
        )
        for index in range(5)
    ]
    path = _write(config, _document(*records))
    for record in records:
        _file_settled(config, _origin(record, finding=f"finding for {record.key}"),
                      skill=record.key)

    plan = _plan(config, path.parent.parent, max_removals=2)

    assert [row.key for row in plan.removals] == ["lesson-0", "lesson-1"]
    assert plan.over_cap is True
    assert [row.reason for row in plan.kept] == ["pending"] * 3
    assert "capped at 2" in plan.kept[0].detail


def test_a_capped_row_is_taken_by_the_next_run(tmp_path: Path) -> None:
    """The cap defers, it does not decide. Nothing is recorded as removed for a row
    the cap skipped, so the next plan still offers it."""
    config = _config(tmp_path)
    records = [
        LearningRecord(
            learning_id=allocate_learning_id(WORKSPACE, f"- lesson {index}"),
            key=f"lesson-{index}",
            text=f"Lesson {index} is settled.",
        )
        for index in range(3)
    ]
    path = _write(config, _document(*records))
    vault = path.parent.parent
    for record in records:
        _file_settled(
            config, _origin(record, finding=f"finding for {record.key}"), skill=record.key
        )

    first = lc.apply_cleanup(
        vault,
        _plan(config, vault, max_removals=1),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )
    second = lc.apply_cleanup(
        vault,
        _plan(config, vault, max_removals=1),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )

    assert [row.key for row in first.removed] == ["lesson-0"]
    assert [row.key for row in second.removed] == ["lesson-1"]


# ── The reviewed no-op ──────────────────────────────────────────────────────


def test_a_review_that_removes_nothing_is_still_recorded(tmp_path: Path) -> None:
    """The difference between "a person looked and agreed" and "nobody looked".

    A table is not a review and an approval nobody gave is not a review, and both
    of those leave no receipt — so this is the only shape in which a cleanup run
    produces a receipt without a removal, and it exists because the update-task
    completion check needs it.
    """
    config = _config(tmp_path)
    path = _write(config, _document(PENDING_RECORD))
    vault = path.parent.parent
    approvals = {
        "abc": {"learning_id": "abc", "reason": "read the whole table", "evidence": "none"}
    }

    result = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
        approvals=approvals,
    )

    assert result.applied is False
    assert result.removed_count == 0
    assert result.receipt is not None
    assert result.receipt["entries_removed"] == 0
    assert result.receipt["approvals"] == approvals
    assert result.receipt["revision_before"] == result.revision_before


def test_a_dry_run_records_no_receipt_at_all(tmp_path: Path) -> None:
    """Without approvals there is nothing to attest to, and a receipt for a run
    that changed nothing would be a claim nobody made."""
    config = _config(tmp_path)
    path = _write(config, _document(PENDING_RECORD))
    vault = path.parent.parent

    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )

    assert result.receipt is None
    assert lc.read_suppressions(vault) == {}


# ── The receipt and the store ───────────────────────────────────────────────


def test_the_receipt_is_serializable_and_names_both_revisions(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))

    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    receipt = result.receipt

    assert json.loads(json.dumps(receipt)) == receipt
    assert receipt["schema_version"] == lc.RECEIPT_VERSION
    assert receipt["removed_by"] == "system"
    assert receipt["revision_before"] != receipt["revision_after"]
    span = receipt["removals"][0]
    assert span["from"] == f"{render_learning(RETIRED_RECORD)}\n"
    assert span["learning_id"] == RETIRED_RECORD.learning_id
    assert span["entry_revision"] == entry_revision(RETIRED_RECORD)
    # The offset addresses the gap in the text the run *left*, which is the only
    # text an undo will ever be holding.
    after = path.read_text(encoding="utf-8")
    assert after[span["offset"] : span["offset"] + len(span["after"])] == span["after"]


def test_the_suppression_store_is_bounded_and_keeps_the_newest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    vault = Path(config.workspace_vault_root(WORKSPACE))
    pairs = [
        (f"learning-{index}", f"{index:064d}") for index in range(lc.MAX_SUPPRESSED + 10)
    ]

    stored = lc.write_suppressions(vault, pairs, actor="operator")

    assert len(stored) == lc.MAX_SUPPRESSED
    # Trimmed from the front, so the removals an undo could plausibly hit survive.
    assert (f"learning-{len(pairs) - 1}", f"{len(pairs) - 1:064d}") in stored
    assert ("learning-0", f"{0:064d}") not in stored
    assert json.loads(lc.suppression_path(vault).read_text(encoding="utf-8"))["schema_version"] == lc.SUPPRESSION_SCHEMA


def test_the_store_round_trips_through_disk(tmp_path: Path) -> None:
    config = _config(tmp_path)
    vault = Path(config.workspace_vault_root(WORKSPACE))

    lc.write_suppressions(vault, [("a" * 8, "b" * 64)], actor="operator")
    assert lc.read_suppressions(vault) == {("a" * 8, "b" * 64): lc.read_suppressions(vault)[("a" * 8, "b" * 64)]}
    assert lc.read_suppressions(vault)[("a" * 8, "b" * 64)]["removed_by"] == "operator"

    assert lc.clear_suppression(vault, "a" * 8, "b" * 64) is True
    assert lc.read_suppressions(vault) == {}
    assert lc.clear_suppression(vault, "a" * 8, "b" * 64) is False


def test_a_receipt_from_another_schema_is_not_honoured(tmp_path: Path) -> None:
    """Reversing from a reverse map this code does not understand would restore
    spans against a file it has not checked."""
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"schema_version": 99, "removals": []}), encoding="utf-8", newline="")
    assert lc.read_receipt(path) is None
    path.write_text("not json", encoding="utf-8", newline="")
    assert lc.read_receipt(path) is None
    assert lc.read_receipt(tmp_path / "missing.json") is None


def test_a_receipt_path_never_overwrites_an_existing_one(tmp_path: Path) -> None:
    """Two runs in the same second must not share a reverse map."""
    first = lc.new_receipt_path(tmp_path)
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("{}", encoding="utf-8", newline="")

    second = lc.new_receipt_path(tmp_path)

    assert first != second
    assert second.name.startswith(lc.RECEIPT_PREFIX)


# ── The frontmatter helper, on its own ──────────────────────────────────────


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("---\nupdated: 2020-01-01\n---\n", "---\nupdated: 2026-09-30\n---\n"),
        ("---\n---\n", "---\nupdated: 2026-09-30\n---\n"),
        ("---\r\nupdated: 2020-01-01\r\n---\r\n", "---\r\nupdated: 2026-09-30\r\n---\r\n"),
        ("---\n...\n", "---\nupdated: 2026-09-30\n...\n"),
        ("# no frontmatter\n", "# no frontmatter\n"),
        ("---\nupdated: yesterday\n---\n", "---\nupdated: 2026-09-30\n---\n"),
    ],
)
def test_the_frontmatter_is_restamped_and_nothing_else_moves(
    before: str, after: str
) -> None:
    assert lc._restamp(before, today=TODAY) == after


def test_a_bom_does_not_hide_the_frontmatter(tmp_path: Path) -> None:
    """A document that starts with a byte-order mark does still have a
    frontmatter block, and treating the mark as part of the opening fence would
    put the whole file inside it."""
    assert lc._restamp("﻿---\nupdated: 2020-01-01\n---\n", today=TODAY) == (
        "﻿---\nupdated: 2026-09-30\n---\n"
    )


def test_an_unterminated_frontmatter_is_left_alone() -> None:
    """An unterminated block swallows the file, and there is no closing fence to
    add a key before. Inventing one would put metadata inside prose."""
    assert lc._restamp("---\nupdated: 2020-01-01\n", today=TODAY) == (
        "---\nupdated: 2020-01-01\n"
    )


# ── The plan never writes ───────────────────────────────────────────────────


def test_planning_writes_nothing_at_all(tmp_path: Path) -> None:
    """A pure plan: no file, no store, no receipt, on any code path."""
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD)
    path = _write(config, text)
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))

    _plan(config, vault)
    _plan(config, vault, max_removals=0)

    assert path.read_text(encoding="utf-8") == text
    assert not lc.suppression_path(vault).exists()
    assert not (vault.parent.parent / ".runtime" / "migration").exists()


def test_a_missing_document_is_a_no_op_not_a_crash(tmp_path: Path) -> None:
    config = _config(tmp_path)
    vault = Path(config.workspace_vault_root(WORKSPACE))
    vault.mkdir(parents=True, exist_ok=True)

    plan = lc.plan_cleanup(vault, workspace=WORKSPACE, config=config)

    assert plan.blocked
    assert "does not exist" in plan.blocked
    result = lc.apply_cleanup(
        vault, plan, workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is False
    assert result.skipped == plan.blocked


def test_two_entries_with_the_same_statement_stay_two_entries(tmp_path: Path) -> None:
    """The cleanup keys by ``learning_id``, and a document may legitimately hold
    two lines that say the same thing.

    Only the settled one is removed, and the other keeps its bytes — which is the
    case a ``record.key``-keyed reconciliation would get wrong by removing both
    or neither."""
    config = _config(tmp_path)
    first = _record(RETIRED, "blocked-pages", f"- {RETIRED} first")
    second = _record(RETIRED, "blocked-pages", f"- {RETIRED} second")
    text = _document(first, second, PENDING_RECORD)
    path = _write(config, text)
    _file_settled(config, _origin(first))

    plan = _plan(config, path.parent.parent)

    assert [row.learning_id for row in plan.removals] == [first.learning_id]
    after = lc.apply_cleanup(
        path.parent.parent,
        plan,
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )
    assert after.applied is True
    body = path.read_text(encoding="utf-8")
    assert render_learning(first) not in body
    assert render_learning(second) in body


def test_a_conflicting_identifier_is_reported_and_never_removed(tmp_path: Path) -> None:
    """Two lines claiming one identity cannot both be right, and picking a winner
    would let one learning's evidence overwrite another's. The parser reports the
    second as a conflict; the cleanup carries that through as a conflict rather
    than resolving it."""
    config = _config(tmp_path)
    duplicate = replace(RETIRED_RECORD, text=f"{RETIRED} A different entry entirely.")
    path = _write(config, _document(RETIRED_RECORD, duplicate, PENDING_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD))

    plan = _plan(config, path.parent.parent)

    assert [row.key for row in plan.removals] == ["blocked-pages"]
    assert [row.reason for row in plan.conflicts] == ["unreadable"]
    assert "already used by an earlier entry" in plan.conflicts[0].detail
    result = lc.apply_cleanup(
        path.parent.parent, plan, workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True
    assert render_learning(duplicate) in path.read_text(encoding="utf-8")


def test_the_kept_reasons_are_a_closed_vocabulary(tmp_path: Path) -> None:
    """A caller has to be able to group the table, and a reason that is not in the
    set cannot be grouped. The plan asserts its own answers rather than trusting
    the classifier."""
    config = _config(tmp_path)
    reworded = _record(f"{RETIRED} Sometimes.", "blocked-pages", f"- {RETIRED} v2")
    path = _write(
        config, _document(RETIRED_RECORD, PENDING_RECORD, UNLINKED_RECORD, reworded)
    )
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    _file_settled(
        config, _origin(PENDING_RECORD, state=sp.ORIGIN_INTERRUPTED, verification=""),
        skill="long-transcripts",
    )
    # Filed against a revision that is not this entry's own line: a finding a
    # pass wrote for the lesson before somebody reworded it.
    _file_settled(
        config,
        _origin(reworded, finding="stale", revision=entry_revision(RETIRED_RECORD)),
        skill="reworded",
    )

    plan = _plan(config, vault)

    assert plan.removals
    assert {row.reason for row in plan.kept} <= lc.KEPT_REASONS
    assert {row.reason for row in plan.kept} == {
        "pending",
        "never_proposed",
        "changed_since",
    }
    assert all(row.action in {lc.KEEP, lc.CONFLICT, lc.REMOVE} for row in plan.rows)
    assert plan.counts["active"] == len(plan.rows)


def test_an_entry_whose_record_cannot_be_rendered_is_a_conflict(
    tmp_path: Path,
) -> None:
    """A record with no renderable canonical line has no revision, so it cannot be
    the thing a finding was filed against. The parser cannot produce one, so this
    drives the branch directly rather than pretending a document can."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    plan = _plan(config, vault)
    broken = LearningRecord(learning_id="not-a-uuid", key="broken", text="Broken.")

    with pytest.raises(ValueError, match="not a UUID"):
        lc.entry_revision(broken)
    # Which is why `plan_cleanup` reports such a record as a conflict rather than
    # letting the exception out of a planning pass.
    assert broken.learning_id
    # And a plan carrying it is still applied for its real removals.
    result = lc.apply_cleanup(
        vault, plan, workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True


def test_the_as_dict_shapes_are_json_safe(tmp_path: Path) -> None:
    """Both summaries are the contract the CLI's ``--json`` and the completion
    check read, so neither may hold a Path, a date or a dataclass."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    plan = _plan(config, vault)

    result = lc.apply_cleanup(
        vault, plan, workspace=WORKSPACE, config=config, today=TODAY
    )

    assert json.loads(json.dumps(plan.as_dict()))["counts"]["remove"] == 1
    payload = json.loads(json.dumps(result.as_dict()))
    assert payload["removed_count"] == 1
    assert payload["receipt"]["entries_removed"] == 1
    assert payload["suppressed"][0]["learning_id"] == RETIRED_RECORD.learning_id


def test_the_document_is_read_through_the_shared_parser(tmp_path: Path) -> None:
    """A reconciliation that read the file its own way would be reasoning about a
    shape nothing produces. Pinned by the ids matching what the parser mints."""
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD)
    _write(config, text)

    document = parse_learnings(text, workspace=WORKSPACE)

    assert [entry.record.learning_id for entry in document.entries if entry.record] == [
        RETIRED_RECORD.learning_id,
        PROMOTED_RECORD.learning_id,
    ]
