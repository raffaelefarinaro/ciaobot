from __future__ import annotations

import base64
import itertools
import json
from typing import Any

import pytest

from ciao.chat_authority import (
    ChatAuthority,
    Command,
    HmacSigner,
    sign_command,
)
from ciao.chat_authority_client import (
    PROTOCOL_VERSION,
    ChatAuthorityClient,
    ChatAuthorityUnavailable,
    TransportError,
    TransportTimeout,
    TransportUnprovisioned,
    helper_status,
    static_provisioning_ok,
)
from ciao.share_evidence import CHAT_EVIDENCE_STATUSES

def status(client: ChatAuthorityClient, ok: bool = True) -> str:
    return helper_status(client, provisioned=lambda: ok)


SCOPE = {"workspace": "w1", "project": "p1", "chat": "c1"}


class MemStorage:
    blob: bytes | None = None

    def read(self) -> bytes | None:
        return self.blob

    def write(self, data: bytes) -> None:
        self.blob = data


class FakeHelper:
    """Serves the wire protocol from a real ChatAuthority, like the daemon will."""

    def __init__(self) -> None:
        self.operator = HmacSigner(b"o" * 32)
        ids = itertools.count(1)
        nonces = itertools.count(1)
        self.auth = ChatAuthority(
            signer=HmacSigner(b"a" * 32),
            operator=self.operator,
            storage=MemStorage(),
            new_id=lambda: f"id{next(ids)}",
        )
        self._counter = itertools.count(1)
        self._nonces = nonces
        self.state_counter = 5
        self.ready = True
        self.mutate_reply: Any = None
        self.raises: Exception | None = None
        self.requests: list[dict[str, Any]] = []
        self.timeouts: list[float] = []

    def op(self, kind: str, params: dict[str, Any], payload: bytes = b"") -> Any:
        cmd = Command(kind, next(self._counter), "app", params, payload)
        signed = sign_command(self.operator, cmd)
        return getattr(
            self.auth,
            {
                "create_grant": "create_grant",
                "submit_turn": "submit_turn",
                "revoke_grant": "revoke_grant",
            }[kind],
        )(signed)

    def call(self, request: bytes, *, timeout: float) -> bytes:
        self.timeouts.append(timeout)
        if self.raises is not None:
            raise self.raises
        req = json.loads(request)
        self.requests.append(req)
        reply: dict[str, Any] = {"v": PROTOCOL_VERSION, "ok": True, "nonce": req["nonce"]}
        if req["op"] == "health":
            reply["result"] = {
                "ready": self.ready,
                "helper_version": "1.0.0",
                "state_counter": self.state_counter,
                "epoch": self.auth.epoch,
                "authority_key_id": "k1",
            }
        elif req["op"] == "eligible_events":
            try:
                lookup = self.auth.eligible_events(
                    grant_id=req["grant_id"],
                    producer=req["producer"],
                    limit=req["limit"],
                    chat=req.get("chat"),
                    expected_epoch=req.get("expected_epoch"),
                )
            except Exception as exc:
                reply = {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "nonce": req["nonce"],
                    "code": getattr(exc, "code", "error"),
                }
            else:
                reply["result"] = {
                    "epoch": lookup.epoch,
                    "events": [
                        {
                            "event_id": e.event_id,
                            "turn_id": e.turn_id,
                            "revision": e.revision,
                            "seq": e.seq,
                            "send_id": e.send_id,
                            "workspace": e.workspace,
                            "project": e.project,
                            "chat": e.chat,
                            "principal": e.principal,
                            "text_b64": base64.b64encode(e.text).decode(),
                            "text_sha256": e.text_sha256,
                            "opted_out": e.opted_out,
                            "grant_ids": list(e.grant_ids),
                            "epoch": e.epoch,
                            "signature_b64": base64.b64encode(e.signature).decode(),
                        }
                        for e in lookup.events
                    ],
                }
        else:
            reply = {"v": PROTOCOL_VERSION, "ok": False, "nonce": req["nonce"], "code": "unknown_op"}
        if self.mutate_reply is not None:
            reply = self.mutate_reply(reply)
        return json.dumps(reply).encode()


