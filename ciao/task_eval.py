"""Agentic task eval: run real chat turns on an isolated engine and grade the workspace.

Where ``behavioral_eval`` asks a model to *describe* the tool calls it would
make, this module lets Ciaobot act: each task boots its own engine on a
synthetic fixture vault, runs one chat turn (or delegates one task), grades the
resulting workspace state with deterministic checks, and records token, tool,
wall-time and cost figures. Nothing reads a live vault.

Catalog: ``ciao/stock/evals/tasks/<task_id>/task.json`` (plus an optional
``overlay/`` tree). Docs: ``docs/TASK_EVAL.md``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import signal
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Protocol

import yaml

from ciao import behavioral_eval

TASKS_ROOT: Path = Path(__file__).resolve().parent / "stock" / "evals" / "tasks"
FIXTURE_DIRNAME = "_fixture"
WORKSPACE = "personal"
TIERS: frozenset[str] = frozenset({"easy", "medium", "hard"})
ENTRY_KINDS: frozenset[str] = frozenset({"prompt", "delegate"})
SETUP_KINDS: frozenset[str] = frozenset({"task", "project"})
SCHEMA = "ciao-task-eval/1"
DEFAULT_TIMEOUT_S = 600.0
ANSWER_TAIL_CHARS = 2000

# Phrases that count as a clean refusal to answer from the vault. Curly
# apostrophes are accepted too, since models often emit them.
ABSTAIN_RE = re.compile(
    r"don[’']t know|do not know|not (recorded|mentioned|in (the|your) vault|listed)"
    r"|no (record|information|mention)|couldn[’']t find|could not find|can[’']t find"
    r"|cannot find|doesn[’']t (say|mention|record|include)"
    r"|does not (say|mention|record|include)|isn[’']t (recorded|mentioned)",
    re.IGNORECASE,
)
_MD_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
WEEKDAY_NAMES: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class TaskCatalogError(ValueError):
    """A task catalog entry is malformed or violates the catalog contract."""


@dataclass(frozen=True)
class EvalTask:
    id: str
    tier: str
    category: str
    prompt: str
    entry: dict[str, Any]
    setup: list[dict[str, Any]]
    requires: list[str]
    timeout_s: float
    checks: list[dict[str, Any]]
    overlay: Path | None = None


@dataclass(frozen=True)
class CheckResult:
    kind: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Snapshot:
    """Workspace state after setup and before the entry runs."""

    task_ids: frozenset[str]
    schedule_ids: frozenset[str]
    project_ids: frozenset[str]
    files: frozenset[str]
    saved: dict[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class GradeContext:
    agent_root: Path
    answer: str
    api: Callable[..., Any]
    snapshot: Snapshot
    run_date: date


CheckFn = Callable[[GradeContext, dict[str, Any]], CheckResult]


# --- catalog ----------------------------------------------------------------------


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TaskCatalogError(f"{path}: not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise TaskCatalogError(f"{path}: must be a JSON object")
    return raw


def _require_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskCatalogError(f"{where}: must be a non-empty string")
    return value


def _validate_check(task_id: str, spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise TaskCatalogError(f"{task_id}: each check must be an object")
    kind = spec.get("kind")
    if kind not in CHECKS:
        raise TaskCatalogError(f"{task_id}: unknown check kind {kind!r}")
    return spec


def _validate_setup(task_id: str, steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list):
        raise TaskCatalogError(f"{task_id}: setup must be a list")
    for step in steps:
        if not isinstance(step, dict) or step.get("kind") not in SETUP_KINDS:
            raise TaskCatalogError(f"{task_id}: setup step kind must be one of {sorted(SETUP_KINDS)}")
        if step["kind"] == "task":
            _require_text(step.get("title"), f"{task_id}: setup task title")
        else:
            _require_text(step.get("name"), f"{task_id}: setup project name")
    return list(steps)


def _load_one(folder: Path) -> EvalTask:
    task_id = folder.name
    body = _read_json_object(folder / "task.json")
    if body.get("id") != task_id:
        raise TaskCatalogError(f"{task_id}: id {body.get('id')!r} must equal the directory name")
    tier = body.get("tier")
    if tier not in TIERS:
        raise TaskCatalogError(f"{task_id}: tier must be one of {sorted(TIERS)}")
    entry = body.get("entry")
    if not isinstance(entry, dict) or entry.get("kind") not in ENTRY_KINDS:
        raise TaskCatalogError(f"{task_id}: entry.kind must be one of {sorted(ENTRY_KINDS)}")
    prompt = str(body.get("prompt") or "")
    if entry["kind"] == "prompt":
        _require_text(prompt, f"{task_id}: prompt")
    else:
        _require_text(entry.get("task_title"), f"{task_id}: entry.task_title")
    requires = body.get("requires", [])
    if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
        raise TaskCatalogError(f"{task_id}: requires must be a list of capability names")
    checks = body.get("checks")
    if not isinstance(checks, list) or not checks:
        raise TaskCatalogError(f"{task_id}: checks must be a non-empty list")
    overlay = folder / "overlay"
    return EvalTask(
        id=task_id,
        tier=str(tier),
        category=_require_text(body.get("category"), f"{task_id}: category"),
        prompt=prompt,
        entry=dict(entry),
        setup=_validate_setup(task_id, body.get("setup", [])),
        requires=list(requires),
        timeout_s=float(body.get("timeout_s", DEFAULT_TIMEOUT_S)),
        checks=[_validate_check(task_id, c) for c in checks],
        overlay=overlay if overlay.is_dir() else None,
    )


def _reject_private_markers(root: Path) -> None:
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        raw = path.read_bytes()
        for marker in behavioral_eval.PRIVATE_MARKERS:
            if marker.encode("utf-8") in raw:
                rel = path.relative_to(root).as_posix()
                raise TaskCatalogError(f"{rel}: contains private marker {marker!r}")


def load_tasks(root: Path | None = None) -> list[EvalTask]:
    """Every task in the catalog, validated. Subdirectories starting with ``_`` are skipped."""
    base = TASKS_ROOT if root is None else root
    if not base.is_dir():
        raise TaskCatalogError(f"task catalog directory missing: {base}")
    _reject_private_markers(base)
    tasks: list[EvalTask] = []
    seen: set[str] = set()
    for folder in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("_")):
        task = _load_one(folder)
        if task.id in seen:
            raise TaskCatalogError(f"duplicate task id {task.id!r}")
        seen.add(task.id)
        tasks.append(task)
    return tasks


def catalog_sha256(root: Path | None = None) -> str:
    """sha256 over sorted (relative path, bytes) of everything in the catalog."""
    base = TASKS_ROOT if root is None else root
    digest = hashlib.sha256()
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        digest.update(path.relative_to(base).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _fail(kind: str, detail: str) -> CheckResult:
    return CheckResult(kind=kind, ok=False, detail=detail)


def _text_has(text: str, needles: list[str], *, all_of: bool) -> bool:
    lowered = text.lower()
    hits = [needle.lower() in lowered for needle in needles]
    return all(hits) if all_of else any(hits)


def _contains_spec(text: str, spec: dict[str, Any]) -> tuple[bool, str]:
    """Apply an ``all``/``any`` needle spec; both absent is a malformed spec."""
    all_needles = [str(n) for n in spec.get("all", [])]
    any_needles = [str(n) for n in spec.get("any", [])]
    if not all_needles and not any_needles:
        return False, "check names no needles (all/any)"
    ok = True
    if all_needles:
        ok = ok and _text_has(text, all_needles, all_of=True)
    if any_needles:
        ok = ok and _text_has(text, any_needles, all_of=False)
    return ok, f"all={all_needles} any={any_needles}"


def _resolve_in_root(root: Path, rel: str) -> Path:
    """The resolved path of ``rel`` under ``root``; anything that escapes it, symlinks included, is refused."""
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ValueError(f"path must be relative to the agent root: {rel!r}")
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes the agent root through a symlink: {rel!r}")
    return candidate


def _check_answer_contains(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    ok, detail = _contains_spec(ctx.answer, spec)
    return CheckResult("answer_contains", ok, detail)


def _check_answer_excludes(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    needles = [str(n) for n in spec.get("any", [])]
    if not needles:
        return _fail("answer_excludes", "check names no needles (any)")
    present = [n for n in needles if n.lower() in ctx.answer.lower()]
    return CheckResult("answer_excludes", not present, f"present={present}")


def _check_abstains(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    matched = ABSTAIN_RE.search(ctx.answer) is not None
    return CheckResult("abstains", matched, "declined" if matched else "answered")


def _tasks_in_workspace(ctx: GradeContext) -> list[dict[str, Any]]:
    payload = ctx.api("GET", f"/api/tasks?workspace={WORKSPACE}")
    return list(payload.get("tasks", []))


def _check_new_tasks(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    new = [
        row
        for row in _tasks_in_workspace(ctx)
        if row.get("id") and row["id"] not in ctx.snapshot.task_ids
    ]
    count = int(spec.get("count", 1))
    problems: list[str] = []
    if len(new) != count:
        problems.append(f"expected {count} new task(s), found {len(new)}")
    needles = [str(n).lower() for n in spec.get("title_contains", [])]
    due_spec = spec.get("due")
    for row in new:
        title = str(row.get("title") or "")
        for needle in needles:
            if needle not in title.lower():
                problems.append(f"title {title!r} lacks {needle!r}")
        if due_spec is not None and not _due_matches(row.get("due"), str(due_spec), ctx.run_date):
            problems.append(f"task {title!r} due {row.get('due')!r} != {due_spec!r}")
    return CheckResult("new_tasks", not problems, "; ".join(problems) or f"{len(new)} new task(s) ok")


def _due_matches(due: Any, spec: str, run_date: date) -> bool:
    if not due:
        return False
    if not spec.startswith("weekday:"):
        return str(due) == spec
    wanted = spec.split(":", 1)[1].strip().lower()
    try:
        due_date = date.fromisoformat(str(due))
    except ValueError:
        return False
    if WEEKDAY_NAMES[due_date.weekday()] != wanted[:3]:
        return False
    return 1 <= (due_date - run_date).days <= 7


def _check_new_schedules(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    payload = ctx.api("GET", "/api/schedules")
    new = [
        row
        for row in payload
        if row.get("schedule_id") not in ctx.snapshot.schedule_ids
        and row.get("scope", "user") != "system"
    ]
    count = int(spec.get("count", 1))
    problems: list[str] = []
    if len(new) != count:
        problems.append(f"expected {count} new schedule(s), found {len(new)}")
    for row in new:
        if "frequency" in spec and row.get("frequency") != spec["frequency"]:
            problems.append(f"frequency {row.get('frequency')!r} != {spec['frequency']!r}")
        if "days_of_week" in spec:
            got = {str(d).lower()[:3] for d in (row.get("days_of_week") or [])}
            want = {str(d).lower()[:3] for d in spec["days_of_week"]}
            if got != want:
                problems.append(f"days {sorted(got)} != {sorted(want)}")
        if "time" in spec and row.get("daily_time_utc") != spec["time"]:
            problems.append(f"time {row.get('daily_time_utc')!r} != {spec['time']!r}")
        prompt = str(row.get("prompt") or "").lower()
        for needle in spec.get("prompt_contains", []):
            if str(needle).lower() not in prompt:
                problems.append(f"prompt lacks {needle!r}")
    return CheckResult("new_schedules", not problems, "; ".join(problems) or f"{len(new)} new schedule(s) ok")


def _read_note(ctx: GradeContext, rel: str) -> str:
    return _resolve_in_root(ctx.agent_root, rel).read_text(encoding="utf-8")


def _check_file_contains(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    try:
        text = _read_note(ctx, str(spec["path"]))
    except (OSError, ValueError) as exc:
        return _fail("file_contains", f"cannot read {spec.get('path')!r}: {exc}")
    ok, detail = _contains_spec(text, spec)
    return CheckResult("file_contains", ok, detail)


def _check_file_excludes(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    try:
        text = _read_note(ctx, str(spec["path"]))
    except (OSError, ValueError) as exc:
        return _fail("file_excludes", f"cannot read {spec.get('path')!r}: {exc}")
    present = [n for n in spec.get("any", []) if str(n).lower() in text.lower()]
    return CheckResult("file_excludes", not present, f"present={present}")


def _check_occurrences(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    try:
        text = _read_note(ctx, str(spec["path"]))
    except (OSError, ValueError) as exc:
        return _fail("occurrences", f"cannot read {spec.get('path')!r}: {exc}")
    needle = str(spec["text"]).lower()
    found = text.lower().count(needle)
    want = int(spec["count"])
    return CheckResult("occurrences", found == want, f"found {found}, want {want}")


def _glob_to_regex(pattern: str) -> str:
    """Translate a relative glob (``*``, ``?``, ``**``) into a posix-path regex."""
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return "".join(parts)


def _glob_match(rel: str, pattern: str) -> bool:
    """Whether a posix relative path matches a relative glob."""
    return re.fullmatch(_glob_to_regex(pattern), rel) is not None


def _list_files(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _check_new_files(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    pattern = str(spec["glob"])
    new = sorted(
        rel
        for rel in _list_files(ctx.agent_root)
        if _glob_match(rel, pattern) and rel not in ctx.snapshot.files
    )
    if "count" in spec:
        want = int(spec["count"])
        return CheckResult("new_files", len(new) == want, f"{len(new)} new, want {want}: {new[:10]}")
    limit = int(spec.get("max", 0))
    return CheckResult("new_files", len(new) <= limit, f"{len(new)} new, max {limit}: {new[:10]}")


_FENCE_RE = re.compile(r"^---[ \t]*$", re.MULTILINE)


def _frontmatter_problems(text: str) -> str | None:
    if not text.startswith("---\n"):
        return "no frontmatter block"
    closing = _FENCE_RE.search(text, 4)
    if closing is None:
        return "unterminated frontmatter block"
    try:
        block = yaml.safe_load(text[4:closing.start()])
    except yaml.YAMLError as exc:
        return f"frontmatter does not parse: {exc}"
    if not isinstance(block, dict):
        return "frontmatter is not a mapping"
    missing = [key for key in ("type", "updated") if key not in block]
    return f"frontmatter lacks {missing}" if missing else None


def _check_frontmatter_valid(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    if "path" in spec:
        targets = [str(spec["path"])]
    else:
        targets = [rel for rel in _list_files(ctx.agent_root) if _glob_match(rel, str(spec["glob"]))]
    if not targets:
        return _fail("frontmatter_valid", f"nothing matched {spec.get('path') or spec.get('glob')!r}")
    problems: list[str] = []
    for rel in targets:
        try:
            text = _read_note(ctx, rel)
        except (OSError, ValueError) as exc:
            problems.append(f"{rel}: {exc}")
            continue
        issue = _frontmatter_problems(text)
        if issue:
            problems.append(f"{rel}: {issue}")
    return CheckResult("frontmatter_valid", not problems, "; ".join(problems) or f"{len(targets)} note(s) ok")


def _check_links_resolve(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    rel = str(spec["path"])
    try:
        note = _resolve_in_root(ctx.agent_root, rel)
        text = note.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return _fail("links_resolve", f"cannot read {rel!r}: {exc}")
    broken: list[str] = []
    for target in _MD_LINK_RE.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        local = target.split("#", 1)[0].split("?", 1)[0]
        if not (note.parent / local).exists():
            broken.append(target)
    return CheckResult("links_resolve", not broken, f"broken: {broken}" if broken else "all links resolve")


def _check_unchanged(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    rel = str(spec["path"])
    if rel not in ctx.snapshot.saved:
        return _fail("unchanged", f"{rel!r} was not captured in the snapshot")
    try:
        current = _resolve_in_root(ctx.agent_root, rel).read_bytes()
    except (OSError, ValueError) as exc:
        return _fail("unchanged", f"cannot read {rel!r}: {exc}")
    return CheckResult("unchanged", current == ctx.snapshot.saved[rel], rel)


def _check_task_attempt(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    needle = str(spec["title_contains"]).lower()
    matches = [row for row in _tasks_in_workspace(ctx) if needle in str(row.get("title") or "").lower()]
    if not matches:
        return _fail("task_attempt", f"no task titled like {needle!r}")
    task_id = str(matches[0]["id"])
    payload = ctx.api("GET", f"/api/tasks/{task_id}/attempts?workspace={WORKSPACE}")
    outcomes = [str(a.get("outcome") or "") for a in payload.get("attempts", [])]
    want = str(spec["outcome"])
    count = int(spec.get("count", 1))
    found = outcomes.count(want)
    return CheckResult("task_attempt", found == count, f"{found} {want!r} of {count}; attempts={outcomes}")


def _check_command(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    argv = [sys.executable if part == "{python}" else str(part) for part in spec["argv"]]
    rel_cwd = str(spec.get("cwd", ""))
    cwd = ctx.agent_root if rel_cwd in ("", ".") else _resolve_in_root(ctx.agent_root, rel_cwd)
    timeout = float(spec.get("timeout_s", 120))
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=_without_live_vars(_BASE_ENV),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _fail("command", repr(exc)[:400])
    output = (proc.stdout or "") + (proc.stderr or "")
    return CheckResult("command", proc.returncode == 0, output[-400:])


def _check_new_projects(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    payload = ctx.api("GET", "/api/projects")
    new = [row for row in payload if row.get("project_id") not in ctx.snapshot.project_ids]
    count = int(spec.get("count", 1))
    problems: list[str] = []
    if len(new) != count:
        problems.append(f"expected {count} new project(s), found {len(new)}")
    for row in new:
        name = str(row.get("name") or "").lower()
        for needle in spec.get("name_contains", []):
            if str(needle).lower() not in name:
                problems.append(f"project {name!r} lacks {needle!r}")
    return CheckResult("new_projects", not problems, "; ".join(problems) or f"{len(new)} new project(s) ok")


CHECKS: dict[str, CheckFn] = {
    "answer_contains": _check_answer_contains,
    "answer_excludes": _check_answer_excludes,
    "abstains": _check_abstains,
    "new_tasks": _check_new_tasks,
    "new_schedules": _check_new_schedules,
    "new_projects": _check_new_projects,
    "file_contains": _check_file_contains,
    "file_excludes": _check_file_excludes,
    "occurrences": _check_occurrences,
    "new_files": _check_new_files,
    "frontmatter_valid": _check_frontmatter_valid,
    "links_resolve": _check_links_resolve,
    "unchanged": _check_unchanged,
    "task_attempt": _check_task_attempt,
    "command": _check_command,
}


def grade(task: EvalTask, ctx: GradeContext) -> list[CheckResult]:
    """Run every check of a task. A check that raises is a failed check, never a crash."""
    results: list[CheckResult] = []
    for spec in task.checks:
        kind = str(spec.get("kind"))
        try:
            results.append(CHECKS[kind](ctx, spec))
        except Exception as exc:  # noqa: BLE001 - a broken check must not abort grading
            results.append(_fail(kind, f"check raised {exc!r}"[:400]))
    return results


# --- isolation --------------------------------------------------------------------

ENV_PREFIXES: tuple[str, ...] = ("CIAO_", "PWA_", "GWS_")
# The caller's environment, captured once at import. Threads never read os.environ
# (it is temporarily rewritten while setup_workspace runs), so every subprocess and
# engine environment is built from this snapshot.
_BASE_ENV: dict[str, str] = dict(os.environ)


def _without_live_vars(env: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in env.items() if not key.startswith(ENV_PREFIXES)}
ENGINE_READY_TIMEOUT_S = 60.0
ENGINE_STOP_TIMEOUT_S = 20.0
SETTLE_POLL_S = 2.0
SETTLE_MIN_AFTER_ACCEPT_S = 5.0
SETTLE_STABLE_POLLS = 2
# setup_workspace reads os.environ and the working directory, so the engine
# boot holds this lock while it runs. Engine runs themselves are independent.
_PROCESS_STATE_LOCK = threading.Lock()


class EngineAPIError(RuntimeError):
    """The isolated engine answered an API call with an error status."""


def engine_env(base_env: Mapping[str, str], workdir: Path, ws: Path) -> dict[str, str]:
    """The environment an isolated engine runs with.

    Every ``CIAO_``/``PWA_``/``GWS_`` variable from the caller is dropped, so a
    run started inside a live Ciaobot chat cannot reach the live install. The
    storage roots are pointed into the throwaway ``workdir``.
    """
    env = _without_live_vars(base_env)
    env.update(
        {
            "CIAO_WORKSPACE": str(ws),
            "CIAO_NO_BROWSER": "1",
            "CIAO_MEMORY_DIR": str(workdir / "dotciao"),
            "CIAO_LAUNCH_AGENTS_DIR": str(workdir / "LA"),
            "CIAO_QUEUE_LOCK_DIR": str(workdir / "locks"),
            "PWA_HOST": "127.0.0.1",
        }
    )
    return env


@contextlib.contextmanager
def _engine_environ(workdir: Path, ws: Path) -> Iterator[None]:
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(engine_env(_BASE_ENV, workdir, ws))
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@contextlib.contextmanager
def _in_directory(path: Path) -> Iterator[None]:
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _pin_workspace_model(registry: Path, provider: str, model: str) -> None:
    """Write the requested provider and model into the eval workspace's registry entry.

    ``CiaoConfig.default_model_for_workspace`` reads ``provider_overrides`` from
    the entry, so delegated chats (which take the workspace default) resolve to
    the requested model.
    """
    entries: Any = json.loads(registry.read_text(encoding="utf-8"))
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == WORKSPACE:
            entry["provider_overrides"] = {provider: {"model": model}}
            registry.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8", newline="")
            return
    raise RuntimeError(f"workspace registry has no {WORKSPACE!r} entry: {registry}")


def prepare_workspace(
    workdir: Path,
    provider: str,
    model: str,
    task: EvalTask,
    *,
    auth_token: str | None = None,
    port: int | None = None,
) -> Path:
    """Build the throwaway workspace under ``workdir/ws`` without booting anything.

    ``setup_workspace`` runs with the live environment scrubbed and the working
    directory set to ``workdir``. The fixture vault is copied into
    ``ws/personal/memory-vault`` and the task overlay over ``ws/personal``. The
    workspace defaults to ``provider`` and pins ``model`` for it.
    """
    from ciao import cli

    workdir.mkdir(parents=True, exist_ok=True)
    ws = workdir / "ws"
    with _PROCESS_STATE_LOCK, _engine_environ(workdir, ws), _in_directory(workdir):
        cli.setup_workspace(
            ws,
            auth_token=auth_token or secrets.token_urlsafe(32),
            port=port or _free_port(),
            launch_agents_dir=workdir / "LA",
            app_dir=workdir / "Apps",
            default_provider=provider,
        )
    _pin_workspace_model(ws / ".runtime" / "workspaces.json", provider, model)
    personal = ws / WORKSPACE
    shutil.copytree(TASKS_ROOT / FIXTURE_DIRNAME, personal / "memory-vault", dirs_exist_ok=True)
    if task.overlay is not None:
        shutil.copytree(task.overlay, personal, dirs_exist_ok=True)
    return ws


def _stop_process_group(proc: subprocess.Popen[bytes]) -> None:
    """Stop an engine and whatever it spawned (provider CLIs), on POSIX via its process group."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=ENGINE_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=ENGINE_STOP_TIMEOUT_S)
    if sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)


