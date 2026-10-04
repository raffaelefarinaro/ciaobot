"""``FactCandidate.source_anchors`` — an imported fact, and where it came from.

The type mismatch this file exists for: ``FactCandidate.source_message_ids`` is
``tuple[int, ...]`` — Ciaobot transcript *indices*, peeled off a bullet's
``[idx=N]`` tag and parsed with ``int()`` — while a message in somebody else's
conversation is named by a provider *string* (an OpenCode ``msg_…`` id, a Claude
Code uuid). The anchors therefore cannot go in the integer field, and the honest
answer is a field of their own beside it rather than prose that loses the match.

What is pinned here:

* the new field round-trips through the record's JSON form and back;
* the ``provider:source_id:anchor`` tag is read out of a bullet's
  ``source_section`` **by code** (``assert_external_provenance``), not by a colon
  count, so a Ciaobot chat id in that position is refused;
* for an import the integer field stays empty, ``provenance`` stays
  ``"unknown"`` and ``attended`` stays ``None`` — the anchors carry the
  attribution and none of the verdicts is upgraded to agree with them;
* an accepted import's region write stamps the anchor on its receipt, so a saved
  fact can be traced back to a specific message years later — asserted through
  the accept path *and* through the PWA accept helper that feeds it, because a
  row's tag only reaches the receipt if that call site hands it over;
* a row filed before this field existed — the anchor only in ``source_section`` —
  still reads back with its anchor, and still lists.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

from ciao import fact_candidates as fc
from ciao import memory_proposals as mp
from ciao.proposal_kinds import parse_bullet

SESSION_ID = "ses_synthetic0001"
CLAUDE_SESSION_ID = "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"
TAG = f"opencode:{SESSION_ID}:msg_0003"
CHAT_ID = "chat-1a2b3c4d"
QUEUE = "Workspace/Memory-Proposals.md"


def _proposal(**overrides: Any) -> mp.MemoryProposal:
    base: dict[str, Any] = {
        "target": "memory",
        "text": "Deploys run on Thursdays.",
        "source_section": TAG,
        "payload": "",
        "citations": (),
    }
    base.update(overrides)
    return mp.MemoryProposal(**base)


def _accept(
    tmp_path: Path,
    *,
    text: str,
    source_section: str,
    target: str = "memory",
) -> dict[str, Any]:
    """Accept one row through the real path and hand back the committed receipt.

    ``accept_region_fact`` is the only durable writer a person reaches by
    pressing accept, and ``receipt_out`` is what it fills when a write actually
    happened. ``tmp_path`` is the workspace: the guide sits at its root and the
    receipt journal inside its vault.
    """
    from ciao import memory_tool as mt

    tmp_path.mkdir(parents=True, exist_ok=True)
    guide = tmp_path / "AGENTS.md"
    guide.write_text("# Guide\n", encoding="utf-8")
    mt.ensure_regions(guide)

    receipt: dict[str, Any] = {}
    outcome, _promotable = mp.accept_region_fact(
        guide_path=guide,
        target=target,
        text=text,
        vault_root=tmp_path / "vault",
        actor="operator",
        source="pwa",
        workspace="personal",
        source_section=source_section,
        receipt_out=receipt,
    )

    assert outcome == "written", outcome
    assert receipt, "a written accept commits a receipt"
    return receipt


# ── The field itself ───────────────────────────────────────────────────────


def test_the_anchor_round_trips_through_the_record_s_json_form() -> None:
    """A record written today has to read back as the same record.

    The JSON form is what the receipt stores, so a field that did not survive it
    would be a field that exists for the duration of one call.
    """
    candidate = fc.candidate_from_proposal(
        _proposal(text="Deploys run on Thursdays. [as-of: 2024-05-01]")
    )

    payload = json.loads(json.dumps(candidate.to_dict()))
    assert payload["source_anchors"] == [TAG]
    assert payload["source_message_ids"] == []
    assert payload["provenance"] == fc.PROVENANCE_UNKNOWN

    # And the field is reconstructible: nothing in the row is a key the record
    # itself does not take, so a receipt can be read back into a candidate.
    rebuilt = fc.FactCandidate(**payload)
    assert rebuilt.to_dict() == candidate.to_dict()


def test_the_default_record_carries_no_anchors() -> None:
    """A Ciaobot bullet with no external tag has no anchors, not a guessed one."""
    candidate = fc.candidate_from_proposal(
        _proposal(source_section="User corrections", citations=(3,))
    )

    assert candidate.source_anchors == ()
    assert candidate.source_message_ids == (3,)
    assert candidate.provenance == fc.PROVENANCE_CITED


# ── The tag is read by code, not by a colon count ──────────────────────────


def test_only_an_external_three_part_tag_becomes_an_anchor() -> None:
    """``provider:session_id:anchor``, verified — and nothing else.

    A heading, a bare session id, a Ciaobot chat id and a file path with two
    colons in it are all refusals of the same two rules: the leading segment must
    be a provider the import contract knows, and the tag must name an *external*
    conversation. Neither is a spelling to remember.
    """
    assert fc.external_anchor(TAG) == TAG
    assert fc.external_anchor(f"claude_code:{CLAUDE_SESSION_ID}:uuid-1") == (
        f"claude_code:{CLAUDE_SESSION_ID}:uuid-1"
    )
    for not_an_anchor in (
        "",
        "   ",
        "User corrections",
        "chat-1a2b3c4d",
        CHAT_ID,
        "opencode:ses_synthetic0001",
        f"opencode:{CHAT_ID}:msg_0003",
        f"chat-1a2b3c4d:{SESSION_ID}:msg_0003",
        f"opencode::{SESSION_ID}:msg_0003",
        "/Users/someone/Documents:notes:msg_0003",
        f"claude:{SESSION_ID}:msg_0003",
        f"opencode:{SESSION_ID}",
        f"opencode:{SESSION_ID}:",
    ):
        assert fc.external_anchor(not_an_anchor) == "", not_an_anchor


def test_an_import_records_its_anchor_and_leaves_the_verdicts_alone() -> None:
    """The anchors carry the attribution; nothing else is upgraded.

    An imported conversation has no Ciaobot transcript index to cite, so the
    integer field is empty; the evidence policy that would have graded it is gone
    (#627), so ``provenance`` is ``"unknown"`` and stays that way; and there is
    no Ciaobot turn to judge, so ``attended`` is ``None`` — never ``False``,
    which would read as "the assistant made it up".
    """
    candidate = fc.candidate_from_proposal(
        _proposal(text="Refreshes run on Sunday. [as-of: 2024-05-01]")
    )

    assert candidate.source_anchors == (TAG,)
    assert candidate.source_message_ids == (), "no Ciaobot turn to cite"
    assert candidate.provenance == fc.PROVENANCE_UNKNOWN
    assert candidate.attended is None
    assert candidate.section == TAG
    assert candidate.as_of == "2024-05-01", "the source message's own date"


# ── The bullet round trip ──────────────────────────────────────────────────


def test_the_anchor_survives_the_bullet_round_trip(tmp_path: Path) -> None:
    """``as_bullet`` → the queue file → ``parse_bullet`` → the candidate.

    This is the path a real import row takes: C4 writes the tag into
    ``source_section``, ``as_bullet`` flattens it into the bullet's
    ``_(from: …)_`` tail, and the review surface reads the bullet back out of the
    file. The anchor is still there at the end, which is what makes the receipt
    able to name a message years later.
    """
    filed = _proposal(text="Refreshes run on Sunday. [as-of: 2024-05-01]")
    bullet = filed.as_bullet()
    assert f"_(from: {TAG})_" in bullet

    queue = tmp_path / QUEUE
    queue.parent.mkdir(parents=True)
    queue.write_text(bullet + "\n", encoding="utf-8")

    rows = mp.list_proposals(queue)
    assert len(rows) == 1
    assert rows[0]["source"] == TAG
    assert rows[0]["text"] == "Refreshes run on Sunday. [as-of: 2024-05-01]"

    parsed = parse_bullet(bullet)
    assert parsed is not None
    candidate = fc.candidate_from_proposal(
        mp.MemoryProposal(
            target=parsed.kind,
            text=parsed.text,
            source_section=parsed.source,
            payload=parsed.target,
            citations=(),
            request=parsed.request,
        )
    )
    assert candidate.source_anchors == (TAG,)
    assert candidate.source_message_ids == ()
    assert candidate.provenance == fc.PROVENANCE_UNKNOWN
    assert candidate.as_of == "2024-05-01"


def test_a_row_filed_before_the_structured_field_still_lists(
    tmp_path: Path,
) -> None:
    """Backward-compatible read: an anchor that lives only in ``source_section``.

    Every row filed before this field existed carries its attribution exactly
    here, because ``as_bullet`` is the only place a bullet line can hold it. The
    new field is additive: such a row lists, reads back with its anchor, and its
    verdict is unchanged.
    """
    legacy = "- [memory] Refreshes run on Sunday.  _(from: %s)_" % TAG
    queue = tmp_path / QUEUE
    queue.parent.mkdir(parents=True)
    queue.write_text(legacy + "\n", encoding="utf-8")

    rows = mp.list_proposals(queue)
    assert [row["source"] for row in rows] == [TAG]

    parsed = parse_bullet(legacy)
    assert parsed is not None
    candidate = fc.candidate_from_proposal(
        mp.MemoryProposal(
            target=parsed.kind,
            text=parsed.text,
            source_section=parsed.source,
            payload=parsed.target,
            citations=(),
        )
    )
    assert candidate.source_anchors == (TAG,)


# ── The receipt an accepted fact leaves ────────────────────────────────────


def test_an_accepted_import_stamps_the_anchor_on_its_receipt(tmp_path: Path) -> None:
    """The committed receipt carries the anchor, not just the prose.

    This goes through the real accept path — ``accept_region_fact`` with the
    tag the queue row carries — and reads the receipt ``memory_receipts``
    committed beside the region change, because the anchor only reaches an
    accepted fact through the ``source_section`` that path builds. A test on
    ``_provenance_row`` alone passes with a hand-built proposal and would keep
    passing if the accept path stamped nothing at all, which is exactly the bug
    this pins.
    """
    receipt = _accept(
        tmp_path,
        text="Refreshes run on Sunday. [as-of: 2024-05-01]",
        source_section=TAG,
    )

    row = receipt["provenance"]
    assert row["source_anchors"] == [TAG]
    assert row["section"] == TAG
    assert row["source_message_ids"] == [], "no Ciaobot transcript index to cite"
    assert row["provenance"] == fc.PROVENANCE_UNKNOWN
    assert (
        row["as_of"] == "2024-05-01"
    ), "the source message's date, never the import date"
    assert json.loads(json.dumps(row)) == row, "the receipt row is JSON-safe"

    # The receipt is on disk, not only in the out-parameter.
    journal = tmp_path / "vault" / "Workspace" / "Memory-Receipts.jsonl"
    committed = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    assert committed["provenance"]["source_anchors"] == [TAG], (
        "the anchor is in the durable receipt, so it survives the response"
    )


def test_the_accept_route_hands_the_queue_tags_anchor_to_the_receipt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The call site, not just the function: a real queue row reaches the receipt.

    ``accept_region_fact`` doing the right thing with a tag it was handed is half
    the promise; the other half is the PWA handing it over. This drives
    ``proposal_service._promote_region_row`` — the helper behind the accept
    button — with the row ``list_proposals`` returns for an imported bullet, and
    reads the committed receipt. Dropping the ``source_section=`` argument at
    that call site is what made the field dead where it mattered, and only this
    test fails when it happens again.
    """
    from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
    from ciao.web import proposal_service

    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", lambda *a, **k: None)
    reset_reroot_cache()
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal")
        },
    )
    vault = config.workspace_vault_root("personal")
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)

    for bullet, expect in (
        (
            f"- [memory] Refreshes run on Sunday. [as-of: 2024-05-01]  _(from: {TAG})_",
            [TAG],
        ),
        ("- [memory] Deploys run on Thursdays.  _(from: User corrections)_", []),
        (f"- [memory] Handled by Ciaobot itself.  _(from: {CHAT_ID}:{SESSION_ID})_", []),
    ):
        queue = vault / QUEUE
        queue.write_text(bullet + "\n", encoding="utf-8")
        row = dict(mp.list_proposals(queue)[0])
        row["workspace"] = "personal"

        outcome = asyncio.run(proposal_service._promote_region_row(config, row))

        assert outcome.ok, outcome.as_dict()
        journal = vault / "Workspace" / "Memory-Receipts.jsonl"
        committed = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
        assert committed["provenance"]["source_anchors"] == expect, bullet


