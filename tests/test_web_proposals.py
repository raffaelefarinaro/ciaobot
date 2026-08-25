"""Tests for the proposal-queue API routes (P5 server half).

The routes live in ``ciao/web/routes_api.py`` and are exercised here through a
bare Starlette app that registers them directly, mirroring the pattern in
``test_schedule_api_delivery_modes.py``. The PWA route wiring in ``app.py`` is
the UI delegate's follow-up, so it is not asserted here.
"""

from __future__ import annotations

import pathlib

import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web import routes_api
from ciao.web.routes_api import (
    _scan_proposal_rows,
    dismiss_older_than,
    list_proposals,
    proposal_action,
    proposals_batch,
)


def _config(tmp_path: Path) -> CiaoConfig:
    vault = tmp_path / "memory-vault"
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=vault,
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal", "work")
        },
    )


def _write_queue(config: CiaoConfig, workspace: str, content: str) -> None:
    path = config.workspace_vault_root(workspace) / "Workspace" / "Memory-Proposals.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_people_note(config: CiaoConfig, path: str, tags: list[str]) -> None:
    """Write a person note with frontmatter tags so rehome signal is real."""
    target = config.vault_root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    tag_yaml = "\n".join(f"  - {t}" for t in tags)
    target.write_text(
        f"---\ntype: person\ntags:\n{tag_yaml}\ndescription: A person.\n---\n# {Path(path).stem}\n",
        encoding="utf-8",
    )


def _client(config: CiaoConfig) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/proposals", list_proposals, methods=["GET"]),
            Route("/api/proposals/{id}/{action}", proposal_action, methods=["POST"]),
            Route("/api/proposals/batch", proposals_batch, methods=["POST"]),
            Route("/api/proposals/dismiss-older-than", dismiss_older_than, methods=["POST"]),
        ]
    )
    app.state.config = config
    return TestClient(app)


_SIMPLE_QUEUE = """# Memory Proposals

## 2026-08-19 curation pass (this pass)

- [memory] Remember the lesson about check-first.  _(from: Decisions)_
- [profile] Raffa prefers direct implementation.  _(from: Decisions)_
- [user] A legacy user bullet normalizes to profile.  _(from: Decisions)_
"""


def _default_vault(tmp_path: Path) -> CiaoConfig:
    config = _config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    _write_queue(config, "personal", _SIMPLE_QUEUE)
    return config


# BOTH regions, because a missing region is CREATED rather than refused — only a
# duplicated one is unwritable. Corrupting `memory` alone let every profile row
# through and the batch half-succeeded.
_DUPLICATED_REGION_GUIDE = (
    "# Guide\n"
    "<!-- ciao:memory:start cap=2200 -->\n- a fact\n<!-- ciao:memory:end -->\n"
    "<!-- ciao:memory:start cap=2200 -->\n- another\n<!-- ciao:memory:end -->\n"
    "<!-- ciao:profile:start cap=1375 -->\n- a trait\n<!-- ciao:profile:end -->\n"
    "<!-- ciao:profile:start cap=1375 -->\n- another\n<!-- ciao:profile:end -->\n"
)


def _corrupt_guide(config: CiaoConfig, workspace: str) -> None:
    """Make one workspace's guide unwritable by duplicating a region's markers.

    A realistic cause — a bad hand edit — and the failure the write-then-dismiss
    order exists for. It replaced an over-cap region as the injection here: the
    cap is advisory now, so an over-cap write succeeds and no longer fails a batch.
    """
    guide = config.agent_root(workspace) / "CLAUDE.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    guide.write_text(_DUPLICATED_REGION_GUIDE, encoding="utf-8")

