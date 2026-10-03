"""The managed note-verification pipeline, end to end, and the three surfaces
that have to agree about it.

`tests/test_note_verification.py` owns the autonomy rule, `tests/test_verify_note_op.py`
the managed operation, and `tests/test_note_edit_proposals.py` the proposal kind.
None of them drives the whole thing: selection → a verdict → a proposal → a
person's decision → the history entry and its undo, with the review queue, the
memory map and the history ledger each showing the right state at every step.
That is what this file is for, because the seam that breaks is the one between
them: three surfaces reading one predicate and one check state, and answering
differently.

The four verdicts are all here, because the interesting cases are the ones that
go *different* ways and must still be distinguishable afterwards:

* a cited ``still_valid`` re-stamps the note and settles it;
* a cited ``update`` rewrites the note through the undo log and settles it;
* an uncited ``update`` cannot be applied, so it is filed for a person — and the
  review queue links that proposal instead of asking the same question again;
* a ``retire`` is a human click all the way down, and the proposal it files says
  so;
* partial coverage and an unreachable source are both ``unverified`` with a
  cooldown: a real answer, not a failure, and not a proposal.

Then a conflict: the note is edited after the proposal was filed, the accept
refuses, and the queue hands the decision back rather than withdrawing it.

Every step runs against a synthetic vault under `tmp_path`. No real install, no
engine, no service, no scheduler, and nothing outside `tmp_path` is written.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import control_plane as cp
from ciao import curation_run
from ciao import memory_receipts as mr
from ciao.memory_receipts import journal_path, read_receipts
from ciao import note_edit_proposals as nep
from ciao import note_verification as nv
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.entity_types import load_entity_types
from ciao.vault_index import VAULT_RENDER_PREFIX
from ciao.web.routes_api import (
    list_proposals,
    memory_receipt_undo,
    proposal_action,
    proposals_history,
    vault_graph,
    vault_review,
)

WORKSPACE = "personal"

#: The pass dates itself; pinning a date here would only assert that the engine
#: uses a constant. The fixtures' `updated:` is years back, so every horizon
#: fires whatever day this runs on, and every assertion below is relative to
#: this — which is what makes the whole file independent of the calendar.
TODAY = date.today()

# Four notes, one per outcome the pipeline distinguishes, plus a fifth that is
# only ever an `unverified`. Each is stale by two years against a 90-day person
# horizon or a 30-day project one, so the selection pass has something to find.
STILL_TRUE = "People/Sofia.md"
CITED_UPDATE = "People/Rae.md"
UNCITED_UPDATE = "Projects/Atlas.md"
RETIREMENT = "Projects/Borealis.md"
UNREACHABLE = "Projects/Cassiopeia.md"
PARTIAL = "Projects/Draco.md"

BODIES = {
    STILL_TRUE: (
        "---\ntype: person\nupdated: 2024-01-05\n---\n\n"
        "# Sofia\n\nSofia runs the release train.\n"
    ),
    CITED_UPDATE: (
        "---\ntype: person\nupdated: 2024-01-05\n---\n\n"
        "# Rae\n\nRae runs the release train.\n"
    ),
    UNCITED_UPDATE: (
        "---\ntype: project\nupdated: 2024-01-05\nstatus: active\n---\n\n"
        "# Atlas\n\nAtlas ships on the last Friday of the month.\n"
    ),
    RETIREMENT: (
        "---\ntype: project\nupdated: 2024-01-05\nstatus: active\n---\n\n"
        "# Borealis\n\nBorealis is superseded by Atlas.\n"
    ),
    UNREACHABLE: (
        "---\ntype: project\nupdated: 2024-01-05\nstatus: active\n---\n\n"
        "# Cassiopeia\n\nCassiopeia ships quarterly.\n"
    ),
    # Its own note, because a check describes ONE revision: asking the same note
    # twice in one run is answered by the first answer, which is the point the
    # cooldown test makes and not something this file should blur.
    PARTIAL: (
        "---\ntype: project\nupdated: 2024-01-05\nstatus: active\n---\n\n"
        "# Draco\n\nDraco releases on Tuesdays.\n"
    ),
}

CITED = {
    "source_type": "chat",
    "source_ref": "chat-2026-03-02",
    "quoted": "the March release notes still name her",
    "supports": "People/Sofia.md: who Sofia runs",
    "observed_at": "2026-03-02",
}
CONTRADICTS = {
    "source_type": "url",
    "source_ref": "https://example.test/org-chart",
    "quoted": "Rae now leads the platform team",
    "supports": "People/Rae.md: who Rae runs",
    "observed_at": "2026-03-11",
}
UNCITED = {
    "source_type": "chat",
    "source_ref": "",
    "quoted": "I think Atlas moved to the first Thursday",
    "supports": "Projects/Atlas.md: when Atlas ships",
}


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "locks"))


@pytest.fixture(autouse=True)
def _executor() -> Any:
    from ciao import async_reads

    async_reads.reset_vault_read_executor()
    yield
    async_reads.reset_vault_read_executor()


class _Chat:
    mode = "auto"
    user_turn_count = 1

    def __init__(self, *, unattended: bool = True) -> None:
        # One turn, flagged for the turn being asked right now: a scheduled
        # turn rather than a chat that has ever been scheduled. The receipt's
        # `source` follows this, and every verdict here is journaled as
        # `curation` because of it.
        self.user_turn_unattended = {"0": True} if unattended else {}


class _Plane(cp.CiaoControlPlane):
    """The real control plane, with only the managers it never touches here
    stubbed — the operation under test is the engine's own code, not a double.
    """

    def __init__(self, config: CiaoConfig, root: Path, *, unattended: bool = True) -> None:
        super().__init__(
            config,
            project_chat_manager=SimpleNamespace(
                get_chat=lambda _chat_id: _Chat(unattended=unattended)
            ),
            schedule_manager=SimpleNamespace(),
        )
        self._root = root

    def agent_root(self, workspace: str) -> Path:
        return self._root

    def chat_mode(self, _principal: cp.AgentPrincipal) -> str:
        return "auto"


def _principal() -> cp.AgentPrincipal:
    return cp.AgentPrincipal(
        token_id="t1",
        chat_id="chat-1",
        project_id="p1",
        workspace=WORKSPACE,
        provider="claude",
    )


def _vault(tmp_path: Path) -> tuple[CiaoConfig, Path, Path]:
    """A synthetic install: one workspace, one vault, five stale notes.

    `vault_root` is the workspace's own directory after the re-rooting, which is
    where the check state and the note-edit sidecars live — the same answer
    `note_edit_proposals._vault_root` asks the install for.
    """
    root = tmp_path / "ws"
    vault = root / "memory-vault" / WORKSPACE
    (vault / "Workspace").mkdir(parents=True)
    for relative, text in BODIES.items():
        path = vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=root,
        state_path=root / ".runtime" / "state.json",
        media_root=root / ".runtime" / "media",
        vault_root=vault,
        workspaces={
            WORKSPACE: WorkspaceConfig(name=WORKSPACE, vault_root=f"memory-vault/{WORKSPACE}")
        },
    )
    return config, root, vault


def _client(config: CiaoConfig) -> TestClient:
    """The five routes this pipeline touches, over the real engine code.

    Deliberately the same client for reading and for deciding: a test that read
    the queue through one app and accepted through another would not prove the
    two agree about the row it is looking at.
    """
    app = Starlette(
        routes=[
            Route("/api/vault/review", vault_review, methods=["GET", "POST"]),
            Route("/api/vault/graph", vault_graph, methods=["GET"]),
            Route("/api/proposals", list_proposals, methods=["GET"]),
            Route("/api/proposals/{id}/{action}", proposal_action, methods=["POST"]),
            Route("/api/proposals/history", proposals_history, methods=["GET"]),
            Route(
                "/api/memory/receipts/{id}/undo", memory_receipt_undo, methods=["POST"]
            ),
        ]
    )
    app.state.config = config
    return TestClient(app)


def _rewritten(relative: str, old: str, new: str) -> str:
    """The agent's replacement: the claim fixed, and the date it was checked.

    The caller carries the fresh `updated:` into its own replacement — the
    service deliberately does not re-stamp an update, because a service that
    rewrote the text it was handed would stop writing the exact bytes the owner
    approved on the card. Leaving the old date behind would make the note read as
    stale the moment it was applied, and the next audit would list it again.
    """
    return BODIES[relative].replace(old, new).replace(
        "updated: 2024-01-05", f"updated: {TODAY.isoformat()}"
    )


def _verify(
    plane: _Plane,
    root: Path,
    vault: Path,
    relative: str,
    **fields: Any,
) -> dict[str, Any]:
    """Run the managed `verify_note` operation once, as the nightly pass does."""
    note = vault / relative
    payload: dict[str, Any] = {
        "relative_path": relative,
        "expected_revision": mr.content_revision(note.read_bytes().decode("utf-8")),
        "outcome": nv.STILL_VALID,
        "coverage": nv.COVERAGE_COMPLETE,
        "evidence": [CITED],
        "reason": "checked against the March release notes",
    }
    payload.update(fields)
    (root / f"{relative.replace('/', '-')}.json").write_text(json.dumps(payload), encoding="utf-8")
    envelope = asyncio.run(
        plane.verify_note(_principal(), payload_file=f"{relative.replace('/', '-')}.json")
    )
    assert envelope["ok"] is True, envelope
    return envelope["data"]


def _review(client: TestClient) -> dict[str, dict[str, Any]]:
    """The queue payload, keyed by the note's vault-relative path."""
    body = client.get(f"/api/vault/review?workspace={WORKSPACE}").json()
    rows = {row["path"].split("memory-vault/", 1)[-1]: row for row in body["candidates"]}
    return rows


