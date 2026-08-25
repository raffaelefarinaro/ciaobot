"""Tests for ``ciao.proposal_outcomes`` (the resolution-tally recorder)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import proposal_outcomes as po


def _read_events(tmp_path: Path) -> list[dict]:
    path = tmp_path / po.PROPOSAL_OUTCOMES_NAME
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def _write_events(tmp_path: Path, lines: list[str]) -> None:
    path = tmp_path / po.PROPOSAL_OUTCOMES_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── record ────────────────────────────────────────────────────────────────


def test_record_appends_one_schema_shaped_event(tmp_path: Path) -> None:
    po.record("memory", "promoted", workspace="personal")
    po.record("review", "dismissed", workspace="", via="agent")

    events = _read_events(tmp_path)
    assert len(events) == 2
    assert set(events[0]) == {"ts", "workspace", "kind", "action", "via"}
    assert events[0]["action"] == "promoted"
    assert events[0]["kind"] == "memory"
    assert events[0]["workspace"] == "personal"
    # The PWA is the default surface; the CLI names itself explicitly.
    assert events[0]["via"] == "pwa"
    assert events[1]["via"] == "agent"
    # The timestamp must be parseable for the 30-day window to mean anything.
    parsed = datetime.fromisoformat(events[0]["ts"])
    assert parsed.tzinfo is not None


def test_record_refuses_an_unknown_action(tmp_path: Path) -> None:
    """The aggregator folds only the two decisions it knows; writing a third
    spelling would record a decision that silently vanishes from every count."""
    po.record("memory", "nuked")
    assert _read_events(tmp_path) == []


def test_record_is_fail_open(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file in the way", encoding="utf-8")
    po.configure(blocker)
    try:
        po.record("memory", "promoted")  # must not raise
    finally:
        po.configure(tmp_path)


# ── tally ────────────────────────────────────────────────────────────────


def test_tally_folds_totals_workspaces_and_recent_window() -> None:
    po.record("memory", "promoted", workspace="personal")
    po.record("profile", "promoted", workspace="personal")
    po.record("rehome", "dismissed", workspace="work")
    po.record("skill", "dismissed", workspace="work")

    report = po.tally()

    assert report["promoted"] == 2
    assert report["dismissed"] == 2
    assert report["by_workspace"] == {
        "personal": {"promoted": 2, "dismissed": 0},
        "work": {"promoted": 0, "dismissed": 2},
    }
    # Fresh events are all inside the window.
    assert report["recent_30d"] == {"promoted": 2, "dismissed": 2}


def test_tally_of_an_empty_log_is_all_zeros(tmp_path: Path) -> None:
    assert po.tally() == {
        "promoted": 0,
        "dismissed": 0,
        "by_workspace": {},
        "recent_30d": {"promoted": 0, "dismissed": 0},
    }
    assert not (tmp_path / po.PROPOSAL_OUTCOMES_NAME).exists()


def test_tally_skips_malformed_lines_and_unknown_actions(tmp_path: Path) -> None:
    _write_events(tmp_path, [
        "this is not json",
        "[memory] a bullet someone pasted into the log",
        json.dumps({"ts": datetime.now(UTC).isoformat(), "workspace": "x",
                    "kind": "memory", "action": "nuked", "via": "pwa"}),
        json.dumps({"workspace": "", "kind": "memory", "action": "dismissed"}),
        json.dumps({"ts": datetime.now(UTC).isoformat(), "kind": "memory",
                    "action": "promoted"}),
    ])

    report = po.tally()

    assert report["promoted"] == 1
    assert report["dismissed"] == 1
    assert report["by_workspace"][""]["dismissed"] == 1


def test_tally_30_day_window_boundary(tmp_path: Path) -> None:
    now = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
    at_edge = (now - timedelta(days=30)).isoformat()
    one_second_older = (now - timedelta(days=30, seconds=1)).isoformat()
    unreadable_ts = "not-a-timestamp"
    _write_events(tmp_path, [
        json.dumps({"ts": at_edge, "workspace": "w", "kind": "memory",
                    "action": "promoted", "via": "pwa"}),
        json.dumps({"ts": one_second_older, "workspace": "w", "kind": "memory",
                    "action": "promoted", "via": "pwa"}),
        json.dumps({"ts": unreadable_ts, "workspace": "w", "kind": "memory",
                    "action": "dismissed", "via": "pwa"}),
    ])

    report = po.tally(now=now)

    # The boundary event counts as recent; one second past it does not. An
    # unreadable timestamp still counts in the totals but cannot claim recency.
    assert report["promoted"] == 2
    assert report["dismissed"] == 1
    assert report["recent_30d"] == {"promoted": 1, "dismissed": 0}


def test_tally_never_raises_on_a_broken_log(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file in the way", encoding="utf-8")
    po.configure(blocker)
    try:
        report = po.tally()
    finally:
        po.configure(tmp_path)
    assert report["promoted"] == 0


# ── endpoint cache ────────────────────────────────────────────────────────


def test_tally_cached_serves_stale_within_ttl_then_refreshes() -> None:
    po.record("memory", "promoted", workspace="personal")
    first = po.tally_cached()
    assert first["promoted"] == 1

    # A resolution landing after the cached read is invisible until the TTL
    # expires or the cache is dropped — that staleness is what keeps the
    # endpoint cheap.
    po.record("profile", "dismissed", workspace="work")
    assert po.tally_cached() is first

    po.reset_tally_cache()
    refreshed = po.tally_cached()
    assert refreshed["promoted"] == 1
    assert refreshed["dismissed"] == 1


# ── agent path (curation CLI) ─────────────────────────────────────────────


def test_cli_dismiss_records_an_agent_outcome(tmp_path: Path, monkeypatch) -> None:
    """The nightly curation agent resolves through the CLI, not the PWA; its
    dismissals must land in the same tally with ``via: "agent"``."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n- [memory] durable lesson  _(from: Decisions)_\n",
        encoding="utf-8",
    )

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "durable lesson",
    ])

    assert rc == 0
    events = _read_events(tmp_path)
    assert len(events) == 1
    assert events[0]["action"] == "dismissed"
    assert events[0]["kind"] == "memory"
    assert events[0]["workspace"] == str(tmp_path)
    assert events[0]["via"] == "agent"


