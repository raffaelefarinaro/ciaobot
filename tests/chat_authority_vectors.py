"""Shared chat authority vectors (#3623 slice 1).

`build()` runs the Python reference over fixed test keys and records the
canonical signing bytes, signatures, command sequences with their results and
refusal codes, the final state digest and MAC, and the anchor load rules.
`tests/vectors/chat_authority_v1.json` is that output; the Swift daemon must
reproduce every value in it. Regenerate after a protocol change, in the same PR
as the Swift change:

    PYTHONPATH=. .venv/bin/python -m tests.chat_authority_vectors

TEST KEYS ONLY - never use outside tests. Every key below is public, checked in
and protects nothing; a real operator, event or state key must never be one of
these.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from ciao.chat_authority import (
    MAX_COUNTER,
    AuthorityError,
    ChatAuthority,
    Command,
    EcdsaP256Signer,
    Ed25519Signer,
    SignedCommand,
    StateMac,
    TurnEvent,
    _canonical,
    _State,
    _state_to_dict,
    _STATE_CONTEXT,
    sign_command,
)

VECTORS_PATH = Path(__file__).parent / "vectors" / "chat_authority_v1.json"
OP_SCALAR = 0x1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF
EVENT_SEED = bytes(range(32))
STATE_KEY = bytes(range(32, 64))
SCOPE = {"workspace": "w1", "project": "p1", "chat": "c1"}


class VectorAnchor:
    def __init__(self, value: int = 0) -> None:
        self.value = value

    def current(self) -> int:
        return self.value

    def advance(self, to: int) -> None:
        if to <= self.value:
            raise ValueError("anchor only moves forward")
        self.value = to


class VectorStorage:
    def __init__(self, blob: bytes | None = None) -> None:
        self.blob = blob

    def read(self) -> bytes | None:
        return self.blob

    def write(self, data: bytes) -> None:
        self.blob = data


def keys() -> dict[str, str]:
    op = EcdsaP256Signer.from_private_scalar(OP_SCALAR)
    ev = Ed25519Signer.from_seed(EVENT_SEED)
    return {
        "operator_private_scalar_hex": f"{OP_SCALAR:064x}",
        "operator_public_x963_hex": op.public_key_x963().hex(),
        "operator_key_id": op.key_id(),
        "event_seed_hex": EVENT_SEED.hex(),
        "event_public_hex": ev.public_key().hex(),
        "state_key_hex": STATE_KEY.hex(),
    }


def _command_json(signed: SignedCommand) -> dict[str, Any]:
    c = signed.command
    return {
        "kind": c.kind,
        "counter": c.counter,
        "principal": c.principal,
        "params": dict(c.params),
        "payload_hex": c.payload.hex(),
        "key_id": c.key_id,
        "signing_bytes_hex": c.signing_bytes().hex(),
        "signature_hex": signed.signature.hex(),
    }


def _command_from_json(raw: dict[str, Any]) -> SignedCommand:
    return SignedCommand(
        Command(
            raw["kind"],
            raw["counter"],
            raw["principal"],
            raw["params"],
            bytes.fromhex(raw["payload_hex"]),
            raw["key_id"],
        ),
        bytes.fromhex(raw["signature_hex"]),
    )


def _open(ids: list[str], anchor: VectorAnchor, storage: VectorStorage) -> ChatAuthority:
    cycle = iter(ids)
    return ChatAuthority(
        event_signer=Ed25519Signer.from_seed(EVENT_SEED),
        state_mac=StateMac(STATE_KEY),
        operator=EcdsaP256Signer.from_private_scalar(OP_SCALAR).verifier(),
        storage=storage,
        anchor=anchor,
        new_id=lambda: next(cycle),
    )


def _do(auth: ChatAuthority, op: str, signed: SignedCommand) -> dict[str, Any]:
    """Run one command; return its recorded outcome."""
    try:
        if op == "create_grant":
            return {"grant_id": auth.create_grant(signed).grant_id}
        if op == "submit_turn":
            ev = auth.submit_turn(signed)
            return {
                "event_id": ev.event_id,
                "turn_id": ev.turn_id,
                "revision": ev.revision,
                "seq": ev.seq,
                "grant_ids": list(ev.grant_ids),
                "event_signature_hex": ev.signature.hex(),
            }
        if op in ("revoke_grant", "withdraw_grant", "withdraw_turn"):
            return {"epoch": getattr(auth, op)(signed)}
    except AuthorityError as exc:
        return {"error": exc.code}
    raise ValueError(op)


def _lookup(auth: ChatAuthority, args: dict[str, Any]) -> dict[str, Any]:
    try:
        look = auth.eligible_events(**args)
    except AuthorityError as exc:
        return {"error": exc.code}
    return {"epoch": look.epoch, "send_ids": [e.send_id for e in look.events]}


class _Recorder:
    def __init__(self, name: str, ids: list[str]) -> None:
        self.name = name
        self.ids = ids
        self.anchor = VectorAnchor()
        self.storage = VectorStorage()
        self.auth = _open(ids, self.anchor, self.storage)
        self.op = EcdsaP256Signer.from_private_scalar(OP_SCALAR)
        self.steps: list[dict[str, Any]] = []

    def cmd(
        self,
        op: str,
        counter: int,
        params: dict[str, Any],
        payload: bytes = b"",
        principal: str = "op",
        signer: EcdsaP256Signer | None = None,
        kind: str | None = None,
    ) -> None:
        signed = sign_command(
            signer or self.op, Command(kind or op, counter, principal, params, payload)
        )
        self.steps.append(
            {
                "op": op,
                "command": _command_json(signed),
                "expect": _do(self.auth, op, signed),
                "anchor_after": self.anchor.value,
            }
        )

    def lookup(self, **args: Any) -> None:
        self.steps.append(
            {"op": "eligible_events", "args": args, "expect": _lookup(self.auth, args)}
        )

    def finish(self) -> dict[str, Any]:
        blob = json.loads(self.storage.blob or b"{}")
        return {
            "name": self.name,
            "ids": self.ids,
            "steps": self.steps,
            "final": {
                "state_counter": self.auth.state_counter,
                "state_canonical_sha256": hashlib.sha256(
                    _canonical(blob["state"])
                ).hexdigest(),
                "state_mac_hex": base64.b64decode(blob["mac_b64"]).hex(),
            },
        }


def _lookup_args(grant_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "grant_id": grant_id,
        "producer": "memory_extraction",
        "limit": 10,
        "destination": "brain",
        "audience": "team",
        **extra,
    }


def _grant_params(**extra: Any) -> dict[str, Any]:
    return {
        "workspace": "w1",
        "project": "p1",
        "chat": "c1",
        "destination": "brain",
        "audience": "team",
        "producers": ["memory_extraction"],
        **extra,
    }


def _sequences() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    r = _Recorder("grant_submit_lookup_revoke", ["g1", "t1", "e1", "t2", "e2"])
    r.cmd("create_grant", 1, _grant_params())
    r.cmd("submit_turn", 2, {"send_id": "s1", **SCOPE}, "caf\u00e9 \U0001f600".encode())
    r.lookup(**_lookup_args("g1"))
    r.lookup(**_lookup_args("g1", destination="elsewhere"))
    r.lookup(**_lookup_args("g1", audience="everyone"))
    r.lookup(**_lookup_args("g1", expected_epoch=5))
    r.cmd("revoke_grant", 3, {"grant_id": "g1"})
    r.lookup(**_lookup_args("g1"))
    out.append(r.finish())

    r = _Recorder("correction_and_withdraw_turn", ["g1", "t1", "e1", "e2", "t2", "e3"])
    r.cmd("create_grant", 1, _grant_params(chat=None))
    r.cmd("submit_turn", 1, {"send_id": "s1", **SCOPE}, b"v1", principal="alice")
    r.cmd(
        "submit_turn", 2, {"send_id": "s2", **SCOPE, "corrects": "t1"}, b"v2", principal="mallory"
    )
    r.cmd("submit_turn", 2, {"send_id": "s2", **SCOPE, "corrects": "t1"}, b"v2", principal="alice")
    r.lookup(**_lookup_args("g1"))
    r.cmd("withdraw_turn", 3, {"turn_id": "t1"}, principal="alice")
    r.lookup(**_lookup_args("g1"))
    out.append(r.finish())

    r = _Recorder("refusals_consume_counters", ["g1", "g1", "t1", "e1"])
    other = EcdsaP256Signer.from_private_scalar(7)
    r.cmd("create_grant", 1, _grant_params())
    r.cmd("create_grant", 2, _grant_params())  # id_collision
    r.cmd("create_grant", 2, _grant_params())  # replayed
    r.cmd("create_grant", 3, _grant_params(), signer=other)  # bad_signature
    r.cmd("revoke_grant", 3, {"grant_id": "g1"}, kind="withdraw_grant")  # bad_command (kind)
    r.cmd("revoke_grant", MAX_COUNTER + 1, {"grant_id": "g1"})  # bad_command (counter cap)
    r.cmd("revoke_grant", 3, {"grant_id": "nope"})  # unknown_grant, consumes 3
    r.cmd("revoke_grant", 3, {"grant_id": "g1"})  # replayed
    r.cmd("submit_turn", 4, {"send_id": "s1", **SCOPE}, b"")  # invalid_text
    r.cmd("submit_turn", 5, {"send_id": "s1", **SCOPE}, b"one")
    r.cmd("submit_turn", 6, {"send_id": "s1", **SCOPE}, b"two")  # retry_conflict
    r.cmd("submit_turn", MAX_COUNTER, {"send_id": "s1", **SCOPE}, b"one")  # same event again
    r.cmd("submit_turn", 7, {"send_id": "s9", **SCOPE}, b"x")  # replayed (past the cap counter)
    out.append(r.finish())
    return out


def _load_rules() -> list[dict[str, Any]]:
    def blob(counter: int, mac_key: bytes = STATE_KEY) -> str:
        body = _state_to_dict(_State(), counter)
        mac = StateMac(mac_key).tag(_STATE_CONTEXT + _canonical(body))
        return _canonical(
            {"state": body, "mac_b64": base64.b64encode(mac).decode("ascii")}
        ).decode("ascii")

    rules: list[tuple[str, str | None, int, str]] = [
        ("equal", blob(5), 5, "ok"),
        ("crash_window_blob_one_behind", blob(4), 5, "ok"),
        ("rollback", blob(3), 5, "state_rollback"),
        ("blob_ahead_of_anchor", blob(6), 5, "state_invalid"),
        ("zero_counter_blob", blob(0), 0, "state_invalid"),
        ("fresh_start", None, 0, "ok"),
        ("missing_blob_anchor_above_zero", None, 1, "state_missing"),
        ("blob_with_anchor_zero", blob(1), 0, "state_invalid"),
        ("wrong_mac_key", blob(5, b"x" * 32), 5, "state_invalid"),
        ("not_json", "not json", 1, "state_invalid"),
    ]
    out = []
    for name, text, anchor, expect in rules:
        result = "ok"
        try:
            _open(
                [],
                VectorAnchor(anchor),
                VectorStorage(None if text is None else text.encode()),
            )
        except AuthorityError as exc:
            result = exc.code
        assert result == expect, (name, result)
        out.append({"name": name, "blob": text, "anchor": anchor, "expect": expect})
    return out


def _signing_vectors() -> dict[str, Any]:
    op = EcdsaP256Signer.from_private_scalar(OP_SCALAR)
    commands = [
        Command("create_grant", 1, "op", _grant_params(), b""),
        Command("create_grant", 2, "op", _grant_params(chat=None), b""),
        Command("submit_turn", 3, "op", {"send_id": "s1", **SCOPE}, "caf\u00e9 \U0001f600".encode()),
        Command("revoke_grant", 4, "op", {"grant_id": "g1"}, b""),
        Command("withdraw_turn", MAX_COUNTER, "op2", {"turn_id": "t1"}, b""),
    ]
    text = b"hello"
    event = TurnEvent(
        event_id="e1",
        turn_id="t1",
        revision=1,
        seq=1,
        send_id="s1",
        workspace="w1",
        project="p1",
        chat="c1",
        principal="op",
        text=text,
        text_sha256=hashlib.sha256(text).hexdigest(),
        opted_out=False,
        grant_ids=("g1",),
        epoch=0,
        signature=b"",
    )
    ev_signer = Ed25519Signer.from_seed(EVENT_SEED)
    return {
        "commands": [_command_json(sign_command(op, c)) for c in commands],
        "event": {
            "fields": {
                "event_id": event.event_id,
                "turn_id": event.turn_id,
                "revision": event.revision,
                "seq": event.seq,
                "send_id": event.send_id,
                "workspace": event.workspace,
                "project": event.project,
                "chat": event.chat,
                "principal": event.principal,
                "text_hex": text.hex(),
                "opted_out": event.opted_out,
                "grant_ids": list(event.grant_ids),
                "epoch": event.epoch,
            },
            "signing_bytes_hex": event.signing_bytes().hex(),
            "signature_hex": ev_signer.sign(event.signing_bytes()).hex(),
        },
        "state_mac": {
            "body_canonical": _canonical(_state_to_dict(_State(), 1)).decode("ascii"),
            "mac_hex": StateMac(STATE_KEY)
            .tag(_STATE_CONTEXT + _canonical(_state_to_dict(_State(), 1)))
            .hex(),
        },
    }


def build() -> dict[str, Any]:
    return {
        "format": 1,
        "notes": "Generated by tests/chat_authority_vectors.py; every port must reproduce it.",
        "warning": "TEST KEYS ONLY - never use outside tests. All keys in this file are public.",
        "max_counter": MAX_COUNTER,
        "keys": keys(),
        "signing": _signing_vectors(),
        "sequences": _sequences(),
        "load_rules": _load_rules(),
    }


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def replay(sequence: dict[str, Any]) -> None:
    """Re-run a recorded sequence with its recorded signatures and compare."""
    anchor = VectorAnchor()
    storage = VectorStorage()
    auth = _open(sequence["ids"], anchor, storage)
    for step in sequence["steps"]:
        if step["op"] == "eligible_events":
            assert _lookup(auth, step["args"]) == step["expect"], step
            continue
        assert _do(auth, step["op"], _command_from_json(step["command"])) == step["expect"], step
        assert anchor.value == step["anchor_after"], step
    blob = json.loads(storage.blob or b"{}")
    final = sequence["final"]
    assert auth.state_counter == final["state_counter"]
    assert hashlib.sha256(_canonical(blob["state"])).hexdigest() == final["state_canonical_sha256"]
    assert base64.b64decode(blob["mac_b64"]).hex() == final["state_mac_hex"]


if __name__ == "__main__":
    VECTORS_PATH.parent.mkdir(exist_ok=True)
    VECTORS_PATH.write_text(render(build()), encoding="utf-8")
    print(f"wrote {VECTORS_PATH}")