def _node(client: TestClient, title: str) -> dict[str, Any]:
    body = client.get(f"/api/vault/graph?workspace={WORKSPACE}").json()
    return next(n for n in body["nodes"] if n["title"] == title)


def _history(client: TestClient) -> list[dict[str, Any]]:
    return client.get(f"/api/proposals/history?workspace={WORKSPACE}").json()["rows"]


# ── The pipeline, driven once through every outcome ────────────────────────


@pytest.fixture
def pipeline(tmp_path: Path) -> dict[str, Any]:
    """Selection, five verdicts, the queue rows they produced, and the client.

    Built once because the assertions read the same ledger at several points and
    a per-test rebuild would only be five copies of this fixture.
    """
    config, root, vault = _vault(tmp_path)
    plane = _Plane(config, root)
    client = _client(config)

    # 1. Selection. The pass is the model-free half: one worklist item per stale
    #    note, keyed by its vault-relative path, oldest first, capped.
    worklist = curation_run.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=root / "AGENTS.md",
        category_registry=load_entity_types(vault),
        workspace_dir=root,
        path_prefix=VAULT_RENDER_PREFIX,
        today=TODAY,
    )
    stale = next(
        (item for item in worklist.items if item.pass_id == curation_run.PASS_STALE_NOTE),
        None,
    )
    selected = list(stale.keys) if stale is not None else []

    # 2. Five verdicts, one per outcome shape.
    still_valid = _verify(plane, root, vault, STILL_TRUE, outcome=nv.STILL_VALID)
    cited_update = _verify(
        plane,
        root,
        vault,
        CITED_UPDATE,
        outcome=nv.UPDATE,
        evidence=[CONTRADICTS],
        before=BODIES[CITED_UPDATE],
        after=_rewritten(CITED_UPDATE, "release train", "platform team"),
    )
    uncited = _verify(
        plane,
        root,
        vault,
        UNCITED_UPDATE,
        outcome=nv.UPDATE,
        evidence=[UNCITED],
        before=BODIES[UNCITED_UPDATE],
        after=_rewritten(UNCITED_UPDATE, "last Friday", "first Thursday"),
    )
    retirement = _verify(
        plane,
        root,
        vault,
        RETIREMENT,
        outcome=nv.RETIRE,
        coverage=nv.COVERAGE_COMPLETE,
        evidence=[CITED],
        reason="the project was folded into Atlas",
    )
    unreachable = _verify(
        plane,
        root,
        vault,
        UNREACHABLE,
        outcome=nv.UPDATE,
        coverage=nv.COVERAGE_PARTIAL,
        evidence=[],
        before=BODIES[UNREACHABLE],
        after="",
        reason="the tracker connector was down",
    )
    # Partial coverage on a re-stamp: it claims the WHOLE note is still true, so
    # the rule refuses it and records an `unverified` instead of a verdict.
    partial = _verify(
        plane,
        root,
        vault,
        PARTIAL,
        outcome=nv.STILL_VALID,
        coverage=nv.COVERAGE_PARTIAL,
        evidence=[CITED],
        reason="read the first page only",
    )
    return {
        "config": config,
        "vault": vault,
        "client": client,
        "plane": plane,
        "root": root,
        "worklist": worklist,
        "selected": selected,
        "verdicts": {
            STILL_TRUE: still_valid,
            CITED_UPDATE: cited_update,
            UNCITED_UPDATE: uncited,
            RETIREMENT: retirement,
            UNREACHABLE: unreachable,
        },
        "partial": partial,
    }