def test_a_refused_cli_dismiss_records_nothing(tmp_path: Path) -> None:
    """An ambiguous substring removes nothing and decides nothing."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [memory] durable lesson one\n"
        "- [profile] durable lesson two\n",
        encoding="utf-8",
    )

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "durable lesson",
    ])

    assert rc == 1
    assert _read_events(tmp_path) == []


# ── automation payload shape ──────────────────────────────────────────────


def _automation_client() -> TestClient:
    from ciao.web.routes_api import list_automation

    app = Starlette(routes=[Route("/api/automation", list_automation, methods=["GET"])])
    return TestClient(app)


def test_automation_default_payload_stays_a_bare_list() -> None:
    response = _automation_client().get("/api/automation")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert any(item["job"] == "insights" for item in payload)


def test_automation_include_outcomes_serves_the_envelope() -> None:
    po.record("memory", "promoted", workspace="personal")
    po.record("review", "dismissed", workspace="")

    payload = _automation_client().get("/api/automation?include=outcomes").json()

    assert isinstance(payload, dict)
    assert {item["job"] for item in payload["jobs"]} >= {"insights"}
    outcomes = payload["proposal_outcomes"]
    assert outcomes["promoted"] == 1
    assert outcomes["dismissed"] == 1
    assert outcomes["by_workspace"]["personal"]["promoted"] == 1
    assert outcomes["recent_30d"] == {"promoted": 1, "dismissed": 1}