def test_list_returns_rows_with_workspace_path_and_kind(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    resp = _client(config).get("/api/proposals")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    kinds = {r["kind"] for r in rows}
    assert kinds == {"memory", "profile", "user"}
    for row in rows:
        assert row["workspace"] == "personal"
        assert row["path"] == "personal/Workspace/Memory-Proposals.md"
        assert isinstance(row["line"], int)
        assert row["id"]


def test_ids_are_stable_across_calls(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    first = {r["id"] for r in client.get("/api/proposals").json()["rows"]}
    second = {r["id"] for r in client.get("/api/proposals").json()["rows"]}
    assert first == second


def test_ids_survive_another_row_being_removed(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    memory = next(r for r in rows if r["kind"] == "memory")
    profile = next(r for r in rows if r["kind"] == "profile")
    client.post(f"/api/proposals/{memory['id']}/dismiss")
    rows_after = client.get("/api/proposals").json()["rows"]
    # The survivor's id is unchanged: dismissing a row never renumbers or
    # renames another, because the id derives from content, not position.
    assert next(r["id"] for r in rows_after if r["kind"] == "profile") == profile["id"]
    assert {r["kind"] for r in rows_after} == {"profile", "user"}


def _accept_kind_row(client: TestClient, kind: str) -> dict:
    rows = client.get("/api/proposals").json()["rows"]
    return next(r for r in rows if r["kind"] == kind)


def test_accept_dispatches_the_kinds_own_action(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    for kind, expected_region in (("memory", "memory"), ("profile", "profile"), ("user", "profile")):
        row = _accept_kind_row(client, kind)
        resp = client.post(f"/api/proposals/{row['id']}/accept")
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["action"] == "edit_region"
        assert result["region"] == expected_region


def _rerooted_vault(config: CiaoConfig, tmp_path: Path) -> None:
    """Give the fixture the per-root layout a real move needs."""
    receipt = tmp_path / ".runtime" / "migration" / "workspace-rooting.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"status": "migrated"}), encoding="utf-8")
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()


def _per_root_config(tmp_path: Path) -> CiaoConfig:
    """A migrated install: each workspace owns a folder holding its own vault."""
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"{name}/memory-vault")
            for name in ("personal", "work")
        },
    )


def _rehome_fixture(tmp_path: Path, tags: list[str]) -> tuple[CiaoConfig, TestClient, dict]:
    config = _per_root_config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "People").mkdir(parents=True, exist_ok=True)
    _rerooted_vault(config, tmp_path)
    note = config.workspace_vault_root("personal") / "People" / "Mo.md"
    note.write_text(
        "---\ntype: person\ntags:\n" + "".join(f"  - {t}\n" for t in tags) + "---\n# Mo\n",
        encoding="utf-8",
    )
    _write_queue(config, "personal", (
        "## curation pass\n\n"
        "- [rehome] Re-home `personal/People/Mo.md` to `work/People/Mo.md`? "
        "Uncertain.  _(from: vault-rehome)_\n"
    ))
    client = _client(config)
    row = _accept_kind_row(client, "rehome")
    return config, client, row


def test_an_ambiguous_rehome_accept_asks_instead_of_guessing(tmp_path: Path) -> None:
    """Tags naming two workspaces is a question only the operator can answer, so
    accepting without a choice must not move somebody's note on a guess."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "friend", "colleague"])

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 400
    assert "pick one explicitly" in resp.json()["error"]
    assert (config.workspace_vault_root("personal") / "People" / "Mo.md").is_file()


def test_an_explicit_choice_moves_the_note(tmp_path: Path) -> None:
    """The whole point: the queue can now carry out the answer it asked for."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "friend", "colleague"])

    resp = client.post(f"/api/proposals/{row['id']}/accept?workspace=work")

    assert resp.status_code == 200, resp.json()
    assert not (config.workspace_vault_root("personal") / "People" / "Mo.md").exists()
    assert (config.workspace_vault_root("work") / "People" / "Mo.md").is_file()
    # And the row is gone from the queue.
    assert not [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]


def test_a_choice_the_tags_do_not_name_is_still_honoured(tmp_path: Path) -> None:
    """The tags are a hint; the operator asking is the authority.

    Restricting the choice to tag-named candidates left every queued row on the
    reference install unmovable, because most have no tag naming anywhere.
    """
    config, client, row = _rehome_fixture(tmp_path, ["person"])
    assert row["rehome"]["candidates"] == [], row["rehome"]

    resp = client.post(f"/api/proposals/{row['id']}/accept?workspace=work")

    assert resp.status_code == 200, resp.json()
    assert (config.workspace_vault_root("work") / "People" / "Mo.md").is_file()


