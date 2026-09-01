"""Tests for ``ciao.proposal_outcomes`` (the resolution-tally recorder)."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
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


def test_record_refuses_an_unknown_via(tmp_path: Path) -> None:
    """Every call site already passes "pwa" or "agent"; a typo like "agents"
    must be refused rather than silently fragment the by-surface split."""
    po.record("memory", "promoted", via="agents")
    assert _read_events(tmp_path) == []


def test_record_refuses_a_non_extraction_kind(tmp_path: Path) -> None:
    """Skill proposals and rehome judgements share the review surface but
    answer to different pipelines; this ledger counts only extraction kinds."""
    po.record("skill", "promoted")
    po.record("rehome", "dismissed")
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


def test_tally_folds_totals_workspaces_and_recent_window(tmp_path: Path) -> None:
    # ``rehome``/``skill`` are not extraction kinds, so ``record()`` itself
    # refuses to write them (see test_record_refuses_a_non_extraction_kind).
    # tally() folds any well-formed line by action regardless of kind, so
    # seed these two straight into the log — before the ``record()`` calls
    # below, since ``_write_events`` overwrites the whole file.
    _write_events(tmp_path, [
        json.dumps({"ts": datetime.now(UTC).isoformat(), "workspace": "work",
                    "kind": "rehome", "action": "dismissed", "via": "pwa"}),
        json.dumps({"ts": datetime.now(UTC).isoformat(), "workspace": "work",
                    "kind": "skill", "action": "dismissed", "via": "pwa"}),
    ])
    po.record("memory", "promoted", workspace="personal")
    po.record("profile", "promoted", workspace="personal")

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


def test_configure_invalidates_the_tally_cache(tmp_path: Path) -> None:
    """A runtime-dir change must not keep serving a tally read from the
    previous directory until the TTL happens to expire."""
    po.record("memory", "promoted", workspace="personal")
    first = po.tally_cached()
    assert first["promoted"] == 1

    other = tmp_path / "other-runtime"
    po.configure(other)
    try:
        # The new directory has no log yet, so a stale cache would still show
        # the old directory's count; a correctly invalidated cache shows zero.
        assert po.tally_cached()["promoted"] == 0
    finally:
        po.configure(tmp_path)


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
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "durable lesson",
    ])

    assert rc == 0
    # The CLI records into the workspace's own .runtime, not the fixture's
    # override: a dismissal from an arbitrary cwd must land where the server
    # tallies it.
    log = tmp_path / ".runtime" / po.PROPOSAL_OUTCOMES_NAME
    assert log.is_file()
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(events) == 1
    assert events[0]["action"] == "dismissed"
    assert events[0]["kind"] == "memory"
    # The logical workspace name from CIAO_ACTIVE_WORKSPACE, never the
    # filesystem path the command was pointed at.
    assert events[0]["workspace"] == "work"
    assert events[0]["via"] == "agent"

    # --runtime-root overrides the default explicitly.
    rc2 = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--runtime-root", str(tmp_path / "custom-runtime"),
        "durable lesson",
    ])
    assert rc2 != 0  # the row is gone, so the second dismiss finds no match
    assert (tmp_path / "custom-runtime" / po.PROPOSAL_OUTCOMES_NAME).exists() is False


def test_cli_promote_then_dismiss_records_a_promotion(
    tmp_path: Path, monkeypatch
) -> None:
    """The curator files the fact first and drops the row second; that flow is
    a promotion, and only a bare rejection counts as dismissed."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n- [learnings] reuse the retry helper\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "personal")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--promoted",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "retry helper",
    ])

    assert rc == 0
    log = tmp_path / ".runtime" / po.PROPOSAL_OUTCOMES_NAME
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(events) == 1
    assert events[0]["action"] == "promoted"


