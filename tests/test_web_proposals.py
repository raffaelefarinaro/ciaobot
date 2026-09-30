"""Tests for the proposal-queue API routes (P5 server half).

The routes live in ``ciao/web/routes_api.py`` and are exercised here through a
bare Starlette app that registers them directly, mirroring the pattern in
``test_schedule_api_delivery_modes.py``. The PWA route wiring in ``app.py`` is
the UI delegate's follow-up, so it is not asserted here.
"""

from __future__ import annotations

import pathlib

import json
import re
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import skill_proposals
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.models import ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import proposal_service
from ciao.web.project_chats import ProjectChatManager
from ciao.web import routes_api
from ciao.web.proposal_service import _scan_proposal_rows
from ciao.web.routes_api import (
    dismiss_older_than,
    list_proposals,
    proposal_action,
    proposal_implement,
    proposal_preview,
    proposals_batch,
    proposals_history,
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
    _queue_text(config, workspace).write_text(content, encoding="utf-8")


def _queue_text(config: CiaoConfig, workspace: str) -> Path:
    """The workspace's memory proposal queue, the file a bullet lives in."""
    path = config.workspace_vault_root(workspace) / "Workspace" / "Memory-Proposals.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_people_note(config: CiaoConfig, path: str, tags: list[str]) -> None:
    """Write a person note with frontmatter tags so rehome signal is real."""
    target = config.vault_root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    tag_yaml = "\n".join(f"  - {t}" for t in tags)
    target.write_text(
        f"---\ntype: person\ntags:\n{tag_yaml}\ndescription: A person.\n---\n# {Path(path).stem}\n",
        encoding="utf-8",
    )


def _client(config: CiaoConfig, pcm: Any = None) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/proposals", list_proposals, methods=["GET"]),
            Route("/api/proposals/history", proposals_history, methods=["GET"]),
            Route("/api/proposals/{id}/preview", proposal_preview, methods=["POST"]),
            # Before `{id}/{action}`, exactly as app.py orders them: Starlette
            # matches in order and would otherwise read "implement" as an action.
            Route("/api/proposals/{id}/implement", proposal_implement, methods=["POST"]),
            Route("/api/proposals/{id}/{action}", proposal_action, methods=["POST"]),
            Route("/api/proposals/batch", proposals_batch, methods=["POST"]),
            Route("/api/proposals/dismiss-older-than", dismiss_older_than, methods=["POST"]),
        ]
    )
    app.state.config = config
    app.state.project_chat_manager = pcm
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
    guide = config.agent_root(workspace) / "AGENTS.md"
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


def test_dismiss_preserves_the_text_for_append_dedupe(tmp_path: Path) -> None:
    """A PWA dismissal must stop the nightly curator from re-filing the fact.

    The curator re-reads the same recent transcripts every night; dedupe
    against live bullets alone forgets the decision the moment the row is
    dropped, so the sidecar has to carry the rejected text.
    """
    from ciao.memory_proposals import append_proposals, dismissed_log_path
    from ciao.memory_proposals import MemoryProposal

    config = _default_vault(tmp_path)
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    row = next(r for r in rows if r["text"].startswith("Remember the lesson"))

    resp = client.post(f"/api/proposals/{row['id']}/dismiss")
    assert resp.status_code == 200

    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    assert row["text"] in dismissed_log_path(queue).read_text(encoding="utf-8")

    # The nightly pass finds the same fact and tries again: refused as a dupe.
    assert (
        append_proposals(
            [MemoryProposal(target="memory", text=row["text"], source_section="curation")],
            config.workspace_vault_root("personal"),
        )
        is None
    )


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
    guide = config.workspace_vault_root("personal") / "AGENTS.md"
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
    # The sweep is a decision per row: the dedupe history must carry the
    # swept text or the nightly curator re-files it from the same transcript.
    from ciao.memory_proposals import dismissed_log_path

    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    log_text = dismissed_log_path(queue).read_text(encoding="utf-8")
    assert "An old fact about a forgotten chat." in log_text
    assert "A recent fact worth keeping." not in log_text


def test_batch_dismiss_records_every_row_in_the_dedupe_history(tmp_path: Path) -> None:
    """Multi-select dismiss persists each rejected row's text, like the single route."""
    config = _default_vault(tmp_path)
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    targets = [r for r in rows if r["kind"] in {"memory", "profile"}]
    resp = client.post(
        "/api/proposals/batch",
        json={"action": "dismiss", "ids": [r["id"] for r in targets]},
    )
    assert resp.status_code == 200

    from ciao.memory_proposals import append_proposals, dismissed_log_path
    from ciao.memory_proposals import MemoryProposal

    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    log_text = dismissed_log_path(queue).read_text(encoding="utf-8")
    for row in targets:
        assert row["text"] in log_text
        assert (
            append_proposals(
                [MemoryProposal(target=row["kind"], text=row["text"], source_section="curation")],
                config.workspace_vault_root("personal"),
            )
            is None
        )


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

    expected = {
        "/api/proposals",
        "/api/proposals/history",
        "/api/proposals/batch",
        "/api/proposals/dismiss-older-than",
        "/api/proposals/{id}/preview",
        "/api/proposals/{id}/implement",
        "/api/proposals/{id}/{action}",
    }
    assert registered == expected, f"app.py route table drifted: {registered}"
    # Every registered route must also appear in PWA_API.md, or the drift above
    # is only drift in one direction. Substring, not set equality: the prose
    # around each route names its path in curl examples too.
    api_doc = pathlib.Path("PWA_API.md").read_text()
    missing = {path for path in expected if path not in api_doc}
    assert not missing, f"PWA_API.md does not document: {missing}"

    # Every concrete path the docs show must be served by one of the registered
    # patterns. `$ID/accept` in a curl recipe is the {id}/{action} route.
    for path in ("/api/proposals/batch", "/api/proposals/dismiss-older-than"):
        assert path in api_doc, f"{path} is registered but undocumented"


# -- Accept has to actually write the fact -----------------------------------
#
# It used to remove the bullet and return a descriptor saying what SHOULD happen,
# matching the MCP flow where the agent edits and then dismisses. In a UI where a
# person clicks Accept, that meant the fact left the queue and landed nowhere:
# one click from losing any of the 109 queued on the reference install.


def _region_entries(config, workspace: str, region: str) -> list[str]:
    """A region's entries with their learned-at stamps removed.

    Accepting a fact appends a learned-at `[YYYY-MM-DD]` stamp — the stamp the
    aging audit reads to surface facts that have not been re-verified.
    Stripping it here is the same normalisation the production dedupe does, and
    keeps these assertions from depending on today's date.
    """
    from ciao.memory_audit import strip_learned_stamp
    from ciao.memory_tool import read_region

    guide = Path(config.agent_root(workspace)) / "AGENTS.md"
    entries, _diags = read_region(guide, region)
    return [strip_learned_stamp(entry) for entry in entries]


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


def test_accepting_a_people_row_writes_a_typed_note(tmp_path: Path) -> None:
    """A `[people]` accept files the note the category registry names, typed.

    The writer used to emit `tags: [person]` and no `type:` at all, so the note
    it created was one `ciao vault-lint` reports as untyped. Destination and
    content both come from the registry now, and this pins the whole accept:
    the row leaves the queue and a note with `type: person` is in `People/`.
    """
    config = _default_vault(tmp_path)
    _write_queue(
        config,
        "personal",
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [people Mo] Leads the pilot.  _(from: Decisions)_\n",
    )
    client = _client(config)
    row = _accept_kind_row(client, "people")

    resp = client.post(f"/api/proposals/{row['id']}/accept")
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["action"] == "write_people_note"
    assert result["destination"] == "People/Mo.md"

    note = config.workspace_vault_root(row["workspace"]) / "People" / "Mo.md"
    text = note.read_text(encoding="utf-8")
    assert "type: person" in text
    assert "tags: [person]" in text
    assert re.search(r"^updated: \d{4}-\d{2}-\d{2}$", text, re.MULTILINE), text
    assert row["text"] in text
    assert row["id"] not in {r["id"] for r in client.get("/api/proposals").json()["rows"]}


def test_accepting_a_people_row_honours_an_edited_category_folder(
    tmp_path: Path,
) -> None:
    """The accept files the note in the folder the owner's edit names.

    The registry is read at the agent vault root — the one holding
    `entity-types.yaml` beside `VOCABULARY.md`, the file
    `PATCH /api/memory/entity-types` writes — while the note is written under
    the workspace's notes root. Reading the category from the notes root instead
    is what made an owner's `person.folder: Humans` invisible here: the accept
    went on writing `People/Mo.md` beside the very file that renamed it, on the
    default pre-re-rooting install where those two roots differ. This is that
    install: `agent_vault_root` is the shared vault, `workspace_vault_root` the
    `personal` subtree under it.
    """
    config = _default_vault(tmp_path)
    agent_root = config.agent_vault_root("personal")
    assert agent_root != config.workspace_vault_root("personal")
    (agent_root / "entity-types.yaml").write_text(
        "- id: person\n  folder: Humans\n", encoding="utf-8"
    )
    _write_queue(
        config,
        "personal",
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [people Mo] Leads the pilot.  _(from: Decisions)_\n",
    )
    client = _client(config)
    row = _accept_kind_row(client, "people")

    # The review card resolves the same destination, or the card would show a
    # folder the accept does not write to.
    preview = proposal_service._people_preview(config, row, str(row.get("text") or ""))
    assert preview["destination"] == "Humans/Mo.md", preview
    assert preview["can_accept"] is True, preview

    resp = client.post(f"/api/proposals/{row['id']}/accept")
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["action"] == "write_people_note"
    assert result["destination"] == "Humans/Mo.md", result

    notes_root = config.workspace_vault_root(row["workspace"])
    assert (notes_root / "Humans" / "Mo.md").is_file()
    assert not (notes_root / "People").exists(), "the stock folder, not the owner's"


def test_accept_persists_the_text_against_refiling(tmp_path: Path) -> None:
    """An accept must land in the same decision history a dismissal does.

    Append-time dedupe consults the live queue and the decision sidecar —
    never the promoted destination — so an accepted fact that leaves no
    trace in the sidecar is re-read by the nightly curator from the same
    transcript and queued again as if it were new.
    """
    from ciao.memory_proposals import MemoryProposal, append_proposals, dismissed_log_path

    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept")
    assert resp.status_code == 200

    queue = config.workspace_vault_root(row["workspace"]) / "Workspace" / "Memory-Proposals.md"
    assert row["text"] in dismissed_log_path(queue).read_text(encoding="utf-8")

    # The nightly pass re-reads the same transcript: refused as a dupe.
    assert (
        append_proposals(
            [MemoryProposal(target="memory", text=row["text"], source_section="curation")],
            config.workspace_vault_root(row["workspace"]),
        )
        is None
    )


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