def test_an_unregistered_choice_is_still_refused(tmp_path: Path) -> None:
    """Free choice among REGISTERED workspaces, not a free-text path."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "friend", "colleague"])

    resp = client.post(f"/api/proposals/{row['id']}/accept?workspace=nowhere")

    assert resp.status_code == 409
    assert "not a registered workspace" in resp.json()["error"]
    assert (config.workspace_vault_root("personal") / "People" / "Mo.md").is_file()


def test_a_justified_row_moves_without_a_choice(tmp_path: Path) -> None:
    """A single clean tag signal already names the destination."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    assert row["rehome"]["justified"] is True, row["rehome"]

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 200, resp.json()
    assert (config.workspace_vault_root("work") / "People" / "Mo.md").is_file()


def test_a_failed_move_keeps_the_bullet(tmp_path: Path) -> None:
    """Move-then-dismiss, the same order as a region write: a note silently left
    where it was, with nothing recording that it should not be, is the outcome to
    avoid."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    # Something is already there, so the move refuses rather than merging.
    (config.workspace_vault_root("work") / "People" / "Mo.md").write_text(
        "# A different Mo\n", encoding="utf-8"
    )

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409
    assert "already exists" in resp.json()["error"]
    assert (config.workspace_vault_root("personal") / "People" / "Mo.md").is_file()
    assert [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]


def test_dismiss_does_not_mutate_the_workspace_guide(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    guide = config.workspace_vault_root("personal") / "CLAUDE.md"
    guide.write_text("<!-- ciao:memory:start -->\n## Agent memory\n<!-- ciao:memory:end -->\n", encoding="utf-8")
    before = guide.read_text(encoding="utf-8")
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    client.post(f"/api/proposals/{row['id']}/accept")
    assert guide.read_text(encoding="utf-8") == before


def test_batch_dismiss_is_atomic(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    before = queue.read_text(encoding="utf-8")
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    profile = next(r for r in rows if r["kind"] == "profile")
    resp = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [profile["id"], "does-not-exist"]})
    assert resp.status_code == 404
    assert queue.read_text(encoding="utf-8") == before


def test_unknown_id_returns_404(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    resp = _client(config).post("/api/proposals/nope/dismiss")
    assert resp.status_code == 404


def test_no_signal_row_exposes_no_justified_destination(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    _write_people_note(config, "personal/People/Christian.md", [])
    _write_queue(config, "personal", (
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [rehome] Re-home `personal/People/Christian.md` to `work/People/Christian.md`? Uncertain: no tag names a workspace. Move it and its links with `ciao vault-rehome --apply` only after tagging it.  _(from: vault-rehome)_\n"
    ))
    client = _client(config)
    row = _accept_kind_row(client, "rehome")
    assert row["rehome"]["justified"] is False
    # No tag names a workspace, so there is no evidence-backed candidate; the
    # destination field holds the computed guess and must not be pre-accepted.
    assert row["rehome"]["candidates"] == []


def test_dual_tag_row_exposes_more_than_one_candidate(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    _write_people_note(config, "personal/People/Mo.md", ["ex-colleague", "friend", "person"])
    _write_queue(config, "personal", (
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [rehome] Re-home `personal/People/Mo.md` to `work/People/Mo.md`? Uncertain: tags name both personal and work (ex-colleague, friend). Move it and its links with `ciao vault-rehome --apply` only after tagging it.  _(from: vault-rehome)_\n"
    ))
    client = _client(config)
    row = _accept_kind_row(client, "rehome")
    assert len(row["rehome"]["candidates"]) > 1
    assert row["rehome"]["candidates"] == ["personal", "work"]


def test_personal_people_user_never_appears(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    # Wave 1 fixed the rehome detector so it never proposes the operator's own
    # identity note. Guard that at the API too: even with a User.md note on
    # disk and a queue that could hold one, the row never surfaces.
    _write_people_note(config, "personal/People/User.md", [])
    resp = _client(config).get("/api/proposals")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert not any("User.md" in r.get("text", "") for r in rows)
    from ciao import vault_rehome
    candidates = vault_rehome.detect_misfiled_people(config.vault_root, workspaces=config.workspace_names())
    assert not any(c.path == "personal/People/User.md" for c in candidates)


def test_region_accept_from_non_primary_workspace_carries_leak_warning(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    _write_queue(config, "work", "## 2026-08-19 curation pass (this pass)\n\n- [memory] A work-origin fact.  _(from: Decisions)_\n")
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    work_memory = next(r for r in rows if r["workspace"] == "work" and r["kind"] == "memory")
    assert work_memory["leak_warning"] is True


def test_dismiss_older_than_removes_old_sections(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", (
        "## 2026-07-01 curation pass\n\n"
        "- [memory] An old fact about a forgotten chat.  _(from: Decisions)_\n\n"
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [memory] A recent fact worth keeping.  _(from: Decisions)_\n"
    ))
    client = _client(config)
    resp = client.post("/api/proposals/dismiss-older-than?date=2026-08-01")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 1
    rows = client.get("/api/proposals").json()["rows"]
    assert len(rows) == 1
    assert rows[0]["text"] == "A recent fact worth keeping."


def test_the_real_app_serves_every_documented_proposal_route() -> None:
    """The routes must be reachable on the app the server actually builds.

    The tests above mount the handlers on a hand-built Starlette app, which
    proves the handlers work but not that anything serves them. Registration
    lives in ciao/web/app.py, so a handler can be complete, documented, and
    still unreachable in production with the whole suite green. This asserts
    the real route table, and pins it to the paths PWA_API.md advertises.
    """
    import re

    from ciao.web import app as app_module

    registered = set(re.findall(r'Route\("(/api/proposals[^"]*)"', pathlib.Path(app_module.__file__).read_text()))
    documented = set(re.findall(r"/api/proposals[A-Za-z0-9_/{}-]*", pathlib.Path("PWA_API.md").read_text()))

    expected = {
        "/api/proposals",
        "/api/proposals/batch",
        "/api/proposals/dismiss-older-than",
        "/api/proposals/{id}/{action}",
    }
    assert registered == expected, f"app.py route table drifted: {registered}"

    # Every concrete path the docs show must be served by one of the registered
    # patterns. `$ID/accept` in a curl recipe is the {id}/{action} route.
    assert "/api/proposals" in documented
    for path in ("/api/proposals/batch", "/api/proposals/dismiss-older-than"):
        assert path in documented, f"{path} is registered but undocumented"


# -- Accept has to actually write the fact -----------------------------------
#
# It used to remove the bullet and return a descriptor saying what SHOULD happen,
# matching the MCP flow where the agent edits and then dismisses. In a UI where a
# person clicks Accept, that meant the fact left the queue and landed nowhere:
# one click from losing any of the 109 queued on the reference install.


def _region_entries(config, workspace: str, region: str) -> list[str]:
    from ciao.memory_tool import read_region

    guide = Path(config.agent_root(workspace)) / "CLAUDE.md"
    entries, _diags = read_region(guide, region)
    return entries


def test_accept_writes_the_fact_into_the_region(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["promoted"] is True
    assert row["text"] in _region_entries(config, row["workspace"], "memory")
    # And the bullet is gone, in that order.
    assert row["id"] not in {r["id"] for r in client.get("/api/proposals").json()["rows"]}


def test_a_failed_write_keeps_the_bullet(tmp_path: Path) -> None:
    """Write-then-dismiss, never the reverse: the reverse loses the fact.

    A guide whose region markers are duplicated is the realistic cause: the write
    cannot tell which copy to edit, so it refuses and the bullet has to survive.
    """
    config = _default_vault(tmp_path)
    _corrupt_guide(config, "personal")
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409
    assert "id" in resp.json()
    # Still queued, so the operator can fix the cap and retry.
    assert row["id"] in {r["id"] for r in client.get("/api/proposals").json()["rows"]}
    assert row["text"] not in _region_entries(config, row["workspace"], "memory")


def test_a_batch_accept_writes_every_fact_it_dismisses(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    rows = [
        r for r in client.get("/api/proposals").json()["rows"]
        if r["kind"] in {"memory", "profile", "user"}
    ]
    assert len(rows) >= 2

    resp = client.post(
        "/api/proposals/batch",
        json={"action": "accept", "ids": [r["id"] for r in rows]},
    )

    assert resp.status_code == 200
    assert all(r["promoted"] for r in resp.json()["results"])
    for row in rows:
        region = "memory" if row["kind"] == "memory" else "profile"
        assert row["text"] in _region_entries(config, row["workspace"], region), row["text"]


def test_a_batch_keeps_the_bullets_it_could_not_write(tmp_path: Path) -> None:
    """A batch that removed the lines first would lose every unwritable fact at once."""
    config = _default_vault(tmp_path)
    _corrupt_guide(config, "personal")
    client = _client(config)
    rows = [
        r for r in client.get("/api/proposals").json()["rows"]
        if r["kind"] in {"memory", "profile", "user"}
    ]

    resp = client.post(
        "/api/proposals/batch",
        json={"action": "accept", "ids": [r["id"] for r in rows]},
    )

    results = resp.json()["results"]
    assert all(r["promoted"] is False and r["dismissed"] is False for r in results)
    still = {r["id"] for r in client.get("/api/proposals").json()["rows"]}
    for row in rows:
        assert row["id"] in still, row["id"]


def test_dismiss_still_writes_nothing(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    client.post(f"/api/proposals/{row['id']}/dismiss")

    assert row["text"] not in _region_entries(config, row["workspace"], "memory")
    assert row["id"] not in {r["id"] for r in client.get("/api/proposals").json()["rows"]}


# -- a skill proposal is a row you can actually act on -------------------------


def _write_skill_proposal(config: CiaoConfig, workspace: str, name: str) -> Path:
    path = (
        config.workspace_vault_root(workspace)
        / "Workspace" / "Skill-Proposals" / f"{name}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {name}\n\nA proposed skill.\n", encoding="utf-8")
    return path


def test_a_skill_row_is_resolvable_by_id(tmp_path: Path) -> None:
    """It was listed but never registered, so the dismiss button the UI renders
    for every skill row answered 404 from BOTH endpoints. 49 dead buttons on a
    real vault."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")

    rows, by_id = _scan_proposal_rows(config)

    skill = [r for r in rows if r["kind"] == "skill"]
    assert len(skill) == 1
    assert skill[0]["id"] in by_id