def seeded() -> tuple[FakeHelper, ChatAuthorityClient, str]:
    helper = FakeHelper()
    grant = helper.op(
        "create_grant",
        {**SCOPE, "destination": "d", "audience": "a", "producers": ["memory"]},
    )
    for n in range(3):
        helper.op("submit_turn", {**SCOPE, "send_id": f"s{n}"}, f"text {n}".encode())
    return helper, make(helper), grant.grant_id


def _nonces() -> Any:
    counter = itertools.count(1)
    return lambda: f"{next(counter):032x}"


def make(helper: FakeHelper) -> ChatAuthorityClient:
    return ChatAuthorityClient(helper, nonce_factory=_nonces())


def test_status_enum_values_are_the_existing_ones() -> None:
    helper = FakeHelper()
    for value in (
        status(make(helper)),
        status(make(_raising(TransportUnprovisioned()))),
        status(make(_raising(TransportTimeout()))),
    ):
        assert value in CHAT_EVIDENCE_STATUSES
    # the client never reports platform or channel states; those stay in the engine
    assert "remote_client" not in {status(make(helper))}


def _raising(exc: Exception) -> FakeHelper:
    helper = FakeHelper()
    helper.raises = exc
    return helper


def test_available_only_when_helper_verified() -> None:
    assert status(make(FakeHelper())) == "available"


def test_unprovisioned_when_no_service_registered() -> None:
    assert status(make(_raising(TransportUnprovisioned()))) == "unprovisioned"


@pytest.mark.parametrize(
    "exc", [TransportTimeout(), TransportError("boom"), OSError("x")]
)
def test_degraded_on_transport_failure(exc: Exception) -> None:
    assert status(make(_raising(exc))) == "degraded"


def test_degraded_when_helper_not_ready() -> None:
    helper = FakeHelper()
    helper.ready = False
    assert status(make(helper)) == "degraded"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: {**r, "v": 99},
        lambda r: {**r, "nonce": "0" * 32},
        lambda r: {k: v for k, v in r.items() if k != "nonce"},
        lambda r: {**r, "result": "nope"},
        lambda r: {**r, "ok": False, "code": "boom"},
        lambda r: {**r, "result": {**r["result"], "state_counter": "5"}},
    ],
)
def test_degraded_on_bad_reply(mutate: Any) -> None:
    helper = FakeHelper()
    helper.mutate_reply = mutate
    assert status(make(helper)) == "degraded"


def test_garbage_and_oversize_replies_degrade() -> None:
    class Garbage:
        def __init__(self, data: bytes) -> None:
            self.data = data

        def call(self, request: bytes, *, timeout: float) -> bytes:
            return self.data

    for data in (b"not json", b"[]", b"x" * (9 * 1024 * 1024)):
        assert status(ChatAuthorityClient(Garbage(data), nonce_factory=_nonces())) == "degraded"


def test_counter_rollback_is_degraded_and_sticky_until_new_client() -> None:
    helper = FakeHelper()
    client = make(helper)
    assert status(client) == "available"
    helper.state_counter = 4  # restored older blob / counter went backwards
    assert status(client) == "degraded"
    helper.state_counter = 9  # a higher number does not clear a seen regression
    assert status(client) == "degraded"
    assert status(make(helper)) == "available"


def test_counter_may_advance_and_repeat() -> None:
    helper = FakeHelper()
    client = make(helper)
    for value in (5, 5, 6, 100):
        helper.state_counter = value
        assert status(client) == "available"


def test_authority_key_change_is_degraded() -> None:
    helper = FakeHelper()
    client = make(helper)
    assert status(client) == "available"

    def other_key(r: dict[str, Any]) -> dict[str, Any]:
        r["result"]["authority_key_id"] = "k2"
        return r

    helper.mutate_reply = other_key
    assert status(client) == "degraded"


def test_each_request_uses_a_fresh_nonce_and_a_timeout() -> None:
    helper = FakeHelper()
    client = make(helper)
    status(client)
    status(client)
    assert len({r["nonce"] for r in helper.requests}) == 2
    assert helper.timeouts and all(0 < t <= 10 for t in helper.timeouts)