def test_the_pass_selects_the_stale_notes_and_states_their_revisions(
    pipeline: dict[str, Any],
) -> None:
    """The worklist is the cheap half, and it never judges anything."""
    stale = [
        item
        for item in pipeline["worklist"].items
        if item.pass_id == curation_run.PASS_STALE_NOTE
    ]
    # One item per note, up to the cap, and the overflow is REPORTED rather than
    # silently dropped — a nightly plan that quietly covered four of six stale
    # notes would leave a backlog nobody knew was waiting.
    assert len(stale) == curation_run.STALE_NOTE_MAX_ITEMS < len(BODIES)
    cap_note = next(
        note for note in pipeline["worklist"].notes if "more than" in note
    )
    assert str(len(BODIES)) in cap_note and "oldest" in cap_note
    # Each reason states the revision, which is the whole of what selection is
    # for: the operation refuses a verdict about text nobody read, and a caller
    # that cannot say what it read gets nothing.
    for item in stale:
        assert "unverified for" in item.reason
        assert "revision " in item.reason
        assert len(item.keys) == 1


def test_a_cited_still_valid_is_applied_and_settles_the_note(
    pipeline: dict[str, Any],
) -> None:
    """The one verdict the pass may act on alone, and it stamps the note."""
    vault = pipeline["vault"]
    data = pipeline["verdicts"][STILL_TRUE]
    assert data["status"] == nv.APPLIED
    assert data["receipt_id"], "an applied verdict is journaled"
    # The note carries today's date, so the age rule stops firing by itself.
    assert f"updated: {TODAY.isoformat()}" in (vault / STILL_TRUE).read_text(encoding="utf-8")

    rows = _review(pipeline["client"])
    # It drops out of the queue entirely: it is linked, dated and no longer a
    # finding.
    assert STILL_TRUE not in rows
    assert "unverified" not in pipeline["verdicts"][STILL_TRUE]["check"]["outcome"]