def test_cli_runtime_root_prefers_the_environment(tmp_path: Path, monkeypatch) -> None:
    """An install pointing CIAO_RUNTIME_ROOT elsewhere keeps agent outcomes in
    that same runtime, even without an explicit --runtime-root."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text("# Memory Proposals\n\n- [memory] keep it simple\n", encoding="utf-8")
    custom = tmp_path / "elsewhere"
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", str(custom))

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "keep it simple",
    ])

    assert rc == 0
    assert (custom / po.PROPOSAL_OUTCOMES_NAME).is_file()
    assert not (tmp_path / ".runtime" / po.PROPOSAL_OUTCOMES_NAME).exists()


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


# ── rotation keeps lifetime totals ────────────────────────────────────────


def test_rotation_preserves_lifetime_totals(tmp_path: Path, monkeypatch) -> None:
    """Trimming the log must not make history disappear: the dropped lines are
    archived verbatim, so promoted/dismissed (and the per-workspace split)
    keep growing across rotations."""
    monkeypatch.setattr(po, "MAX_BYTES", 4 * 1024)  # force the trim on
    base = datetime.now(UTC) - timedelta(days=90)
    with po._log_path().open("w", encoding="utf-8") as f:
        for n in range(po.KEEP_LINES + 25):  # enough rows that a trim drops some
            # Distinct timestamps: byte-identical lines are what a crash
            # duplicate looks like, and the fold de-duplicates those on
            # purpose — so a fixture must not write the same ts twice.
            # Offset from the `base` captured above rather than re-reading the
            # clock: `now() - n µs` inside the loop lets wall-clock advance
            # cancel the decrement, so rows collide, the fold drops them as
            # crash duplicates, and the totals come up short by however many
            # microseconds the loop happened to take on that machine.
            old = (base - timedelta(microseconds=n)).isoformat()
            f.write(json.dumps({
                "ts": old, "workspace": "personal" if n % 2 else "work",
                "kind": "memory", "action": "promoted", "via": "pwa",
            }) + "\n")
    po.record("learnings", "dismissed", workspace="personal")

    report = po.tally()

    assert report["promoted"] == po.KEEP_LINES + 25
    assert report["dismissed"] == 1
    # 2025 promoted events alternate work/personal; the trim drops only the
    # oldest 25 (all work), and the archive carries them.
    assert report["by_workspace"]["work"]["promoted"] == 1013
    assert report["by_workspace"]["personal"]["promoted"] == 1012
    # The 30-day window is derived from the live file only: rotated events are
    # 90 days old, and the one fresh dismissal is a dismissal.
    assert report["recent_30d"] == {"promoted": 0, "dismissed": 1}
    assert list(tmp_path.glob("proposal_outcomes.rotated-*.jsonl"))


def test_rotation_archive_counts_once_after_a_crash_between_the_two_swaps(
    tmp_path: Path, monkeypatch
) -> None:
    """The recoverable rotation: a crash after the archive write but before the
    live-log swap leaves the dropped lines in BOTH files, and the fold must
    count each of them once — never twice."""
    monkeypatch.setattr(po, "MAX_BYTES", 4 * 1024)
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    with po._log_path().open("w", encoding="utf-8") as f:
        for n in range(25):
            f.write(json.dumps({
                "ts": old, "workspace": "work",
                "kind": "memory", "action": "promoted", "via": "pwa",
                "n": n,
            }) + "\n")

    # Simulate the crash window: the archive got the dropped lines, but the
    # live-log swap never ran, so the lines are still in the log too.
    archive = tmp_path / "proposal_outcomes.rotated-20260101T000000000000Z.jsonl"
    lines = po._log_path().read_text(encoding="utf-8").splitlines(keepends=True)
    archive.write_text("".join(lines[:25]), encoding="utf-8")

    report = po.tally()

    # 25 events archived + still live → each counted once, not twice.
    assert report["promoted"] == 25
    # A later trim absorbs the leftover duplicated lines into a new archive;
    # the fold still counts each event exactly once.
    po.record("memory", "dismissed", workspace="personal")
    report2 = po.tally()
    assert report2["promoted"] == 25
    assert report2["dismissed"] == 1


def test_tally_survives_a_corrupt_rotation_archive(tmp_path: Path) -> None:
    """A broken archive degrades to the live file's counts, never a failed page."""
    archive = tmp_path / "proposal_outcomes.rotated-20260101T000000000000Z.jsonl"
    archive.write_text("{not json\n", encoding="utf-8")
    po.record("memory", "promoted", workspace="personal")

    report = po.tally()

    assert report["promoted"] == 1
    assert report["by_workspace"] == {"personal": {"promoted": 1, "dismissed": 0}}


# ── kinds outside the extraction pipeline ─────────────────────────────────


def test_rehome_and_skill_kinds_are_not_extraction_kinds() -> None:
    assert not po.is_extraction_kind("skill")
    assert not po.is_extraction_kind("rehome")
    assert po.is_extraction_kind("review")