def test_lookup_round_trips_real_events() -> None:
    helper, _, grant_id = seeded()
    client = make(helper)
    lookup = client.eligible_events(grant_id=grant_id, producer="memory", limit=2)
    assert [e.text for e in lookup.events] == [b"text 2", b"text 1"]
    assert lookup.epoch == helper.auth.epoch
    direct = helper.auth.eligible_events(grant_id=grant_id, producer="memory", limit=2)
    assert lookup.events == direct.events
    assert all(helper.auth.verify_event(e) for e in lookup.events)


def test_lookup_passes_filters_to_the_helper_not_the_client() -> None:
    helper, _, grant_id = seeded()
    client = make(helper)
    client.eligible_events(
        grant_id=grant_id, producer="memory", limit=1, chat="c1", expected_epoch=helper.auth.epoch
    )
    sent = helper.requests[-1]
    assert (sent["grant_id"], sent["producer"], sent["limit"], sent["chat"]) == (grant_id, "memory", 1, "c1")
    assert sent["expected_epoch"] == helper.auth.epoch


def test_stale_epoch_surfaces_as_a_refusal_with_the_helper_code() -> None:
    helper, _, grant_id = seeded()
    client = make(helper)
    with pytest.raises(ChatAuthorityUnavailable) as err:
        client.eligible_events(grant_id=grant_id, producer="memory", limit=1, expected_epoch=999)
    assert err.value.code == "epoch_changed"


def test_revoked_grant_yields_nothing() -> None:
    helper, _, grant_id = seeded()
    helper.op("revoke_grant", {"grant_id": grant_id})
    lookup = make(helper).eligible_events(grant_id=grant_id, producer="memory", limit=5)
    assert lookup.events == ()
    assert lookup.epoch == helper.auth.epoch


def test_lookup_fails_closed_on_every_transport_problem() -> None:
    for exc in (TransportUnprovisioned(), TransportTimeout(), TransportError("x")):
        helper, _, grant_id = seeded()
        helper.raises = exc
        with pytest.raises(ChatAuthorityUnavailable):
            make(helper).eligible_events(grant_id=grant_id, producer="memory", limit=1)


def _tamper(fn: Any) -> Any:
    def mutate(reply: dict[str, Any]) -> dict[str, Any]:
        if reply.get("ok") and "events" in reply["result"]:
            fn(reply["result"])
        return reply

    return mutate


@pytest.mark.parametrize(
    "fn",
    [
        lambda res: res["events"][0].update(text_sha256="0" * 64),  # bytes do not match digest
        lambda res: res["events"][0].update(text_b64="!!!"),
        lambda res: res["events"][0].update(grant_ids=["other"]),  # not under requested grant
        lambda res: res["events"][0].update(chat="c2"),  # outside the requested chat
        lambda res: res["events"][0].update(epoch=res["epoch"] + 1),  # from the future
        lambda res: res["events"].append(res["events"][0]),  # duplicate and over the limit
        lambda res: res["events"][0].pop("signature_b64"),
        lambda res: res["events"][0].update(revision="1"),
        lambda res: res.update(events="x"),
    ],
)
def test_client_refuses_inconsistent_events(fn: Any) -> None:
    helper, _, grant_id = seeded()
    helper.mutate_reply = _tamper(fn)
    with pytest.raises(ChatAuthorityUnavailable):
        make(helper).eligible_events(grant_id=grant_id, producer="memory", limit=2, chat="c1")


def test_lookup_epoch_must_match_expected_epoch() -> None:
    helper, _, grant_id = seeded()
    helper.mutate_reply = _tamper(lambda res: res.update(epoch=res["epoch"] + 1))
    with pytest.raises(ChatAuthorityUnavailable):
        make(helper).eligible_events(
            grant_id=grant_id, producer="memory", limit=1, expected_epoch=helper.auth.epoch
        )