def test_dismissing_a_skill_row_deletes_the_file(tmp_path: Path) -> None:
    """A reviewed proposal is a resolved decision — implemented or disregarded —
    so dismissing it deletes the file rather than moving it aside; re-reviewing
    the same suggestion should not re-ask it."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")

    response = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert response.status_code == 200, response.json()
    assert not source.exists()
    assert not source.parent.exists() or not list(source.parent.glob("dismissed/**/*"))
    assert [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill"] == []


def test_accepting_a_skill_row_is_refused_with_a_reason(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")

    response = client.post(f"/api/proposals/{row['id']}/accept")

    assert response.status_code == 400
    assert "nothing to promote" in response.json()["error"]
    assert source.is_file()   # untouched


def test_a_batch_dismiss_covers_skill_rows_and_bullets_together(tmp_path: Path) -> None:
    """The batch groups by queue file and drops bullet lines; a skill row has no
    line in any queue, so it has to be handled before that grouping."""
    config = _config(tmp_path)
    _write_queue(config, "personal", "# Proposals\n\n- [memory] Remember the thing\n")
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    ids = [r["id"] for r in rows]
    assert len(ids) == 2

    response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": ids})

    assert response.status_code == 200, response.json()
    assert all(r["dismissed"] for r in response.json()["results"]), response.json()
    assert not source.exists()
    assert client.get("/api/proposals").json()["rows"] == []


def test_dismissing_the_same_skill_twice_deletes_both(tmp_path: Path) -> None:
    """Two proposals can share a name across runs; each dismiss deletes its own
    file, so dismissing the second run's copy is unaffected by the first."""
    config = _config(tmp_path)
    client = _client(config)
    for _ in range(2):
        _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
        row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")
        assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 200

    queue = config.workspace_vault_root("personal") / "Workspace" / "Skill-Proposals"
    assert not queue.is_dir() or list(queue.glob("*.md")) == []
    assert not (queue / "dismissed").is_dir()


