"""The webhook feature as one journey: configure → send → receipt → chat.

Covers #1039 (child A6 of #974) end to end, which is the point of this file:
A1–A5 each pinned a piece of the feature in isolation (the store, the routes,
the receiver, dispatch, the UI), and every one of those tests would still pass if
the pieces did not actually fit together. This one drives the whole path with
nothing mocked but the model:

1. configure a trigger through the agent control plane, and see that a trigger
   that has just been configured cannot authenticate — it is stored disabled;
2. enable it at the revision it was read at;
3. ``POST /hooks/v1/{trigger_id}`` with that trigger's own bearer secret and an
   ``Idempotency-Key`` — the machine surface, no cookie — and get ``202`` plus a
   receipt that is already durable in the journal;
4. dispatch the accepted receipt against a chat manager that records what it
   was asked to do, and see **exactly one** ordinary chat, in the trigger's own
   mode, with ``unattended`` never named;
5. resend the same key with the same body and get the same receipt and no second
   chat, and resend it with a different body and be refused ``409``.

So every claim a surface can make about this feature has to be true at once:
the sender's secret is a real credential for one trigger and nothing else, a
``202`` is a receipt rather than a launch result, one event is one turn, and a
webhook event is not authorization to escalate.
"""

from __future__ import annotations

import json
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
from ciao.control_plane import AgentPrincipal, CiaoControlPlane
from ciao.web.auth import AuthMiddleware
from ciao.web.project_chats import ChatInfo, ProjectInfo
from ciao.web.routes_hooks import webhook_receive
from ciao.webhook_dispatch import EVENT_FENCE_CLOSE, dispatch_receipt
from ciao.webhooks import (
    ACCEPTED,
    LAUNCHED,
    RECEIPTS_NAME,
    WebhookReceiver,
    WebhookStore,
    reset_rate_limits,
)

_EVENT = "the nightly build failed on main"
_INSTRUCTIONS = "File the failure as an intake note and tell nobody"


@pytest.fixture(autouse=True)
def _clear_shared_window():
    """The per-trigger rate window is module-level; each test starts empty."""
    reset_rate_limits()
    yield
    reset_rate_limits()


class _RecordingPcm:
    """The chat-manager surface dispatch uses, recording every call.

    ``start_stream`` takes ``**kwargs`` on purpose: a double declared
    ``(chat_id, prompt, unattended=False)`` would accept ``unattended=True``
    without complaint, and "a webhook event is not permission to escalate" would
    then rest on reading the implementation instead of on what was passed.
    """

    def __init__(self, projects: list[ProjectInfo]) -> None:
        self.projects = projects
        self.created: list[dict[str, Any]] = []
        self.started: list[tuple[str, str, dict[str, Any]]] = []
        self._minted = 0

    def list_projects(self, workspace: str | None = None) -> list[ProjectInfo]:
        if workspace is None:
            return list(self.projects)
        return [p for p in self.projects if p.workspace == workspace]

    def create_chat(
        self,
        project_id: str,
        title: str = "New Chat",
        model: str | None = None,
        mode: str | None = None,
        provider: str | None = None,
    ) -> ChatInfo:
        self._minted += 1
        self.created.append(
            {
                "project_id": project_id,
                "title": title,
                "model": model,
                "mode": mode,
                "provider": provider,
            }
        )
        return ChatInfo(
            chat_id=f"chat-webhook-{self._minted}",
            project_id=project_id,
            title=title,
            model=model or "opus",
            provider=provider or "claude",
            mode=mode,  # type: ignore[arg-type]
        )

    def start_stream(self, chat_id: str, prompt: str, **kwargs: Any) -> object:
        self.started.append((chat_id, prompt, kwargs))
        return object()


def _config(tmp_path: Path, monkeypatch) -> CiaoConfig:
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr(
        "ciao.sync_skills.sync_workspace_skills", lambda *_a, **_kw: None
    )
    reset_reroot_cache()
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return CiaoConfig(
        pwa_auth_token="test-token",
        pwa_auth_required=True,
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            )
        },
    )