def test_limit_validated_before_any_call() -> None:
    helper = FakeHelper()
    client = make(helper)
    for bad in (0, -1, 201):
        with pytest.raises(ValueError):
            client.eligible_events(grant_id="g", producer="memory", limit=bad)
    assert helper.requests == []


def test_engine_client_has_no_way_to_mint_or_change_authority() -> None:
    public = {n for n in dir(ChatAuthorityClient) if not n.startswith("_")}
    assert public == {"health", "eligible_events"}


def test_health_alone_never_reads_available() -> None:
    assert status(make(FakeHelper()), ok=False) == "degraded"


def test_unprovisioned_transport_wins_over_a_failing_static_check() -> None:
    assert status(make(_raising(TransportUnprovisioned())), ok=False) == "unprovisioned"


def test_static_check_is_not_asked_when_health_fails() -> None:
    calls: list[int] = []
    helper = _raising(TransportTimeout())
    assert (
        helper_status(make(helper), provisioned=lambda: calls.append(1) or True) == "degraded"
    )
    assert calls == []


def test_call_is_reachable_only_from_the_two_public_methods() -> None:
    import ast
    import inspect

    import ciao.chat_authority_client as mod

    tree = ast.parse(inspect.getsource(mod))
    callers: set[str] = set()
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and node.attr == "_call":
                    callers.add(fn.name)
    assert callers == {"health", "eligible_events"}
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        assert not any(
            isinstance(n, ast.Attribute) and n.attr == "_call" for n in ast.walk(fn)
        ), fn.name


def _tree(tmp_path: Any, mode: int = 0o755) -> Any:
    import os

    root = tmp_path / "root"
    for rel in ("Library/PrivilegedHelperTools", "Library/LaunchDaemons"):
        (root / rel).mkdir(parents=True)
    files = [
        "Library/PrivilegedHelperTools/com.ciaobot.chat-authority",
        "Library/PrivilegedHelperTools/com.ciaobot.chat-authority-anchor",
        "Library/LaunchDaemons/com.ciaobot.chat-authority.plist",
        "Library/LaunchDaemons/com.ciaobot.chat-authority-anchor.plist",
    ]
    for rel in files:
        (root / rel).write_text("x")
        os.chmod(root / rel, mode & 0o755)
    for p in (root, *[root / "Library", root / "Library/PrivilegedHelperTools", root / "Library/LaunchDaemons"]):
        os.chmod(p, 0o755)
    return root, files


def test_static_check_passes_on_a_correct_tree(tmp_path: Any) -> None:
    import os

    root, _ = _tree(tmp_path)
    assert static_provisioning_ok(root, owner_uid=os.getuid(), account_ok=lambda: True)


def test_static_check_refuses_wrong_owner_missing_file_and_bad_account(tmp_path: Any) -> None:
    import os

    root, files = _tree(tmp_path)
    me = os.getuid()
    assert not static_provisioning_ok(root, owner_uid=me + 1, account_ok=lambda: True)
    assert not static_provisioning_ok(root, owner_uid=me, account_ok=lambda: False)
    (root / files[0]).unlink()
    assert not static_provisioning_ok(root, owner_uid=me, account_ok=lambda: True)


@pytest.mark.parametrize("target", ["file", "parent", "root"])
def test_static_check_refuses_group_or_world_writable(tmp_path: Any, target: str) -> None:
    import os

    root, files = _tree(tmp_path)
    path = {
        "file": root / files[0],
        "parent": root / "Library/PrivilegedHelperTools",
        "root": root / "Library",
    }[target]
    os.chmod(path, 0o775 if target != "file" else 0o775)
    assert not static_provisioning_ok(root, owner_uid=os.getuid(), account_ok=lambda: True)


def test_static_check_refuses_symlinks(tmp_path: Any) -> None:
    import os

    root, files = _tree(tmp_path)
    real = tmp_path / "evil"
    real.write_text("x")
    os.chmod(real, 0o755)
    (root / files[2]).unlink()
    (root / files[2]).symlink_to(real)
    assert not static_provisioning_ok(root, owner_uid=os.getuid(), account_ok=lambda: True)
