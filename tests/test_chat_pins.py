"""Durable chat-pin state and its cross-device contract (#1118).

The pin is engine state in `.runtime/web_projects.json`, not browser state, so
these tests build a real `ProjectChatManager` and a real `TestClient` over the
same routes the PWA uses (the construction pattern from
`tests/test_chat_fork_route.py`) rather than stubbing the seam. A stub could
agree with itself about a field the registry never persisted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ChatPinConflictError, ProjectChatManager
from ciao.web.routes_api import chat_detail, chat_file_path


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _restart(manager: ProjectChatManager) -> ProjectChatManager:
    """Build a second manager over the same registry, as a restart would."""
    config = manager._config
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, manager._config.workspace_root,
                               config.media_root),
        transcript_store=TranscriptStore(
            Path(config.state_path).parent,
            Path(config.state_path).parent / "transcripts",
        ),
        path=manager._path,
    )


def _make_client(manager: ProjectChatManager) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/chats/{chat_id}", chat_detail, methods=["PATCH"]),
            Route("/api/chats/{chat_id}/file-path", chat_file_path, methods=["GET"]),
        ]
    )
    app.state.project_chat_manager = manager
    app.state.config = manager._config
    return TestClient(app, raise_server_exceptions=False)


def _one_chat(manager: ProjectChatManager):
    project = manager.create_project("Pins", workspace="work")
    return manager.create_chat(project.project_id, title="Pinned")


@pytest.mark.parametrize("suffix", [".md", ".html", ".htm", ".pdf", ".png"])
def test_chat_file_identity_uses_agent_root(tmp_path, monkeypatch, suffix):
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    root = tmp_path / "work"
    root.mkdir()
    relative = f"draft{suffix}"
    (root / relative).write_text("correct workspace")
    (tmp_path / relative).write_text("wrong workspace")
    monkeypatch.setattr(CiaoConfig, "agent_root", lambda self, workspace: root)
    response = _make_client(manager).get(
        f"/api/chats/{chat.chat_id}/file-path", params={"path": relative}
    )
    assert response.status_code == 200
    assert response.json() == {"path": (root / relative).as_posix()}


def test_pin_relative_path_uses_chat_agent_root(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    root = tmp_path / "work"
    root.mkdir()
    (root / "draft.md").write_text("correct workspace")
    (tmp_path / "draft.md").write_text("wrong workspace")
    monkeypatch.setattr(CiaoConfig, "agent_root", lambda self, workspace: root)
    response = _make_client(manager).patch(
        f"/api/chats/{chat.chat_id}",
        json={"pin": {"path": "draft.md", "expected_revision": 0}},
    )
    assert response.status_code == 200
    assert response.json()["pinned_file_path"] == (root / "draft.md").as_posix()


def test_chat_file_identity_missing_target_never_searches_another_workspace(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    (tmp_path / "draft.md").write_text("wrong workspace")
    monkeypatch.setattr(CiaoConfig, "agent_root", lambda self, workspace: tmp_path / "work")
    client = _make_client(manager)
    assert client.get(f"/api/chats/{chat.chat_id}/file-path", params={"path": "draft.md"}).status_code == 404
    assert client.get("/api/chats/missing/file-path", params={"path": "draft.md"}).status_code == 404


def _route_workspaces(manager: ProjectChatManager, monkeypatch, tmp_path: Path) -> dict[str, Path]:
    """Register two workspaces, each with its own agent root and vault root under tmp_path."""
    roots = {}
    for name in ("work", "other"):
        roots[name] = tmp_path / name
        (tmp_path / name / "vault").mkdir(parents=True)
        manager._config.workspaces[name] = WorkspaceConfig(name=name, vault_root=f"{name}/vault")
    monkeypatch.setattr(
        CiaoConfig, "agent_root", lambda self, workspace: roots[workspace]
    )
    monkeypatch.setattr(
        CiaoConfig,
        "workspace_vault_root",
        lambda self, workspace: roots[workspace] / "vault",
    )
    return roots


def test_chat_file_bare_filename_resolves_inside_agent_root(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    roots = _route_workspaces(manager, monkeypatch, tmp_path)
    (roots["work"] / "notes").mkdir()
    (roots["work"] / "notes" / "plan.md").write_text("in the chat workspace")
    response = _make_client(manager).get(
        f"/api/chats/{chat.chat_id}/file-path", params={"path": "plan.md"}
    )
    assert response.status_code == 200
    assert response.json() == {"path": (roots["work"] / "notes" / "plan.md").as_posix()}


def test_chat_file_vault_relative_path_resolves_inside_workspace_vault(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    roots = _route_workspaces(manager, monkeypatch, tmp_path)
    (roots["work"] / "vault" / "people").mkdir()
    (roots["work"] / "vault" / "people" / "ana.md").write_text("vault note")
    response = _make_client(manager).get(
        f"/api/chats/{chat.chat_id}/file-path", params={"path": "people/ana.md"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "path": (roots["work"] / "vault" / "people" / "ana.md").as_posix()
    }


def test_chat_file_same_name_in_other_workspace_is_404(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    roots = _route_workspaces(manager, monkeypatch, tmp_path)
    (roots["other"] / "draft.md").write_text("other workspace")
    (roots["other"] / "vault" / "people").mkdir()
    (roots["other"] / "vault" / "people" / "ana.md").write_text("other vault")
    client = _make_client(manager)
    url = f"/api/chats/{chat.chat_id}/file-path"
    assert client.get(url, params={"path": "draft.md"}).status_code == 404
    assert client.get(url, params={"path": "people/ana.md"}).status_code == 404


def test_chat_file_fuzzy_stem_only_match_is_404(tmp_path, monkeypatch):
    """A missing report.md must not open scripts/report.py."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    roots = _route_workspaces(manager, monkeypatch, tmp_path)
    (roots["work"] / "scripts").mkdir()
    (roots["work"] / "scripts" / "report.py").write_text("print('not the note')")
    response = _make_client(manager).get(
        f"/api/chats/{chat.chat_id}/file-path", params={"path": "report.md"}
    )
    assert response.status_code == 404


