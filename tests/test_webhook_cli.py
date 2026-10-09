"""The `ciao webhook` noun: the agent's management surface over a trigger store.

Covers #1039 (child A6 of #974) through the real dispatcher — ``POST
/agent/v1/{op}`` with a bearer token — over the real store in a tmp runtime, so
what is pinned here is what the code under test had to produce rather than what
a mock was told to return.

The properties that matter, in the order they matter:

* **The one-time secret comes back to the caller that asked for it** and
  nowhere else: not in a list, not in an update, not in a ``repr``, not in the
  stored file, and not in anything this surface logs;
* **a trigger is created disabled** and cannot authenticate until it is enabled
  at its revision, so configuring one is not handing a stranger a credential;
* **rotation revokes on rotate** — the old secret stops working at once, which
  is the whole recovery path for an exposed one;
* **one workspace cannot reach another's trigger**: an id from elsewhere is
  ``webhook_not_found``, the same answer a wrong id gets, so it is not an
  existence oracle;
* every write is revision-checked and a stale one is a retryable refusal with
  nothing written.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import pytest

from ciao import agent_cli, mcp_server
from ciao.web.routes_agent import agent_dispatch_endpoint
from ciao.webhooks import WebhookStore
from tests.test_mcp_server import _service

_INSTRUCTIONS = "File the failure as an intake note and tell nobody"


class _Pcm:
    """Only what the webhook path reads: this chat's mode, and the roots."""

    def __init__(self, mode: str = "auto") -> None:
        self.chat = SimpleNamespace(mode=mode)

    def _workspace_vault_root(self, workspace: str) -> Path:
        return workspace

    def get_chat(self, _chat_id: str):
        return self.chat

    def get_active_stream(self, _chat_id: str):
        return None

    def get_project(self, _project_id: str):
        return None

    def list_projects(self, _workspace: str | None = None):
        return []


def _service_over(tmp_path: Path, *, mode: str = "auto"):
    """A real control plane over two sibling workspaces, plus a dispatcher."""
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.control_plane import CiaoControlPlane

    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            ),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    service, _fake = _service(tmp_path)
    service.bind(
        CiaoControlPlane(
            config, project_chat_manager=_Pcm(mode), schedule_manager=SimpleNamespace()
        )
    )
    return service


def _client(service) -> TestClient:
    app = Starlette(
        routes=[Route("/agent/v1/{op}", agent_dispatch_endpoint, methods=["POST"])]
    )
    app.state.mcp_service = service
    return TestClient(
        app, base_url="http://127.0.0.1:18443", client=("127.0.0.1", 5555)
    )


def _token(service, chat_id: str = "chat-1", workspace: str = "personal") -> str:
    token, _ = service.registry.issue(
        chat_id=chat_id, project_id="project-1", workspace=workspace, provider="claude"
    )
    return token


def _post(client: TestClient, token: str, op: str, arguments: dict | None = None):
    return client.post(
        f"/agent/v1/{op}",
        headers={"Authorization": f"Bearer {token}"},
        json=arguments if arguments is not None else {},
    )


def _store(tmp_path: Path) -> WebhookStore:
    return WebhookStore(tmp_path / ".runtime" / "webhooks.json")


def _create(client: TestClient, token: str, **overrides) -> tuple[dict, str]:
    arguments = {"name": "Nightly build", "instructions": _INSTRUCTIONS}
    arguments.update(overrides)
    response = _post(client, token, "webhook_create", arguments)
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    return payload["trigger"], payload["secret"]


# ── The noun's request and response shapes ─────────────────────────────────