def _write_skill_proposal(
    config: CiaoConfig,
    workspace: str,
    name: str,
    *,
    problem: str = "Repeated fetch failures.",
    change: str = "Add a defuddle fallback.",
) -> Path:
    """A versioned record, as the evolution pass writes it."""
    stored = skill_proposals.upsert_proposal(
        config,
        skill_proposals.SkillProposal(
            id=skill_proposals.proposal_id(workspace, name),
            workspace=workspace,
            skill=name,
            canonical_path=f"/agent/skills/{name}/SKILL.md",
            reviewed_revision="a" * 64,
            title=f"Skill reflection: {name}",
            problem=problem,
            change=change,
            rationale="It handles blocked pages.",
            sources=(
                skill_proposals.SkillEvidence(
                    chat_id="sess-a1",
                    archive="2026-08-09T10:00:00Z",
                    turn="",
                    excerpt="outcome=needs_review corrections=1 errors=0 turns=3",
                ),
            ),
            lifecycle=skill_proposals.PENDING,
            chat_id="",
            updated_at="2026-08-09T10:00:00Z",
        ),
    )
    return skill_proposals.proposal_path(config, workspace, stored.skill)


def test_a_skill_row_carries_the_record_not_the_filename(tmp_path: Path) -> None:
    """The listing used to hand the UI a bare stem, so a review card could say
    nothing about the proposal it was asking about: not what was noticed, not
    what was proposed, not which sessions it came from."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")

    rows, by_id = _scan_proposal_rows(config)

    skill = [r for r in rows if r["kind"] == "skill"]
    assert len(skill) == 1
    assert skill[0]["id"] in by_id
    assert skill[0]["skill"] == "2026-08-09-defuddle"
    assert skill[0]["title"] == "Skill reflection: 2026-08-09-defuddle"
    assert skill[0]["problem"] == "Repeated fetch failures."
    assert skill[0]["change"] == "Add a defuddle fallback."
    assert skill[0]["rationale"] == "It handles blocked pages."
    assert [item["chat_id"] for item in skill[0]["sources"]] == ["sess-a1"]
    assert skill[0]["lifecycle"] == skill_proposals.PENDING
    # Empty for a proposal filed before origins existed. An empty list is not
    # "this learning is dealt with": it is "this record links nothing", which is
    # why no learning behind it can be retired by settling the row.
    assert skill[0]["origins"] == []


def test_a_skill_row_carries_its_learning_links(tmp_path: Path) -> None:
    """Which learnings a finding came from, and what became of each one. The row
    is the only place a reviewer can see that accepting it will retire a lesson
    — and that it will not, until the sibling findings are answered too."""
    config = _config(tmp_path)
    skill_proposals.upsert_proposal(
        config,
        skill_proposals.SkillProposal(
            id=skill_proposals.proposal_id("personal", "notes"),
            workspace="personal",
            skill="notes",
            canonical_path="/agent/skills/notes/SKILL.md",
            reviewed_revision="a" * 64,
            title="Skill reflection: notes",
            problem="The user corrected the note type twice.",
            change="Read the Categories block before a type.",
            rationale="It repeated.",
            sources=(
                skill_proposals.SkillEvidence(
                    chat_id="sess-a1",
                    archive="2026-08-09T10:00:00Z",
                    turn="",
                    excerpt="no, that's a person",
                ),
            ),
            lifecycle=skill_proposals.PENDING,
            chat_id="",
            updated_at="2026-08-09T10:00:00Z",
            origins=(
                skill_proposals.SkillOrigin(
                    workspace="personal",
                    learning_id="5d6b0a1e-6f4a-5b1c-9d2e-3a4b5c6d7e8f",
                    source_revision="c" * 64,
                    finding="read the Categories block first",
                    summary="Add the Categories step.",
                ),
            ),
        ),
    )

    rows, _by_id = _scan_proposal_rows(config)

    skill = [r for r in rows if r["kind"] == "skill"]
    assert skill[0]["origins"] == [
        {
            "workspace": "personal",
            "learning_id": "5d6b0a1e-6f4a-5b1c-9d2e-3a4b5c6d7e8f",
            "source_revision": "c" * 64,
            "finding": "read the Categories block first",
            "summary": "Add the Categories step.",
            "state": skill_proposals.ORIGIN_PENDING,
            "verification": "",
        }
    ]


def test_a_legacy_skill_proposal_file_is_still_listed(tmp_path: Path) -> None:
    """The pre-#683 loose files have no versioned frontmatter. They must keep
    listing — a proposal a user already has is not something an upgrade gets to
    hide from the queue it was filed in."""
    config = _config(tmp_path)
    path = (
        config.workspace_vault_root("personal")
        / "Workspace" / "Skill-Proposals" / "2026-05-20-defuddle.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Skill reflection: defuddle\n\nA loose proposal.\n", encoding="utf-8")

    rows, _by_id = _scan_proposal_rows(config)

    skill = [r for r in rows if r["kind"] == "skill"]
    assert len(skill) == 1
    assert skill[0]["text"] == "2026-05-20-defuddle"
    assert skill[0]["title"] == "Skill reflection: defuddle"


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


def test_dismissing_a_skill_row_records_a_decision_and_leaves_the_queue(
    tmp_path: Path,
) -> None:
    """A reviewed proposal is a resolved decision — implemented or disregarded.
    It used to be resolved by deleting the file, which destroyed the only record
    that anyone had looked at it, so the next pass that saw the same evidence
    filed the same suggestion again as a new file. Now the decision is recorded
    and the record stays."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")

    response = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert response.status_code == 200, response.json()
    assert [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill"] == []
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.DISMISSED
    assert record.problem == "Repeated fetch failures."


def test_a_dismissed_skill_proposal_is_a_decision_the_history_shows(
    tmp_path: Path,
) -> None:
    """The decision is keyed by a synthetic ``skill:<name>`` text so a skill row
    can never be read as a memory fact that happens to share its wording, and so
    the pass that must honour it can find it after the file is gone."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")

    client.post(f"/api/proposals/{row['id']}/dismiss")

    history = client.get("/api/proposals/history").json()["rows"]
    assert [item["text"] for item in history] == ["skill:2026-08-09-defuddle"]
    assert history[0]["kind"] == "skill"
    assert history[0]["action"] == "dismissed"
    assert history[0]["via"] == "pwa"


def test_the_same_evidence_does_not_reappear_after_a_dismissal(tmp_path: Path) -> None:
    """The whole point of recording the decision. The pass merges into the
    settled record and it stays settled, so the queue does not re-ask a question
    the operator already answered — and the record still holds the evidence that
    was gathered."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")
    assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 200

    # A later pass sees the same session again, from a file that is gone.
    source.unlink()
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.DISMISSED
    assert [
        r for r in _client(config).get("/api/proposals").json()["rows"]
        if r["kind"] == "skill"
    ] == []


def test_dismissing_a_skill_row_twice_is_not_an_error(tmp_path: Path) -> None:
    """Two tabs racing on one row, or a retry after a dropped response. The
    second has nothing left to decide, which is not a failure."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")

    assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 200
    # The row is gone from the listing, so a retry is an unknown id — the same
    # answer every other settled row gives, and the reason settlement is
    # recorded rather than the row being kept around to be dismissed again.
    assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 404


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
    # Both rows are settled: the bullet leaves the queue file, the skill record
    # flips its lifecycle. Neither is deleted.
    assert "Remember the thing" not in _queue_text(config, "personal").read_text(
        encoding="utf-8"
    )
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.DISMISSED
    assert client.get("/api/proposals").json()["rows"] == []


def test_two_runs_of_one_skill_are_one_row_and_one_decision(tmp_path: Path) -> None:
    """Two dated proposals for the same skill used to be two files and two
    dismissals — the same finding, answered twice, with the second run's file
    left to be discovered and re-decided. Keyed by the skill, it is one record,
    so the second run merges into the first and one dismiss settles both."""
    config = _config(tmp_path)
    client = _client(config)
    first = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    # A later run writes to the same record: the same skill, new evidence.
    second = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")

    assert first == second
    rows = [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill"]
    assert len(rows) == 1

    assert client.post(f"/api/proposals/{rows[0]['id']}/dismiss").status_code == 200
    assert [
        r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill"
    ] == []
    history = client.get("/api/proposals/history").json()["rows"]
    assert [item["text"] for item in history] == ["skill:2026-08-09-defuddle"]


# -- accepting a skill proposal into a chat ----------------------------------
#
# The association used to be browser-local, so a reload or a second device
# started a second implementation and nothing recorded that either had run.


class _FakeProject:
    def __init__(self, project_id: str, name: str, workspace: str) -> None:
        self.project_id = project_id
        self.name = name
        self.workspace = workspace


class _FakeChat:
    def __init__(self, chat_id: str, project_id: str, title: str) -> None:
        self.chat_id = chat_id
        self.project_id = project_id
        self.title = title
        self.archived = False


class _FakePcm:
    """Just enough of the chat manager for the accept route."""

    def __init__(self, *, general_in: tuple[str, ...] = ("personal", "work")) -> None:
        self._projects = [
            _FakeProject(f"proj-{ws}", "General", ws) for ws in general_in
        ]
        self._chats: dict[str, _FakeChat] = {}
        self.prompts: list[tuple[str, str]] = []
        self.helpers: list[dict | None] = []

    def list_projects(self, workspace: str | None = None) -> list[_FakeProject]:
        if workspace is None:
            return list(self._projects)
        return [p for p in self._projects if p.workspace == workspace]

    def create_project(self, name: str, workspace: str) -> _FakeProject:
        project = _FakeProject(f"proj-{workspace}-new", name, workspace)
        self._projects.append(project)
        return project

    def create_chat(
        self, project_id: str, title: str = "New Chat", helper: dict | None = None
    ) -> _FakeChat:
        chat = _FakeChat(f"chat-{len(self._chats) + 1}", project_id, title)
        self._chats[chat.chat_id] = chat
        self.helpers.append(helper)
        return chat

    def get_chat(self, chat_id: str) -> _FakeChat | None:
        return self._chats.get(chat_id)

    def start_stream(self, chat_id: str, prompt: str) -> None:
        self.prompts.append((chat_id, prompt))


def _skill_row(client: TestClient) -> dict[str, Any]:
    return next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")


def test_accepting_a_skill_row_opens_one_chat_in_its_own_workspace(
    tmp_path: Path,
) -> None:
    """The skill is work's, so the chat is work's. Hosting it anywhere else
    edits the wrong copy of the catalog while the record says the right one."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "work", "2026-08-09-defuddle")
    pcm = _FakePcm()
    client = _client(config, pcm)
    row = _skill_row(client)

    response = client.post(f"/api/proposals/{row['id']}/implement")

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["ok"] is True
    assert body["created"] is True
    assert pcm.get_chat(body["chat_id"]).project_id == "proj-work"
    assert pcm.get_chat(body["chat_id"]).title == "Improve 2026-08-09-defuddle"
    # A resolution helper, so the chat archives once the proposal is resolved.
    assert pcm.helpers == [
        {
            "kind": "proposal",
            "intent": "resolve",
            "proposal_ids": [row["id"]],
            "archive_policy": "when_resolved",
        }
    ]


