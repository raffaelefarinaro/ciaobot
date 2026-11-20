"""The one read-only model judgment of a prepared stale note (#3237).

The provider is always an injected fake: ``ciao.curation_judgments.run_oneshot``
is monkeypatched, so no test reaches a real provider. Inputs are built directly
as :class:`ciao.curation_evidence.PreparedNote` values; nothing reads HOME, the
live engine or a real vault.
"""

from __future__ import annotations

import json

import pytest

from ciao import curation_evidence as ce
from ciao import curation_judgments as cj
from ciao import curation_run as cr
from ciao.providers.oneshot import OneShotError

REL = "People/Sofia.md"
REV = "rev-1"
RUN = "run-1"
BATCH = "batch-1"
ITEM = cr.item_key(cr.PASS_STALE_NOTE, REL)
LIMIT = 16 * 1024


def _ref(
    sidecar_id: str,
    *,
    proposal_id: str = "",
    status: str = "open",
) -> ce.ProposalReference:
    return ce.ProposalReference(
        sidecar_id=sidecar_id,
        proposal_id=proposal_id,
        status=status,
        revision="current",
        operation="replace",
        expected_revision=REV,
        settled_at="" if status == "open" else "2026-11-01T00:00:00Z",
        accepted=status == "settled",
    )


def _prepared(*refs: ce.ProposalReference) -> ce.PreparedNote:
    return ce.PreparedNote(item_key=ITEM, relative_path=REL, revision=REV, references=refs)


def _result(*, drop: tuple[str, ...] = (), **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "item_key": ITEM,
        "relative_path": REL,
        "revision": REV,
        "verdict": "propose",
        "refs": ["ref-open"],
        "section": "## Role",
        "change": "Add the new role.",
    }
    for key in drop:
        result.pop(key, None)
    result.update(overrides)
    return result


def _verdict(verdict: str, **overrides: object) -> dict[str, object]:
    """A result for a non-propose verdict: the propose-only fields are not carried."""
    return _result(drop=("section", "change"), verdict=verdict, **overrides)


def _reply(result: object | None = None, **envelope: object) -> str:
    body: dict[str, object] = {
        "schema_version": 1,
        "run_id": RUN,
        "batch_id": BATCH,
        "role": "vault_verification",
        "results": [_result() if result is None else result],
    }
    body.update(envelope)
    return json.dumps(body)


class _Fake:
    """Stands in for ``run_oneshot``: records each call, then returns or raises."""

    def __init__(self, reply: object = "", exc: BaseException | None = None) -> None:
        self.reply = reply
        self.exc = exc
        self.calls: list[dict[str, object]] = []

    async def __call__(self, prompt: str, **kwargs: object) -> str:
        self.calls.append({"prompt": prompt, **kwargs})
        if self.exc is not None:
            raise self.exc
        assert isinstance(self.reply, str)
        return self.reply


def _install(monkeypatch: pytest.MonkeyPatch, fake: _Fake) -> _Fake:
    monkeypatch.setattr(cj, "run_oneshot", fake)
    return fake


async def _judge(
    prepared: object,
    *,
    independent_refs: frozenset[str] = frozenset(),
) -> cj.JudgmentOutcome:
    return await cj.judge_prepared_note(
        prepared,  # type: ignore[arg-type]
        run_id=RUN,
        batch_id=BATCH,
        model="sonnet",
        provider="claude",
        timeout_s=30.0,
        independent_refs=independent_refs,
    )


def _invalid_reason(outcome: cj.JudgmentOutcome) -> str:
    assert isinstance(outcome, cj.Invalid), outcome
    return outcome.attempt.reason


# ── Provider call count ────────────────────────────────────────────────────


async def test_eligible_row_makes_exactly_one_bounded_attempt(monkeypatch):
    fake = _install(monkeypatch, _Fake(_reply()))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert isinstance(outcome, cj.Validated)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["max_retries"] == 0
    assert call["timeout_s"] == 30.0
    assert call["provider"] == "claude"
    assert call["model"] == "sonnet"


async def test_two_eligible_rows_make_two_attempts(monkeypatch):
    fake = _install(monkeypatch, _Fake(_reply()))
    await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    "selection",
    [ce.NoSelectedNote(), ce.DeferredNote(reason="the note changed since it was planned")],
    ids=["no-selection", "deferred"],
)
async def test_no_selection_or_deferred_preparation_makes_no_call(monkeypatch, selection):
    fake = _install(monkeypatch, _Fake(_reply()))
    outcome = await _judge(selection)
    assert isinstance(outcome, cj.NothingToJudge)
    assert fake.calls == []


# ── References: known versus fabricated ────────────────────────────────────


async def test_reference_in_input_is_accepted(monkeypatch):
    _install(monkeypatch, _Fake(_reply()))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert isinstance(outcome, cj.Validated)
    assert outcome.judgment.refs == ("ref-open",)


async def test_fabricated_reference_is_refused(monkeypatch):
    _install(monkeypatch, _Fake(_reply(_result(refs=["ref-invented"]))))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "the result names a reference that is not in the input"