def test_create_returns_a_disabled_trigger_and_the_secret_once(tmp_path: Path) -> None:
    service = _service_over(tmp_path)
    token = _token(service)
    with _client(service) as client:
        trigger, secret = _create(client, token, project_id="proj-alpha")

        # Disabled by construction, at revision 1 — and the store's own public
        # record, with nothing extra on it and no permission mode of its own.
        assert trigger["enabled"] is False
        assert trigger["revision"] == 1
        assert trigger["project_id"] == "proj-alpha"
        assert "mode" not in trigger
        assert trigger["workspace"] == "personal"
        assert secret

        listed = _post(client, token, "webhook_list")
        assert listed.status_code == 200
        assert listed.json()["data"] == [trigger]
        assert secret not in listed.text

        # The envelope is the one every operation returns: ok plus data.
        assert json.loads(listed.text).keys() == {"ok", "data"}


def test_the_one_time_secret_is_not_stored_in_the_clear_or_logged(
    tmp_path: Path, caplog
) -> None:
    """Only a hash of the secret exists anywhere after the create response.

    The agent *is* the operator acting through a shell, so the value belongs in
    the envelope — but it is the only copy there will ever be, so what matters
    is that nothing else keeps one: not the store file, not the telemetry, not a
    log line, not the record's own repr.
    """
    service = _service_over(tmp_path)
    token = _token(service)
    with caplog.at_level("DEBUG"):
        with _client(service) as client:
            trigger, secret = _create(client, token)

    stored = (tmp_path / ".runtime" / "webhooks.json").read_text(encoding="utf-8")
    assert secret not in stored
    assert "secret_sha256" in stored
    assert secret not in caplog.text
    telemetry = service._telemetry_path.read_text(encoding="utf-8")
    assert secret not in telemetry
    # The public record has no field that could carry one.
    assert secret not in repr(trigger)
    assert secret not in json.dumps(trigger)


def test_update_is_revision_checked_and_only_touches_the_fields_passed(
    tmp_path: Path,
) -> None:
    service = _service_over(tmp_path)
    token = _token(service)
    with _client(service) as client:
        trigger, _secret = _create(client, token)

        stale = _post(
            client,
            token,
            "webhook_update",
            {
                "trigger_id": trigger["trigger_id"],
                "expected_revision": "9",
                "name": "stale rename",
            },
        )
        assert stale.status_code == 422
        assert stale.json()["error"]["code"] == "webhook_revision_conflict"
        assert stale.json()["error"]["retryable"] is True

        renamed = _post(
            client,
            token,
            "webhook_update",
            {
                "trigger_id": trigger["trigger_id"],
                "expected_revision": "1",
                "name": "Nightly build v2",
            },
        )
        assert renamed.status_code == 200, renamed.text
        after = renamed.json()["data"]
        assert after["name"] == "Nightly build v2"
        assert after["revision"] == 2
        # Instructions and the target survive an edit that did not mention them.
        assert after["instructions"] == _INSTRUCTIONS
        assert after["project_id"] is None
        assert after["enabled"] is False


def test_a_nonnumeric_revision_is_the_store_s_refusal(tmp_path: Path) -> None:
    """A revision the store would refuse answers ``webhook_invalid``.

    The CLI and the operation schema both carry a revision as a string, so a
    non-numeric one has to be parsed here — otherwise it leaks a ``ValueError``
    into the generic ``invalid_request`` envelope and reads like a malformed
    request rather than a value this store will not accept.
    """
    service = _service_over(tmp_path)
    token = _token(service)
    with _client(service) as client:
        trigger, _secret = _create(client, token)
        for revision in ("", "one", "1.5"):
            refused = _post(
                client,
                token,
                "webhook_update",
                {
                    "trigger_id": trigger["trigger_id"],
                    "expected_revision": revision,
                    "name": "x",
                },
            )
            assert refused.status_code == 422, revision
            assert refused.json()["error"]["code"] == "webhook_invalid", revision


