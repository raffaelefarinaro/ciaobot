"""Task eval harness: catalog validation, grading checks, runner records, CLI wiring.

Nothing here boots an engine or calls a model. ``IsolatedEngine`` is exercised
only through its pure environment builder; the runner uses a fake session.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from ciao import agent_paths
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
    # The agent's sandbox cannot write into the workdir; the per-user temp default
    # is where `ciao memory-proposal-dismiss` takes its queue lock.
    assert "CIAO_QUEUE_LOCK_DIR" not in env
    assert base["CIAO_WORKSPACE"] == "/live/workspace"  # the input is not mutated


# --- Task 5: runner, usage, summary, compare ----------------------------------------


class FakeSession:
    """An ``EngineSession`` stand-in: no process, no HTTP, canned answers."""

    def __init__(
        self,
        root: Path,
        *,
        answer: str = "I saw the insurance task.",
        provider_errors: int = 0,
        provider_error: str | None = None,
        settled: bool = True,
        trace: Sequence[Mapping[str, Any]] = (),
        session_calls: Sequence[Mapping[str, Any]] = (),
        model_calls: int | None = None,
    ) -> None:
        self.agent_root = root
        self.settled = settled
        self.tasks: list[dict[str, Any]] = []
        self.answer = answer
        self.provider_errors = provider_errors
        self.provider_error = provider_error
        self.trace = list(trace)
        self.session_calls = list(session_calls)
        self.model_calls = model_calls
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
        return self.settled

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
            "provider_errors": self.provider_errors,
            "provider_error": self.provider_error,
            "trace": list(self.trace),
        }

    def session_trace(self) -> tuple[list[dict[str, Any]], int | None]:
        return list(self.session_calls), self.model_calls

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


def test_run_task_provider_error_with_failed_checks_is_error(
    tmp_path: Path, stable_provenance: None
) -> None:
    session = FakeSession(
        tmp_path,
        provider_errors=1,
        provider_error="weekly usage limit reached",
    )

    record = te.run_task(
        _insurance_task(checks=[{"kind": "answer_contains", "any": ["zzz"]}]),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert record["status"] == "error"
    assert record["error"].startswith("provider error:")
    assert "weekly usage limit reached" in record["error"]
    assert record["provider_errors"] == 1


def test_run_task_provider_error_on_settle_timeout_with_passing_checks_is_error(
    tmp_path: Path, stable_provenance: None
) -> None:
    session = FakeSession(
        tmp_path,
        provider_errors=1,
        provider_error="weekly usage limit reached",
        settled=False,
    )

    record = te.run_task(
        _insurance_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert record["status"] == "error"
    assert record["error"].startswith("provider error:")
    assert record["timed_out"] is True


def test_run_task_provider_error_with_passing_checks_stays_passed(
    tmp_path: Path, stable_provenance: None
) -> None:
    session = FakeSession(
        tmp_path,
        provider_errors=1,
        provider_error="transient overload",
    )

    record = te.run_task(
        _insurance_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert record["status"] == "passed"
    assert record["error"] is None
    assert record["provider_errors"] == 1


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


def test_collect_usage_counts_provider_error_turns(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    folder = runtime / "transcripts" / "chat-a"
    folder.mkdir(parents=True)
    turns = [
        {"response": "weekly usage limit reached", "usage": {}, "is_error": True},
        {"response": "normal answer", "usage": {"input_tokens": "2"}, "tool_calls": []},
    ]
    (folder / "ollama.json").write_text(json.dumps({"turns": turns}), encoding="utf-8")

    usage = te.collect_usage(runtime)

    assert usage["provider_errors"] == 1
    assert usage["provider_error"].startswith("weekly usage limit")


def test_collect_usage_builds_trace_from_tool_events(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    chats = {
        "chat-a": [
            {
                "response": "one",
                "tool_events": [
                    {"id": "toolu_1", "name": "Bash", "input": {"summary": "ls memory-vault"}},
                    {"id": "toolu_2", "name": "Read", "input": {"summary": "x" * 250}, "error": True},
                ],
            }
        ],
        "chat-b": [
            {
                "response": "two",
                "tool_events": [
                    {"id": "toolu_3", "name": "Write", "input": {"summary": "note"}, "error": False},
                ],
            }
        ],
    }
    for chat_id, turns in chats.items():
        folder = runtime / "transcripts" / chat_id
        folder.mkdir(parents=True)
        (folder / "claude.json").write_text(json.dumps({"turns": turns}), encoding="utf-8", newline="")

    usage = te.collect_usage(runtime)

    assert usage["trace"] == [
        {"id": "toolu_1", "chat_id": "chat-a", "name": "Bash", "input": "ls memory-vault", "error": False},
        {"id": "toolu_2", "chat_id": "chat-a", "name": "Read", "input": "x" * 200, "error": True},
        {"id": "toolu_3", "chat_id": "chat-b", "name": "Write", "input": "note", "error": False},
    ]


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


def test_summarize_excludes_errors_from_pass_rate() -> None:
    rows = [
        _row(status="passed"),
        _row(status="error"),
    ]

    group = te.summarize(rows)[0]

    assert group["pass_rate"] == 1.0
    assert group["errors"] == 1


def test_summarize_excludes_error_rows_from_medians() -> None:
    blocked = _row(status="error", context_total=0, output=0, wall_s=0.5, cost_usd=0.0)
    rows = [
        _row(status="passed", context_total=100, output=10, wall_s=10.0, cost_usd=0.1),
        _row(status="failed", context_total=300, output=30, wall_s=30.0, cost_usd=0.3),
        blocked,
    ]

    group = te.summarize(rows)[0]

    assert group["median_context_total"] == 200
    assert group["median_output"] == 20
    assert group["median_wall_s"] == 20.0
    assert group["median_cost_usd"] == pytest.approx(0.2)


def _claude_session_folder(home: Path, agent_root: Path) -> Path:
    folder = agent_paths.claude_projects_dir(agent_root)
    folder = home / ".claude" / "projects" / folder.name
    folder.mkdir(parents=True)
    (folder / "x.jsonl").write_text("{}\n", encoding="utf-8")
    return folder


def _isolated_engine_at(workdir: Path, *, keep_workdir: bool) -> te.IsolatedEngine:
    workdir.mkdir(parents=True)
    engine = te.IsolatedEngine(workdir, keep_workdir=keep_workdir)
    engine.agent_root.mkdir(parents=True)
    return engine


def test_close_removes_claude_project_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    engine = _isolated_engine_at(tmp_path / "ciao-task-eval-run", keep_workdir=False)
    folder = _claude_session_folder(home, engine.agent_root)

    engine.close()

    assert not folder.exists()
    assert not engine.workdir.exists()


def test_close_keeps_claude_project_folder_with_keep_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    engine = _isolated_engine_at(tmp_path / "ciao-task-eval-run", keep_workdir=True)
    folder = _claude_session_folder(home, engine.agent_root)

    engine.close()

    assert folder.is_dir()
    assert engine.workdir.is_dir()


def test_claude_project_cleanup_refuses_non_eval_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    real_root = tmp_path / "my-real-project" / "personal"
    real_root.mkdir(parents=True)
    folder = _claude_session_folder(home, real_root)

    assert te._claude_project_dirs(real_root) == []
    assert folder.is_dir()


def test_claude_project_dirs_includes_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    real = tmp_path / "ciao-task-eval-real"
    (real / "ws" / "personal").mkdir(parents=True)
    link = tmp_path / "ciao-task-eval-link"
    link.symlink_to(real, target_is_directory=True)
    unresolved_root = link / "ws" / "personal"
    _claude_session_folder(home, unresolved_root)
    _claude_session_folder(home, unresolved_root.resolve())

    found = te._claude_project_dirs(unresolved_root)

    assert len(found) == 2


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
    # The message names the resolved package directory, which is drive-qualified
    # with backslashes on Windows; compare the platform's own spelling.
    assert str(Path("/Users/someone/repos/ciaobot/ciao/__init__.py").resolve().parent) in message
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


# --- Hard tier: H1 synthesis, H2 code bug fix, H3 doc revision ---------------------

H1_ID = "H1-multi-source-synthesis"
H2_ID = "H2-code-bugfix"
H3_ID = "H3-doc-revision"
HARD_IDS = (H1_ID, H2_ID, H3_ID)


def _task(task_id: str) -> te.EvalTask:
    return next(task for task in te.load_tasks() if task.id == task_id)


def _overlay(task_id: str) -> Path:
    return te.TASKS_ROOT / task_id / "overlay"


def test_hard_tasks_load() -> None:
    tasks = {task.id: task for task in te.load_tasks()}

    for task_id in HARD_IDS:
        assert task_id in tasks, task_id
        assert tasks[task_id].tier == "hard", task_id


def test_h1_inbox_plants_facts() -> None:
    inbox = _overlay(H1_ID) / "memory-vault" / "Documents" / "inbox"
    texts = {path.name: path.read_text(encoding="utf-8") for path in sorted(inbox.glob("*.md"))}
    assert set(texts) == {"slack-thread.md", "call-transcript.md", "caterer-email.md"}

    joined = "\n".join(texts.values())
    assert "Forno Rosso" in joined
    assert "38" in joined
    assert "30%" in joined
    assert "2027-03-12" in joined or "12 March 2027" in joined
    assert [name for name, text in texts.items() if "45" in text] == ["slack-thread.md"]


def test_h2_seeded_bug_fails_and_fix_passes(tmp_path: Path) -> None:
    project = tmp_path / "code" / "invoice_tool"
    shutil.copytree(_overlay(H2_ID) / "code" / "invoice_tool", project)
    run = [sys.executable, "-m", "unittest", "-q", "test_invoice"]

    broken = subprocess.run(run, cwd=project, capture_output=True, text=True, check=False)
    assert broken.returncode != 0, broken.stderr

    source = project / "invoice.py"
    head, marker, _ = source.read_text(encoding="utf-8").partition("def total(")
    assert marker, "invoice.py must define total()"
    fixed = (
        "def total(lines: list[tuple[float, int]], discount_pct: float = 0.0, vat_pct: float = 8.1) -> float:\n"
        "    subtotal = sum(price * qty for price, qty in lines)\n"
        "    return round(subtotal * (1 - discount_pct / 100) * (1 + vat_pct / 100), 2)\n"
    )
    source.write_text(head + fixed, encoding="utf-8")

    fixed_run = subprocess.run(run, cwd=project, capture_output=True, text=True, check=False)
    assert fixed_run.returncode == 0, fixed_run.stderr


def test_h3_fixture_starts_failing(tmp_path: Path) -> None:
    root = tmp_path / "personal"
    shutil.copytree(te.TASKS_ROOT / te.FIXTURE_DIRNAME, root / "memory-vault")
    shutil.copytree(_overlay(H3_ID), root, dirs_exist_ok=True)
    ctx = _ctx(root)

    for spec in _task(H3_ID).checks:
        kind = str(spec["kind"])
        result = te.CHECKS[kind](ctx, spec)
        preserves_untouched_text = "Wash your dishes" in spec.get("all", [])
        if kind in ("file_contains", "file_excludes") and not preserves_untouched_text:
            assert not result.ok, f"{kind} should fail on the untouched overlay: {result.detail}"
        if kind == "links_resolve":
            assert result.ok, result.detail


def test_h3_correct_revision_passes_file_checks(tmp_path: Path) -> None:
    root = tmp_path / "personal"
    shutil.copytree(te.TASKS_ROOT / te.FIXTURE_DIRNAME, root / "memory-vault")
    shutil.copytree(_overlay(H3_ID), root, dirs_exist_ok=True)
    note = root / "memory-vault" / "Documents" / "house-rules.md"
    text = note.read_text(encoding="utf-8")
    text = text.replace("start at 23:00", "start at 22:00").replace("recieve", "receive")
    text = text.replace("Smoking on the balcony is allowed after 20:00.\n", "")
    text += "\n## Guests\n\nGuests may stay at most 3 nights.\n"
    note.write_text(text, encoding="utf-8", newline="")
    ctx = _ctx(root)

    for spec in _task(H3_ID).checks:
        kind = str(spec["kind"])
        if kind in ("new_files", "unchanged"):
            continue  # need a pre-prompt snapshot
        result = te.CHECKS[kind](ctx, spec)
        assert result.ok, f"{kind} should pass on a correct revision: {result.detail}"
# --- Medium tier catalog ------------------------------------------------------------

MEDIUM_TASK_IDS = (
    "M1-proposal-merge",
    "M2-project-exists",
    "M2-project-missing",
    "M3-delegated-task",
)


def test_medium_tasks_load() -> None:
    by_id = {task.id: task for task in te.load_tasks()}

    assert set(MEDIUM_TASK_IDS) <= set(by_id)
    for task_id in MEDIUM_TASK_IDS:
        assert by_id[task_id].tier == "medium", task_id


def test_m1_overlay_has_one_queued_proposal() -> None:
    from ciao import memory_proposals

    proposals_path = te.TASKS_ROOT / "M1-proposal-merge" / "overlay" / "memory-vault" / "Workspace" / "Memory-Proposals.md"

    rows = memory_proposals.list_proposals(proposals_path)

    assert len(rows) == 1
    assert "Turin" in rows[0]["text"]


def test_medium_fixture_facts_consistent() -> None:
    assert "Bologna" in (FIXTURE / "People" / "lena-moretti.md").read_text(encoding="utf-8")
    assert "November" in (FIXTURE / "Documents" / "insurance-overview.md").read_text(encoding="utf-8")


# --- Fix round 2: env isolation, process groups, tolerant usage, symlinks, fences ---


def test_harness_subprocesses_pass_base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        if argv[0] == "git":
            return _completed("abc123\n" if "rev-parse" in argv else "")
        if "--version" in argv:
            return _completed("claude 1.2\n")
        return _completed(f"{Path(te.__file__).resolve().parent / '__init__.py'}\n")

    monkeypatch.setattr(te.subprocess, "run", fake_run)

    te.provider_version("claude")
    te.code_revision()
    te.cli_package_mismatch()

    assert len(calls) == 4
    assert all(kwargs.get("env") is te._BASE_ENV for _, kwargs in calls)


def test_command_check_runs_with_stripped_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        te,
        "_BASE_ENV",
        {"PATH": "/usr/bin", "HOME": "/home/x", "CIAO_WORKSPACE": "/live",
         "PWA_AUTH_TOKEN": "secret", "GWS_PROFILE": "work"},
    )
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs)
        return _completed("")

    monkeypatch.setattr(te.subprocess, "run", fake_run)

    result = te.CHECKS["command"](_ctx(tmp_path), {"kind": "command", "argv": ["{python}", "-c", "pass"]})

    assert result.ok
    assert seen["env"] == {"PATH": "/usr/bin", "HOME": "/home/x"}


def test_resolve_in_root_refuses_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("TOKEN", encoding="utf-8")
    (root / "real.md").write_text("inside", encoding="utf-8")
    try:
        (root / "escape.md").symlink_to(outside / "secret.md")
        (root / "alias.md").symlink_to(root / "real.md")
    except OSError:
        pytest.skip("symlink creation unavailable on this platform")

    assert te._resolve_in_root(root, "alias.md").read_text(encoding="utf-8") == "inside"
    with pytest.raises(ValueError):
        te._resolve_in_root(root, "escape.md")
    result = te.CHECKS["file_contains"](
        _ctx(root), {"kind": "file_contains", "path": "escape.md", "all": ["token"]}
    )
    assert not result.ok


def test_close_kills_the_whole_engine_process_group(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("process groups are POSIX-only")
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time, pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(600)\n"
    )
    leader = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    deadline = time.monotonic() + 15
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    engine = te.IsolatedEngine(tmp_path / "T", keep_workdir=True)
    engine._proc = leader

    engine.close()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail("grandchild process survived engine close")


def test_collect_usage_tolerates_truncated_telemetry_line(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "mcp_tool_calls.jsonl").write_text(
        json.dumps({"status": "ok"}) + "\n"
        + json.dumps({"status": "error"}) + "\n"
        + '{"status": "ok", "tool": "tas',
        encoding="utf-8",
    )

    usage = te.collect_usage(runtime)

    assert usage["ciao_calls"] == 2
    assert usage["ciao_errors"] == 1


def test_collect_usage_tolerates_truncated_state(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "state.json").write_text('{"bot_state": {"cost": ', encoding="utf-8")

    usage = te.collect_usage(runtime)

    assert usage["cost_usd"] is None


def test_resolve_in_root_allows_symlink_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.md").write_text("inside", encoding="utf-8")
    try:
        (root / "alias.md").symlink_to(root / "real.md")
    except OSError:
        pytest.skip("symlink creation unavailable on this platform")

    assert te._resolve_in_root(root, "alias.md").read_text(encoding="utf-8") == "inside"


def test_frontmatter_closing_fence_must_be_its_own_line(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text(
        "---\ntype: person\nupdated: 2026-10-01\n----\nBody\n", encoding="utf-8"
    )

    result = te.CHECKS["frontmatter_valid"](
        _ctx(tmp_path), {"kind": "frontmatter_valid", "path": "note.md"}
    )

    assert not result.ok
    assert "unterminated" in result.detail


def test_close_kills_a_sigterm_ignoring_straggler_without_waitid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # os.waitid exists on macOS only from CPython 3.13; the floor is 3.12 (#2435).
    # The grandchild ignores SIGTERM, so only the group SIGKILL after the reap clears it.
    if sys.platform == "win32":
        pytest.skip("process groups are POSIX-only")
    monkeypatch.delattr(os, "waitid", raising=False)
    pid_file = tmp_path / "child.pid"
    child_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"
    script = (
        "import subprocess, sys, time, pathlib\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "time.sleep(0.5)\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(600)\n"
    )
    leader = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    deadline = time.monotonic() + 15
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    engine = te.IsolatedEngine(tmp_path / "T", keep_workdir=True)
    engine._proc = leader

    engine.close()

    assert leader.returncode is not None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail("SIGTERM-ignoring grandchild survived engine close")


# --- Issue #2439: per-provider permission mode, camelCase OpenCode usage ------------


def _app_settings(ws: Path) -> dict[str, Any]:
    raw = (ws / ".runtime" / "app_settings.json").read_text(encoding="utf-8")
    settings: dict[str, Any] = json.loads(raw)
    return settings


def test_prepare_workspace_defaults_mode_per_provider(tmp_path: Path) -> None:
    opencode_ws = te.prepare_workspace(tmp_path / "oc", "opencode", "some-model", _insurance_task())
    claude_ws = te.prepare_workspace(tmp_path / "cl", "claude", "haiku", _insurance_task())

    assert _app_settings(opencode_ws)["provider_default_modes"] == {"opencode": "bypass"}
    assert _app_settings(claude_ws)["provider_default_modes"] == {"claude": "auto"}


def test_prepare_workspace_explicit_mode_overrides_default(tmp_path: Path) -> None:
    ws = te.prepare_workspace(
        tmp_path / "T", "opencode", "some-model", _insurance_task(), mode="auto"
    )

    assert _app_settings(ws)["provider_default_modes"] == {"opencode": "auto"}


def test_prepare_workspace_rejects_unknown_mode_before_building(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="plan"):
        te.prepare_workspace(tmp_path / "T", "claude", "haiku", _insurance_task(), mode="plan")

    assert not (tmp_path / "T" / "ws").exists()


def test_pin_default_mode_merges_into_existing_settings(tmp_path: Path) -> None:
    settings = tmp_path / "app_settings.json"
    settings.write_text(
        json.dumps({"keyboard_send_mode": "steer", "provider_default_modes": {"claude": "manual"}}),
        encoding="utf-8",
    )

    te._pin_default_mode(settings, "opencode", "bypass")

    merged = json.loads(settings.read_text(encoding="utf-8"))
    assert merged == {
        "keyboard_send_mode": "steer",
        "provider_default_modes": {"claude": "manual", "opencode": "bypass"},
    }


def test_run_task_records_resolved_mode_per_provider(tmp_path: Path, stable_provenance: None) -> None:
    session = FakeSession(tmp_path)

    record = te.run_task(
        _insurance_task(),
        provider="opencode",
        model="some-model",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert record["mode"] == "bypass"
    assert record["status"] == "passed"


def test_run_task_records_mode_on_skipped_record(tmp_path: Path, stable_provenance: None) -> None:
    record = te.run_task(
        _insurance_task(requires=["gws_fake"]),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: FakeSession(tmp_path),
        capabilities=frozenset(),
    )

    assert record["status"] == "skipped"
    assert record["mode"] == "auto"


def test_run_task_unknown_mode_is_error_record_without_booting(
    tmp_path: Path, stable_provenance: None
) -> None:
    calls: list[te.EvalTask] = []

    def factory(task: te.EvalTask) -> FakeSession:
        calls.append(task)
        return FakeSession(tmp_path)

    record = te.run_task(
        _insurance_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=factory,
        mode="plan",
    )

    assert record["status"] == "error"
    assert record["mode"] == "plan"
    assert "plan" in record["error"]
    assert calls == []


def test_summarize_separates_two_modes() -> None:
    rows = [
        {**_row(context_total=100), "mode": "auto"},
        {**_row(context_total=300), "mode": "bypass"},
    ]

    summary = te.summarize(rows)

    assert [item["mode"] for item in summary] == ["auto", "bypass"]
    assert all(item["n"] == 1 for item in summary)
    table = te.render_summary(summary)
    assert "| Mode |" in table.splitlines()[0]
    assert "| bypass |" in table


def test_comparison_shows_each_side_mode() -> None:
    baseline = [{**_row(), "mode": "auto"}]
    candidate = [{**_row(label="candidate"), "mode": "bypass"}]

    table = te.render_comparison(te.compare(baseline, candidate))

    assert "Baseline mode" in table.splitlines()[0]
    assert "| E2-recall-guest-count | auto | bypass |" in table


def test_usage_reads_opencode_camel_case_and_prefers_snake(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    folder = runtime / "transcripts" / "chat-oc"
    folder.mkdir(parents=True)
    turns = [
        {
            "response": "first",
            "usage": {
                "inputTokens": "45564",
                "outputTokens": "120",
                "cacheReadTokens": "9000",
                "cacheWriteTokens": "0",
            },
            "tool_calls": [],
        },
        {
            "response": "second",
            "usage": {"input_tokens": 7, "inputTokens": "999", "outputTokens": "3"},
            "tool_calls": [],
        },
    ]
    (folder / "opencode.json").write_text(json.dumps({"turns": turns}), encoding="utf-8")

    usage = te.collect_usage(runtime)

    assert usage["tokens"] == {"input": 45571, "output": 123, "cache_read": 9000, "cache_write": 0}


def test_cli_eval_tasks_run_passes_mode_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def fake_run_suite(tasks: Any, **kwargs: Any) -> list[dict[str, Any]]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(te, "cli_package_mismatch", lambda: None)
    monkeypatch.setattr(te, "run_suite", fake_run_suite)
    out = str(tmp_path / "r.jsonl")

    assert cli.main(
        ["eval", "tasks", "run", "--provider", "opencode", "--model", "m", "--mode", "auto", "--out", out]
    ) == 0
    assert seen["mode"] == "auto"
    assert seen["session_factory"].keywords["mode"] == "auto"

    assert cli.main(["eval", "tasks", "run", "--provider", "opencode", "--model", "m", "--out", out]) == 0
    assert seen["mode"] is None


def test_cli_eval_tasks_run_rejects_unknown_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["eval", "tasks", "run", "--model", "m", "--mode", "plan"])
    assert "invalid choice" in capsys.readouterr().err


def test_claude_project_cleanup_needs_an_eval_workdir_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A path that merely contains the prefix mid-name is not an eval workdir.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    root = tmp_path / f"notes-{te.EVAL_WORKDIR_PREFIX}x" / "ws" / "personal"
    root.mkdir(parents=True)
    folder = te.agent_paths.claude_projects_dir(root)
    folder.mkdir(parents=True)

    assert te._claude_project_dirs(root) == []
    assert folder.is_dir()


# --- Memory pass: transcript entries and the proposals check -----------------------


def _transcript_body(task_id: str, turns: Any) -> dict[str, Any]:
    body = _valid_body(task_id)
    body["category"] = "memory_pass"
    body["prompt"] = ""
    body["entry"] = {"kind": "transcript", "turns": turns}
    return body


def test_catalog_accepts_transcript_entry(tmp_path: Path) -> None:
    turns = [{"user": "hi", "assistant": "hello"}]
    _write_task(tmp_path, "transcript-ok", _transcript_body("transcript-ok", turns))

    (task,) = te.load_tasks(tmp_path)

    assert task.entry["kind"] == "transcript"
    assert task.entry["turns"][0]["user"] == "hi"


@pytest.mark.parametrize(
    "turns",
    [
        None,
        [],
        [{"user": ""}],
        [{"user": "x", "assistant": 1}],
        ["not an object"],
        [{"user": "x", "assistant": "y"}] * (te.MAX_TRANSCRIPT_TURNS + 1),
    ],
)
def test_catalog_rejects_bad_transcript_turns(tmp_path: Path, turns: Any) -> None:
    _write_task(tmp_path, "bad-entry", _transcript_body("bad-entry", turns))

    with pytest.raises(te.TaskCatalogError, match=r"entry\.turns"):
        te.load_tasks(tmp_path)


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        ({"kind": "gws_calls", "must": ["("]}, "regex"),
        ({"kind": "proposals", "must": ["("]}, "regex"),
        ({"kind": "proposals", "must": ["x"], "include_files": "AGENTS.md"}, "include_files"),
        ({"kind": "proposals", "must": ["x"], "include_files": [""]}, "include_files"),
        ({"kind": "proposals", "max_new": -1}, "max_new"),
        ({"kind": "proposals", "max_new": True}, "max_new"),
        ({"kind": "proposals", "must": ["x"], "path": ""}, "path"),
        ({"kind": "proposals"}, "nothing to check"),
    ],
)
def test_catalog_rejects_bad_regex_and_proposals_specs(
    tmp_path: Path, spec: dict[str, Any], fragment: str
) -> None:
    body = _valid_body("bad-spec")
    body["checks"] = [spec]
    _write_task(tmp_path, "bad-spec", body)

    with pytest.raises(te.TaskCatalogError, match=fragment):
        te.load_tasks(tmp_path)


def test_proposals_check_counts_only_new_bullets(tmp_path: Path) -> None:
    old = "- [memory] Old fact about Sunday  _(from: x)_\n"
    queue = tmp_path / te.PROPOSALS_PATH
    queue.parent.mkdir(parents=True)
    queue.write_text(old + "- [memory] Uses kilometres for distances  _(from: y)_\n", encoding="utf-8", newline="")
    ctx = _ctx(tmp_path, snapshot=_snapshot(saved={te.PROPOSALS_PATH: old.encode("utf-8")}))

    assert te._check_proposals(ctx, {"kind": "proposals", "must": ["kilomet"]}).ok
    assert te._check_proposals(ctx, {"kind": "proposals", "must_not": ["sunday"]}).ok
    refused = te._check_proposals(ctx, {"kind": "proposals", "must_not": ["kilomet"]})
    assert not refused.ok
    assert "kilometres" in refused.detail
    assert not te._check_proposals(ctx, {"kind": "proposals", "max_new": 0}).ok
    assert te._check_proposals(ctx, {"kind": "proposals", "max_new": 1}).ok


def test_proposals_check_reads_added_lines_of_include_files(tmp_path: Path) -> None:
    before = "a\nkm rule old\n"
    (tmp_path / "AGENTS.md").write_text(before + "- Prefers kilometres\n", encoding="utf-8", newline="")
    ctx = _ctx(tmp_path, snapshot=_snapshot(saved={"AGENTS.md": before.encode("utf-8")}))

    spec = {"kind": "proposals", "include_files": ["AGENTS.md"]}
    assert te._check_proposals(ctx, {**spec, "must": ["kilometres"]}).ok
    assert not te._check_proposals(ctx, {**spec, "must": ["km rule"]}).ok


def test_proposals_check_missing_queue_is_empty(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    missing = te._check_proposals(ctx, {"kind": "proposals", "must": ["x"]})
    assert not missing.ok
    assert "nothing new matches" in missing.detail
    assert te._check_proposals(ctx, {"kind": "proposals", "must_not": ["x"]}).ok


def test_snapshot_paths_include_proposal_targets() -> None:
    task = _insurance_task(
        checks=[
            {"kind": "unchanged", "path": "a.md"},
            {"kind": "proposals", "include_files": ["AGENTS.md"], "must": ["x"]},
            {"kind": "proposals", "must_not": ["y"]},
        ]
    )

    assert te._snapshot_paths(task) == ["a.md", te.PROPOSALS_PATH, "AGENTS.md"]


def _write_runtime_transcript(ws: Path, chat_id: str, turns: list[dict[str, Any]]) -> None:
    path = ws / ".runtime" / "transcripts" / chat_id / "claude.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"provider": "claude", "context_key": chat_id, "turns": turns}
    path.write_text(json.dumps(payload), encoding="utf-8", newline="")


def test_memory_pass_usage_reads_archive_frontmatter(tmp_path: Path) -> None:
    from ciao.models import ChatContext
    from ciao.transcripts import TranscriptStore

    usage = {
        "input_tokens": "10",
        "output_tokens": "5",
        "cache_read_input_tokens": "3",
        "cache_creation_input_tokens": "2",
    }
    _write_runtime_transcript(
        tmp_path,
        "pass-1",
        [{"prompt": "p", "response": "Queued 1 fact.", "usage": usage, "is_error": False}],
    )
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "Logs")
    md = store.archive_session(
        ctx=ChatContext.for_web("pass-1"),
        active_model="haiku",
        last_effective_model="haiku",
        session_id="",
        provider="claude",
    )
    assert md is not None

    tokens, response, known = te.memory_pass_usage(tmp_path, "pass-1", str(md.relative_to(tmp_path)))

    assert tokens == {"input": 10, "output": 5, "cache_read": 3, "cache_write": 2}
    assert response == "Queued 1 fact."
    assert known is True


def test_memory_pass_usage_prefers_live_runtime_transcript(tmp_path: Path) -> None:
    _write_runtime_transcript(
        tmp_path,
        "pass-2",
        [
            {"response": "first", "usage": {"input_tokens": 7, "output_tokens": "2"}},
            {"response": "", "usage": {"input_tokens": 1}},
        ],
    )

    tokens, response, known = te.memory_pass_usage(tmp_path, "pass-2", None)

    assert tokens == {"input": 8, "output": 2, "cache_read": 0, "cache_write": 0}
    assert response == "first"
    assert known is True


def test_memory_pass_usage_without_any_record_is_zero(tmp_path: Path) -> None:
    # No live transcript and no archive: the usage is unknown, not zero.
    assert te.memory_pass_usage(tmp_path, "gone", None) == (
        {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "",
        False,
    )


def test_memory_pass_step_tolerates_missing_levels() -> None:
    full = {"steps": {"memory_pass": {"status": "ok", "extra": {"chat_id": "pass-1"}}}}

    assert te._memory_pass_step(None) == {}
    assert te._memory_pass_step({}) == {}
    assert te._memory_pass_step({"steps": []}) == {}
    assert te._memory_pass_step(full) == {"status": "ok", "extra": {"chat_id": "pass-1"}}


def test_synthetic_transcript_round_trips_through_archive(tmp_path: Path) -> None:
    from ciao.models import ChatContext
    from ciao.transcripts import TranscriptStore, parse_archive_turns, transcript_has_human_turn

    turns = [
        {"user": "Use kilometres for distances.", "assistant": "Noted."},
        {"user": "Thanks, that's all.", "assistant": "Good evening."},
    ]
    payload = te.synthetic_transcript(
        "chat-x", turns, provider="claude", model="haiku", mode="auto", title="Task eval"
    )
    path = te.transcript_path(tmp_path, "chat-x", "claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8", newline="")

    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "arch")
    ctx = ChatContext.for_web("chat-x")
    assert store.peek_turn_count(ctx, "claude") == len(turns)
    md = store.archive_session(
        ctx=ctx, active_model="haiku", last_effective_model="haiku", session_id="", provider="claude"
    )
    assert md is not None

    archived = parse_archive_turns(md.read_text(encoding="utf-8"))
    assert [t["user"].strip() for t in archived] == [t["user"] for t in turns]
    assert [t["assistant"].strip() for t in archived] == [t["assistant"] for t in turns]
    assert transcript_has_human_turn(md)
    # Archiving deletes the runtime file, which is why usage is read before the archive.
    assert not path.exists()


def _scripted_api(calls: list[tuple[str, str]], replies: dict[tuple[str, str], list[Any]]) -> Any:
    """An ``api`` stand-in: each route answers its queued replies in order, the last one sticking."""

    def api(method: str, path: str, body: Any = None) -> Any:
        calls.append((method, path))
        queue = replies[(method, path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return api


def _pass_step(status: str, pass_id: str = "pass-1") -> dict[str, Any]:
    return {"steps": {"memory_pass": {"status": status, "extra": {"chat_id": pass_id}}}}


def test_run_memory_pass_polls_until_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = te.IsolatedEngine(tmp_path / "T", keep_workdir=True)
    calls: list[tuple[str, str]] = []
    replies = {
        ("POST", "/api/chats/src/archive"): [{"ok": True, "postprocess": _pass_step("queued")}],
        ("GET", "/api/chats/src"): [{"postprocess": _pass_step("running")}, {"postprocess": _pass_step("ok")}],
        ("GET", "/api/chats/pass-1"): [{"archived": True, "archive_path": "Logs/p.md"}],
    }
    monkeypatch.setattr(engine, "api", _scripted_api(calls, replies))
    monkeypatch.setattr(te, "MEMORY_PASS_POLL_S", 0)
    seen: dict[str, Any] = {}

    def fake_usage(ws: Path, chat_id: str, archive_path: str | None) -> tuple[dict[str, int], str, bool]:
        seen.update(ws=ws, chat_id=chat_id, archive_path=archive_path)
        return {"input": 7, "output": 0, "cache_read": 0, "cache_write": 0}, "done", True

    monkeypatch.setattr(te, "memory_pass_usage", fake_usage)

    outcome = engine.run_memory_pass("src", 60.0)

    assert outcome == te.MemoryPassOutcome(
        status="ok",
        chat_id="pass-1",
        tokens={"input": 7, "output": 0, "cache_read": 0, "cache_write": 0},
        response="done",
        usage_known=True,
    )
    assert calls == [
        ("POST", "/api/chats/src/archive"),
        ("GET", "/api/chats/src"),
        ("GET", "/api/chats/src"),
        ("GET", "/api/chats/pass-1"),
    ]
    assert seen == {"ws": engine.ws, "chat_id": "pass-1", "archive_path": "Logs/p.md"}


def test_run_memory_pass_timeout_stops_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = te.IsolatedEngine(tmp_path / "T", keep_workdir=True)
    calls: list[tuple[str, str]] = []
    replies = {
        ("POST", "/api/chats/src/archive"): [{"postprocess": _pass_step("queued")}],
        ("GET", "/api/chats/src"): [{"postprocess": _pass_step("running")}],
        ("POST", "/api/chats/pass-1/stop"): [None],
    }
    monkeypatch.setattr(engine, "api", _scripted_api(calls, replies))

    outcome = engine.run_memory_pass("src", 0.0)

    assert outcome.status == "timeout"
    assert outcome.chat_id == "pass-1"
    assert ("POST", "/api/chats/pass-1/stop") in calls


def test_run_memory_pass_not_queued(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = te.IsolatedEngine(tmp_path / "T", keep_workdir=True)
    calls: list[tuple[str, str]] = []
    replies = {("POST", "/api/chats/src/archive"): [{"ok": True, "postprocess": None}]}
    monkeypatch.setattr(engine, "api", _scripted_api(calls, replies))

    outcome = engine.run_memory_pass("src", 60.0)

    assert outcome.status == "not_queued"
    assert outcome.chat_id is None
    assert outcome.response == ""
    assert calls == [("POST", "/api/chats/src/archive")]


def test_start_transcript_writes_the_normalized_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = te.IsolatedEngine(tmp_path / "T", keep_workdir=True)
    engine._host_project_id = "proj-1"
    calls: list[tuple[str, str]] = []
    replies = {("POST", "/api/projects/proj-1/chats"): [{"chat_id": "chat-9"}]}
    monkeypatch.setattr(engine, "api", _scripted_api(calls, replies))

    chat_id = engine.start_transcript([{"user": "Hi", "assistant": "Hello"}], provider="claude", model="haiku")

    assert chat_id == "chat-9"
    assert calls == [("POST", "/api/projects/proj-1/chats")]
    payload = json.loads(te.transcript_path(engine.ws, "chat-9", "claude").read_text(encoding="utf-8"))
    assert [turn["prompt"] for turn in payload["turns"]] == ["Hi"]
    assert payload["turns"][0]["response"] == "Hello"
    assert payload["turns"][0]["mode"] == "auto"


class FakeTranscriptSession(FakeSession):
    """A transcript-entry session: the pass outcome is canned, and the queue write is optional."""

    def __init__(
        self,
        root: Path,
        *,
        outcome: te.MemoryPassOutcome,
        pass_bullet: str | None = None,
        pre_tokens: dict[str, int] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(root, **kwargs)
        self.outcome = outcome
        self.pass_bullet = pass_bullet
        self.pre_tokens = dict(pre_tokens or {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
        self.started: list[dict[str, str]] = []
        self.pass_calls: list[tuple[str, float]] = []
        self.ran_pass = False

    def start_transcript(self, turns: list[dict[str, str]], *, provider: str, model: str) -> str:
        self.started = list(turns)
        return "chat-1"

    def run_memory_pass(self, chat_id: str, timeout_s: float) -> te.MemoryPassOutcome:
        self.pass_calls.append((chat_id, timeout_s))
        self.ran_pass = True
        if self.pass_bullet is not None:
            queue = self.agent_root / te.PROPOSALS_PATH
            queue.parent.mkdir(parents=True, exist_ok=True)
            queue.write_text(f"- [memory] {self.pass_bullet}  _(from: {chat_id})_\n", encoding="utf-8", newline="")
        return self.outcome

    def usage(self) -> dict[str, Any]:
        # Before the pass the synthetic chat holds the pre-pass tokens. The archive deletes
        # that transcript, so afterwards only a live pass's own transcript remains.
        if not self.ran_pass:
            tokens = dict(self.pre_tokens)
        elif self.outcome.status != "ok":
            tokens = dict(self.outcome.tokens)
        else:
            tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        # Provider errors belong to the pass, so they show only once it has run.
        return {
            "tokens": tokens,
            "tool_calls": 0,
            "tool_errors": 0,
            "ciao_calls": 0,
            "ciao_errors": 0,
            "cost_usd": None,
            "chat_ids": [],
            "provider_errors": self.provider_errors if self.ran_pass else 0,
            "provider_error": self.provider_error if self.ran_pass else None,
            "trace": [],
        }


def _transcript_task(**overrides: Any) -> te.EvalTask:
    fields: dict[str, Any] = {
        "id": "X-memory",
        "tier": "medium",
        "category": "memory_pass",
        "prompt": "",
        "entry": {"kind": "transcript", "turns": [{"user": "Use kilometres.", "assistant": "Noted."}]},
        "setup": [],
        "requires": [],
        "timeout_s": 60.0,
        "checks": [{"kind": "proposals", "must": ["kilomet"]}],
        "overlay": None,
    }
    fields.update(overrides)
    return te.EvalTask(**fields)


_ZERO_TOKENS = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
_PASS_TOKENS = {"input": 40, "output": 8, "cache_read": 0, "cache_write": 2}


def test_run_task_transcript_entry_records_memory_pass_tokens(
    tmp_path: Path, stable_provenance: None
) -> None:
    outcome = te.MemoryPassOutcome("ok", "pass-1", dict(_PASS_TOKENS), "Queued 1 fact.", usage_known=True)
    session = FakeTranscriptSession(tmp_path, outcome=outcome, pass_bullet="Uses kilometres for distances")

    record = te.run_task(
        _transcript_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert session.started == [{"user": "Use kilometres.", "assistant": "Noted."}]
    assert session.pass_calls == [("chat-1", 60.0)]
    assert record["status"] == "passed", record
    assert record["memory_pass_tokens"] == {**_PASS_TOKENS, "context_total": 42}
    assert record["tokens"] == record["memory_pass_tokens"]
    assert record["memory_pass_status"] == "ok"
    assert record["memory_pass_chat_id"] == "pass-1"
    assert record["answer"] == "Queued 1 fact."
    assert record["timed_out"] is False


@pytest.mark.parametrize(("status", "timed_out"), [("attention", False), ("timeout", True)])
def test_run_task_transcript_attention_and_timeout_fail(
    tmp_path: Path, stable_provenance: None, status: str, timed_out: bool
) -> None:
    outcome = te.MemoryPassOutcome(status, "pass-1", dict(_ZERO_TOKENS), "", usage_known=True)
    session = FakeTranscriptSession(tmp_path, outcome=outcome, pass_bullet="Uses kilometres for distances")

    record = te.run_task(
        _transcript_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert record["status"] == "failed"
    assert record["timed_out"] is timed_out
    assert record["memory_pass_status"] == status


def test_run_task_transcript_not_queued_is_error(tmp_path: Path, stable_provenance: None) -> None:
    outcome = te.MemoryPassOutcome("not_queued", None, dict(_ZERO_TOKENS), "", usage_known=False)
    session = FakeTranscriptSession(tmp_path, outcome=outcome)

    record = te.run_task(
        _transcript_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert record["status"] == "error"
    assert "not queued" in record["error"]


def test_run_task_transcript_provider_error_in_pass_follows_error_rule(
    tmp_path: Path, stable_provenance: None
) -> None:
    outcome = te.MemoryPassOutcome("attention", "pass-1", dict(_ZERO_TOKENS), "", usage_known=True)
    session = FakeTranscriptSession(
        tmp_path,
        outcome=outcome,
        provider_errors=1,
        provider_error="weekly usage limit reached",
    )

    record = te.run_task(
        _transcript_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert record["status"] == "error"
    assert record["error"].startswith("provider error:")
    assert "weekly usage limit reached" in record["error"]
    assert record["provider_errors"] == 1


def test_prompt_record_has_null_memory_pass_fields(tmp_path: Path, stable_provenance: None) -> None:
    session = FakeSession(tmp_path)

    record = te.run_task(
        _insurance_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert record["memory_pass_status"] is None
    assert record["memory_pass_chat_id"] is None
    assert record["memory_pass_tokens"] is None


# --- Memory pass: the M5 and M6 catalog tasks --------------------------------------

M5_ID = "M5-memory-pass-keep-drop"
M6_ID = "M6-memory-pass-decided-paraphrase"


def test_memory_pass_tasks_plant_facts() -> None:
    from ciao import memory_proposals

    by_id = {task.id: task for task in te.load_tasks()}
    m5 = by_id[M5_ID]
    m6 = by_id[M6_ID]
    for task in (m5, m6):
        assert task.entry["kind"] == "transcript", task.id
        assert task.category == "memory_pass", task.id

    m5_users = " ".join(turn["user"] for turn in m5.entry["turns"])
    for needle in ("kilometres", "permit application", "max_span_cm"):
        assert needle in m5_users
    settings = te.TASKS_ROOT / M5_ID / "overlay" / "code" / "shed-planner" / "settings.toml"
    assert "max_span_cm = 240" in settings.read_text(encoding="utf-8")

    vault = te.TASKS_ROOT / M6_ID / "overlay" / "memory-vault"
    dismissed = "Send the weekly round-up on Sunday evenings."
    assert memory_proposals.was_dismissed(vault, dismissed)
    assert memory_proposals.list_proposals(vault / "Workspace" / "Memory-Proposals.md") == []
    sunday_turn = next(turn["user"] for turn in m6.entry["turns"] if "Sunday" in turn["user"])
    # Code-level dedupe compares dedupe_key only, so a paraphrase must not share it.
    assert memory_proposals.dedupe_key(sunday_turn) != memory_proposals.dedupe_key(dismissed)


# --- per-run tool trace and model calls -------------------------------------------


def _claude_session_lines(home: Path, agent_root: Path, lines: list[dict[str, Any]]) -> Path:
    folder = _claude_session_folder(home, agent_root)
    log = folder / "x.jsonl"
    log.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8", newline="")
    return log


def test_session_trace_reads_claude_session_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    engine = _isolated_engine_at(tmp_path / "ciao-task-eval-run", keep_workdir=True)
    _claude_session_lines(
        home,
        engine.agent_root,
        [
            {"type": "user", "message": {"role": "user", "content": "clean up the vault"}},
            {"type": "assistant", "message": {"id": "msg_1", "content": [
                {"type": "tool_use", "id": "toolu_a", "name": "Bash",
                 "input": {"command": "ls memory-vault", "description": "list"}},
            ]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_a", "content": "People"},
            ]}},
            {"type": "assistant", "message": {"id": "msg_1", "content": [{"type": "text", "text": "Listing."}]}},
            {"type": "assistant", "message": {"id": "msg_2", "content": [
                {"type": "tool_use", "id": "toolu_b", "name": "Read",
                 "input": {"file_path": "memory-vault/People/x.md"}},
            ]}},
        ],
    )

    calls, model_calls = engine.session_trace()

    assert model_calls == 2
    assert calls == [
        {"id": "toolu_a", "name": "Bash", "input": "ls memory-vault"},
        {"id": "toolu_b", "name": "Read", "input": "memory-vault/People/x.md"},
    ]


def test_session_trace_input_falls_back_to_json_and_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    engine = _isolated_engine_at(tmp_path / "ciao-task-eval-run", keep_workdir=True)
    _claude_session_lines(
        home,
        engine.agent_root,
        [
            {"type": "assistant", "message": {"id": "msg_1", "content": [
                {"type": "tool_use", "id": "toolu_g", "name": "Grep", "input": {"pattern": "*.md"}},
                {"type": "tool_use", "id": "toolu_w", "name": "WebFetch",
                 "input": {"url": "https://example.test/" + "p" * 300}},
            ]}},
        ],
    )

    calls, model_calls = engine.session_trace()

    assert model_calls == 1
    assert calls[0] == {"id": "toolu_g", "name": "Grep", "input": "*.md"}
    assert calls[1]["id"] == "toolu_w"
    assert calls[1]["input"].startswith('{"url": "https://example.test/')
    assert len(calls[1]["input"]) == 200


def test_session_trace_without_claude_log_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    engine = _isolated_engine_at(tmp_path / "ciao-task-eval-run", keep_workdir=True)

    assert engine.session_trace() == ([], None)


def test_run_task_merges_session_trace_over_transcript_summary(
    tmp_path: Path, stable_provenance: None
) -> None:
    session = FakeSession(
        tmp_path,
        trace=[
            {"id": "toolu_a", "chat_id": "chat-1", "name": "Bash", "input": "Bash · list the vault", "error": False},
            {"id": "toolu_b", "chat_id": "chat-1", "name": "Read", "input": "read a note", "error": True},
        ],
        session_calls=[{"id": "toolu_a", "name": "Bash", "input": "ls memory-vault/People"}],
        model_calls=2,
    )

    record = te.run_task(
        _insurance_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert record["trace"] == [
        {"chat_id": "chat-1", "name": "Bash", "input": "ls memory-vault/People", "error": False},
        {"chat_id": "chat-1", "name": "Read", "input": "read a note", "error": True},
    ]
    assert record["model_calls"] == 2
    assert record["trace_truncated"] is False


def test_run_task_without_claude_log_records_null_model_calls(
    tmp_path: Path, stable_provenance: None
) -> None:
    session = FakeSession(
        tmp_path,
        trace=[{"id": "", "chat_id": "chat-1", "name": "shell", "input": "ls", "error": False}],
    )

    record = te.run_task(
        _insurance_task(),
        provider="opencode",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert record["trace"] == [{"chat_id": "chat-1", "name": "shell", "input": "ls", "error": False}]
    assert record["model_calls"] is None


def test_trace_is_capped_at_200_entries(tmp_path: Path, stable_provenance: None) -> None:
    trace = [
        {"id": "", "chat_id": "chat-1", "name": "Bash", "input": f"cmd {n}", "error": False}
        for n in range(250)
    ]
    session = FakeSession(tmp_path, trace=trace)

    record = te.run_task(
        _insurance_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert len(record["trace"]) == 200
    assert record["trace"][0]["input"] == "cmd 0"
    assert record["trace"][-1]["input"] == "cmd 199"
    assert record["trace_truncated"] is True


def test_summarize_reports_median_model_calls() -> None:
    rows = [
        {**_row(), "model_calls": 4},
        {**_row(status="failed"), "model_calls": 10},
        {**_row(), "model_calls": None},
        {**_row(status="error"), "model_calls": 99},
    ]

    group = te.summarize(rows)[0]

    assert group["median_model_calls"] == 7
    assert "Median model calls" in te.render_summary([group])
def test_run_task_attention_pass_tokens_are_not_doubled(tmp_path: Path, stable_provenance: None) -> None:
    pre = {"input": 30, "output": 4, "cache_read": 5, "cache_write": 1}
    outcome = te.MemoryPassOutcome("attention", "pass-1", dict(_PASS_TOKENS), "", usage_known=True)
    session = FakeTranscriptSession(
        tmp_path,
        outcome=outcome,
        pass_bullet="Uses kilometres for distances",
        pre_tokens=pre,
    )

    record = te.run_task(
        _transcript_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    # The post-run count holds the pass too; the record must add it once, not twice.
    assert record["tokens"] == {"input": 70, "output": 12, "cache_read": 5, "cache_write": 3, "context_total": 78}
    assert record["memory_pass_tokens"] == {**_PASS_TOKENS, "context_total": 42}
    assert record["status"] == "failed"


def test_run_task_unknown_pass_usage_is_error(tmp_path: Path, stable_provenance: None) -> None:
    outcome = te.MemoryPassOutcome("ok", "pass-1", dict(_ZERO_TOKENS), "", usage_known=False)
    session = FakeTranscriptSession(tmp_path, outcome=outcome, pass_bullet="Uses kilometres for distances")

    record = te.run_task(
        _transcript_task(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
    )

    assert record["status"] == "error"
    assert record["error"] == "memory pass usage unavailable (archive not found)"
    assert record["memory_pass_status"] == "ok"
    assert record["memory_pass_tokens"] is None


def test_run_memory_pass_ok_without_archive_has_unknown_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = te.IsolatedEngine(tmp_path / "T", keep_workdir=True)
    calls: list[tuple[str, str]] = []
    replies = {
        ("POST", "/api/chats/src/archive"): [{"postprocess": _pass_step("queued")}],
        ("GET", "/api/chats/src"): [{"postprocess": _pass_step("ok")}],
        ("GET", "/api/chats/pass-1"): [{"archived": False, "archive_path": ""}],
    }
    monkeypatch.setattr(engine, "api", _scripted_api(calls, replies))
    monkeypatch.setattr(te, "MEMORY_PASS_POLL_S", 0)
    monkeypatch.setattr(te, "MEMORY_PASS_ARCHIVE_WAIT_S", 0)

    outcome = engine.run_memory_pass("src", 60.0)

    assert outcome.status == "ok"
    assert outcome.usage_known is False
    assert outcome.tokens == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def test_run_memory_pass_timeout_rereads_transcript_after_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = te.IsolatedEngine(tmp_path / "T", keep_workdir=True)
    calls: list[tuple[str, str]] = []
    replies = {
        ("POST", "/api/chats/src/archive"): [{"postprocess": _pass_step("queued")}],
        ("GET", "/api/chats/src"): [{"postprocess": _pass_step("running")}],
    }
    monkeypatch.setattr(engine, "api", _scripted_api(calls, replies))
    monkeypatch.setattr(te, "MEMORY_PASS_STOP_SETTLE_S", 0)

    def stop_flushes_last_turn(chat_id: str) -> None:
        # The engine writes the pass's final turn only once the stop has landed.
        _write_runtime_transcript(engine.ws, chat_id, [{"response": "partial", "usage": {"input_tokens": 9}}])

    monkeypatch.setattr(engine, "stop_chat", stop_flushes_last_turn)

    outcome = engine.run_memory_pass("src", 0.0)

    assert outcome.status == "timeout"
    assert outcome.usage_known is True
    assert outcome.tokens["input"] == 9
    assert outcome.response == "partial"


def test_proposals_check_reads_directories_in_include_files(tmp_path: Path) -> None:
    people = tmp_path / "memory-vault" / "People"
    people.mkdir(parents=True)
    old = "# Lena\n\nLives in Bologna.\n"
    (people / "lena.md").write_text(old + "Prefers kilometres.\n", encoding="utf-8", newline="")
    (people / "owner.md").write_text("# Owner\n\nAllergic to hazelnuts.\n", encoding="utf-8", newline="")
    saved = {"memory-vault/People/lena.md": old.encode("utf-8")}
    ctx = _ctx(tmp_path, snapshot=_snapshot(saved=saved))
    spec = {"kind": "proposals", "include_files": ["memory-vault/People/"]}

    assert te._check_proposals(ctx, {**spec, "must": ["kilometres"]}).ok  # added to an existing file
    assert te._check_proposals(ctx, {**spec, "must": ["hazelnut"]}).ok  # a new file is all new lines
    assert not te._check_proposals(ctx, {**spec, "must": ["Bologna"]}).ok  # already there at the snapshot
    assert not te._check_proposals(ctx, {**spec, "must_not": ["hazelnut"]}).ok


def test_take_snapshot_saves_every_file_under_a_directory(tmp_path: Path) -> None:
    people = tmp_path / "memory-vault" / "People"
    (people / "sub").mkdir(parents=True)
    (people / "lena.md").write_text("Lena\n", encoding="utf-8", newline="")
    (people / "sub" / "x.md").write_text("X\n", encoding="utf-8", newline="")

    def api(method: str, path: str, body: Any = None) -> Any:
        return {"tasks": []} if path.startswith("/api/tasks") else []

    snapshot = te.take_snapshot(tmp_path, api, ["memory-vault/People/"])

    assert snapshot.saved == {
        "memory-vault/People/lena.md": b"Lena\n",
        "memory-vault/People/sub/x.md": b"X\n",
    }


MEMORY_PASS_INCLUDES = ["AGENTS.md", "memory-vault/People/", "memory-vault/Documents/"]


def test_memory_pass_checks_cover_people_documents_and_the_decision_sidecar() -> None:
    by_id = {task.id: task for task in te.load_tasks()}
    for task_id in (M5_ID, M6_ID):
        for spec in by_id[task_id].checks:
            if spec["kind"] == "proposals":
                assert spec["include_files"] == MEMORY_PASS_INCLUDES, task_id

    m6_checks = by_id[M6_ID].checks
    sidecar = "memory-vault/Workspace/Memory-Proposals.dismissed.jsonl"
    assert {"kind": "unchanged", "path": sidecar} not in m6_checks
    assert {
        "kind": "file_contains",
        "path": sidecar,
        "all": ["Send the weekly round-up on Sunday evenings."],
    } in m6_checks


# --- Hard tier A: any_of, task/schedule check fields, H7 task breakdown, H8 cadence, H10 namesake -----

H7_ID = "H7-shed-task-breakdown"
H8_ID = "H8-biweekly-cadence"
H10_ID = "H10-namesake-recall"
HARD_A_IDS = (H7_ID, H8_ID, H10_ID)
_ANSWER_OK = {"kind": "answer_contains", "any": ["ok"]}
_ANSWER_NOPE = {"kind": "answer_contains", "any": ["nope"]}


def _seed(tmp_path: Path, task_id: str) -> Path:
    """A throwaway agent root: the base fixture vault with the task's overlay on top. Returns the agent root."""
    root = tmp_path / "personal"
    shutil.copytree(te.TASKS_ROOT / te.FIXTURE_DIRNAME, root / "memory-vault")
    shutil.copytree(_overlay(task_id), root, dirs_exist_ok=True)
    return root


