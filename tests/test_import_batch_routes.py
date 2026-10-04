"""The import batch routes, against the signed cookie and a real config.

The five routes are ``POST /api/import/batches``,
``GET /api/import/batches?workspace=``, ``POST
/api/import/batches/{batch_id}/run``, ``POST
/api/import/batches/{batch_id}/cancel`` and ``DELETE
/api/import/batches/{batch_id}``. What is pinned here is the surface, not the
store rules (those are ``tests/test_import_store.py``) and not the extraction
(``tests/test_import_run.py``): the session boundary, the workspace check
(unknown is a 400, a batch filed for another workspace is a 404), and the two
promises that matter for ``run`` — that filing a batch calls no provider and no
model at all, and that *running* one starts no turn inside the request either.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.import_discover import BATCH_CAP
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_import import (
    import_batch_cancel,
    import_batch_delete,
    import_batch_run,
    import_batches_create,
    import_batches_list,
)


def _world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, dict[str, str]]:
    """A session-guarded batch app over a real config with `personal` and `work`."""
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", lambda *a, **k: None)
    reset_reroot_cache()
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        pwa_auth_required=True,
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    serializer = URLSafeTimedSerializer("test-token")
    app = Starlette(
        routes=[
            Route("/api/import/batches", import_batches_create, methods=["POST"]),
            Route("/api/import/batches", import_batches_list, methods=["GET"]),
            Route(
                "/api/import/batches/{batch_id}/run",
                import_batch_run,
                methods=["POST"],
            ),
            Route(
                "/api/import/batches/{batch_id}/cancel",
                import_batch_cancel,
                methods=["POST"],
            ),
            Route(
                "/api/import/batches/{batch_id}",
                import_batch_delete,
                methods=["DELETE"],
            ),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    client = TestClient(app)
    cookies = {SESSION_COOKIE: serializer.dumps({"user": "owner"})}
    return client, cookies


def _selection(*source_ids: str) -> dict[str, Any]:
    return {
        "workspace": "personal",
        "sources": [
            {"provider": "claude_code", "source_id": source_id}
            for source_id in source_ids
        ],
    }


def _create(client: TestClient, cookies: dict[str, str], *source_ids: str) -> dict[str, Any]:
    response = client.post("/api/import/batches", json=_selection(*source_ids), cookies=cookies)
    assert response.status_code == 201, response.text
    return response.json()["batch"]


# ── The session boundary ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/api/import/batches", {"params": {"workspace": "personal"}}),
        ("post", "/api/import/batches", {"json": _selection("sess-a")}),
        (
            "post",
            "/api/import/batches/0123456789abcdef0123456789abcdef/run",
            {"json": {"workspace": "personal"}},
        ),
        (
            "post",
            "/api/import/batches/0123456789abcdef0123456789abcdef/cancel",
            {"json": {"workspace": "personal"}},
        ),
        (
            "delete",
            "/api/import/batches/0123456789abcdef0123456789abcdef",
            {"params": {"workspace": "personal"}},
        ),
    ],
)
def test_every_batch_route_requires_the_signed_session_cookie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    client, _cookies = _world(tmp_path, monkeypatch)

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401


def test_the_session_cookie_admits_every_batch_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies = _world(tmp_path, monkeypatch)
    # The runner is stubbed because this is about the session boundary: the real
    # one settles the batch it is handed, and a cancel afterwards would then be
    # a conflict rather than the 200 this asserts.
    _stub_runner(monkeypatch)

    batch = _create(client, cookies, "sess-a")
    listed = client.get("/api/import/batches", params={"workspace": "personal"}, cookies=cookies)
    assert listed.status_code == 200
    assert [row["batch_id"] for row in listed.json()["batches"]] == [batch["batch_id"]]
    run = client.post(
        f"/api/import/batches/{batch['batch_id']}/run",
        json={"workspace": "personal"},
        cookies=cookies,
    )
    assert run.status_code == 202
    cancelled = client.post(
        f"/api/import/batches/{batch['batch_id']}/cancel",
        json={"workspace": "personal"},
        cookies=cookies,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["batch"]["status"] == "cancelled"
    deleted = client.delete(
        f"/api/import/batches/{batch['batch_id']}",
        params={"workspace": "personal"},
        cookies=cookies,
    )
    assert deleted.status_code == 204


# ── 400 for a workspace that is not registered ────────────────────────────


@pytest.mark.parametrize("name", ["", "   ", "nope"])
def test_unknown_workspaces_are_a_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    client, cookies = _world(tmp_path, monkeypatch)
    batch = _create(client, cookies, "sess-a")

    assert (
        client.post(
            "/api/import/batches",
            json={"workspace": name, "sources": [{"provider": "claude_code", "source_id": "s"}]},
            cookies=cookies,
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/import/batches", params={"workspace": name}, cookies=cookies
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/import/batches/{batch['batch_id']}/run",
            json={"workspace": name},
            cookies=cookies,
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/import/batches/{batch['batch_id']}/cancel",
            json={"workspace": name},
            cookies=cookies,
        ).status_code
        == 400
    )
    assert (
        client.delete(
            f"/api/import/batches/{batch['batch_id']}",
            params={"workspace": name},
            cookies=cookies,
        ).status_code
        == 400
    )


def test_a_body_that_is_not_an_object_is_a_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies = _world(tmp_path, monkeypatch)

    assert client.post("/api/import/batches", content=b"not json", cookies=cookies).status_code == 400
    assert (
        client.post(
            "/api/import/batches/0123456789abcdef0123456789abcdef/run",
            content=b"not json",
            cookies=cookies,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/import/batches/0123456789abcdef0123456789abcdef/cancel",
            content=b"not json",
            cookies=cookies,
        ).status_code
        == 400
    )


def test_a_bad_selection_is_a_400_and_names_what_is_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies = _world(tmp_path, monkeypatch)

    for body, fragment in (
        ({"workspace": "personal", "sources": "sess-a"}, "sources"),
        ({"workspace": "personal", "sources": [{"source_id": "sess-a"}]}, "provider"),
        (
            {"workspace": "personal", "sources": [{"provider": "nope", "source_id": "a"}]},
            "provider",
        ),
        ({"workspace": "personal", "sources": []}, "at least one"),
    ):
        response = client.post("/api/import/batches", json=body, cookies=cookies)
        assert response.status_code == 400, body
        assert fragment in response.json()["error"]

    over = client.post(
        "/api/import/batches",
        json={
            "workspace": "personal",
            "sources": [
                {"provider": "claude_code", "source_id": f"sess-{index}"}
                for index in range(BATCH_CAP + 1)
            ],
        },
        cookies=cookies,
    )
    assert over.status_code == 400
    assert str(BATCH_CAP) in over.json()["error"]


def test_a_selection_naming_one_conversation_twice_is_a_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies = _world(tmp_path, monkeypatch)

    response = client.post(
        "/api/import/batches",
        json={
            "workspace": "personal",
            "sources": [
                {"provider": "claude_code", "source_id": "sess-a"},
                {"provider": "claude_code", "source_id": " sess-a "},
            ],
        },
        cookies=cookies,
    )
    assert response.status_code == 400
    assert "sess-a" in response.json()["error"]


# ── a batch filed for another workspace is a 404 ──────────────────────────


def test_a_foreign_batch_is_a_404_and_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies = _world(tmp_path, monkeypatch)
    batch = _create(client, cookies, "sess-a")

    ran = client.post(
        f"/api/import/batches/{batch['batch_id']}/run",
        json={"workspace": "work"},
        cookies=cookies,
    )
    assert ran.status_code == 404

    cancelled = client.post(
        f"/api/import/batches/{batch['batch_id']}/cancel",
        json={"workspace": "work"},
        cookies=cookies,
    )
    assert cancelled.status_code == 404

    deleted = client.delete(
        f"/api/import/batches/{batch['batch_id']}",
        params={"workspace": "work"},
        cookies=cookies,
    )
    assert deleted.status_code == 404

    # Left alone: still there, still queued, still this workspace's.
    listed = client.get(
        "/api/import/batches", params={"workspace": "personal"}, cookies=cookies
    )
    assert [row["batch_id"] for row in listed.json()["batches"]] == [batch["batch_id"]]
    assert listed.json()["batches"][0]["status"] == "queued"


def test_unknown_batch_ids_are_a_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies = _world(tmp_path, monkeypatch)
    missing = "0123456789abcdef0123456789abcdef"

    assert (
        client.post(
            f"/api/import/batches/{missing}/run",
            json={"workspace": "personal"},
            cookies=cookies,
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/import/batches/{missing}/cancel",
            json={"workspace": "personal"},
            cookies=cookies,
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/import/batches/{missing}",
            params={"workspace": "personal"},
            cookies=cookies,
        ).status_code
        == 404
    )


# ── batching performs no provider or model call ───────────────────────────


def test_batching_reads_no_conversation_and_calls_no_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies = _world(tmp_path, monkeypatch)

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a batch route must not reach a provider or a model")

    monkeypatch.setattr("ciao.web.routes_import.discover_sources", _refuse)
    monkeypatch.setattr("ciao.web.routes_import.preview_selected", _refuse)
    monkeypatch.setattr("ciao.import_sources.opencode._run_opencode_json", _refuse)
    monkeypatch.setattr(subprocess, "run", _refuse)

    batch = _create(client, cookies, "sess-a")
    assert batch["status"] == "queued"
    assert batch["sources"][0]["content_digest"] == ""
    listed = client.get(
        "/api/import/batches", params={"workspace": "personal"}, cookies=cookies
    )
    assert listed.status_code == 200
    cancelled = client.post(
        f"/api/import/batches/{batch['batch_id']}/cancel",
        json={"workspace": "personal"},
        cookies=cookies,
    )
    assert cancelled.status_code == 200
    deleted = client.delete(
        f"/api/import/batches/{batch['batch_id']}",
        params={"workspace": "personal"},
        cookies=cookies,
    )
    assert deleted.status_code == 204


# ── one batch at a time, dedupe across attempts ───────────────────────────


def test_a_second_open_batch_is_a_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, cookies = _world(tmp_path, monkeypatch)
    _create(client, cookies, "sess-a")

    response = client.post(
        "/api/import/batches", json=_selection("sess-other"), cookies=cookies
    )

    assert response.status_code == 409


def test_a_cancelled_selection_must_be_forgotten_before_it_re_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies = _world(tmp_path, monkeypatch)
    batch = _create(client, cookies, "sess-a")
    assert (
        client.post(
            f"/api/import/batches/{batch['batch_id']}/cancel",
            json={"workspace": "personal"},
            cookies=cookies,
        ).status_code
        == 200
    )

    # The cancelled batch still covers the conversation.
    rerun = client.post("/api/import/batches", json=_selection("sess-a"), cookies=cookies)
    assert rerun.status_code == 409

    assert (
        client.delete(
            f"/api/import/batches/{batch['batch_id']}",
            params={"workspace": "personal"},
            cookies=cookies,
        ).status_code
        == 204
    )
    again = _create(client, cookies, "sess-a")
    assert again["batch_id"] != batch["batch_id"]


def test_cancel_is_idempotent_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies = _world(tmp_path, monkeypatch)
    batch = _create(client, cookies, "sess-a")

    for _ in range(2):
        response = client.post(
            f"/api/import/batches/{batch['batch_id']}/cancel",
            json={"workspace": "personal"},
            cookies=cookies,
        )
        assert response.status_code == 200
        assert response.json()["batch"]["status"] == "cancelled"


# ── Ciaobot's own sessions are never filed ─────────────────────────────────


def test_a_ciaobot_own_session_is_never_filed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    client, cookies = _world(tmp_path, monkeypatch)
    registry = tmp_path / ".runtime" / "web_projects.json"
    registry.write_text(
        _json.dumps(
            {
                "projects": {},
                "chats": {
                    "chat-1": {
                        "project_id": "missing",
                        "session_id": "sess-own",
                        "provider": "claude_code",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    recorded = client.post(
        "/api/import/batches", json=_selection("sess-own"), cookies=cookies
    )
    assert recorded.status_code == 400

    # A bare Ciaobot chat id is refused with no registry answer at all.
    shaped = client.post(
        "/api/import/batches", json=_selection("chat-1234abcd"), cookies=cookies
    )
    assert shaped.status_code == 400


# ── The run route: schedule the extraction, start nothing ──────────────────


def _stub_runner(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace ``run_import_batch`` with one that records the batch and returns.

    The route holds its own import of the runner inside the scheduled coroutine,
    so patching the name on the module the route reaches through is what makes
    the scheduling observable without a model.
    """
    started: list[str] = []

    async def fake_run(config: Any, batch_id: str, **kwargs: Any) -> Any:
        started.append(batch_id)
        return SimpleNamespace(status="done", proposals_filed=0)

    monkeypatch.setattr("ciao.import_run.run_import_batch", fake_run)
    return started


