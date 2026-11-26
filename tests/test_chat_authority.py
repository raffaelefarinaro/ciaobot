from __future__ import annotations

import itertools
import json
from typing import Any

import pytest

from ciao.chat_authority import (
    MAX_COUNTER,
    MAX_GRANT_RECORDS,
    MAX_TEXT_BYTES,
    AuthorityError,
    ChatAuthority,
    Command,
    EcdsaP256Signer,
    EcdsaP256Verifier,
    Ed25519Signer,
    Ed25519Verifier,
    SignedCommand,
    StateMac,
    TurnEvent,
    sign_command,
)

STATE_KEY = b"s" * 32
EVENT_SEED = b"e" * 32
OP_SCALAR = 0x1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF
SCOPE = {"workspace": "w1", "project": "p1", "chat": "c1"}


class MemAnchor:
    """In-memory monotonic counter standing in for the root-owned anchor."""

    def __init__(self) -> None:
        self.value = 0
        self.fail = False
        self.calls: list[int] = []

    def current(self) -> int:
        return self.value

    def advance(self, to: int) -> None:
        if self.fail:
            raise OSError("anchor unavailable")
        if to <= self.value:
            raise ValueError("anchor only moves forward")
        self.value = to
        self.calls.append(to)


class MemStorage:
    def __init__(self, anchor: MemAnchor | None = None) -> None:
        self.blob: bytes | None = None
        self.fail = False
        self.anchor = anchor

    def read(self) -> bytes | None:
        return self.blob

    def write(self, data: bytes) -> None:
        if self.fail:
            raise OSError("disk full")
        if self.anchor is not None:
            # Write-ahead: the anchor already holds the counter this blob carries.
            assert json.loads(data)["state"]["state_counter"] == self.anchor.value
        self.blob = data


class Rig:
    def __init__(self, storage: MemStorage | None = None) -> None:
        self.anchor = MemAnchor()
        self.storage = storage or MemStorage(self.anchor)
        self.operator = EcdsaP256Signer.from_private_scalar(OP_SCALAR)
        self.ids = itertools.count(1)
        self.nonces = itertools.count(1)
        self.open()

    def open(self) -> None:
        self.auth = ChatAuthority(
            event_signer=Ed25519Signer.from_seed(EVENT_SEED),
            state_mac=StateMac(STATE_KEY),
            operator=self.operator.verifier(),
            storage=self.storage,
            anchor=self.anchor,
            new_id=lambda: f"id{next(self.ids)}",
        )

    def cmd(
        self,
        kind: str,
        params: dict[str, Any],
        payload: bytes = b"",
        principal: str = "op",
    ) -> SignedCommand:
        n = next(self.nonces)
        return sign_command(self.operator, Command(kind, n, principal, params, payload))

    def grant(
        self,
        chat: str | None = "c1",
        producers: list[str] | None = None,
        principal: str = "op",
    ) -> str:
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
                principal=principal,
            )
        )
        return g.grant_id

    def send(self, send_id: str, text: bytes = b"hello", **extra: Any) -> TurnEvent:
        params = {"send_id": send_id, **SCOPE, **extra}
        return self.auth.submit_turn(self.cmd("submit_turn", params, text))

    def ids_of(self, grant_id: str, limit: int = 50, **kw: Any) -> list[str]:
        kw.setdefault("destination", "brain")
        kw.setdefault("audience", "team")
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
            grant_id=g,
            producer="memory_extraction",
            limit=5,
            destination="brain",
            audience="team",
            expected_epoch=before,
        )
    assert code(e) == "epoch_changed"
    # Revoking again changes nothing; later turns are not enrolled.
    assert r.auth.revoke_grant(r.cmd("revoke_grant", {"grant_id": g})) == after
    r.send("s2")
    assert r.ids_of(g) == []


def test_replay_after_restart_is_refused() -> None:
    r = Rig()
    signed = r.cmd("submit_turn", {"send_id": "s1", **SCOPE}, b"hi")
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
        r.auth.eligible_events(
            grant_id=g, producer="nightly_curation", limit=5, destination="brain", audience="team"
        ).events
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
        EcdsaP256Signer.generate(),
        Command("submit_turn", 1, "op", {"send_id": "s", **SCOPE}, b"hi"),
    )
    with pytest.raises(AuthorityError) as e:
        r.auth.submit_turn(forged)
    assert code(e) == "bad_signature"
    good = r.cmd("submit_turn", {"send_id": "s", **SCOPE}, b"hi")
    tampered = SignedCommand(
        Command("submit_turn", good.command.counter, "op", good.command.params, b"HI"),
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
            event_signer=Ed25519Signer.from_seed(EVENT_SEED),
            state_mac=StateMac(b"z" * 32),
            operator=r.operator.verifier(),
            storage=r.storage,
            anchor=r.anchor,
            new_id=lambda: "x",
        )
    assert code(e) == "state_invalid"