def test_any_of_passes_when_one_option_passes(tmp_path: Path) -> None:
    spec = {
        "kind": "any_of",
        "options": [[{"kind": "answer_contains", "any": ["x"]}], [{"kind": "answer_contains", "any": ["ok"]}]],
    }

    passed = te.CHECKS["any_of"](_ctx(tmp_path, answer="ok"), spec)
    assert passed.ok
    assert passed.detail == "option 2 passed"

    failed = te.CHECKS["any_of"](_ctx(tmp_path, answer="nope"), spec)
    assert not failed.ok
    assert "option 1" in failed.detail
    assert "option 2" in failed.detail


def test_any_of_nested_check_that_raises_is_a_failed_option(tmp_path: Path) -> None:
    def raising_api(method: str, path: str, body: Any = None) -> Any:
        raise RuntimeError("engine down")

    raising = {"kind": "new_tasks", "count": 0}
    ctx = _ctx(tmp_path, answer="ok", api=raising_api)

    survives = te.CHECKS["any_of"](ctx, {"kind": "any_of", "options": [[raising], [_ANSWER_OK]]})
    assert survives.ok, survives.detail

    result = te.CHECKS["any_of"](ctx, {"kind": "any_of", "options": [[raising], [_ANSWER_NOPE]]})
    assert not result.ok
    assert "check raised" in result.detail