def test_chat_file_fuzzy_same_suffix_in_other_folder_resolves(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    roots = _route_workspaces(manager, monkeypatch, tmp_path)
    (roots["work"] / "notes").mkdir()
    (roots["work"] / "notes" / "report.md").write_text("the note")
    (roots["work"] / "scripts").mkdir()
    (roots["work"] / "scripts" / "report.py").write_text("print('not the note')")
    response = _make_client(manager).get(
        f"/api/chats/{chat.chat_id}/file-path", params={"path": "report.md"}
    )
    assert response.status_code == 200
    assert response.json() == {"path": (roots["work"] / "notes" / "report.md").as_posix()}


def test_chat_file_fuzzy_walks_nested_vault_once(tmp_path, monkeypatch):
    """The vault sits under the agent root, so only the agent root is searched."""
    import ciao.web.routes_helpers as helpers

    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    roots = _route_workspaces(manager, monkeypatch, tmp_path)
    searched: list[list[Path]] = []
    real_find = helpers._find_fuzzy_match

    def recording_find(search_roots, candidate):
        searched.append(list(search_roots))
        return real_find(search_roots, candidate)

    monkeypatch.setattr(helpers, "_find_fuzzy_match", recording_find)
    response = _make_client(manager).get(
        f"/api/chats/{chat.chat_id}/file-path", params={"path": "missing.md"}
    )
    assert response.status_code == 404
    assert searched == [[roots["work"]]]


def _registry(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    )


def test_pin_round_trip_survives_manager_restart(tmp_path: Path) -> None:
    """The pin is in the registry, not in the process that accepted the write.

    Checked at three levels — the on-disk row, the reconstructed `ChatInfo`, and
    the chat payload — because a field that reaches the object but never the
    file looks exactly like one that works until the next restart.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    target = tmp_path / "notes" / "plan.md"
    target.parent.mkdir(parents=True)
    target.write_text("plan", encoding="utf-8")

    payload = manager.set_chat_pin(
        chat.chat_id, target.as_posix(), expected_revision=0
    )
    assert payload == {
        "path": target.as_posix(),
        "dismissed_paths": [],
        "revision": 1,
    }

    row = _registry(tmp_path)["chats"][chat.chat_id]
    assert row["pinned_file_path"] == target.as_posix()
    assert row["dismissed_pin_paths"] == []
    assert row["pin_revision"] == 1

    restored = _restart(manager)
    chat_again = restored.get_chat(chat.chat_id)
    assert chat_again is not None
    assert chat_again.pinned_file_path == target.as_posix()
    assert chat_again.dismissed_pin_paths == []
    assert chat_again.pin_revision == 1
    detail = chat_again.to_dict()
    assert detail["pinned_file_path"] == target.as_posix()
    assert detail["dismissed_pin_paths"] == []
    assert detail["pin_revision"] == 1


def test_zero_state_registry_records_restore_unpinned(tmp_path: Path) -> None:
    """A record written before pins existed starts clean rather than failing.

    Every existing install has a registry with none of the three keys, so this
    is the ordinary upgrade path, not an edge case. It must not import anything
    from a browser either: the engine's registry is the only source.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    registry = tmp_path / ".runtime" / "web_projects.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    for field in ("pinned_file_path", "dismissed_pin_paths", "pin_revision"):
        payload["chats"][chat.chat_id].pop(field, None)
    registry.write_text(json.dumps(payload), encoding="utf-8")

    restored = _restart(manager)
    chat_again = restored.get_chat(chat.chat_id)
    assert chat_again is not None
    assert chat_again.pinned_file_path == ""
    assert chat_again.dismissed_pin_paths == []
    assert chat_again.pin_revision == 0
    assert restored.chat_pin_states[chat.chat_id] == {
        "path": "",
        "dismissed_paths": [],
        "revision": 0,
    }


def test_unpin_dismisses_only_current_path_and_manual_repin_clears_it(
    tmp_path: Path,
) -> None:
    """Closing pins what was open, and repinning clears only that path's own record.

    The dismissal is what stops the next agent surface from reopening what the
    user just closed, so it has to name the path the SERVER had selected rather
    than the empty string the client sent. And a user who deliberately pins that
    same file again has overridden the dismissal for that one path — nothing
    else may be cleared along with it.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    first = tmp_path / "a.md"
    second = tmp_path / "b.md"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")

    manager.set_chat_pin(chat.chat_id, first.as_posix(), expected_revision=0)
    manager.set_chat_pin(chat.chat_id, second.as_posix(), expected_revision=1)
    closed = manager.set_chat_pin(chat.chat_id, "", expected_revision=2)

    assert closed == {
        "path": "",
        # Only the path that was open, not the first one it replaced earlier.
        "dismissed_paths": [second.as_posix()],
        "revision": 3,
    }

    # An agent surface of the dismissed path leaves the panel closed.
    assert manager.surface_chat_file(chat.chat_id, second.as_posix()) == {
        "path": "",
        "dismissed_paths": [second.as_posix()],
        "revision": 3,
    }

    repinned = manager.set_chat_pin(
        chat.chat_id, second.as_posix(), expected_revision=3
    )
    assert repinned == {
        "path": second.as_posix(),
        "dismissed_paths": [],
        "revision": 4,
    }

    # And a surface now reopens it, because the dismissal is gone.
    assert manager.surface_chat_file(chat.chat_id, second.as_posix()) == {
        "path": second.as_posix(),
        "dismissed_paths": [],
        "revision": 4,
    }


def test_unpin_without_a_pin_dismisses_nothing(tmp_path: Path) -> None:
    """Closing an already-closed panel has nothing to dismiss.

    Otherwise the dismissal list would fill with paths the user never saw open,
    and a later surface of one of them would be refused for no reason.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)

    result = manager.set_chat_pin(chat.chat_id, "", expected_revision=0)

    assert result == {"path": "", "dismissed_paths": [], "revision": 0}