async def test_too_many_references_is_refused(monkeypatch):
    refs = [f"ref-{i}" for i in range(ce.MAX_REFERENCES + 1)]
    prepared = _prepared(*(_ref(r, proposal_id=f"row-{r}") for r in refs))
    _install(monkeypatch, _Fake(_reply(_result(refs=refs))))
    outcome = await _judge(prepared)
    assert _invalid_reason(outcome) == "the result's references are malformed"


async def test_repeated_reference_is_refused(monkeypatch):
    _install(monkeypatch, _Fake(_reply(_result(refs=["ref-open", "ref-open"]))))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "the result's references are malformed"


# ── Parser: size, JSON shape, duplicate keys ───────────────────────────────


async def test_reply_at_exactly_the_ceiling_passes_and_one_more_byte_refuses(monkeypatch):
    base = _reply()
    prepared = _prepared(_ref("ref-open", proposal_id="row-1"))
    _install(monkeypatch, _Fake(base + " " * (LIMIT - len(base))))
    assert isinstance(await _judge(prepared), cj.Validated)
    _install(monkeypatch, _Fake(base + " " * (LIMIT + 1 - len(base))))
    assert _invalid_reason(await _judge(prepared)) == "the reply is larger than the read ceiling"


async def test_malformed_json_is_refused(monkeypatch):
    _install(monkeypatch, _Fake('{"schema_version": 1,'))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "the reply is not one JSON object"


async def test_non_finite_number_is_refused(monkeypatch):
    _install(monkeypatch, _Fake(_reply().replace('"schema_version": 1', '"schema_version": NaN')))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "the reply is not one JSON object"


async def test_duplicate_key_is_refused(monkeypatch):
    raw = '{"schema_version": 1, "schema_version": 1, "run_id": "run-1"}'
    _install(monkeypatch, _Fake(raw))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "the reply repeats a JSON key"


async def test_duplicate_key_inside_a_result_is_refused(monkeypatch):
    raw = _reply().replace('"verdict": "propose"', '"verdict": "propose", "verdict": "defer"')
    _install(monkeypatch, _Fake(raw))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "the reply repeats a JSON key"


# ── Envelope and echo ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "envelope",
    [
        {"schema_version": True},
        {"schema_version": "1"},
        {"schema_version": 2},
        {"run_id": "run-other"},
        {"batch_id": "batch-other"},
        {"role": "memory_review"},
        {"extra": "field"},
        {"results": []},
        {"results": [_result(), _result()]},
    ],
    ids=[
        "bool-version", "string-version", "version-2", "other-run", "other-batch",
        "other-role", "extra-envelope-key", "no-result", "two-results",
    ],
)
async def test_envelope_mismatch_is_refused(monkeypatch, envelope):
    _install(monkeypatch, _Fake(_reply(**envelope)))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert isinstance(outcome, cj.Invalid)


async def test_stale_input_echo_is_refused(monkeypatch):
    """A reply about an older revision cannot be applied to the current one."""
    _install(monkeypatch, _Fake(_reply(_result(revision="rev-0"))))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "the result does not echo the prepared note"


async def test_item_and_path_echo_are_checked(monkeypatch):
    _install(monkeypatch, _Fake(_reply(_result(item_key="stale_note:other.md"))))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "the result does not echo the prepared note"
    _install(monkeypatch, _Fake(_reply(_result(relative_path="People/Other.md"))))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "the result does not echo the prepared note"


# ── Result shape: unknown and inapplicable keys ────────────────────────────


async def test_unknown_result_key_is_refused(monkeypatch):
    _install(monkeypatch, _Fake(_reply(_result(confidence=0.9))))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "the result carries unexpected or missing keys"


async def test_key_belonging_to_another_verdict_is_refused(monkeypatch):
    _install(monkeypatch, _Fake(_reply(_result(proposal_ref="row-1"))))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "the result carries unexpected or missing keys"


async def test_unknown_verdict_is_refused(monkeypatch):
    _install(monkeypatch, _Fake(_reply(_result(verdict="approve"))))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "the verdict is not one of the closed set"


# ── Verdict-specific fields ────────────────────────────────────────────────


async def test_propose_needs_section_and_bounded_change(monkeypatch):
    prepared = _prepared(_ref("ref-open", proposal_id="row-1"))
    _install(monkeypatch, _Fake(_reply(_result(section=""))))
    assert "the proposed section" in _invalid_reason(await _judge(prepared))
    _install(monkeypatch, _Fake(_reply(_result(change="x" * (cj.MAX_CHANGE_CHARS + 1)))))
    assert "the proposed change" in _invalid_reason(await _judge(prepared))
    _install(monkeypatch, _Fake(_reply(_result(change="x" * cj.MAX_CHANGE_CHARS))))
    outcome = await _judge(prepared)
    assert isinstance(outcome, cj.Validated)
    assert outcome.judgment.section == "## Role"