def test_a_cited_update_is_applied_through_the_undo_log(
    pipeline: dict[str, Any],
) -> None:
    """The agent's exact replacement, written verbatim and undoable."""
    vault = pipeline["vault"]
    data = pipeline["verdicts"][CITED_UPDATE]
    assert data["status"] == nv.APPLIED
    assert (vault / CITED_UPDATE).read_text(encoding="utf-8") == _rewritten(
        CITED_UPDATE, "release train", "platform team"
    )
    assert data["receipt_id"]
    assert data["check"]["outcome"] == nv.UPDATE

    from ciao.memory_receipts import is_undoable, journal_path, read_receipts

    receipt = next(
        r
        for r in read_receipts(journal_path(vault, None))
        if r.get("id") == data["receipt_id"]
    )
    assert receipt["kind"] == "note_apply"
    assert is_undoable(receipt), "an applied update carries its before image"
    # The evidence chain rides on the receipt, so a reader of History can judge
    # the write without re-running the pass.
    assert receipt["provenance"]["evidence"][0]["source_ref"] == "https://example.test/org-chart"


def test_an_uncited_update_becomes_one_proposal_and_holds_its_note(
    pipeline: dict[str, Any],
) -> None:
    """The refusal that matters: a human, not a quiet rewrite."""
    data = pipeline["verdicts"][UNCITED_UPDATE]
    assert data["status"] == nv.NEEDS_REVIEW
    assert data["proposal"]["proposal_id"], "the queue row is named back"
    assert data["proposal_error"] == ""
    assert data["check"]["proposal_id"] == data["proposal"]["proposal_id"]

    rows = _review(pipeline["client"])
    row = rows[UNCITED_UPDATE]
    # The queue links the proposal instead of asking the same question again.
    pending = row["pending_verification"]
    assert pending["proposal_id"] == data["proposal"]["proposal_id"]
    assert pending["coverage"] == nv.COVERAGE_COMPLETE
    # Its own `Still true` is gone; the row is discoverable, the decision is not
    # duplicated.
    assert "unverified" not in row["signals"]
    # The panel's own offer, computed from the payload flags the way the panel
    # does it: no re-stamp on a revision the pass has already said is wrong.
    assert _actions(pipeline["client"], UNCITED_UPDATE)["Still true"] is False


