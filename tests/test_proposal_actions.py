"""Contract tests for ``ciao.proposal_actions``.

The module is the transport-neutral half of a proposal accept/dismiss: the
payload shape the PWA routes return and the one handler that records a
decision in both ledgers. Everything here is exercised without a Starlette
request, which is the point of the extraction — the same functions serve
``ciao/web/routes_api.py`` and ``ciao memory-proposal-dismiss``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from ciao import proposal_actions, proposal_kinds, proposal_outcomes


def _read_events(tmp_path: Path) -> list[dict]:
    path = tmp_path / proposal_outcomes.PROPOSAL_OUTCOMES_NAME
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]


def _queue(tmp_path: Path) -> Path:
    path = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## 2026-09-01\n", encoding="utf-8")
    return path


# ── the service takes no request ──────────────────────────────────────────


def test_module_has_no_request_dependency() -> None:
    """The extraction's whole point: nothing here needs a web framework.

    Asserted on the module's own syntax tree rather than on behaviour because
    the regression this guards against is a later edit reaching for
    ``request.app.state`` for convenience, which would put the CLI back out
    of reach without failing any route test.
    """
    tree = ast.parse(Path(proposal_actions.__file__).read_text(encoding="utf-8"))

    imported: set[str] = set()
    parameters: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            parameters.update(
                arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
            )

    assert "starlette" not in imported
    assert imported <= {"__future__", "dataclasses", "pathlib", "typing", "ciao"}
    assert "request" not in parameters


# ── ProposalActionResult ──────────────────────────────────────────────────


def test_dismiss_result_reports_three_keys() -> None:
    result = proposal_actions.ProposalActionResult(
        id="abc", action="dismiss", dismissed=True
    )
    assert result.as_dict() == {"id": "abc", "action": "dismiss", "dismissed": True}


def test_absent_fields_are_omitted_not_nulled() -> None:
    """A client reading ``"destination" in row`` must not see a null."""
    payload = proposal_actions.ProposalActionResult(
        id="abc", action="dismiss", dismissed=False, error="boom"
    ).as_dict()
    assert payload == {
        "id": "abc",
        "action": "dismiss",
        "dismissed": False,
        "error": "boom",
    }


def test_region_accept_reports_region_promotion_and_usage() -> None:
    row = {"kind": "memory", "leak_warning": True}
    outcome = {
        "ok": True,
        "region": "memory",
        "written": "Ada prefers plain text.",
        "usage": {"chars": 120, "limit": 3000},
    }

    payload = proposal_actions.build_accept_result(
        "abc",
        proposal_kinds.accept_for("memory"),
        row,
        outcome,
        include_usage=True,
    ).as_dict()

    assert payload == {
        "id": "abc",
        "action": "edit_region",
        "dismissed": True,
        "region": "memory",
        "promoted": True,
        "usage": {"chars": 120, "limit": 3000},
        "leak_warning": True,
        "written": "Ada prefers plain text.",
    }


def test_batch_region_accept_omits_usage() -> None:
    """The batch response has never carried usage; the flag keeps it that way."""
    payload = proposal_actions.build_accept_result(
        "abc",
        proposal_kinds.accept_for("profile"),
        {"kind": "profile"},
        {"ok": True, "region": "profile", "usage": {"chars": 1}},
        include_usage=False,
    ).as_dict()

    assert "usage" not in payload
    assert payload["region"] == "profile"
    assert payload["leak_warning"] is False


def test_region_duplicate_resolves_the_row_without_a_written_field() -> None:
    payload = proposal_actions.build_accept_result(
        "abc",
        proposal_kinds.accept_for("memory"),
        {"kind": "memory"},
        {"ok": True, "region": "memory", "duplicate": True},
        include_usage=True,
    ).as_dict()

    assert payload["duplicate"] is True
    assert payload["dismissed"] is True
    assert "written" not in payload


def test_failed_region_write_keeps_the_row_and_carries_the_error() -> None:
    payload = proposal_actions.build_accept_result(
        "abc",
        proposal_kinds.accept_for("memory"),
        {"kind": "memory"},
        {"ok": False, "region": "memory", "error": "over cap"},
        include_usage=False,
    ).as_dict()

    assert payload["dismissed"] is False
    assert payload["promoted"] is False
    assert payload["error"] == "over cap"


def test_failed_region_write_without_an_error_gets_the_default() -> None:
    payload = proposal_actions.build_accept_result(
        "abc",
        proposal_kinds.accept_for("memory"),
        {"kind": "memory"},
        {"ok": False},
        include_usage=False,
    ).as_dict()

    assert payload["error"] == "could not write the region"
    assert payload["region"] == "memory"


def test_a_conflict_rides_along_and_is_told_apart_from_a_refusal() -> None:
    """A batch row refused on a stale body is still promotable.

    The single-row route answers a conflict with its own 409, but a batch has
    no per-row refusal to carry one — so the flag travels on the result the
    builder makes, and the client reopens the preview instead of reporting a
    permanent failure.
    """
    payload = proposal_actions.build_accept_result(
        "abc",
        proposal_kinds.accept_for("memory"),
        {"kind": "memory"},
        {"ok": False, "region": "memory", "error": "stale", "conflict": True},
        include_usage=False,
    ).as_dict()

    assert payload["conflict"] is True
    assert payload["dismissed"] is False
    assert payload["error"] == "stale"


def test_an_ordinary_refusal_carries_no_conflict_key() -> None:
    payload = proposal_actions.build_accept_result(
        "abc",
        proposal_kinds.accept_for("memory"),
        {"kind": "memory"},
        {"ok": False, "region": "memory", "error": "over cap"},
        include_usage=False,
    ).as_dict()

    assert "conflict" not in payload


def test_destination_accepts_report_where_the_fact_landed() -> None:
    for kind, action, destination in (
        ("project", "fold_doc", "Projects/Alpha.md"),
        ("people", "write_people_note", "People/Mo.md"),
        ("learnings", "append_learnings", "Workspace/Learnings.md"),
    ):
        payload = proposal_actions.build_accept_result(
            "abc",
            proposal_kinds.accept_for(kind),
            {"kind": kind},
            {"ok": True, "destination": destination},
            include_usage=True,
        ).as_dict()
        assert payload == {
            "id": "abc",
            "action": action,
            "dismissed": True,
            "promoted": True,
            "destination": destination,
        }


def test_failed_destination_accept_gets_the_destination_default() -> None:
    payload = proposal_actions.build_accept_result(
        "abc",
        proposal_kinds.accept_for("people"),
        {"kind": "people"},
        {"ok": False},
        include_usage=False,
    ).as_dict()

    assert payload["dismissed"] is False
    assert payload["error"] == "could not write the destination"


def test_rehome_accept_reports_the_rows_candidate_not_the_move() -> None:
    """The note is moved by its own handler; the row reports the candidate."""
    payload = proposal_actions.build_accept_result(
        "abc",
        proposal_kinds.accept_for("rehome"),
        {"kind": "rehome", "rehome": {"destination": "work", "justified": True}},
        {"ok": True, "destination": "work/memory-vault/People/Mo.md"},
        include_usage=True,
    ).as_dict()

    assert payload == {
        "id": "abc",
        "action": "move_file",
        "dismissed": True,
        "promoted": False,
        "destination": "work",
        "justified": True,
    }


def test_review_row_has_no_destination_yet() -> None:
    payload = proposal_actions.build_accept_result(
        "abc", proposal_kinds.accept_for("review"), {"kind": "review"}, {},
        include_usage=False,
    ).as_dict()

    assert payload == {
        "id": "abc",
        "action": "route_manually",
        "dismissed": True,
        "promoted": False,
        "destination": "",
        "justified": False,
    }


# ── record_decision ───────────────────────────────────────────────────────


def test_dismiss_records_history_and_tally(tmp_path: Path) -> None:
    from ciao.memory_proposals import read_decisions

    queue = _queue(tmp_path)
    proposal_actions.record_decision(
        queue,
        action="dismiss",
        text="Ada prefers plain text.",
        kind="memory",
        via="pwa",
        workspace="personal",
        source="chat-1",
        proposal_id="abc",
    )

    decisions = read_decisions(queue)
    assert len(decisions) == 1
    assert decisions[0]["text"] == "Ada prefers plain text."
    assert decisions[0]["action"] == "dismissed"
    assert decisions[0]["proposal_id"] == "abc"

    events = _read_events(tmp_path)
    assert [(e["kind"], e["action"], e["workspace"], e["via"]) for e in events] == [
        ("memory", "dismissed", "personal", "pwa")
    ]


def test_accept_records_a_promotion_with_its_destination(tmp_path: Path) -> None:
    from ciao.memory_proposals import read_decisions

    queue = _queue(tmp_path)
    proposal_actions.record_decision(
        queue,
        action="accept",
        text="Ada prefers plain text.",
        kind="memory",
        via="agent",
        workspace="personal",
        destination="ciao:memory",
        outcome="written",
        proposal_id="abc",
    )

    decisions = read_decisions(queue)
    assert decisions[0]["action"] == "accepted"
    assert decisions[0]["destination"] == "ciao:memory"
    assert decisions[0]["outcome"] == "written"

    events = _read_events(tmp_path)
    assert [(e["action"], e["via"]) for e in events] == [("promoted", "agent")]


def test_an_edited_accept_records_its_receipt(tmp_path: Path) -> None:
    """The history keeps the ORIGINAL bullet, so the receipt is the way back.

    Append-time dedupe compares a re-extracted fact against the recorded text,
    which is why an accept promoted with edited wording still records the
    bullet it came from — and why the change receipt has to ride along, or
    nothing can find what actually landed.
    """
    from ciao.memory_proposals import read_decisions

    queue = _queue(tmp_path)
    proposal_actions.record_decision(
        queue,
        action="accept",
        text="Ada prefers plain text.",
        kind="memory",
        via="pwa",
        workspace="personal",
        destination="ciao:memory",
        outcome="written",
        proposal_id="abc",
        receipt_id="rcpt-1",
    )

    decisions = read_decisions(queue)
    assert decisions[0]["text"] == "Ada prefers plain text."
    assert decisions[0]["receipt_id"] == "rcpt-1"


def test_a_dismissal_records_no_receipt(tmp_path: Path) -> None:
    from ciao.memory_proposals import read_decisions

    queue = _queue(tmp_path)
    proposal_actions.record_decision(
        queue,
        action="dismiss",
        text="Ada prefers plain text.",
        kind="memory",
        via="pwa",
        workspace="personal",
    )

    assert read_decisions(queue)[0]["receipt_id"] == ""


def test_non_extraction_kinds_stay_out_of_the_tally(tmp_path: Path) -> None:
    """Skill and rehome rows are decisions, but not extraction outcomes."""
    from ciao.memory_proposals import read_decisions

    queue = _queue(tmp_path)
    for kind in ("skill", "rehome"):
        proposal_actions.record_decision(
            queue,
            action="dismiss",
            text=f"a {kind} row",
            kind=kind,
            via="pwa",
            workspace="personal",
        )

    assert len(read_decisions(queue)) == 2
    assert _read_events(tmp_path) == []
