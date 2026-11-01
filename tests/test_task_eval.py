"""Task eval harness: catalog validation, grading checks, runner records, CLI wiring.

Nothing here boots an engine or calls a model. ``IsolatedEngine`` is exercised
only through its pure environment builder; the runner uses a fake session.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ciao import behavioral_eval
from ciao import cli
from ciao import task_eval as te


def _snapshot(
    *,
    files: tuple[str, ...] = (),
    task_ids: tuple[str, ...] = (),
    schedule_ids: tuple[str, ...] = (),
    project_ids: tuple[str, ...] = (),
    saved: dict[str, bytes] | None = None,
) -> te.Snapshot:
    return te.Snapshot(
        task_ids=frozenset(task_ids),
        schedule_ids=frozenset(schedule_ids),
        project_ids=frozenset(project_ids),
        files=frozenset(files),
        saved=dict(saved or {}),
    )


def _no_api(method: str, path: str, body: Any = None) -> Any:
    raise AssertionError(f"unexpected API call {method} {path}")


def _ctx(
    root: Path,
    *,
    answer: str = "",
    api: Any = _no_api,
    snapshot: te.Snapshot | None = None,
    run_date: date = date(2026, 11, 1),
) -> te.GradeContext:
    return te.GradeContext(
        agent_root=root,
        answer=answer,
        api=api,
        snapshot=snapshot or _snapshot(),
        run_date=run_date,
    )


def _write_task(root: Path, task_id: str, body: dict[str, Any]) -> None:
    folder = root / task_id
    folder.mkdir(parents=True)
    (folder / "task.json").write_text(json.dumps(body), encoding="utf-8")


def _valid_body(task_id: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "tier": "easy",
        "category": "task_capture",
        "prompt": "Add a task: renew the bike insurance by Friday",
        "entry": {"kind": "prompt"},
        "setup": [],
        "requires": [],
        "timeout_s": 600,
        "checks": [{"kind": "answer_contains", "any": ["ok"]}],
    }


# --- Task 1: catalog validation -------------------------------------------------


def test_catalog_loads_and_validates() -> None:
    tasks = te.load_tasks()
    ids = {task.id for task in tasks}

    assert len(tasks) >= 4
    assert {"E1-task-capture", "E2-recall-guest-count", "E2-recall-abstain"} <= ids
    assert "E3-schedule-create" in ids
    assert {task.tier for task in tasks} <= {"easy", "medium", "hard"}


def test_catalog_rejects_unknown_check_kind(tmp_path: Path) -> None:
    body = _valid_body("bad-check")
    body["checks"] = [{"kind": "vibes_are_good"}]
    _write_task(tmp_path, "bad-check", body)

    with pytest.raises(te.TaskCatalogError):
        te.load_tasks(tmp_path)


def test_catalog_rejects_private_marker(tmp_path: Path) -> None:
    body = _valid_body("leaky")
    body["prompt"] = f"Ask {behavioral_eval.PRIVATE_MARKERS[0]} about the trip"
    _write_task(tmp_path, "leaky", body)

    with pytest.raises(te.TaskCatalogError):
        te.load_tasks(tmp_path)


# --- Task 1: grading checks, one test per check kind ----------------------------


def test_answer_contains_passes_on_any_and_fails_otherwise(tmp_path: Path) -> None:
    spec = {"kind": "answer_contains", "any": ["23", "twenty-three"]}

    assert te.CHECKS["answer_contains"](_ctx(tmp_path, answer="Twenty-Three so far"), spec).ok
    assert not te.CHECKS["answer_contains"](_ctx(tmp_path, answer="unknown"), spec).ok


def test_answer_excludes_flags_forbidden_figure(tmp_path: Path) -> None:
    spec = {"kind": "answer_excludes", "any": ["40"]}

    assert te.CHECKS["answer_excludes"](_ctx(tmp_path, answer="23 guests"), spec).ok
    assert not te.CHECKS["answer_excludes"](_ctx(tmp_path, answer="about 40 guests"), spec).ok


def test_abstains_regex_matches_declines_only(tmp_path: Path) -> None:
    spec = {"kind": "abstains"}

    assert te.CHECKS["abstains"](_ctx(tmp_path, answer="I don't know that one."), spec).ok
    assert te.CHECKS["abstains"](_ctx(tmp_path, answer="The vault does not record it."), spec).ok
    assert not te.CHECKS["abstains"](_ctx(tmp_path, answer="It is 555-0100."), spec).ok


def test_new_tasks_weekday_due_and_title(tmp_path: Path) -> None:
    rows = [
        {"id": "old", "title": "Old chore", "due": None},
        {"id": "t-new", "title": "Renew bike insurance", "due": "2026-11-06"},
    ]

    def api(method: str, path: str, body: Any = None) -> Any:
        assert method == "GET"
        assert path == "/api/tasks?workspace=personal"
        return {"tasks": rows}

    snap = _snapshot(task_ids=("old",))
    spec = {
        "kind": "new_tasks",
        "count": 1,
        "title_contains": ["insurance"],
        "due": "weekday:friday",
    }
    ctx = _ctx(tmp_path, api=api, snapshot=snap, run_date=date(2026, 11, 1))
    assert te.CHECKS["new_tasks"](ctx, spec).ok

    rows[1]["due"] = "2026-11-07"  # a Saturday
    assert not te.CHECKS["new_tasks"](ctx, spec).ok

    rows[1]["due"] = "2026-11-06"
    assert not te.CHECKS["new_tasks"](ctx, {**spec, "count": 2}).ok


def test_new_schedules_matches_days_and_time(tmp_path: Path) -> None:
    schedule = {
        "schedule_id": "s-new",
        "scope": "user",
        "frequency": "weekly",
        "days_of_week": ["sat"],
        "daily_time_utc": "09:00",
        "timezone_name": "Europe/Zurich",
        "prompt": "Water the garden, every Saturday",
    }

    def api(method: str, path: str, body: Any = None) -> Any:
        assert path == "/api/schedules"
        return [schedule, {"schedule_id": "s-sys", "scope": "system", "frequency": "daily"}]

    spec = {
        "kind": "new_schedules",
        "count": 1,
        "frequency": "weekly",
        "days_of_week": ["sat"],
        "time": "09:00",
        "prompt_contains": ["water"],
    }
    ctx = _ctx(tmp_path, api=api)
    assert te.CHECKS["new_schedules"](ctx, spec).ok

    schedule["days_of_week"] = ["sun"]
    assert not te.CHECKS["new_schedules"](ctx, spec).ok


def test_file_contains_and_excludes(tmp_path: Path) -> None:
    note = tmp_path / "Documents" / "insurance.md"
    note.parent.mkdir(parents=True)
    note.write_text("Bike insurance with Velosure, renews each November.", encoding="utf-8")

    contains = {"kind": "file_contains", "path": "Documents/insurance.md", "all": ["velosure"]}
    excludes = {"kind": "file_excludes", "path": "Documents/insurance.md", "any": ["hotel"]}
    assert te.CHECKS["file_contains"](_ctx(tmp_path), contains).ok
    assert te.CHECKS["file_excludes"](_ctx(tmp_path), excludes).ok

    missing = {"kind": "file_contains", "path": "Documents/insurance.md", "all": ["allianz"]}
    assert not te.CHECKS["file_contains"](_ctx(tmp_path), missing).ok
    note.write_text("Stayed at Hotel Boreale.", encoding="utf-8")
    assert not te.CHECKS["file_excludes"](_ctx(tmp_path), excludes).ok


def test_occurrences_detects_duplicate(tmp_path: Path) -> None:
    note = tmp_path / "log.md"
    note.write_text("Velosure renewal. Velosure again.", encoding="utf-8")

    assert te.CHECKS["occurrences"](
        _ctx(tmp_path), {"kind": "occurrences", "path": "log.md", "text": "velosure", "count": 2}
    ).ok
    assert not te.CHECKS["occurrences"](
        _ctx(tmp_path), {"kind": "occurrences", "path": "log.md", "text": "velosure", "count": 1}
    ).ok


def test_new_files_counts_only_files_created_after_snapshot(tmp_path: Path) -> None:
    (tmp_path / "old.md").write_text("old", encoding="utf-8")
    (tmp_path / "fresh.md").write_text("new", encoding="utf-8")
    snap = _snapshot(files=("old.md",))

    spec_none = {"kind": "new_files", "glob": "**/*.md", "max": 0}
    assert not te.CHECKS["new_files"](_ctx(tmp_path, snapshot=snap), spec_none).ok
    spec_one = {"kind": "new_files", "glob": "**/*.md", "count": 1}
    assert te.CHECKS["new_files"](_ctx(tmp_path, snapshot=snap), spec_one).ok
    assert te.CHECKS["new_files"](
        _ctx(tmp_path, snapshot=_snapshot(files=("old.md", "fresh.md"))),
        {"kind": "new_files", "glob": "**/*.md", "max": 0},
    ).ok


def test_frontmatter_valid_requires_type_and_updated(tmp_path: Path) -> None:
    good = tmp_path / "good.md"
    good.write_text("---\ntype: person\nupdated: 2026-10-01\n---\nBody\n", encoding="utf-8")
    spec = {"kind": "frontmatter_valid", "glob": "*.md"}
    assert te.CHECKS["frontmatter_valid"](_ctx(tmp_path), spec).ok

    good.write_text("---\ntype: person\n---\nBody\n", encoding="utf-8")
    assert not te.CHECKS["frontmatter_valid"](_ctx(tmp_path), spec).ok

    assert not te.CHECKS["frontmatter_valid"](
        _ctx(tmp_path), {"kind": "frontmatter_valid", "glob": "*.nothing"}
    ).ok


def test_links_resolve_flags_missing_target(tmp_path: Path) -> None:
    (tmp_path / "other.md").write_text("x", encoding="utf-8")
    note = tmp_path / "note.md"
    note.write_text(
        "See [other](other.md) and [web](https://example.com) and [gone](gone.md).",
        encoding="utf-8",
    )
    spec = {"kind": "links_resolve", "path": "note.md"}
    result = te.CHECKS["links_resolve"](_ctx(tmp_path), spec)
    assert not result.ok
    assert "gone.md" in result.detail

    note.write_text("See [other](other.md) and [web](https://example.com).", encoding="utf-8")
    assert te.CHECKS["links_resolve"](_ctx(tmp_path), spec).ok


def test_unchanged_compares_with_snapshot_bytes(tmp_path: Path) -> None:
    keep = tmp_path / "keep.md"
    keep.write_bytes(b"same")
    snap = _snapshot(saved={"keep.md": b"same"})
    spec = {"kind": "unchanged", "path": "keep.md"}
    assert te.CHECKS["unchanged"](_ctx(tmp_path, snapshot=snap), spec).ok

    keep.write_bytes(b"edited")
    assert not te.CHECKS["unchanged"](_ctx(tmp_path, snapshot=snap), spec).ok


def test_task_attempt_counts_outcome(tmp_path: Path) -> None:
    def api(method: str, path: str, body: Any = None) -> Any:
        if path.startswith("/api/tasks?"):
            return {"tasks": [{"id": "t-1", "title": "Renew passport"}]}
        assert path == "/api/tasks/t-1/attempts?workspace=personal"
        return {"attempts": [{"outcome": "done"}]}

    spec = {"kind": "task_attempt", "title_contains": "passport", "outcome": "done", "count": 1}
    assert te.CHECKS["task_attempt"](_ctx(tmp_path, api=api), spec).ok

    blocked = {**spec, "outcome": "blocked"}
    assert not te.CHECKS["task_attempt"](_ctx(tmp_path, api=api), blocked).ok


def test_command_check_uses_sys_executable(tmp_path: Path) -> None:
    passing = {"kind": "command", "argv": ["{python}", "-c", "import sys"], "cwd": "."}
    assert te.CHECKS["command"](_ctx(tmp_path), passing).ok

    failing = {"kind": "command", "argv": ["{python}", "-c", "raise SystemExit(4)"], "cwd": "."}
    assert not te.CHECKS["command"](_ctx(tmp_path), failing).ok


def test_new_projects_matches_name_and_snapshot(tmp_path: Path) -> None:
    projects = [
        {"project_id": "proj-old", "name": "Garden shed"},
        {"project_id": "proj-new", "name": "Summer party"},
    ]

    def api(method: str, path: str, body: Any = None) -> Any:
        assert path == "/api/projects"
        return projects

    snap = _snapshot(project_ids=("proj-old",))
    spec = {"kind": "new_projects", "count": 1, "name_contains": ["summer"]}
    assert te.CHECKS["new_projects"](_ctx(tmp_path, api=api, snapshot=snap), spec).ok

    spec_bad = {"kind": "new_projects", "count": 1, "name_contains": ["winter"]}
    assert not te.CHECKS["new_projects"](_ctx(tmp_path, api=api, snapshot=snap), spec_bad).ok


# --- Task 3/4: base fixture vault and the E1-E3 catalog ---------------------------

FIXTURE = te.TASKS_ROOT / "_fixture"


def test_fixture_guest_counts_match_e2() -> None:
    import csv
    from collections import Counter

    path = FIXTURE / "projects" / "active" / "summer-party" / "guests.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["name", "rsvp", "dietary"]
        rows = list(reader)

    assert Counter(row["rsvp"] for row in rows) == {"yes": 23, "no": 6, "maybe": 4}


def test_fixture_notes_have_frontmatter() -> None:
    ctx = _ctx(FIXTURE)
    result = te.CHECKS["frontmatter_valid"](ctx, {"kind": "frontmatter_valid", "glob": "**/*.md"})

    assert result.ok, result.detail


# --- Task 2: engine environment isolation ------------------------------------------


def test_isolated_engine_env_strips_live_vars(tmp_path: Path) -> None:
    base = {
        "CIAO_WORKSPACE": "/live/workspace",
        "PWA_AUTH_TOKEN": "live-token",
        "GWS_PROFILE": "work",
        "HOME": "/Users/someone",
        "PATH": "/usr/bin",
    }
    workdir = tmp_path / "T"
    ws = workdir / "ws"

    env = te.engine_env(base, workdir, ws)

    assert env["CIAO_WORKSPACE"] == str(ws)
    assert "PWA_AUTH_TOKEN" not in env
    assert "GWS_PROFILE" not in env
    assert Path(env["CIAO_MEMORY_DIR"]).is_relative_to(workdir)
    assert Path(env["CIAO_LAUNCH_AGENTS_DIR"]).is_relative_to(workdir)
    assert env["PWA_HOST"] == "127.0.0.1"
    assert env["CIAO_NO_BROWSER"] == "1"
    assert env["PATH"] == "/usr/bin"
    assert base["CIAO_WORKSPACE"] == "/live/workspace"  # the input is not mutated


# --- Task 5: runner, usage, summary, compare ----------------------------------------


class FakeSession:
    """An ``EngineSession`` stand-in: no process, no HTTP, canned answers."""

    def __init__(self, root: Path, *, answer: str = "I saw the insurance task.") -> None:
        self.agent_root = root
        self.tasks: list[dict[str, Any]] = []
        self.answer = answer
        self.closed = False
        self.stopped: list[str] = []

    def api(self, method: str, path: str, body: Any = None) -> Any:
        if path.startswith("/api/tasks?"):
            return {"tasks": list(self.tasks)}
        if path == "/api/schedules" or path == "/api/projects":
            return []
        raise AssertionError(f"unexpected {method} {path}")

    def start_prompt(self, prompt: str, *, provider: str, model: str) -> str:
        self.tasks.append({"id": "t-new", "title": "Renew bike insurance", "due": "2026-11-06"})
        return "chat-1"

    def delegate(self, task_id: str, revision: str, *, provider: str, model: str) -> str:
        raise AssertionError("prompt entry must not delegate")

    def wait_settled(self, timeout_s: float) -> bool:
        return True

    def stop_chat(self, chat_id: str) -> None:
        self.stopped.append(chat_id)

    def answer_text(self) -> str:
        return self.answer

    def usage(self) -> dict[str, Any]:
        return {
            "tokens": {"input": 100, "output": 20, "cache_read": 50, "cache_write": 10},
            "tool_calls": 2,
            "tool_errors": 1,
            "ciao_calls": 3,
            "ciao_errors": 0,
            "cost_usd": 0.5,
            "chat_ids": ["chat-1"],
        }

    def close(self) -> None:
        self.closed = True


def _insurance_task(**overrides: Any) -> te.EvalTask:
    fields: dict[str, Any] = {
        "id": "X-insurance",
        "tier": "easy",
        "category": "task_capture",
        "prompt": "Add a task: renew the bike insurance by Friday",
        "entry": {"kind": "prompt"},
        "setup": [],
        "requires": [],
        "timeout_s": 60.0,
        "checks": [
            {"kind": "new_tasks", "count": 1, "title_contains": ["insurance"], "due": "2026-11-06"}
        ],
        "overlay": None,
    }
    fields.update(overrides)
    return te.EvalTask(**fields)


@pytest.fixture
def stable_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(te, "provider_version", lambda provider: "fake 1.0")
    monkeypatch.setattr(te, "code_revision", lambda: "abc123def456-dirty")


def test_run_task_with_fake_session_passes_and_records_usage(
    tmp_path: Path, stable_provenance: None
) -> None:
    session = FakeSession(tmp_path)

    record = te.run_task(
        _insurance_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert set(record) >= {
        "schema", "label", "task_id", "tier", "category", "provider", "model", "repeat",
        "code_revision", "provider_version", "catalog_sha256", "started_at", "status",
        "skip_reason", "error", "checks", "answer", "tokens", "tool_calls", "tool_errors",
        "ciao_calls", "ciao_errors", "cost_usd", "wall_s", "timed_out", "chat_ids",
    }
    assert record["answer"] == "I saw the insurance task."
    assert record["schema"] == "ciao-task-eval/1"
    assert record["status"] == "passed"
    assert record["checks"][0]["ok"] is True
    assert record["tokens"] == {
        "input": 100,
        "output": 20,
        "cache_read": 50,
        "cache_write": 10,
        "context_total": 160,
    }
    assert record["tool_calls"] == 2
    assert record["cost_usd"] == 0.5
    assert record["chat_ids"] == ["chat-1"]
    assert record["timed_out"] is False
    assert record["code_revision"] == "abc123def456-dirty"
    assert session.closed is True


def test_run_task_skips_missing_capability(tmp_path: Path, stable_provenance: None) -> None:
    calls: list[te.EvalTask] = []

    def factory(task: te.EvalTask) -> FakeSession:
        calls.append(task)
        return FakeSession(tmp_path)

    record = te.run_task(
        _insurance_task(requires=["gws_fake"]),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=factory,
        capabilities=frozenset(),
    )

    assert record["status"] == "skipped"
    assert "gws_fake" in record["skip_reason"]
    assert calls == []


def test_run_task_error_never_raises(tmp_path: Path, stable_provenance: None) -> None:
    def factory(task: te.EvalTask) -> FakeSession:
        raise RuntimeError("boom")

    record = te.run_task(
        _insurance_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=factory,
    )

    assert record["status"] == "error"
    assert record["error"].startswith("RuntimeError(")
    assert "boom" in record["error"]


def test_usage_sums_all_transcripts(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    turns_a = [
        {
            "response": "first",
            "usage": {
                "input_tokens": "10",
                "output_tokens": "5",
                "cache_read_input_tokens": "3",
                "cache_creation_input_tokens": "2",
            },
            "tool_calls": [{"name": "x", "input_chars": 3}, {"name": "y", "error": "bad"}],
        }
    ]
    turns_b = [
        {"response": "second", "usage": {"input_tokens": "1", "output_tokens": "1"}, "tool_calls": []},
        {"response": "third", "usage": {"input_tokens": "4"}, "tool_calls": [{"name": "z"}]},
    ]
    for chat_id, turns in (("chat-a", turns_a), ("chat-b", turns_b)):
        folder = runtime / "transcripts" / chat_id
        folder.mkdir(parents=True)
        (folder / "claude.json").write_text(json.dumps({"turns": turns}), encoding="utf-8")
    (runtime / "mcp_tool_calls.jsonl").write_text(
        json.dumps({"tool": "tasks_list", "status": "ok"})
        + "\n"
        + json.dumps({"tool": "tasks_add", "status": "error"})
        + "\n",
        encoding="utf-8",
    )
    (runtime / "state.json").write_text(json.dumps({"bot_state": {"cost": 1.25}}), encoding="utf-8")

    usage = te.collect_usage(runtime)

    assert usage["tokens"] == {"input": 15, "output": 6, "cache_read": 3, "cache_write": 2}
    assert usage["tool_calls"] == 3
    assert usage["tool_errors"] == 1
    assert usage["ciao_calls"] == 2
    assert usage["ciao_errors"] == 1
    assert usage["cost_usd"] == 1.25
    assert usage["chat_ids"] == ["chat-a", "chat-b"]


def _row(
    *,
    task_id: str = "E2-recall-guest-count",
    status: str = "passed",
    context_total: int = 100,
    output: int = 10,
    wall_s: float = 10.0,
    cost_usd: float | None = 0.1,
    label: str = "baseline",
    timed_out: bool = False,
) -> dict[str, Any]:
    return {
        "schema": "ciao-task-eval/1",
        "label": label,
        "task_id": task_id,
        "provider": "claude",
        "model": "haiku",
        "code_revision": "rev-a",
        "status": status,
        "tokens": {"input": context_total, "output": output, "cache_read": 0, "cache_write": 0,
                   "context_total": context_total},
        "tool_errors": 1,
        "wall_s": wall_s,
        "cost_usd": cost_usd,
        "timed_out": timed_out,
    }


def test_summarize_and_compare() -> None:
    baseline = [_row(context_total=100, wall_s=10.0), _row(status="failed", context_total=200, wall_s=20.0)]
    candidate = [_row(label="candidate", context_total=150, wall_s=15.0, cost_usd=0.2)]

    summary = te.summarize(baseline)
    assert len(summary) == 1
    group = summary[0]
    assert group["n"] == 2
    assert group["pass_rate"] == 0.5
    assert group["median_context_total"] == 150
    assert group["median_wall_s"] == 15.0
    assert group["tool_errors"] == 2

    comparison = te.compare(baseline, candidate)
    assert len(comparison) == 1
    item = comparison[0]
    assert item["task_id"] == "E2-recall-guest-count"
    assert item["delta"]["pass_rate"] == pytest.approx(0.5)
    assert item["delta"]["context_total_pct"] == pytest.approx(0.0)
    assert item["delta"]["wall_s_pct"] == pytest.approx(0.0)

    table = te.render_summary(summary)
    assert "| E2-recall-guest-count" in table
    assert "E2-recall-guest-count" in te.render_comparison(comparison)


def test_read_jsonl_loads_every_row(tmp_path: Path) -> None:
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    first.write_text(json.dumps(_row()) + "\n", encoding="utf-8")
    second.write_text(json.dumps(_row(label="candidate")) + "\n\n", encoding="utf-8")

    rows = te.read_jsonl([first, second])

    assert [row["label"] for row in rows] == ["baseline", "candidate"]


# --- Task 6: CLI wiring -------------------------------------------------------------


def test_cli_eval_tasks_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["eval", "tasks", "list", "--json"]) == 0

    out = json.loads(capsys.readouterr().out)
    ids = {item["id"] for item in out}
    assert {"E1-task-capture", "E2-recall-guest-count", "E2-recall-abstain", "E3-schedule-create"} <= ids


# --- Fix round: agent root, answer tail, CLI mismatch preflight, pinned workspace --


def test_engine_agent_root_is_the_personal_workspace(tmp_path: Path) -> None:
    engine = te.IsolatedEngine(tmp_path / "T", keep_workdir=True)

    assert engine.agent_root == tmp_path / "T" / "ws" / "personal"


def test_catalog_new_file_globs_are_agent_root_relative() -> None:
    for task in te.load_tasks():
        for spec in task.checks:
            if spec["kind"] == "new_files":
                assert spec["glob"].startswith("memory-vault/"), (task.id, spec["glob"])


def test_snapshot_excludes_host_project_scaffold(tmp_path: Path) -> None:
    agent_root = tmp_path / "personal"
    scaffold = agent_root / "memory-vault" / "projects" / "active" / "task-eval-host"
    scaffold.mkdir(parents=True)
    (scaffold / "task-eval-host.md").write_text("scaffold", encoding="utf-8")

    def api(method: str, path: str, body: Any = None) -> Any:
        return {"tasks": []} if path.startswith("/api/tasks?") else []

    snap = te.take_snapshot(agent_root, api, keep=())
    ctx = _ctx(agent_root, snapshot=snap)
    projects_glob = {"kind": "new_files", "glob": "memory-vault/projects/**", "max": 0}
    assert te.CHECKS["new_files"](ctx, projects_glob).ok

    (agent_root / "memory-vault" / "projects" / "active" / "x.md").write_text("new", encoding="utf-8")
    result = te.CHECKS["new_files"](_ctx(agent_root, snapshot=snap), projects_glob)
    assert not result.ok
    assert "x.md" in result.detail
    assert "task-eval-host" not in result.detail


def test_run_task_records_answer_tail(tmp_path: Path, stable_provenance: None) -> None:
    long_answer = "a" * 2500 + "THE-END"
    session = FakeSession(tmp_path, answer=long_answer)

    record = te.run_task(
        _insurance_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert record["answer"] == long_answer[-2000:]
    assert len(record["answer"]) == 2000
    assert record["answer"].endswith("THE-END")


def _completed(stdout: str, returncode: int = 0) -> Any:
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_cli_package_mismatch_is_none_for_the_same_package(monkeypatch: pytest.MonkeyPatch) -> None:
    harness_init = Path(te.__file__).resolve().parent / "__init__.py"
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return _completed(f"{harness_init}\n")

    monkeypatch.setattr(te.subprocess, "run", fake_run)

    assert te.cli_package_mismatch() is None
    assert calls[0][1:3] == ["-I", "-c"]


def test_cli_package_mismatch_names_both_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        te.subprocess, "run",
        lambda argv, **kwargs: _completed("/Users/someone/repos/ciaobot/ciao/__init__.py\n"),
    )

    message = te.cli_package_mismatch()

    assert message is not None
    assert "/Users/someone/repos/ciaobot/ciao" in message
    assert str(Path(te.__file__).resolve().parent) in message
    assert "uv venv .venv-eval" in message
    assert ".venv-eval/bin/ciao eval tasks run" in message


def test_cli_package_mismatch_reports_failed_isolated_import(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(te.subprocess, "run", lambda argv, **kwargs: _completed("", returncode=1))

    message = te.cli_package_mismatch()

    assert message is not None
    assert "could not import" in message


def test_eval_tasks_run_refuses_before_running_on_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse_run(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("run_suite must not start on a mismatch")

    monkeypatch.setattr(te, "cli_package_mismatch", lambda: "MISMATCH: fix the install")
    monkeypatch.setattr(te, "run_suite", refuse_run)

    assert cli.main(["eval", "tasks", "run", "--model", "haiku"]) == 2
    assert "MISMATCH: fix the install" in capsys.readouterr().err


def _registry_entry(ws: Path, name: str) -> dict[str, Any]:
    raw = (ws / ".runtime" / "workspaces.json").read_text(encoding="utf-8")
    entries: Any = json.loads(raw)
    return next(entry for entry in entries if entry["name"] == name)


def test_prepare_workspace_pins_requested_provider_and_model(tmp_path: Path) -> None:
    from ciao.config import CiaoConfig, _parse_workspaces_json

    ws = te.prepare_workspace(tmp_path / "T", "claude", "haiku", _insurance_task())

    assert ws == tmp_path / "T" / "ws"
    assert (ws / "personal" / "memory-vault" / "People" / "tomas-berg.md").is_file()
    entry = _registry_entry(ws, "personal")
    assert entry["default_provider"] == "claude"
    assert entry["provider_overrides"] == {"claude": {"model": "haiku"}}

    registry = ws / ".runtime" / "workspaces.json"
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=ws,
        state_path=ws / ".runtime" / "state.json",
        media_root=ws / ".runtime" / "media",
        workspaces=_parse_workspaces_json(registry.read_text(encoding="utf-8")),
    )
    assert config.default_provider_for_workspace("personal") == "claude"
    assert config.default_model_for_workspace("personal", "claude") == "haiku"