def test_cli_dismiss_of_a_rehome_row_records_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """Rehome rows are queued by vault hygiene; dismissing one through the CLI
    is a hygiene decision, not an extraction outcome."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n- [rehome] Move `personal/People/Mo.md`? Uncertain.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "personal")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--runtime-root", str(tmp_path / ".runtime"),
        "Move `personal/People/Mo.md`",
    ])

    assert rc == 0
    assert not (tmp_path / ".runtime" / po.PROPOSAL_OUTCOMES_NAME).exists()


def test_rotation_keeps_still_recent_events_in_the_30d_window(
    tmp_path: Path, monkeypatch
) -> None:
    """A trim that drops events younger than the window must not make the
    30-day count fall: the archive keeps the verbatim lines and the fold
    counts them."""
    monkeypatch.setattr(po, "MAX_BYTES", 4 * 1024)
    base = datetime.now(UTC)
    with po._log_path().open("w", encoding="utf-8") as f:
        # 2000 recent promoted events + one recent dismissal; the trim drops
        # the oldest promoted lines. Timestamps derive from one synthetic base
        # at distinct microsecond offsets — the wall clock can step backwards
        # or tick coarsely, and a repeated ts is by definition a crash
        # duplicate as far as the fold is concerned.
        for n in range(po.KEEP_LINES):
            fresh = (base - timedelta(microseconds=n)).isoformat()
            f.write(json.dumps({
                "ts": fresh, "workspace": "personal",
                "kind": "memory", "action": "promoted", "via": "pwa",
            }) + "\n")
        f.write(json.dumps({
            "ts": (base + timedelta(seconds=1)).isoformat(), "workspace": "work",
            "kind": "learnings", "action": "dismissed", "via": "pwa",
        }) + "\n")
        f.write(json.dumps({
            "ts": (base - timedelta(days=90)).isoformat(),
            "workspace": "personal",
            "kind": "memory", "action": "promoted", "via": "pwa",
        }) + "\n")

    report = po.tally()

    assert report["promoted"] == po.KEEP_LINES + 1
    assert report["dismissed"] == 1
    assert report["recent_30d"]["promoted"] == po.KEEP_LINES
    assert report["recent_30d"]["dismissed"] == 1


def test_record_works_without_fcntl(tmp_path: Path, monkeypatch) -> None:
    """On platforms without fcntl the lock degrades to unlocked behaviour and
    recording keeps working."""
    monkeypatch.setattr(po, "fcntl", None)

    po.record("memory", "promoted", workspace="personal")

    report = po.tally()
    assert report["promoted"] == 1


# ── concurrency ─────────────────────────────────────────────────────────────


def test_concurrent_record_calls_all_land_without_corruption(tmp_path: Path) -> None:
    """Many threads calling record() at once must all land: one JSON object
    per line, none merged or torn. ``flock`` is scoped to the open file
    description rather than the process, so two same-process threads each
    opening the lock file separately still serialize against each other —
    this pins that same-process concurrency, not just cross-process."""
    n_threads = 8
    n_per_thread = 25

    def worker(i: int) -> None:
        for _ in range(n_per_thread):
            po.record("memory", "promoted", workspace=f"w{i}")

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(worker, range(n_threads)))

    raw_lines = po._log_path().read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == n_threads * n_per_thread
    # json.loads raises on anything torn or interleaved; every line must
    # parse as exactly the one event that was written.
    events = [json.loads(line) for line in raw_lines]
    assert all(e["action"] == "promoted" and e["kind"] == "memory" for e in events)

    report = po.tally()
    assert report["promoted"] == n_threads * n_per_thread


def test_tally_survives_concurrent_rotation(tmp_path: Path, monkeypatch) -> None:
    """tally() takes the shared lock while a concurrent recorder holds the
    exclusive lock to trim-and-archive; interleaving many rotations with many
    reads must never raise, and the final tally must neither lose nor
    double-count an event once every thread has finished."""
    monkeypatch.setattr(po, "MAX_BYTES", 2 * 1024)  # force frequent rotation
    n_events = 250
    errors: list[BaseException] = []

    def writer() -> None:
        for _ in range(n_events):
            po.record("memory", "promoted", workspace="w")

    def reader() -> None:
        for _ in range(50):
            try:
                po.tally()
            except BaseException as exc:  # noqa: BLE001 - pragma: no cover
                errors.append(exc)

    writer_thread = threading.Thread(target=writer)
    reader_threads = [threading.Thread(target=reader) for _ in range(4)]
    writer_thread.start()
    for t in reader_threads:
        t.start()
    writer_thread.join()
    for t in reader_threads:
        t.join()

    assert errors == []
    final = po.tally()
    assert final["promoted"] == n_events
    assert list(tmp_path.glob("proposal_outcomes.rotated-*.jsonl"))


def test_tally_tolerates_a_truncated_final_line(tmp_path: Path) -> None:
    """A crash mid-append can leave the log's last line partially written: no
    trailing newline, and the JSON object itself cut short. tally() must
    still count every complete line before it and never raise."""
    complete = json.dumps({
        "ts": datetime.now(UTC).isoformat(), "workspace": "personal",
        "kind": "memory", "action": "promoted", "via": "pwa",
    })
    po._log_path().parent.mkdir(parents=True, exist_ok=True)
    with po._log_path().open("w", encoding="utf-8") as f:
        f.write(complete + "\n")
        f.write('{"ts": "2026-08-2')  # write interrupted before it completed

    report = po.tally()

    assert report["promoted"] == 1
    assert report["by_workspace"] == {"personal": {"promoted": 1, "dismissed": 0}}


def test_log_lock_and_archive_files_are_created_owner_only(
    tmp_path: Path, monkeypatch
) -> None:
    """Runtime files here hold workspace names; they must not be readable by
    other accounts on the box, the same convention jsonio.write_private_text
    uses for on-disk secrets, rather than trusting the process umask."""
    monkeypatch.setattr(po, "MAX_BYTES", 1024)  # force a rotation to happen
    for _ in range(50):
        po.record("memory", "promoted", workspace="w")

    log_mode = os.stat(po._log_path()).st_mode & 0o777
    lock_mode = os.stat(tmp_path / po._LOCK_NAME).st_mode & 0o777
    assert log_mode == 0o600
    assert lock_mode == 0o600

    archives = list(tmp_path.glob("proposal_outcomes.rotated-*.jsonl"))
    assert archives
    for archive in archives:
        assert os.stat(archive).st_mode & 0o777 == 0o600


# ── memory-proposal-dismiss --text-file ───────────────────────────────────
#
# A proposal's text is arbitrary user prose. `memory-proposal-add` already
# keeps it out of argv for that reason; the dismissal names the same text and
# needed the same door, or every recipe that files a fact safely had to hand it
# back to the shell to remove it.


def _queue_with(tmp_path: Path, bullet: str) -> tuple[Path, Path]:
    """A workspace vault holding one queued proposal."""
    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(f"# Memory Proposals\n\n- {bullet}\n", encoding="utf-8")
    return vault, queue


def test_dismiss_reads_the_substring_from_a_file(tmp_path, monkeypatch):
    from ciao import cli

    hazardous = "prefers $(whoami) over `id`; never use \"quotes\""
    vault, queue = _queue_with(tmp_path, f"[memory] {hazardous}  _(from: Decisions)_")
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    needle = tmp_path / "needle.txt"
    # Trailing newline is an artifact of writing the file, never part of the
    # proposal it has to match.
    needle.write_text(hazardous + "\n", encoding="utf-8")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--text-file", str(needle),
    ])

    assert rc == 0
    assert hazardous not in queue.read_text(encoding="utf-8")


def test_dismiss_refuses_both_text_and_text_file(tmp_path, monkeypatch, capsys):
    from ciao import cli

    vault, _ = _queue_with(tmp_path, "[memory] durable lesson  _(from: Decisions)_")
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    needle = tmp_path / "needle.txt"
    needle.write_text("durable lesson", encoding="utf-8")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "durable lesson",
        "--text-file", str(needle),
    ])

    assert rc == 2
    assert "not both" in capsys.readouterr().err


def test_dismiss_reports_an_unreadable_text_file(tmp_path, monkeypatch, capsys):
    from ciao import cli

    vault, _ = _queue_with(tmp_path, "[memory] durable lesson  _(from: Decisions)_")
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--text-file", str(tmp_path / "missing.txt"),
    ])

    assert rc == 2
    assert "could not read" in capsys.readouterr().err


def test_dismiss_still_requires_some_text(tmp_path, monkeypatch, capsys):
    """Neither form given: the positional is optional now, not absent."""
    from ciao import cli

    vault, _ = _queue_with(tmp_path, "[memory] durable lesson  _(from: Decisions)_")
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
    ])

    assert rc == 2
    assert "required" in capsys.readouterr().err


def test_add_then_dismiss_round_trips_a_multiline_fact_file(tmp_path, monkeypatch):
    """The workflow the curation skill prescribes, end to end.

    `memory-proposal-add` flattens whitespace because the queue is
    line-oriented Markdown; `remove_proposal_by_substring` then matches one
    line at a time. Without the same flattening on the dismiss side, a needle
    read from the very file the fact was filed from could never match its own
    bullet — the promotion would go unrecorded, the row would be re-asked every
    night, and the only way out would be the forbidden direct file edit.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    fact_file = tmp_path / "fact.txt"
    fact_file.write_text(
        "The user prefers $(whoami) shells\nand never uses `id`  directly.\n",
        encoding="utf-8",
    )

    assert cli.main([
        "memory-proposal-add",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--kind", "memory",
        "--text-file", str(fact_file),
    ]) == 0

    queue = vault / "Workspace" / "Memory-Proposals.md"
    assert "The user prefers $(whoami) shells and never uses `id` directly." in queue.read_text(
        encoding="utf-8"
    )

    assert cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--text-file", str(fact_file),
        "--promoted",
    ]) == 0

    assert "$(whoami)" not in queue.read_text(encoding="utf-8")