async def test_attach_needs_exact_live_proposal_reference(monkeypatch):
    prepared = _prepared(
        _ref("ref-open", proposal_id="row-1"),
        _ref("ref-settled", status="settled"),
    )
    _install(
        monkeypatch,
        _Fake(_reply(_verdict("attach", refs=["ref-open"], proposal_ref="row-1"))),
    )
    outcome = await _judge(prepared)
    assert isinstance(outcome, cj.Validated)
    assert outcome.judgment.proposal_ref == "row-1"

    for bad in (
        {"proposal_ref": "row-9", "refs": ["ref-open"]},
        {"proposal_ref": "row-1", "refs": ["ref-settled"]},
        {"proposal_ref": "row-1", "refs": []},
    ):
        _install(monkeypatch, _Fake(_reply(_verdict("attach", **bad))))
        outcome = await _judge(prepared)
        assert _invalid_reason(outcome) == "attach needs an exact live proposal reference"


@pytest.mark.parametrize("verdict", ["conflict", "defer"])
async def test_conflict_and_defer_need_a_reason(monkeypatch, verdict):
    prepared = _prepared(_ref("ref-open", proposal_id="row-1"))
    _install(monkeypatch, _Fake(_reply(_result(drop=("section", "change", "reason"), verdict=verdict, refs=[]))))
    assert _invalid_reason(await _judge(prepared)) == "the result carries unexpected or missing keys"
    _install(monkeypatch, _Fake(_reply(_verdict(verdict, refs=[], reason="  "))))
    assert "the reason" in _invalid_reason(await _judge(prepared))
    _install(
        monkeypatch,
        _Fake(_reply(_verdict(verdict, refs=[], reason="x" * (cj.MAX_REASON_CHARS + 1)))),
    )
    assert "the reason" in _invalid_reason(await _judge(prepared))
    _install(monkeypatch, _Fake(_reply(_verdict(verdict, refs=[], reason="Two sources disagree."))))
    outcome = await _judge(prepared)
    assert isinstance(outcome, cj.Validated)
    assert outcome.judgment.reason == "Two sources disagree."


# ── verified: only with synthetic independent evidence (D4) ────────────────


async def test_verified_is_rejected_for_pilot_input(monkeypatch):
    """A note plus its open proposal is not independent corroboration."""
    _install(monkeypatch, _Fake(_reply(_verdict("verified", refs=["ref-open"]))))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "verified needs independent evidence in the input"


async def test_verified_is_rejected_with_no_references(monkeypatch):
    _install(monkeypatch, _Fake(_reply(_verdict("verified", refs=[]))))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert _invalid_reason(outcome) == "verified needs independent evidence in the input"


async def test_verified_is_accepted_with_synthetic_independent_evidence(monkeypatch):
    prepared = _prepared(
        _ref("ref-open", proposal_id="row-1"),
        _ref("ref-synthetic-source"),
    )
    _install(
        monkeypatch,
        _Fake(_reply(_verdict("verified", refs=["ref-synthetic-source"]))),
    )
    outcome = await _judge(prepared, independent_refs=frozenset({"ref-synthetic-source"}))
    assert isinstance(outcome, cj.Validated)
    assert outcome.judgment.verdict == "verified"


async def test_verified_citing_only_non_independent_refs_is_rejected(monkeypatch):
    prepared = _prepared(
        _ref("ref-open", proposal_id="row-1"),
        _ref("ref-synthetic-source"),
    )
    _install(monkeypatch, _Fake(_reply(_verdict("verified", refs=["ref-open"]))))
    outcome = await _judge(prepared, independent_refs=frozenset({"ref-synthetic-source"}))
    assert _invalid_reason(outcome) == "verified needs independent evidence in the input"


# ── Timeout, unavailable, and no secrets in the record ─────────────────────


async def test_timeout_is_a_timed_out_outcome(monkeypatch):
    _install(monkeypatch, _Fake(exc=TimeoutError()))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert isinstance(outcome, cj.TimedOut)
    assert outcome.attempt.status == "timed_out"


async def test_provider_failure_is_unavailable_and_keeps_detail_out(monkeypatch):
    secret = "SECRET-PROMPT-AND-KEY-sk-123"
    _install(monkeypatch, _Fake(exc=OneShotError(secret, transient=False)))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert isinstance(outcome, cj.Unavailable)
    assert outcome.attempt.status == "unavailable"
    assert secret not in repr(outcome)


async def test_invalid_reply_text_never_reaches_the_record(monkeypatch):
    _install(monkeypatch, _Fake('{"schema_version": 1, "leak": "SECRET-REPLY-TEXT"'))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert isinstance(outcome, cj.Invalid)
    assert "SECRET-REPLY-TEXT" not in repr(outcome)


async def test_attempt_record_names_run_batch_item_and_provider(monkeypatch):
    _install(monkeypatch, _Fake(_reply()))
    outcome = await _judge(_prepared(_ref("ref-open", proposal_id="row-1")))
    assert isinstance(outcome, cj.Validated)
    attempt = outcome.attempt
    assert (attempt.run_id, attempt.batch_id, attempt.item_key) == (RUN, BATCH, ITEM)
    assert (attempt.provider, attempt.model, attempt.status) == ("claude", "sonnet", "validated")
