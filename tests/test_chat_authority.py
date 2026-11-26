from __future__ import annotations

import itertools
import json
from typing import Any

import pytest

from ciao.chat_authority import (
    MAX_TEXT_BYTES,
    AuthorityError,
    ChatAuthority,
    Command,
    HmacSigner,
    SignedCommand,
    TurnEvent,
    sign_command,
)

AUTH_KEY = b"a" * 32
OP_KEY = b"o" * 32
SCOPE = {"workspace": "w1", "project": "p1", "chat": "c1"}


class MemStorage:
    def __init__(self) -> None:
        self.blob: bytes | None = None
        self.fail = False

    def read(self) -> bytes | None:
        return self.blob

    def write(self, data: bytes) -> None:
        if self.fail:
            raise OSError("disk full")
        self.blob = data


class Rig:
    def __init__(self, storage: MemStorage | None = None) -> None:
        self.storage = storage or MemStorage()
        self.operator = HmacSigner(OP_KEY)
        self.ids = itertools.count(1)
        self.nonces = itertools.count(1)
        self.open()

    def open(self) -> None:
        self.auth = ChatAuthority(
            signer=HmacSigner(AUTH_KEY),
            operator=self.operator,
            storage=self.storage,
            new_id=lambda: f"id{next(self.ids)}",
        )

    def cmd(self, kind: str, params: dict[str, Any], payload: bytes = b"", nonce: str | None = None) -> SignedCommand:
        n = nonce or f"n{next(self.nonces)}"
        return sign_command(self.operator, Command(kind, n, "op", params, payload))

    def grant(self, chat: str | None = "c1", producers: list[str] | None = None) -> str:
        g = self.auth.create_grant(
            self.cmd(
                "create_grant",
                {
                    "workspace": "w1",
                    "project": "p1",
                    "chat": chat,
                    "destination": "brain",
                    "audience": "team",
                    "producers": producers or ["memory_extraction"],
                },
            )
        )
        return g.grant_id

    def send(self, send_id: str, text: bytes = b"hello", **extra: Any) -> TurnEvent:
        params = {"send_id": send_id, **SCOPE, **extra}
        return self.auth.submit_turn(self.cmd("submit_turn", params, text))

    def ids_of(self, grant_id: str, limit: int = 50, **kw: Any) -> list[str]:
        look = self.auth.eligible_events(
            grant_id=grant_id, producer="memory_extraction", limit=limit, **kw
        )
        return [e.send_id for e in look.events]


def code(exc: pytest.ExceptionInfo[AuthorityError]) -> str:
    return exc.value.code


def test_retry_with_different_bytes_is_refused_and_identical_retry_dedups() -> None:
    r = Rig()
    first = r.send("s1", b"exact bytes")
    again = r.send("s1", b"exact bytes")
    assert again.event_id == first.event_id
    assert again.text == b"exact bytes"
    with pytest.raises(AuthorityError) as e:
        r.send("s1", b"exact bytes ")
    assert code(e) == "retry_conflict"
    with pytest.raises(AuthorityError) as e:
        r.auth.submit_turn(
            r.cmd("submit_turn", {"send_id": "s1", **SCOPE, "chat": "c2"}, b"exact bytes")
        )
    assert code(e) == "retry_conflict"
    with pytest.raises(AuthorityError) as e:
        r.send("s1", b"exact bytes", opted_out=True)
    assert code(e) == "retry_conflict"


def test_revocation_advances_epoch_and_stops_eligibility() -> None:
    r = Rig()
    g = r.grant()
    r.send("s1")
    before = r.auth.epoch
    assert r.ids_of(g) == ["s1"]
    after = r.auth.revoke_grant(r.cmd("revoke_grant", {"grant_id": g}))
    assert after == before + 1 == r.auth.epoch
    assert r.ids_of(g) == []
    with pytest.raises(AuthorityError) as e:
        r.auth.eligible_events(
            grant_id=g, producer="memory_extraction", limit=5, expected_epoch=before
        )
    assert code(e) == "epoch_changed"
    # Revoking again changes nothing; later turns are not enrolled.
    assert r.auth.revoke_grant(r.cmd("revoke_grant", {"grant_id": g})) == after
    r.send("s2")
    assert r.ids_of(g) == []


