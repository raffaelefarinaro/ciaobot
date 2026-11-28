"""Memory · Skills: the open skill-edit proposals and `/api/memory/skill-proposals`."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ciao import skill_proposals as sp
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web.app import create_app
from tests.session import signed_in


def proposal(
    *,
    kind: str = "skill-edit",
    status: str = "draft",
    title: str = "Ask for the date range first",
    created: str = "2026-11-26",
    change: str = "Add a first step that asks which dates to cover.",
    extra: str = "",
) -> str:
    return "\n".join([
        "---",
        f"type: {kind}",
        "target: weekly-summary",
        f"title: {title}",
        "summary: The summary skill guesses the week; ask first.",
        "rationale: |",
        "  Two chats stopped the summary to correct the range.",
        "evidence:",
        "  - chat-a290cb48",
        "  - Workspace/Care-Report.md",
        f"proposed_change: {change}",
        f"created: {created}",
        f"status: {status}",
        "---",
        "",
        f"More detail.{extra}",
        "",
    ])


def _write(vault: Path, name: str, text: str) -> Path:
    folder = vault / "Workspace" / "Proposals"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def test_lists_only_open_skill_edits_newest_first(tmp_path: Path) -> None:
    _write(tmp_path, "older-skill.md", proposal(title="Older", created="2026-11-20", status="ready"))
    _write(tmp_path, "newer-skill.md", proposal(title="Newer", created="2026-11-27"))
    _write(tmp_path, "applied-skill.md", proposal(status="applied"))
    _write(tmp_path, "rejected-skill.md", proposal(status="rejected"))
    _write(tmp_path, "guide.md", proposal(kind="guide-edit"))
    _write(tmp_path, "Not-A-Slug.md", proposal())
    _write(tmp_path, "no-front-matter.md", "just text\n")
    body = sp.read_skill_proposals(tmp_path)
    assert body["found"] is True
    assert [i["title"] for i in body["items"]] == ["Newer", "Older"]
    assert body["counts"] == {"waiting": 2}
    first = body["items"][0]
    assert first["path"] == "Workspace/Proposals/newer-skill.md"
    assert first["target"] == "weekly-summary"
    assert first["rationale"] == "Two chats stopped the summary to correct the range."
    assert first["evidence"] == ["chat-a290cb48", "Workspace/Care-Report.md"]
    assert first["proposed_change"] == "Add a first step that asks which dates to cover."
    assert first["created"] == "2026-11-27"
    assert first["body"] == "More detail."
    assert first["withheld"] is False
    assert first["problems"] == []


def test_no_folder_is_found_false(tmp_path: Path) -> None:
    assert sp.read_skill_proposals(tmp_path) == {
        "found": False, "items": [], "counts": {"waiting": 0}, "skipped": 0, "truncated": False,
    }


def test_the_checker_problems_ride_along(tmp_path: Path) -> None:
    text = proposal().replace("summary: The summary skill guesses the week; ask first.\n", "")
    _write(tmp_path, "missing-summary.md", text)
    [item] = sp.read_skill_proposals(tmp_path)["items"]
    assert item["summary"] == ""
    assert item["problems"] == ["missing or empty front matter fields: summary"]


def test_credential_shaped_text_is_withheld(tmp_path: Path) -> None:
    secret = "hunter2-very-secret"
    _write(tmp_path, "leaky.md", proposal(change=f"'Set password: {secret} in the config'"))
    [item] = sp.read_skill_proposals(tmp_path)["items"]
    assert item["withheld"] is True
    assert secret not in repr(item)
    assert item["title"] == "" and item["proposed_change"] == "" and item["evidence"] == []
    assert any("credential-shaped" in problem for problem in item["problems"])


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_links_are_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.md").write_text(proposal(title="Outside"), encoding="utf-8")
    vault = tmp_path / "vault"
    _write(vault, "real.md", proposal(title="Real"))
    (vault / "Workspace" / "Proposals" / "linked.md").symlink_to(outside / "elsewhere.md")
    body = sp.read_skill_proposals(vault)
    assert [i["title"] for i in body["items"]] == ["Real"]
    assert body["skipped"] == 1
    # A link at the folder itself: nothing is listed.
    other = tmp_path / "other"
    (other / "Workspace").mkdir(parents=True)
    (other / "Workspace" / "Proposals").symlink_to(vault / "Workspace" / "Proposals")
    assert sp.read_skill_proposals(other)["items"] == []
    assert sp.read_skill_proposals(other)["found"] is False


def test_an_oversized_file_is_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "huge.md", proposal(extra="x" * (70 * 1024)))
    body = sp.read_skill_proposals(tmp_path)
    assert body["items"] == [] and body["skipped"] == 1


def test_only_the_first_files_are_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sp, "MAX_FILES", 2)
    for n in range(3):
        _write(tmp_path, f"p-{n}.md", proposal(title=f"P{n}"))
    body = sp.read_skill_proposals(tmp_path)
    assert body["truncated"] is True and len(body["items"]) == 2


@pytest.fixture
def setup(tmp_path: Path) -> SimpleNamespace:
    cfg = CiaoConfig(
        pwa_auth_token="test-secret",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime",
        media_root=tmp_path / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    app = create_app(cfg)
    return SimpleNamespace(app=app, client=signed_in(TestClient(app)), root=tmp_path)


def test_route_reads_the_named_workspace(setup: SimpleNamespace) -> None:
    _write(setup.root / "memory-vault" / "personal", "ask-dates.md", proposal())
    _write(setup.root / "memory-vault" / "work", "other.md", proposal(title="Work one"))
    body = setup.client.get("/api/memory/skill-proposals?workspace=personal").json()
    assert body["workspace"] == "personal"
    assert body["counts"] == {"waiting": 1}
    [item] = body["items"]
    assert item["open_path"] == "memory-vault/personal/Workspace/Proposals/ask-dates.md"
    assert item["title"] == "Ask for the date range first"
    work = setup.client.get("/api/memory/skill-proposals?workspace=work").json()
    assert [i["title"] for i in work["items"]] == ["Work one"]


def test_route_requires_a_session_and_a_known_workspace(setup: SimpleNamespace) -> None:
    assert TestClient(setup.app).get("/api/memory/skill-proposals?workspace=personal").status_code == 401
    assert setup.client.get("/api/memory/skill-proposals").status_code == 400
    assert setup.client.get("/api/memory/skill-proposals?workspace=nope").status_code == 400
    # A path is never taken from the client.
    body = setup.client.get("/api/memory/skill-proposals?workspace=personal&path=/etc").json()
    assert body["items"] == [] and body["found"] is False
    assert setup.client.post("/api/memory/skill-proposals", json={}).status_code == 405
