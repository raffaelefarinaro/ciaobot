"""One read-only model judgment of one prepared stale note (#3237).

:func:`judge_prepared_note` asks the configured provider, at most once, for a
verdict on the note that :mod:`ciao.curation_evidence` prepared. It writes
nothing, files no proposal and involves no scheduler. The reply is untrusted
text: it is validated here against the input, and only a validated
:class:`Judgment` leaves this module.

Verdicts: ``propose`` (an exact section and a bounded change), ``attach`` (an
exact live proposal reference), ``conflict`` and ``defer`` (a reason is
required), and ``verified``. A ``verified`` verdict needs independent evidence:
the note text or an open proposal alone never supports it. The caller names the
reference ids of such evidence through ``independent_refs``. A
:class:`PreparedNote` carries no corroboration beyond the target and its related
proposals, so with no independent references the pilot rejects ``verified``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from ciao.curation_evidence import (
    MAX_REFERENCES,
    PreparedNote,
    PreparedOutcome,
    ProposalReference,
)
from ciao.providers.oneshot import run_oneshot

#: The raw reply is read up to this many bytes; one more refuses.
MAX_JUDGMENT_BYTES = 16 * 1024
SCHEMA_VERSION = 1
ROLE = "vault_verification"
VERDICTS = frozenset({"propose", "attach", "conflict", "defer", "verified"})
MAX_REASON_CHARS = 300
MAX_SECTION_CHARS = 200
MAX_CHANGE_CHARS = 1000
MAX_PROPOSAL_REF_CHARS = 128

_ENVELOPE_KEYS = frozenset({"schema_version", "run_id", "batch_id", "role", "results"})
_RESULT_KEYS = frozenset({"item_key", "relative_path", "revision", "verdict", "refs"})
_VERDICT_KEYS: dict[str, frozenset[str]] = {
    "propose": frozenset({"section", "change"}),
    "attach": frozenset({"proposal_ref"}),
    "conflict": frozenset({"reason"}),
    "defer": frozenset({"reason"}),
    "verified": frozenset(),
}

_SYSTEM_PROMPT = (
    "You judge one stale note in a personal knowledge vault. You have no tools and "
    "change nothing. Reply with exactly one JSON object and no other text: "
    '{"schema_version": 1, "run_id": <run_id>, "batch_id": <batch_id>, '
    '"role": "vault_verification", "results": [<one result>]}. '
    "The result has item_key, relative_path, revision (echoed exactly from the input), "
    "verdict (one of propose, attach, conflict, defer, verified) and refs (a list of "
    "reference ids copied from the input, possibly empty). Add section and change for "
    "propose, proposal_ref (an open reference's proposal_id) for attach, and reason "
    "for conflict and defer. Use no other keys."
)


@dataclass(frozen=True, slots=True)
class Judgment:
    """A validated result, with every field checked against the prepared input."""

    item_key: str
    relative_path: str
    revision: str
    verdict: str
    refs: tuple[str, ...]
    reason: str = ""
    section: str = ""
    change: str = ""
    proposal_ref: str = ""


@dataclass(frozen=True, slots=True)
class JudgmentAttempt:
    """The per-attempt record. It holds fixed reasons only, never prompt or reply text."""

    run_id: str
    batch_id: str
    item_key: str
    provider: str
    model: str
    status: Literal["validated", "invalid", "timed_out", "unavailable"]
    reason: str


@dataclass(frozen=True, slots=True)
class Validated:
    attempt: JudgmentAttempt
    judgment: Judgment


@dataclass(frozen=True, slots=True)
class Invalid:
    attempt: JudgmentAttempt


@dataclass(frozen=True, slots=True)
class TimedOut:
    attempt: JudgmentAttempt


@dataclass(frozen=True, slots=True)
class Unavailable:
    attempt: JudgmentAttempt


@dataclass(frozen=True, slots=True)
class NothingToJudge:
    """Preparation selected no note or deferred one; no provider call was made."""

    reason: str


JudgmentOutcome = Validated | Invalid | TimedOut | Unavailable | NothingToJudge


class _Refused(Exception):
    """An internal refusal; its message is a fixed reason safe to hand back."""


class _DuplicateKey(ValueError):
    """A JSON object repeats a key."""


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _no_constants(name: str) -> object:
    """``NaN`` and the infinities are not JSON; refuse them as the parser's own error."""
    raise ValueError(name)