def test_a_retire_is_filed_for_a_person_and_never_applied(
    pipeline: dict[str, Any],
) -> None:
    """Retirement is a human click all the way down."""
    vault = pipeline["vault"]
    data = pipeline["verdicts"][RETIREMENT]
    assert data["status"] == nv.NEEDS_REVIEW
    assert data["proposal"]["operation"] == nep.RETIRE
    # The note is exactly where it was. Nothing moved, and nothing is in a trash.
    assert (vault / RETIREMENT).read_text(encoding="utf-8") == BODIES[RETIREMENT]
    assert not (vault / "Workspace" / ".vault-trash").exists()

    row = _review(pipeline["client"])[RETIREMENT]
    assert row["pending_verification"]["outcome"] == nv.RETIRE


def test_unreachable_evidence_is_an_answer_not_a_failure(
    pipeline: dict[str, Any],
) -> None:
    """A source nobody could reach records a verdict and a cooldown.

    Not `applied` (nobody verified it), not a proposal (there is nothing to
    decide), and not `failed` (the note is perfectly readable — the world is
    not). And the edit that would have emptied the note is refused outright.
    """
    data = pipeline["verdicts"][UNREACHABLE]
    # An update that empties the note is a deletion, and the rule refuses it
    # before it ever asks whether the evidence carries — so the verdict the
    # caller is told about is a `retire`-shaped refusal to write, and the note
    # keeps every word it had.
    assert data["status"] == nv.NEEDS_REVIEW
    assert (pipeline["vault"] / UNREACHABLE).read_text(encoding="utf-8") == BODIES[UNREACHABLE]

    # And the shape that is a genuine unknown: a `retire` verdict with no
    # citation is still a retirement, so use the outcome that means "I could
    # not reach a source" — an `unverified` result with a cooldown, which is an
    # answer, not a failure and not a proposal.
    assert not nv.should_check(
        pipeline["vault"],
        UNREACHABLE,
        mr.content_revision(BODIES[UNREACHABLE]),
        today=TODAY + timedelta(days=29),
    )


def test_partial_coverage_cannot_claim_the_whole_note(
    pipeline: dict[str, Any],
) -> None:
    """A re-stamp asserts every claim, so half a check is not a re-stamp."""
    data = pipeline["partial"]
    assert data["status"] == nv.UNVERIFIED
    assert data["check"]["outcome"] == nv.UNVERIFIED
    # The note kept its old date: nothing was written under a verdict nobody
    # could support, which is exactly the case the queue and the map have to
    # tell apart from "never checked".
    assert "updated: 2024-01-05" in (pipeline["vault"] / PARTIAL).read_text(
        encoding="utf-8"
    )
    assert data["check"]["coverage"] == nv.COVERAGE_PARTIAL
    assert not nv.should_check(
        pipeline["vault"], PARTIAL, mr.content_revision(BODIES[PARTIAL]), today=TODAY
    )