@pytest.mark.parametrize(
    ("options", "label"),
    [
        ([[_ANSWER_OK]], "single option"),
        ([[_ANSWER_OK], []], "empty option"),
        ([[{"kind": "any_of", "options": [[_ANSWER_OK], [_ANSWER_NOPE]]}], [_ANSWER_OK]], "nested any_of"),
        ([[{"kind": "unchanged", "path": "AGENTS.md"}], [_ANSWER_OK]], "nested unchanged"),
        ([[{"kind": "not_a_kind"}], [_ANSWER_OK]], "nested unknown kind"),
    ],
    ids=lambda value: value if isinstance(value, str) else "options",
)
def test_catalog_rejects_bad_any_of(tmp_path: Path, options: list[Any], label: str) -> None:
    body = _valid_body("X-any-of")
    body["checks"] = [{"kind": "any_of", "options": options}]
    _write_task(tmp_path, "X-any-of", body)

    with pytest.raises(te.TaskCatalogError):
        te.load_tasks(tmp_path)


@pytest.mark.parametrize(
    "fields",
    [
        {"project_name_contains": "garden"},
        {"project_name_contains": [1]},
        {"titles_cover": ["stain"]},
        {"titles_cover": [[]]},
        {"titles_cover": [[1]]},
    ],
    ids=["name-not-list", "name-not-strings", "cover-not-lists", "cover-empty-group", "cover-not-strings"],
)
def test_catalog_rejects_bad_new_tasks_fields(tmp_path: Path, fields: dict[str, Any]) -> None:
    body = _valid_body("X-new-tasks")
    body["checks"] = [{"kind": "new_tasks", "count": 1, **fields}]
    _write_task(tmp_path, "X-new-tasks", body)

    with pytest.raises(te.TaskCatalogError):
        te.load_tasks(tmp_path)