def test_accepting_twice_returns_the_same_chat_and_creates_no_duplicate(
    tmp_path: Path,
) -> None:
    """A double tap, a retry after a dropped response, and a second device all
    land here. Each is the same row, so each gets the same chat back."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    row = _skill_row(_client(config, pcm))

    first = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").json()
    second = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").json()

    assert second["chat_id"] == first["chat_id"]
    assert (first["created"], second["created"]) == (True, False)
    assert len(pcm.prompts) == 1, "the prompt was dispatched twice"
    # And the association is on the record, so a fresh client sees it too.
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.chat_id == first["chat_id"]
    assert record.lifecycle == skill_proposals.IMPLEMENTING


def test_a_second_device_reads_the_live_chat_off_the_row(tmp_path: Path) -> None:
    """The server row is the source of truth. The browser used to keep the same
    association in localStorage, which is exactly what another device cannot
    see, and a reload throws away."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    row = _skill_row(_client(config, pcm))
    accepted = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").json()

    relisted = _skill_row(_client(config, pcm))

    assert relisted["chat_id"] == accepted["chat_id"]
    assert relisted["lifecycle"] == skill_proposals.IMPLEMENTING
    # The finding is still on the row, so the review card can show what the
    # chat was asked to do.
    assert relisted["problem"] == "Repeated fetch failures."
    assert relisted["reviewed_revision"] == "a" * 64
    # Still queued: the work is unfinished, so the question stays on screen.
    assert _skill_row(_client(config, pcm))["id"] == row["id"]


def test_the_prompt_names_the_existing_skill_and_the_reviewed_revision(
    tmp_path: Path,
) -> None:
    """The prompt is the acceptance, and it is the server's. The browser's copy
    had drifted into telling the chat to create a skill — a different task from
    the one the record describes, since the skill is already there."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    client = _client(config, pcm)
    row = _skill_row(client)

    client.post(f"/api/proposals/{row['id']}/implement")

    prompt = pcm.prompts[0][1]
    assert prompt == skill_proposals.render_improvement_prompt(
        skill_proposals.find_proposal(config, row["id"])
    )
    assert "/agent/skills/2026-08-09-defuddle/SKILL.md" in prompt
    assert "already exists" in prompt
    assert "Repeated fetch failures." in prompt
    assert "Add a defuddle fallback." in prompt
    assert "aaaaaaaaaaaa" in prompt   # the reviewed revision, shortened
    assert "sess-a1" in prompt       # the evidence it came from
    assert "ciao skill-proposal-remove" in prompt


def test_an_archived_chat_is_not_a_live_implementation(tmp_path: Path) -> None:
    """A chat the operator can no longer open is not the answer to "is this
    still being worked on". The proposal is still queued, so re-accepting has
    to be able to start a fresh one rather than hand back a dead link."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    row = _skill_row(_client(config, pcm))
    first = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").json()
    pcm.get_chat(first["chat_id"]).archived = True

    second = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").json()

    assert second["created"] is True
    assert second["chat_id"] != first["chat_id"]
    # On disk, not just in the reply: a row still naming the dead chat is a row
    # whose "Open chat" is a link to nothing, and the next accept would think
    # the work is already running somewhere it cannot be.
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.chat_id == second["chat_id"]
    assert record.lifecycle == skill_proposals.IMPLEMENTING


def _real_pcm(config: CiaoConfig, tmp_path: Path) -> ProjectChatManager:
    """A real chat manager, so the route runs the real ``start_stream``.

    ``_FakePcm``'s ``start_stream`` appends to a list and returns, which is why
    it cannot see the defect this half of the file guards: the real
    ``ProjectChatManager.start_stream`` reaches
    ``ChatStreaming.start_drive``, which calls ``asyncio.create_task``. That is
    correct on the event loop and a ``RuntimeError: no running event loop`` in a
    worker thread, so an accept that dispatched its turn off-loop failed every
    time while every fake-based test stayed green.
    """
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def test_accepting_dispatches_the_turn_on_the_event_loop(tmp_path: Path) -> None:
    """The route with the real manager, through a real running loop.

    The off-loop half is the vault scan, the project lookup and ``create_chat``;
    the provider turn has to go out on the loop thread, like every other caller
    that opens a chat. Asserted on the turn actually being driven, not on a
    200: a route that returned OK without dispatching would leave a chat nobody
    is working in.
    """
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "work", "2026-08-09-defuddle")
    pcm = _real_pcm(config, tmp_path)
    driven: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        driven.append(prompt)
        yield ResultEvent(
            type="result",
            result="done",
            session_id="sess-x",
            is_error=False,
            usage={},
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    with _client(config, pcm) as client:
        row = _skill_row(client)
        response = client.post(f"/api/proposals/{row['id']}/implement")
        assert response.status_code == 200, response.json()
        body = response.json()
        # The drive task is created by ``start_stream`` itself, so a running
        # loop turns the turn into a call on the fake provider above.
        deadline = time.monotonic() + 5.0
        while not driven and time.monotonic() < deadline:
            time.sleep(0.01)

    assert driven, "the accept never dispatched a provider turn"
    assert "2026-08-09-defuddle" in driven[0]
    record = skill_proposals.parse_proposal(source, "work")
    assert record is not None
    assert record.chat_id == body["chat_id"]
    assert record.lifecycle == skill_proposals.IMPLEMENTING


def test_a_failed_dispatch_leaves_the_association_behind(tmp_path: Path) -> None:
    """A turn that cannot start must not orphan the chat it was opened for.

    Binding before dispatching is what makes a second accept return the same
    chat instead of minting another one: the record is the association, so it
    is written first, and the operator still has an "Open chat" to send into.
    """

    class _RefusingPcm(_FakePcm):
        def start_stream(self, chat_id: str, prompt: str) -> None:
            raise RuntimeError("no provider")

    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _RefusingPcm()
    row = _skill_row(_client(config, pcm))

    first = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement")

    assert first.status_code == 500
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.chat_id
    second = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").json()
    assert second["chat_id"] == record.chat_id
    assert second["created"] is False


def test_accepting_refuses_a_row_that_is_not_a_skill_proposal(tmp_path: Path) -> None:
    """A memory bullet is promoted by writing to a region. Routing one here
    would open a chat to do work the queue already does inline."""
    config = _default_vault(tmp_path)
    pcm = _FakePcm()
    client = _client(config, pcm)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "memory")

    response = client.post(f"/api/proposals/{row['id']}/implement")

    assert response.status_code == 409
    assert "skill proposal" in response.json()["error"]
    assert pcm.prompts == []


def test_accepting_an_unknown_or_settled_proposal_is_not_found(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    row = _skill_row(_client(config, pcm))
    assert _client(config, pcm).post(f"/api/proposals/{row['id']}/dismiss").status_code == 200

    assert _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").status_code == 404
    assert _client(config, pcm).post("/api/proposals/nope/implement").status_code == 404
    assert pcm.prompts == []


def test_accepting_settles_nothing_on_its_own(tmp_path: Path) -> None:
    """Completion is never inferred from the chat ending. The record stays
    queued until the work records an outcome, one way or the other."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    client = _client(config, pcm)
    row = _skill_row(client)
    client.post(f"/api/proposals/{row['id']}/implement")

    history = client.get("/api/proposals/history").json()["rows"]
    assert history == []
    assert _skill_row(_client(config, pcm))["lifecycle"] == skill_proposals.IMPLEMENTING


def test_a_dismissal_during_an_implementation_still_records_its_decision(
    tmp_path: Path,
) -> None:
    """Settling is a decision a person makes, in whatever state the work is in."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    client = _client(config, pcm)
    row = _skill_row(client)
    client.post(f"/api/proposals/{row['id']}/implement")

    assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 200

    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.DISMISSED
    history = client.get("/api/proposals/history").json()["rows"]
    assert [item["text"] for item in history] == ["skill:2026-08-09-defuddle"]


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

    assert proposal_service._leak_warning(config, "memory", "work") is False
    assert proposal_service._leak_warning(config, "profile", "work") is False


def test_a_shared_guide_still_warns(tmp_path: Path) -> None:
    """Before the re-rooting one CLAUDE.md really is loaded by every session."""
    config = _default_vault(tmp_path)
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()   # no receipt: shared layout

    assert proposal_service._leak_warning(config, "memory", "work") is True
    # The primary workspace's own row is where the guide belongs, so no warning.
    assert proposal_service._leak_warning(config, "memory", "personal") is False


def test_a_rehome_never_warns_in_either_layout(tmp_path: Path) -> None:
    """A move is not a region write."""
    config = _default_vault(tmp_path)
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()
    assert proposal_service._leak_warning(config, "rehome", "work") is False
    _rerooted(config, tmp_path)
    assert proposal_service._leak_warning(config, "rehome", "work") is False


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


# -- resolution paths settle only what they carried out -----------------------


def test_a_failed_accept_is_refused(tmp_path: Path) -> None:
    """A refused write leaves the bullet queued: an attempt, not a decision."""
    config = _default_vault(tmp_path)
    _corrupt_guide(config, "personal")
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 409


def test_an_unknown_batch_id_is_a_404(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]

    resp = client.post(
        "/api/proposals/batch",
        json={"action": "dismiss", "ids": [rows[0]["id"], "does-not-exist"]},
    )

    assert resp.status_code == 404


def test_a_batch_accept_keeps_unwritable_rows_queued(tmp_path: Path) -> None:
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


def test_the_bulk_sweep_removes_only_older_rows(tmp_path: Path) -> None:
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


def test_dismissing_a_skill_row_keeps_its_record(tmp_path: Path) -> None:
    """A skill row is settled rather than deleted."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")

    response = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert response.status_code == 200
    assert source.is_file()


def test_a_batch_rehome_accept_dismisses_every_result(tmp_path: Path) -> None:
    """A successful batch rehome produces two results (the move above the
    grouping and the bullet drop inside it); both report the row resolved."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])

    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})

    assert response.status_code == 200, response.json()
    assert all(r.get("dismissed") for r in response.json()["results"]), response.json()


def test_a_batch_row_removed_by_another_request_is_not_rewritten(
    tmp_path: Path, monkeypatch
) -> None:
    """When a concurrent resolver drops the bullet between this batch's scan
    and its write, ``_remove_bullet_line`` matches nothing; the loser reports
    success to the client but must not rewrite the queue."""
    config = _config(tmp_path)
    queue_path = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    _write_queue(config, "personal", "# Proposals\n\n- [memory] Remember the thing\n")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "memory")

    monkeypatch.setattr(proposal_service, "_remove_bullet_line", lambda *a, **k: False)
    response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]})

    assert response.status_code == 200
    assert response.json()["results"][0]["dismissed"] is True  # gone from review either way
    # The loser did not rewrite the queue around the winner's deletion.
    assert "Remember the thing" in queue_path.read_text()


def test_an_accept_that_loses_the_bullet_race_leaves_the_queue(tmp_path: Path, monkeypatch) -> None:
    """A concurrent resolver (another request, or the CLI) removes the bullet
    between this request's scan and its write: ``_remove_bullet_line`` matches
    nothing. The loser must not rewrite the queue around the winner's deletion
    — a decision it did not carry out."""
    config = _config(tmp_path)
    _write_queue(config, "personal", "# Proposals\n\n- [memory] Remember the thing\n")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "memory")

    monkeypatch.setattr(proposal_service, "_remove_bullet_line", lambda *a, **k: False)
    response = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert response.status_code == 200
    # The queue was left as the winner wrote it.
    queue_path = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    assert "- [memory] Remember the thing" in queue_path.read_text()


