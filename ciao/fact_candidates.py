"""The provenance record a region write is stamped with.

A fact saved into a bounded region has to be able to answer, years later and
from the receipt alone: where did this come from, and what admitted it. A
:class:`FactCandidate` is that answer — the destination it claimed, the
transcript message ids it cited, its temporal bounds, and whether anything
about it was actually checked. Every field is either read from the proposal or
left explicitly unknown; nothing here invents evidence a bullet did not carry.

:func:`candidate_from_proposal` is the entry point. It lifts an
already-parsed :class:`~ciao.memory_proposals.MemoryProposal` without
re-parsing it, and :func:`ciao.memory_proposals._provenance_row` turns the
result into the row ``ciao.memory_receipts`` commits beside the region change.
A bullet with an ``[idx=N]`` citation yields ``provenance="cited"``; a bullet
without one yields ``provenance="unknown"`` and an empty
``source_message_ids``. Unknown provenance is recorded as unknown; it is never
upgraded into a guess — the receipt has to be able to say "this archive never
said where this came from", which is a different fact from "it came from
turn 3".

**External anchors are a separate field, and they do not change that.** A
conversation imported from somebody else's agent (the ``provider:session_id:anchor``
tag the import path builds) has a **string** anchor — an OpenCode ``msg_…`` id, a
Claude Code uuid — while :attr:`FactCandidate.source_message_ids` is
``tuple[int, ...]`` and parsed with ``int()``. The type mismatch is not a matter
of taste, so :attr:`FactCandidate.source_anchors` carries them instead and the
integer field stays empty for an import: there is no Ciaobot transcript index to
cite. :attr:`FactCandidate.provenance` therefore stays ``"unknown"`` there, which
is exactly this module's own rule — the anchors carry the attribution, the
provenance verdict stays what the integer evidence supports, and neither is
upgraded into the other.

The evidence policy that used to check a candidate against the normalized
transcript (``validate_candidate``, and the ``evidence-policy/v1`` verdicts) was
removed in #627 with the archive-time auto-apply it guarded. ``POLICY_VERSION``
stays on the record because receipts written before that still carry it, and a
new write has to keep naming a policy rather than a blank.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ── Versions ──────────────────────────────────────────────────────────────

SCHEMA_VERSION = "fact-candidate/v1"
"""Bumped when the candidate record's field set changes. Stamped onto every
candidate and into the region receipt, so a fact saved today can be read back
against the schema it was written under."""

EXTRACTION_VERSION_MARKDOWN = "insights-markdown/v1"
"""The tagged-Markdown extraction contract the fact was read from."""

POLICY_VERSION = "evidence-policy/v1"
"""The rule set that used to admit or refuse the fact. Recorded next to the
saved fact so an accepted fact says which policy admitted it. Since #627
nothing re-checks it; the field survives so a receipt written under the old
policy is still readable as the same shape."""


# ── Provenance vocabulary ─────────────────────────────────────────────────

PROVENANCE_CITED = "cited"
PROVENANCE_UNKNOWN = "unknown"


# ── The record ────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class FactCandidate:
    """One fact plus the evidence it claims.

    ``text`` is the bullet as written, minus only the citation and destination
    tags — the temporal tags stay inline so a candidate renders back to the
    exact bullet it was parsed from, and :attr:`as_of` / :attr:`expires`
    surface the same dates as fields.

    ``attended`` is tri-state on purpose: ``True`` when a cited turn is one the
    user typed, ``False`` when every cited turn is assistant output or an
    automation turn, and ``None`` when there is no transcript to decide it
    from. ``None`` is *not* ``False``; a hand-filed proposal has to be able to
    say "unknown" without that reading as "the assistant made it up".

    ``source_anchors`` holds ``provider:source_id:anchor`` tags — the external
    conversation and the message inside it — for a fact that came from an
    imported conversation. It is additive beside :attr:`source_message_ids`, not
    a second spelling of it: the integer field is a Ciaobot transcript index and
    is empty for an import because there is no such transcript, while an anchor
    is a provider's own string id. See the module docstring.
    """

    text: str
    destination: str
    payload: str = ""
    section: str = ""
    source_message_ids: tuple[int, ...] = ()
    source_anchors: tuple[str, ...] = ()
    evidence_excerpt: str = ""
    as_of: str = ""
    expires: str = ""
    attended: bool | None = None
    provenance: str = PROVENANCE_UNKNOWN
    schema: str = SCHEMA_VERSION
    extraction_version: str = EXTRACTION_VERSION_MARKDOWN
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe record."""
        return {
            "schema": self.schema,
            "text": self.text,
            "destination": self.destination,
            "payload": self.payload,
            "section": self.section,
            "source_message_ids": list(self.source_message_ids),
            "source_anchors": list(self.source_anchors),
            "evidence_excerpt": self.evidence_excerpt,
            "as_of": self.as_of,
            "expires": self.expires,
            "attended": self.attended,
            "provenance": self.provenance,
            "extraction_version": self.extraction_version,
            "policy_version": self.policy_version,
        }