def _text(value: object, limit: int, what: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise _Refused(f"{what} is missing or longer than allowed")
    return value


def parse_judgment(
    raw: object,
    prepared: PreparedNote,
    *,
    run_id: str,
    batch_id: str,
    independent_refs: frozenset[str] = frozenset(),
) -> Judgment:
    """Validate one reply against the prepared input, or refuse with a fixed reason."""
    if type(raw) is not str:
        raise _Refused("the reply is not text")
    if len(raw.encode("utf-8")) > MAX_JUDGMENT_BYTES:
        raise _Refused("the reply is larger than the read ceiling")
    try:
        data = json.loads(
            raw, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants
        )
    except _DuplicateKey as exc:
        raise _Refused("the reply repeats a JSON key") from exc
    except ValueError as exc:
        raise _Refused("the reply is not one JSON object") from exc

    if type(data) is not dict or set(data) != _ENVELOPE_KEYS:
        raise _Refused("the reply envelope has unexpected keys")
    if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
        raise _Refused("the reply schema version is not 1")
    if data["run_id"] != run_id or data["batch_id"] != batch_id:
        raise _Refused("the reply names another run or batch")
    if data["role"] != ROLE:
        raise _Refused("the reply names another role")
    results = data["results"]
    if type(results) is not list or len(results) != 1:
        raise _Refused("the reply must carry exactly one result")
    result = results[0]
    if type(result) is not dict:
        raise _Refused("the result is not an object")

    verdict = result.get("verdict")
    if type(verdict) is not str or verdict not in VERDICTS:
        raise _Refused("the verdict is not one of the closed set")
    if set(result) != _RESULT_KEYS | _VERDICT_KEYS[verdict]:
        raise _Refused("the result carries unexpected or missing keys")
    if (
        result["item_key"] != prepared.item_key
        or result["relative_path"] != prepared.relative_path
        or result["revision"] != prepared.revision
    ):
        raise _Refused("the result does not echo the prepared note")

    raw_refs = result["refs"]
    if (
        type(raw_refs) is not list
        or len(raw_refs) > MAX_REFERENCES
        or not all(type(ref) is str for ref in raw_refs)
        or len(set(raw_refs)) != len(raw_refs)
    ):
        raise _Refused("the result's references are malformed")
    known = {ref.sidecar_id for ref in prepared.references}
    if any(ref not in known for ref in raw_refs):
        raise _Refused("the result names a reference that is not in the input")
    refs: tuple[str, ...] = tuple(raw_refs)

    reason = section = change = proposal_ref = ""
    if verdict == "propose":
        section = _text(result["section"], MAX_SECTION_CHARS, "the proposed section")
        change = _text(result["change"], MAX_CHANGE_CHARS, "the proposed change")
    elif verdict == "attach":
        proposal_ref = _text(result["proposal_ref"], MAX_PROPOSAL_REF_CHARS, "the proposal reference")
        if not _live_reference_cited(prepared.references, proposal_ref, refs):
            raise _Refused("attach needs an exact live proposal reference")
    elif verdict in ("conflict", "defer"):
        reason = _text(result["reason"], MAX_REASON_CHARS, "the reason")
    elif not set(refs) & (independent_refs & known):
        # Note text and an open proposal are not independent of the note they describe.
        raise _Refused("verified needs independent evidence in the input")

    return Judgment(
        item_key=prepared.item_key,
        relative_path=prepared.relative_path,
        revision=prepared.revision,
        verdict=verdict,
        refs=refs,
        reason=reason,
        section=section,
        change=change,
        proposal_ref=proposal_ref,
    )


def _live_reference_cited(
    references: tuple[ProposalReference, ...],
    proposal_ref: str,
    cited: tuple[str, ...],
) -> bool:
    """True when an open reference carries ``proposal_ref`` and the result cites it."""
    return any(
        ref.status == "open" and ref.proposal_id == proposal_ref and ref.sidecar_id in cited
        for ref in references
    )


def _build_prompt(prepared: PreparedNote, *, run_id: str, batch_id: str) -> str:
    lines = [
        f"run_id: {run_id}",
        f"batch_id: {batch_id}",
        f"item_key: {prepared.item_key}",
        f"relative_path: {prepared.relative_path}",
        f"revision: {prepared.revision}",
        "related_references:",
    ]
    for ref in prepared.references:
        lines.append(
            f"- ref={ref.sidecar_id} status={ref.status} revision={ref.revision} "
            f"proposal_id={ref.proposal_id or '-'} operation={ref.operation}"
        )
    if not prepared.references:
        lines.append("- none")
    return "\n".join(lines)


async def judge_prepared_note(
    prepared: PreparedOutcome,
    *,
    run_id: str,
    batch_id: str,
    model: str,
    provider: str,
    timeout_s: float,
    independent_refs: frozenset[str] = frozenset(),
) -> JudgmentOutcome:
    """Judge one prepared note with one provider attempt, or make no call at all."""
    if not isinstance(prepared, PreparedNote):
        return NothingToJudge(reason="preparation selected nothing to judge")

    def attempt(status: Literal["validated", "invalid", "timed_out", "unavailable"], reason: str) -> JudgmentAttempt:
        return JudgmentAttempt(
            run_id=run_id,
            batch_id=batch_id,
            item_key=prepared.item_key,
            provider=provider,
            model=model,
            status=status,
            reason=reason,
        )

    prompt = _build_prompt(prepared, run_id=run_id, batch_id=batch_id)
    try:
        reply = await run_oneshot(
            prompt,
            system_prompt=_SYSTEM_PROMPT,
            model=model,
            provider=provider,
            timeout_s=timeout_s,
            max_retries=0,
        )
    except TimeoutError:
        return TimedOut(attempt("timed_out", "the model call timed out"))
    except Exception:  # noqa: BLE001 -- failure detail can carry prompt or reply text
        return Unavailable(attempt("unavailable", "the model call failed"))

    try:
        judgment = parse_judgment(
            reply,
            prepared,
            run_id=run_id,
            batch_id=batch_id,
            independent_refs=independent_refs,
        )
    except _Refused as exc:
        return Invalid(attempt("invalid", str(exc)))
    return Validated(attempt("validated", "the reply was validated"), judgment)