def test_failed_write_changes_nothing_and_does_not_burn_the_nonce() -> None:
    r = Rig()
    signed = r.cmd("submit_turn", {"send_id": "s1", **SCOPE}, b"hi")
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


def test_state_mac_rejects_short_keys() -> None:
    with pytest.raises(ValueError):
        StateMac(b"short")


def test_revoke_and_withdraw_still_work_at_capacity_and_refusals_are_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ciao.chat_authority.MAX_EVENTS", 2)
    r = Rig()
    g = r.grant()
    r.send("s1")
    r.send("s2")
    refused = r.cmd("submit_turn", {"send_id": "s3", **SCOPE}, b"x")
    with pytest.raises(AuthorityError) as e:
        r.auth.submit_turn(refused)
    assert code(e) == "capacity"
    # A refused command is consumed: it cannot be replayed once room appears.
    with pytest.raises(AuthorityError) as e:
        r.auth.submit_turn(refused)
    assert code(e) == "replayed"
    epoch = r.auth.revoke_grant(r.cmd("revoke_grant", {"grant_id": g}))
    assert epoch == r.auth.epoch == 1
    g2 = r.grant()
    assert r.auth.withdraw_grant(r.cmd("withdraw_grant", {"grant_id": g2})) == 2


def test_counter_is_monotonic_per_principal_and_state_stays_small() -> None:
    r = Rig()
    low = r.cmd("submit_turn", {"send_id": "a", **SCOPE}, b"x")
    high = r.cmd("submit_turn", {"send_id": "b", **SCOPE}, b"x")
    r.auth.submit_turn(high)
    with pytest.raises(AuthorityError) as e:
        r.auth.submit_turn(low)
    assert code(e) == "replayed"
    other = r.cmd("submit_turn", {"send_id": "c", **SCOPE}, b"x", principal="op2")
    r.auth.submit_turn(other)
    assert "nonces" not in json.loads(r.storage.blob or b"{}")["state"]


def test_unknown_grant_refusal_consumes_the_counter() -> None:
    r = Rig()
    bad = r.cmd("revoke_grant", {"grant_id": "nope"})
    with pytest.raises(AuthorityError) as e:
        r.auth.revoke_grant(bad)
    assert code(e) == "unknown_grant"
    with pytest.raises(AuthorityError) as e:
        r.auth.revoke_grant(bad)
    assert code(e) == "replayed"


def _cycling_rig(ids: list[str]) -> Rig:
    r = Rig()
    cycle = itertools.cycle(ids)
    r.ids = cycle  # type: ignore[assignment]
    r.new_id = lambda: next(cycle)
    r.auth = ChatAuthority(
        event_signer=Ed25519Signer.from_seed(EVENT_SEED),
        state_mac=StateMac(STATE_KEY),
        operator=r.operator.verifier(),
        storage=r.storage,
        anchor=r.anchor,
        new_id=r.new_id,
    )
    return r


def test_repeated_grant_id_cannot_reactivate_or_overwrite() -> None:
    r = _cycling_rig(["g"])
    g = r.grant()
    r.auth.revoke_grant(r.cmd("revoke_grant", {"grant_id": g}))
    with pytest.raises(AuthorityError) as e:
        r.grant()
    assert code(e) == "id_collision"
    grant = r.auth.grant("g")
    assert grant is not None and grant.status == "revoked"


def test_repeated_turn_or_event_id_is_refused() -> None:
    r = _cycling_rig(["t", "e"])
    r.send("s1")
    with pytest.raises(AuthorityError) as e:
        r.send("s2")
    assert code(e) == "id_collision"
    constant = _cycling_rig(["same"])
    with pytest.raises(AuthorityError) as e:
        constant.send("s1")
    assert code(e) == "id_collision"
    # an id used by a grant cannot be reused by an event either
    mixed = _cycling_rig(["g", "t", "g"])
    mixed.grant()
    with pytest.raises(AuthorityError) as e:
        mixed.send("s1")
    assert code(e) == "id_collision"


