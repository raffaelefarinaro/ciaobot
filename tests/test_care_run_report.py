"""The persisted care run report and its dispatch-id read (#3647, Refs #3056).

Pure selection and join first, then the strict ``last_run`` schema, both end
paths that write the report, and the read endpoint. Vaults live under
``tmp_path``; the route runs against stub managers.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import curation_cycle as cc
from ciao import curation_run as cr
from ciao.care_run_report import (
    MAX_ITEMS,
    MAX_RECEIPT_IDS,
    bounded_report,
    join_last_run,
    report_valid,
    select_run,
)

CARE_ID = "system-memory-curation@personal"
DISPATCH = "2026-11-25~abc"
BASE_RUN = {
    "status": "ok",
    "finished_at": "2026-11-25T00:30:00Z",
    "planned": 1,
    "completed": 1,
    "deferred": 0,
    "reasons": [],
}


def _item(key: str = "k1", status: str = "applied", **extra: Any) -> dict[str, Any]:
    return {"item_key": key, "pass_id": "proposals", "status": status, "detail": "", "receipt_ids": [], **extra}


def _report(items: list[dict] | None = None) -> dict[str, Any]:
    return {"items": items if items is not None else [_item()], "truncated": False, "weekly": {"marker": "advanced"}}


def _row(dispatch: str = DISPATCH, *, status: str = "ok", chat: str = "chat-1", **extra: Any) -> dict[str, Any]:
    return {
        "job": "schedule_dispatch",
        "status": status,
        "extra": {"schedule_id": CARE_ID, "workspace": "personal", "dispatch_id": dispatch, "chat_id": chat, **extra},
    }


# -- select_run: exact dispatch id, never a newer row ------------------------


def test_no_row_is_pending() -> None:
    assert select_run([_row("other")], CARE_ID, "personal", DISPATCH).state == "pending"
    assert select_run([], CARE_ID, "personal", DISPATCH).state == "pending"


def test_two_rows_for_one_dispatch_are_ambiguous() -> None:
    assert select_run([_row(), _row()], CARE_ID, "personal", DISPATCH).state == "ambiguous"


def test_a_row_naming_another_chat_is_ambiguous_when_the_chat_is_known() -> None:
    rows = [_row(chat="chat-9")]
    assert select_run(rows, CARE_ID, "personal", DISPATCH, chat_id="chat-1").state == "ambiguous"
    assert select_run(rows, CARE_ID, "personal", DISPATCH).state == "ok"


def test_filter_comes_before_any_limit() -> None:
    rows = [_row()] + [_row(f"newer-{i}") for i in range(25)]
    picked = select_run(rows, CARE_ID, "personal", DISPATCH)
    assert picked.state == "ok" and picked.job_status == "ok"


def test_other_schedule_or_workspace_rows_are_not_matched() -> None:
    other = _row()
    other["extra"]["workspace"] = "work"
    assert select_run([other], CARE_ID, "personal", DISPATCH).state == "pending"


def test_unknown_job_status_is_ambiguous() -> None:
    assert select_run([_row(status="weird")], CARE_ID, "personal", DISPATCH).state == "ambiguous"


def test_error_status_is_reported_as_error() -> None:
    assert select_run([_row(status="error")], CARE_ID, "personal", DISPATCH).job_status == "error"


# -- join_last_run -----------------------------------------------------------


def test_join_requires_the_same_dispatch_id() -> None:
    run = {**BASE_RUN, "dispatch_id": "newer", "report": _report()}
    assert join_last_run(run, DISPATCH) == "superseded"
    assert join_last_run({**BASE_RUN, "report": _report()}, DISPATCH) == "superseded"
    assert join_last_run({**BASE_RUN, "dispatch_id": DISPATCH}, DISPATCH) == "ok"


# -- bounded_report and the strict schema ------------------------------------


def test_bounded_report_caps_items_detail_and_receipts() -> None:
    outcomes = [
        SimpleNamespace(item_key=f"k{i}", pass_id="p", status="applied", detail="d" * 500,
                        receipt_ids=tuple(f"r{n}" for n in range(9)))
        for i in range(MAX_ITEMS + 5)
    ]
    report = bounded_report(outcomes, weekly={"marker": "advanced"})
    assert len(report["items"]) == MAX_ITEMS and report["truncated"] is True
    assert len(report["items"][0]["detail"]) == 200
    assert len(report["items"][0]["receipt_ids"]) == MAX_RECEIPT_IDS
    assert report_valid(report)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("weekly"),
        lambda r: r.update(surprise=1),
        lambda r: r.update(truncated="no"),
        lambda r: r.update(items="x"),
        lambda r: r["items"].append(_item(status="bogus")),
        lambda r: r["items"].append({**_item(), "extra": 1}),
        lambda r: r["items"].append(_item(detail="d" * 201)),
        lambda r: r["items"].append(_item(receipt_ids=["r"] * (MAX_RECEIPT_IDS + 1))),
        lambda r: r["items"].append(_item(receipt_ids=[1])),
        lambda r: r["items"].extend(_item() for _ in range(MAX_ITEMS + 1)),
        lambda r: r.update(weekly={"marker": 1}),
    ],
)
def test_report_validation_is_strict(mutate) -> None:
    report = _report()
    mutate(report)
    assert not report_valid(report)


def _write_v1(vault: Path, last_run: dict) -> None:
    path = cr.state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "done_keys": {}, "lease": {}, "last_run": last_run}), encoding="utf-8")


def test_v1_loads_a_valid_report_and_refuses_an_invalid_one(tmp_path: Path) -> None:
    good = {**BASE_RUN, "dispatch_id": DISPATCH, "report": _report()}
    _write_v1(tmp_path, good)
    assert cr.load_state(tmp_path).last_run == good
    _write_v1(tmp_path, {**good, "report": {**_report(), "truncated": 1}})
    with pytest.raises(cr.CurationStateError):
        cr.load_state(tmp_path)
    _write_v1(tmp_path, {**BASE_RUN, "dispatch_id": 5})
    with pytest.raises(cr.CurationStateError):
        cr.load_state(tmp_path)


def test_v2_codec_accepts_the_report_and_refuses_a_bad_one() -> None:
    from tests.test_care_run_outcome import _v2_text

    good = {**BASE_RUN, "dispatch_id": DISPATCH, "report": _report()}
    assert cc.decode_state(_v2_text(good)).last_run == good
    with pytest.raises(cc.CycleError):
        cc.decode_state(_v2_text({**good, "report": {"items": []}}))


def test_end_run_persists_report_dispatch_and_weekly_state(tmp_path: Path) -> None:
    lease = cr.begin_run(tmp_path, holder="nightly", chat_id="chat-1")
    report = _report()
    del report["weekly"]
    cr.end_run(tmp_path, holder=lease.holder, status="ok", planned=1, completed=1, failed=0,
               counts_complete=True, report=report, dispatch_id=DISPATCH)
    stored = cr.load_state(tmp_path).last_run
    assert stored["dispatch_id"] == DISPATCH
    assert stored["report"]["items"] == report["items"]
    assert stored["report"]["weekly"] == {"marker": "not_advanced"}


def test_end_run_without_a_report_writes_none(tmp_path: Path) -> None:
    lease = cr.begin_run(tmp_path, holder="nightly", chat_id="chat-1")
    cr.end_run(tmp_path, holder=lease.holder, status="ok", failed=0, counts_complete=True)
    stored = cr.load_state(tmp_path).last_run
    assert "report" not in stored and "dispatch_id" not in stored


# -- the CLI end path --------------------------------------------------------


def test_cli_derive_run_counts_builds_the_report(tmp_path: Path) -> None:
    from ciao import cli

    lease = cr.begin_run(tmp_path, holder="nightly", chat_id="chat-1")
    cr.record_plan(tmp_path, holder=lease.holder, plan={
        "planned": [["proposals", "key-a"]], "blocked": [["proposals", "key-b"]],
        "blocked_overflow": 0, "deferred_count": 2,
    })
    counts = cli._derive_run_counts(tmp_path, holder=lease.holder, workspace="personal")
    assert counts.report is not None and report_valid({**counts.report, "weekly": {}})
    by_key = {item["item_key"]: item["status"] for item in counts.report["items"]}
    assert by_key == {"key-a": "unfinished", "key-b": "blocked"}


def test_cli_without_a_plan_has_no_report(tmp_path: Path) -> None:
    from ciao import cli

    lease = cr.begin_run(tmp_path, holder="nightly", chat_id="chat-1")
    assert cli._derive_run_counts(tmp_path, holder=lease.holder, workspace="personal").report is None


def test_cli_curation_end_stores_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao import cli

    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("CIAO_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("CIAO_AGENT_URL", raising=False)
    assert cli.main(["curation-begin", "--vault-root", str(vault), "--json"]) == 0
    holder = cr.load_state(vault).lease["holder"]
    assert cli.main(["curation-end", "--vault-root", str(vault), "--holder", holder, "--status", "ok", "--json"]) == 0
    stored = cr.load_state(vault).last_run
    assert "report" in stored and "dispatch_id" not in stored
    assert report_valid(stored["report"])


# -- the read endpoint -------------------------------------------------------


class _Store:
    def __init__(self, rows: dict[str, SimpleNamespace]) -> None:
        self._rows = rows

    def get(self, schedule_id: str):
        return self._rows.get(schedule_id)


def _client(tmp_path: Path, job_rows: list[dict] | None) -> TestClient:
    from ciao.web.routes_api import schedule_care_run

    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    if job_rows is not None:
        (runtime / "job_runs.jsonl").write_text("\n".join(json.dumps(r) for r in job_rows) + "\n", encoding="utf-8")
    config = SimpleNamespace(workspace_vault_root=lambda name: tmp_path / "vault", state_path=runtime / "state.json")
    app = Starlette(routes=[Route("/api/schedules/{schedule_id}/care-run", schedule_care_run, methods=["GET"])])
    app.state.schedule_manager = SimpleNamespace(_store=_Store({
        CARE_ID: SimpleNamespace(last_run_chat_id="", timezone_name="UTC"),
        "daily": SimpleNamespace(last_run_chat_id="", timezone_name="UTC"),
    }))
    app.state.project_chat_manager = SimpleNamespace()
    app.state.config = config
    return TestClient(app)


def _settle(vault: Path, *, dispatch: str | None = DISPATCH, report: dict | None = None) -> None:
    lease = cr.begin_run(vault, holder="nightly", chat_id="chat-1")
    report = report if report is not None else {k: v for k, v in _report().items() if k != "weekly"}
    cr.end_run(vault, holder=lease.holder, status="ok", planned=1, completed=1, failed=0, counts_complete=True,
               report=report, dispatch_id=dispatch or "")


def _get(client: TestClient, dispatch: str = DISPATCH, schedule: str = CARE_ID):
    return client.get(f"/api/schedules/{schedule}/care-run", params={"dispatch_id": dispatch})


def test_endpoint_returns_the_report_for_the_exact_dispatch(tmp_path: Path) -> None:
    _settle(tmp_path / "vault")
    body = _get(_client(tmp_path, [_row()])).json()
    assert body["state"] == "ok" and body["job_status"] == "ok" and body["report_available"] is True
    assert body["counts"] == {"planned": 1, "completed": 1, "deferred": 0, "failed": 0}
    assert body["outcomes"][0]["item_key"] == "k1" and body["weekly"] == {"marker": "not_advanced"}
    assert body["run_id"].startswith("nightly") and body["care_run"]["counts_complete"] is True
    assert "chat_id" not in json.dumps(body) and str(tmp_path) not in json.dumps(body)


def test_endpoint_pending_without_a_job_row(tmp_path: Path) -> None:
    _settle(tmp_path / "vault")
    body = _get(_client(tmp_path, [])).json()
    assert body["state"] == "pending" and body["report_available"] is False and body["care_run"] is None


def test_endpoint_ambiguous_for_duplicate_rows(tmp_path: Path) -> None:
    _settle(tmp_path / "vault")
    assert _get(_client(tmp_path, [_row(), _row()])).json()["state"] == "ambiguous"


def test_endpoint_superseded_shows_no_counts(tmp_path: Path) -> None:
    _settle(tmp_path / "vault", dispatch="newer")
    body = _get(_client(tmp_path, [_row()])).json()
    assert body["state"] == "superseded" and body["care_run"] is None
    assert "counts" not in body and "outcomes" not in body and body["report_available"] is False


def test_endpoint_older_run_without_a_report_is_not_available(tmp_path: Path) -> None:
    lease = cr.begin_run(tmp_path / "vault", holder="nightly", chat_id="chat-1")
    cr.end_run(tmp_path / "vault", holder=lease.holder, status="ok", planned=1, completed=1, failed=0,
               counts_complete=True, dispatch_id=DISPATCH)
    body = _get(_client(tmp_path, [_row()])).json()
    assert body["state"] == "ok" and body["report_available"] is False and "outcomes" not in body


def test_endpoint_job_error_with_clean_counts_stays_error(tmp_path: Path) -> None:
    _settle(tmp_path / "vault")
    body = _get(_client(tmp_path, [_row(status="error")])).json()
    assert body["job_status"] == "error" and body["counts"]["failed"] == 0


def test_endpoint_unreadable_job_log_is_unavailable(tmp_path: Path) -> None:
    _settle(tmp_path / "vault")
    client = _client(tmp_path, [])
    (tmp_path / "runtime" / "job_runs.jsonl").write_bytes(b"\xff\xfe")
    assert _get(client).json()["state"] == "unavailable"


def test_endpoint_unreadable_state_is_503(tmp_path: Path) -> None:
    path = cr.state_path(tmp_path / "vault")
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    assert _get(_client(tmp_path, [_row()])).status_code == 503


def test_endpoint_caps_outcomes_and_reasons(tmp_path: Path) -> None:
    items = [_item(f"k{i}") for i in range(MAX_ITEMS)]
    lease = cr.begin_run(tmp_path / "vault", holder="x" * 300, chat_id="chat-1")
    cr.end_run(tmp_path / "vault", holder=lease.holder, status="ok", planned=1, completed=1, failed=0,
               counts_complete=True, reasons=[f"r{i}" + "z" * 400 for i in range(30)],
               report={"items": items, "truncated": False}, dispatch_id=DISPATCH)
    body = _get(_client(tmp_path, [_row()])).json()
    assert len(body["outcomes"]) == MAX_ITEMS
    assert len(body["care_run"]["reasons"]) == 10 and all(len(r) <= 200 for r in body["care_run"]["reasons"])
    assert len(body["run_id"]) == 200


def test_endpoint_non_care_schedule_is_null_and_unknown_is_404(tmp_path: Path) -> None:
    client = _client(tmp_path, [_row()])
    assert _get(client, schedule="daily").json() == {"care_run": None}
    assert _get(client, schedule="nope").status_code == 404


def test_endpoint_refuses_an_oversize_dispatch_id(tmp_path: Path) -> None:
    assert _get(_client(tmp_path, []), dispatch="d" * 201).status_code == 400