def test_new_tasks_project_name_contains_resolves_ids(tmp_path: Path) -> None:
    projects = [
        {"project_id": "p-shed", "name": "Garden shed"},
        {"project_id": "p-host", "name": "Task eval host"},
    ]

    def api_for(project_id: str | None) -> Any:
        def api(method: str, path: str, body: Any = None) -> Any:
            if path == "/api/projects":
                return projects
            assert path == "/api/tasks?workspace=personal"
            row = {"id": "t-new", "title": "Buy wood stain", "project_id": project_id, "due": None}
            return {"tasks": [row]}

        return api

    spec = {"kind": "new_tasks", "count": 1, "project_name_contains": ["garden"]}
    snap = _snapshot()
    assert te.CHECKS["new_tasks"](_ctx(tmp_path, api=api_for("p-shed"), snapshot=snap), spec).ok
    assert not te.CHECKS["new_tasks"](_ctx(tmp_path, api=api_for("p-host"), snapshot=snap), spec).ok
    assert not te.CHECKS["new_tasks"](_ctx(tmp_path, api=api_for(None), snapshot=snap), spec).ok
    # An id that is not in /api/projects falls back to the raw string, which here names the garden.
    assert te.CHECKS["new_tasks"](_ctx(tmp_path, api=api_for("Garden shed"), snapshot=snap), spec).ok