def test_dismiss_matches_a_row_written_with_repeated_whitespace(tmp_path, monkeypatch):
    """The curation skill appends `[review]` questions to the queue directly.

    Those rows keep whatever whitespace the writer used, and
    `remove_proposal_by_substring` compares against the raw line — so
    flattening the needle unconditionally would break the form that worked
    before `--text-file` existed.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        '# Memory Proposals\n\n- [review] Keep "foo  bar" in ciao:memory?  _(from: curation)_\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        'Keep "foo  bar" in ciao:memory?',
    ])

    assert rc == 0
    assert "foo  bar" not in queue.read_text(encoding="utf-8")


def test_dismiss_reports_an_undecodable_text_file(tmp_path, monkeypatch, capsys):
    """UnicodeDecodeError is a ValueError, so `except OSError` never saw it.

    Left uncaught it escaped as a traceback and aborted an unattended run.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    binary = tmp_path / "needle.bin"
    binary.write_bytes(b"\xff\xfe\x00garbage")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--text-file", str(binary),
    ])

    assert rc == 2
    assert "could not read" in capsys.readouterr().err


def test_add_reports_an_undecodable_text_file(tmp_path, monkeypatch, capsys):
    """The add command had the same hole."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    binary = tmp_path / "fact.bin"
    binary.write_bytes(b"\xff\xfe\x00garbage")

    rc = cli.main([
        "memory-proposal-add",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--kind", "memory",
        "--text-file", str(binary),
    ])

    assert rc == 2
    assert "could not read" in capsys.readouterr().err


def test_dismiss_refuses_an_ambiguous_needle_instead_of_falling_back(tmp_path, monkeypatch, capsys):
    """The exact scenario the two-pass fallback would have got wrong.

    `remove_proposal_by_substring` reports no match and an ambiguous match
    identically, so retrying the flattened needle after an AMBIGUOUS first pass
    can uniquely hit a differently spaced row and delete the wrong proposal.
    Here `foo  bar` matches two rows; flattened it would uniquely match the
    third, which must NOT be removed.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [memory] foo  bar one  _(from: Decisions)_\n"
        "- [memory] foo  bar two  _(from: Decisions)_\n"
        "- [memory] foo bar three  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "foo  bar",
    ])

    assert rc == 1
    assert "longer, unique substring" in capsys.readouterr().err
    # Every row survives — above all the third, which the fallback would have
    # deleted and recorded a dismissal for.
    body = queue.read_text(encoding="utf-8")
    assert "foo bar three" in body
    assert "foo  bar one" in body
    assert "foo  bar two" in body