# -- the leak warning is about a SHARED guide, not about a workspace ----------


def _rerooted(config: CiaoConfig, tmp_path: Path) -> None:
    """Flip the config's layout to per-root, as the migration does."""
    receipt = tmp_path / ".runtime" / "migration" / "workspace-rooting.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"status": "migrated"}), encoding="utf-8")
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()


def test_no_leak_warning_once_each_workspace_owns_its_guide(tmp_path: Path) -> None:
    """It said a work row would be "visible in every workspace" — of a guide only
    work reads. A warning that is false teaches the operator to click through."""
    config = _default_vault(tmp_path)
    _rerooted(config, tmp_path)

    assert routes_api._leak_warning(config, "memory", "work") is False
    assert routes_api._leak_warning(config, "profile", "work") is False


def test_a_shared_guide_still_warns(tmp_path: Path) -> None:
    """Before the re-rooting one CLAUDE.md really is loaded by every session."""
    config = _default_vault(tmp_path)
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()   # no receipt: shared layout

    assert routes_api._leak_warning(config, "memory", "work") is True
    # The primary workspace's own row is where the guide belongs, so no warning.
    assert routes_api._leak_warning(config, "memory", "personal") is False


def test_a_rehome_never_warns_in_either_layout(tmp_path: Path) -> None:
    """A move is not a region write."""
    config = _default_vault(tmp_path)
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()
    assert routes_api._leak_warning(config, "rehome", "work") is False
    _rerooted(config, tmp_path)
    assert routes_api._leak_warning(config, "rehome", "work") is False