def test_run_answers_202_and_starts_no_turn_in_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response is the batch; the extraction is scheduled, not performed.

    ``run_oneshot`` is poisoned and the whole provider surface with it, so a
    model call inside the request would be a failure rather than an absence. The
    runner is also stubbed, because what is under test here is the scheduling,
    not the extraction (``tests/test_import_run.py`` owns that).
    """
    client, cookies = _world(tmp_path, monkeypatch)
    batch = _create(client, cookies, "sess-a")
    started = _stub_runner(monkeypatch)

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the run route must not reach a provider or a model")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", _refuse)
    monkeypatch.setattr("ciao.import_sources.opencode._run_opencode_json", _refuse)
    monkeypatch.setattr(subprocess, "run", _refuse)

    with client:
        response = client.post(
            f"/api/import/batches/{batch['batch_id']}/run",
            json={"workspace": "personal"},
            cookies=cookies,
        )

    assert response.status_code == 202
    assert response.json()["batch"]["status"] == "running"
    assert started == [batch["batch_id"]], "the runner was scheduled, once"


def test_run_never_starts_a_second_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second press answers the current state; a settled batch is a 409.

    One batch at a time is C6's rule, and the race two presses of the button
    would otherwise lose is exactly what the first branch here prevents.
    """
    client, cookies = _world(tmp_path, monkeypatch)
    batch = _create(client, cookies, "sess-a")
    started = _stub_runner(monkeypatch)

    def _run() -> Any:
        return client.post(
            f"/api/import/batches/{batch['batch_id']}/run",
            json={"workspace": "personal"},
            cookies=cookies,
        )

    with client:
        first = _run()
        assert first.status_code == 202
        # The scheduled task has not run yet, so the store still says `running`.
        second = _run()

    assert second.status_code == 200
    assert second.json()["batch"]["status"] == "running"

    with client:
        cancelled = client.post(
            f"/api/import/batches/{batch['batch_id']}/cancel",
            json={"workspace": "personal"},
            cookies=cookies,
        )
    assert cancelled.status_code == 200
    with client:
        settled = _run()
    assert settled.status_code == 409
    assert len(started) == 1, "the settled press started nothing either"