def test_retry_must_name_the_same_correction_target() -> None:
    r = Rig()
    t1 = r.send("a")
    t2 = r.send("b")
    r.send("c", b"v2", corrects=t1.turn_id)
    assert r.send("c", b"v2", corrects=t1.turn_id).revision == 2
    for other in (t2.turn_id, None):
        extra = {} if other is None else {"corrects": other}
        with pytest.raises(AuthorityError) as e:
            r.send("c", b"v2", **extra)
        assert code(e) == "retry_conflict"


def test_correction_carries_original_grants_and_never_enrolls_in_newer_ones() -> None:
    r = Rig()
    first = r.send("s1", b"v1")  # captured before any grant
    g = r.grant()
    r.send("s2", b"v2", corrects=first.turn_id)
    assert r.ids_of(g) == []
    # A turn enrolled at capture keeps its grants through a correction, and a
    # grant created later still does not reach it.
    second = r.send("s3", b"x")
    assert r.ids_of(g) == ["s3"]
    r.send("s4", b"x2", corrects=second.turn_id)
    later = r.grant()
    assert r.ids_of(g) == ["s4"]
    assert r.ids_of(later) == []


def test_non_json_params_are_a_bad_command_not_a_crash() -> None:
    r = Rig()
    for params in ({"send_id": "s", **SCOPE, "x": {1, 2}}, {"send_id": float("nan")}):
        cmd = Command("submit_turn", 1, "op", params, b"hi")
        with pytest.raises(AuthorityError) as e:
            r.auth.submit_turn(SignedCommand(cmd, b"sig"))
        assert code(e) == "bad_command"
    with pytest.raises(AuthorityError) as e:
        r.auth.submit_turn(
            SignedCommand(Command("submit_turn", 1, "op", {"send_id": object()}, b"hi"), b"s")
        )
    assert code(e) == "bad_command"


def test_total_stored_bytes_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ciao.chat_authority.MAX_TOTAL_TEXT_BYTES", 10)
    r = Rig()
    r.send("s1", b"123456")
    with pytest.raises(AuthorityError) as e:
        r.send("s2", b"12345")
    assert code(e) == "capacity"
    r.send("s3", b"1234")


def test_principal_count_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ciao.chat_authority.MAX_PRINCIPALS", 1)
    r = Rig()
    r.send("s1")
    with pytest.raises(AuthorityError) as e:
        r.auth.submit_turn(
            r.cmd("submit_turn", {"send_id": "s2", **SCOPE}, b"x", principal="op2")
        )
    assert code(e) == "capacity"


# ---- hardening (#3623 slice 1, #3662) ---------------------------------


def _cmd_with(
    signer: EcdsaP256Signer, counter: int, kind: str, params: dict[str, Any], payload: bytes = b""
) -> SignedCommand:
    return sign_command(signer, Command(kind, counter, "op", params, payload))


def test_operator_verifier_holds_no_signing_ability() -> None:
    r = Rig()
    verifier = r.operator.verifier()
    assert isinstance(verifier, EcdsaP256Verifier)
    assert not hasattr(verifier, "sign")
    # A signer cannot be handed in as the operator verifier.
    with pytest.raises(ValueError):
        ChatAuthority(
            event_signer=Ed25519Signer.from_seed(EVENT_SEED),
            state_mac=StateMac(STATE_KEY),
            operator=r.operator,  # type: ignore[arg-type]
            storage=r.storage,
            anchor=r.anchor,
            new_id=lambda: "x",
        )
    # Rebuilding the verifier from the public bytes alone verifies commands.
    public = EcdsaP256Verifier(r.operator.public_key_x963())
    signed = r.cmd("submit_turn", {"send_id": "s", **SCOPE}, b"hi")
    assert public.verify(signed.command.signing_bytes(), signed.signature)
    assert len(signed.signature) == 64
    assert not public.verify(signed.command.signing_bytes(), signed.signature[:-1])
    assert not public.verify(signed.command.signing_bytes() + b"x", signed.signature)
    with pytest.raises(ValueError):
        EcdsaP256Verifier(b"\x04" + b"\x01" * 64)  # not a point on the curve