# ---- concurrent queue rewrites ---------------------------------------------


def test_dismiss_route_runs_the_locked_transaction_off_the_event_loop(
    tmp_path: Path, monkeypatch
) -> None:
    """A contended queue lock must not freeze the server.

    `queue_lock` waits with a synchronous sleep; if the route held it inline
    the event loop would stall for the wait. The rewrite helper must be
    dispatched through `asyncio.to_thread`.
    """
    import asyncio

    config = _config(tmp_path)
    _write_queue(config, "personal", "# Proposals\n\n- [memory] Remember the thing\n")
    client = _client(config)
    row = next(
        r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "memory"
    )

    real = proposal_service._rewrite_queue_single
    worker_threads: list[int] = []
    main_thread = __import__("threading").get_ident()

    def spy(*args, **kwargs):
        worker_threads.append(__import__("threading").get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, "_rewrite_queue_single", spy)
    response = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert response.status_code == 200
    assert worker_threads and worker_threads[0] != main_thread


def test_a_shifted_line_index_does_not_delete_a_bystander():
    """The captured index is a hint, not an address.

    `_scan_proposal_rows` records a line index, and an accept then awaits an
    unbounded model call before rewriting the queue - with no lock anywhere. A
    second accept or dismiss landing in that window removes a line and shifts
    every later index, so deleting by index alone took out an UNRELATED
    proposal and left the accepted one sitting in the queue.
    """
    from ciao.web.proposal_service import _remove_bullet_line

    # The bullet was at index 1 when it was scanned; a concurrent dismiss has
    # since removed the line above it, so index 1 now holds someone else.
    lines = ["- [memory] mine", "- [memory] a bystander"]

    assert _remove_bullet_line(lines, 2, "- [memory] mine") is True
    assert lines == ["- [memory] a bystander"], "it deleted the wrong bullet"


def test_removing_a_bullet_that_is_already_gone_is_a_no_op():
    """Whoever removed it got there first; nothing else may be taken instead."""
    from ciao.web.proposal_service import _remove_bullet_line

    lines = ["- [memory] someone else's"]

    assert _remove_bullet_line(lines, 0, "- [memory] already dismissed") is False
    assert lines == ["- [memory] someone else's"]


def test_accept_reports_the_text_it_actually_wrote(tmp_path: Path) -> None:
    """What landed is not always the sentence on the row.

    The event-shape guard promotes only a bullet's trailing "Durable rule:"
    clause, so a caller echoing the row's own text would tell the user something
    different from what is now in the region. The endpoint reports what it wrote.
    """
    config = _config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    _write_queue(
        config,
        "personal",
        "# Memory Proposals\n\n"
        "- [memory] User asked me to stop using em dashes. "
        "Durable rule: Avoid em dashes; use commas.  _(from: Decisions)_\n",
    )
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept")
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["promoted"] is True
    assert result["written"] == "Avoid em dashes; use commas."
    assert "Avoid em dashes; use commas." in _region_entries(
        config, row["workspace"], "memory"
    )


def test_accept_reports_a_duplicate_rather_than_claiming_a_write(tmp_path: Path) -> None:
    """A row already in the region resolves, but nothing was written.

    Without this the caller cannot tell "stored" from "was already there",
    which is the difference between a write and a no-op.
    """
    config = _config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    fact = "Prefers direct implementation over discussion."
    _write_queue(
        config,
        "personal",
        f"# Memory Proposals\n\n- [memory] {fact}  _(from: Decisions)_\n",
    )
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    first = client.post(f"/api/proposals/{row['id']}/accept")
    assert first.status_code == 200
    assert "duplicate" not in first.json()["result"]

    # Queue the identical fact again; the region already holds it.
    _write_queue(
        config,
        "personal",
        f"# Memory Proposals\n\n- [memory] {fact}  _(from: Decisions)_\n",
    )
    again = _accept_kind_row(client, "memory")
    resp = client.post(f"/api/proposals/{again['id']}/accept")
    assert resp.status_code == 200
    assert resp.json()["result"]["duplicate"] is True


# -- Decision history paging and identity -------------------------------------


def _seed_history(config: CiaoConfig, workspace: str, entries: list[dict]) -> None:
    """Write decision rows straight into a workspace's sidecar."""
    from ciao.memory_proposals import dismissed_log_path

    queue = config.workspace_vault_root(workspace) / "Workspace" / "Memory-Proposals.md"
    log = dismissed_log_path(queue)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )


def test_history_row_ids_are_unique_across_workspaces(tmp_path: Path) -> None:
    """The same fact decided in two workspaces in the same second.

    Timestamps are second-precision and the id used to hash only
    ts|action|kind|text, so these two rows collided. The History list keys its
    <li> on the id, so a collision made Vue drop one row on patch.
    """
    config = _config(tmp_path)
    row = {"promoted_at": "2026-09-01T10:00:00+00:00", "kind": "memory", "text": "Same fact."}
    for ws in ("personal", "work"):
        _seed_history(config, ws, [row])

    rows = _client(config).get("/api/proposals/history").json()["rows"]

    assert len(rows) == 2
    assert len({r["id"] for r in rows}) == 2
    assert {r["workspace"] for r in rows} == {"personal", "work"}


def test_history_row_ids_are_unique_for_identical_undated_rows(tmp_path: Path) -> None:
    """Two legacy rows with no timestamp and the same text are still two rows."""
    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [{"kind": "memory", "text": "Legacy fact."}, {"kind": "memory", "text": "Legacy fact."}],
    )

    rows = _client(config).get("/api/proposals/history").json()["rows"]

    assert len(rows) == 2
    assert len({r["id"] for r in rows}) == 2


def test_history_row_ids_are_stable_across_reads(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [
            {"promoted_at": "2026-09-01T10:00:00+00:00", "kind": "memory", "text": "One."},
            {"dismissed_at": "2026-09-01T11:00:00+00:00", "kind": "memory", "text": "Two."},
        ],
    )
    client = _client(config)

    first = client.get("/api/proposals/history").json()["rows"]
    second = client.get("/api/proposals/history").json()["rows"]

    assert [r["id"] for r in first] == [r["id"] for r in second]


def test_history_does_not_leak_the_internal_sequence_field(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_history(config, "personal", [{"kind": "memory", "text": "One."}])

    rows = _client(config).get("/api/proposals/history").json()["rows"]

    assert "seq" not in rows[0]


def test_history_reports_at_max_when_the_limit_is_clamped(tmp_path: Path) -> None:
    """Past the cap a wider limit returns the same page, and the client has to
    know that: a "show more" button wired to `truncated` alone stayed visible
    and did nothing forever.
    """
    from ciao.web import routes_api as api_module

    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [
            {"promoted_at": f"2026-09-01T10:00:{i:02d}+00:00", "kind": "memory", "text": f"Fact {i}."}
            for i in range(6)
        ],
    )
    client = _client(config)

    monkeyed = api_module._HISTORY_MAX_LIMIT
    api_module._HISTORY_MAX_LIMIT = 4
    try:
        data = client.get("/api/proposals/history", params={"limit": 200}).json()
    finally:
        api_module._HISTORY_MAX_LIMIT = monkeyed

    assert len(data["rows"]) == 4
    assert data["limit"] == 4
    assert data["total"] == 6
    # There ARE more rows...
    assert data["truncated"] is True
    # ...but asking for more will not return them.
    assert data["at_max"] is True


def test_history_does_not_report_at_max_below_the_cap(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [
            {"promoted_at": f"2026-09-01T10:00:{i:02d}+00:00", "kind": "memory", "text": f"Fact {i}."}
            for i in range(6)
        ],
    )

    data = _client(config).get("/api/proposals/history", params={"limit": 2}).json()

    assert data["truncated"] is True
    assert data["at_max"] is False
    assert data["limit"] == 2


def test_history_workspace_filter_pages_within_that_workspace(tmp_path: Path) -> None:
    """A quiet workspace's decisions must not fall outside a global page.

    The client used to fetch the newest N rows across every workspace and
    filter them client-side, so on a busy install a quiet workspace's History
    tab read "No decisions yet." with a full ledger.
    """
    config = _config(tmp_path)
    _seed_history(
        config,
        "work",
        [
            {"promoted_at": f"2026-09-02T10:00:{i:02d}+00:00", "kind": "memory", "text": f"Busy {i}."}
            for i in range(30)
        ],
    )
    _seed_history(
        config,
        "personal",
        [{"promoted_at": "2026-01-01T09:00:00+00:00", "kind": "memory", "text": "Quiet fact."}],
    )

    data = _client(config).get(
        "/api/proposals/history", params={"workspace": "personal", "limit": 5}
    ).json()

    assert [r["text"] for r in data["rows"]] == ["Quiet fact."]
    assert data["total"] == 1
    assert data["truncated"] is False


def test_history_reports_at_max_for_a_request_of_exactly_the_cap(tmp_path: Path) -> None:
    """A request for the cap is already at it.

    `at_max` was `requested > limit`, so asking for exactly the cap came back
    False alongside `truncated: True` — the client saw "more available, keep
    asking" and burned one full-page refetch before the flag finally tripped.
    """
    from ciao.web import routes_api as api_module

    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [
            {"promoted_at": f"2026-09-01T10:00:{i:02d}+00:00", "kind": "memory", "text": f"Fact {i}."}
            for i in range(6)
        ],
    )
    client = _client(config)

    monkeyed = api_module._HISTORY_MAX_LIMIT
    api_module._HISTORY_MAX_LIMIT = 4
    try:
        data = client.get("/api/proposals/history", params={"limit": 4}).json()
    finally:
        api_module._HISTORY_MAX_LIMIT = monkeyed

    assert data["limit"] == 4
    assert data["truncated"] is True
    assert data["at_max"] is True


def test_history_reads_a_shared_sidecar_once(tmp_path: Path) -> None:
    """Two registered names can resolve to one vault root, hence one sidecar.

    The loop trusted one-sidecar-per-name, so both names read the same file and
    every decision in it appeared twice with `total` double-counted.
    """
    vault = tmp_path / "memory-vault"
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=vault,
        workspaces={
            # Both point at the same root, as a `vault_root: "."` entry or the
            # legacy entity layout does.
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/shared"),
            "alias": WorkspaceConfig(name="alias", vault_root="memory-vault/shared"),
        },
    )
    _seed_history(
        config,
        "personal",
        [{"promoted_at": "2026-09-01T10:00:00+00:00", "kind": "memory", "text": "One."}],
    )

    data = _client(config).get("/api/proposals/history").json()

    assert data["total"] == 1
    assert [r["text"] for r in data["rows"]] == ["One."]