def test_a_run_of_a_done_batch_is_a_conflict_not_a_second_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch that already settled keeps its outcome; re-running is a new batch."""
    client, cookies = _world(tmp_path, monkeypatch)
    batch = _create(client, cookies, "sess-a")
    started = _stub_runner(monkeypatch)

    with client:
        assert (
            client.post(
                f"/api/import/batches/{batch['batch_id']}/run",
                json={"workspace": "personal"},
                cookies=cookies,
            ).status_code
            == 202
        )
        cancelled = client.post(
            f"/api/import/batches/{batch['batch_id']}/cancel",
            json={"workspace": "personal"},
            cookies=cookies,
        )
    assert cancelled.status_code == 200

    with client:
        again = client.post(
            f"/api/import/batches/{batch['batch_id']}/run",
            json={"workspace": "personal"},
            cookies=cookies,
        )
    assert again.status_code == 409
    assert "cancelled" in again.json()["error"]
    assert len(started) == 1


def test_the_route_and_the_runner_share_one_begin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request takes the single-run gate; the runner must not take it again.

    The route moves the batch to ``running`` so it can answer a second press
    instead of scheduling one, and hands that begun batch to the runner. If the
    runner claimed it a second time it would collide with its own gate and the
    extraction would never happen — so this test runs the **real** runner behind
    the **real** route, with only the conversation read and the turn stubbed, and
    asserts the batch actually settled with its proposal filed.
    """
    client, cookies = _world(tmp_path, monkeypatch)
    filed: list[str] = []
    batch = client.post(
        "/api/import/batches",
        json={
            "workspace": "personal",
            "sources": [{"provider": "opencode", "source_id": "sess-a"}],
        },
        cookies=cookies,
    ).json()["batch"]

    from ciao.import_sources.contract import (
        ROLE_USER,
        NormalizedMessage,
        NormalizedSession,
        SourceRef,
    )

    session = NormalizedSession(
        source=SourceRef(provider="opencode", source_id="sess-a"),
        messages=(
            NormalizedMessage(
                role=ROLE_USER,
                text="When do we deploy?",
                anchor="msg_0001",
                timestamp="2025-02-07T10:00:00Z",
            ),
        ),
    )
    monkeypatch.setattr("ciao.import_run.read_opencode_session", lambda _sid: session)
    monkeypatch.setattr(
        "ciao.import_run._opencode_membership",
        lambda selected, _root: frozenset(ref.source_id for ref in selected),
    )

    async def fake_run_oneshot(_prompt: str, **_kwargs: object) -> str:
        filed.append("called")
        return json.dumps(
            [
                {
                    "text": "Deploys happen on Fridays.",
                    "destination": "memory",
                    "payload": "",
                    "source_anchor": "msg_0001",
                    "as_of": "2026-01-01",
                }
            ]
        )

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_run_oneshot)

    from ciao.memory_proposals import list_proposals

    settled: dict[str, Any] = {}
    with client:
        assert (
            client.post(
                f"/api/import/batches/{batch['batch_id']}/run",
                json={"workspace": "personal"},
                cookies=cookies,
            ).status_code
            == 202
        )
        # Poll through the app itself: every request yields to the event loop the
        # runner is scheduled on, so the extraction actually progresses. A sleep
        # here would block the very loop it is waiting for.
        for _ in range(400):
            listed = client.get(
                "/api/import/batches",
                params={"workspace": "personal"},
                cookies=cookies,
            ).json()["batches"]
            settled = next(row for row in listed if row["batch_id"] == batch["batch_id"])
            if settled["status"] != "running":
                break

    config = client.app.state.config
    assert settled["status"] == "done", settled
    assert settled["progress"]["proposals_filed"] == 1
    assert filed == ["called"], "the real runner ran the real turn"
    rows = list_proposals(
        config.workspace_vault_root("personal") / "Workspace/Memory-Proposals.md"
    )
    assert [row["text"] for row in rows] == [
        "Deploys happen on Fridays. [as-of: 2025-02-07]"
    ], "the source message's date, never the model's or the import's"
    assert [row["source"] for row in rows] == ["opencode:sess-a:msg_0001"]


def test_a_run_body_cannot_choose_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configuration decides what reads a user's history, not the body.

    The field is not part of the contract, so it is ignored rather than
    refused — an ignored unknown key is what a client that guessed gets, and a
    refusal would be a claim the route does not need to make.
    """
    client, cookies = _world(tmp_path, monkeypatch)
    batch = _create(client, cookies, "sess-a")
    _stub_runner(monkeypatch)

    with client:
        response = client.post(
            f"/api/import/batches/{batch['batch_id']}/run",
            json={"workspace": "personal", "model": "someone-elses-model"},
            cookies=cookies,
        )

    assert response.status_code == 202
    assert response.json()["batch"]["batch_id"] == batch["batch_id"]
