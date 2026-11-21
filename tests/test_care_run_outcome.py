"""The last care run's derived outcome, bound to the chat that ran it (#3056 S3).

Covers the optional ``last_run`` fields in both codecs, the chat binding taken
at ``curation-begin``, the pure view that returns an outcome only for the chat
that owns it, and the read-only schedule route. Every vault lives under
``tmp_path``; the route runs against stub managers, never a real engine.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import curation_cycle as cc
from ciao import curation_run as cr
from ciao.care_run_outcome import MAX_REASON_CHARS, MAX_REASONS, outcome_for_chat

OLD_LAST_RUN = {
    "status": "ok",
    "finished_at": "2026-11-16T00:30:00Z",
    "planned": 3,
    "completed": 2,
    "deferred": 1,
    "reasons": [],
}


def _write_v1(vault: Path, last_run: dict) -> None:
    path = cr.state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "done_keys": {}, "lease": {}, "last_run": last_run}),
        encoding="utf-8",
    )


def _v2_text(last_run: dict) -> str:
    prior = cc.marker_identity_of(b"---\nlast_full_pass: 2026-11-09\n---\n\n# Curation log\n")
    return cc.encode_state(
        cc.CycleState(
            cycle_id=cc.cycle_identity(prior, ""),
            prior_marker=prior,
            required={},
            optional={},
            carried=(),
            legacy_historical={},
            daily={},
            lease={},
            last_run=last_run,
            pending=None,
        )
    )


# ── Codecs: old files load, optional fields are type-checked ─────────────


def test_v1_state_without_the_optional_fields_still_loads(tmp_path: Path) -> None:
    _write_v1(tmp_path, OLD_LAST_RUN)
    assert cr.load_state(tmp_path).last_run == OLD_LAST_RUN


def test_v1_optional_fields_load_when_well_typed(tmp_path: Path) -> None:
    last_run = {
        **OLD_LAST_RUN,
        "run_id": "host:1:abc",
        "chat_id": "chat-1",
        "failed": 0,
        "counts_complete": True,
    }
    _write_v1(tmp_path, last_run)
    assert cr.load_state(tmp_path).last_run == last_run


@pytest.mark.parametrize(
    "extra",
    [
        {"failed": "1"},
        {"failed": True},
        {"counts_complete": "yes"},
        {"counts_complete": 1},
        {"chat_id": 7},
        {"run_id": None},
    ],
)
def test_v1_refuses_a_mistyped_optional_field(tmp_path: Path, extra: dict) -> None:
    _write_v1(tmp_path, {**OLD_LAST_RUN, **extra})
    with pytest.raises(cr.CurationStateError):
        cr.load_state(tmp_path)


def test_v2_document_without_the_optional_fields_still_decodes() -> None:
    assert cc.decode_state(_v2_text(OLD_LAST_RUN)).last_run == OLD_LAST_RUN


def test_v2_optional_fields_round_trip() -> None:
    last_run = {**OLD_LAST_RUN, "run_id": "h:1:u", "chat_id": "chat-1", "failed": 2, "counts_complete": False}
    text = _v2_text(last_run)
    assert cc.decode_state(text).last_run == last_run


@pytest.mark.parametrize("extra", [{"failed": "2"}, {"counts_complete": None}, {"chat_id": []}])
def test_v2_refuses_a_mistyped_optional_field(extra: dict) -> None:
    with pytest.raises(cc.CycleError):
        cc.decode_state(_v2_text({**OLD_LAST_RUN, **extra}))


def test_v2_still_refuses_an_unknown_last_run_field() -> None:
    with pytest.raises(cc.CycleError):
        cc.decode_state(_v2_text({**OLD_LAST_RUN, "surprise": 1}))


# ── Lease binding and end-of-run copy ────────────────────────────────────


def test_begin_records_the_chat_and_renewal_keeps_it(tmp_path: Path) -> None:
    lease = cr.begin_run(tmp_path, holder="nightly", chat_id="chat-1")
    assert cr.load_state(tmp_path).lease["chat_id"] == "chat-1"
    assert cr.renew_run(tmp_path, holder=lease.holder)
    assert cr.load_state(tmp_path).lease["chat_id"] == "chat-1"


def test_end_copies_the_binding_and_the_derived_counts(tmp_path: Path) -> None:
    lease = cr.begin_run(tmp_path, holder="nightly", chat_id="chat-1")
    summary = cr.end_run(
        tmp_path,
        holder=lease.holder,
        status="ok",
        planned=2,
        completed=1,
        deferred=0,
        reasons=["1 accept(s) failed"],
        failed=1,
        counts_complete=False,
    )
    stored = cr.load_state(tmp_path).last_run
    assert stored["run_id"] == lease.holder
    assert stored["chat_id"] == "chat-1"
    assert stored["failed"] == 1
    assert stored["counts_complete"] is False
    assert summary["chat_id"] == "chat-1"
    assert cr.load_state(tmp_path).lease == {}


def test_end_without_derived_counts_omits_the_optional_fields(tmp_path: Path) -> None:
    lease = cr.begin_run(tmp_path, holder="nightly", chat_id="chat-1")
    cr.end_run(tmp_path, holder=lease.holder, status="error", planned=0, completed=0, deferred=0)
    stored = cr.load_state(tmp_path).last_run
    assert "failed" not in stored
    assert "counts_complete" not in stored


def test_curation_begin_reads_the_chat_from_the_agent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ciao import cli

    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("CIAO_ACTIVE_WORKSPACE", raising=False)
    monkeypatch.setenv("CIAO_CHAT_ID", "chat-from-env")
    assert cli.main(["curation-begin", "--vault-root", str(vault), "--json"]) == 0
    capsys.readouterr()
    assert cr.load_state(vault).lease["chat_id"] == "chat-from-env"

    assert cli.main(
        ["curation-end", "--vault-root", str(vault), "--holder", cr.load_state(vault).lease["holder"],
         "--status", "ok", "--json"]
    ) == 0
    stored = cr.load_state(vault).last_run
    assert stored["chat_id"] == "chat-from-env"
    assert stored["counts_complete"] is True
    assert stored["failed"] == 0


# ── The pure view: only the owning chat, never a newer run ───────────────

_RUN = {
    **OLD_LAST_RUN,
    "run_id": "h:1:u",
    "chat_id": "chat-1",
    "failed": 1,
    "counts_complete": False,
    "reasons": ["incomplete"],
}


def test_outcome_is_returned_for_the_owning_chat() -> None:
    assert outcome_for_chat(_RUN, "chat-1") == {
        "run_id": "h:1:u",
        "planned": 3,
        "completed": 2,
        "deferred": 1,
        "failed": 1,
        "counts_complete": False,
        "reasons": ["incomplete"],
    }


@pytest.mark.parametrize("chat_id", ["", "chat-2"])
def test_outcome_is_none_for_another_or_no_chat(chat_id: str) -> None:
    assert outcome_for_chat(_RUN, chat_id) is None


def test_outcome_is_none_when_the_run_has_no_derived_counts() -> None:
    bare = {k: v for k, v in _RUN.items() if k not in ("failed", "counts_complete")}
    assert outcome_for_chat(bare, "chat-1") is None


def test_outcome_is_none_for_an_empty_last_run() -> None:
    assert outcome_for_chat({}, "chat-1") is None


def test_outcome_reasons_are_capped_in_count_and_length() -> None:
    long = "x" * (MAX_REASON_CHARS * 2)
    run = {**_RUN, "reasons": [long] * (MAX_REASONS + 5)}
    outcome = outcome_for_chat(run, "chat-1")
    assert outcome is not None
    assert len(outcome["reasons"]) == MAX_REASONS
    assert all(len(reason) <= MAX_REASON_CHARS for reason in outcome["reasons"])


# ── The read-only route ──────────────────────────────────────────────────


class _Store:
    def __init__(self, rows: dict[str, SimpleNamespace]) -> None:
        self._rows = rows

    def get(self, schedule_id: str):
        return self._rows.get(schedule_id)


class _Pcm:
    def get_chat(self, chat_id: str):
        if chat_id in {"chat-1", "chat-2"}:
            return SimpleNamespace(project_id="proj-1")
        return None

    def get_project(self, project_id: str):
        return SimpleNamespace(workspace="personal") if project_id == "proj-1" else None


def _client(tmp_path: Path, rows: dict[str, SimpleNamespace]) -> TestClient:
    from ciao.web.routes_api import schedule_care_run

    vault = tmp_path / "vault"
    config = SimpleNamespace(workspace_vault_root=lambda name: vault)
    app = Starlette(routes=[Route("/api/schedules/{schedule_id}/care-run", schedule_care_run, methods=["GET"])])
    app.state.schedule_manager = SimpleNamespace(_store=_Store(rows))
    app.state.project_chat_manager = _Pcm()
    app.state.config = config
    return TestClient(app)


def _finished_run(vault: Path, chat_id: str) -> None:
    lease = cr.begin_run(vault, holder="nightly", chat_id=chat_id)
    cr.end_run(
        vault, holder=lease.holder, status="ok", planned=2, completed=2, deferred=0,
        failed=0, counts_complete=True,
    )


def test_route_returns_the_outcome_for_the_schedules_own_chat(tmp_path: Path) -> None:
    rows = {"system-memory-curation@personal": SimpleNamespace(last_run_chat_id="chat-1")}
    _finished_run(tmp_path / "vault", "chat-1")
    response = _client(tmp_path, rows).get("/api/schedules/system-memory-curation@personal/care-run")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"care_run"}
    assert body["care_run"]["planned"] == 2
    assert body["care_run"]["counts_complete"] is True
    assert set(body["care_run"]) == {
        "run_id", "planned", "completed", "deferred", "failed", "counts_complete", "reasons",
    }


def test_route_is_null_when_the_latest_schedule_chat_is_not_the_stored_run(tmp_path: Path) -> None:
    rows = {"system-memory-curation@personal": SimpleNamespace(last_run_chat_id="chat-2")}
    _finished_run(tmp_path / "vault", "chat-1")
    response = _client(tmp_path, rows).get("/api/schedules/system-memory-curation@personal/care-run")
    assert response.json() == {"care_run": None}


def test_route_is_null_when_the_schedule_has_no_run_yet(tmp_path: Path) -> None:
    rows = {"system-memory-curation@personal": SimpleNamespace(last_run_chat_id="")}
    response = _client(tmp_path, rows).get("/api/schedules/system-memory-curation@personal/care-run")
    assert response.json() == {"care_run": None}


def test_route_is_null_when_no_run_is_recorded_in_the_vault(tmp_path: Path) -> None:
    rows = {"system-memory-curation@personal": SimpleNamespace(last_run_chat_id="chat-1")}
    response = _client(tmp_path, rows).get("/api/schedules/system-memory-curation@personal/care-run")
    assert response.json() == {"care_run": None}


def test_route_is_404_for_an_unknown_schedule(tmp_path: Path) -> None:
    response = _client(tmp_path, {}).get("/api/schedules/nope/care-run")
    assert response.status_code == 404


def test_route_refuses_an_unreadable_state_file_with_503(tmp_path: Path) -> None:
    rows = {"system-memory-curation@personal": SimpleNamespace(last_run_chat_id="chat-1")}
    path = cr.state_path(tmp_path / "vault")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    response = _client(tmp_path, rows).get("/api/schedules/system-memory-curation@personal/care-run")
    assert response.status_code == 503
    assert "care" in response.json()["error"]
