"""History opens the archive it names at the turn the fact came from.

A decision row already carries the archive it came from (``source_path``),
which answers "which file". It did not answer "where in the file": a transcript
can hold hundreds of turns, and the viewer opened every one of them at line 1.
The receipt records the turns the write cited, so the row can carry the line of
that turn's ``## Turn N`` heading.

These tests pin the three states that matter: a citation that resolves, a
citation that no longer resolves (the archive must open at the top rather than
at a guess), and no citation at all (an operator edit or an import names no
turn).
"""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web.routes_api import (
    _attach_source_line,
    _cited_turns,
    list_proposals,
    proposal_action,
    proposals_history,
)


def _archive(turns: int) -> str:
    """A transcript in the shape the renderer writes, with ``turns`` turns."""
    out = ["---", "provider: opencode", "---", ""]
    for turn in range(1, turns + 1):
        out += [
            f"## Turn {turn}",
            "",
            "- Time: 2026-08-01T10:00:00Z",
            "- Input kind: text",
            "- Mode: bypass",
            "- Effective model: sonnet",
            "- Images: 0",
            "",
            "### User",
            "",
            "```text",
            f"question {turn}",
            "```",
            "",
            "### Assistant",
            "",
            "```text",
            f"answer {turn}",
            "```",
            "",
        ]
    return "\n".join(out)


def _config(tmp_path: Path, bullet: str) -> CiaoConfig:
    config = CiaoConfig(
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
    queue = (
        config.workspace_vault_root("personal")
        / "Workspace"
        / "Memory-Proposals.md"
    )
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "## 2026-08-19 curation pass (this pass)\n\n"
        f"- [memory] {bullet}  _(from: chat-1)_\n",
        encoding="utf-8",
    )
    return config


def _write_archive(config: CiaoConfig, body: str) -> Path:
    """Put one archived transcript where ``_source_index`` looks for it."""
    root = config.chats_roots()[0]
    path = root / "chat-1" / "opencode" / "chat-1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _client(config: CiaoConfig) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/proposals", list_proposals, methods=["GET"]),
            Route("/api/proposals/history", proposals_history, methods=["GET"]),
            Route("/api/proposals/{id}/{action}", proposal_action, methods=["POST"]),
        ]
    )
    app.state.config = config
    return TestClient(app)


def _accept_plain_row(client: TestClient) -> dict:
    """Accept the fixture's own bullet, which cites nothing.

    A cited bullet cannot go through the accept path (``[idx=N]`` is an
    event-shape marker, so the guard keeps it queued for a rephrase), which is
    why the provenance these tests read is written the way a region write
    writes it: through :func:`commit_region_change`.
    """
    row = client.get("/api/proposals").json()["rows"][0]
    client.post(f"/api/proposals/{row['id']}/accept")
    return row


def test_history_points_the_source_at_the_turn_the_fact_cited(
    tmp_path: Path,
) -> None:
    from ciao.memory_proposals import record_promotion
    from ciao.memory_receipts import commit_region_change, journal_path

    config = _config(tmp_path, "Raffa deploys with the check-first script.")
    archive = _write_archive(config, _archive(3))
    client = _client(config)
    _accept_plain_row(client)

    vault = config.workspace_vault_root("personal")
    guide = Path(config.agent_root("personal")) / "AGENTS.md"
    receipt = commit_region_change(
        guide,
        "memory",
        entries=["Raffa reviews PRs before merging."],
        actor="operator",
        source="chat-1",
        vault_root=vault,
        fact_text="Raffa reviews PRs before merging.",
        destination="ciao:memory",
        provenance={
            "schema": "fact-candidate/v1",
            "source_message_ids": [2],
            "source_anchors": [],
            "provenance": "cited",
            "section": "memory",
            "as_of": "",
            "expires": "",
            "extraction_version": "insights-markdown/v1",
            "policy_version": "evidence-policy/v1",
        },
    )
    record_promotion(
        vault / "Workspace" / "Memory-Proposals.md",
        text="Raffa reviews PRs before merging.",
        kind="memory",
        via="pwa",
        source="chat-1",
        destination="ciao:memory",
        receipt_id=str(receipt["id"]),
    )
    assert journal_path(vault, None).exists()

    rows = client.get("/api/proposals/history").json()["rows"]
    row = next(r for r in rows if r["text"] == "Raffa reviews PRs before merging.")

    assert row["source_path"] == str(archive)
    assert row["change"]["source_turns"] == [2]
    assert row["source_turn"] == 2
    # The heading itself, not a line that merely holds the text: the viewer
    # jumps here and must land on the turn.
    lines = archive.read_text(encoding="utf-8").split("\n")
    assert lines[row["source_line"] - 1] == "## Turn 2"


def test_a_cited_turn_the_archive_no_longer_has_opens_at_the_top(
    tmp_path: Path,
) -> None:
    """A transcript an operator edited must not produce a line that now points
    at whatever happens to sit at that offset."""
    archive = _write_archive(_config(tmp_path, "x"), _archive(2))
    row: dict = {"change": {"source_turns": [9]}}
    _attach_source_line(row, str(archive), 9, {})

    assert "source_line" not in row
    assert "source_turn" not in row


def test_an_unreadable_archive_opens_at_the_top(tmp_path: Path) -> None:
    row: dict = {"change": {"source_turns": [1]}}
    _attach_source_line(row, str(tmp_path / "gone.md"), 1, {})

    assert "source_line" not in row


def test_a_receipt_with_no_evidence_names_no_turn(tmp_path: Path) -> None:
    """An operator's own wording cites nothing, and row 1 is not turn 1."""
    config = _config(tmp_path, "Raffa deploys with the check-first script.")
    _write_archive(config, _archive(2))
    client = _client(config)
    _accept_plain_row(client)

    rows = client.get("/api/proposals/history").json()["rows"]
    accepted = next(r for r in rows if r["text"] == "Raffa deploys with the check-first script.")

    assert accepted["source_path"]
    assert accepted["change"]["source_turns"] == []
    assert "source_line" not in accepted


def test_cited_turns_reads_both_evidence_shapes() -> None:
    assert _cited_turns({"provenance": {"source_message_ids": [3, 7]}}) == [3, 7]
    assert _cited_turns({"observations": [{"turn": 4}, {"turn": 0}]}) == [4]
    assert _cited_turns({"sources": [{"turn": 2}]}) == [2]
    # Malformed values must not become a line number in the viewer.
    assert _cited_turns({"provenance": {"source_message_ids": ["3", True, -1]}}) == []
    assert _cited_turns({}) == []