def test_history_does_not_leak_the_internal_sidecar_field(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_history(config, "personal", [{"kind": "memory", "text": "One."}])

    rows = _client(config).get("/api/proposals/history").json()["rows"]

    assert "log" not in rows[0]
    assert "seq" not in rows[0]


def test_a_stale_rehome_row_says_so_to_the_client(tmp_path: Path) -> None:
    """A bullet that outlived its cause reaches the PWA marked `stale`.

    `_rehome_lookup` computes `stale` for exactly this case — a `[rehome]`
    bullet naming a note that no longer produces a live signal — so the UI can
    offer to clear the litter instead of asking the operator to decide it. The
    row builder copied the signal field by field and dropped `stale`, so the
    stale branch in the client was dead code and such a row rendered as a
    genuine "no destination, needs a decision" question.
    """
    config = _per_root_config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "People").mkdir(parents=True, exist_ok=True)
    _rerooted_vault(config, tmp_path)
    # Deliberately no note on disk: nothing backs this bullet any more.
    _write_queue(config, "personal", (
        "## curation pass\n\n"
        "- [rehome] Re-home `personal/People/Ghost.md` to `work/People/Ghost.md`? "
        "Uncertain.  _(from: vault-rehome)_\n"
    ))
    client = _client(config)

    row = _accept_kind_row(client, "rehome")

    assert row["rehome"]["stale"] is True, row["rehome"]
    assert row["rehome"]["candidates"] == []


def test_a_live_rehome_row_is_not_marked_stale(tmp_path: Path) -> None:
    """The companion guard: a real question must not read as litter."""
    _config, _client_, row = _rehome_fixture(tmp_path, ["person", "colleague"])

    assert row["rehome"]["stale"] is False, row["rehome"]


_LEARNINGS_TEXT = "Check the queue lock before writing the destination."


def _learnings_queue(text: str = _LEARNINGS_TEXT) -> str:
    return (
        "# Memory Proposals\n\n"
        "## 2026-08-19 curation pass (this pass)\n\n"
        f"- [learnings] {text}  _(from: Decisions)_\n"
    )


def _learnings_vault(tmp_path: Path) -> CiaoConfig:
    config = _config(tmp_path)
    (config.workspace_vault_root("personal") / "Workspace").mkdir(
        parents=True, exist_ok=True
    )
    _write_queue(config, "personal", _learnings_queue())
    return config


def _learnings_count(config: CiaoConfig) -> int:
    """The recurrence count on the seeded learning, or 0 if nothing was written."""
    path = config.workspace_vault_root("personal") / "Workspace" / "Learnings.md"
    if not path.exists():
        return 0
    match = re.search(r"\(x(\d+)\)", path.read_text(encoding="utf-8"))
    return int(match.group(1)) if match else 0


# ---- Category proposals (issue #647) ----------------------------------------
#
# The notes carry the RAW spelling the cluster was detected under and the sidecar
# carries the id derived from it, so a retype is a real change of bytes rather
# than a rewrite of a value to itself.

_RAW_TYPE = "Recipe Book"
_CATEGORY_ID = "recipe-book"


def _category_vault(
    tmp_path: Path,
    *,
    folder: str = "Recipes",
    ticked: tuple[str, ...] = ("One.md", "Two.md", "Three.md"),
    declared: str | None = _RAW_TYPE,
) -> CiaoConfig:
    """A workspace whose queue holds one `[category]` row over a note cluster.

    ``ticked`` is the note list the sidecar carries — the rows the owner ticked
    in the drawer — and ``declared`` is what each of those notes currently says.
    A test for drift passes a different type, and one for a frontmatter-less
    note passes ``None``.

    The sidecar's paths are rendered ``Entry.path`` values: a vault directory
    name followed by the note's path inside the vault that was scanned, which is
    what ``vault_migration`` strips the same way when it retypes a note.
    """
    from ciao.memory_proposals import MemoryProposal, append_proposals
    from ciao.vocabulary_proposals import write_category_sidecar

    config = _config(tmp_path)
    vault = config.workspace_vault_root("personal")
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    notes = vault / "Journals"
    notes.mkdir(parents=True, exist_ok=True)
    for name in ticked:
        frontmatter = f"type: {declared}\n" if declared else ""
        (notes / name).write_text(
            f"---\n{frontmatter}---\n# {Path(name).stem}\n", encoding="utf-8"
        )
    write_category_sidecar(
        vault,
        {
            "id": _CATEGORY_ID,
            "label": "Recipe book",
            "folder": folder,
            "description": f"Proposed from {len(ticked)} notes already typed {_RAW_TYPE}.",
            "source_type": _RAW_TYPE,
            "paths": [f"memory-vault/Journals/{name}" for name in ticked],
            "declined": False,
        },
    )
    append_proposals(
        [
            MemoryProposal(
                target="category",
                payload=_CATEGORY_ID,
                text=(
                    f"Recipe book → {folder}: Proposed from {len(ticked)} notes "
                    f"already typed {_RAW_TYPE}."
                ),
                source_section=f"{len(ticked)} notes typed {_RAW_TYPE}",
            )
        ],
        vault,
    )
    return config


def _registry_ids(config: CiaoConfig) -> list[str]:
    from ciao import entity_types

    registry = entity_types.load_entity_types(config.agent_vault_root("personal"))
    return [entry.id for entry in registry.entries()]


def test_accepting_a_category_adds_it_and_retypes_the_ticked_notes(
    tmp_path: Path,
) -> None:
    """Nothing is moved: every note stays in the same file and only its
    frontmatter ``type:`` changes."""
    config = _category_vault(tmp_path)
    journals = config.workspace_vault_root("personal") / "Journals"
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 200, resp.json()
    result = resp.json()["result"]
    assert result["action"] == "add_category"
    assert result["promoted"] is True
    assert result["destination"] == "Recipes"
    assert _CATEGORY_ID in _registry_ids(config)
    assert sorted(p.name for p in journals.iterdir()) == ["One.md", "Three.md", "Two.md"]
    for note in journals.iterdir():
        # The body and the file are untouched; only the type line moved.
        assert note.read_text(encoding="utf-8") == (
            f"---\ntype: {_CATEGORY_ID}\n---\n# {note.stem}\n"
        )
    # The row is gone, and the registry file names the new category.
    assert client.get("/api/proposals").json()["rows"] == []
    yaml = (config.agent_vault_root("personal") / "entity-types.yaml").read_text(
        encoding="utf-8"
    )
    assert f"id: {_CATEGORY_ID}" in yaml
    assert "folder: Recipes" in yaml


def test_accepting_a_category_leaves_an_unticked_note_alone(tmp_path: Path) -> None:
    """The sidecar is the note list, so a note the owner left unticked keeps
    the type it had."""
    config = _category_vault(tmp_path, ticked=("One.md", "Two.md"))
    journals = config.workspace_vault_root("personal") / "Journals"
    (journals / "Three.md").write_text(
        f"---\ntype: {_RAW_TYPE}\n---\n# Three\n", encoding="utf-8"
    )
    client = _client(config)
    row = _accept_kind_row(client, "category")

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200

    assert (journals / "One.md").read_text(encoding="utf-8") == (
        f"---\ntype: {_CATEGORY_ID}\n---\n# One\n"
    )
    assert (journals / "Three.md").read_text(encoding="utf-8") == (
        f"---\ntype: {_RAW_TYPE}\n---\n# Three\n"
    )


def test_accepting_a_category_retypes_a_note_with_no_frontmatter(
    tmp_path: Path,
) -> None:
    """A note that never said what it was has no type anybody could have
    changed, so it is retyped rather than reported as drifted."""
    config = _category_vault(tmp_path, ticked=("One.md",), declared=None)
    journals = config.workspace_vault_root("personal") / "Journals"
    client = _client(config)
    row = _accept_kind_row(client, "category")

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200

    assert (journals / "One.md").read_text(encoding="utf-8") == (
        f"---\ntype: {_CATEGORY_ID}\n---\n# One\n"
    )


def test_a_category_whose_folder_is_taken_keeps_the_bullet(tmp_path: Path) -> None:
    """`Recipes` is not a collision, `People` is. The registry's own validator
    refuses it, the row survives, and the refusal is a 400 the owner can act on
    rather than a retry of the same request."""
    config = _category_vault(tmp_path, folder="People")
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 400, resp.json()
    assert "People" in resp.json()["error"]
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]
    assert _CATEGORY_ID not in _registry_ids(config)


def test_a_note_retyped_since_the_proposal_keeps_the_bullet(tmp_path: Path) -> None:
    """The cluster the operator read is not the cluster on disk, so nothing is
    written — not the registry, and not the notes that were fine."""
    config = _category_vault(tmp_path)
    journals = config.workspace_vault_root("personal") / "Journals"
    (journals / "Two.md").write_text("---\ntype: document\n---\n# Two\n", encoding="utf-8")
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    assert "Two.md" in resp.json()["error"]
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]
    assert _CATEGORY_ID not in _registry_ids(config)
    assert (journals / "One.md").read_text(encoding="utf-8") == (
        f"---\ntype: {_RAW_TYPE}\n---\n# One\n"
    )


def test_dismissing_a_category_records_the_refusal_by_id(tmp_path: Path) -> None:
    """The queue bullet is gone but the cluster it described is still in the
    vault. A text-keyed decision has nothing left to match once the row is
    removed, so the id is what stops the same category coming back."""
    from ciao.vocabulary_proposals import declined_category_ids, read_category_sidecar

    config = _category_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _accept_kind_row(client, "category")

    assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 200

    assert client.get("/api/proposals").json()["rows"] == []
    assert declined_category_ids(vault) == {_CATEGORY_ID}
    # The rest of the sidecar survives: a refusal is not a deletion.
    assert read_category_sidecar(vault, _CATEGORY_ID)["paths"]


def test_a_batch_dismiss_records_the_refusal_too(tmp_path: Path) -> None:
    """The batch path records the id before the rewrite too, or a bulk "reject
    these" would bring the whole cluster back the next night."""
    from ciao.vocabulary_proposals import declined_category_ids

    config = _category_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(
        "/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]}
    )

    assert resp.status_code == 200, resp.json()
    assert client.get("/api/proposals").json()["rows"] == []
    assert declined_category_ids(vault) == {_CATEGORY_ID}


def test_a_category_refusal_that_cannot_be_recorded_keeps_the_bullet(
    tmp_path: Path,
) -> None:
    """The sidecar is the only record of the note list, so a dismiss without one
    has nothing to refuse: the row survives rather than being resolved into a
    decision that was not made."""
    from ciao.vocabulary_proposals import category_sidecar_path

    config = _category_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    category_sidecar_path(vault, _CATEGORY_ID).unlink()
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert resp.status_code == 409, resp.json()
    assert _CATEGORY_ID in resp.json()["error"]
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_a_category_preview_names_the_notes_it_would_retype(tmp_path: Path) -> None:
    """The card must not fall through to the review row's "no destination yet"
    wording: a category has a destination and the whole list of notes it moves
    onto, and both are known before the click."""
    config = _category_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/preview")

    assert resp.status_code == 200, resp.json()
    body = resp.json()["preview"]
    assert body["action"] == "add_category"
    assert body["can_accept"] is True
    assert body["exact"] is True
    assert body["category"]["id"] == _CATEGORY_ID
    assert body["category"]["folder"] == "Recipes"
    assert len(body["category"]["notes"]) == 3


def test_batch_accept_covers_a_category_row(tmp_path: Path) -> None:
    config = _category_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}
    )

    assert resp.status_code == 200, resp.json()
    assert resp.json()["results"][0]["promoted"] is True
    assert _CATEGORY_ID in _registry_ids(config)