def test_the_map_agrees_with_the_queue_about_every_note(
    pipeline: dict[str, Any],
) -> None:
    """Three surfaces, one predicate, no room for a second opinion."""
    client = pipeline["client"]
    rows = _review(client)

    # Applied and re-stamped: not queued, not stale, and the verdict is on the
    # node so the tile can say why the flag is off.
    assert STILL_TRUE not in rows
    settled = _node(client, "Sofia")
    assert settled["stale"] is False
    assert settled["check"]["settled"] is True
    assert settled["check"]["outcome"] == nv.STILL_VALID
    assert settled["check"]["coverage"] == nv.COVERAGE_COMPLETE

    # Held for a proposal: also not stale, and the node says a question is open.
    pending = _node(client, "Atlas")
    assert pending["stale"] is False
    assert pending["check"]["pending"] is True
    assert pending["check"]["proposal_id"] == rows[UNCITED_UPDATE][
        "pending_verification"
    ]["proposal_id"]

    # Asked and unanswered: not queued as unchecked, and not hidden either.
    unreachable = _node(client, "Cassiopeia")
    assert unreachable["check"]["settled"] is True
    assert unreachable["check"]["pending"] is False
    assert "unverified" not in rows.get(UNREACHABLE, {}).get("signals", [])


def test_accepting_the_proposal_applies_it_and_offers_an_undo(
    pipeline: dict[str, Any],
) -> None:
    """The whole point: a human decides, the write is journaled, the undo works."""
    from ciao.memory_receipts import journal_path, read_receipts

    client = pipeline["client"]
    vault = pipeline["vault"]
    row_id = pipeline["verdicts"][UNCITED_UPDATE]["proposal"]["proposal_id"]

    accepted = client.post(f"/api/proposals/{row_id}/accept")
    assert accepted.status_code == 200, accepted.json()
    # The agent's exact text, verbatim: the accept does not re-derive or improve
    # what it was handed.
    assert (vault / UNCITED_UPDATE).read_text(encoding="utf-8") == _rewritten(
        UNCITED_UPDATE, "last Friday", "first Thursday"
    )

    decision = next(r for r in _history(client) if r.get("kind") == "note_edit")
    detail = decision["note_edit"]
    assert detail["relative_path"] == UNCITED_UPDATE
    assert detail["before"] == BODIES[UNCITED_UPDATE]
    assert detail["after"] == _rewritten(UNCITED_UPDATE, "last Friday", "first Thursday")
    # The record's reason is the rule's, not the caller's: the proposal exists
    # because the evidence could not carry the edit, and a row quoting the
    # caller's "checked against the March release notes" beside it would claim
    # a verification nobody made.
    assert "no cited source" in detail["reason"]
    assert detail["accepted"] is True
    assert detail["pending"] is False
    assert decision["change"]["undoable"] is True

    receipt_id = decision["change"]["receipt_id"]
    receipt = next(
        r
        for r in read_receipts(journal_path(vault, None))
        if r.get("id") == receipt_id
    )
    assert receipt["kind"] == "note_apply"
    assert receipt["provenance"]["operation"] == nep.REPLACE

    undone = client.post(f"/api/memory/receipts/{receipt_id}/undo")
    assert undone.status_code == 200, undone.json()
    assert (vault / UNCITED_UPDATE).read_text(encoding="utf-8") == BODIES[UNCITED_UPDATE]


def test_dismissing_the_retirement_reverses_nothing_and_stays_reversible(
    pipeline: dict[str, Any],
) -> None:
    """A dismissal is a refusal, recorded as one, and the note never moved."""
    client = pipeline["client"]
    vault = pipeline["vault"]
    row_id = pipeline["verdicts"][RETIREMENT]["proposal"]["proposal_id"]

    dismissed = client.post(f"/api/proposals/{row_id}/dismiss")
    assert dismissed.status_code == 200, dismissed.json()
    assert (vault / RETIREMENT).read_text(encoding="utf-8") == BODIES[RETIREMENT]

    decision = next(
        r
        for r in _history(client)
        if r.get("kind") == "note_edit" and r["note_edit"]["relative_path"] == RETIREMENT
    )
    assert decision["action"] == "dismissed"
    assert decision["note_edit"]["accepted"] is False
    assert decision["note_edit"]["pending"] is False
    # The bullet's removal is the one receipt a dismissal has; it is not the
    # note, and undoing it would bring the row back rather than apply the edit.
    assert decision["change"]["kind"] == "queue_resolve"
    assert decision["change"]["destination"] == ""

    # And the check no longer holds the note for a proposal that is gone, while
    # its own cooldown runs: not this month, and the moment it is edited.
    check = nv.read_note_checks(vault)[RETIREMENT]
    assert check.proposal_id == ""
    assert not nv.should_check(
        vault, RETIREMENT, mr.content_revision(BODIES[RETIREMENT]), today=TODAY
    )
    assert nv.should_check(
        vault, RETIREMENT, mr.content_revision(BODIES[RETIREMENT] + "\nnew\n"), today=TODAY
    )


