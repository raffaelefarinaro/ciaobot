"""Tests for the proposal-queue API routes (P5 server half).

The routes live in ``ciao/web/routes_api.py`` and are exercised here through a
bare Starlette app that registers them directly, mirroring the pattern in
``test_schedule_api_delivery_modes.py``. The PWA route wiring in ``app.py`` is
the UI delegate's follow-up, so it is not asserted here.
"""

from __future__ import annotations

import pathlib

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.web.routes_api import (
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


def test_rehome_accept_never_performs_a_region_edit(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    _write_people_note(config, "personal/People/Mo.md", ["ex-colleague", "friend", "person"])
    _write_people_note(config, "personal/People/Christian.md", [])
    _write_queue(config, "personal", (
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [rehome] Re-home `personal/People/Mo.md` to `work/People/Mo.md`? Uncertain: tags name both personal and work (ex-colleague, friend). Move it and its links with `ciao vault-rehome --apply` only after tagging it.  _(from: vault-rehome)_\n"
    ))
    client = _client(config)
    row = _accept_kind_row(client, "rehome")
    resp = client.post(f"/api/proposals/{row['id']}/accept")
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["action"] == "move_file"
    assert "region" not in result


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

    An over-cap region is the realistic cause — the reference install's
    `ciao:memory` sits at 139% of its cap, so `memory_update` already refuses new
    entries there.
    """
    config = _default_vault(tmp_path)
    object.__setattr__(config, "memory_char_limit", 1)
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
    """A batch that removed the lines first would lose every over-cap fact at once."""
    config = _default_vault(tmp_path)
    object.__setattr__(config, "memory_char_limit", 1)
    object.__setattr__(config, "user_char_limit", 1)
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
