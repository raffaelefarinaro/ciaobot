"""Transport-neutral result type and decision recorder for a proposal action.

Accepting or dismissing a queued proposal happens on two surfaces: the PWA
routes in ``ciao/web/routes_api.py`` (one row, or a batch) and the CLI's
``ciao memory-proposal-dismiss``. Both surfaces used to inline the same two
things — the payload shape an accept reports, and the pair of ledgers a
decision has to land in — and the copies had already drifted.

This module owns both:

* :class:`ProposalActionResult` is the shape the PWA renders for one resolved
  row, built once in :func:`build_accept_result` for the single-row and batch
  routes alike;
* :func:`record_decision` is the one place a decision is written, to the
  dedupe sidecar (``memory_proposals``) and to the outcomes tally
  (``proposal_outcomes``), in that order.

Nothing here touches Starlette: no ``Request``, no response objects, no app
state. A caller passes the queue path, the row's fields and what the accept
did, so the CLI can use the same handler the routes do and a test can exercise
either without building a request.

What stays with the callers: reading the request or argv, resolving which row
an id or a substring names, performing the promotion itself (that lives in
``ciao/web/proposal_service.py``, which owns the queue files), and mapping a
result onto an HTTP status or an exit code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ciao import proposal_kinds

# Every accept descriptor a queued kind can carry. Spelled out here rather
# than passed as a bare action string so a caller cannot hand this module a
# region it read off a descriptor that has none.
AcceptDescriptor = (
    proposal_kinds.RegionAccept
    | proposal_kinds.RehomeAccept
    | proposal_kinds.DocFoldAccept
    | proposal_kinds.PeopleAccept
    | proposal_kinds.LearningsAccept
    | proposal_kinds.ReviewAccept
)

# The accept actions that write to a named destination file rather than to a
# bounded region of the workspace guide. They share one result shape.
_DESTINATION_ACTIONS = ("fold_doc", "write_people_note", "append_learnings")


@dataclass(frozen=True)
class ProposalActionResult:
    """What one accept or dismiss did with one proposal row.

    Fields default to ``None`` to mean "not part of this row's answer", which
    is how :meth:`as_dict` decides what to emit: a dismissed bullet reports
    three keys, a region write reports its region and usage, a re-home reports
    where the note went. The wire payload is therefore unchanged from the
    hand-built dictionaries this type replaces — the type exists so both
    routes and their tests name the same contract, not to widen it.
    """

    id: str
    action: str
    dismissed: bool
    promoted: bool | None = None
    region: str | None = None
    usage: Mapping[str, Any] | None = None
    leak_warning: bool | None = None
    written: str | None = None
    duplicate: bool = False
    destination: str | None = None
    justified: bool | None = None
    already_moved: bool | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        """The JSON object the PWA receives for this row."""
        payload: dict[str, object] = {
            "id": self.id,
            "action": self.action,
            "dismissed": self.dismissed,
        }
        if self.region is not None:
            payload["region"] = self.region
        if self.promoted is not None:
            payload["promoted"] = self.promoted
        if self.usage is not None:
            payload["usage"] = dict(self.usage)
        if self.leak_warning is not None:
            payload["leak_warning"] = self.leak_warning
        if self.written:
            payload["written"] = self.written
        if self.duplicate:
            payload["duplicate"] = True
        if self.destination is not None:
            payload["destination"] = self.destination
        if self.justified is not None:
            payload["justified"] = self.justified
        if self.already_moved is not None:
            payload["already_moved"] = self.already_moved
        if self.error is not None:
            payload["error"] = self.error
        return payload


def build_accept_result(
    proposal_id: str,
    accept: AcceptDescriptor,
    row: Mapping[str, Any],
    outcome: Mapping[str, Any],
    *,
    include_usage: bool,
) -> ProposalActionResult:
    """Describe one accepted row from the promotion's own outcome.

    ``outcome`` is what the promotion helper in ``proposal_service`` returned,
    or ``{}`` for an accept performed elsewhere (a re-home move runs before the
    queue-file grouping). An outcome that carries ``ok: False`` is a failure:
    the bullet stayed queued, so ``dismissed`` is false and the error rides
    along. An absent ``ok`` means nothing was written here, which is a success.

    ``include_usage`` is the one difference between the two routes: the
    single-row response reports the region's usage after the write, the batch
    response never has. Passing it explicitly keeps that asymmetry visible
    instead of hiding it in two divergent builders.
    """
    failed = "ok" in outcome and not outcome["ok"]
    dismissed = not failed
    if isinstance(accept, proposal_kinds.RegionAccept):
        usage = outcome.get("usage", {}) if include_usage else None
        return ProposalActionResult(
            id=proposal_id,
            action=accept.action,
            dismissed=dismissed,
            region=str(outcome.get("region", accept.region)),
            promoted=bool(outcome.get("ok")),
            usage=usage,
            leak_warning=bool(row.get("leak_warning", False)),
            written=outcome.get("written") or None,
            duplicate=bool(outcome.get("duplicate")),
            error=(
                str(outcome.get("error", "could not write the region"))
                if failed
                else None
            ),
        )
    if accept.action in _DESTINATION_ACTIONS:
        return ProposalActionResult(
            id=proposal_id,
            action=accept.action,
            dismissed=dismissed,
            promoted=bool(outcome.get("ok")),
            destination=str(outcome.get("destination", "")),
            error=(
                str(outcome.get("error", "could not write the destination"))
                if failed
                else None
            ),
        )
    # Re-home and route_manually: nothing was written into a region or a doc.
    # A re-home's move is performed by its own handler and reported through the
    # row's candidate destination, which is what the panel already shows.
    rehome = row.get("rehome") or {}
    return ProposalActionResult(
        id=proposal_id,
        action=accept.action,
        dismissed=dismissed,
        promoted=False,
        destination=str(rehome.get("destination", "")),
        justified=bool(rehome.get("justified", False)),
    )


def record_decision(
    queue: Path,
    *,
    action: str,
    text: str,
    kind: str,
    via: str,
    workspace: str = "",
    source: str = "",
    destination: str = "",
    outcome: str = "",
    proposal_id: str = "",
) -> None:
    """Record one resolved proposal in both ledgers the queue depends on.

    Order matters and is fixed here: the decision history first, the outcomes
    tally second. The history is what ``append_proposals`` dedupes against, so
    a decision missing from it means the next curator pass re-files the fact
    the operator just resolved; the tally only counts kinds and is trimmed.

    ``action`` is ``"accept"`` or ``"dismiss"``; anything else is treated as a
    dismissal by the history and refused by the tally, which is the behaviour
    each caller already had. The tally is written only for the extraction
    kinds — ``skill`` rows come from skill evolution and ``rehome`` rows from
    vault hygiene, and neither measures the memory pipeline.

    Imports are deferred so a test that patches ``ciao.memory_proposals`` or
    ``ciao.proposal_outcomes`` still sees its patch honoured here.
    """
    from ciao import proposal_outcomes
    from ciao.memory_proposals import record_dismissal, record_promotion

    accepted = action == "accept"
    recorder = record_promotion if accepted else record_dismissal
    recorder(
        queue,
        text=text,
        kind=kind,
        via=via,
        source=source,
        destination=destination,
        outcome=outcome,
        proposal_id=proposal_id,
    )
    if not proposal_outcomes.is_extraction_kind(kind):
        return
    proposal_outcomes.record(
        kind=kind,
        action="promoted" if accepted else "dismissed",
        workspace=workspace,
        via=via,
    )