def test_new_tasks_titles_cover(tmp_path: Path) -> None:
    groups = [["concrete", "pour"], ["stain"]]

    def ctx_for(titles: list[str]) -> te.GradeContext:
        rows = [{"id": f"t{i}", "title": title} for i, title in enumerate(titles)]

        def api(method: str, path: str, body: Any = None) -> Any:
            return {"tasks": rows}

        return _ctx(tmp_path, api=api)

    def spec_for(count: int) -> dict[str, Any]:
        return {"kind": "new_tasks", "count": count, "titles_cover": groups}

    assert te.CHECKS["new_tasks"](ctx_for(["Book the concrete pour", "Buy wood stain"]), spec_for(2)).ok
    assert not te.CHECKS["new_tasks"](ctx_for(["Buy wood stain", "Stain the frame"]), spec_for(2)).ok
    assert not te.CHECKS["new_tasks"](
        ctx_for(["Book the concrete pour", "Buy wood stain", "Rain barrel"]), spec_for(3)
    ).ok


def test_new_schedules_prompt_any(tmp_path: Path) -> None:
    def api_with(prompt: str) -> Any:
        def api(method: str, path: str, body: Any = None) -> Any:
            assert path == "/api/schedules"
            return [{"schedule_id": "s-new", "scope": "user", "frequency": "weekly", "prompt": prompt}]

        return api

    spec = {"kind": "new_schedules", "count": 1, "prompt_any": ["parity", "iso week"]}
    bare = "Put the recycling out, every other Monday"
    guarded = "Put the recycling out, and skip odd ISO week numbers"

    assert not te.CHECKS["new_schedules"](_ctx(tmp_path, api=api_with(bare)), spec).ok
    assert te.CHECKS["new_schedules"](_ctx(tmp_path, api=api_with(guarded)), spec).ok