def test_stale_pin_revision_conflicts_without_overwriting(tmp_path: Path) -> None:
    """A write that raced a newer selection must not replace it.

    Two devices share one chat and therefore one pin. Without the revision check
    the second device would silently reopen what the first had just closed, and
    the user would see a panel they did not ask for appear.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    target = tmp_path / "file.md"
    target.write_text("x", encoding="utf-8")

    manager.set_chat_pin(chat.chat_id, target.as_posix(), expected_revision=0)
    # Another device closed it at revision 2; this write still quotes 1.
    manager.set_chat_pin(chat.chat_id, "", expected_revision=1)

    with pytest.raises(ChatPinConflictError) as excinfo:
        manager.set_chat_pin(chat.chat_id, "/other.md", expected_revision=1)

    # The conflict carries the authoritative state so the caller can re-apply.
    assert excinfo.value.pin == {
        "path": "",
        "dismissed_paths": [target.as_posix()],
        "revision": 2,
    }
    assert manager.get_chat(chat.chat_id).pinned_file_path == ""
    assert manager.get_chat(chat.chat_id).pin_revision == 2

    # And it never reached disk either.
    row = _registry(tmp_path)["chats"][chat.chat_id]
    assert row["pinned_file_path"] == ""
    assert row["pin_revision"] == 2


def test_unrelated_title_change_does_not_conflict(tmp_path: Path) -> None:
    """The revision guards pins only; ordinary updates stay unversioned.

    A title change is not competing for the same shared selection, so making it
    quote a pin revision would fail a write that has no reason to fail.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    target = tmp_path / "file.md"
    target.write_text("x", encoding="utf-8")
    manager.set_chat_pin(chat.chat_id, target.as_posix(), expected_revision=0)

    updated = manager.update_chat(chat.chat_id, title="Renamed")

    assert updated.title == "Renamed"
    assert updated.pinned_file_path == target.as_posix()
    assert updated.pin_revision == 1