def test_dismiss_refuses_when_raw_and_flattened_name_different_rows(tmp_path, monkeypatch, capsys):
    """The one-raw/one-flattened collision.

    A directly written row keeps its double space; a row filed through
    `memory-proposal-add` was normalised. A needle of `foo  bar` then matches
    the first uniquely and its flattened form matches the second uniquely — so
    taking whichever was tried first deletes a row nobody named and records an
    outcome for it, while the row the caller meant stays queued.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [review] foo  bar written directly  _(from: curation)_\n"
        "- [memory] foo bar filed through add  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "foo  bar",
    ])

    assert rc == 1
    assert "longer, unique substring" in capsys.readouterr().err
    body = queue.read_text(encoding="utf-8")
    assert "written directly" in body
    assert "filed through add" in body


def test_add_reads_the_payload_from_a_file(tmp_path, monkeypatch):
    """The payload is user-controlled too — a doc path or a person's name.

    `--text-file` kept the fact out of argv; a payload containing spaces, `;`,
    `$()` or quotes had no such door and was interpolated into the command.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    payload = tmp_path / "payload.txt"
    payload.write_text("Projects/A $(whoami) & B.md\n", encoding="utf-8")
    fact = tmp_path / "fact.txt"
    fact.write_text("The doc owns the rollout plan.\n", encoding="utf-8")

    rc = cli.main([
        "memory-proposal-add",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--kind", "project",
        "--payload-file", str(payload),
        "--text-file", str(fact),
    ])

    assert rc == 0
    body = (vault / "Workspace" / "Memory-Proposals.md").read_text(encoding="utf-8")
    assert "Projects/A $(whoami) & B.md" in body