# ---- note_edit proposals: a whole note, decided by a person ----------------
#
# The fixture FILES the proposal through the proposer rather than hand-writing
# a bullet, so these tests drive the real path from a `needs_review` verdict to
# a click: the row's id, the sidecar, and the check the filing recorded are all
# the ones a nightly pass would produce.

_EDIT_NOTE = "notes/office.md"
_EDIT_PLAIN = (
    "---\ntype: note\nupdated: 2024-01-05\n---\n\n"
    "# Office\n\nThe office is on Via Verdi 12, third floor.\n"
)
_EDIT_FOURTH = _EDIT_PLAIN.replace("third", "fourth")


def _note_edit_vault(
    tmp_path: Path,
    *,
    operation: str = "replace",
    outcome: str = "update",
    after: str = _EDIT_FOURTH,
    before: str = _EDIT_PLAIN,
    text_on_disk: str | None = None,
    today: date | None = None,
) -> tuple[CiaoConfig, Any]:
    """A workspace whose queue holds one `[note_edit]` row over a real note."""
    from ciao import memory_receipts as mr
    from ciao import note_edit_proposals as nep

    config = _config(tmp_path)
    vault = config.workspace_vault_root("personal")
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    note = vault / _EDIT_NOTE
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_bytes((text_on_disk if text_on_disk is not None else before).encode("utf-8"))
    proposal = nep.file_note_edit(
        config,
        workspace="personal",
        relative_path=_EDIT_NOTE,
        expected_revision=mr.content_revision(before),
        operation=operation,
        before=before,
        after=after,
        outcome=outcome,
        coverage="complete",
        evidence=(),
        reason="the third floor no longer exists",
        today=today,
    )
    return config, proposal


def _note_edit_row(client: TestClient) -> dict:
    return _accept_kind_row(client, "note_edit")


def test_a_note_edit_row_names_the_note_it_is_about(tmp_path: Path) -> None:
    """The bullet's payload is a sidecar id; the row a person reads is a path.

    A digest on the row would tell a reviewer nothing about which note they are
    being asked to change, and `destination_revision` needs the note to have
    something to compare.
    """
    config, proposal = _note_edit_vault(tmp_path)
    client = _client(config)

    row = _note_edit_row(client)

    assert row["target"] == _EDIT_NOTE
    assert row["note_edit"]["id"] == proposal.id
    assert row["note_edit"]["operation"] == "replace"
    assert row["note_edit"]["can_accept"] is True
    assert row["note_edit"]["settled"] == ""


def test_a_note_edit_preview_is_byte_exact_to_the_accept(tmp_path: Path) -> None:
    """The card's "after" IS the bytes the write produces.

    A whole-note rewrite is the one accept whose result is fully known before the
    click, so `exact` is True and `revision` is the digest the accept re-checks
    — and the preview's after-image has to be the file's bytes afterwards, not
    something the card recomputed.
    """
    config, _proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/preview")

    assert resp.status_code == 200, resp.json()
    body = resp.json()["preview"]
    assert body["action"] == "note_edit"
    assert body["operation"] == "note_edit"
    assert body["destination"] == _EDIT_NOTE
    assert body["destination_path"] == str(vault / _EDIT_NOTE)
    assert body["exact"] is True
    assert body["can_accept"] is True
    assert body["before"] == _EDIT_PLAIN
    assert body["after"] == _EDIT_FOURTH

    accepted = client.post(f"/api/proposals/{row['id']}/accept")

    assert accepted.status_code == 200, accepted.json()
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == body["after"]


def test_accepting_a_note_edit_applies_it_through_the_note_receipt(
    tmp_path: Path,
) -> None:
    """Journaled, revision-checked and undoable like every other note write."""
    from ciao.memory_receipts import journal_path, read_receipts

    config, proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 200, resp.json()
    result = resp.json()["result"]
    assert result["action"] == "note_edit"
    assert result["promoted"] is True
    assert result["dismissed"] is True
    assert result["destination"] == _EDIT_NOTE
    assert "region" not in result, "a note edit is not a region write"
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_FOURTH

    receipt = [
        r
        for r in read_receipts(journal_path(vault, None))
        if r["kind"] == "note_apply"
    ][-1]
    assert receipt["provenance"]["operation"] == "replace"
    assert receipt["provenance"]["proposal_id"] == proposal.proposal_id

    # The row is gone and the record says the decision was made, with the
    # receipt that can undo it.
    assert client.get("/api/proposals").json()["rows"] == []
    from ciao import note_edit_proposals as nep

    settled = nep.read_sidecar(config, "personal", proposal.id)
    assert settled is not None and settled.accepted is True
    assert settled.receipt_id == receipt["id"]


def test_a_note_that_moved_since_the_proposal_is_a_conflict(tmp_path: Path) -> None:
    """The replacement was planned against text that is no longer there.

    A note nobody can overwrite is the whole point of the expected revision, and
    a conflict is told apart from a failure so the client reopens its preview
    rather than reporting the row as broken.
    """
    config, _proposal = _note_edit_vault(
        tmp_path, text_on_disk=_EDIT_PLAIN + "\nA line somebody added by hand.\n"
    )
    vault = config.workspace_vault_root("personal")
    edited = (vault / _EDIT_NOTE).read_bytes()
    client = _client(config)
    row = _note_edit_row(client)

    preview = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]
    assert preview["can_accept"] is False
    assert "changed since" in preview["reason"]

    resp = client.post(
        f"/api/proposals/{row['id']}/accept", json={"expected_revision": preview["revision"]}
    )

    assert resp.status_code == 409, resp.json()
    assert resp.json()["conflict"] is True
    assert (vault / _EDIT_NOTE).read_bytes() == edited, "byte-identical, untouched"
    # The row survives, so the owner can re-read the note and decide again.
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_a_stale_preview_is_refused_before_the_write(tmp_path: Path) -> None:
    """The card's own revision handshake, on the note this accept writes."""
    config, _proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)
    revision = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"][
        "revision"
    ]
    (vault / _EDIT_NOTE).write_text(
        _EDIT_PLAIN + "\nA line somebody added by hand.\n", encoding="utf-8"
    )

    resp = client.post(
        f"/api/proposals/{row['id']}/accept", json={"expected_revision": revision}
    )

    assert resp.status_code == 409, resp.json()
    assert "changed since this preview" in resp.json()["error"]
    assert "preview" in resp.json()
    assert _EDIT_PLAIN in (vault / _EDIT_NOTE).read_text(encoding="utf-8")


def test_accepting_a_retire_trashes_the_note_and_links_the_outcome(
    tmp_path: Path,
) -> None:
    """Attended-only, reversible, and never a delete.

    The only primitive reached is `vault_review.trash_note`, so the note is in
    the review trash with its metadata and the panel's own restore puts it back.
    """
    config, _proposal = _note_edit_vault(
        tmp_path, operation="retire", outcome="retire", after=""
    )
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 200, resp.json()
    result = resp.json()["result"]
    assert result["promoted"] is True
    assert not Path(result["destination"]).is_absolute(), (
        "one destination shape, whichever operation wrote it"
    )
    trashed = vault / result["destination"]
    assert not (vault / _EDIT_NOTE).exists()
    assert trashed.is_file()
    assert trashed.read_text(encoding="utf-8") == _EDIT_PLAIN

    from ciao import vault_review as review

    review.restore_note(vault, trashed.stem)
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_PLAIN


def test_a_retire_of_a_note_that_moved_is_a_conflict(tmp_path: Path) -> None:
    """A retirement is checked against the revision, like every other operation.

    A retirement moves whatever is on disk, and `trash_note`'s hash guard compares
    the note to the bytes the accept just read — so without the expected revision
    in front of it, a note rewritten after the proposal was filed would go to the
    trash with the owner's hand-written line in it, under a 200. The note nobody
    judged is not a note anybody may retire.
    """
    config, _proposal = _note_edit_vault(
        tmp_path, operation="retire", outcome="retire", after=""
    )
    vault = config.workspace_vault_root("personal")
    edited = _EDIT_PLAIN + "\nA line somebody added by hand.\n"
    (vault / _EDIT_NOTE).write_text(edited, encoding="utf-8")
    client = _client(config)
    row = _note_edit_row(client)

    preview = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]

    assert preview["operation"] == "retire_note"
    assert preview["can_accept"] is False
    assert "changed since" in preview["reason"]
    # The card's own revision handshake covers a retirement too, rather than
    # being skipped for the one write a person cannot walk back by editing.
    assert preview["revision"]

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    assert resp.json()["conflict"] is True
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == edited, (
        "byte-identical, untouched: nothing reached the trash"
    )
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_a_settled_note_edit_row_cannot_be_accepted_again(tmp_path: Path) -> None:
    """The accept writes first and settles second, so a row can outlive its decision.

    A bullet whose removal failed leaves exactly that: a settled record behind a
    queued row. The second click must find the decision already on record, or a
    refusal is applied as a rewrite and an accepted retirement is performed twice.
    """
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)
    nep.settle_note_edit(config, "personal", proposal.id, accepted=True)

    preview = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]

    assert preview["can_accept"] is False
    assert "already decided" in preview["reason"]
    assert _note_edit_row(client)["note_edit"]["can_accept"] is False

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    assert "already decided" in resp.json()["error"]
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_PLAIN, (
        "the settled decision was applied once, not twice"
    )
    # The row is still there, because a decision cannot un-settle itself.
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_a_restamp_applies_the_date_it_was_filed_on(tmp_path: Path) -> None:
    """A re-stamp's bytes are computed, so the date has to be on the record.

    `date.today()` at accept time made the preview and the write two different
    documents whenever the click landed on the other side of midnight — and the
    card is labelled `exact`. Filed on a fixed date, the note comes back carrying
    THAT date whatever day the accept runs on.
    """
    filed = date(2026, 3, 14)
    config, _proposal = _note_edit_vault(
        tmp_path, operation="restamp", outcome="still_valid", after="", today=filed
    )
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    preview = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]

    assert preview["can_accept"] is True
    assert preview["exact"] is True
    assert "updated: 2026-03-14" in preview["after"]

    accepted = client.post(f"/api/proposals/{row['id']}/accept")

    assert accepted.status_code == 200, accepted.json()
    written = (vault / _EDIT_NOTE).read_text(encoding="utf-8")
    assert "updated: 2026-03-14" in written
    assert written == preview["after"], "the preview's bytes are the written bytes"


def test_dismissing_a_note_edit_settles_it_without_refusing_it_forever(
    tmp_path: Path,
) -> None:
    """A dismissal releases the check, and leaves an expiring cooldown behind.

    Not a "rejected forever" marker: the note is not asked about again this
    month, and the moment it is edited the check no longer describes it and a
    fresh pass may propose it again.
    """
    from ciao import note_edit_proposals as nep
    from ciao import note_verification as nv
    from ciao.memory_receipts import content_revision

    config, proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert resp.status_code == 200, resp.json()
    assert client.get("/api/proposals").json()["rows"] == []
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_PLAIN
    settled = nep.read_sidecar(config, "personal", proposal.id)
    assert settled is not None and settled.settled != ""
    assert settled.accepted is False
    check = nv.read_note_checks(vault)[_EDIT_NOTE]
    assert check.proposal_id == ""
    assert nv.should_check(
        vault, _EDIT_NOTE, content_revision(_EDIT_PLAIN), today=date.today()
    ) is False, "the cooldown is still running"


