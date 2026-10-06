"""The webhook receipt-history reads: ``GET …/receipts`` (#1044).

Covers the read surface over the ingress journal against a real ``CiaoConfig``
over a tmp runtime, behind the signed session cookie, with a real
``WebhookReceiver`` writing the journal the route then reads.

The claims pinned here, and why each one is load-bearing:

- **The session decides the workspace, and only then is anything read.** An
  absent or unregistered ``?workspace=`` is a 400; a trigger that is not that
  workspace's is the same 404 as one that does not exist, because a history that
  answered "yes, that id is real, over there" would be a probe into another
  workspace.
- **A receipt row is a projection.** ``to_public_dict`` is the only shape that
  leaves the engine, and what it drops (the sender's idempotency key, the body
  digest) is what it must: a credential never reaches a history, and neither does
  the machinery that collapses a retry.
- **The read is bounded and newest-first**, because a history is a question
  about the recent tail rather than a walk over a journal the trim bounds at four
  megabytes.
- **Retention is stated honestly.** The dedupe window is seven days and it steps
  the *key*; a settled receipt stays readable after it, which is the difference a
  person reviewing an incident actually needs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_webhooks import webhook_delete, webhook_receipts, webhook_trigger_receipts
from ciao.webhooks import (
    DEDUPE_RETENTION_DAYS,
    RECEIPTS_HISTORY_LIMIT,
    WebhookReceiver,
    WebhookStore,
    _RateWindow,
)

_BODY = json.dumps({"text": "the nightly build failed on main"}).encode()


class _Clock:
    """A clock a test moves by hand, so the dedupe window is not a wait."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