def test_a_ciaobot_proposal_stamps_no_anchors(tmp_path: Path) -> None:
    """The additive half: the field is empty for everything that is not an import.

    Same path, a row whose ``source_section`` is prose: the anchor field stays
    empty and the integer citations are still lifted, so the new field changed
    nothing for a Ciaobot bullet.
    """
    proposal = mp.MemoryProposal(
        target="memory",
        text="Deploys run on Thursdays. [idx=3]",
        source_section="User corrections",
        citations=(3,),
    )

    row = mp._provenance_row(proposal)

    assert row["source_anchors"] == []
    assert row["source_message_ids"] == [3]
    assert row["provenance"] == fc.PROVENANCE_CITED

    receipt = _accept(
        tmp_path / "ciaobot", text="Deploys run on Thursdays.", source_section="review"
    )
    assert receipt["provenance"]["source_anchors"] == [], (
        "a row with no external tag stamps no anchor, through the accept path too"
    )


def test_the_accepted_region_entry_keeps_the_source_date(tmp_path: Path) -> None:
    """Accepting an imported row writes its own date into the region.

    The region is the durable state, so what a reader sees years later is the
    fact plus the date the *conversation* had — the acceptance criterion that an
    import is never passed off as verified today. The accept path is the
    existing one, unchanged; only the row it was handed is an imported one.
    """
    from ciao import memory_tool as mt
    from ciao.memory_proposals import accept_region_fact

    guide = tmp_path / "AGENTS.md"
    guide.write_text("# Guide\n", encoding="utf-8")
    mt.ensure_regions(guide)

    outcome, _promotable = accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Refreshes run on Sunday. [as-of: 2024-05-01]",
        vault_root=tmp_path / "vault",
        actor="operator",
        source="pwa",
        workspace="personal",
    )

    assert outcome == "written", outcome
    entries = mt.read_region(guide, "memory")[0]
    assert any(
        "2024-05-01" in entry and "Sunday" in entry for entry in entries
    ), entries
    assert not any(
        f"[as-of: {date.today().isoformat()}]" in entry for entry in entries
    ), "an import is never dated by the day it was imported"