def test_an_already_moved_row_clears_instead_of_erroring(tmp_path: Path) -> None:
    """The stuck-row case, end to end: the note is at the destination and its
    bullet is still queued, which is what a cancelled handler leaves. Accepting
    must clear the row rather than answer "no note at ..." forever."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    # Simulate the interrupted attempt: note moved, bullet still there.
    source = config.workspace_vault_root("personal") / "People" / "Mo.md"
    destination = config.workspace_vault_root("work") / "People" / "Mo.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)

    # With the note gone from its source there is no live signal, so the row is
    # unjustified and the operator names the destination — which is what the
    # picker does. The point is that naming it CLEARS the row instead of
    # answering "no note at ..." forever.
    resp = client.post(f"/api/proposals/{row['id']}/accept?workspace=work")

    assert resp.status_code == 200, resp.json()
    assert not [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]


def test_a_batch_moves_every_selected_row_to_one_workspace(tmp_path: Path) -> None:
    config = _per_root_config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "People").mkdir(parents=True, exist_ok=True)
    _rerooted_vault(config, tmp_path)
    for name in ("Mo", "Ida"):
        (config.workspace_vault_root("personal") / "People" / f"{name}.md").write_text(
            f"---\ntype: person\ntags:\n  - person\n---\n# {name}\n", encoding="utf-8"
        )
    _write_queue(config, "personal", (
        "## curation pass\n\n"
        "- [rehome] Re-home `personal/People/Mo.md` to `work/People/Mo.md`?  _(from: vault-rehome)_\n"
        "- [rehome] Re-home `personal/People/Ida.md` to `work/People/Ida.md`?  _(from: vault-rehome)_\n"
    ))
    client = _client(config)
    ids = [r["id"] for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]
    assert len(ids) == 2

    resp = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": ids, "workspace": "work"}
    )

    assert resp.status_code == 200, resp.json()
    assert all(r["dismissed"] for r in resp.json()["results"]), resp.json()
    for name in ("Mo", "Ida"):
        assert (config.workspace_vault_root("work") / "People" / f"{name}.md").is_file()
        assert not (config.workspace_vault_root("personal") / "People" / f"{name}.md").exists()
    assert not [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]


def test_a_batch_keeps_the_rows_whose_move_failed(tmp_path: Path) -> None:
    """One bad row must not drop the others' bullets, and must keep its own."""
    config = _per_root_config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "People").mkdir(parents=True, exist_ok=True)
    _rerooted_vault(config, tmp_path)
    for name in ("Mo", "Ida"):
        (config.workspace_vault_root("personal") / "People" / f"{name}.md").write_text(
            f"---\ntype: person\ntags:\n  - person\n---\n# {name}\n", encoding="utf-8"
        )
    # Something already occupies Ida's destination, so her move refuses.
    (config.workspace_vault_root("work") / "People" / "Ida.md").write_text(
        "# A different Ida\n", encoding="utf-8"
    )
    _write_queue(config, "personal", (
        "## curation pass\n\n"
        "- [rehome] Re-home `personal/People/Mo.md` to `work/People/Mo.md`?  _(from: vault-rehome)_\n"
        "- [rehome] Re-home `personal/People/Ida.md` to `work/People/Ida.md`?  _(from: vault-rehome)_\n"
    ))
    client = _client(config)
    ids = [r["id"] for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]

    resp = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": ids, "workspace": "work"}
    )

    results = {r["id"]: r for r in resp.json()["results"]}
    assert sum(1 for r in results.values() if r["dismissed"]) == 1
    assert any("already exists" in str(r.get("error", "")) for r in results.values())
    left = [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]
    assert len(left) == 1
    assert "Ida" in left[0]["rehome"]["note"]
    assert (config.workspace_vault_root("personal") / "People" / "Ida.md").is_file()