def test_replay_after_restart_is_refused() -> None:
    r = Rig()
    signed = r.cmd("submit_turn", {"send_id": "s1", **SCOPE}, b"hi", nonce="fixed")
    r.auth.submit_turn(signed)
    with pytest.raises(AuthorityError) as e:
        r.auth.submit_turn(signed)
    assert code(e) == "replayed"
    r.open()  # restart: new object over the same storage
    with pytest.raises(AuthorityError) as e:
        r.auth.submit_turn(signed)
    assert code(e) == "replayed"
    # A fresh nonce for the identical send still resolves to the same event.
    assert r.send("s1", b"hi").send_id == "s1"
    assert len(r.auth._state.events or []) == 1


def test_state_survives_restart_including_grants_epoch_and_withdrawals() -> None:
    r = Rig()
    g = r.grant()
    ev = r.send("s1", "caf\u00e9".encode())
    r.send("s2")
    r.auth.withdraw_turn(r.cmd("withdraw_turn", {"turn_id": ev.turn_id}))
    epoch = r.auth.epoch
    r.open()
    assert r.auth.epoch == epoch
    assert r.ids_of(g) == ["s2"]
    assert r.auth.grant(g) is not None
    # still withdrawn: an identical retry returns the event but does not re-enroll it
    r.send("s1", "caf\u00e9".encode())
    assert r.ids_of(g) == ["s2"]


def test_withdrawn_grant_yields_nothing_and_never_comes_back() -> None:
    r = Rig()
    g = r.grant()
    r.send("s1")
    epoch = r.auth.withdraw_grant(r.cmd("withdraw_grant", {"grant_id": g}))
    assert epoch == r.auth.epoch
    assert r.ids_of(g) == []
    r.open()
    assert r.ids_of(g) == []
    # A revoked grant can still be withdrawn once; an unknown one is refused.
    g2 = r.grant()
    r.auth.revoke_grant(r.cmd("revoke_grant", {"grant_id": g2}))
    e1 = r.auth.epoch
    assert r.auth.withdraw_grant(r.cmd("withdraw_grant", {"grant_id": g2})) == e1 + 1
    with pytest.raises(AuthorityError) as e:
        r.auth.withdraw_grant(r.cmd("withdraw_grant", {"grant_id": "nope"}))
    assert code(e) == "unknown_grant"


def test_limit_applies_after_filters_newest_first() -> None:
    r = Rig()
    g = r.grant()
    turns = [r.send(f"s{i}") for i in range(6)]
    # The three newest are ineligible: withdrawn, opted out, withdrawn.
    r.send("s6", opted_out=True)
    r.auth.withdraw_turn(r.cmd("withdraw_turn", {"turn_id": turns[5].turn_id}))
    r.auth.withdraw_turn(r.cmd("withdraw_turn", {"turn_id": turns[4].turn_id}))
    r.send("s7", opted_out=True)
    assert r.ids_of(g, limit=2) == ["s3", "s2"]
    assert r.ids_of(g, limit=50) == ["s3", "s2", "s1", "s0"]
    with pytest.raises(AuthorityError):
        r.ids_of(g, limit=0)


def test_grant_does_not_reach_older_turns_other_chats_or_other_producers() -> None:
    r = Rig()
    r.send("old")
    g = r.grant()
    r.send("new")
    r.auth.submit_turn(r.cmd("submit_turn", {"send_id": "other", **SCOPE, "chat": "c2"}, b"x"))
    r.auth.submit_turn(
        r.cmd("submit_turn", {"send_id": "ws", **SCOPE, "workspace": "w2"}, b"x")
    )
    assert r.ids_of(g) == ["new"]
    assert (
        r.auth.eligible_events(grant_id=g, producer="nightly_curation", limit=5).events
        == ()
    )
    project_wide = r.grant(chat=None)
    r.auth.submit_turn(r.cmd("submit_turn", {"send_id": "c2b", **SCOPE, "chat": "c2"}, b"x"))
    r.send("c1b")
    assert r.ids_of(project_wide) == ["c1b", "c2b"]
    assert r.ids_of(project_wide, chat="c2") == ["c2b"]