def test_a_batch_with_a_note_edit_row_returns_an_explicit_result(
    tmp_path: Path,
) -> None:
    """Both ways, and neither may fall through to the `[review]` wording.

    "No destination yet" describes a row with nowhere to go, which is the
    opposite of a row whose destination is the note it names — and a client
    reading it would show a decision that has not been asked for.
    """
    config, _proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    accepted = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}
    )

    assert accepted.status_code == 200, accepted.json()
    results = accepted.json()["results"]
    assert len(results) == 1
    assert results[0]["action"] == "note_edit"
    assert results[0]["promoted"] is True
    assert "no destination yet" not in json.dumps(results)
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_FOURTH

    # And a row it cannot apply says so, per row, and stays queued.
    config, _proposal = _note_edit_vault(
        tmp_path / "conflict",
        text_on_disk=_EDIT_PLAIN + "\nA line somebody added by hand.\n",
    )
    client = _client(config)
    row = _note_edit_row(client)

    refused = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}
    )

    assert refused.status_code == 200, refused.json()
    result = refused.json()["results"][0]
    assert result["promoted"] is False
    assert result["conflict"] is True
    assert "no destination yet" not in result.get("error", "")
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_a_batch_dismiss_settles_a_note_edit_too(tmp_path: Path) -> None:
    """A bulk "reject these" must settle what it removed, or the check keeps
    holding a note with nothing in the queue to settle it."""
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(tmp_path)
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(
        "/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]}
    )

    assert resp.status_code == 200, resp.json()
    assert client.get("/api/proposals").json()["rows"] == []
    settled = nep.read_sidecar(config, "personal", proposal.id)
    assert settled is not None and settled.accepted is False


def test_a_note_edit_without_its_record_keeps_the_bullet(tmp_path: Path) -> None:
    """The record IS the operation. Without it there is nothing to apply, so the
    row survives for the owner to dismiss rather than resolving into a decision
    that was not made."""
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    nep.sidecar_path(config, "personal", proposal.id).unlink()
    client = _client(config)
    row = _note_edit_row(client)
    assert row["note_edit"]["can_accept"] is False

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    assert "no longer on file" in resp.json()["error"]
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_PLAIN


def test_a_write_that_cannot_be_settled_keeps_the_row_and_says_why(
    tmp_path: Path, monkeypatch
) -> None:
    """The write landed; the decision did not. The row has to survive.

    An ``ok=True`` here removes the bullet, and the bullet is the only thing left
    to act on: the sidecar stays unsettled, the check keeps this revision pinned
    to a proposal nobody is being asked about, and
    ``note_verification._check_settles`` suppresses the note for good — for a
    re-stamp of an already-current note, whose revision never moves, that means
    it is never verified again. So the accept refuses with what landed and where,
    and the row stays for the dismissal that settles it.
    """
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    def _read_only(*_args: Any, **_kwargs: Any) -> Any:
        raise nep.NoteEditSidecarError("the sidecar is read-only")

    monkeypatch.setattr(nep, "settle_note_edit", _read_only)

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    error = resp.json()["error"]
    assert f"{_EDIT_NOTE} was written and the receipt is" in error
    assert "the sidecar is read-only" in error
    assert "Dismiss this row" in error
    # The write stands and the row is still there to be acted on.
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_FOURTH
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]
    unsettled = nep.read_sidecar(config, "personal", proposal.id)
    assert unsettled is not None and unsettled.settled == ""

    # The transient failure is over, and the row is the recovery path: a
    # dismissal settles the record it could not.
    monkeypatch.undo()
    dismissed = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert dismissed.status_code == 200, dismissed.json()
    assert client.get("/api/proposals").json()["rows"] == []
    settled = nep.read_sidecar(config, "personal", proposal.id)
    assert settled is not None and settled.settled != ""
    assert settled.accepted is False, "a dismissal is a refusal, not an accept"


def test_a_retire_that_cannot_be_settled_keeps_the_row_too(
    tmp_path: Path, monkeypatch
) -> None:
    """A retirement is the one write a person cannot walk back by editing.

    So the row that says the note is in the trash has to stay until the record of
    the decision is on disk: a check pinned to a proposal that is no longer
    queued suppresses a note nothing will ever ask about again.
    """
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(
        tmp_path, operation="retire", outcome="retire", after=""
    )
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    def _read_only(*_args: Any, **_kwargs: Any) -> Any:
        raise nep.NoteEditSidecarError("the sidecar is read-only")

    monkeypatch.setattr(nep, "settle_note_edit", _read_only)

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    assert "could not be settled" in resp.json()["error"]
    assert not (vault / _EDIT_NOTE).exists(), "the note really was trashed"
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]
    unsettled = nep.read_sidecar(config, "personal", proposal.id)
    assert unsettled is not None and unsettled.settled == ""


def test_a_dismiss_that_cannot_be_settled_keeps_the_bullet(tmp_path: Path) -> None:
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(tmp_path)
    nep.sidecar_path(config, "personal", proposal.id).unlink()
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert resp.status_code == 409, resp.json()
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def _undo_client(config: CiaoConfig) -> TestClient:
    """A client that also serves the undo the History card calls.

    The accept and the undo are two routes on the same decision, so a test that
    drove one over HTTP and the other as a function would not prove the pair
    works: the receipt is the only thing between them.
    """
    from ciao.web.routes_api import memory_receipt_undo

    app = Starlette(
        routes=[
            Route("/api/proposals", list_proposals, methods=["GET"]),
            Route("/api/proposals/{id}/{action}", proposal_action, methods=["POST"]),
            Route(
                "/api/memory/receipts/{id}/undo", memory_receipt_undo, methods=["POST"]
            ),
        ]
    )
    app.state.config = config
    return TestClient(app)


def test_undoing_an_accepted_category_restores_the_yaml_and_every_note(
    tmp_path: Path,
) -> None:
    """Acceptance: the receipt undoes the registry entry AND every retyped note.

    Driven through the two routes, over the real cluster: the notes carry
    ``type: Recipe Book`` and the id derived from it is ``recipe-book``, so each
    retype is a real change of bytes and the restore is observed rather than
    asserted against files that never moved. A receipt that recorded no image of
    what the retype left behind could not tell an unrelated later edit from its
    own work, and would refuse the undo of every category it had just applied.
    """
    from ciao.memory_receipts import journal_path, read_receipts

    config = _category_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    journals = vault / "Journals"
    registry = config.agent_vault_root("personal") / "entity-types.yaml"
    before = {note.name: note.read_text(encoding="utf-8") for note in journals.iterdir()}
    assert not registry.exists(), "the fixture must start with no category file"
    client = _undo_client(config)
    row = _accept_kind_row(client, "category")

    accepted = client.post(f"/api/proposals/{row['id']}/accept")

    assert accepted.status_code == 200, accepted.json()
    assert registry.exists()
    receipt = [
        r
        for r in read_receipts(journal_path(vault, None))
        if r["kind"] == "category_apply"
    ][-1]
    # Every note really changed, and every note's after-image says so.
    images = {
        entry["path"]: entry["after"] for entry in receipt["category_notes"]
    }
    assert set(images) == {str(note) for note in journals.iterdir()}
    for note in journals.iterdir():
        assert note.read_text(encoding="utf-8") != before[note.name]
        assert images[str(note)] == note.read_text(encoding="utf-8")

    undo = client.post(
        f"/api/memory/receipts/{receipt['id']}/undo", params={"workspace": "personal"}
    )

    assert undo.status_code == 200, undo.json()
    # The vault had no category file before, so the undo leaves none behind.
    assert not registry.exists()
    for name, text in before.items():
        assert (journals / name).read_text(encoding="utf-8") == text


def test_an_accepted_category_is_not_offered_again(tmp_path: Path) -> None:
    """The accepted id is canonical, so the cluster it came from is not drift.

    The accept writes ``entity-types.yaml`` at the AGENT vault root — the same
    file ``GET``/``PATCH /api/memory/entity-types`` reads, and a different
    directory from the notes on an install that has not re-rooted. A pass that
    measured the notes against the registry under the notes root would keep
    seeing drift: the bullet comes back, and accepting it 400s on a duplicate id
    and a duplicate folder that no owner edit can resolve.
    """
    from ciao import entity_types
    from ciao import curation_run as cr
    from ciao.memory_tool import ensure_regions

    config = _category_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _accept_kind_row(client, "category")
    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200
    # A fourth note, already carrying the accepted id: the cluster is intact and
    # every note in it is canonical.
    (vault / "Journals" / "Four.md").write_text(
        f"---\ntype: {_CATEGORY_ID}\n---\n# Four\n", encoding="utf-8"
    )
    guide = config.agent_root("personal") / "CLAUDE.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    ensure_regions(guide)

    worklist = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        category_registry=entity_types.load_entity_types(
            config.agent_vault_root("personal")
        ),
        workspace_dir=config.workspace_root,
    )

    assert cr.PASS_CATEGORIES not in {item.pass_id for item in worklist.items}
    queue = (vault / "Workspace" / "Memory-Proposals.md").read_text(encoding="utf-8")
    assert "[category" not in queue


def _append_category_row(
    config: CiaoConfig,
    *,
    category_id: str,
    raw_type: str,
    folder: str,
    names: tuple[str, str, str],
) -> None:
    """A second ``[category]`` row over its own cluster, in the same queue."""
    from ciao.memory_proposals import MemoryProposal, append_proposals
    from ciao.vocabulary_proposals import write_category_sidecar

    vault = config.workspace_vault_root("personal")
    journals = vault / "Journals"
    for name in names:
        (journals / name).write_text(
            f"---\ntype: {raw_type}\n---\n# {Path(name).stem}\n", encoding="utf-8"
        )
    write_category_sidecar(
        vault,
        {
            "id": category_id,
            "label": category_id.replace("-", " ").title(),
            "folder": folder,
            "description": f"Proposed from {len(names)} notes already typed {raw_type}.",
            "source_type": raw_type,
            "paths": [f"memory-vault/Journals/{name}" for name in names],
            "declined": False,
        },
    )
    append_proposals(
        [
            MemoryProposal(
                target="category",
                payload=category_id,
                text=(
                    f"{category_id.replace('-', ' ').title()} → {folder}: "
                    f"Proposed from {len(names)} notes already typed {raw_type}."
                ),
                source_section=f"{len(names)} notes typed {raw_type}",
            )
        ],
        vault,
    )


def test_two_category_accepts_both_persist(tmp_path: Path) -> None:
    """Two clusters accepted one after the other: both categories are on disk.

    Each accept appends to the same registry file, so this is the case where a
    second write built from a stale snapshot drops the first entry — silently,
    with the second accept still reporting success.
    """
    config = _category_vault(tmp_path)
    _append_category_row(
        config,
        category_id="cook-book",
        raw_type="Cook Book",
        folder="Cook Books",
        names=("Four.md", "Five.md", "Six.md"),
    )
    client = _client(config)
    first = _accept_kind_row(client, "category")
    assert client.post(f"/api/proposals/{first['id']}/accept").status_code == 200
    second = _accept_kind_row(client, "category")
    assert client.post(f"/api/proposals/{second['id']}/accept").status_code == 200

    ids = _registry_ids(config)
    assert _CATEGORY_ID in ids
    assert "cook-book" in ids
    assert client.get("/api/proposals").json()["rows"] == []


