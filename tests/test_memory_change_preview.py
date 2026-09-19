"""Preview a memory change before accepting it, and undo it from History.

The queue says what was noticed; the promotion reconciles against whatever the
destination holds *now*. These tests pin the three things that gap requires:

* a preview computed from the same functions the accept calls, so the card and
  the write cannot disagree;
* a revision handshake, so a destination that moved between preview and accept
  is a conflict with a refreshed preview instead of an unseen overwrite;
* a History join that offers Undo only where a receipt can honour it, and says
  "no snapshot" — rather than nothing — for every decision recorded before the
  receipt protocol existed. A decision names its receipt outright, which is the
  only join that survives the operator editing the wording before accepting;
  rows written before that id existed still fall back to matching on text.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web.routes_api import (
    memory_receipt_detail,
    memory_receipt_undo,
    memory_receipts,
    list_proposals,
    proposal_action,
    proposal_preview,
    proposals_batch,
    proposals_history,
)


QUEUE = """# Memory Proposals

## 2026-08-19 curation pass (this pass)

- [memory] Raffa deploys with the check-first script.  _(from: chat-1)_
- [memory] Raffa reviews PRs before merging.  _(from: chat-1)_
- [learnings] Vitest skips component files on old Node.  _(from: chat-1)_
"""


def _config(tmp_path: Path) -> CiaoConfig:
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            )
        },
    )


def _vault(tmp_path: Path) -> CiaoConfig:
    config = _config(tmp_path)
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(QUEUE, encoding="utf-8")
    return config


def _client(config: CiaoConfig) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/proposals", list_proposals, methods=["GET"]),
            Route("/api/proposals/history", proposals_history, methods=["GET"]),
            Route("/api/proposals/batch", proposals_batch, methods=["POST"]),
            Route("/api/proposals/{id}/preview", proposal_preview, methods=["GET"]),
            Route("/api/proposals/{id}/{action}", proposal_action, methods=["POST"]),
            Route("/api/memory/receipts", memory_receipts, methods=["GET"]),
            Route("/api/memory/receipts/{id}", memory_receipt_detail, methods=["GET"]),
            Route(
                "/api/memory/receipts/{id}/undo", memory_receipt_undo, methods=["POST"]
            ),
        ]
    )
    app.state.config = config
    return TestClient(app)


def _row(client: TestClient, kind: str, needle: str = "") -> dict:
    for row in client.get("/api/proposals").json()["rows"]:
        if row["kind"] == kind and (not needle or needle in row["text"]):
            return row
    raise AssertionError(f"no {kind} row matching {needle!r}")


def _guide(config: CiaoConfig) -> Path:
    return Path(config.agent_root("personal")) / "CLAUDE.md"


# ── Preview ───────────────────────────────────────────────────────────────


def test_preview_names_the_destination_and_the_exact_replacement(
    tmp_path: Path,
) -> None:
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")

    preview = client.get(f"/api/proposals/{row['id']}/preview").json()["preview"]

    assert preview["destination"] == "ciao:memory"
    assert preview["operation"] == "add"
    assert preview["exact"] is True
    assert preview["can_accept"] is True
    assert preview["revision"]
    # The replacement is the body the accept produces, stamp and all — not the
    # bullet's own text, which is what the queue already showed.
    assert row["text"] in preview["after"]
    assert row["text"] not in preview["before"]
    assert preview["after"] != preview["before"]


def test_preview_matches_what_the_accept_actually_writes(tmp_path: Path) -> None:
    """The point of the card: the after image is the destination afterwards."""
    from ciao.memory_tool import read_region, serialize_entries

    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    preview = client.get(f"/api/proposals/{row['id']}/preview").json()["preview"]

    assert (
        client.post(
            f"/api/proposals/{row['id']}/accept",
            json={"expected_revision": preview["revision"]},
        ).status_code
        == 200
    )

    entries, _diags = read_region(_guide(config), "memory")
    assert serialize_entries(entries) == preview["after"]


def test_preview_says_when_the_fact_is_already_remembered(tmp_path: Path) -> None:
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    client.post(f"/api/proposals/{row['id']}/accept")

    # Re-queue the same fact and preview it against a region that now holds it.
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    queue.write_text(QUEUE, encoding="utf-8")
    again = _row(client, "memory", "check-first")

    preview = client.get(f"/api/proposals/{again['id']}/preview").json()["preview"]
    assert preview["operation"] == "none"
    assert preview["before"] == preview["after"]
    assert preview["can_accept"] is True
    assert "already holds this" in preview["reason"]


def test_preview_refuses_an_event_shaped_bullet(tmp_path: Path) -> None:
    config = _config(tmp_path)
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n## 2026-08-19 pass\n\n"
        "- [memory] User said the deploy script had to run first, so it was run.  "
        "_(from: chat-1)_\n",
        encoding="utf-8",
    )
    client = _client(config)
    row = _row(client, "memory")

    preview = client.get(f"/api/proposals/{row['id']}/preview").json()["preview"]
    assert preview["operation"] == "none"
    assert preview["can_accept"] is False
    assert "event" in preview["reason"]


def test_preview_of_an_edited_wording_runs_against_the_same_destination(
    tmp_path: Path,
) -> None:
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")

    preview = client.get(
        f"/api/proposals/{row['id']}/preview",
        params={"text": "Raffa always deploys through scripts/check-first.sh."},
    ).json()["preview"]

    assert "scripts/check-first.sh" in preview["after"]
    assert row["text"] not in preview["after"]


def test_preview_of_a_learning_shows_the_recurrence_bump(tmp_path: Path) -> None:
    from ciao.memory_proposals import append_learning

    config = _vault(tmp_path)
    vault = Path(config.workspace_vault_root("personal"))
    append_learning(vault, "Vitest skips component files on old Node.")
    client = _client(config)
    row = _row(client, "learnings")

    preview = client.get(f"/api/proposals/{row['id']}/preview").json()["preview"]
    assert preview["destination"] == "Workspace/Learnings.md"
    # Not a second copy: the existing entry's count goes up, which is the whole
    # reason a bullet's own text cannot describe the write.
    assert preview["operation"] == "update"
    assert "(x2)" in preview["after"]
    assert "(x2)" not in preview["before"]


def test_preview_of_an_unknown_proposal_is_404(tmp_path: Path) -> None:
    client = _client(_vault(tmp_path))
    assert client.get("/api/proposals/nope/preview").status_code == 404


# ── The revision handshake ────────────────────────────────────────────────


def test_a_destination_that_changed_refuses_the_accept_and_refreshes(
    tmp_path: Path,
) -> None:
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    preview = client.get(f"/api/proposals/{row['id']}/preview").json()["preview"]

    # Something else writes the region while the card is on screen.
    other = _row(client, "memory", "reviews PRs")
    assert client.post(f"/api/proposals/{other['id']}/accept").status_code == 200

    resp = client.post(
        f"/api/proposals/{row['id']}/accept",
        json={"expected_revision": preview["revision"]},
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["conflict"] is True
    # Refreshed, not the stale one: the card can re-render without a round trip.
    assert body["preview"]["revision"] != preview["revision"]
    assert "reviews PRs" in body["preview"]["before"]
    # Nothing was written and the bullet is still queued.
    assert row["text"] not in _guide(config).read_text(encoding="utf-8")
    assert row["id"] in {r["id"] for r in client.get("/api/proposals").json()["rows"]}


def test_a_matching_revision_lets_the_accept_through(tmp_path: Path) -> None:
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    preview = client.get(f"/api/proposals/{row['id']}/preview").json()["preview"]

    resp = client.post(
        f"/api/proposals/{row['id']}/accept",
        json={"expected_revision": preview["revision"]},
    )

    assert resp.status_code == 200
    assert row["text"] in _guide(config).read_text(encoding="utf-8")


def test_an_accept_without_a_revision_behaves_as_before(tmp_path: Path) -> None:
    """Every existing client sends no body; none of them may start failing."""
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200
    assert row["text"] in _guide(config).read_text(encoding="utf-8")


def test_an_edited_wording_is_what_lands(tmp_path: Path) -> None:
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    edited = "Raffa always deploys through scripts/check-first.sh."

    assert (
        client.post(
            f"/api/proposals/{row['id']}/accept", json={"text": edited}
        ).status_code
        == 200
    )

    guide = _guide(config).read_text(encoding="utf-8")
    assert edited in guide
    assert row["text"] not in guide
    # The ORIGINAL text is what the dedupe readers compare against, so that is
    # what the decision history keeps: otherwise the curator re-queues the
    # bullet it just watched the operator accept.
    sidecar = (
        config.workspace_vault_root("personal")
        / "Workspace"
        / "Memory-Proposals.dismissed.jsonl"
    )
    texts = [json.loads(line)["text"] for line in sidecar.read_text().splitlines() if line]
    assert row["text"] in texts


# ── Bulk acceptance ───────────────────────────────────────────────────────


def test_batch_reports_a_per_destination_summary(tmp_path: Path) -> None:
    config = _vault(tmp_path)
    client = _client(config)
    ids = [r["id"] for r in client.get("/api/proposals").json()["rows"]]

    body = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": ids}
    ).json()

    summary = {entry["destination"]: entry for entry in body["summary"]}
    assert summary["ciao:memory"]["ok"] == 2
    assert summary["ciao:memory"]["failed"] == 0
    assert summary["Workspace/Learnings.md"]["ok"] == 1
    # The per-row results survive the grouping.
    assert len(body["results"]) == 3


def test_batch_fails_only_the_row_whose_destination_moved(tmp_path: Path) -> None:
    config = _vault(tmp_path)
    client = _client(config)
    stale = _row(client, "memory", "check-first")
    fresh = _row(client, "memory", "reviews PRs")
    learning = _row(client, "learnings")
    preview = client.get(f"/api/proposals/{stale['id']}/preview").json()["preview"]

    # Move the region out from under the stale card.
    from ciao.memory_tool import update_region

    update_region(_guide(config), "memory", action="add", entry="An unrelated fact.")

    body = client.post(
        "/api/proposals/batch",
        json={
            "action": "accept",
            "ids": [stale["id"], fresh["id"], learning["id"]],
            "revisions": {stale["id"]: preview["revision"]},
        },
    ).json()

    results = {entry["id"]: entry for entry in body["results"]}
    assert results[stale["id"]]["conflict"] is True
    assert results[stale["id"]]["promoted"] is False
    assert results[fresh["id"]]["promoted"] is True
    assert results[learning["id"]]["promoted"] is True
    summary = {entry["destination"]: entry for entry in body["summary"]}
    assert summary["ciao:memory"]["conflicts"] == 1
    assert summary["ciao:memory"]["ok"] == 1
    # The conflicted bullet is still queued; only it.
    queued = {r["id"] for r in client.get("/api/proposals").json()["rows"]}
    assert queued == {stale["id"]}


# ── History: changes and undo ─────────────────────────────────────────────


def test_history_points_an_accepted_row_at_its_receipt(tmp_path: Path) -> None:
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    client.post(f"/api/proposals/{row['id']}/accept")

    rows = client.get("/api/proposals/history").json()["rows"]
    accepted = next(r for r in rows if r["text"] == row["text"])

    change = accepted["change"]
    assert change["destination"] == "ciao:memory"
    assert change["undoable"] is True
    detail = client.get(f"/api/memory/receipts/{change['receipt_id']}").json()
    assert detail["has_snapshot"] is True
    assert row["text"] in detail["after"]
    assert row["text"] not in detail["before"]
    assert any(d["op"] == "added" and row["text"] in d["text"] for d in detail["diff"])


EDITED = "Raffa always runs scripts/check-first.sh before a release."


def test_history_points_an_edited_accept_at_the_change_it_made(
    tmp_path: Path,
) -> None:
    """An edited accept is undoable, and its change is the REGION write.

    The ledger records the ORIGINAL bullet while the receipt records the edited
    wording, so no text match can join the two. The decision row therefore
    carries the receipt's id outright. Borrowing the accept's queue receipt
    instead would describe the bullet's removal, and undoing THAT would re-queue
    a fact the region still holds.
    """
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    client.post(f"/api/proposals/{row['id']}/accept", json={"text": EDITED})

    rows = client.get("/api/proposals/history").json()["rows"]
    accepted = next(r for r in rows if r["text"] == row["text"])

    change = accepted["change"]
    assert change["kind"] == "region_apply"
    assert change["destination"] == "ciao:memory"
    assert change["undoable"] is True

    detail = client.get(f"/api/memory/receipts/{change['receipt_id']}").json()
    assert detail["has_snapshot"] is True
    # Before/after describe what was WRITTEN, which is the edited wording.
    assert EDITED not in detail["before"]
    assert EDITED in detail["after"]
    assert row["text"] not in detail["after"]
    assert any(d["op"] == "added" and EDITED in d["text"] for d in detail["diff"])


def test_an_edited_accept_still_records_the_original_text_in_the_ledger(
    tmp_path: Path,
) -> None:
    """The dedupe contract: the sidecar keeps the bullet the curator will re-extract.

    Recording the edited wording there would let the nightly curator re-queue
    the very bullet the operator just accepted, because ``append_proposals``
    dedupes against this sidecar and never against the destination. The receipt
    reference is additive; it must not have moved the recorded text.
    """
    from ciao.memory_proposals import read_decisions, was_promoted

    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    client.post(f"/api/proposals/{row['id']}/accept", json={"text": EDITED})

    sidecar = (
        config.workspace_vault_root("personal")
        / "Workspace"
        / "Memory-Proposals.dismissed.jsonl"
    )
    entries = [
        json.loads(line)
        for line in sidecar.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    promoted = [e for e in entries if "promoted_at" in e]
    assert len(promoted) == 1
    assert promoted[0]["text"] == row["text"]
    assert promoted[0]["text"] != EDITED
    assert promoted[0]["receipt_id"]

    decisions = read_decisions(
        config.workspace_vault_root("personal")
        / "Workspace"
        / "Memory-Proposals.md"
    )
    assert [d["text"] for d in decisions] == [row["text"]]
    # The dedupe reader the nightly curator uses still recognises the bullet,
    # and does NOT recognise the edited wording (which it never recorded).
    vault = Path(config.workspace_vault_root("personal"))
    assert was_promoted(vault, row["text"])
    assert not was_promoted(vault, EDITED)


def test_history_does_not_serve_the_raw_receipt_id_outside_change(
    tmp_path: Path,
) -> None:
    """The reference reaches the client once, inside ``change``."""
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    client.post(f"/api/proposals/{row['id']}/accept", json={"text": EDITED})

    accepted = next(
        r
        for r in client.get("/api/proposals/history").json()["rows"]
        if r["text"] == row["text"]
    )
    assert "receipt_id" not in accepted
    assert accepted["change"]["receipt_id"]


def test_history_shows_no_change_for_a_named_receipt_the_journal_lost(
    tmp_path: Path,
) -> None:
    """A recorded id that no longer resolves must not fall back to text matching.

    The id was written precisely because the text cannot identify the write; a
    silent fallback would hand such a row the bullet's own queue receipt and
    offer an undo that re-queues a fact the region still holds.
    """
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    client.post(f"/api/proposals/{row['id']}/accept")

    sidecar = (
        config.workspace_vault_root("personal")
        / "Workspace"
        / "Memory-Proposals.dismissed.jsonl"
    )
    lines = []
    for line in sidecar.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if "promoted_at" in entry:
            entry["receipt_id"] = "mrcpt_gone"
        lines.append(json.dumps(entry))
    sidecar.write_text("\n".join(lines) + "\n", encoding="utf-8")

    accepted = next(
        r
        for r in client.get("/api/proposals/history").json()["rows"]
        if r["text"] == row["text"]
    )
    assert "change" not in accepted


def test_history_matches_a_dismissal_to_its_queue_receipt(tmp_path: Path) -> None:
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    client.post(f"/api/proposals/{row['id']}/dismiss")

    rows = client.get("/api/proposals/history").json()["rows"]
    dismissed = next(r for r in rows if r["text"] == row["text"])
    assert dismissed["change"]["kind"] == "queue_resolve"
    assert dismissed["change"]["undoable"] is True


def test_history_leaves_a_legacy_decision_without_a_change(tmp_path: Path) -> None:
    """Rows recorded before receipts existed must say so, not offer an undo."""
    config = _vault(tmp_path)
    sidecar = (
        config.workspace_vault_root("personal")
        / "Workspace"
        / "Memory-Proposals.dismissed.jsonl"
    )
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "promoted_at": "2025-01-02T03:04:05+00:00",
                "kind": "memory",
                "text": "A fact decided before receipts existed.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rows = _client(config).get("/api/proposals/history").json()["rows"]
    legacy = next(r for r in rows if "before receipts" in r["text"])
    assert "change" not in legacy


def test_history_falls_back_to_text_for_a_row_with_no_receipt_id(
    tmp_path: Path,
) -> None:
    """Ledger rows written before the id existed must stay undoable.

    Their text IS the written text (nothing was edited), so the old
    accept-matches-destination-receipts join still identifies the change.
    """
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    client.post(f"/api/proposals/{row['id']}/accept")

    sidecar = (
        config.workspace_vault_root("personal")
        / "Workspace"
        / "Memory-Proposals.dismissed.jsonl"
    )
    # Rewrite the sidecar in the pre-change shape: no receipt reference at all.
    lines = []
    for line in sidecar.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        entry.pop("receipt_id", None)
        lines.append(json.dumps(entry))
    sidecar.write_text("\n".join(lines) + "\n", encoding="utf-8")

    accepted = next(
        r
        for r in client.get("/api/proposals/history").json()["rows"]
        if r["text"] == row["text"]
    )
    change = accepted["change"]
    assert change["kind"] == "region_apply"
    assert change["undoable"] is True
    assert client.post(
        f"/api/memory/receipts/{change['receipt_id']}/undo"
    ).status_code == 200
    assert row["text"] not in _guide(config).read_text(encoding="utf-8")


def test_undo_of_an_edited_accept_restores_the_region_then_refuses_a_later_edit(
    tmp_path: Path,
) -> None:
    """The edited accept's undo is real, and still guarded by the revision check.

    ``undo_receipt`` re-checks the destination's revision under the guide lock;
    threading the receipt id onto the decision row must not let History talk it
    into overwriting a write that landed afterwards.
    """
    from ciao.memory_tool import update_region

    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    client.post(f"/api/proposals/{row['id']}/accept", json={"text": EDITED})
    change = next(
        r["change"]
        for r in client.get("/api/proposals/history").json()["rows"]
        if r["text"] == row["text"]
    )
    assert EDITED in _guide(config).read_text(encoding="utf-8")

    update_region(_guide(config), "memory", action="add", entry="A later unrelated fact.")

    conflict = client.post(f"/api/memory/receipts/{change['receipt_id']}/undo")
    assert conflict.status_code == 409
    assert conflict.json()["conflict"] is True
    guide = _guide(config).read_text(encoding="utf-8")
    assert "A later unrelated fact." in guide
    assert EDITED in guide


def test_undo_restores_the_region_and_then_refuses_a_later_edit(
    tmp_path: Path,
) -> None:
    from ciao.memory_tool import update_region

    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    client.post(f"/api/proposals/{row['id']}/accept")
    change = next(
        r["change"]
        for r in client.get("/api/proposals/history").json()["rows"]
        if r["text"] == row["text"]
    )

    # A later, unrelated fact lands on the same region.
    update_region(_guide(config), "memory", action="add", entry="A later unrelated fact.")

    conflict = client.post(f"/api/memory/receipts/{change['receipt_id']}/undo")
    assert conflict.status_code == 409
    assert conflict.json()["conflict"] is True
    guide = _guide(config).read_text(encoding="utf-8")
    assert "A later unrelated fact." in guide
    assert row["text"] in guide


def test_undo_restores_a_conflict_free_operation(tmp_path: Path) -> None:
    config = _vault(tmp_path)
    client = _client(config)
    row = _row(client, "memory", "check-first")
    client.post(f"/api/proposals/{row['id']}/accept")
    change = next(
        r["change"]
        for r in client.get("/api/proposals/history").json()["rows"]
        if r["text"] == row["text"]
    )

    resp = client.post(f"/api/memory/receipts/{change['receipt_id']}/undo")

    assert resp.status_code == 200
    assert row["text"] not in _guide(config).read_text(encoding="utf-8")


def test_receipt_detail_is_404_for_an_unknown_id(tmp_path: Path) -> None:
    assert _client(_vault(tmp_path)).get("/api/memory/receipts/nope").status_code == 404


def test_a_region_diff_is_one_row_per_entry(tmp_path: Path) -> None:
    """Not per line: the region's units are entries joined by a separator.

    A serialized region ends with a newline and joins its entries with
    ``\n§\n``. Diffed as lines, appending one entry produced a blank added row
    for the trailing newline and a second one reading "§".
    """
    from ciao.memory_tool import ensure_regions, update_region

    config = _vault(tmp_path)
    client = _client(config)
    ensure_regions(_guide(config))
    update_region(_guide(config), "memory", action="add", entry="An existing fact.")
    row = _row(client, "memory", "check-first")
    client.post(f"/api/proposals/{row['id']}/accept")
    change = next(
        r["change"]
        for r in client.get("/api/proposals/history").json()["rows"]
        if r["text"] == row["text"]
    )

    diff = client.get(f"/api/memory/receipts/{change['receipt_id']}").json()["diff"]
    assert [entry["op"] for entry in diff] == ["added"]
    assert diff[0]["text"].startswith(row["text"])


def test_a_region_preview_names_its_entry_separator(tmp_path: Path) -> None:
    client = _client(_vault(tmp_path))
    row = _row(client, "memory", "check-first")
    preview = client.get(f"/api/proposals/{row['id']}/preview").json()["preview"]
    assert preview["separator"] == "\n\u00a7\n"


def test_a_file_preview_separates_by_line(tmp_path: Path) -> None:
    client = _client(_vault(tmp_path))
    row = _row(client, "learnings")
    preview = client.get(f"/api/proposals/{row['id']}/preview").json()["preview"]
    assert preview["separator"] == "\n"