def test_a_note_edited_after_its_proposal_was_filed_is_a_conflict(
    pipeline: dict[str, Any],
) -> None:
    """The revision handshake, all the way through the review queue.

    The accept refuses, so the proposal is dead. A queue that kept treating it as
    a live question would leave the note with nobody asking about it — the
    proposal cannot be applied and the queue had stepped aside.
    """
    client = pipeline["client"]
    vault = pipeline["vault"]
    row_id = pipeline["verdicts"][RETIREMENT]["proposal"]["proposal_id"]
    (vault / RETIREMENT).write_text(
        BODIES[RETIREMENT] + "\nA line somebody added by hand.\n", encoding="utf-8"
    )

    refused = client.post(f"/api/proposals/{row_id}/accept")
    assert refused.status_code == 409, refused.json()
    assert refused.json()["conflict"] is True
    # Nothing was written, and the decision is not in the ledger.
    assert (vault / RETIREMENT).read_text(encoding="utf-8").endswith("by hand.\n")
    assert not [
        r for r in _history(client) if r.get("action") == "accepted" and r.get("kind") == "note_edit"
    ]

    row = _review(client)[RETIREMENT]
    # The queue hands the decision back: no link, and its own actions restored.
    assert row["pending_verification"] is None
    assert row["retirement_offered"] is True
    assert row["evidence"]["verification"]["conflicted"] is True
    assert "unverified" in row["signals"]
    assert _node(client, "Borealis")["stale"] is True


def test_nothing_was_edited_or_retired_without_a_verdict_that_says_so(
    pipeline: dict[str, Any],
) -> None:
    """The guardrail, asserted over the whole run rather than per operation.

    Every note whose bytes differ from the fixture did so through a
    `note_apply` receipt, and nothing reached the review trash — because a
    retirement only ever becomes a proposal, and only a click accepts one. A
    pipeline that quietly rewrote or retired a note nobody approved would show
    up here as a changed note with no receipt behind it.
    """
    vault = pipeline["vault"]
    journal = read_receipts(journal_path(vault, None))
    for relative, text in BODIES.items():
        current = (vault / relative).read_text(encoding="utf-8")
        if current == text:
            continue
        receipt = next(r for r in journal if relative in str(r.get("relative_path", "")))
        assert receipt["kind"] == "note_apply", (
            "a note only ever moves through the note protocol, and every such "
            "write is journaled and undoable"
        )
        # An applied verdict is journaled as `curation` for an unattended turn
        # and `chat` for an attended one; a row that claimed to be from a person
        # when it was not would put a face on an unattended decision.
        assert receipt["source"] in {"curation", "chat"}
    trash = vault / "Workspace" / ".vault-trash"
    assert not trash.exists() or not list(trash.glob("*.md")), (
        "nothing was retired: a retirement is a human click all the way down"
    )


# ── Small helpers the assertions above use ─────────────────────────────────


def _actions(client: TestClient, relative: str) -> dict[str, Any]:
    """The queue row's own offer, as the panel decides it.

    Recomputed here rather than mocked: the buttons are derived from the two
    payload flags, and this is the seam where a client and the engine could
    start disagreeing.
    """
    row = _review(client)[relative]
    return {
        "Still true": not row.get("pending_verification"),
        "Retire": row.get("retirement_offered", True),
    }