# ── Lifting a proposal ────────────────────────────────────────────────────

_AS_OF_RE = re.compile(r"\[as-of:\s*(\d{4}-\d{2}-\d{2})\]", re.IGNORECASE)
_EXPIRES_RE = re.compile(r"\[expires:\s*(\d{4}-\d{2}-\d{2})\]", re.IGNORECASE)


def external_anchor(section: str) -> str:
    """The ``provider:source_id:anchor`` tag in ``section``, or ``""``.

    A proposal sourced from an imported conversation carries that tag in its
    ``source_section`` — which is where ``MemoryProposal.as_bullet`` flattens it
    into the bullet's ``_(from: …)_`` tail, and where it still is after a round
    trip through the queue file.

    It is recognised in code and not by a colon count, by two rules together:
    the leading segment must be a provider the import contract knows
    (:data:`ciao.import_sources.contract.KNOWN_PROVIDERS`, which is what
    :class:`~ciao.import_sources.contract.SourceRef` admits and therefore what
    the tag was built from), and the whole tag must pass
    :func:`ciao.import_decouple.assert_external_provenance`, which refuses a
    Ciaobot chat id however it is written. A heading, a file name, a bare
    session id and a path with two colons in it are all refusals of the same
    rule rather than four spellings to remember.

    Imported lazily: ``memory_proposals`` calls into this module while it stamps
    a receipt, so a module-level import from here would close that cycle.
    """
    from ciao.import_decouple import ProvenanceNotExternal, assert_external_provenance
    from ciao.import_sources.contract import KNOWN_PROVIDERS

    tag = section.strip()
    provider = tag.split(":", 1)[0].strip()
    if provider not in KNOWN_PROVIDERS:
        return ""
    try:
        assert_external_provenance(tag)
    except ProvenanceNotExternal:
        return ""
    return tag


def candidate_from_proposal(proposal: Any) -> FactCandidate:
    """Lift an already-parsed :class:`~ciao.memory_proposals.MemoryProposal`.

    Typed loosely to keep the import one-directional: ``memory_proposals``
    calls into this module when it stamps a receipt, so a module-level import
    of its dataclass here would close the cycle.

    ``source_anchors`` is lifted from the ``provider:session_id:anchor`` tag in
    the proposal's ``source_section``. That is where an import row's anchor
    already is: C4 builds the tag there, ``MemoryProposal.as_bullet`` flattens it
    into the bullet's ``_(from: ...)_`` tail, and the review surface reads it back
    out of the file — so the one place to read it from is the one place every
    row carries it, whether it was filed yesterday or a year ago.

    :attr:`FactCandidate.provenance` is decided by the integer ids alone, so an
    imported fact stays ``"unknown"`` there rather than borrowing the anchors'
    confidence; and :attr:`FactCandidate.attended` stays ``None``, because an
    imported conversation has no Ciaobot turn to judge.
    """
    text = str(proposal.text)
    ids = tuple(int(i) for i in getattr(proposal, "citations", ()) or ())
    as_of = _AS_OF_RE.search(text)
    expires = _EXPIRES_RE.search(text)
    section = str(getattr(proposal, "source_section", "") or "")
    tag = external_anchor(section)
    anchors = (tag,) if tag else ()
    return FactCandidate(
        text=text,
        destination=str(proposal.target),
        payload=str(getattr(proposal, "payload", "") or ""),
        section=section,
        source_message_ids=ids,
        source_anchors=anchors,
        evidence_excerpt="",
        as_of=as_of.group(1) if as_of else "",
        expires=expires.group(1) if expires else "",
        attended=None,
        provenance=PROVENANCE_CITED if ids else PROVENANCE_UNKNOWN,
        extraction_version=EXTRACTION_VERSION_MARKDOWN,
    )