def test_state_and_event_keys_are_separate_and_not_interchangeable() -> None:
    event = Ed25519Signer.from_seed(EVENT_SEED)
    with pytest.raises(ValueError):
        ChatAuthority(
            event_signer=StateMac(STATE_KEY),  # type: ignore[arg-type]
            state_mac=StateMac(STATE_KEY),
            operator=EcdsaP256Signer.generate().verifier(),
            storage=MemStorage(),
            anchor=MemAnchor(),
            new_id=lambda: "x",
        )
    r = Rig()
    r.send("s1")
    # An event signature is not a state MAC and the reverse (domain separation).
    mac = StateMac(STATE_KEY)
    sig = event.sign(b"ciao-chat-authority/state/v1\0body")
    assert not mac.verify(b"ciao-chat-authority/state/v1\0body", sig)
    assert not Ed25519Verifier(event.public_key()).verify(b"x", mac.tag(b"x"))
    # Whoever holds only the event public key can check events but not forge them.
    ev = r.send("s1")
    verifier = Ed25519Verifier(event.public_key())
    assert verifier.verify(ev.signing_bytes(), ev.signature)
    assert not hasattr(verifier, "sign")


def test_lookup_is_bound_to_destination_and_audience() -> None:
    r = Rig()
    g = r.grant()
    r.send("s1")
    assert r.ids_of(g) == ["s1"]
    look = r.auth.eligible_events(
        grant_id=g, producer="memory_extraction", limit=5, destination="brain", audience="team"
    )
    assert (look.grant_id, look.destination, look.audience) == (g, "brain", "team")
    assert r.ids_of(g, destination="other") == []
    assert r.ids_of(g, audience="everyone") == []
    # A mismatch is checked at lookup, before any event is picked, and echoes the ask.
    look = r.auth.eligible_events(
        grant_id=g, producer="memory_extraction", limit=5, destination="other", audience="team"
    )
    assert look.events == () and look.destination == "other"
    with pytest.raises(TypeError):
        r.auth.eligible_events(grant_id=g, producer="memory_extraction", limit=5)  # type: ignore[call-arg]


def test_counter_is_capped_below_two_to_the_53() -> None:
    r = Rig()
    assert MAX_COUNTER == 2**53 - 1
    for bad in (MAX_COUNTER + 1, 2**62):
        with pytest.raises(AuthorityError) as e:
            r.auth.submit_turn(_cmd_with(r.operator, bad, "submit_turn", {"send_id": "s", **SCOPE}, b"x"))
        assert code(e) == "bad_command"
    # The refused giant counter did not lock the principal out.
    assert r.send("s").send_id == "s"
    top = _cmd_with(r.operator, MAX_COUNTER, "submit_turn", {"send_id": "t", **SCOPE}, b"x")
    assert r.auth.submit_turn(top).send_id == "t"


def test_only_live_grants_count_and_ended_unreferenced_grants_are_pruned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ciao.chat_authority.MAX_GRANTS", 2)
    r = Rig()
    a = r.grant(chat="a")
    b = r.grant(chat="b")
    with pytest.raises(AuthorityError) as e:
        r.grant(chat="c")
    assert code(e) == "capacity"
    r.auth.revoke_grant(r.cmd("revoke_grant", {"grant_id": a}))
    r.auth.withdraw_grant(r.cmd("withdraw_grant", {"grant_id": b}))
    # Both slots are free again, however many grants were ended before.
    for i in range(6):
        g = r.grant(chat=f"n{i}")
        r.auth.revoke_grant(r.cmd("revoke_grant", {"grant_id": g}))
    assert len(r.auth._state.grants) <= 2 + 1
    assert r.auth.grant(a) is None  # pruned
    live = r.grant(chat="live")
    assert r.auth.grant(live) is not None


def test_pruning_keeps_grants_that_stored_events_still_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ciao.chat_authority.MAX_GRANT_RECORDS", 3)
    r = Rig()
    g = r.grant()
    r.send("s1")  # enrolled under g
    r.auth.revoke_grant(r.cmd("revoke_grant", {"grant_id": g}))
    r.grant(chat="x0")
    # The referenced, ended grant stays; the total record cap then refuses.
    assert r.auth.grant(g) is not None
    r.grant(chat="x1")
    assert len(r.auth._state.grants) == 3
    with pytest.raises(AuthorityError) as e:
        r.grant(chat="x3")
    assert code(e) == "capacity"
    assert MAX_GRANT_RECORDS >= 500


