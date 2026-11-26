"""The checked-in vectors match the Python reference and replay from the file alone."""

from __future__ import annotations

import json

import pytest

from ciao.chat_authority import (
    AuthorityError,
    Command,
    EcdsaP256Verifier,
    Ed25519Verifier,
    StateMac,
)
from tests import chat_authority_vectors as v


def _doc() -> dict:  # type: ignore[type-arg]
    return json.loads(v.VECTORS_PATH.read_text(encoding="utf-8"))


def test_checked_in_vectors_are_what_the_reference_generates() -> None:
    assert v.VECTORS_PATH.read_text(encoding="utf-8") == v.render(v.build()), (
        "regenerate with: PYTHONPATH=. .venv/bin/python -m tests.chat_authority_vectors"
    )


def test_signing_vectors_verify_from_public_keys_only() -> None:
    doc = _doc()
    operator = EcdsaP256Verifier(bytes.fromhex(doc["keys"]["operator_public_x963_hex"]))
    event_key = Ed25519Verifier(bytes.fromhex(doc["keys"]["event_public_hex"]))
    for raw in doc["signing"]["commands"]:
        signed = v._command_from_json(raw)
        assert signed.command.signing_bytes().hex() == raw["signing_bytes_hex"]
        assert operator.verify(signed.command.signing_bytes(), signed.signature)
        assert raw["signing_bytes_hex"].startswith(b"ciao-chat-authority/command/v1\0".hex())
    ev = doc["signing"]["event"]
    assert event_key.verify(bytes.fromhex(ev["signing_bytes_hex"]), bytes.fromhex(ev["signature_hex"]))
    mac = doc["signing"]["state_mac"]
    assert StateMac(bytes.fromhex(doc["keys"]["state_key_hex"])).verify(
        b"ciao-chat-authority/state/v1\0" + mac["body_canonical"].encode(),
        bytes.fromhex(mac["mac_hex"]),
    )


@pytest.mark.parametrize("name", [s["name"] for s in _doc()["sequences"]])
def test_command_sequences_replay_with_the_recorded_signatures(name: str) -> None:
    (seq,) = [s for s in _doc()["sequences"] if s["name"] == name]
    v.replay(seq)


def test_sequences_cover_the_refusal_codes() -> None:
    codes = {
        step["expect"]["error"]
        for seq in _doc()["sequences"]
        for step in seq["steps"]
        if "error" in step["expect"]
    }
    assert {
        "bad_signature",
        "bad_command",
        "replayed",
        "unknown_grant",
        "id_collision",
        "invalid_text",
        "retry_conflict",
    } <= codes


@pytest.mark.parametrize("rule", _doc()["load_rules"], ids=lambda r: r["name"])
def test_anchor_load_rules(rule: dict) -> None:  # type: ignore[type-arg]
    blob = None if rule["blob"] is None else rule["blob"].encode()
    try:
        v._open([], v.VectorAnchor(rule["anchor"]), v.VectorStorage(blob))
        got = "ok"
    except AuthorityError as exc:
        got = exc.code
    assert got == rule["expect"]


def test_command_context_is_part_of_the_signed_bytes() -> None:
    c = Command("revoke_grant", 1, "op", {"grant_id": "g"})
    assert c.signing_bytes().startswith(b"ciao-chat-authority/command/v1\0")


def test_vectors_carry_the_test_keys_warning_and_the_key_id() -> None:
    doc = _doc()
    assert doc["warning"].startswith("TEST KEYS ONLY")
    assert "TEST KEYS ONLY" in (v.__doc__ or "")
    for raw in doc["signing"]["commands"]:
        assert raw["key_id"] == doc["keys"]["operator_key_id"]
        assert doc["keys"]["operator_key_id"] in bytes.fromhex(raw["signing_bytes_hex"]).decode()