def test_enabling_is_what_makes_a_trigger_callable(tmp_path: Path) -> None:
    """Created disabled, uncallable, and openable only on purpose."""
    service = _service_over(tmp_path)
    token = _token(service)
    store = _store(tmp_path)
    with _client(service) as client:
        trigger, secret = _create(client, token)
        assert store.authenticate(trigger["trigger_id"], secret) is None

        enabled = _post(
            client,
            token,
            "webhook_update",
            {
                "trigger_id": trigger["trigger_id"],
                "expected_revision": "1",
                "enabled": True,
            },
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["data"]["enabled"] is True
        assert store.authenticate(trigger["trigger_id"], secret) is not None

        stopped = _post(
            client,
            token,
            "webhook_update",
            {
                "trigger_id": trigger["trigger_id"],
                "expected_revision": str(enabled.json()["data"]["revision"]),
                "enabled": False,
            },
        )
        assert stopped.status_code == 200, stopped.text
        assert store.authenticate(trigger["trigger_id"], secret) is None


def test_rotate_returns_a_new_secret_and_kills_the_old_one(tmp_path: Path) -> None:
    service = _service_over(tmp_path)
    token = _token(service)
    store = _store(tmp_path)
    with _client(service) as client:
        trigger, secret = _create(client, token)
        enabled = _post(
            client,
            token,
            "webhook_update",
            {
                "trigger_id": trigger["trigger_id"],
                "expected_revision": "1",
                "enabled": True,
            },
        ).json()["data"]

        rotated = _post(
            client,
            token,
            "webhook_rotate",
            {
                "trigger_id": trigger["trigger_id"],
                "expected_revision": str(enabled["revision"]),
            },
        )
        assert rotated.status_code == 200, rotated.text
        payload = rotated.json()["data"]
        fresh = payload["secret"]
        assert fresh and fresh != secret
        # Rotation keeps `enabled`: a secret is not consent to run anything, and
        # an enabled trigger stays enabled.
        assert payload["trigger"]["enabled"] is True
        assert payload["trigger"]["revision"] == enabled["revision"] + 1

        # Revoke on rotate: the old secret authorizes nothing from now on.
        assert store.authenticate(trigger["trigger_id"], secret) is None
        assert store.authenticate(trigger["trigger_id"], fresh) is not None


def test_delete_removes_the_trigger_and_its_verifier(tmp_path: Path) -> None:
    service = _service_over(tmp_path)
    token = _token(service)
    store = _store(tmp_path)
    with _client(service) as client:
        trigger, secret = _create(client, token)

        deleted = _post(
            client,
            token,
            "webhook_delete",
            {"trigger_id": trigger["trigger_id"], "expected_revision": "1"},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["data"] == {
            "trigger_id": trigger["trigger_id"],
            "deleted": True,
        }
        assert store.authenticate(trigger["trigger_id"], secret) is None
        assert _post(client, token, "webhook_list").json()["data"] == []

        again = _post(
            client,
            token,
            "webhook_delete",
            {"trigger_id": trigger["trigger_id"], "expected_revision": "1"},
        )
        assert again.status_code == 422
        assert again.json()["error"]["code"] == "webhook_not_found"


# ── The scope ─────────────────────────────────────────────────────────────


def test_a_trigger_in_another_workspace_is_not_reachable(tmp_path: Path) -> None:
    """An id from elsewhere reads exactly like one that does not exist.

    The same non-oracle the task surface keeps: if a foreign id were refused
    with its own code, a managed chat could probe another workspace's triggers
    one id at a time.
    """
    service = _service_over(tmp_path)
    personal, work = _token(service), _token(service, "chat-w", workspace="work")
    store = _store(tmp_path)
    with _client(service) as client:
        mine, _secret = _create(client, personal, name="Personal intake")
        # Store a foreign trigger directly, as a route over the same store would,
        # and enable it so its live secret is what the refusals had to leave.
        theirs, their_secret = store.create(
            name="Work intake", workspace="work", instructions=_INSTRUCTIONS
        )
        store.update(theirs.trigger_id, expected_revision=1, enabled=True)
        assert _post(client, personal, "webhook_list").json()["data"] == [mine]
        assert _post(client, work, "webhook_list").json()["data"] == [
            store.get(theirs.trigger_id).to_dict()
        ]

        for op, arguments in (
            ("webhook_update", {"name": "mine now"}),
            ("webhook_rotate", {}),
            ("webhook_delete", {}),
        ):
            response = _post(
                client,
                personal,
                op,
                {
                    "trigger_id": theirs.trigger_id,
                    "expected_revision": "1",
                    **arguments,
                },
            )
            assert response.status_code == 422, op
            assert response.json()["error"]["code"] == "webhook_not_found", op
        # Untouched, secret and all.
        assert store.get(theirs.trigger_id).name == "Work intake"
        assert store.authenticate(theirs.trigger_id, their_secret) is not None


def test_plan_mode_gates_every_webhook_write(tmp_path: Path) -> None:
    """The gate the annotations claim, on the operations it applies to.

    A trigger is a credential the user can revoke, and creating or enabling one
    on their behalf from a read-only chat is not a thing a plan-mode turn gets to
    do — so ``_WRITE`` on these is load-bearing, not decoration.
    """
    service = _service_over(tmp_path, mode="plan")
    token = _token(service)
    with _client(service) as client:
        listed = _post(client, token, "webhook_list")
        created = _post(
            client,
            token,
            "webhook_create",
            {"name": "Not in plan mode", "instructions": _INSTRUCTIONS},
        )
        edited = _post(
            client,
            token,
            "webhook_update",
            {"trigger_id": "a" * 32, "expected_revision": "1", "enabled": True},
        )
    assert listed.status_code == 200
    for response in (created, edited):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "plan_mode_read_only"


# ── The surface itself ────────────────────────────────────────────────────


def test_the_operations_are_declared_with_the_shape_the_cli_sends() -> None:
    """The signatures are the transport's schema; the CLI is their client.

    ``AgentDispatcher`` validates arguments against the operation's FastMCP tool,
    so a parameter renamed here without the CLI changing with it is a command
    that fails with ``invalid_request`` instead of doing its job.
    """
    signatures = {
        name: set(inspect.signature(operation.fn).parameters) - {"service"}
        for name, operation in mcp_server.OPERATIONS_BY_NAME.items()
        if name.startswith("webhook_")
    }
    assert signatures == {
        "webhook_list": set(),
        # ``mode`` is declared only so a caller who still sends it gets a
        # refusal rather than a silent drop; the CLI never sends it.
        "webhook_create": {"name", "instructions", "project_id", "mode"},
        "webhook_update": {
            "trigger_id",
            "expected_revision",
            "name",
            "instructions",
            "enabled",
        },
        "webhook_rotate": {"trigger_id", "expected_revision"},
        "webhook_delete": {"trigger_id", "expected_revision"},
    }


def test_every_webhook_operation_states_the_secret_rules_in_its_own_help() -> None:
    """The two unrecoverable facts are stated where the secret is returned.

    A secret cannot be read back and a rotation kills the previous one, so a
    caller who misses either has to rotate and fix a sender. The operation
    description is the operation's own schema doc — the text a consumer of the
    operation table reads, and the string the dispatcher hands the model as the
    tool description — so the rules belong there and not only in the skill
    document, which is one read away and easy to skip.
    """
    described = {
        name: operation.description
        for name, operation in mcp_server.OPERATIONS_BY_NAME.items()
        if name.startswith("webhook_")
    }
    assert "shown once" in described["webhook_create"]
    assert "disabled" in described["webhook_create"]
    assert "revokes on rotate" in described["webhook_rotate"].lower()
    assert "shown once" in described["webhook_rotate"]


def test_the_noun_is_an_agent_invocation() -> None:
    parser = agent_cli.build_parser()
    assert agent_cli.is_agent_invocation(["webhook", "list"])
    # A noun with no verb is argparse's own usage error, like every other noun.
    with pytest.raises(SystemExit) as excinfo:
        agent_cli.main(["webhook"])
    assert excinfo.value.code == 2
    # Instructions are prose: a refusal must name the flag the caller used.
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli.resolve(
            parser.parse_args(
                ["webhook", "create", "--name", "x", "--instructions-file", "nope.md"]
            )
        )
    assert "--instructions-file" in str(excinfo.value)
