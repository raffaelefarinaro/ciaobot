"""Tests for ``ciao.fact_candidates``.

The module is now one thing: the provenance record a region write is stamped
with. These pin what it has to be able to say years later from the receipt
alone — where the fact came from, and what is unknown rather than guessed.
"""

from __future__ import annotations

from typing import Any

import pytest

from ciao import fact_candidates as fc
from ciao import memory_proposals as mp


def _proposal(**overrides: Any) -> mp.MemoryProposal:
    base: dict[str, Any] = {
        "target": "memory",
        "text": "Deploys run on Thursdays.",
        "source_section": "User corrections",
        "payload": "",
        "citations": (),
    }
    base.update(overrides)
    return mp.MemoryProposal(**base)


def test_a_cited_bullet_records_its_turns() -> None:
    """A citation is the only evidence a proposal carries; it must survive."""
    candidate = fc.candidate_from_proposal(
        _proposal(text="Deploys run on Thursdays. [idx=3,7]", citations=(3, 7))
    )

    assert candidate.source_message_ids == (3, 7)
    assert candidate.provenance == fc.PROVENANCE_CITED
    assert candidate.destination == "memory"
    assert candidate.section == "User corrections"


def test_an_uncited_bullet_is_recorded_as_unknown() -> None:
    """Unknown provenance is recorded as unknown, never upgraded into a guess.

    A receipt has to be able to say "this archive never said where this came
    from", which is a different fact from "it came from turn 3".
    """
    candidate = fc.candidate_from_proposal(_proposal())

    assert candidate.source_message_ids == ()
    assert candidate.provenance == fc.PROVENANCE_UNKNOWN
    # No transcript means no verdict on whether the turn was the user's.
    assert candidate.attended is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Insights model is sonnet. [as-of:2026-01-02]", ("2026-01-02", "")),
        ("Contract ends 2027-03-04. [expires:2027-03-04]", ("", "2027-03-04")),
        ("Both. [as-of:2026-01-02] [expires:2027-03-04]", ("2026-01-02", "2027-03-04")),
        ("No dates here.", ("", "")),
    ],
)
def test_temporal_bounds_surface_as_fields(text: str, expected: tuple[str, str]) -> None:
    """The tags stay inline on the text and are also lifted into fields."""
    candidate = fc.candidate_from_proposal(_proposal(text=text))

    assert (candidate.as_of, candidate.expires) == expected
    assert candidate.text == text


def test_the_record_names_the_schema_and_policy_it_was_written_under() -> None:
    """A fact saved today must be readable back against what admitted it."""
    candidate = fc.candidate_from_proposal(_proposal(citations=(1,)))
    row = candidate.to_dict()

    assert row["schema"] == fc.SCHEMA_VERSION
    assert row["extraction_version"] == fc.EXTRACTION_VERSION_MARKDOWN
    assert row["policy_version"] == fc.POLICY_VERSION
    # And the row is JSON-safe, which is what the receipt writes.
    assert isinstance(row["source_message_ids"], list)