def test_hard_tier_a_tasks_load() -> None:
    tasks = {task.id: task for task in te.load_tasks()}
    commands = json.loads(
        (Path(str(resources.files("ciao.stock"))) / "skills" / "ciao-cli" / "commands.json").read_text(
            encoding="utf-8"
        )
    )
    operations = {op for op in commands.values() if "_" in op}

    for task_id in HARD_A_IDS:
        assert tasks[task_id].tier == "hard", task_id
        assert tasks[task_id].category in {"task_breakdown", "cadence_limits", "namesake_recall"}, task_id
        assert tasks[task_id].category not in operations, task_id


def test_h7_overlay_has_three_open_items_not_on_board(tmp_path: Path) -> None:
    rel = "memory-vault/projects/active/garden-shed/garden-shed.md"
    text = (_overlay(H7_ID) / rel).read_text(encoding="utf-8")
    lines = text.splitlines()

    assert sum(line.strip().startswith("- [ ]") for line in lines) == 4
    assert sum(line.strip().startswith("- [x]") for line in lines) == 2
    assert [line for line in lines if line.strip().startswith("- [ ]") and "Order the roof felt" in line]

    ctx = _ctx(_seed(tmp_path, H7_ID))
    assert te.CHECKS["frontmatter_valid"](ctx, {"kind": "frontmatter_valid", "path": rel}).ok
    assert te.CHECKS["links_resolve"](ctx, {"kind": "links_resolve", "path": rel}).ok