def _receiver_app(config: CiaoConfig) -> TestClient:
    """The ingress route alone, over the real AuthMiddleware.

    No chat manager is wired, which is deliberate: the receiver only schedules
    its own dispatch when one is there, so the launch stays something this test
    drives at a moment of its own choosing instead of something that happens
    behind the response while the assertion is racing it.
    """
    serializer = URLSafeTimedSerializer("test-token")
    app = Starlette(
        routes=[Route("/hooks/v1/{trigger_id}", webhook_receive, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    return TestClient(app)


def _plane(config: CiaoConfig) -> CiaoControlPlane:
    return CiaoControlPlane(
        config,
        project_chat_manager=SimpleNamespace(
            get_chat=lambda _chat_id: SimpleNamespace(mode="auto")
        ),
        schedule_manager=SimpleNamespace(),
    )


def _principal() -> AgentPrincipal:
    return AgentPrincipal(
        token_id="tok-1",
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="claude",
    )


def _projects() -> list[ProjectInfo]:
    return [
        ProjectInfo(
            project_id="proj-alpha", name="Alpha", workspace="personal", created_at=""
        ),
        ProjectInfo(
            project_id="proj-personal-general",
            name="General",
            workspace="personal",
            created_at="",
        ),
    ]


def _send(
    client: TestClient, trigger_id: str, *, secret: str, key: str, text: str = _EVENT
):
    return client.post(
        f"/hooks/v1/{trigger_id}",
        json={"text": text},
        headers={"Authorization": f"Bearer {secret}", "Idempotency-Key": key},
    )


def _rows(config: CiaoConfig) -> list[dict[str, Any]]:
    journal = config.state_path.parent / RECEIPTS_NAME
    return [
        json.loads(line)
        for line in journal.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def test_one_configured_event_becomes_one_ordinary_chat(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    plane = _plane(config)
    principal = _principal()
    store = WebhookStore(config.state_path.parent / "webhooks.json")

    # 1. Configure it through the agent surface. The reply carries the secret
    #    once, and the trigger it describes cannot authenticate yet.
    created = plane.webhook_create(
        principal,
        name="Nightly build",
        instructions=_INSTRUCTIONS,
        project_id="proj-alpha",
        mode="normal",
    )["data"]
    trigger, secret = created["trigger"], created["secret"]
    assert created.keys() == {"trigger", "secret"}
    assert trigger["enabled"] is False and trigger["revision"] == 1
    assert store.authenticate(trigger["trigger_id"], secret) is None

    # 2. Enable it, at the revision create reported.
    enabled = plane.webhook_update(
        principal,
        trigger["trigger_id"],
        expected_revision=str(trigger["revision"]),
        enabled=True,
    )["data"]
    assert enabled["enabled"] is True
    assert store.authenticate(trigger["trigger_id"], secret) is not None

    # 3. The sender's request: one trigger's own bearer secret, an idempotency
    #    key, and nothing else. No cookie is involved or needed.
    with _receiver_app(config) as client:
        answer = _send(client, trigger["trigger_id"], secret=secret, key="k1")

        assert answer.status_code == 202, answer.text
        body = answer.json()
        assert body == {
            "receipt_id": body["receipt_id"],
            "trigger_id": trigger["trigger_id"],
            "status": ACCEPTED,
        }
        # The response is a receipt: no chat id, no output, never "completed".
        assert secret not in answer.text
        # Durable before any launch is attempted — that ordering is what makes a
        # crash in the launch window reviewable instead of invisible.
        rows = _rows(config)
        assert [row["status"] for row in rows] == [ACCEPTED]
        assert rows[0]["idempotency_key"] == "k1"
        assert rows[0]["event_text"] == _EVENT
        assert rows[0]["workspace"] == "personal"
        assert rows[0]["project_id"] == "proj-alpha"

        # 4. Dispatch the accepted receipt against a recording chat manager.
        receiver = WebhookReceiver(store.path)
        pcm = _RecordingPcm(_projects())
        settled = await dispatch_receipt(receiver, store, pcm, body["receipt_id"])

        assert settled is not None and settled.status == LAUNCHED
        assert settled.detail == "chat chat-webhook-1"

        # Exactly one chat, in the trigger's own target, titled by the trigger.
        assert len(pcm.created) == 1
        assert pcm.created[0]["project_id"] == "proj-alpha"
        assert pcm.created[0]["title"] == "Nightly build"
        # The trigger's configured mode is the turn's mode, and no model or
        # provider was chosen by anything a sender wrote.
        assert pcm.created[0]["mode"] == "normal"
        assert pcm.created[0]["model"] is None
        assert pcm.created[0]["provider"] is None

        # One turn, in that chat, with no permission escalation: `unattended`
        # is what turns a non-plan chat into `bypass`, and it is never named.
        assert len(pcm.started) == 1
        chat_id, prompt, kwargs = pcm.started[0]
        assert chat_id == "chat-webhook-1"
        assert kwargs == {}
        assert "unattended" not in prompt
        # The sender's text is data inside a fence, after the operator's
        # instructions — a sender can describe an event, not add one.
        assert prompt.startswith(_INSTRUCTIONS)
        assert prompt.index(_INSTRUCTIONS) < prompt.index("<webhook-event>")
        assert _EVENT in prompt
        assert prompt.index(_EVENT) < prompt.index(EVENT_FENCE_CLOSE)

        # The journal says the same thing in the order it says it.
        assert [row["status"] for row in _rows(config)] == [
            ACCEPTED,
            "launching",
            LAUNCHED,
        ]

        # 5a. The same key with the same body is the same event: the receipt it
        #     already has, appended nothing, and no second chat. The status is
        #     now `launched` rather than `accepted` because a retry answers with
        #     the receipt as it now stands — the record, not a replay.
        retry = _send(client, trigger["trigger_id"], secret=secret, key="k1")
        assert retry.status_code == 202
        assert retry.json() == {
            "receipt_id": body["receipt_id"],
            "trigger_id": trigger["trigger_id"],
            "status": LAUNCHED,
        }
        assert len(_rows(config)) == 3
        assert len(pcm.created) == len(pcm.started) == 1

        # 5b. The same key with a different body is a different event, and the
        #     receiver does not get to pick which of the two to run.
        conflict = _send(
            client,
            trigger["trigger_id"],
            secret=secret,
            key="k1",
            text="something else entirely",
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        assert len(_rows(config)) == 3
        assert receiver.get(body["receipt_id"]).event_text == _EVENT
        assert len(pcm.created) == len(pcm.started) == 1

    # 6. And the operator's off switch reaches an event already in the journal:
    #    disabling now cannot un-launch the chat above, but nothing new is
    #    accepted, and a dispatch of what is left launches nothing.
    plane.webhook_update(
        principal,
        trigger["trigger_id"],
        expected_revision=str(enabled["revision"]),
        enabled=False,
    )
    assert store.authenticate(trigger["trigger_id"], secret) is None
    with _receiver_app(config) as client:
        refused = _send(client, trigger["trigger_id"], secret=secret, key="k2")
    assert refused.status_code == 401
    assert len(_rows(config)) == 3

    # 7. Rotating kills the old secret at once, and keeps `enabled` off: a
    #    secret is not consent to run anything.
    rotated = plane.webhook_rotate(
        principal,
        trigger["trigger_id"],
        expected_revision=str(store.get(trigger["trigger_id"]).revision),
    )["data"]
    assert rotated["trigger"]["enabled"] is False
    assert rotated["secret"] and rotated["secret"] != secret
    assert store.authenticate(trigger["trigger_id"], secret) is None
    assert store.get(trigger["trigger_id"]).revision == rotated["trigger"]["revision"]