class EngineSession(Protocol):
    """What a task run needs from an engine: API access, a chat, and its usage."""

    agent_root: Path

    def api(self, method: str, path: str, body: Any = None) -> Any: ...

    def start_prompt(self, prompt: str, *, provider: str, model: str) -> str: ...

    def delegate(self, task_id: str, revision: str, *, provider: str, model: str) -> str: ...

    def wait_settled(self, timeout_s: float) -> bool: ...

    def stop_chat(self, chat_id: str) -> None: ...

    def answer_text(self) -> str: ...

    def usage(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class IsolatedEngine(EngineSession):
    """A real engine on a throwaway workspace, booted the way the operator boots one.

    The agent root is ``ws/personal``; the fixture vault is copied to
    ``ws/personal/memory-vault`` and the task overlay goes over ``ws/personal``. Use :meth:`boot`, which
    either returns a running, authenticated engine or cleans up after itself.
    """

    def __init__(self, workdir: Path, *, keep_workdir: bool) -> None:
        self.workdir = workdir
        self.ws = workdir / "ws"
        self.agent_root = self.ws / WORKSPACE
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.token = secrets.token_urlsafe(32)
        self._keep_workdir = keep_workdir
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_path = workdir / "engine.log"
        self._log: Any = None
        self._host_project_id = ""
        self._accepted_at = 0.0
        self._chat_ids: list[str] = []
        self._closed = False

    @classmethod
    def boot(
        cls, task: EvalTask, *, provider: str, model: str, keep_workdir: bool = False
    ) -> "IsolatedEngine":
        workdir = Path(tempfile.mkdtemp(prefix="ciao-task-eval-"))
        engine = cls(workdir, keep_workdir=keep_workdir)
        try:
            engine._start(task, provider, model)
        except BaseException:
            engine.close()
            raise
        return engine

    def _start(self, task: EvalTask, provider: str, model: str) -> None:
        prepare_workspace(
            self.workdir, provider, model, task, auth_token=self.token, port=self.port
        )
        self._log = self._log_path.open("wb")
        script = Path(sys.executable).with_name("ciao")
        command = [str(script), "run"] if script.exists() else [sys.executable, "-m", "ciao", "run"]
        group: dict[str, Any] = {"start_new_session": True} if sys.platform != "win32" else {}
        self._proc = subprocess.Popen(
            command,
            cwd=self.ws,
            env=engine_env(_BASE_ENV, self.workdir, self.ws),
            stdout=self._log,
            stderr=subprocess.STDOUT,
            **group,
        )
        self._wait_ready()
        self.api("POST", "/api/auth", {"token": self.token})
        project = self.api(
            "POST",
            "/api/projects",
            {
                "name": "Task eval host",
                "workspace": WORKSPACE,
                "context": "Hosts evaluation chats. Not a real project.",
            },
        )
        self._host_project_id = str(project["project_id"])

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + ENGINE_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(f"engine exited with {self._proc.returncode}: {_tail(self._log_path)}")
            try:
                urllib.request.urlopen(f"{self.base}/api/status", timeout=2)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"engine did not answer /api/status within {ENGINE_READY_TIMEOUT_S:.0f} s")

    def api(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Origin": self.base, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=60) as response:
                raw: bytes = response.read()
        except urllib.error.HTTPError as exc:
            raise EngineAPIError(f"{method} {path} -> {exc.code}: {exc.read()[:300]!r}") from exc
        return json.loads(raw) if raw else None

    def start_prompt(self, prompt: str, *, provider: str, model: str) -> str:
        chat = self.api(
            "POST",
            f"/api/projects/{self._host_project_id}/chats",
            {"title": "Task eval", "provider": provider, "model": model},
        )
        chat_id = str(chat["chat_id"])
        self.api("POST", f"/api/chats/{chat_id}/prompt", {"prompt": prompt})
        self._accepted_at = time.monotonic()
        self._chat_ids.append(chat_id)
        return chat_id

    def delegate(self, task_id: str, revision: str, *, provider: str, model: str) -> str:
        # The delegate route takes no provider or model (_DELEGATE_KEYS in
        # ciao/web/routes_tasks.py). The spawned chat takes the workspace's
        # default provider and model, which prepare_workspace pinned to the
        # requested pair.
        outcome = self.api(
            "POST",
            f"/api/tasks/{task_id}/delegate",
            {"workspace": WORKSPACE, "expected_revision": revision},
        )
        chat_id = str(outcome["chat_id"])
        self._accepted_at = time.monotonic()
        self._chat_ids.append(chat_id)
        return chat_id

    def wait_settled(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        stable = 0
        while time.monotonic() < deadline:
            blockers = self.api("GET", "/api/admin/restart-blockers").get("restart_blockers", [])
            past_accept = time.monotonic() - self._accepted_at >= SETTLE_MIN_AFTER_ACCEPT_S
            stable = stable + 1 if past_accept and not blockers else 0
            if stable >= SETTLE_STABLE_POLLS:
                return True
            time.sleep(SETTLE_POLL_S)
        return False

    def stop_chat(self, chat_id: str) -> None:
        self.api("POST", f"/api/chats/{chat_id}/stop", {})

    def answer_text(self) -> str:
        return answer_from_transcripts(self.ws / ".runtime")

    def usage(self) -> dict[str, Any]:
        usage = collect_usage(self.ws / ".runtime")
        usage["chat_ids"] = sorted(set(usage["chat_ids"]) | set(self._chat_ids))
        return usage

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc is not None:
                _stop_process_group(self._proc)
        finally:
            if self._log is not None:
                self._log.close()
            if self._keep_workdir:
                print(f"kept task eval workdir: {self.workdir}", file=sys.stderr)
            else:
                shutil.rmtree(self.workdir, ignore_errors=True)


# --- usage and transcripts --------------------------------------------------------

_TOKEN_FIELDS: dict[str, str] = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _read_transcript(path: Path) -> list[dict[str, Any]]:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    turns = data.get("turns", []) if isinstance(data, dict) else []
    return [turn for turn in turns if isinstance(turn, dict)]


def _transcripts(runtime: Path) -> list[Path]:
    return sorted((runtime / "transcripts").glob("*/*.json"), key=lambda p: (p.parent.name, p.name))


def answer_from_transcripts(runtime: Path) -> str:
    """Every turn's response across every transcript, the most recently written chat last."""
    paths = sorted(_transcripts(runtime), key=lambda p: p.stat().st_mtime if p.exists() else 0.0)
    parts: list[str] = []
    for path in paths:
        for turn in _read_transcript(path):
            response = turn.get("response")
            if isinstance(response, str) and response:
                parts.append(response)
    return "\n".join(parts)


def collect_usage(runtime: Path) -> dict[str, Any]:
    """Token, tool, ciao-call and cost figures for one engine's runtime directory."""
    tokens = {name: 0 for name in _TOKEN_FIELDS}
    tool_calls = 0
    tool_errors = 0
    chat_ids: list[str] = []
    for path in _transcripts(runtime):
        chat_ids.append(path.parent.name)
        for turn in _read_transcript(path):
            usage = turn.get("usage") or {}
            if isinstance(usage, dict):
                for name, key in _TOKEN_FIELDS.items():
                    tokens[name] += _as_int(usage.get(key))
            for call in turn.get("tool_calls") or []:
                tool_calls += 1
                if isinstance(call, dict) and call.get("error"):
                    tool_errors += 1

    ciao_calls = 0
    ciao_errors = 0
    telemetry = runtime / "mcp_tool_calls.jsonl"
    if telemetry.is_file():
        for line in telemetry.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row: Any = json.loads(line)
            except ValueError:
                continue  # a line the engine is still writing
            ciao_calls += 1
            if isinstance(row, dict) and row.get("status") != "ok":
                ciao_errors += 1

    cost_usd: float | None = None
    state_path = runtime / "state.json"
    if state_path.is_file():
        state: Any = None
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = None  # still being written; the cost is unknown for this read
        bot_state = state.get("bot_state") if isinstance(state, dict) else None
        cost = bot_state.get("cost") if isinstance(bot_state, dict) else None
        cost_usd = float(cost) if isinstance(cost, (int, float)) else None

    return {
        "tokens": tokens,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "ciao_calls": ciao_calls,
        "ciao_errors": ciao_errors,
        "cost_usd": cost_usd,
        "chat_ids": chat_ids,
    }


# --- snapshot, setup, runner ------------------------------------------------------


def take_snapshot(agent_root: Path, api: Callable[..., Any], keep: Iterable[str]) -> Snapshot:
    """Workspace state after setup. ``keep`` names files whose bytes are saved for ``unchanged``."""
    tasks = api("GET", f"/api/tasks?workspace={WORKSPACE}").get("tasks", [])
    schedules = api("GET", "/api/schedules")
    projects = api("GET", "/api/projects")
    saved: dict[str, bytes] = {}
    for rel in keep:
        path = _resolve_in_root(agent_root, rel)
        if path.is_file():
            saved[rel] = path.read_bytes()
    return Snapshot(
        task_ids=frozenset(str(r["id"]) for r in tasks if r.get("id")),
        schedule_ids=frozenset(str(r["schedule_id"]) for r in schedules if r.get("schedule_id")),
        project_ids=frozenset(str(r["project_id"]) for r in projects if r.get("project_id")),
        files=frozenset(_list_files(agent_root)),
        saved=saved,
    )


def _apply_setup(api: Callable[..., Any], task: EvalTask) -> None:
    for step in task.setup:
        if step["kind"] == "task":
            api(
                "POST",
                "/api/tasks",
                {
                    "workspace": WORKSPACE,
                    "title": step["title"],
                    "body": step.get("body", ""),
                    "due": step.get("due"),
                },
            )
        else:
            api(
                "POST",
                "/api/projects",
                {"name": step["name"], "context": step.get("context", ""), "workspace": WORKSPACE},
            )


def _find_delegate_target(api: Callable[..., Any], title: str) -> tuple[str, str]:
    rows = api("GET", f"/api/tasks?workspace={WORKSPACE}").get("tasks", [])
    for row in rows:
        if row.get("title") == title:
            return str(row["id"]), str(row["revision"])
    raise LookupError(f"no task titled {title!r} to delegate")


def _unchanged_paths(task: EvalTask) -> list[str]:
    return [str(spec["path"]) for spec in task.checks if spec.get("kind") == "unchanged"]


def _tokens(raw: Mapping[str, Any]) -> dict[str, int]:
    tokens = {
        "input": _as_int(raw.get("input")),
        "output": _as_int(raw.get("output")),
        "cache_read": _as_int(raw.get("cache_read")),
        "cache_write": _as_int(raw.get("cache_write")),
    }
    tokens["context_total"] = tokens["input"] + tokens["cache_read"] + tokens["cache_write"]
    return tokens


def provider_version(provider: str) -> str | None:
    """First line of ``<provider> --version``, or None when the CLI is not runnable."""
    try:
        proc = subprocess.run(
            [provider, "--version"], capture_output=True, encoding="utf-8", errors="replace", timeout=15,
            check=False, env=_BASE_ENV
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (proc.stdout or proc.stderr or "").strip().splitlines()
    return lines[0] if proc.returncode == 0 and lines else None


def cli_package_mismatch() -> str | None:
    """Why the ``ciao`` an agent would run is not this harness's package, or None.

    The engine's sandbox only reads the package that is running it, so a
    ``ciao`` on the agent's PATH that imports a different checkout is refused.
    Isolated mode (``-I``) ignores PYTHONPATH, which is what the venv's ``ciao``
    script sees.
    """
    harness = Path(__file__).resolve().parent
    probe = [sys.executable, "-I", "-c", "import ciao; print(ciao.__file__)"]
    try:
        proc = subprocess.run(
            probe, capture_output=True, encoding="utf-8", errors="replace", timeout=60, check=False, env=_BASE_ENV
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not import ciao in isolated mode: {exc!r}"
    printed = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not printed:
        return f"could not import ciao in isolated mode (exit {proc.returncode}): {(proc.stderr or '')[-300:]}"
    installed = Path(printed[-1]).resolve().parent
    if installed == harness:
        return None
    return (
        f"CLI package mismatch: the `ciao` an agent runs imports {installed}, "
        f"but this harness runs {harness}. The engine sandbox only reads the running "
        "package, so the agent's ciao calls would be refused. Remedy: run the eval from "
        "an install of the checkout under test, e.g. `uv venv .venv-eval && uv pip install "
        "-p .venv-eval -e .` in that checkout, then `.venv-eval/bin/ciao eval tasks run ...`."
    )


def code_revision() -> str | None:
    """Short HEAD of the repository this package lives in, ``-dirty`` when the tree is not clean."""
    repo = Path(__file__).resolve().parents[1]

    def git(*args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["git", "-C", str(repo), *args], capture_output=True, encoding="utf-8", errors="replace",
                timeout=15, check=False, env=_BASE_ENV
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    head = git("rev-parse", "--short=12", "HEAD")
    if head is None or head.returncode != 0 or not head.stdout.strip():
        return None
    status = git("status", "--porcelain")
    dirty = status is not None and status.returncode == 0 and bool(status.stdout.strip())
    return f"{head.stdout.strip()}-dirty" if dirty else head.stdout.strip()


def _base_record(task: EvalTask, *, provider: str, model: str, label: str, repeat: int) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "label": label,
        "task_id": task.id,
        "tier": task.tier,
        "category": task.category,
        "provider": provider,
        "model": model,
        "repeat": repeat,
        "code_revision": code_revision(),
        "provider_version": provider_version(provider),
        "catalog_sha256": catalog_sha256(),
        "started_at": datetime.now(UTC).isoformat(),
        "status": "error",
        "skip_reason": None,
        "error": None,
        "checks": [],
        "answer": "",
        "tokens": _tokens({}),
        "tool_calls": 0,
        "tool_errors": 0,
        "ciao_calls": 0,
        "ciao_errors": 0,
        "cost_usd": None,
        "wall_s": 0.0,
        "timed_out": False,
        "chat_ids": [],
    }


def run_task(
    task: EvalTask,
    *,
    provider: str,
    model: str,
    label: str,
    repeat: int,
    session_factory: Callable[[EvalTask], EngineSession] | None = None,
    capabilities: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """One graded run of one task. Never raises: a failure becomes ``status="error"``.

    Without ``session_factory`` the run boots a real engine on ``provider``/``model``.
    """
    record = _base_record(task, provider=provider, model=model, label=label, repeat=repeat)
    missing = [name for name in task.requires if name not in capabilities]
    if missing:
        record["status"] = "skipped"
        record["skip_reason"] = f"missing capability: {', '.join(missing)}"
        return record

    factory = session_factory or partial(IsolatedEngine.boot, provider=provider, model=model)
    started = time.monotonic()
    session: EngineSession | None = None
    try:
        session = factory(task)
        _apply_setup(session.api, task)
        snapshot = take_snapshot(session.agent_root, session.api, _unchanged_paths(task))
        if task.entry["kind"] == "prompt":
            chat_id = session.start_prompt(task.prompt, provider=provider, model=model)
        else:
            task_id, revision = _find_delegate_target(session.api, str(task.entry["task_title"]))
            chat_id = session.delegate(task_id, revision, provider=provider, model=model)
        settled = session.wait_settled(task.timeout_s)
        if not settled:
            session.stop_chat(chat_id)
        ctx = GradeContext(
            agent_root=session.agent_root,
            answer=session.answer_text(),
            api=session.api,
            snapshot=snapshot,
            run_date=date.today(),
        )
        results = grade(task, ctx)
        usage = session.usage()
        record.update(
            status="passed" if settled and all(r.ok for r in results) else "failed",
            checks=[asdict(r) for r in results],
            answer=ctx.answer[-ANSWER_TAIL_CHARS:],
            tokens=_tokens(usage["tokens"]),
            tool_calls=_as_int(usage["tool_calls"]),
            tool_errors=_as_int(usage["tool_errors"]),
            ciao_calls=_as_int(usage["ciao_calls"]),
            ciao_errors=_as_int(usage["ciao_errors"]),
            cost_usd=usage["cost_usd"],
            timed_out=not settled,
            chat_ids=list(usage["chat_ids"]),
        )
    except Exception as exc:  # noqa: BLE001 - a run is recorded, never raised
        record.update(status="error", error=repr(exc)[:500])
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001 - cleanup failure is noted, not raised
                record["error"] = record["error"] or f"close failed: {exc!r}"[:500]
        record["wall_s"] = round(time.monotonic() - started, 3)
    return record


def run_suite(
    tasks: Sequence[EvalTask],
    *,
    provider: str,
    model: str,
    label: str,
    repeats: int = 3,
    jobs: int = 1,
    out: Path,
    session_factory: Callable[[EvalTask], EngineSession] | None = None,
    capabilities: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Run every task ``repeats`` times, appending each record to ``out`` as it finishes."""
    out.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    rows: list[dict[str, Any]] = []

    def one(task: EvalTask, repeat: int) -> None:
        row = run_task(
            task,
            provider=provider,
            model=model,
            label=label,
            repeat=repeat,
            session_factory=session_factory,
            capabilities=capabilities,
        )
        with lock:
            with out.open("a", encoding="utf-8", newline="") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            rows.append(row)

    work = [(task, repeat) for repeat in range(1, repeats + 1) for task in tasks]
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for future in [pool.submit(one, task, repeat) for task, repeat in work]:
            future.result()
    return rows


def default_report_path(label: str) -> Path:
    """``<runtime>/evals/tasks/<UTC stamp>-<label>.jsonl``, mirroring the behavioral report path."""
    runtime = Path(os.environ.get("CIAO_RUNTIME_ROOT", ".runtime"))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(ch for ch in label if ch.isalnum() or ch in "-_") or "run"
    return runtime / "evals" / "tasks" / f"{stamp}-{safe}.jsonl"


def read_jsonl(paths: Sequence[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row: dict[str, Any] = json.loads(line)
                rows.append(row)
    return rows


# --- summary and comparison -------------------------------------------------------


def _median(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return statistics.median(present) if present else None


def summarize(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per (label, provider, model, code revision, task): counts, pass rate and medians."""
    groups: dict[tuple[str, str, str, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("label")),
            str(row.get("provider")),
            str(row.get("model")),
            str(row.get("code_revision")),
            str(row.get("task_id")),
        )
        groups.setdefault(key, []).append(row)

    summary: list[dict[str, Any]] = []
    for key in sorted(groups):
        label, provider, model, revision, task_id = key
        items = groups[key]
        ran = [r for r in items if r.get("status") != "skipped"]
        passed = sum(1 for r in ran if r.get("status") == "passed")
        summary.append(
            {
                "label": label,
                "provider": provider,
                "model": model,
                "code_revision": revision,
                "task_id": task_id,
                "n": len(items),
                "ran": len(ran),
                "skipped": len(items) - len(ran),
                "pass_rate": passed / len(ran) if ran else None,
                "median_context_total": _median([r["tokens"]["context_total"] for r in ran]),
                "median_output": _median([r["tokens"]["output"] for r in ran]),
                "median_wall_s": _median([r.get("wall_s") for r in ran]),
                "median_cost_usd": _median([r.get("cost_usd") for r in ran]),
                "tool_errors": sum(int(r.get("tool_errors") or 0) for r in items),
                "timeouts": sum(1 for r in items if r.get("timed_out")),
            }
        )
    return summary


def _index_by_task(summary: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in summary:
        index.setdefault(str(item["task_id"]), item)
    return index


def _delta(new: float | None, old: float | None) -> float | None:
    return None if new is None or old is None else new - old


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / old * 100.0


def compare(baseline: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per task: the baseline and candidate summaries and their deltas.

    Each side should hold one group per task; if it holds several, the first is used.
    """
    base = _index_by_task(summarize(baseline))
    cand = _index_by_task(summarize(candidate))
    comparison: list[dict[str, Any]] = []
    for task_id in sorted(set(base) | set(cand)):
        b = base.get(task_id)
        c = cand.get(task_id)
        b_ctx = b["median_context_total"] if b else None
        c_ctx = c["median_context_total"] if c else None
        b_wall = b["median_wall_s"] if b else None
        c_wall = c["median_wall_s"] if c else None
        b_cost = b["median_cost_usd"] if b else None
        c_cost = c["median_cost_usd"] if c else None
        comparison.append(
            {
                "task_id": task_id,
                "baseline": b,
                "candidate": c,
                "delta": {
                    "pass_rate": _delta(c["pass_rate"] if c else None, b["pass_rate"] if b else None),
                    "context_total_pct": _pct(c_ctx, b_ctx),
                    "wall_s_pct": _pct(c_wall, b_wall),
                    "cost_pct": _pct(c_cost, b_cost),
                },
            }
        )
    return comparison


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_summary(summary: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "| Task | Label | Provider | Model | Revision | n | Ran | Pass rate | Median context | Median output | Median wall s | Median cost USD | Tool errors | Timeouts |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summary:
        pass_rate = item["pass_rate"]
        lines.append(
            "| {task} | {label} | {provider} | {model} | {rev} | {n} | {ran} | {pass_rate} | "
            "{ctx} | {out} | {wall} | {cost} | {terr} | {timeouts} |".format(
                task=item["task_id"],
                label=item["label"],
                provider=item["provider"],
                model=item["model"],
                rev=item["code_revision"],
                n=item["n"],
                ran=item["ran"],
                pass_rate="-" if pass_rate is None else f"{pass_rate:.2f}",
                ctx=_fmt(item["median_context_total"], 0),
                out=_fmt(item["median_output"], 0),
                wall=_fmt(item["median_wall_s"], 1),
                cost=_fmt(item["median_cost_usd"], 4),
                terr=item["tool_errors"],
                timeouts=item["timeouts"],
            )
        )
    return "\n".join(lines)


def render_comparison(comparison: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "| Task | Baseline pass | Candidate pass | Delta pass | Delta context % | Delta wall % | Delta cost % |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in comparison:
        base = item["baseline"] or {}
        cand = item["candidate"] or {}
        delta = item["delta"]
        lines.append(
            "| {task} | {bp} | {cp} | {dp} | {dc} | {dw} | {dco} |".format(
                task=item["task_id"],
                bp=_fmt(base.get("pass_rate")),
                cp=_fmt(cand.get("pass_rate")),
                dp=_fmt(delta["pass_rate"]),
                dc=_fmt(delta["context_total_pct"], 1),
                dw=_fmt(delta["wall_s_pct"], 1),
                dco=_fmt(delta["cost_pct"], 1),
            )
        )
    return "\n".join(lines)