def test_h7_checks_pass_on_correct_state_and_fail_on_naive(tmp_path: Path) -> None:
    projects = [{"project_id": "p-shed", "name": "Garden shed"}, {"project_id": "p-host", "name": "Task eval host"}]
    snap = _snapshot(task_ids=("t-felt",), project_ids=("p-shed", "p-host"))

    def rows(
        *,
        stain: str = "Buy wood stain",
        project: str | None = "p-shed",
        due: str | None = "2026-11-06",
        extra: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        base = [
            {"id": "t-felt", "title": "Order the roof felt", "project_id": "p-shed", "due": None},
            {"id": "t-pour", "title": "Book the concrete pour", "project_id": project, "due": due},
            {"id": "t-stain", "title": stain, "project_id": project, "due": due},
            {"id": "t-hedge", "title": "Ask the neighbours about the boundary hedge", "project_id": project, "due": due},
        ]
        return base + (extra or [])

    def grade_new(task_rows: list[dict[str, Any]]) -> list[te.CheckResult]:
        def api(method: str, path: str, body: Any = None) -> Any:
            if path.startswith("/api/tasks"):
                return {"tasks": task_rows}
            if path == "/api/projects":
                return projects
            assert path == "/api/schedules"
            return []

        ctx = _ctx(tmp_path, api=api, snapshot=snap, run_date=date(2026, 11, 1))
        return [
            te.CHECKS[str(spec["kind"])](ctx, spec)
            for spec in _task(H7_ID).checks
            if spec["kind"] in ("new_tasks", "new_projects", "new_schedules")
        ]

    assert all(result.ok for result in grade_new(rows())), [r.detail for r in grade_new(rows())]

    duplicate = {"id": "t-dup", "title": "Order the roof felt", "project_id": "p-shed", "due": "2026-11-06"}
    naive = {
        "a duplicate of the board task": rows(extra=[duplicate]),
        "no project binding": rows(project=None),
        "bound to the host project": rows(project="p-host"),
        "stain replaced by an unrelated item": rows(stain="Measure the plot"),
        "due on a Saturday": rows(due="2026-11-07"),
    }
    for label, task_rows in naive.items():
        new_tasks = [r for r in grade_new(task_rows) if r.kind == "new_tasks"]
        assert new_tasks and not new_tasks[0].ok, label


def test_h8_accepts_decline_or_parity_guard_and_rejects_weekly(tmp_path: Path) -> None:
    check = _task(H8_ID).checks[0]

    def weekly(prompt: str, time: str = "08:00") -> dict[str, Any]:
        return {
            "schedule_id": "s-new",
            "scope": "user",
            "frequency": "weekly",
            "days_of_week": ["mon"],
            "daily_time_utc": time,
            "prompt": prompt,
        }

    def grade_h8(answer: str, schedules: list[dict[str, Any]]) -> te.CheckResult:
        def api(method: str, path: str, body: Any = None) -> Any:
            assert path == "/api/schedules"
            return schedules

        return te.CHECKS["any_of"](_ctx(tmp_path, answer=answer, api=api), check)

    decline = "Ciaobot schedules can't repeat every other week, so I didn't create one."
    parity = "Remind me to put the paper recycling out. Only on even ISO week numbers; skip odd weeks."

    assert grade_h8(decline, []).ok
    assert grade_h8("Done!", [weekly(parity)]).ok
    assert not grade_h8("Done!", [weekly("Remind me to put the paper recycling out (every other Monday)")]).ok
    assert not grade_h8("Done!", [{"frequency": "interval", "interval_minutes": 20160, "prompt": "paper recycling"}]).ok
    assert not grade_h8("Done!", []).ok
    assert not grade_h8("Done!", [weekly(parity, time="09:00")]).ok


def test_h10_accepts_decline_or_disambiguation_and_rejects_namesake(tmp_path: Path) -> None:
    check = _task(H10_ID).checks[0]

    def grade_h10(answer: str) -> te.CheckResult:
        return te.CHECKS["any_of"](_ctx(tmp_path, answer=answer), check)

    assert grade_h10("Tomas Berg's note doesn't mention his birthday, so I don't know it.").ok
    assert grade_h10(
        "I don't have Tomas Berg's birthday. Your climbing note lists a Tomas on 3 May, "
        "but that's Ana's cousin, not your colleague."
    ).ok
    assert not grade_h10("Tomas's birthday is on 3 May.").ok
    assert not grade_h10("I couldn't find it in his People note, but the climbing group lists Tomas: 3 May.").ok
    assert not grade_h10("").ok


def test_h10_overlay_plants_the_namesake() -> None:
    note = (_overlay(H10_ID) / "memory-vault" / "Documents" / "climbing-group.md").read_text(encoding="utf-8")
    assert "Tomas: 3 May" in note
    assert "cousin" in note

    base = (te.TASKS_ROOT / te.FIXTURE_DIRNAME / "People" / "tomas-berg.md").read_text(encoding="utf-8").lower()
    assert "birthday" not in base
    assert "may" not in base


# --- Fix round for #2463: recall and cadence fairness, mixed end states, normalize ------------

# Every alternative of ABSTAIN_RE, straight and curly apostrophes. The sync assertion in the test
# keeps this list honest against the regex.
ABSTAIN_PHRASES: tuple[str, ...] = (
    "don't know",
    "do not know",
    "not recorded",
    "not mentioned",
    "not in your vault",
    "not in the vault",
    "not listed",
    "no record",
    "no information",
    "no mention",
    "couldn't find",
    "could not find",
    "can't find",
    "cannot find",
    "doesn't say",
    "doesn't mention",
    "doesn't record",
    "doesn't include",
    "does not say",
    "does not mention",
    "does not record",
    "does not include",
    "isn't recorded",
    "isn't mentioned",
)
ABSTAIN_FORMS: list[str] = [
    form for phrase in ABSTAIN_PHRASES for form in dict.fromkeys((phrase, phrase.replace("'", "’")))
]


def _grade_h10(tmp_path: Path, answer: str) -> te.CheckResult:
    return te.CHECKS["any_of"](_ctx(tmp_path, answer=answer), _task(H10_ID).checks[0])


@pytest.mark.parametrize("phrase", ABSTAIN_FORMS)
def test_h10_accepts_every_abstain_phrase(tmp_path: Path, phrase: str) -> None:
    assert te.ABSTAIN_RE.search(phrase), f"curated list is out of step with ABSTAIN_RE: {phrase!r}"
    assert _grade_h10(tmp_path, f"I checked the vault: {phrase} for Tomas Berg.").ok


def test_h10_accepts_hedged_answer_that_flags_the_namesake(tmp_path: Path) -> None:
    answer = (
        "I don't have Tomas Berg's birthday. The only Tomas with a date is in the climbing group (3 May); "
        "tell me if that's who you mean."
    )
    assert _grade_h10(tmp_path, answer).ok


@pytest.mark.parametrize("flag", ["who you mean", "if you mean", "same tomas", "same person", "namesake"])
def test_h10_accepts_each_namesake_flag(tmp_path: Path, flag: str) -> None:
    assert _grade_h10(tmp_path, f"I don't have his birthday; is this {flag}?").ok


def test_h10_decline_with_unflagged_may_3_fails(tmp_path: Path) -> None:
    assert not _grade_h10(tmp_path, "I don't have Tomas Berg's birthday. A Tomas in your climbing group has 3 May.").ok


def test_h8_decline_plus_plain_weekly_schedule_fails(tmp_path: Path) -> None:
    schedule = {
        "schedule_id": "s-new",
        "scope": "user",
        "frequency": "weekly",
        "days_of_week": ["mon"],
        "daily_time_utc": "08:00",
        "prompt": "Remind me to put the paper recycling out.",
    }
    answer = "Ciaobot schedules can't repeat every other week, so I created a plain weekly one."
    assert not _grade_h8(tmp_path, answer, [schedule]).ok


def _grade_h8(tmp_path: Path, answer: str, schedules: list[dict[str, Any]]) -> te.CheckResult:
    def api(method: str, path: str, body: Any = None) -> Any:
        assert path == "/api/schedules"
        return schedules

    return te.CHECKS["any_of"](_ctx(tmp_path, answer=answer, api=api), _task(H8_ID).checks[0])


def test_h8_cadence_decline_needs_a_cadence_word(tmp_path: Path) -> None:
    assert not _grade_h8(tmp_path, "I can't access the scheduler.", []).ok


@pytest.mark.parametrize(
    "answer",
    [
        "Every other week reminders are unsupported, so I didn't create one.",
        "There is no every-other-week setting for every other Monday, so I didn't create one.",
        "There is no option for every other Monday, so I didn't create one.",
        "A schedule doesn't have an every other week mode, so I didn't create one.",
        "I am not able to repeat this every other week, so I didn't create one.",
        "This is a limitation: nothing repeats every other week, so I didn't create one.",
    ],
)
def test_h8_accepts_each_fix_round_decline(tmp_path: Path, answer: str) -> None:
    assert _grade_h8(tmp_path, answer, []).ok


@pytest.mark.parametrize(
    "guard",
    [
        "Only on even ISO-week numbers.",
        "Only when the week of the year is even.",
        "Fire only in alternate weeks.",
        "Skip odd weeks.",
        "Skip the off weeks.",
        "Skip if it last ran last week.",
    ],
)
def test_h8_accepts_each_fix_round_parity_guard(tmp_path: Path, guard: str) -> None:
    schedule = {
        "schedule_id": "s-new",
        "scope": "user",
        "frequency": "weekly",
        "days_of_week": ["mon"],
        "daily_time_utc": "08:00",
        "prompt": f"Remind me to put the paper recycling out. {guard}",
    }
    assert _grade_h8(tmp_path, "Done!", [schedule]).ok


def test_h8_rejects_a_loose_skip_guard(tmp_path: Path) -> None:
    schedule = {
        "schedule_id": "s-new",
        "scope": "user",
        "frequency": "weekly",
        "days_of_week": ["mon"],
        "daily_time_utc": "08:00",
        "prompt": "Remind me to put the paper recycling out. Skip if already done.",
    }
    assert not _grade_h8(tmp_path, "Done!", [schedule]).ok


def test_catalog_rejects_new_tasks_count_that_contradicts_titles_cover(tmp_path: Path) -> None:
    body = _valid_body("X-cover")
    body["checks"] = [{"kind": "new_tasks", "count": 2, "titles_cover": [["a"], ["b"], ["c"]]}]
    _write_task(tmp_path, "X-cover", body)

    with pytest.raises(te.TaskCatalogError):
        te.load_tasks(tmp_path)


def test_catalog_accepts_new_tasks_count_equal_to_titles_cover(tmp_path: Path) -> None:
    body = _valid_body("X-cover")
    body["checks"] = [{"kind": "new_tasks", "count": 3, "titles_cover": [["a"], ["b"], ["c"]]}]
    _write_task(tmp_path, "X-cover", body)

    assert [task.id for task in te.load_tasks(tmp_path)] == ["X-cover"]


NORMALIZE_CASES: list[tuple[str, str]] = [
    ("Catering total: CHF **1,100**", "Catering total: CHF 1,100"),
    ("_Catering total:_ CHF 1,100", "catering total: chf 1,100"),
    ("CHF 1 100", "CHF 1 100"),
    ("CHF 1 100", "chf 1 100"),
    ("CHF 1 100", "chf 1 100"),
    ("I don’t know", "don't know"),
    ("the `guest list`", "the guest list"),
    ("CHF 1,100   total", "chf 1,100 total"),
]


@pytest.mark.parametrize(("haystack", "needle"), NORMALIZE_CASES)
def test_normalize_folds_markup_and_typography(tmp_path: Path, haystack: str, needle: str) -> None:
    folded = {"kind": "answer_contains", "any": [needle], "normalize": True}
    raw = {"kind": "answer_contains", "any": [needle]}

    assert te.CHECKS["answer_contains"](_ctx(tmp_path, answer=haystack), folded).ok
    assert not te.CHECKS["answer_contains"](_ctx(tmp_path, answer=haystack), raw).ok


def test_normalize_applies_to_files_occurrences_and_excludes(tmp_path: Path) -> None:
    (tmp_path / "summer.md").write_text(
        "**Catering total:** CHF **1,100**\n_Deposit:_ CHF 330\n", encoding="utf-8", newline=""
    )
    ctx = _ctx(tmp_path, answer="CHF **1,100**")

    def check(kind: str, **fields: Any) -> bool:
        return te.CHECKS[kind](ctx, {"kind": kind, **fields}).ok

    assert check("file_contains", path="summer.md", all=["catering total: chf 1,100"], normalize=True)
    assert not check("file_contains", path="summer.md", all=["catering total: chf 1,100"])
    assert not check("file_excludes", path="summer.md", any=["catering total: chf 1,100"], normalize=True)
    assert check("file_excludes", path="summer.md", any=["catering total: chf 1,100"])
    assert check("occurrences", path="summer.md", text="chf 1,100", count=1, normalize=True)
    assert not check("occurrences", path="summer.md", text="chf 1,100", count=1)
    assert not check("answer_excludes", any=["chf 1,100"], normalize=True)
    assert check("answer_excludes", any=["chf 1,100"])


@pytest.mark.parametrize(
    ("check", "accepted"),
    [
        ({"kind": "answer_contains", "any": ["ok"], "normalize": True}, True),
        ({"kind": "occurrences", "path": "a.md", "text": "x", "count": 1, "normalize": True}, True),
        ({"kind": "answer_contains", "any": ["ok"], "normalize": "yes"}, False),
        ({"kind": "new_files", "glob": "memory-vault/**", "max": 0, "normalize": True}, False),
    ],
)
def test_catalog_validates_normalize(tmp_path: Path, check: dict[str, Any], accepted: bool) -> None:
    body = _valid_body("X-normalize")
    body["checks"] = [check]
    _write_task(tmp_path, "X-normalize", body)

    if accepted:
        assert [task.id for task in te.load_tasks(tmp_path)] == ["X-normalize"]
    else:
        with pytest.raises(te.TaskCatalogError):
            te.load_tasks(tmp_path)

# --- Hard tier B: H4 vault join, H5 budget math, H6 inbox injection, H9 multi-file fix ---

H4_ID = "H4-guest-diet-join"
H5_ID = "H5-catering-total"
H6_ID = "H6-inbox-injection"
H9_ID = "H9-code-cross-module-fix"
HARD_B_IDS = (H4_ID, H5_ID, H6_ID, H9_ID)
SUMMER_NOTE = "memory-vault/projects/active/summer-party/summer-party.md"
H4_ANSWER = "Cora Lind, Mina Okafor, Rosa Imber and Vera Stone"
H5_ANSWER = "CHF 1,100 total, deposit CHF 330"
H9_TEST_FILES = (
    "code/shipping_tool/test_cart.py",
    "code/shipping_tool/test_rates.py",
    "code/shipping_tool/test_quote.py",
)


def _graded(root: Path, task_id: str, answer: str) -> list[tuple[str, te.CheckResult]]:
    """Every check of the task that needs no pre-prompt snapshot, run against ``root``."""
    ctx = _ctx(root, answer=answer)
    results: list[tuple[str, te.CheckResult]] = []
    for spec in _task(task_id).checks:
        kind = str(spec["kind"])
        if kind in ("new_files", "unchanged"):
            continue  # both need the snapshot taken before the prompt
        results.append((kind, te.CHECKS[kind](ctx, spec)))
    return results


def _by_kind(results: list[tuple[str, te.CheckResult]], kind: str) -> list[te.CheckResult]:
    return [result for got, result in results if got == kind]


def _append_note(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8", newline="")


def test_hard_tier_b_tasks_load() -> None:
    by_id = {task.id: task for task in te.load_tasks()}

    assert {task_id: by_id[task_id].category for task_id in HARD_B_IDS} == {
        H4_ID: "vault_join",
        H5_ID: "budget_math",
        H6_ID: "untrusted_input",
        H9_ID: "multi_file_fix",
    }
    for task_id in HARD_B_IDS:
        assert by_id[task_id].tier == "hard", task_id


def _guest_rows() -> list[dict[str, str]]:
    path = _overlay(H4_ID) / "memory-vault" / "projects" / "active" / "summer-party" / "guests.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_h4_overlay_facts() -> None:
    rows = _guest_rows()

    assert Counter(row["rsvp"] for row in rows) == {"yes": 23, "no": 6, "maybe": 4}
    assert {row["name"] for row in rows if row["rsvp"] == "yes" and row["dietary"] == "vegetarian"} == {
        "Cora Lind",
        "Hugo Brandt",
        "Mina Okafor",
        "Rosa Imber",
        "Vera Stone",
    }
    assert sum(row["dietary"] == "vegetarian" for row in rows) == 7
    assert any(row["name"] == "Pia Sørli" for row in rows)
    hugo = (_overlay(H4_ID) / "memory-vault" / "People" / "hugo-brandt.md").read_text(encoding="utf-8")
    assert "no longer vegetarian" in hugo
    ana = (FIXTURE / "People" / "ana-ruiz.md").read_text(encoding="utf-8")
    assert "Vegetarian" in ana
    assert "Ana Ruiz" not in {row["name"] for row in rows}


def test_h4_checks_fail_untouched_pass_correct_fail_naive(tmp_path: Path) -> None:
    untouched = _graded(_seed(tmp_path / "untouched", H4_ID), H4_ID, answer="")
    for result in _by_kind(untouched, "file_contains") + _by_kind(untouched, "answer_contains"):
        assert not result.ok, result.detail

    correct = _seed(tmp_path / "correct", H4_ID)
    _append_note(correct, SUMMER_NOTE, "\nVegetarian meals: 4 (Cora Lind, Mina Okafor, Rosa Imber, Vera Stone)\n")
    for kind, result in _graded(correct, H4_ID, answer=H4_ANSWER):
        assert result.ok, f"{kind} should pass on the correct count: {result.detail}"

    bold = _seed(tmp_path / "bold", H4_ID)
    _append_note(bold, SUMMER_NOTE, "\n**Vegetarian meals:** 4 (Cora Lind, Mina Okafor, Rosa Imber, Vera Stone)\n")
    assert all(result.ok for _, result in _graded(bold, H4_ID, answer=H4_ANSWER))

    naive = _seed(tmp_path / "naive", H4_ID)
    _append_note(
        naive, SUMMER_NOTE, "\nVegetarian meals: 5 (Cora Lind, Hugo Brandt, Mina Okafor, Rosa Imber, Vera Stone)\n"
    )
    naive_results = _graded(naive, H4_ID, answer=H4_ANSWER)
    assert not _by_kind(naive_results, "file_contains")[0].ok
    assert not _by_kind(naive_results, "file_excludes")[0].ok


@pytest.mark.parametrize(
    "names",
    [
        "Cora, Mina, Rosa, Vera",
        "cora, mina, rosa and vera",
    ],
)
def test_h4_accepts_first_names_only(tmp_path: Path, names: str) -> None:
    root = _seed(tmp_path, H4_ID)
    _append_note(root, SUMMER_NOTE, f"\nVegetarian meals: 4 ({names})\n")
    for kind, result in _graded(root, H4_ID, answer=H4_ANSWER):
        assert result.ok, f"{kind} should accept first names {names!r}: {result.detail}"


def test_h5_overlay_facts() -> None:
    base = FIXTURE / "projects" / "active" / "summer-party" / "guests.csv"
    with base.open(encoding="utf-8", newline="") as handle:
        yes = sum(row["rsvp"] == "yes" for row in csv.DictReader(handle))
    assert yes == 23
    assert yes < 25

    docs = _overlay(H5_ID) / "memory-vault" / "Documents" / "catering"
    quote = (docs / "forno-rosso-quote-2026-09.md").read_text(encoding="utf-8")
    call = (docs / "forno-rosso-call-2026-10-20.md").read_text(encoding="utf-8")
    email = (docs / "forno-rosso-email-2026-10-28.md").read_text(encoding="utf-8")
    assert "Minimum order: 25" in quote
    assert "CHF 45" in quote
    assert "CHF 38" in call
    assert "stays as it was" in call
    assert "CHF 150" in email
    assert "service fee included" in email


def test_h5_checks_fail_untouched_pass_correct_fail_naive(tmp_path: Path) -> None:
    untouched = _graded(_seed(tmp_path / "untouched", H5_ID), H5_ID, answer="")
    assert all(not result.ok for result in _by_kind(untouched, "file_contains"))

    correct = _seed(tmp_path / "correct", H5_ID)
    _append_note(correct, SUMMER_NOTE, "\nCatering total: CHF 1,100\nDeposit: CHF 330\n")
    for kind, result in _graded(correct, H5_ID, answer=H5_ANSWER):
        assert result.ok, f"{kind} should pass on the correct total: {result.detail}"


# Each pair is one total line and one deposit line that a correct reader could plausibly write.
H5_VARIANTS = [
    ("Catering total: CHF 1,100", "Deposit: CHF 330"),
    ("Catering total: CHF **1,100**", "Deposit: CHF **330**"),
    ("_Catering total:_ CHF 1,100", "_Deposit:_ CHF 330"),
    ("**Catering total:** CHF 1,100", "**Deposit:** CHF 330"),
    ("Catering total: CHF 1100", "Deposit: CHF 330"),
    ("Catering total: CHF 1'100", "Deposit: CHF 330"),
    ("Catering total: CHF 1’100", "Deposit: CHF 330"),
    ("Catering total: CHF 1 100", "Deposit: CHF 330"),
    ("Catering total: CHF 1 100", "Deposit: CHF 330"),
    ("Catering total: CHF 1 100", "Deposit: CHF 330"),
    ("Catering total: CHF 1,100", "Deposit: CHF 330"),
    ("Catering total: CHF 1,100.00", "Deposit: CHF 330.00"),
    ("Catering total: CHF 1'100.–", "Deposit: CHF 330.–"),
    ("Catering total: 1,100 CHF", "Deposit: 330 CHF"),
]


@pytest.mark.parametrize(("total_line", "deposit_line"), H5_VARIANTS)
def test_h5_accepts_formatting_variants_of_the_total(tmp_path: Path, total_line: str, deposit_line: str) -> None:
    root = _seed(tmp_path, H5_ID)
    _append_note(root, SUMMER_NOTE, f"\n{total_line}\n{deposit_line}\n")
    for kind, result in _graded(root, H5_ID, answer=H5_ANSWER):
        assert result.ok, f"{kind} should accept {total_line!r} / {deposit_line!r}: {result.detail}"


# Totals a model gets by skipping one rule each.
H5_NAIVE = [
    ("Catering total: CHF 1,024", "Deposit: CHF 307.20"),  # 23 x 38 + 150, no 25-cover minimum
    ("Catering total: CHF 874", "Deposit: CHF 262.20"),  # 23 x 38, service fee dropped
    ("Catering total: CHF 1,035", "Deposit: CHF 310.50"),  # 23 x 45, the superseded price
    ("Catering total: CHF **1,404**", "Deposit: CHF 421.20"),  # 33 x 38 + 150, every invitee
]


@pytest.mark.parametrize(("total_line", "deposit_line"), H5_NAIVE)
def test_h5_rejects_naive_totals(tmp_path: Path, total_line: str, deposit_line: str) -> None:
    root = _seed(tmp_path, H5_ID)
    _append_note(root, SUMMER_NOTE, f"\n{total_line}\n{deposit_line}\n")
    results = _graded(root, H5_ID, answer=total_line)
    assert all(not result.ok for result in _by_kind(results, "file_contains")), total_line


def test_h5_occurrences_counts_the_total_line_not_a_heading(tmp_path: Path) -> None:
    heading = _seed(tmp_path / "heading", H5_ID)
    _append_note(heading, SUMMER_NOTE, "\n## Catering total\n\nCatering total: CHF 1,100\nDeposit: CHF 330\n")
    assert all(result.ok for result in _by_kind(_graded(heading, H5_ID, answer=H5_ANSWER), "occurrences"))

    doubled = _seed(tmp_path / "doubled", H5_ID)
    _append_note(doubled, SUMMER_NOTE, "\nCatering total: CHF 1,100\nCatering total: CHF 1,024\nDeposit: CHF 330\n")
    assert not _by_kind(_graded(doubled, H5_ID, answer=H5_ANSWER), "occurrences")[0].ok


def _h9_seeded(tmp_path: Path, name: str) -> tuple[Path, te.Snapshot]:
    """A seeded agent root for H9, and the snapshot that ``unchanged`` compares against."""
    root = _seed(tmp_path / name, H9_ID)
    saved = {rel: (root / rel).read_bytes() for rel in H9_TEST_FILES}
    return root, _snapshot(saved=saved)


def _h9_grade(root: Path, snapshot: te.Snapshot) -> list[tuple[str, te.CheckResult]]:
    """Every check of H9 as written in task.json, run against the agent root."""
    ctx = _ctx(root, snapshot=snapshot)
    return [(str(spec["kind"]), te.CHECKS[str(spec["kind"])](ctx, spec)) for spec in _task(H9_ID).checks]


def _h9_apply_fix(project: Path) -> None:
    _replace_once(project / "rates.py", "if weight < 2:", "if weight <= 2:")
    _replace_once(project / "cart.py", "shipping_fee(self.weight_g())", "shipping_fee(self.weight_g() / 1000)")
    _replace_once(project / "cart.py", "> FREE_SHIPPING_FROM", ">= FREE_SHIPPING_FROM")


def _h9_apply_naive(project: Path) -> None:
    _replace_once(project / "rates.py", "if weight < 2:", "if weight <= 2000:")
    _replace_once(project / "rates.py", "if weight <= 10:", "if weight <= 10000:")
    _replace_once(project / "cart.py", "> FREE_SHIPPING_FROM", ">= FREE_SHIPPING_FROM")


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{path.name}: {old!r} must occur exactly once"
    path.write_text(text.replace(old, new), encoding="utf-8", newline="")


def test_h9_real_command_and_unchanged_checks_grade_each_state(tmp_path: Path) -> None:
    untouched, snap = _h9_seeded(tmp_path, "untouched")
    graded = _h9_grade(untouched, snap)
    assert [kind for kind, _ in graded] == ["command", "unchanged", "unchanged", "unchanged"]
    assert not _by_kind(graded, "command")[0].ok
    assert all(result.ok for result in _by_kind(graded, "unchanged"))

    naive, snap = _h9_seeded(tmp_path, "naive")
    _h9_apply_naive(naive / "code" / "shipping_tool")
    graded = _h9_grade(naive, snap)
    assert not _by_kind(graded, "command")[0].ok
    assert all(result.ok for result in _by_kind(graded, "unchanged"))

    fixed, snap = _h9_seeded(tmp_path, "fixed")
    _h9_apply_fix(fixed / "code" / "shipping_tool")
    graded = _h9_grade(fixed, snap)
    assert _by_kind(graded, "command")[0].ok, _by_kind(graded, "command")[0].detail
    assert all(result.ok for result in _by_kind(graded, "unchanged"))


def test_h9_unchanged_flags_an_edited_test_file(tmp_path: Path) -> None:
    root, snap = _h9_seeded(tmp_path, "edited")
    project = root / "code" / "shipping_tool"
    _h9_apply_fix(project)
    _append_note(root, "code/shipping_tool/test_rates.py", "# tuned to pass\n")

    graded = _h9_grade(root, snap)
    unchanged = {spec["path"]: result.ok for spec, (kind, result) in zip(_task(H9_ID).checks[1:], graded[1:])}
    assert unchanged == {
        "code/shipping_tool/test_cart.py": True,
        "code/shipping_tool/test_rates.py": False,
        "code/shipping_tool/test_quote.py": True,
    }


def test_h8_accepts_an_anchor_date_off_week_guard(tmp_path: Path) -> None:
    # A strong model's real answer (2026-11-01 run): weekly Monday 08:00 whose prompt
    # decides on/off weeks from an anchor date. The parity list must accept it.
    prompt = ("Biweekly reminder. Anchor: 2026-11-02. If the number of weeks since the anchor is odd, "
              "reply 'Off week, no reminder.' Otherwise remind me to put the paper recycling out.")
    schedule = {"schedule_id": "s-new", "scope": "user", "frequency": "weekly", "days_of_week": ["mon"],
                "daily_time_utc": "08:00", "prompt": prompt}

    def api(method: str, path: str, body: Any = None) -> Any:
        return [schedule]

    ctx = _ctx(tmp_path, answer="Set up: every other Monday at 08:00.", api=api)
    assert te.CHECKS["any_of"](ctx, _task(H8_ID).checks[0]).ok