def test_correction_must_come_from_the_original_principal() -> None:
    r = Rig()
    g = r.grant()
    first = r.auth.submit_turn(r.cmd("submit_turn", {"send_id": "s1", **SCOPE}, b"v1", principal="alice"))
    with pytest.raises(AuthorityError) as e:
        r.auth.submit_turn(
            r.cmd(
                "submit_turn",
                {"send_id": "s2", **SCOPE, "corrects": first.turn_id},
                b"forged",
                principal="mallory",
            )
        )
    assert code(e) == "bad_command"
    assert r.ids_of(g) == ["s1"]
    ok = r.auth.submit_turn(
        r.cmd(
            "submit_turn",
            {"send_id": "s3", **SCOPE, "corrects": first.turn_id},
            b"v2",
            principal="alice",
        )
    )
    assert ok.revision == 2


def test_principal_capacity_refuses_a_new_revoker_but_not_known_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ciao.chat_authority.MAX_PRINCIPALS", 1)
    r = Rig()
    g = r.grant()
    with pytest.raises(AuthorityError) as e:
        r.auth.revoke_grant(r.cmd("revoke_grant", {"grant_id": g}, principal="op2"))
    assert code(e) == "capacity"
    assert r.auth.revoke_grant(r.cmd("revoke_grant", {"grant_id": g})) == 1


def test_anchor_advances_before_the_state_write_and_failure_stops_the_command() -> None:
    r = Rig()
    r.send("s1")
    r.send("s2")
    assert r.anchor.calls == [1, 2]
    assert json.loads(r.storage.blob or b"")["state"]["state_counter"] == 2
    assert r.auth.state_counter == 2
    # Anchor refusal: nothing is stored, the counter is not burnt, state unchanged.
    signed = r.cmd("submit_turn", {"send_id": "s3", **SCOPE}, b"x")
    r.anchor.fail = True
    with pytest.raises(OSError):
        r.auth.submit_turn(signed)
    assert r.anchor.value == 2 and r.auth.state_counter == 2
    r.anchor.fail = False
    assert r.auth.submit_turn(signed).send_id == "s3"


def test_refusals_also_advance_the_anchor() -> None:
    r = Rig()
    with pytest.raises(AuthorityError):
        r.auth.revoke_grant(r.cmd("revoke_grant", {"grant_id": "nope"}))
    assert r.anchor.value == 1 and r.auth.state_counter == 1


def test_crash_between_anchor_and_blob_is_accepted_once() -> None:
    r = Rig()
    r.send("s1")
    signed = r.cmd("submit_turn", {"send_id": "s2", **SCOPE}, b"x")
    r.storage.fail = True  # anchor advances, blob write fails: the crash window
    with pytest.raises(OSError):
        r.auth.submit_turn(signed)
    r.storage.fail = False
    assert r.anchor.value == 2
    r.open()  # blob counter 1, anchor 2: accepted
    assert r.auth.state_counter == 2
    assert [e.send_id for e in r.auth._state.events] == ["s1"]
    r.send("s3")  # moves on; counter keeps advancing from the anchor
    assert r.anchor.value == 3
    r.open()
    assert r.auth.state_counter == 3


def test_rollback_missing_blob_and_forged_counter_leave_the_authority_closed() -> None:
    r = Rig()
    r.send("s1")
    old = r.storage.blob
    r.send("s2")
    r.send("s3")
    # A validly signed older blob: counter 1 against anchor 3.
    r.storage.blob = old
    with pytest.raises(AuthorityError) as e:
        r.open()
    assert code(e) == "state_rollback"
    # A missing blob while the anchor is above zero is not a fresh start.
    r.storage.blob = None
    with pytest.raises(AuthorityError) as e:
        r.open()
    assert code(e) == "state_missing"
    # A blob ahead of the anchor cannot be real.
    fresh = Rig()
    fresh.send("s1")
    fresh.anchor.value = 0
    with pytest.raises(AuthorityError) as e:
        fresh.open()
    assert code(e) == "state_invalid"
    # A missing blob with the anchor at zero is the only fresh initialisation.
    empty = Rig()
    assert empty.auth.state_counter == 0 and empty.auth.epoch == 0


def test_two_failed_writes_in_a_row_fail_closed_on_reload() -> None:
    r = Rig()
    r.send("s1")
    r.storage.fail = True
    for sid in ("s2", "s3"):
        with pytest.raises(OSError):
            r.send(sid)
    r.storage.fail = False
    with pytest.raises(AuthorityError) as e:
        r.open()
    assert code(e) == "state_rollback"
