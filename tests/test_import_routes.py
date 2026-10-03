"""The import consent routes, against synthetic sessions and the signed cookie.

The two routes are ``GET /api/import/sources`` and ``POST /api/import/preview``.
What is pinned here is the surface, not the discovery rules (those are
``tests/test_import_discover.py``): the session boundary, the workspace check,
and the two promises the browser is given — that a listing carries no transcript
text, and that a preview covers the **selected** refs only and returns no
transcript text either.

Every session here is synthetic, and the OpenCode adapter's bounded calls are
replaced, so nothing reads a real ``~/.claude``/``~/.opencode`` and nothing
executes a CLI.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.agent_paths import claude_projects_dir
from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.import_discover import BATCH_CAP, MAX_SELECTION
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_import import import_preview, import_sources

#: The version the enforced V2 floor accepts. Never run: the adapter's own
#: version check and JSON command are replaced below.
SUPPORTED_VERSION = "opencode v2.0.22"

SYNTHETIC_BINARY = "/nonexistent/opencode"

#: Text that must never appear in either answer. It is the whole point of the
#: two assertions that use it: a consent screen describes conversations, it does
#: not carry them.
FIRST_TURN = "NEVER-SURFACED first user turn"
REPLY = "NEVER-SURFACED assistant reply"


def _entry(uuid: str, parent: str | None, kind: str, text: str) -> str:
    return json.dumps(
        {
            "type": kind,
            "uuid": uuid,
            "parentUuid": parent,
            "sessionId": "synthetic-session",
            "message": {"role": kind, "content": text},
        }
    )


def _session_body() -> str:
    return "\n".join(
        (_entry("u1", None, "user", FIRST_TURN), _entry("a1", "u1", "assistant", REPLY))
    )


def _fake_opencode(monkeypatch: pytest.MonkeyPatch, *, rows: Any = None) -> list[tuple[str, ...]]:
    """Replace the OpenCode adapter's bounded calls; return each JSON command's argv.

    Nothing is executed: the binary resolution, the version check and the JSON
    command are all replaced, so no server is started and no session database is
    reachable. A test can therefore assert both what was asked for (``session
    list`` versus ``session export``) and what came back.
    """
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "ciao.import_sources.opencode._resolve_binary", lambda binary: SYNTHETIC_BINARY
    )
    monkeypatch.setattr(
        "ciao.import_sources.opencode._require_supported_v2",
        lambda binary, *, timeout: SUPPORTED_VERSION,
    )

    def _run(binary: str, args: Sequence[str], timeout: float, **_: object) -> Any:
        calls.append(tuple(args))
        return rows

    monkeypatch.setattr("ciao.import_sources.opencode._run_opencode_json", _run)
    return calls


def _world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sessions: Sequence[str] = ()
) -> tuple[TestClient, dict[str, str], CiaoConfig]:
    """A session-guarded app over a real config with `personal` and `work`.

    ``personal`` holds the synthetic sessions named in *sessions*; ``work`` holds
    one, so a listing answered for the wrong workspace is visible.
    """
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
    for name, ids in (
        ("personal", sessions),
        # This install has no re-rooting receipt, so both workspaces resolve to
        # the install root as their agent root and therefore to ONE Claude Code
        # slug directory. That is the real behaviour discovery inherits
        # (`agent_root_for` is the seam), so the listing shows both workspaces'
        # sessions rather than pretending the directory is narrower than it is.
        ("work", ("work-session",)),
    ):
        directory = claude_projects_dir(config.agent_root(name))
        directory.mkdir(parents=True, exist_ok=True)
        for session_id in ids:
            (directory / f"{session_id}.jsonl").write_text(_session_body(), encoding="utf-8")
    for name in ("personal", "work"):
        Path(config.workspace_vault_root(name)).mkdir(parents=True, exist_ok=True)

    serializer = URLSafeTimedSerializer("test-token")
    app = Starlette(
        routes=[
            Route("/api/import/sources", import_sources, methods=["GET"]),
            Route("/api/import/preview", import_preview, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    client = TestClient(app)
    cookies = {SESSION_COOKIE: serializer.dumps({"user": "owner"})}
    return client, cookies, config


# ── The session boundary ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/api/import/sources", {"params": {"workspace": "personal"}}),
        (
            "post",
            "/api/import/preview",
            {"json": {"workspace": "personal", "sources": []}},
        ),
    ],
)
def test_both_routes_require_the_signed_session_cookie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    client, _cookies, _config = _world(tmp_path, monkeypatch, ["sess-a"])

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401


def test_the_session_cookie_admits_both_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch, ["sess-a"])

    assert client.get("/api/import/sources?workspace=personal", cookies=cookies).status_code == 200
    preview = client.post(
        "/api/import/preview",
        json={"workspace": "personal", "sources": [{"provider": "claude_code", "source_id": "sess-a"}]},
        cookies=cookies,
    )
    assert preview.status_code == 200, preview.text


# ── 400 for a workspace that is not registered ────────────────────────────


@pytest.mark.parametrize("name", ["", "   ", "nope"])
def test_sources_refuses_an_unregistered_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch, ["sess-a"])

    response = client.get("/api/import/sources", params={"workspace": name}, cookies=cookies)

    assert response.status_code == 400
    assert "unknown workspace" in response.json()["error"]


@pytest.mark.parametrize("name", ["", "   ", "nope"])
def test_preview_refuses_an_unregistered_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch, ["sess-a"])

    response = client.post(
        "/api/import/preview",
        json={"workspace": name, "sources": [{"provider": "claude_code", "source_id": "sess-a"}]},
        cookies=cookies,
    )

    assert response.status_code == 400
    assert "unknown workspace" in response.json()["error"]


def test_a_bad_selection_is_a_400_and_names_what_is_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch, ["sess-a"])

    for body, fragment in (
        ({"workspace": "personal", "sources": "sess-a"}, "sources"),
        ({"workspace": "personal", "sources": [{"source_id": "sess-a"}]}, "provider"),
        (
            {"workspace": "personal", "sources": [{"provider": "claude_code"}]},
            "session id",
        ),
        ({"workspace": "personal", "sources": [{"provider": "nope", "source_id": "a"}]}, "provider"),
        ({"workspace": "personal", "sources": ["sess-a"]}, "object"),
    ):
        response = client.post("/api/import/preview", json=body, cookies=cookies)
        assert response.status_code == 400, body
        assert fragment in response.json()["error"]

    over = client.post(
        "/api/import/preview",
        json={
            "workspace": "personal",
            "sources": [
                {"provider": "claude_code", "source_id": f"sess-{index}"}
                for index in range(MAX_SELECTION + 1)
            ],
        },
        cookies=cookies,
    )
    assert over.status_code == 400
    assert str(MAX_SELECTION) in over.json()["error"]


def test_a_body_that_is_not_an_object_is_a_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch, ["sess-a"])

    response = client.post("/api/import/preview", content=b"not json", cookies=cookies)

    assert response.status_code == 400


# ── The two promises the browser is given ─────────────────────────────────


def test_the_sources_answer_carries_no_transcript_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch, ["sess-a"])
    _fake_opencode(monkeypatch, rows=[{"id": "ses_open", "directory": str(tmp_path)}])

    response = client.get("/api/import/sources?workspace=personal", cookies=cookies)

    assert response.status_code == 200
    assert FIRST_TURN not in response.text
    assert REPLY not in response.text
    sources = response.json()["sources"]
    assert [ref["source_id"] for ref in sources["available"]] == [
        "sess-a",
        "work-session",
        "ses_open",
    ]
    assert set(sources) == {"available", "excluded", "unsupported", "truncated"}


def test_the_preview_lists_only_the_selected_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch, ["sess-a", "sess-b"])
    _fake_opencode(monkeypatch, rows=[])

    response = client.post(
        "/api/import/preview",
        json={
            "workspace": "personal",
            "sources": [{"provider": "claude_code", "source_id": "sess-b"}],
        },
        cookies=cookies,
    )

    assert response.status_code == 200, response.text
    preview = response.json()["preview"]
    # One row for the one selected ref: a preview that described the whole
    # listing would put conversations on screen the user did not choose.
    assert [row["source"]["source_id"] for row in preview["conversations"]] == ["sess-b"]
    assert "sess-a" not in response.text


def test_the_preview_returns_counts_and_the_confirmation_and_no_transcript_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies, config = _world(tmp_path, monkeypatch, ["sess-a"])
    _fake_opencode(monkeypatch, rows=[])

    response = client.post(
        "/api/import/preview",
        json={
            "workspace": "personal",
            "sources": [{"provider": "claude_code", "source_id": "sess-a"}],
        },
        cookies=cookies,
    )

    assert response.status_code == 200
    assert FIRST_TURN not in response.text
    assert REPLY not in response.text
    preview = response.json()["preview"]
    row = preview["conversations"][0]
    assert row["state"] == "ready"
    assert row["classification"] == "external"
    assert row["message_count"] == 2
    assert row["estimated_chars"] == len(FIRST_TURN) + len(REPLY)
    assert row["already_imported"] is False
    # Everything a person must be told before the first model call.
    assert preview["provider"] == config.default_provider_for_workspace("personal")
    assert preview["model"] == config.default_model_for_workspace(
        "personal", preview["provider"]
    )
    assert preview["estimated_messages"] == 2
    assert preview["batch_cap"] == BATCH_CAP
    assert Path(preview["destination"]) == config.workspace_vault_root("personal")
    assert preview["workspace"] == "personal"


def test_a_selection_names_a_source_and_never_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch, ["sess-a"])
    secret = tmp_path / "outside.jsonl"
    secret.write_text(_session_body(), encoding="utf-8")
    _fake_opencode(monkeypatch, rows=[])

    response = client.post(
        "/api/import/preview",
        json={
            "workspace": "personal",
            "sources": [
                # A caller naming a path, and a traversal through the id: both
                # are ids here, so both are resolved against the workspace's own
                # slug directory or refused.
                {"provider": "claude_code", "source_id": str(secret), "path": str(secret)},
                {"provider": "claude_code", "source_id": "../../outside"},
            ],
        },
        cookies=cookies,
    )

    assert response.status_code == 200
    rows = {row["source"]["source_id"]: row for row in response.json()["preview"]["conversations"]}
    assert set(rows) == {str(secret), "../../outside"}
    assert all(row["state"] == "unreadable" for row in rows.values())
    assert rows[str(secret)]["message_count"] == 0


def test_an_opencode_id_this_workspace_does_not_list_is_refused_without_an_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch, ["sess-a"])
    # The listing names one session. `opencode session export <id>` resolves ids
    # across every project on the machine, so an id a caller posts is not
    # evidence that the session belongs to this workspace — and the export must
    # not run to find out.
    calls = _fake_opencode(monkeypatch, rows=[{"id": "ses_mine", "directory": str(tmp_path)}])

    response = client.post(
        "/api/import/preview",
        json={
            "workspace": "personal",
            "sources": [{"provider": "opencode", "source_id": "ses_other_project"}],
        },
        cookies=cookies,
    )

    assert response.status_code == 200, response.text
    row = response.json()["preview"]["conversations"][0]
    assert row["state"] == "unreadable"
    assert row["reason"] == "unreadable"
    assert "this workspace" in row["message"]
    assert row["message_count"] == 0
    # Listed, never exported: the check happens before the content is read.
    assert [args[:2] for args in calls] == [("session", "list")]


def test_an_opencode_session_this_workspace_lists_is_still_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch, ["sess-a"])
    calls = _fake_opencode(monkeypatch, rows=[{"id": "ses_mine", "directory": str(tmp_path)}])

    response = client.post(
        "/api/import/preview",
        json={
            "workspace": "personal",
            "sources": [{"provider": "opencode", "source_id": "ses_mine"}],
        },
        cookies=cookies,
    )

    # The export answers this fake with the listing's own shape, which is not a
    # session; the point is only that it was asked for the listed id.
    assert [args[:2] for args in calls] == [("session", "list"), ("session", "export")]
    assert calls[-1][2] == "ses_mine"


def test_the_listing_carries_no_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch, ["sess-a"])
    _fake_opencode(monkeypatch, rows=[])

    response = client.get("/api/import/sources?workspace=personal", cookies=cookies)

    assert response.status_code == 200
    sources = response.json()["sources"]
    refs = sources["available"] + [row["ref"] for row in sources["excluded"]]
    assert refs, "the fixture listed nothing, so this asserts nothing"
    assert all("path" not in ref for ref in refs)
    assert set(refs[0]) == {"provider", "source_id", "project_hint"}
    # Nothing of the user's home directory crosses into the browser.
    assert str(tmp_path) not in response.text


def test_a_source_that_could_not_be_listed_is_a_row_in_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.import_sources.opencode import SourceError

    client, cookies, _config = _world(tmp_path, monkeypatch, ["sess-a"])
    monkeypatch.setattr(
        "ciao.import_sources.opencode._resolve_binary", lambda binary: SYNTHETIC_BINARY
    )
    monkeypatch.setattr(
        "ciao.import_sources.opencode._require_supported_v2",
        lambda binary, *, timeout: "opencode v1.9.4",
    )

    def _run(binary: str, args: Sequence[str], timeout: float, **_: object) -> Any:
        raise SourceError("unsupported_version", "opencode is too old")

    monkeypatch.setattr("ciao.import_sources.opencode._run_opencode_json", _run)

    response = client.get("/api/import/sources?workspace=personal", cookies=cookies)

    assert response.status_code == 200
    sources = response.json()["sources"]
    assert [ref["source_id"] for ref in sources["available"]] == ["sess-a", "work-session"]
    assert [row["reason"] for row in sources["unsupported"]] == ["unsupported_version"]


def test_an_unreadable_ciaobot_registry_is_a_500_not_a_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cookies, config = _world(tmp_path, monkeypatch, ["sess-a"])
    _fake_opencode(monkeypatch, rows=[])
    (Path(config.state_path).parent / "web_projects.json").write_text("{ not json", encoding="utf-8")

    response = client.get("/api/import/sources?workspace=personal", cookies=cookies)

    # A listing built from an exclusion set that could not be read would offer
    # Ciaobot's own sessions as the user's history.
    assert response.status_code == 500
    assert "sources" not in response.json()