def test_add_refuses_both_payload_forms(tmp_path, monkeypatch, capsys):
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    payload = tmp_path / "payload.txt"
    payload.write_text("Projects/A.md", encoding="utf-8")

    rc = cli.main([
        "memory-proposal-add",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--kind", "project",
        "--payload", "Projects/B.md",
        "--payload-file", str(payload),
        "a fact",
    ])

    assert rc == 2
    assert "not both" in capsys.readouterr().err


def test_add_says_previously_dismissed_rather_than_already_queued(tmp_path, monkeypatch, capsys):
    """A user who changed their mind must not be told their request landed.

    `append_proposals` returns None both for a fact already queued and for one
    already dismissed, and the CLI reported both as "already in the queue" —
    so an explicit /remember of a previously rejected fact created no row while
    the agent confirmed success.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    fact = "Prefers tabs over spaces."

    assert cli.main([
        "memory-proposal-add", "--workspace", str(tmp_path),
        "--vault-root", str(vault), "--kind", "memory", fact,
    ]) == 0
    assert cli.main([
        "memory-proposal-dismiss", "--workspace", str(tmp_path),
        "--vault-root", str(vault), fact,
    ]) == 0
    capsys.readouterr()

    assert cli.main([
        "memory-proposal-add", "--workspace", str(tmp_path),
        "--vault-root", str(vault), "--kind", "memory", fact,
    ]) == 0
    out = capsys.readouterr().out
    assert "Previously dismissed" in out
    assert "Already in the queue" not in out


def test_add_still_says_already_queued_for_a_pending_row(tmp_path, monkeypatch, capsys):
    """The other None case keeps its own, different message."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    fact = "Prefers tabs over spaces."

    for _ in range(2):
        assert cli.main([
            "memory-proposal-add", "--workspace", str(tmp_path),
            "--vault-root", str(vault), "--kind", "memory", fact,
        ]) == 0
    out = capsys.readouterr().out
    assert "Already in the queue" in out
    assert "Previously dismissed" not in out


def test_add_does_not_call_a_promoted_fact_dismissed(tmp_path, monkeypatch, capsys):
    """`record_promotion` writes to the same sidecar as `record_dismissal`.

    Reading every logged text would tell someone their fact had been rejected
    when it was in fact accepted and may already be live in the region — the
    opposite of what happened, and it would invite them to re-file something
    that is already there.
    """
    from ciao import cli
    from ciao.memory_proposals import record_promotion

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text("# Memory Proposals\n", encoding="utf-8")
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    fact = "Prefers tabs over spaces."

    record_promotion(queue, text=fact, kind="memory")
    capsys.readouterr()

    assert cli.main([
        "memory-proposal-add", "--workspace", str(tmp_path),
        "--vault-root", str(vault), "--kind", "memory", fact,
    ]) == 0
    out = capsys.readouterr().out
    assert "Previously dismissed" not in out
    # Nor "already in the queue": a promoted fact is not in the queue at all,
    # it is in its destination. Three states, three messages.
    assert "Already in the queue" not in out
    assert "Already promoted" in out