def test_correction_supersedes_earlier_revision() -> None:
    r = Rig()
    g = r.grant()
    first = r.send("s1", b"v1")
    second = r.send("s2", b"v2", corrects=first.turn_id)
    assert (second.turn_id, second.revision) == (first.turn_id, 2)
    assert r.ids_of(g) == ["s2"]
    r.auth.withdraw_turn(r.cmd("withdraw_turn", {"turn_id": first.turn_id}))
    assert r.ids_of(g) == []
    with pytest.raises(AuthorityError) as e:
        r.send("s3", b"v3", corrects="missing")
    assert code(e) == "bad_command"


def test_commands_need_a_valid_operator_signature_and_matching_kind() -> None:
    r = Rig()
    forged = sign_command(
        HmacSigner(b"x" * 32), Command("submit_turn", "n", "op", {"send_id": "s", **SCOPE}, b"hi")
    )
    with pytest.raises(AuthorityError) as e:
        r.auth.submit_turn(forged)
    assert code(e) == "bad_signature"
    good = r.cmd("submit_turn", {"send_id": "s", **SCOPE}, b"hi")
    tampered = SignedCommand(
        Command("submit_turn", good.command.nonce, "op", good.command.params, b"HI"),
        good.signature,
    )
    with pytest.raises(AuthorityError) as e:
        r.auth.submit_turn(tampered)
    assert code(e) == "bad_signature"
    with pytest.raises(AuthorityError) as e:
        r.auth.revoke_grant(good)
    assert code(e) == "bad_command"
    assert r.auth.epoch == 0


def test_text_must_be_bounded_nonempty_utf8() -> None:
    r = Rig()
    for bad in (b"", b"\xff\xfe", b"a" * (MAX_TEXT_BYTES + 1)):
        with pytest.raises(AuthorityError) as e:
            r.send("s", bad)
        assert code(e) == "invalid_text"
    assert r.send("s", b"a" * MAX_TEXT_BYTES).text == b"a" * MAX_TEXT_BYTES


def test_tampered_or_unsigned_state_refuses_to_load() -> None:
    r = Rig()
    r.grant()
    r.send("s1")
    assert r.storage.blob is not None
    doc = json.loads(r.storage.blob)
    doc["state"]["events"][0]["text_b64"] = "SEVMTE8="  # edited text
    r.storage.blob = json.dumps(doc).encode()
    with pytest.raises(AuthorityError) as e:
        r.open()
    assert code(e) == "state_invalid"
    r.storage.blob = b"not json"
    with pytest.raises(AuthorityError) as e:
        r.open()
    assert code(e) == "state_invalid"
    # State signed under a different authority key is refused too.
    other = Rig()
    other.send("s1")
    r.storage.blob = other.storage.blob
    with pytest.raises(AuthorityError) as e:
        ChatAuthority(
            signer=HmacSigner(b"z" * 32),
            operator=r.operator,
            storage=r.storage,
            new_id=lambda: "x",
        )
    assert code(e) == "state_invalid"


def test_failed_write_changes_nothing_and_does_not_burn_the_nonce() -> None:
    r = Rig()
    signed = r.cmd("submit_turn", {"send_id": "s1", **SCOPE}, b"hi", nonce="n-x")
    r.storage.fail = True
    with pytest.raises(OSError):
        r.auth.submit_turn(signed)
    assert r.auth._state.events == []
    r.storage.fail = False
    assert r.auth.submit_turn(signed).send_id == "s1"


def test_event_signature_covers_bytes_and_scope() -> None:
    r = Rig()
    ev = r.send("s1", b"hi")
    assert r.auth.verify_event(ev)
    from dataclasses import replace

    assert not r.auth.verify_event(replace(ev, text=b"ho"))
    assert not r.auth.verify_event(replace(ev, chat="c2"))
    assert not r.auth.verify_event(replace(ev, grant_ids=("g",)))


def test_signer_rejects_short_keys() -> None:
    with pytest.raises(ValueError):
        HmacSigner(b"short")