def test_set_pin_on_missing_chat_returns_not_found(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)

    assert manager.set_chat_pin("chat-nope", "/a.md", expected_revision=0) is None
    assert manager.surface_chat_file("chat-nope", "/a.md") is None


def test_noop_pin_does_not_publish_or_increment(tmp_path: Path) -> None:
    """Repeating the state a chat is already in is not a change.

    Otherwise a reconnect, a double-tap, or an agent surfacing the file already
    pinned would each bump the revision and publish, which would make the
    revision useless as a change counter and drown real events.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    target = tmp_path / "file.md"
    target.write_text("x", encoding="utf-8")
    published: list[dict] = []
    hub = manager._events
    original = hub.publish
    hub.publish = published.append  # type: ignore[method-assign]

    manager.set_chat_pin(chat.chat_id, target.as_posix(), expected_revision=0)
    assert len(published) == 1

    # Same path again, at the current revision.
    again = manager.set_chat_pin(chat.chat_id, target.as_posix(), expected_revision=1)
    hub.publish = original  # type: ignore[method-assign]

    assert again == {"path": target.as_posix(), "dismissed_paths": [], "revision": 1}
    assert len(published) == 1
    # An agent surfacing the same file is also a no-op.
    assert manager.surface_chat_file(chat.chat_id, target.as_posix()) == again
    assert manager.get_chat(chat.chat_id).pin_revision == 1


def test_pin_save_failure_rolls_back_and_publishes_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """A pin that could not be written must not look accepted.

    The client would otherwise cache a selection the engine does not have, and
    the next write would quote a revision nothing ever stored. So the in-memory
    fields go back, nothing is published, and the error propagates instead of
    being swallowed into a success.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    target = tmp_path / "file.md"
    target.write_text("x", encoding="utf-8")
    published: list[dict] = []
    hub = manager._events
    original = hub.publish
    hub.publish = published.append  # type: ignore[method-assign]

    def fail_save(*args, **kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_save", fail_save)
    try:
        with pytest.raises(OSError):
            manager.set_chat_pin(chat.chat_id, target.as_posix(), expected_revision=0)
    finally:
        hub.publish = original  # type: ignore[method-assign]

    chat_again = manager.get_chat(chat.chat_id)
    assert chat_again.pinned_file_path == ""
    assert chat_again.dismissed_pin_paths == []
    assert chat_again.pin_revision == 0
    assert published == []

    # The restored baseline still describes the chat, so a later save persists
    # the retry rather than dropping it as already-applied.
    monkeypatch.undo()
    result = manager.set_chat_pin(chat.chat_id, target.as_posix(), expected_revision=0)
    assert result["revision"] == 1
    assert _registry(tmp_path)["chats"][chat.chat_id]["pinned_file_path"] == (
        target.as_posix()
    )


def _pin_body(path: str, revision: int) -> dict:
    return {"pin": {"path": path, "expected_revision": revision}}


def test_pin_patch_sets_and_clears_a_pin(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    target = tmp_path / "note.md"
    target.write_text("n", encoding="utf-8")
    client = _make_client(manager)

    response = client.patch(
        f"/api/chats/{chat.chat_id}", json=_pin_body(target.as_posix(), 0)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["pinned_file_path"] == target.as_posix()
    assert body["dismissed_pin_paths"] == []
    assert body["pin_revision"] == 1

    cleared = client.patch(f"/api/chats/{chat.chat_id}", json=_pin_body("", 1))
    assert cleared.status_code == 200
    assert cleared.json()["pinned_file_path"] == ""
    assert cleared.json()["dismissed_pin_paths"] == [target.as_posix()]
    assert cleared.json()["pin_revision"] == 2


def test_pin_patch_resolves_a_relative_path_to_canonical_identity(
    tmp_path: Path,
) -> None:
    """What is stored is the resolved path, so the identity always matches.

    A relative string would never equal the canonical path an agent surface
    records for the same file, so the dismissal check would silently miss.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "october.md").write_text("o", encoding="utf-8")
    client = _make_client(manager)

    response = client.patch(
        f"/api/chats/{chat.chat_id}",
        json=_pin_body("reports/october.md", 0),
    )

    assert response.status_code == 200
    resolved = (tmp_path / "reports" / "october.md").resolve()
    assert response.json()["pinned_file_path"] == resolved.as_posix()


def test_pin_patch_does_not_fuzzy_match(tmp_path: Path) -> None:
    """Fuzzy resolution would store a path the caller never named.

    The viewer's fuzzy fallback answers a bare filename with the best
    same-stem match anywhere under the roots. Fine for a link a model emitted,
    wrong here: a pin is an identity key, so a wrong-but-plausible file would
    be pinned and later dismissed under a path the user never chose.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "october.md").write_text("o", encoding="utf-8")
    client = _make_client(manager)

    response = client.patch(
        f"/api/chats/{chat.chat_id}", json=_pin_body("december.md", 0)
    )

    assert response.status_code == 404
    assert manager.get_chat(chat.chat_id).pinned_file_path == ""


def test_deleted_file_can_be_unpinned(tmp_path: Path) -> None:
    """Closing a pin must not need the file to still exist.

    The file is exactly the thing likely to have been deleted when the user
    wants the panel gone. A close path that touched the filesystem would be
    unusable in that case — the one moment it is needed.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    target = tmp_path / "doomed.md"
    target.write_text("d", encoding="utf-8")
    client = _make_client(manager)
    client.patch(f"/api/chats/{chat.chat_id}", json=_pin_body(target.as_posix(), 0))

    target.unlink()
    response = client.patch(f"/api/chats/{chat.chat_id}", json=_pin_body("", 1))

    assert response.status_code == 200
    assert response.json()["pinned_file_path"] == ""
    assert response.json()["dismissed_pin_paths"] == [target.as_posix()]


@pytest.mark.parametrize(
    ("body", "expected_status"),
    [
        pytest.param({"pin": "note.md"}, 400, id="pin-not-an-object"),
        pytest.param({"pin": {}}, 400, id="missing-path"),
        pytest.param({"pin": {"expected_revision": 0}}, 400, id="missing-path-key"),
        pytest.param(_pin_body("", None), 400, id="missing-revision"),
        pytest.param(_pin_body("note.md", True), 400, id="bool-revision"),
        pytest.param(_pin_body("note.md", -1), 400, id="negative-revision"),
        pytest.param(_pin_body("note.md", 1.0), 400, id="float-revision"),
        pytest.param(_pin_body("note.md", "1"), 400, id="string-revision"),
        pytest.param(_pin_body(7, 0), 400, id="non-string-path"),
        pytest.param(_pin_body("bad\x00path.md", 0), 400, id="nul-in-path"),
        pytest.param(
            {"pin": {"path": "note.md", "expected_revision": 0, "extra": 1}},
            400,
            id="extra-pin-key",
        ),
        pytest.param(
            {"pin": {"path": "note.md", "expected_revision": 0}, "title": "New"},
            400,
            id="combined-with-title",
        ),
        pytest.param(
            {"pin": {"path": "note.md", "expected_revision": 0}, "model": "opus"},
            400,
            id="combined-with-model",
        ),
        pytest.param(
            {"pin": {"path": "note.md", "expected_revision": 0}, "mode": "plan"},
            400,
            id="combined-with-mode",
        ),
    ],
)
def test_pin_patch_rejects_malformed_bodies(
    tmp_path: Path, body: dict, expected_status: int
) -> None:
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    (tmp_path / "note.md").write_text("n", encoding="utf-8")
    client = _make_client(manager)

    response = client.patch(f"/api/chats/{chat.chat_id}", json=body)

    assert response.status_code == expected_status
    # Nothing was applied, including for the combined-update cases: a pin
    # conflict must not leave the title it came with applied.
    chat_again = manager.get_chat(chat.chat_id)
    assert chat_again.pinned_file_path == ""
    assert chat_again.pin_revision == 0
    assert chat_again.title == "Pinned"


def test_pin_patch_rejects_missing_chat_before_touching_the_filesystem(
    tmp_path: Path,
) -> None:
    """An unknown chat is a 404, not "that file does not exist".

    Otherwise a probe can distinguish "no such chat" from "no such file" and the
    route leaks chat existence and file existence through one error code. The
    path below does not exist, so a filesystem-first order would answer 404 with
    a different body.
    """
    manager = _make_manager(tmp_path)
    client = _make_client(manager)

    response = client.patch(
        "/api/chats/chat-missing", json=_pin_body("no/such/file.md", 0)
    )

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_pin_patch_rejects_nonexistent_file_and_directory(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    (tmp_path / "reports").mkdir()
    client = _make_client(manager)

    missing = client.patch(
        f"/api/chats/{chat.chat_id}", json=_pin_body("reports/nope.md", 0)
    )
    assert missing.status_code == 404

    # A directory is not a file the panel can render. The resolver answers a
    # directory with the same 404 it gives a missing file.
    directory = client.patch(
        f"/api/chats/{chat.chat_id}", json=_pin_body("reports", 0)
    )
    assert directory.status_code == 404
    assert manager.get_chat(chat.chat_id).pinned_file_path == ""


def test_pin_patch_rejects_a_type_the_viewer_would_not_serve(tmp_path: Path) -> None:
    """Pins carry the viewer's existing permissions, not wider ones.

    A `.env` file is under the workspace root and exists, but the text viewer
    deliberately refuses it. Pinning one would hand the panel a file the viewer
    route will 415 anyway, and would widen what the panel can be pointed at.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    client = _make_client(manager)

    response = client.patch(
        f"/api/chats/{chat.chat_id}", json=_pin_body(".env", 0)
    )

    assert response.status_code == 415
    assert manager.get_chat(chat.chat_id).pinned_file_path == ""


def test_stale_pin_patch_returns_409_with_current_state(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    first = tmp_path / "first.md"
    first.write_text("1", encoding="utf-8")
    client = _make_client(manager)
    client.patch(f"/api/chats/{chat.chat_id}", json=_pin_body(first.as_posix(), 0))

    # This device still quotes revision 0.
    response = client.patch(
        f"/api/chats/{chat.chat_id}", json=_pin_body(first.as_posix(), 0)
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"] == "pin_revision_conflict"
    assert payload["pin"] == {
        "path": first.as_posix(),
        "dismissed_paths": [],
        "revision": 1,
    }
    assert manager.get_chat(chat.chat_id).pin_revision == 1


def test_pin_patch_save_failure_does_not_return_200(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    target = tmp_path / "note.md"
    target.write_text("n", encoding="utf-8")
    client = _make_client(manager)

    def fail_save(*args, **kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_save", fail_save)
    response = client.patch(
        f"/api/chats/{chat.chat_id}", json=_pin_body(target.as_posix(), 0)
    )

    assert response.status_code == 500
    assert manager.get_chat(chat.chat_id).pinned_file_path == ""


def test_ordinary_patch_behavior_is_unchanged(tmp_path: Path) -> None:
    """The pre-existing PATCH contract still answers exactly as it did.

    A pin key is a dedicated body shape, not a new optional argument on
    `update_chat`, so the ordinary path keeps its own semantics: unknown fields
    ignored, `None` meaning "leave alone", and a rejected value still a 400.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    client = _make_client(manager)

    renamed = client.patch(f"/api/chats/{chat.chat_id}", json={"title": "Renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Renamed"
    # Ignored, as before, and in particular not read as a pin.
    ignored = client.patch(f"/api/chats/{chat.chat_id}", json={"nope": 1})
    assert ignored.status_code == 200
    assert ignored.json()["title"] == "Renamed"
    assert ignored.json()["pinned_file_path"] == ""
    invalid = client.patch(f"/api/chats/{chat.chat_id}", json={"mode": "bogus"})
    assert invalid.status_code == 400
    missing = client.patch("/api/chats/chat-missing", json={"title": "x"})
    assert missing.status_code == 404
    assert missing.json() == {"error": "not found"}


def _control_plane(manager: ProjectChatManager) -> CiaoControlPlane:
    """A real control plane over the real manager.

    The same `CiaoConfig` the manager was built with, so `agent_root()` resolves
    the way it does in production rather than to a hand-made stub root.
    """
    return CiaoControlPlane(
        manager._config,
        project_chat_manager=manager,
        schedule_manager=None,
    )


def _principal(chat_id: str) -> McpPrincipal:
    return McpPrincipal(
        token_id="t",
        chat_id=chat_id,
        project_id="p",
        workspace="personal",
        provider="opencode",
    )


def test_surface_without_viewers_persists_and_honors_path_dismissal(
    tmp_path: Path,
) -> None:
    """An agent surface is durable and works with nobody watching.

    The panel may be opened minutes later or from another device, so a surface
    with zero viewers has to be recorded anyway — that is the case a
    presence-gated implementation loses. And once the user closes that exact
    path, surfacing it again must leave the panel closed.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")
    plane = _control_plane(manager)

    result = plane.file_surface(_principal(chat.chat_id), "first.md")

    assert result["ok"] is True
    # No viewer is attached at all, and the pin is still there.
    assert result["data"]["viewers"] == 0
    assert manager.get_chat(chat.chat_id).pinned_file_path == first.resolve().as_posix()
    assert manager.get_chat(chat.chat_id).pin_revision == 1

    # A second manager over the same registry: it survived the restart.
    restored = _restart(manager)
    assert restored.get_chat(chat.chat_id).pinned_file_path == (
        first.resolve().as_posix()
    )

    # A different file surfaces normally.
    plane.file_surface(_principal(chat.chat_id), "second.md")
    assert manager.get_chat(chat.chat_id).pinned_file_path == second.resolve().as_posix()

    # The user closes that exact path; the same surface must not reopen it.
    manager.set_chat_pin(chat.chat_id, "", expected_revision=2)
    plane.file_surface(_principal(chat.chat_id), "second.md")
    chat_again = manager.get_chat(chat.chat_id)
    assert chat_again.pinned_file_path == ""
    assert chat_again.dismissed_pin_paths == [second.resolve().as_posix()]

    # The other file is still not dismissed, so it still surfaces.
    plane.file_surface(_principal(chat.chat_id), "first.md")
    assert manager.get_chat(chat.chat_id).pinned_file_path == first.resolve().as_posix()


def test_non_chat_principal_mutates_no_pin(tmp_path: Path) -> None:
    """An unscoped principal validates and reports, and pins nothing.

    It has no chat to attribute the intent to, so choosing one on its behalf
    would put a pin on a conversation the model is not even in.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    target = tmp_path / "note.md"
    target.write_text("n", encoding="utf-8")
    plane = _control_plane(manager)

    result = plane.file_surface(_principal(""), "note.md")

    assert result["ok"] is True
    assert result["data"]["path"] == "note.md"
    chat_again = manager.get_chat(chat.chat_id)
    assert chat_again.pinned_file_path == ""
    assert chat_again.pin_revision == 0


def test_surface_of_a_missing_file_pins_nothing(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    plane = _control_plane(manager)

    with pytest.raises(ControlPlaneError):
        plane.file_surface(_principal(chat.chat_id), "nope.md")

    chat_again = manager.get_chat(chat.chat_id)
    assert chat_again.pinned_file_path == ""
    assert chat_again.pin_revision == 0


def test_ordinary_file_touch_does_not_pin(tmp_path: Path) -> None:
    """Writing a file is not intent to show it.

    Pin is an explicit act — a user click, or an agent's `file_surface`. If an
    ordinary Write/Edit implied it, every edited file would hijack the panel.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    target = tmp_path / "touched.md"
    target.write_text("t", encoding="utf-8")
    manager.update_chat(chat.chat_id, title="Renamed")
    manager.mark_read(chat.chat_id)

    chat_again = manager.get_chat(chat.chat_id)
    assert chat_again.pinned_file_path == ""
    assert chat_again.pin_revision == 0
    assert manager.chat_pin_states[chat.chat_id] == {
        "path": "",
        "dismissed_paths": [],
        "revision": 0,
    }


def test_pin_does_not_change_read_activity_or_turn_state(tmp_path: Path) -> None:
    """Pins live beside the lifecycle fields, not inside them.

    Unread state, activity stamps and turn bookkeeping answer "is this chat
    waiting for me"; a pin answers "what is in the panel beside it". Touching
    one while writing the other would make opening a file mark a chat read, or
    bump a turn counter, on every other device.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    manager._chats[chat.chat_id].last_activity_at = "2026-01-02T03:04:05+00:00"
    manager._chats[chat.chat_id].user_turn_count = 3
    manager.mark_unread(chat.chat_id)
    before = manager.get_chat(chat.chat_id)
    snapshot = (
        before.last_read_at,
        before.last_activity_at,
        before.last_snippet,
        before.user_turn_count,
        before.user_turn_images,
        before.user_turn_timings,
        before.session_id,
        before.title,
    )

    manager.set_chat_pin(chat.chat_id, "/a.md", expected_revision=0)
    manager.surface_chat_file(chat.chat_id, "/b.md")
    manager.set_chat_pin(chat.chat_id, "", expected_revision=2)

    after = manager.get_chat(chat.chat_id)
    assert (
        after.last_read_at,
        after.last_activity_at,
        after.last_snippet,
        after.user_turn_count,
        after.user_turn_images,
        after.user_turn_timings,
        after.session_id,
        after.title,
    ) == snapshot
    # The activity stamp is still strictly later than the read stamp, so the
    # chat is still unread: pinning did not read it.
    assert after.last_activity_at > after.last_read_at


def test_archive_keeps_the_pin_and_a_fork_starts_without_one(tmp_path: Path) -> None:
    """Archiving is not a close, and a fork is not a copy of the panel.

    Archiving keeps the pin so reopening the archived chat shows what it showed
    before. A fork is a new conversation about the same history, so it starts
    unpinned — inheriting the source's pin would surface a file the new
    conversation never asked for.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    target = tmp_path / "file.md"
    target.write_text("x", encoding="utf-8")
    manager.set_chat_pin(chat.chat_id, target.as_posix(), expected_revision=0)

    assert manager.chat_pin_states[chat.chat_id]["path"] == target.as_posix()

    fork = manager.fork_chat(
        chat.chat_id,
        messages=[
            {"role": "user", "content": "Question", "turn_index": 0},
            {"role": "assistant", "content": "Answer"},
        ],
        turn_index=0,
    )
    fork_again = manager.get_chat(fork.chat_id)
    assert fork_again.pinned_file_path == ""
    assert fork_again.dismissed_pin_paths == []
    assert fork_again.pin_revision == 0

    new_chat = manager.create_chat(chat.project_id, title="Fresh")
    assert manager.get_chat(new_chat.chat_id).pinned_file_path == ""


def test_chat_pin_states_lists_every_persisted_chat(tmp_path: Path) -> None:
    """The map includes archived chats, because a reconnect still shows them.

    A client converging on the snapshot closes a stale pin for whatever the map
    mentions. A chat missing from it can only be left showing stale state, so
    "unpinned" and "archived" must both be present with an empty path.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    target = tmp_path / "file.md"
    target.write_text("x", encoding="utf-8")
    manager.set_chat_pin(chat.chat_id, target.as_posix(), expected_revision=0)
    manager.set_chat_pin(chat.chat_id, "", expected_revision=1)
    manager._chats[chat.chat_id].archived = True
    manager._save()
    restored = _restart(manager)

    states = restored.chat_pin_states
    assert chat.chat_id in states
    assert states[chat.chat_id] == {
        "path": "",
        "dismissed_paths": [target.as_posix()],
        "revision": 2,
    }
    for other in restored.list_chats():
        assert other.chat_id in states