# -- every resolution leaves one outcome event --------------------------------
#
# Settings → Automation measures whether memory extraction is *useful*
# (promoted vs dismissed), not only whether it ran. These pin that each resolve
# path records exactly the decisions it settled — and nothing for the ones it
# refused.


def _outcome_events(tmp_path: Path) -> list[dict]:
    import json as _json

    from ciao import proposal_outcomes

    path = tmp_path / proposal_outcomes.PROPOSAL_OUTCOMES_NAME
    if not path.exists():
        return []
    return [
        _json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_single_accept_and_dismiss_record_outcomes(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    memory = _accept_kind_row(client, "memory")
    profile = _accept_kind_row(client, "profile")

    client.post(f"/api/proposals/{memory['id']}/accept")
    client.post(f"/api/proposals/{profile['id']}/dismiss")

    events = _outcome_events(tmp_path)
    assert [e["action"] for e in events] == ["promoted", "dismissed"]
    assert all(e["workspace"] == "personal" for e in events)
    assert {e["kind"] for e in events} == {"memory", "profile"}
    assert all(e["via"] == "pwa" for e in events)


def test_a_failed_accept_records_no_outcome(tmp_path: Path) -> None:
    """A refused write leaves the bullet queued: an attempt, not a decision."""
    config = _default_vault(tmp_path)
    _corrupt_guide(config, "personal")
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 409

    assert _outcome_events(tmp_path) == []


def test_an_unknown_batch_id_records_nothing(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]

    resp = client.post(
        "/api/proposals/batch",
        json={"action": "dismiss", "ids": [rows[0]["id"], "does-not-exist"]},
    )

    assert resp.status_code == 404
    assert _outcome_events(tmp_path) == []


def test_a_batch_records_one_event_per_resolved_row(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    rows = [
        r for r in client.get("/api/proposals").json()["rows"]
        if r["kind"] in {"memory", "profile", "user"}
    ]

    resp = client.post(
        "/api/proposals/batch",
        json={"action": "dismiss", "ids": [r["id"] for r in rows]},
    )

    assert resp.status_code == 200
    events = _outcome_events(tmp_path)
    assert len(events) == len(rows)
    assert all(e["action"] == "dismissed" for e in events)
    assert all(e["workspace"] == "personal" for e in events)


def test_a_batch_accept_records_promotions_not_failures(tmp_path: Path) -> None:
    """The batch keeps unwritable bullets queued; those are not promotions."""
    # Per-root layout: in the shared layout both workspaces resolve ONE guide,
    # so corrupting work's would refuse personal's rows too.
    config = _per_root_config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    _rerooted_vault(config, tmp_path)
    _write_queue(config, "personal", (
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [memory] A personal fact.  _(from: Decisions)_\n"
    ))
    # Corrupting only work's guide splits the batch: personal's rows write,
    # work's refuse, and only the writes may count as decisions.
    _write_queue(config, "work", (
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [memory] A work-origin fact.  _(from: Decisions)_\n"
    ))
    _corrupt_guide(config, "work")
    client = _client(config)
    rows = [
        r for r in client.get("/api/proposals").json()["rows"]
        if r["kind"] == "memory"
    ]
    assert {r["workspace"] for r in rows} == {"personal", "work"}

    resp = client.post(
        "/api/proposals/batch",
        json={"action": "accept", "ids": [r["id"] for r in rows]},
    )

    results = resp.json()["results"]
    promoted_count = sum(1 for r in results if r.get("promoted"))
    assert promoted_count == 1
    assert any(not r.get("promoted") for r in results)

    events = _outcome_events(tmp_path)
    assert len(events) == promoted_count
    assert [e["workspace"] for e in events] == ["personal"]
    assert all(e["action"] == "promoted" for e in events)


def test_the_bulk_sweep_records_one_event_per_removed_row(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    _write_queue(config, "work", (
        "## 2026-07-01 curation pass\n\n"
        "- [memory] An old work fact.  _(from: Decisions)_\n\n"
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [memory] A recent work fact.  _(from: Decisions)_\n"
    ))
    client = _client(config)

    resp = client.post("/api/proposals/dismiss-older-than?date=2026-08-01")

    assert resp.json()["removed"] == 1
    events = _outcome_events(tmp_path)
    assert len(events) == 1
    assert events[0]["kind"] == "memory"
    assert events[0]["action"] == "dismissed"
    # The workspace is threaded from the queue the row was swept out of.
    assert events[0]["workspace"] == "work"


def test_dismissing_a_skill_row_records_its_kind(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")

    response = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert response.status_code == 200
    assert not source.exists()
    events = _outcome_events(tmp_path)
    assert len(events) == 1
    assert events[0]["kind"] == "skill"
    assert events[0]["action"] == "dismissed"


def test_a_rehome_accept_records_a_promotion(tmp_path: Path) -> None:
    """A rehome accept is a decision on a queued row like any other kind."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200

    events = _outcome_events(tmp_path)
    assert [(e["kind"], e["action"]) for e in events] == [("rehome", "promoted")]


# ---- concurrent queue rewrites ---------------------------------------------


def test_a_shifted_line_index_does_not_delete_a_bystander():
    """The captured index is a hint, not an address.

    `_scan_proposal_rows` records a line index, and an accept then awaits an
    unbounded model call before rewriting the queue - with no lock anywhere. A
    second accept or dismiss landing in that window removes a line and shifts
    every later index, so deleting by index alone took out an UNRELATED
    proposal and left the accepted one sitting in the queue.
    """
    from ciao.web.routes_api import _remove_bullet_line

    # The bullet was at index 1 when it was scanned; a concurrent dismiss has
    # since removed the line above it, so index 1 now holds someone else.
    lines = ["- [memory] mine", "- [memory] a bystander"]

    assert _remove_bullet_line(lines, 2, "- [memory] mine") is True
    assert lines == ["- [memory] a bystander"], "it deleted the wrong bullet"


def test_removing_a_bullet_that_is_already_gone_is_a_no_op():
    """Whoever removed it got there first; nothing else may be taken instead."""
    from ciao.web.routes_api import _remove_bullet_line

    lines = ["- [memory] someone else's"]

    assert _remove_bullet_line(lines, 0, "- [memory] already dismissed") is False
    assert lines == ["- [memory] someone else's"]
