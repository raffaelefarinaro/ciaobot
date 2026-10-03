"""The normalized source contract, and what it owes a consumer.

The adapter's own tests pin how a Claude Code file is read; these pin the shape
that read produces — that a session survives a snapshot, that the vocabularies
are closed so a consumer can be written against them, that nothing invents a
date, and that the one thing every downstream consumer needs first (the first
thing the user said) is a contract member rather than a re-derivation.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from ciao.import_decouple import (
    AMBIGUOUS,
    CIAOBOT_OWN,
    CIAO_CONTEXT_BEGIN,
    EXTERNAL,
    canonical_provider,
    classify_session,
)
from ciao.import_sources.claude_code import read_claude_code_session
from ciao.import_sources.contract import (
    MAX_SESSION_BYTES,
    OMISSION_KINDS,
    PROVIDER_CLAUDE_CODE,
    PROVIDER_OPENCODE,
    ROLE_ASSISTANT,
    ROLE_USER,
    ROLES,
    ContractError,
    NormalizedMessage,
    NormalizedSession,
    Omission,
    SourceRef,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "import"


def _session(**overrides: object) -> NormalizedSession:
    source = SourceRef(
        provider=PROVIDER_CLAUDE_CODE,
        source_id="5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b",
        project_hint="-synthetic-checkout",
        path="/synthetic/-synthetic-checkout/5e6f7a8b.jsonl",
    )
    fields: dict[str, object] = {
        "source": source,
        "messages": (
            NormalizedMessage(role=ROLE_USER, text="what did we decide?", anchor="u1"),
            NormalizedMessage(role=ROLE_ASSISTANT, text="that", anchor="a1"),
        ),
        "omissions": (Omission(kind=OMISSION_KINDS[0], count=2),),
        "truncated": False,
    }
    fields.update(overrides)
    return NormalizedSession(**fields)  # type: ignore[arg-type]


def test_the_cap_is_a_bound_the_contract_owns() -> None:
    """``MAX_SESSION_BYTES`` is positive and lives with the contract, not an adapter.

    An adapter that read more than this would break a promise the contract makes
    to everything downstream of it, which is why the number is defined here and
    the adapter reads it from here.
    """
    assert isinstance(MAX_SESSION_BYTES, int)
    assert 0 < MAX_SESSION_BYTES <= 64 * 1024 * 1024


def test_a_session_round_trips_through_its_snapshot() -> None:
    """``to_json``/``from_json`` are lossless, so C6 can store a batch and read it back.

    Both fixtures are round-tripped, not a hand-built object: the snapshot has
    to carry what a real read produces (empty texts beside an omission, a null
    timestamp, a zero-message session would be the next one to break).
    """
    for name in ("claude_code_session_minimal.jsonl", "claude_code_session_branched.jsonl"):
        session = read_claude_code_session(FIXTURES / name)

        payload = session.to_json()
        assert json.loads(json.dumps(payload)) == payload, "the snapshot must be JSON"
        assert NormalizedSession.from_json(payload) == session


def test_a_snapshot_keeps_an_unknown_date_unknown() -> None:
    """``None`` survives the round trip as ``None``, and a real date as itself.

    The difference matters to a consumer: "this turn has no known date" and "this
    turn has an empty date" are different facts, and a snapshot that collapsed
    them would leave a caller guessing whether a date was lost.
    """
    unknown = NormalizedSession.from_json(_session().to_json())
    dated = NormalizedSession.from_json(
        _session(
            messages=(
                NormalizedMessage(
                    role=ROLE_USER,
                    text="what did we decide?",
                    anchor="u1",
                    timestamp="2026-01-02T03:04:05Z",
                ),
            )
        ).to_json()
    )

    assert unknown.messages[0].timestamp is None
    assert dated.messages[0].timestamp == "2026-01-02T03:04:05Z"


def test_the_vocabulary_is_closed() -> None:
    """A role, provider or omission kind outside the contract is refused.

    These are the words a consumer (the consent UI in particular) is written
    against, so an unclosed set would make every such consumer handle a value it
    had never been shown. A typo has to fail here rather than mint an entry.
    """
    with pytest.raises(ContractError):
        NormalizedMessage(role="model", text="hi", anchor="a1")
    with pytest.raises(ContractError):
        SourceRef(provider="anthropic", source_id="sid")
    with pytest.raises(ContractError):
        Omission(kind="sidechain", count=1)
    with pytest.raises(ContractError):
        Omission(kind=OMISSION_KINDS[0], count=-1)
    with pytest.raises(ContractError):
        Omission(kind=OMISSION_KINDS[0], count=True)

    # The closed sets are what make the refusals above a fixed list.
    assert ROLES == (ROLE_USER, ROLE_ASSISTANT, "other")
    assert PROVIDER_CLAUDE_CODE != PROVIDER_OPENCODE


def test_a_source_and_a_message_must_name_themselves() -> None:
    """A blank ``source_id`` or ``anchor`` is refused.

    Both are the only way back to the source: ``source_id`` is the dedupe and
    provenance key, ``anchor`` is what makes a message citable to one turn. A
    session carrying neither would be quotable and fileable without saying
    where from.
    """
    with pytest.raises(ContractError):
        SourceRef(provider=PROVIDER_CLAUDE_CODE, source_id="")
    with pytest.raises(ContractError):
        NormalizedMessage(role=ROLE_USER, text="hi", anchor="")

    assert SourceRef(provider=PROVIDER_CLAUDE_CODE, source_id="sid").project_hint == ""
    assert NormalizedMessage(role=ROLE_USER, text="hi", anchor="a1").timestamp is None


def test_a_snapshot_that_cannot_be_read_whole_is_refused() -> None:
    """``from_json`` raises rather than half-filling a session.

    A payload that lost an anchor or an omission kind would otherwise come back
    as a session that looks complete, which is the one thing the contract exists
    to prevent: a batch that cannot state what it dropped must not be runnable.
    """
    payload = _session().to_json()

    with pytest.raises(ContractError):
        NormalizedSession.from_json({**payload, "messages": [{"role": "user", "text": "x"}]})
    with pytest.raises(ContractError):
        NormalizedSession.from_json({**payload, "omissions": [{"kind": "sidechain", "count": 1}]})
    with pytest.raises(ContractError):
        NormalizedSession.from_json({**payload, "omissions": {"kind": "isMeta"}})
    with pytest.raises(ContractError):
        NormalizedSession.from_json({**payload, "truncated": "yes"})
    with pytest.raises(ContractError):
        NormalizedSession.from_json({**payload, "source": "claude_code"})

    assert NormalizedSession.from_json(payload) == _session()


def test_a_normalized_session_is_a_value() -> None:
    """Frozen, so a consumer cannot edit the record it was handed in place.

    The same session is read by the extractor, shown for consent and written to
    a store; an in-place edit by any of them would leave the other two holding
    something nobody produced.
    """
    session = _session()

    with pytest.raises(dataclasses.FrozenInstanceError):
        session.truncated = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        session.source.source_id = "other"  # type: ignore[misc]


def test_omission_counts_state_what_happened_and_nothing_else() -> None:
    """The mapping is the whole record: a kind not in it was not dropped.

    A kind counted zero is left out rather than carried, so a consumer can render
    the mapping as-is instead of filtering it, and a missing kind cannot be read
    as "we looked and there was nothing".
    """
    session = _session(
        omissions=(Omission(kind=OMISSION_KINDS[0], count=2), Omission(kind=OMISSION_KINDS[1], count=0))
    )

    assert session.omission_counts() == {OMISSION_KINDS[0]: 2}
    assert _session(omissions=()).omission_counts() == {}


def test_a_repeated_omission_kind_is_summed_not_overwritten() -> None:
    """Two rows of one kind are that much more of it, however they got grouped.

    ``omissions`` is a tuple rather than a mapping, so nothing stops two callers
    appending the same kind. The count is *how much was left out*, which is a
    quantity: overwriting the first with the second would report three dropped
    entries as one and make the consent screen understate what the import did.
    """
    session = _session(
        omissions=(
            Omission(kind=OMISSION_KINDS[0], count=2),
            Omission(kind=OMISSION_KINDS[1], count=0),
            Omission(kind=OMISSION_KINDS[0], count=3),
        )
    )

    assert session.omission_counts() == {OMISSION_KINDS[0]: 5}


def test_the_first_user_turn_is_the_thing_classify_session_reads() -> None:
    """The contract hands the classifier its input, so no consumer re-derives it.

    :func:`ciao.import_decouple.classify_session` decides whether a session is
    Ciaobot's own from a recorded id, a Ciaobot chat id, or a marker in the
    session's first user turn. The first of those two are registry facts; the
    third is a field on this contract, which is why ``first_user_turn`` exists
    here rather than being searched for in each consumer. The provider names in
    the two vocabularies meet through ``canonical_provider``, so an adapter's
    ``claude_code`` is matched against a Ciaobot record's ``claude``.
    """
    session = read_claude_code_session(FIXTURES / "claude_code_session_branched.jsonl")

    assert canonical_provider(PROVIDER_CLAUDE_CODE) == "claude"
    assert session.first_user_turn == "Where does the parser live?"
    # Nothing recorded against it and no marker in its own first turn: this is
    # the user's own history, and it is the only answer an import may act on.
    assert (
        classify_session(
            session.source.provider,
            session.source.source_id,
            session.first_user_turn,
            known_ids=[],
        )
        == EXTERNAL
    )
    # The same session, recorded in Ciaobot's own registry under the other
    # vocabulary: ``claude`` against the adapter's ``claude_code``, matched
    # rather than compared literally.
    known = [("claude", session.source.source_id)]
    assert classify_session(
        session.source.provider, session.source.source_id, "", known_ids=known
    ) == CIAOBOT_OWN


def test_a_session_with_no_user_turn_classifies_as_ambiguous() -> None:
    """Nothing readable is ``ambiguous``, and ``ambiguous`` is never imported.

    A session whose messages are all assistant turns, and one with no messages
    at all, both leave the classifier nothing to read. Reporting those as the
    user's own history would be the failure this contract is downstream of, so
    the empty first turn is a real answer rather than a default.
    """
    assistant_only = _session(
        messages=(NormalizedMessage(role=ROLE_ASSISTANT, text="unprompted", anchor="a1"),)
    )
    empty = _session(messages=())

    assert assistant_only.first_user_turn == ""
    assert empty.first_user_turn == ""
    assert classify_session(PROVIDER_CLAUDE_CODE, "sid", assistant_only.first_user_turn, []) == AMBIGUOUS
    assert classify_session(PROVIDER_CLAUDE_CODE, "sid", empty.first_user_turn, []) == AMBIGUOUS


def test_a_ciaobot_marker_in_the_first_turn_is_the_one_signal_in_the_file() -> None:
    """A session Ciaobot drove is recognised from its own text, with no row for it.

    The recorded-id rules cannot see a session Ciaobot drove without keeping a
    chat row for it, which is the whole reason the marker is checked against the
    first user turn rather than against a registry this contract does not read.
    """
    session = _session(
        messages=(
            NormalizedMessage(
                role=ROLE_USER, text=f"{CIAO_CONTEXT_BEGIN}\nseeded", anchor="u1"
            ),
        )
    )

    assert (
        classify_session(PROVIDER_CLAUDE_CODE, "sid", session.first_user_turn, []) == CIAOBOT_OWN
    )