def test_the_registry_is_read_inside_the_lock_that_writes_it(
    tmp_path: Path, monkeypatch
) -> None:
    """The read, the append and the write are one transaction.

    Loaded before the lock was taken, two accepts could both read the list before
    either wrote, append to the same snapshot, and the second write would replace
    the first — one category lost with no error anywhere. The lock is the whole
    fix, so the test holds it from here and watches the accept refuse to read
    until it is released. The ``started`` event is what makes that a claim about
    the ordering rather than about how fast the thread happened to start.
    """
    from ciao import entity_types
    from ciao.memory_receipts import queue_lock

    config = _category_vault(tmp_path)
    registry = config.agent_vault_root("personal") / "entity-types.yaml"
    first, second = _client(config), _client(config)
    row = _accept_kind_row(first, "category")
    started, read_registry = threading.Event(), threading.Event()
    real_accept = proposal_service._accept_category_row
    real_load = entity_types.load_entity_types

    def announce(cfg, accepted_row):
        started.set()
        return real_accept(cfg, accepted_row)

    def watched_load(vault):
        read_registry.set()
        return real_load(vault)

    monkeypatch.setattr(proposal_service, "_accept_category_row", announce)
    monkeypatch.setattr(entity_types, "load_entity_types", watched_load)
    accepted: dict[str, Any] = {}

    def accept() -> None:
        accepted["resp"] = second.post(f"/api/proposals/{row['id']}/accept")

    thread = threading.Thread(target=accept)
    with queue_lock(registry):
        thread.start()
        assert started.wait(10), "the accept never reached its category handler"
        # The lock is held here, so the accept is parked before it reads the
        # list it is about to append to.
        assert not read_registry.wait(2), "the registry was read outside the lock"
    thread.join(20)

    assert read_registry.is_set()
    assert accepted["resp"].status_code == 200, accepted["resp"].json()
    assert _CATEGORY_ID in _registry_ids(config)


def test_batch_accept_does_not_promote_before_the_receipt_can_be_written(
    tmp_path: Path,
) -> None:
    """The batch's pre-flight, the same one the single-row accept runs.

    `_rewrite_queue_batch` is what raises `QueueReceiptUnavailable`, and it runs
    after every destination mutation — so an unwritable journal returned 503
    with the learning already appended (or a region written, a doc folded) and
    every bullet still queued, and the operator's retry applied the whole batch
    a second time.
    """
    config = _learnings_vault(tmp_path)
    # A directory where the journal file belongs: the receipt append cannot
    # succeed, the same way a read-only vault leaves it.
    journal = config.workspace_vault_root("personal") / "Workspace" / "Memory-Receipts.jsonl"
    journal.mkdir()
    client = _client(config)
    row = _accept_kind_row(client, "learnings")

    resp = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}
    )

    assert resp.status_code == 503, resp.json()
    # The fact must NOT have been written: a 503 with the destination already
    # mutated is what turns the operator's retry into a double promotion.
    assert _learnings_count(config) == 0
    # And the row is still queued, so the retry has something to apply.
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_two_concurrent_accepts_promote_the_row_once(
    tmp_path: Path, monkeypatch
) -> None:
    """Two tabs accepting one proposal must not write the fact twice.

    An accept promotes first and rewrites the queue last, so both requests
    passed the id lookup, both promoted — the recurrence count incremented
    twice — and only then contended on `_rewrite_queue_single`, where the loser
    removed nothing and still reported success.
    """
    config = _learnings_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    from ciao.memory_proposals import append_learning

    # Seed the learning at (x1) so each promotion is visible as an increment.
    append_learning(vault, _LEARNINGS_TEXT)
    assert _learnings_count(config) == 1

    entered = threading.Event()
    release = threading.Event()
    real_accept = proposal_service._accept_learnings_row
    calls: list[str] = []

    def blocking_accept(cfg, row):
        """Hold the FIRST request inside its promotion: the real race window."""
        calls.append(row["id"])
        if len(calls) == 1:
            entered.set()
            release.wait(10)
        return real_accept(cfg, row)

    monkeypatch.setattr(proposal_service, "_accept_learnings_row", blocking_accept)

    # Two clients, so the second request runs on its own event loop instead of
    # queueing behind the first one's blocked handler.
    first_client, second_client = _client(config), _client(config)
    pid = _accept_kind_row(first_client, "learnings")["id"]
    first: dict[str, Any] = {}

    def run_first() -> None:
        first["resp"] = first_client.post(f"/api/proposals/{pid}/accept")

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert entered.wait(10), "the first accept never reached its promotion"
        second = second_client.post(f"/api/proposals/{pid}/accept")
    finally:
        release.set()
        thread.join(20)

    assert _learnings_count(config) == 2, "the fact was promoted twice"
    assert first["resp"].status_code == 200, first["resp"].json()
    # The loser is told the row is spoken for instead of reporting a success it
    # never performed.
    assert second.status_code == 409, second.json()
    # One promotion, one removal: the bullet is gone exactly once.
    assert first_client.get("/api/proposals").json()["rows"] == []


def test_an_accept_revalidates_the_row_another_resolver_took(
    tmp_path: Path, monkeypatch
) -> None:
    """The other half of the guard: a resolver outside this process.

    The claim is in-process (holding the file lock across a promotion would
    block every other queue writer for a model call), so the CLI, the undo
    path or a second server can still resolve a row between this request's scan
    and its promotion. Promoting anyway writes the fact a second time for a
    bullet this request then fails to remove.
    """
    config = _learnings_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    from ciao.memory_proposals import append_learning

    append_learning(vault, _LEARNINGS_TEXT)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    real_probe = routes_api._accept_journal_writable

    def steal_the_row(cfg, workspace, queue_path):
        """Another resolver lands in the window the pre-flight sits in."""
        text = queue.read_text(encoding="utf-8")
        queue.write_text(
            "\n".join(
                line for line in text.splitlines() if "[learnings]" not in line
            )
            + "\n",
            encoding="utf-8",
        )
        return real_probe(cfg, workspace, queue_path)

    monkeypatch.setattr(routes_api, "_accept_journal_writable", steal_the_row)
    client = _client(config)
    pid = _accept_kind_row(client, "learnings")["id"]

    resp = client.post(f"/api/proposals/{pid}/accept")

    assert resp.status_code == 409, resp.json()
    assert _learnings_count(config) == 1, "the fact was promoted a second time"


# ---- Retry reconciliation from the review path ----------------------------


_COMPETING_QUEUE = """# Memory Proposals

## 2026-09-19 curation pass (this pass)

- [memory] Office is in Berlin.  _(from: Decisions)_
"""


def _competing_vault(tmp_path: Path) -> CiaoConfig:
    """A queued fact that supersedes an entry already in the region.

    The archive-time reconcile deferred it, so it is sitting in the queue with
    its competitor still live. Accepting it plainly appends both.
    """
    config = _config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    _write_queue(config, "personal", _COMPETING_QUEUE)
    from ciao import memory_tool as mt

    guide = config.agent_root("personal") / "AGENTS.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    guide.write_text("# Guide\n\n", encoding="utf-8")
    mt.ensure_regions(guide)
    mt.write_region(guide, "memory", ["Office is in Zurich. [2026-01-01]"])
    return config


def _memory_entries(config: CiaoConfig) -> list[str]:
    from ciao import memory_tool as mt

    entries, _diags = mt.read_region(config.agent_root("personal") / "AGENTS.md", "memory")
    return entries


def test_accept_without_reconcile_stays_model_free(tmp_path: Path, monkeypatch) -> None:
    """The default accept is still one synchronous write, no model call.

    A reconcile on every click costs the batch endpoint one timeout per row,
    which is why the retry is opt-in rather than the new default.
    """

    async def never(*args: object, **kwargs: object) -> str:  # pragma: no cover
        raise AssertionError("the plain accept must not call a model")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", never)
    config = _competing_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200


def test_reconcile_on_accept_replaces_the_competing_entry(
    tmp_path: Path, monkeypatch
) -> None:
    """`?reconcile=1` resolves a deferred fact instead of appending beside it."""

    async def merges(prompt: str, **kwargs: object) -> str:
        assert "Office is in Zurich." in prompt
        return '[{"action": "update", "index": 1, "text": "Office is in Berlin."}]'

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", merges)
    config = _competing_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept?reconcile=1")

    assert resp.status_code == 200, resp.json()
    entries = _memory_entries(config)
    assert len(entries) == 1, "the obsolete entry and its replacement are both live"
    assert entries[0].startswith("Office is in Berlin.")


def test_a_retry_that_cannot_decide_keeps_the_row_queued(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed retry is not a licence to append; the bullet survives."""

    async def timing_out(prompt: str, **kwargs: object) -> str:
        raise TimeoutError("reconcile timed out")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", timing_out)
    config = _competing_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept?reconcile=1")

    assert resp.status_code == 409, resp.json()
    # The reason and the competing entry are on the response, not only in a log.
    assert "Office is in Zurich." in resp.json()["error"]
    assert _memory_entries(config) == ["Office is in Zurich. [2026-01-01]"]
    assert [r["kind"] for r in client.get("/api/proposals").json()["rows"]] == ["memory"]


def test_a_deferral_names_what_it_was_weighed_against(
    tmp_path: Path, monkeypatch
) -> None:
    """The refusal is structured, not only prose.

    The review UI puts the reason and the competing entries next to the retry
    button. Taking them back out of the error sentence would make the wording
    of that sentence part of the contract, so both are carried as fields —
    ``deferred`` marks the one refusal another retry can resolve on its own,
    which is why it and an over-cap region must not look alike.
    """

    async def timing_out(prompt: str, **kwargs: object) -> str:
        raise TimeoutError("reconcile timed out")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", timing_out)
    config = _competing_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    body = client.post(f"/api/proposals/{row['id']}/accept?reconcile=1").json()

    assert body["deferred"] is True
    assert body["reason"], "a deferral with no reason is an ordinary refusal"
    assert body["competing"] == ["Office is in Zurich. [2026-01-01]"]


def test_an_unshaped_refusal_is_not_marked_deferred(
    tmp_path: Path, monkeypatch
) -> None:
    """Only a deferral offers a retry, because only a deferral can be retried.

    An event-shaped fact is refused no matter how many times it is reconciled;
    marking it retryable would put a button on the row that cannot do what it
    says.
    """

    async def unused(*args: object, **kwargs: object) -> str:  # pragma: no cover
        raise AssertionError("an unshaped fact is rejected before any model call")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", unused)
    config = _config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    _write_queue(
        config,
        "personal",
        "# Memory Proposals\n\n## 2026-09-19 curation pass (this pass)\n\n"
        "- [memory] User said the office moved.  _(from: Decisions)_\n",
    )
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept?reconcile=1")

    assert resp.status_code == 409, resp.json()
    assert "deferred" not in resp.json()