def _world(
    tmp_path: Path, monkeypatch
) -> tuple[TestClient, dict[str, str], CiaoConfig, WebhookStore]:
    """A session-guarded app over a real config with `personal` and `work`."""
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr(
        "ciao.sync_skills.sync_workspace_skills", lambda *_a, **_kw: None
    )
    reset_reroot_cache()
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            ),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    serializer = URLSafeTimedSerializer("test-token")
    app = Starlette(
        routes=[
            # The same shapes `ciao/web/app.py` registers, and in the same order:
            # the literal `receipts` segment precedes `{trigger_id}` there, and a
            # test that registered them the other way round would pass against a
            # 405 the real app never answers.
            Route("/api/webhooks/receipts", webhook_receipts, methods=["GET"]),
            Route(
                "/api/webhooks/{trigger_id}/receipts",
                webhook_trigger_receipts,
                methods=["GET"],
            ),
            # Registered so one test can delete the trigger a receipt belongs to
            # through the route that owns it, rather than by reaching past this
            # surface into the store.
            Route("/api/webhooks/{trigger_id}", webhook_delete, methods=["DELETE"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    client = TestClient(app)
    cookies = {SESSION_COOKIE: serializer.dumps({"user": "owner"})}
    return client, cookies, config, WebhookStore(config.state_path.parent / "webhooks.json")


def _receiver(config: CiaoConfig, *, clock: _Clock | None = None, permissive: bool = True) -> WebhookReceiver:
    """A receiver over the same store path the routes build theirs from.

    ``permissive`` by default: every test here writes receipts on purpose, and a
    cap test must not be refused by the sender-facing rate window.
    """
    window = _RateWindow(limit=10**6, window_seconds=60) if permissive else None
    return WebhookReceiver(
        config.state_path.parent / "webhooks.json", clock=clock, window=window
    )


def _trigger(store: WebhookStore, workspace: str = "personal") -> Any:
    """One trigger in ``workspace``, from the store the route reads."""
    created, _secret = store.create(
        name=f"{workspace} intake",
        workspace=workspace,
        instructions="File the failure as an intake note",
    )
    return created


def _event(
    receiver: WebhookReceiver, trigger: Any, key: str, *, text: str = "an event"
) -> str:
    """Record one accepted event and settle it ``launched`` with a chat."""
    receipt = receiver.receive(
        trigger, idempotency_key=key, body=json.dumps({"text": text}).encode()
    )
    receiver.begin_launch(receipt.id)
    receiver.settle_launched(receipt.id, chat_id=f"chat-{key}")
    return receipt.id


def _read(
    client: TestClient, cookies: dict[str, str], trigger_id: str, workspace: str = "personal"
) -> Any:
    response = client.get(
        f"/api/webhooks/{trigger_id}/receipts?workspace={workspace}", cookies=cookies
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_both_receipt_reads_need_the_session_cookie(
    tmp_path: Path, monkeypatch
) -> None:
    client, cookies, _config, store = _world(tmp_path, monkeypatch)
    trigger = _trigger(store)
    paths = (
        f"/api/webhooks/{trigger.trigger_id}/receipts?workspace=personal",
        "/api/webhooks/receipts?workspace=personal",
    )

    for path in paths:
        # No cookie at all, and a cookie nobody signed. This is the whole claim:
        # the ingress route's bearer secret authorizes nothing here, and neither
        # does a session belong to a webhook.
        assert client.get(path).status_code == 401, path
        assert client.get(path, cookies={SESSION_COOKIE: "forged"}).status_code == 401, path

    # Nothing but a read is reachable here, so there is no cross-origin write for
    # a foreign page to make — the origin predicate in `AuthMiddleware` exempts
    # safe methods, and a `GET` is the only one this route registers.
    for path in paths:
        assert client.post(path, cookies=cookies).status_code == 405, path


def test_an_unknown_or_absent_workspace_is_a_400(tmp_path: Path, monkeypatch) -> None:
    client, cookies, _config, store = _world(tmp_path, monkeypatch)
    trigger = _trigger(store)

    for query in ("", "?workspace=", "?workspace=nope", "?workspace=%20"):
        for path in (
            f"/api/webhooks/{trigger.trigger_id}/receipts{query}",
            f"/api/webhooks/receipts{query}",
        ):
            response = client.get(path, cookies=cookies)
            assert response.status_code == 400, (path, response.text)
            assert "unknown workspace" in response.json()["error"]


def test_a_foreign_or_unknown_trigger_is_a_404(tmp_path: Path, monkeypatch) -> None:
    client, cookies, config, store = _world(tmp_path, monkeypatch)
    personal = _trigger(store, "personal")
    work = _trigger(store, "work")
    _event(_receiver(config), work, "w1", text="an event this workspace must not read")

    unknown = client.get(
        f"/api/webhooks/{'0' * 32}/receipts?workspace=personal", cookies=cookies
    )
    assert unknown.status_code == 404, unknown.text

    # A real trigger in another workspace is *the same* 404: this route does not
    # confirm what another workspace has configured, so the id cannot be told
    # from an unknown one by anything but guessing. (The sentence echoes the id
    # the caller sent, exactly as the management routes' does.)
    foreign = client.get(
        f"/api/webhooks/{work.trigger_id}/receipts?workspace=personal",
        cookies=cookies,
    )
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["error"].startswith("no webhook trigger")
    assert unknown.json()["error"].startswith("no webhook trigger")
    assert set(foreign.json()) == set(unknown.json()) == {"error"}

    # And the refusal leaks nothing that workspace received: neither the event
    # text nor the chat it became.
    assert "an event this workspace must not read" not in foreign.text
    assert "chat-w1" not in foreign.text

    # And the workspace that owns it reads its own history normally.
    assert (
        client.get(
            f"/api/webhooks/{personal.trigger_id}/receipts?workspace=personal",
            cookies=cookies,
        ).status_code
        == 200
    )


def test_a_receipt_row_carries_the_outcome_and_no_credential(
    tmp_path: Path, monkeypatch
) -> None:
    client, cookies, config, store = _world(tmp_path, monkeypatch)
    trigger = _trigger(store)
    receiver = _receiver(config)
    receipt_id = _event(receiver, trigger, "k1", text="the nightly build failed on main")

    body = _read(client, cookies, trigger.trigger_id)
    assert body["trigger_id"] == trigger.trigger_id
    assert body["trigger_name"] == trigger.name
    assert body["limit"] == RECEIPTS_HISTORY_LIMIT
    assert len(body["receipts"]) == 1

    row = body["receipts"][0]
    assert row == {
        "receipt_id": receipt_id,
        "trigger_id": trigger.trigger_id,
        "trigger_name": trigger.name,
        # `launched` is the success outcome, and the chat the event became is the
        # whole reason a person needs this row: the chat is an ordinary one whose
        # title says nothing about which event made it.
        "status": "launched",
        "chat_id": "chat-k1",
        "event_text": "the nightly build failed on main",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "detail": "chat chat-k1",
    }
    assert row["created_at"] and row["updated_at"]
    assert row["receipt_id"].startswith("wbrcpt_")

    # The projection is the whole surface: no verifier, and none of the journal's
    # own machinery for collapsing a retry either.
    for absent in ("secret", "secret_sha256", "idempotency_key", "body_digest"):
        assert absent not in row


def test_rows_are_newest_first_and_the_cap_is_enforced(
    tmp_path: Path, monkeypatch
) -> None:
    client, cookies, config, store = _world(tmp_path, monkeypatch)
    trigger = _trigger(store)
    receiver = _receiver(config)
    for index in range(RECEIPTS_HISTORY_LIMIT + 4):
        _event(receiver, trigger, f"k{index:03d}")

    body = _read(client, cookies, trigger.trigger_id)
    assert len(body["receipts"]) == RECEIPTS_HISTORY_LIMIT

    # Newest first, and each row the *effective* state of its receipt rather than
    # its first one: the journal holds accepted → launching → launched per event,
    # so a read that did not fold would show three rows per event and fill its
    # cap with one event's own history.
    chat_ids = [row["chat_id"] for row in body["receipts"]]
    assert chat_ids == [f"chat-k{index:03d}" for index in range(53, 3, -1)]
    assert all(row["status"] == "launched" for row in body["receipts"])

    # And the cap is the receiver's, not a number the route invented.
    assert body["limit"] == RECEIPTS_HISTORY_LIMIT
    assert len(receiver.recent_receipts_for(trigger.trigger_id)) == RECEIPTS_HISTORY_LIMIT


def test_a_settled_receipt_past_the_dedupe_window_is_still_listed(
    tmp_path: Path, monkeypatch
) -> None:
    client, cookies, config, store = _world(tmp_path, monkeypatch)
    trigger = _trigger(store)
    clock = _Clock(datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
    receiver = _receiver(config, clock=clock)

    first = _event(receiver, trigger, "same-key", text="first attempt")
    # Past the dedupe window the key is the sender's to reuse, so the same key
    # takes the next generation instead of folding onto the settled receipt.
    clock.advance(days=DEDUPE_RETENTION_DAYS + 1)
    second = _event(receiver, trigger, "same-key", text="second attempt")
    assert second != first

    rows = _read(client, cookies, trigger.trigger_id)["receipts"]
    assert [row["receipt_id"] for row in rows] == [second, first]

    # The seven-day window is a dedupe window and nothing more: it steps the key,
    # it does not erase the outcome of the attempt that came before it. A person
    # reviewing an incident needs that row.
    older = rows[1]
    assert older["status"] == "launched"
    assert older["chat_id"] == "chat-same-key"
    assert older["event_text"] == "first attempt"


def test_an_interrupted_receipt_is_listed_not_dropped(
    tmp_path: Path, monkeypatch
) -> None:
    client, cookies, config, store = _world(tmp_path, monkeypatch)
    trigger = _trigger(store)
    receiver = _receiver(config)

    settled = _event(receiver, trigger, "k1")
    stranded = receiver.receive(trigger, idempotency_key="k2", body=_BODY)
    receiver.begin_launch(stranded.id)
    # What a crash inside the launch window leaves, without a process alive to
    # run the recovery sweep.
    interrupted = receiver.recover_interrupted()

    assert [receipt.id for receipt in interrupted] == [stranded.id]
    rows = _read(client, cookies, trigger.trigger_id)["receipts"]
    assert [row["receipt_id"] for row in rows] == [stranded.id, settled]
    assert rows[0]["status"] == "interrupted"
    assert rows[0]["chat_id"] is None
    assert "review" in rows[0]["detail"]
    # An accepted-only receipt is visible too: "recorded but not launched" is
    # exactly what an operator has to be able to see.
    pending = receiver.receive(trigger, idempotency_key="k3", body=_BODY)
    statuses = {
        row["receipt_id"]: row["status"]
        for row in _read(client, cookies, trigger.trigger_id)["receipts"]
    }
    assert statuses[pending.id] == "accepted"


def test_a_deleted_trigger_leaves_the_workspace_history_intact(
    tmp_path: Path, monkeypatch
) -> None:
    client, cookies, config, store = _world(tmp_path, monkeypatch)
    trigger = _trigger(store)
    receiver = _receiver(config)
    _event(receiver, trigger, "k1")

    deleted = client.delete(
        f"/api/webhooks/{trigger.trigger_id}?expected_revision={trigger.revision}",
        cookies=cookies,
    )
    assert deleted.status_code == 204, deleted.text

    # Its own history goes with the record the route scopes by, and says so the
    # same way an unknown id does.
    assert (
        client.get(
            f"/api/webhooks/{trigger.trigger_id}/receipts?workspace=personal",
            cookies=cookies,
        ).status_code
        == 404
    )

    # The receipt itself is not: it copied the trigger's workspace so it would
    # outlive it, and the workspace-wide read is what still finds it.
    body = client.get("/api/webhooks/receipts?workspace=personal", cookies=cookies)
    assert body.status_code == 200, body.text
    rows = body.json()["receipts"]
    assert len(rows) == 1
    assert rows[0]["chat_id"] == "chat-k1"
    assert rows[0]["trigger_name"] == "personal intake"

    # And another workspace sees none of it.
    assert (
        client.get("/api/webhooks/receipts?workspace=work", cookies=cookies).json()[
            "receipts"
        ]
        == []
    )


def test_the_workspace_read_spans_every_trigger_newest_first(
    tmp_path: Path, monkeypatch
) -> None:
    client, cookies, config, store = _world(tmp_path, monkeypatch)
    first = _trigger(store, "personal")
    second = _trigger(store, "personal")
    receiver = _receiver(config)
    _event(receiver, first, "k1")
    _event(receiver, second, "k2")

    body = client.get("/api/webhooks/receipts?workspace=personal", cookies=cookies)
    assert body.status_code == 200, body.text
    rows = body.json()
    assert rows["workspace"] == "personal"
    assert rows["limit"] == RECEIPTS_HISTORY_LIMIT
    assert [row["trigger_id"] for row in rows["receipts"]] == [
        second.trigger_id,
        first.trigger_id,
    ]
    assert [row["trigger_name"] for row in rows["receipts"]] == [
        second.name,
        first.name,
    ]

    # One trigger's own read is not this one: it is scoped to that trigger.
    assert [
        row["trigger_id"]
        for row in _read(client, cookies, first.trigger_id)["receipts"]
    ] == [first.trigger_id]


def test_a_journal_that_cannot_be_read_is_a_503(tmp_path: Path, monkeypatch) -> None:
    client, cookies, config, store = _world(tmp_path, monkeypatch)
    trigger = _trigger(store)

    # A journal path that is a directory: the read cannot even open it, and the
    # answer has to be "come back later" rather than an empty history, which
    # would claim the trigger had received nothing.
    (config.state_path.parent / "webhook-receipts.jsonl").mkdir()
    for path in (
        f"/api/webhooks/{trigger.trigger_id}/receipts?workspace=personal",
        "/api/webhooks/receipts?workspace=personal",
    ):
        response = client.get(path, cookies=cookies)
        assert response.status_code == 503, (path, response.text)
        assert "cannot be read" in response.json()["error"]


def test_a_row_this_code_cannot_decode_is_a_500(tmp_path: Path, monkeypatch) -> None:
    client, cookies, config, store = _world(tmp_path, monkeypatch)
    trigger = _trigger(store)
    receiver = _receiver(config)
    receipt_id = _event(receiver, trigger, "k1")

    # Failed closed, like every other reader of this journal: a history that
    # quietly omitted a row it could not parse would be a history claiming the
    # event never arrived.
    journal = receiver.journal
    row = json.loads(journal.read_text(encoding="utf-8").split("\n")[0])
    row["status"] = "not-a-status"
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    response = client.get(
        f"/api/webhooks/{trigger.trigger_id}/receipts?workspace=personal",
        cookies=cookies,
    )
    assert response.status_code == 500, response.text
    assert "unknown status" in response.json()["error"]
    assert receipt_id