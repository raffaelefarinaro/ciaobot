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

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
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
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from functools import partial
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

import yaml

import ciao
from ciao import agent_paths, behavioral_eval, gws_auth
from ciao.config import PERMISSION_MODES
from ciao.os_support.tool_path import dedupe_path
from ciao.tool_path import common_tool_dirs

TASKS_ROOT: Path = Path(__file__).resolve().parent / "stock" / "evals" / "tasks"
FIXTURE_DIRNAME = "_fixture"
WORKSPACE = "personal"
# Prefix of every engine workdir; also the marker that lets cleanup touch a Claude project folder.
EVAL_WORKDIR_PREFIX = "ciao-task-eval-"
TIERS: frozenset[str] = frozenset({"easy", "medium", "hard"})
ENTRY_KINDS: frozenset[str] = frozenset({"prompt", "delegate", "transcript"})
# The memory pass a transcript entry triggers writes its queue under the agent root.
PROPOSALS_PATH = "memory-vault/Workspace/Memory-Proposals.md"
MEMORY_PASS_TERMINAL: frozenset[str] = frozenset({"ok", "attention"})
MEMORY_PASS_POLL_S = 2.0
MEMORY_PASS_ARCHIVE_WAIT_S = 30.0
MAX_TRANSCRIPT_TURNS = 20
SETUP_KINDS: frozenset[str] = frozenset({"task", "project"})
SCHEMA = "ciao-task-eval/1"
DEFAULT_TIMEOUT_S = 600.0
ANSWER_TAIL_CHARS = 2000
# The permission mode a run's chats start in, per provider, unless the caller
# asks for another (`--mode`). Mirrors a typical operator install. OpenCode's
# `auto` puts every shell call behind an approval card that nobody answers in an
# eval, so the run hangs to its timeout with zero usage (#2439).
DEFAULT_MODES: dict[str, str] = {"claude": "auto", "opencode": "bypass"}

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


GWS_FAKE_CAPABILITY = "gws_fake"
GWS_FAKE_VERSION = "0.22.5"
GWS_FAKE_DIRNAME = "eval-gws"
GWS_FAKE_MAILBOX_NAME = "mailbox.json"
GWS_FAKE_LOG_NAME = "calls.jsonl"
_GWS_MESSAGE_STR_KEYS: tuple[str, ...] = ("id", "threadId", "from", "to", "subject", "date", "rfc822_id", "body")


@dataclass(frozen=True)
class GwsFakeSpec:
    """The mailbox a task's fake gws serves, and the profile it links."""

    profile: str
    account: str
    messages: list[dict[str, Any]]


@dataclass(frozen=True)
class GwsFakeInstall:
    profile: str
    bin_dir: Path
    launcher: Path
    log: Path
    env: dict[str, str]


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
    gws_fake: GwsFakeSpec | None = None


@dataclass(frozen=True)
class CheckResult:
    kind: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class MemoryPassOutcome:
    """How a transcript entry's end-of-chat memory pass ended, and what the pass itself used."""

    status: str  # "ok" | "attention" | "timeout" | "not_queued"
    chat_id: str | None
    tokens: dict[str, int]  # raw input / output / cache_read / cache_write
    response: str  # the pass's final reply, "" when unknown


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
    gws_call_log: Path | None = None


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
    if kind == "gws_calls":
        _validate_gws_calls(task_id, spec)
    elif kind == "proposals":
        _validate_proposals(task_id, spec)
    return spec


def _validate_regex_lists(task_id: str, kind: str, spec: dict[str, Any]) -> None:
    for key in ("must", "must_not"):
        patterns = spec.get(key, [])
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise TaskCatalogError(f"{task_id}: {kind} {key} must be a list of regex strings")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise TaskCatalogError(f"{task_id}: {kind} {key} regex {pattern!r}: {exc}") from exc


def _validate_gws_calls(task_id: str, spec: dict[str, Any]) -> None:
    _validate_regex_lists(task_id, "gws_calls", spec)
    minimum = spec.get("min_calls", 0)
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
        raise TaskCatalogError(f"{task_id}: gws_calls min_calls must be a non-negative integer")


def _validate_proposals(task_id: str, spec: dict[str, Any]) -> None:
    _validate_regex_lists(task_id, "proposals", spec)
    if not spec.get("must") and not spec.get("must_not") and "max_new" not in spec:
        raise TaskCatalogError(f"{task_id}: proposals names nothing to check (must, must_not or max_new)")
    if "include_files" in spec:
        files = spec["include_files"]
        if not isinstance(files, list) or not all(isinstance(f, str) and f.strip() for f in files):
            raise TaskCatalogError(f"{task_id}: proposals include_files must be a list of non-empty paths")
    if "max_new" in spec:
        cap = spec["max_new"]
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
            raise TaskCatalogError(f"{task_id}: proposals max_new must be a non-negative integer")
    if "path" in spec:
        _require_text(spec["path"], f"{task_id}: proposals path")


def _load_gws_fake(task_id: str, body: dict[str, Any], requires: list[str]) -> GwsFakeSpec | None:
    raw = body.get("gws_fake")
    wanted = GWS_FAKE_CAPABILITY in requires
    if raw is None:
        if wanted:
            raise TaskCatalogError(f"{task_id}: requires gws_fake but has no gws_fake block")
        return None
    if not wanted:
        raise TaskCatalogError(f"{task_id}: a gws_fake block needs \"requires\": [\"gws_fake\"]")
    if not isinstance(raw, dict):
        raise TaskCatalogError(f"{task_id}: gws_fake must be an object")
    profile = _require_text(raw.get("profile"), f"{task_id}: gws_fake.profile")
    if profile != gws_auth.slugify_profile(profile):
        raise TaskCatalogError(f"{task_id}: gws_fake.profile must be a profile slug, got {profile!r}")
    account = _require_text(raw.get("account"), f"{task_id}: gws_fake.account")
    messages = raw.get("messages")
    if not isinstance(messages, list) or not messages:
        raise TaskCatalogError(f"{task_id}: gws_fake.messages must be a non-empty list")
    seen: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            raise TaskCatalogError(f"{task_id}: each gws_fake message must be an object")
        for key in _GWS_MESSAGE_STR_KEYS:
            if not isinstance(message.get(key), str):
                raise TaskCatalogError(f"{task_id}: gws_fake message {key} must be a string")
        label_ids = message.get("labelIds")
        if not isinstance(label_ids, list) or not all(isinstance(label, str) for label in label_ids):
            raise TaskCatalogError(f"{task_id}: gws_fake message labelIds must be a list of strings")
        try:
            parsedate_to_datetime(message["date"])
        except (TypeError, ValueError) as exc:
            raise TaskCatalogError(f"{task_id}: gws_fake message date is not RFC 2822: {exc}") from exc
        if message["id"] in seen:
            raise TaskCatalogError(f"{task_id}: duplicate gws_fake message id {message['id']!r}")
        seen.add(message["id"])
    return GwsFakeSpec(profile=profile, account=account, messages=[dict(m) for m in messages])


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


def _validate_turns(task_id: str, turns: Any) -> None:
    """A transcript entry's turns: 1 to MAX_TRANSCRIPT_TURNS objects, each with a user line and an assistant string."""
    if not isinstance(turns, list) or not turns or len(turns) > MAX_TRANSCRIPT_TURNS:
        raise TaskCatalogError(
            f"{task_id}: entry.turns must be a list of 1 to {MAX_TRANSCRIPT_TURNS} turns"
        )
    for index, turn in enumerate(turns):
        if not isinstance(turn, dict):
            raise TaskCatalogError(f"{task_id}: entry.turns[{index}] must be an object")
        _require_text(turn.get("user"), f"{task_id}: entry.turns[{index}].user")
        if not isinstance(turn.get("assistant"), str):
            raise TaskCatalogError(f"{task_id}: entry.turns[{index}].assistant must be a string")


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
    elif entry["kind"] == "delegate":
        _require_text(entry.get("task_title"), f"{task_id}: entry.task_title")
    else:
        _validate_turns(task_id, entry.get("turns"))
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
        gws_fake=_load_gws_fake(task_id, body, requires),
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


def _check_gws_calls(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    if ctx.gws_call_log is None:
        return _fail("gws_calls", "no gws fake in this run")
    summaries = [str(row.get("summary") or "") for row in read_gws_calls(ctx.gws_call_log)]
    problems: list[str] = []
    for pattern in spec.get("must", []):
        if not any(re.search(pattern, s, re.IGNORECASE) for s in summaries):
            problems.append(f"no call matches {pattern!r}")
    for pattern in spec.get("must_not", []):
        if any(re.search(pattern, s, re.IGNORECASE) for s in summaries):
            problems.append(f"a call matches {pattern!r}")
    minimum = int(spec.get("min_calls", 0))
    if len(summaries) < minimum:
        problems.append(f"{len(summaries)} call(s), want at least {minimum}")
    if problems:
        return CheckResult("gws_calls", False, "; ".join(problems) + " | " + "; ".join(summaries)[-400:])
    return CheckResult("gws_calls", True, f"{len(summaries)} call(s) ok")


def _queue_items(text: str) -> list[tuple[str, str]]:
    """``(kind, text)`` of every queue bullet, read with the grammar ``list_proposals`` uses."""
    from ciao.proposal_kinds import parse_bullet

    items: list[tuple[str, str]] = []
    for line in text.splitlines():
        bullet = parse_bullet(line)
        if bullet is not None:
            items.append((bullet.kind, bullet.text))
    return items


def _check_proposals(ctx: GradeContext, spec: dict[str, Any]) -> CheckResult:
    """Grade what a memory pass added: new queue bullets, and lines added to ``include_files``.

    Bullets already in the queue at the snapshot do not count, so a pass that
    leaves an old row alone is not credited with it. ``must`` / ``must_not``
    are case-insensitive regexes; ``max_new`` caps only the new queue bullets.
    """
    rel = str(spec.get("path", PROPOSALS_PATH))
    saved = ctx.snapshot.saved
    before = {text for _, text in _queue_items(saved.get(rel, b"").decode("utf-8", "replace"))}
    queue = _resolve_in_root(ctx.agent_root, rel)
    queue_text = queue.read_text(encoding="utf-8", errors="replace") if queue.is_file() else ""
    new = [(kind, text) for kind, text in _queue_items(queue_text) if text not in before]
    items = [f"[{kind}] {text}" for kind, text in new]
    for extra in spec.get("include_files", []):
        target = _resolve_in_root(ctx.agent_root, str(extra))
        old_lines = {
            line.strip() for line in saved.get(str(extra), b"").decode("utf-8", "replace").splitlines()
        }
        if target.is_file():
            for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if stripped and stripped not in old_lines:
                    items.append(stripped)
    problems: list[str] = []
    for pattern in spec.get("must", []):
        if not any(re.search(pattern, item, re.IGNORECASE) for item in items):
            problems.append(f"nothing new matches {pattern!r}")
    for pattern in spec.get("must_not", []):
        hit = next((item for item in items if re.search(pattern, item, re.IGNORECASE)), None)
        if hit is not None:
            problems.append(f"{pattern!r} matches {hit[:120]!r}")
    if "max_new" in spec and len(new) > int(spec["max_new"]):
        problems.append(f"{len(new)} new proposal(s), max {int(spec['max_new'])}")
    summary = "; ".join(problems) if problems else f"{len(new)} new proposal(s) ok"
    return CheckResult("proposals", not problems, f"{summary} | {' / '.join(items)}"[:400])


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
    "gws_calls": _check_gws_calls,
    "proposals": _check_proposals,
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
    storage roots are pointed into the throwaway ``workdir``. The queue-lock
    directory is left at its per-user temp default: the lock key hashes the
    (throwaway) vault path, so it cannot collide with the live install, and the
    agent's sandbox can write there but not into ``workdir``.
    """
    env = _without_live_vars(base_env)
    env.update(
        {
            "CIAO_WORKSPACE": str(ws),
            "CIAO_NO_BROWSER": "1",
            "CIAO_MEMORY_DIR": str(workdir / "dotciao"),
            "CIAO_LAUNCH_AGENTS_DIR": str(workdir / "LA"),
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


def _requested_mode(provider: str, mode: str | None) -> str:
    return mode or DEFAULT_MODES.get(provider, "auto")


def resolve_mode(provider: str, mode: str | None = None) -> str:
    """The permission mode a run uses: ``mode`` when given, else the provider's default.

    An unknown mode raises ``ValueError``; the engine's closed vocabulary is
    ``PERMISSION_MODES`` (the same set ``app_settings`` validates against).
    """
    resolved = _requested_mode(provider, mode)
    if resolved not in PERMISSION_MODES:
        raise ValueError(
            f"unknown permission mode {resolved!r}; expected one of {', '.join(PERMISSION_MODES)}"
        )
    return resolved


def _pin_default_mode(settings: Path, provider: str, mode: str) -> None:
    """Write ``provider_default_modes[provider]`` into the workspace's app settings.

    ``AppSettingsStore`` reads this nested map at engine boot and applies it to
    new chats, delegated chats included. Other keys in the file are kept.
    """
    raw: Any = {}
    if settings.is_file():
        raw = json.loads(settings.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise RuntimeError(f"app settings is not a JSON object: {settings}")
    modes = raw.get("provider_default_modes")
    pinned: dict[str, str] = dict(modes) if isinstance(modes, dict) else {}
    pinned[provider] = mode
    raw["provider_default_modes"] = pinned
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8", newline="")


def prepare_workspace(
    workdir: Path,
    provider: str,
    model: str,
    task: EvalTask,
    *,
    auth_token: str | None = None,
    port: int | None = None,
    mode: str | None = None,
) -> Path:
    """Build the throwaway workspace under ``workdir/ws`` without booting anything.

    ``setup_workspace`` runs with the live environment scrubbed and the working
    directory set to ``workdir``. The fixture vault is copied into
    ``ws/personal/memory-vault`` and the task overlay over ``ws/personal``. The
    workspace defaults to ``provider``, pins ``model`` for it, and starts its
    chats in ``mode`` (see :func:`resolve_mode`).
    """
    from ciao import cli

    resolved = resolve_mode(provider, mode)
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
    _pin_default_mode(ws / ".runtime" / "app_settings.json", provider, resolved)
    personal = ws / WORKSPACE
    shutil.copytree(TASKS_ROOT / FIXTURE_DIRNAME, personal / "memory-vault", dirs_exist_ok=True)
    if task.overlay is not None:
        shutil.copytree(task.overlay, personal, dirs_exist_ok=True)
    if task.gws_fake is not None:
        # The files only. IsolatedEngine._start builds the environment that points at them.
        install_gws_fake(ws, task.gws_fake, base_path=_BASE_ENV.get("PATH", ""))
    return ws


def _stop_process_group(proc: subprocess.Popen[bytes]) -> None:
    """Stop an engine and whatever it spawned (provider CLIs), on POSIX via its process group.

    SIGTERM goes to the whole group while the engine is still alive, so its
    process-group id is reserved and the signal cannot reach an unrelated group.
    The final SIGKILL for stragglers runs right after the reap; a reused group
    id would need the kernel to hand out the same pid and that process to call
    setsid in between, which sequential pid allocation makes impractical. An
    engine some other path already reaped is not signalled at all.
    """
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        proc.terminate()
        try:
            proc.wait(timeout=ENGINE_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=ENGINE_STOP_TIMEOUT_S)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=ENGINE_STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=ENGINE_STOP_TIMEOUT_S)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


class EngineSession(Protocol):
    """What a task run needs from an engine: API access, a chat, and its usage."""

    agent_root: Path
    gws_call_log: Path | None

    def api(self, method: str, path: str, body: Any = None) -> Any: ...

    def start_prompt(self, prompt: str, *, provider: str, model: str) -> str: ...

    def start_transcript(self, turns: list[dict[str, str]], *, provider: str, model: str) -> str: ...

    def run_memory_pass(self, chat_id: str, timeout_s: float) -> MemoryPassOutcome: ...

    def delegate(self, task_id: str, revision: str, *, provider: str, model: str) -> str: ...

    def wait_settled(self, timeout_s: float) -> bool: ...

    def stop_chat(self, chat_id: str) -> None: ...

    def answer_text(self) -> str: ...

    def usage(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


def _claude_project_dirs(agent_root: Path) -> list[Path]:
    """Claude Code's project folders for an eval agent root, as they exist now.

    Claude names the folder after the path it was started in, and on macOS that is
    the resolved path (``/var`` becomes ``/private/var``), so both spellings are
    candidates. Only a folder whose name carries the eval workdir prefix qualifies,
    so a misconfigured root can never remove a real project's history: the root
    itself must sit under a folder the harness created (its name starts with the
    prefix), and the slug must still carry the prefix (a 200-character slug that
    Claude truncated past it is left in place).
    """
    if not any(part.startswith(EVAL_WORKDIR_PREFIX) for part in agent_root.parts):
        return []
    candidates = {
        agent_paths.claude_projects_dir(agent_root),
        agent_paths.claude_projects_dir(agent_root.resolve()),
    }
    return sorted(
        folder for folder in candidates if folder.is_dir() and EVAL_WORKDIR_PREFIX in folder.name
    )


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
        self.gws_call_log: Path | None = None
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
        self._mode = "auto"
        self._chat_ids: list[str] = []
        self._closed = False

    @classmethod
    def boot(
        cls,
        task: EvalTask,
        *,
        provider: str,
        model: str,
        keep_workdir: bool = False,
        mode: str | None = None,
    ) -> "IsolatedEngine":
        workdir = Path(tempfile.mkdtemp(prefix=EVAL_WORKDIR_PREFIX))
        engine = cls(workdir, keep_workdir=keep_workdir)
        try:
            engine._start(task, provider, model, mode)
        except BaseException:
            engine.close()
            raise
        return engine

    def _start(self, task: EvalTask, provider: str, model: str, mode: str | None) -> None:
        prepare_workspace(
            self.workdir,
            provider,
            model,
            task,
            auth_token=self.token,
            port=self.port,
            mode=mode,
        )
        self._mode = resolve_mode(provider, mode)
        self._log = self._log_path.open("wb")
        script = Path(sys.executable).with_name("ciao")
        command = [str(script), "run"] if script.exists() else [sys.executable, "-m", "ciao", "run"]
        group: dict[str, Any] = {"start_new_session": True} if sys.platform != "win32" else {}
        # engine_env drops every CIAO_* variable first; the fake's variables go in after it.
        env = engine_env(_BASE_ENV, self.workdir, self.ws)
        if task.gws_fake is not None:
            bin_dir = gws_fake_bin_dir(self.ws, task.gws_fake.profile)
            env.update(gws_fake_environment(bin_dir, base_path=env.get("PATH", "")))
            self.gws_call_log = bin_dir / GWS_FAKE_LOG_NAME
        self._proc = subprocess.Popen(
            command,
            cwd=self.ws,
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            **group,
        )
        self._wait_ready()
        self.api("POST", "/api/auth", {"token": self.token})
        if task.gws_fake is not None:
            # Also resyncs the gws skills before it returns.
            self.api("PATCH", f"/api/workspaces/{WORKSPACE}", {"gws_profile": task.gws_fake.profile})
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

    def start_transcript(self, turns: list[dict[str, str]], *, provider: str, model: str) -> str:
        """An empty chat whose runtime transcript holds ``turns``, ready for the archive route."""
        chat = self.api(
            "POST",
            f"/api/projects/{self._host_project_id}/chats",
            {"title": "Task eval", "provider": provider, "model": model},
        )
        chat_id = str(chat["chat_id"])
        path = transcript_path(self.ws, chat_id, provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = synthetic_transcript(
            chat_id, turns, provider=provider, model=model, mode=self._mode, title="Task eval"
        )
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8", newline="")
        self._chat_ids.append(chat_id)
        return chat_id

    def run_memory_pass(self, chat_id: str, timeout_s: float) -> MemoryPassOutcome:
        """Archive the chat through the operator route and wait for the pass it queues to settle."""
        reply = self.api("POST", f"/api/chats/{chat_id}/archive", {})
        step = _memory_pass_step((reply or {}).get("postprocess"))
        extra = step.get("extra") or {}
        pass_id = str(extra.get("chat_id") or "")
        if not pass_id:
            return MemoryPassOutcome("not_queued", None, {name: 0 for name in _TOKEN_FIELDS}, "")
        deadline = time.monotonic() + timeout_s
        status = ""
        while True:
            row = self.api("GET", f"/api/chats/{chat_id}") or {}
            status = str(_memory_pass_step(row.get("postprocess")).get("status") or "")
            if status in MEMORY_PASS_TERMINAL or time.monotonic() >= deadline:
                break
            time.sleep(MEMORY_PASS_POLL_S)
        if status not in MEMORY_PASS_TERMINAL:
            self.stop_chat(pass_id)
            status = "timeout"
        archive_path = self._pass_archive_path(pass_id) if status == "ok" else None
        tokens, response = memory_pass_usage(self.ws, pass_id, archive_path)
        self._chat_ids.append(pass_id)
        return MemoryPassOutcome(status, pass_id, tokens, response)

    def _pass_archive_path(self, pass_id: str) -> str | None:
        """The pass's archive path once it has archived. The source step turns ``ok`` before the pass archives."""
        deadline = time.monotonic() + MEMORY_PASS_ARCHIVE_WAIT_S
        while True:
            row = self.api("GET", f"/api/chats/{pass_id}") or {}
            if row.get("archived") and row.get("archive_path"):
                return str(row["archive_path"])
            if time.monotonic() >= deadline:
                return None
            time.sleep(MEMORY_PASS_POLL_S)

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
                claude_dirs = _claude_project_dirs(self.agent_root)
                shutil.rmtree(self.workdir, ignore_errors=True)
                for folder in claude_dirs:
                    shutil.rmtree(folder, ignore_errors=True)


# --- gws fake: a stand-in ``gws`` that logs every call and never reaches Google -----


def available_capabilities() -> frozenset[str]:
    """Capabilities this platform's harness can provide. The fake is a POSIX shell launcher."""
    return frozenset({GWS_FAKE_CAPABILITY}) if sys.platform != "win32" else frozenset()


def gws_fake_bin_dir(ws: Path, profile: str) -> Path:
    """The fake's folder, inside the profile's credential directory (the sandbox grants it)."""
    return ws / "secrets" / f"gws-{profile}" / GWS_FAKE_DIRNAME


def gws_fake_environment(bin_dir: Path, *, base_path: str) -> dict[str, str]:
    """The variables that make ``ciao gws`` and bare ``gws`` reach the fake first."""
    path = dedupe_path(os.pathsep.join([str(bin_dir), base_path, *common_tool_dirs()]))
    return {gws_auth.GWS_BIN_ENV: str(bin_dir / "gws"), "PATH": path}


def _write_json_file(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="")


def _launcher_script(mailbox: Path, log: Path) -> str:
    """``/bin/sh`` launcher: runs ``gws_fake_main`` under the package this harness imports."""
    package_parent = Path(ciao.__file__).resolve().parent.parent
    code = "import sys; from ciao.task_eval import gws_fake_main; raise SystemExit(gws_fake_main(sys.argv[1:]))"
    return (
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(str(package_parent))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        "export PYTHONPATH\n"
        f"exec {shlex.quote(sys.executable)} -c {shlex.quote(code)} "
        f"{shlex.quote(str(mailbox))} {shlex.quote(str(log))} \"$@\"\n"
    )


def install_gws_fake(ws: Path, spec: GwsFakeSpec, *, base_path: str) -> GwsFakeInstall:
    """Write the fake gws and the state that links ``ws`` to ``spec.profile``. POSIX only.

    The credential files are dummies. The profile registry and a fresh health
    reading make the profile look connected; the mailbox and an empty call log
    sit beside the launcher.
    """
    cred = ws / "secrets" / f"gws-{spec.profile}"
    cred.mkdir(parents=True, exist_ok=True)
    client_id = f"{spec.profile}.apps.googleusercontent.com"
    _write_json_file(
        cred / "client_secret.json",
        {
            "installed": {
                "client_id": client_id,
                "client_secret": "eval-not-a-secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
    )
    _write_json_file(
        cred / "credentials.json",
        {
            "type": "authorized_user",
            "client_id": client_id,
            "client_secret": "eval-not-a-secret",
            "refresh_token": "eval-not-a-token",
        },
    )
    runtime = ws / ".runtime"
    runtime.mkdir(exist_ok=True)
    gws_auth.save_profile_registry(
        SimpleNamespace(state_path=runtime / "state.json"),
        [{"name": spec.profile, "label": "Eval sandbox"}],
    )
    _write_json_file(
        runtime / gws_auth.HEALTH_CACHE_NAME,
        {
            "profiles": {
                spec.profile: {
                    "token_valid": True,
                    "token_error": "",
                    "has_refresh_token": True,
                    "checked_at": time.time(),
                    "notified_invalid": False,
                    "consecutive_invalid": 0,
                }
            }
        },
    )
    bin_dir = gws_fake_bin_dir(ws, spec.profile)
    bin_dir.mkdir(parents=True, exist_ok=True)
    mailbox = bin_dir / GWS_FAKE_MAILBOX_NAME
    _write_json_file(mailbox, {"account": spec.account, "messages": spec.messages})
    log = bin_dir / GWS_FAKE_LOG_NAME
    log.write_text("", encoding="utf-8", newline="")
    launcher = bin_dir / "gws"
    launcher.write_text(_launcher_script(mailbox, log), encoding="utf-8", newline="")
    launcher.chmod(0o755)
    return GwsFakeInstall(
        profile=spec.profile,
        bin_dir=bin_dir,
        launcher=launcher,
        log=log,
        env=gws_fake_environment(bin_dir, base_path=base_path),
    )


def read_gws_calls(log: Path) -> list[dict[str, Any]]:
    """Every row of a fake's call log. Blank and unparseable lines are skipped."""
    try:
        text = log.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row: Any = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# The fake's own code. Each call runs gws_fake_main inside the agent's sandbox.
# The launcher imports ciao.task_eval to reach it, which loads this module's
# top-level imports (yaml and the ciao helpers above) before gws_fake_main runs.
# gws_fake_main itself uses only the standard library and never imports ciao.cli.

_FAKE_BOOL_FLAGS: frozenset[str] = frozenset(
    {"draft", "dry-run", "html", "headers", "labels", "page-all", "help", "version"}
)
# Flags that always take the next token, even one that starts with "-" (a body can).
# Any other flag takes the next token only when that token is not itself a flag.
_FAKE_VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "query", "max", "format", "id", "message-id", "body", "to", "cc", "bcc",
        "params", "json", "attach", "subject", "page-token",
    }
)
_QUERY_TOKEN_RE = re.compile(r'(?:(\w+):\(([^)]*)\))|(?:(\w+):"([^"]*)")|"([^"]*)"|(\S+)')
_FAKE_HELP = "gws 0.22.5 (ciaobot eval fake)\nusage: gws <service> <resource> <method> [flags]\n"
_FAKE_MODIFY_METHODS: frozenset[str] = frozenset(
    {"trash", "untrash", "delete", "modify", "batchModify", "insert", "import"}
)
_FAKE_LABELS: tuple[str, ...] = ("INBOX", "UNREAD", "SENT", "DRAFT")
_FAKE_SUMMARY_BODY_CHARS = 2000


@dataclass(frozen=True)
class _FakeReply:
    effect: str
    stdout: str = ""
    stderr: str = ""
    code: int = 0
    thread_id: str | None = None
    message_id: str | None = None
    to: str | None = None
    body: str | None = None


def gws_fake_main(argv: list[str]) -> int:
    """The fake gws. ``argv`` is ``[mailbox_json, log_jsonl, *gws_args]``. Never raises.

    Every call appends one JSON row to the log before it is answered.
    """
    if len(argv) < 2:
        sys.stderr.write(json.dumps({"error": {"code": 2, "message": "gws fake needs MAILBOX LOG first"}}) + "\n")
        return 2
    mailbox, log = Path(argv[0]), Path(argv[1])
    args = argv[2:]
    flags, words = _fake_parse(args)
    try:
        prior = read_gws_calls(log)
        reply = _fake_run(words, flags, len(prior) + 1, prior, mailbox)
        row = {
            "ts": time.time(),
            "argv": args,
            "op": " ".join(words) or "-",
            "effect": reply.effect,
            "thread_id": reply.thread_id,
            "message_id": reply.message_id,
            "to": reply.to,
            "body": reply.body,
            "summary": (
                f"op={' '.join(words) or '-'} effect={reply.effect} thread={reply.thread_id or '-'} "
                f"message={reply.message_id or '-'} to={reply.to or '-'} "
                f"body={_fake_one_line(reply.body)[:_FAKE_SUMMARY_BODY_CHARS] or '-'}"
            ),
        }
        with log.open("a", encoding="utf-8", newline="") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        sys.stderr.write(json.dumps({"error": {"code": 2, "message": f"gws fake cannot log: {exc}"}}) + "\n")
        return 2
    sys.stdout.write(reply.stdout)
    sys.stderr.write(reply.stderr)
    return reply.code


def _fake_one_line(text: str | None) -> str:
    return " ".join(text.split()) if text else ""


def _fake_parse(args: list[str]) -> tuple[dict[str, str], list[str]]:
    """Flags (``--x v``, ``--x=v``, boolean ``--x``, ``-a v``, ``-h``) and the positional words."""
    flags: dict[str, str] = {}
    words: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        next_token = args[index + 1] if index + 1 < len(args) else None
        if token in ("-h", "--help"):
            flags["help"] = "true"
        elif token.startswith("--"):
            name, sep, value = token[2:].partition("=")
            if sep:
                flags[name] = value
            elif name in _FAKE_BOOL_FLAGS:
                flags[name] = "true"
            elif name in _FAKE_VALUE_FLAGS or (next_token is not None and not next_token.startswith("-")):
                flags[name] = next_token or ""
                index += 1
            else:
                flags[name] = ""
        elif token == "-a":
            flags["attach"] = next_token or ""
            index += 1
        else:
            words.append(token)
        index += 1
    return flags, words


def _fake_run(
    words: list[str], flags: dict[str, str], n: int, prior: list[dict[str, Any]], mailbox: Path
) -> _FakeReply:
    try:
        account, messages = _fake_load_mailbox(mailbox)
        return _fake_dispatch(words, flags, n, prior, account, messages)
    except Exception as exc:  # noqa: BLE001 - the fake answers every call; it never raises
        return _fake_error(2, 2, f"gws fake failed: {exc}"[:300], "unknown")


def _fake_error(exit_code: int, error_code: int, message: str, effect: str) -> _FakeReply:
    payload = {"error": {"code": error_code, "message": message}}
    return _FakeReply(effect=effect, stderr=json.dumps(payload) + "\n", code=exit_code)


def _fake_load_mailbox(path: Path) -> tuple[str, list[dict[str, Any]]]:
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list):
        raise ValueError("mailbox must be an object with a messages list")
    messages = [dict(m) for m in raw["messages"] if isinstance(m, dict)]
    return str(raw.get("account", "")), messages


def _fake_json_flag(flags: dict[str, str], name: str) -> dict[str, Any]:
    raw = flags.get(name, "")
    if not raw:
        return {}
    value: Any = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"--{name} must be a JSON object")
    return value


def _fake_dispatch(
    words: list[str],
    flags: dict[str, str],
    n: int,
    prior: list[dict[str, Any]],
    account: str,
    messages: list[dict[str, Any]],
) -> _FakeReply:
    if "version" in flags:
        return _FakeReply(effect="read", stdout=f"gws {GWS_FAKE_VERSION}\n")
    if "help" in flags:
        return _FakeReply(effect="read", stdout=_FAKE_HELP)
    if words[:2] == ["auth", "status"]:
        status = {"token_valid": True, "has_refresh_token": True, "token_error": ""}
        return _FakeReply(effect="read", stdout=json.dumps(status) + "\n")
    if words[:1] == ["schema"]:
        method = words[1] if len(words) > 1 else ""
        schema = {
            "method": method,
            "parameters": {"userId": {"type": "string", "default": "me"}},
            "note": "eval fake",
        }
        return _FakeReply(effect="read", stdout=json.dumps(schema) + "\n")
    if words[:2] == ["gmail", "+triage"]:
        return _fake_triage(flags, messages)
    if words[:2] == ["gmail", "+read"]:
        return _fake_read(flags, messages)
    if words[:2] in (["gmail", "+reply"], ["gmail", "+reply-all"], ["gmail", "+forward"]):
        return _fake_message_reply(words[1], flags, n, messages)
    if words[:2] == ["gmail", "+send"]:
        return _fake_send(flags, n, thread=None, to=_fake_recipients(flags.get("to"), flags.get("cc"), flags.get("bcc")))
    if words[:2] == ["gmail", "users"]:
        return _fake_api(words[2:], flags, n, prior, account, messages)
    return _fake_error(2, 2, f"unknown command: {' '.join(words) or '(none)'}", "unknown")


def _fake_query_terms(query: str) -> list[tuple[str | None, str]]:
    terms: list[tuple[str | None, str]] = []
    for match in _QUERY_TOKEN_RE.finditer(query):
        group_key, group, quoted_key, quoted, phrase, word = match.groups()
        if group_key:
            terms.extend((group_key, part) for part in (group or "").split())
        elif quoted_key:
            terms.append((quoted_key, quoted or ""))
        elif phrase is not None:
            terms.append((None, phrase))
        elif word and word != "OR":
            key, sep, value = word.partition(":")
            terms.append((key if sep else None, value if sep else word))
    return terms


def _fake_term_matches(message: dict[str, Any], key: str | None, value: str) -> bool:
    value = value.strip().lower()
    if not value:
        return True
    if key == "from":
        return value in str(message["from"]).lower()
    if key == "subject":
        return value in str(message["subject"]).lower()
    if key == "is":
        return value != "unread" or "UNREAD" in message["labelIds"]
    if key is None:
        return any(value in str(message[field]).lower() for field in ("from", "subject", "body"))
    return True  # in:, label:, after:, before: and the rest are not modelled


def _fake_select(messages: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Messages matching every query term, newest first."""
    terms = _fake_query_terms(query)
    matching = [m for m in messages if all(_fake_term_matches(m, key, value) for key, value in terms)]
    return sorted(matching, key=_fake_date_key, reverse=True)


def _fake_date_key(message: dict[str, Any]) -> float:
    try:
        return parsedate_to_datetime(str(message["date"])).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _fake_find(messages: list[dict[str, Any]], message_id: str | None) -> dict[str, Any] | None:
    return next((m for m in messages if m["id"] == message_id), None)


def _fake_triage(flags: dict[str, str], messages: list[dict[str, Any]]) -> _FakeReply:
    """JSON by default, as the real ``gws gmail +triage``; ``--format table`` prints one line per message."""
    rows = _fake_select(messages, flags.get("query", "is:unread"))[: int(flags.get("max", "20"))]
    if flags.get("format") == "table":
        lines = "".join(f"{m['date']} | {m['from']} | {m['subject']} | {m['id']}\n" for m in rows)
        return _FakeReply(effect="read", stdout=lines)
    picked = [
        {"id": m["id"], "threadId": m["threadId"], "from": m["from"], "subject": m["subject"], "date": m["date"]}
        for m in rows
    ]
    return _FakeReply(effect="read", stdout=json.dumps(picked, indent=2) + "\n")


def _fake_read(flags: dict[str, str], messages: list[dict[str, Any]]) -> _FakeReply:
    found = _fake_find(messages, flags.get("id"))
    if found is None:
        return _fake_error(1, 404, f"no message {flags.get('id', '')!r}", "read")
    thread = str(found["threadId"])
    if flags.get("format") == "json":
        picked = {key: found[key] for key in ("id", "threadId", "from", "to", "subject", "date", "body")}
        stdout = json.dumps(picked, indent=2) + "\n"
    else:
        head = ""
        if "headers" in flags:
            head = f"From: {found['from']}\nTo: {found['to']}\nSubject: {found['subject']}\nDate: {found['date']}\n\n"
        stdout = head + str(found["body"]) + "\n"
    return _FakeReply(effect="read", stdout=stdout, thread_id=thread, message_id=str(found["id"]))


def _fake_send_effect(flags: dict[str, str]) -> str:
    if "dry-run" in flags:
        return "dry_run"
    if "draft" in flags:
        return "draft_created"
    return "sent"


def _fake_recipients(*values: str | None) -> str | None:
    """Every address in the given header or flag values, comma-joined, or None when there are none."""
    addresses = [addr for _, addr in getaddresses([v for v in values if v]) if addr]
    return ",".join(addresses) or None


def _fake_message_reply(kind: str, flags: dict[str, str], n: int, messages: list[dict[str, Any]]) -> _FakeReply:
    found = _fake_find(messages, flags.get("message-id"))
    if found is None:
        return _fake_error(1, 404, f"no message {flags.get('message-id', '')!r}", "unknown")
    addressee = flags.get("to") if kind == "+forward" else str(found["from"])
    to = _fake_recipients(addressee, flags.get("cc"), flags.get("bcc"))
    return _fake_send(flags, n, thread=str(found["threadId"]), to=to)


def _fake_send(flags: dict[str, str], n: int, *, thread: str | None, to: str | None) -> _FakeReply:
    effect = _fake_send_effect(flags)
    body = flags.get("body", "")
    if effect == "dry_run":
        stdout = json.dumps({"dry_run": True, "threadId": thread, "to": to}) + "\n"
        return _FakeReply(effect, stdout, thread_id=thread, to=to, body=body)
    if effect == "draft_created":
        return _fake_draft_reply(n, thread, to=to, body=body)
    return _FakeReply(
        effect="sent",
        stdout=json.dumps({"id": f"eval-sent-{n}", "threadId": thread, "labelIds": ["SENT"]}) + "\n",
        thread_id=thread,
        message_id=f"eval-sent-{n}",
        to=to,
        body=body,
    )


def _fake_draft_reply(n: int, thread: str | None, *, to: str | None, body: str | None) -> _FakeReply:
    message_id = f"eval-draft-msg-{n}"
    payload = {
        "id": f"r-eval-draft-{n}",
        "message": {"id": message_id, "threadId": thread, "labelIds": ["DRAFT"]},
    }
    return _FakeReply(
        effect="draft_created",
        stdout=json.dumps(payload) + "\n",
        thread_id=thread,
        message_id=message_id,
        to=to,
        body=body,
    )


def _fake_decode_raw(raw: str) -> tuple[str | None, str, str]:
    """(every To, Cc and Bcc address, body, In-Reply-To) of a base64url RFC 822 message."""
    padded = raw + "=" * (-len(raw) % 4)
    message = BytesParser().parsebytes(base64.urlsafe_b64decode(padded))
    payload: Any = message.get_payload(decode=True)
    body = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
    headers = [str(value) for name in ("To", "Cc", "Bcc") for value in (message.get_all(name) or [])]
    return _fake_recipients(*headers), body, str(message.get("In-Reply-To", "")).strip()


def _fake_compose(payload: dict[str, Any], messages: list[dict[str, Any]]) -> tuple[str | None, str | None, str | None]:
    """(thread, to, body) of an API draft or send: its threadId, else the message its raw In-Reply-To names."""
    part: dict[str, Any] = payload["message"] if isinstance(payload.get("message"), dict) else {}
    raw = str(part.get("raw") or payload.get("raw") or "")
    to: str | None = None
    body: str | None = None
    in_reply_to = ""
    if raw:
        to, body, in_reply_to = _fake_decode_raw(raw)
    inline = payload.get("threadId") or part.get("threadId")
    thread = str(inline) if inline else None
    if thread is None and in_reply_to:
        thread = next((str(m["threadId"]) for m in messages if m["rfc822_id"] == in_reply_to), None)
    return thread, to, body


def _fake_resource(message: dict[str, Any]) -> dict[str, Any]:
    headers = [
        {"name": "From", "value": message["from"]},
        {"name": "To", "value": message["to"]},
        {"name": "Subject", "value": message["subject"]},
        {"name": "Date", "value": message["date"]},
        {"name": "Message-ID", "value": message["rfc822_id"]},
    ]
    data = base64.urlsafe_b64encode(str(message["body"]).encode("utf-8")).decode("ascii")
    return {
        "id": message["id"],
        "threadId": message["threadId"],
        "labelIds": message["labelIds"],
        "snippet": _fake_one_line(str(message["body"]))[:100],
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"size": len(str(message["body"]).encode("utf-8")), "data": data},
        },
    }


def _fake_api(
    rest: list[str],
    flags: dict[str, str],
    n: int,
    prior: list[dict[str, Any]],
    account: str,
    messages: list[dict[str, Any]],
) -> _FakeReply:
    params = _fake_json_flag(flags, "params")
    key = tuple(rest[:2])
    query = str(params.get("q", ""))
    if rest[:1] == ["getProfile"]:
        threads = {m["threadId"] for m in messages}
        profile = {"emailAddress": account, "messagesTotal": len(messages), "threadsTotal": len(threads)}
        return _FakeReply(effect="read", stdout=json.dumps(profile) + "\n")
    if key == ("messages", "list"):
        matched = _fake_select(messages, query)
        limit = int(params.get("maxResults", 20))
        rows = [{"id": m["id"], "threadId": m["threadId"]} for m in matched[:limit]]
        listing = {"messages": rows, "resultSizeEstimate": len(matched)}
        return _FakeReply(effect="read", stdout=json.dumps(listing) + "\n")
    if key == ("messages", "get"):
        found = _fake_find(messages, str(params.get("id", "")))
        if found is None:
            return _fake_error(1, 404, "Requested entity was not found.", "read")
        return _FakeReply(effect="read", stdout=json.dumps(_fake_resource(found)) + "\n")
    if key == ("threads", "list"):
        seen = list(dict.fromkeys(str(m["threadId"]) for m in _fake_select(messages, query)))
        return _FakeReply(
            effect="read",
            stdout=json.dumps({"threads": [{"id": t} for t in seen], "resultSizeEstimate": len(seen)}) + "\n",
        )
    if key == ("threads", "get"):
        thread_id = str(params.get("id", ""))
        in_thread = [m for m in messages if m["threadId"] == thread_id]
        if not in_thread:
            return _fake_error(1, 404, "Requested entity was not found.", "read")
        resource = {"id": thread_id, "messages": [_fake_resource(m) for m in in_thread]}
        return _FakeReply(effect="read", stdout=json.dumps(resource) + "\n", thread_id=thread_id)
    if key == ("drafts", "list"):
        drafts = [
            {"id": row["message_id"], "message": {"id": row["message_id"], "threadId": row["thread_id"]}}
            for row in prior
            if row.get("effect") == "draft_created"
        ]
        return _FakeReply(effect="read", stdout=json.dumps({"drafts": drafts}) + "\n")
    if key == ("labels", "list"):
        labels = [{"id": label, "name": label} for label in _FAKE_LABELS]
        return _FakeReply(effect="read", stdout=json.dumps({"labels": labels}) + "\n")
    if key in (("drafts", "create"), ("drafts", "update")):
        payload = _fake_json_flag(flags, "json")
        thread, to, body = _fake_compose(payload, messages)
        return _fake_draft_reply(n, thread, to=to, body=body)
    if key in (("messages", "send"), ("drafts", "send")):
        payload = _fake_json_flag(flags, "json")
        thread, to, body = _fake_compose(payload, messages)
        return _FakeReply(
            effect="sent",
            stdout=json.dumps({"id": f"eval-sent-{n}", "threadId": thread, "labelIds": ["SENT"]}) + "\n",
            thread_id=thread,
            message_id=f"eval-sent-{n}",
            to=to,
            body=body,
        )
    if rest[:1] == ["messages"] and len(rest) > 1 and rest[1] in _FAKE_MODIFY_METHODS:
        return _FakeReply(effect="modified", stdout="{}\n")
    return _fake_error(2, 2, f"unknown API method: {' '.join(rest) or '(none)'}", "unknown")


# --- usage and transcripts --------------------------------------------------------

_TOKEN_FIELDS: dict[str, str] = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}
# OpenCode transcripts spell the same counts in camelCase (the USAGE_ALIASES
# table in scripts/session_audit.py). A field present in both spellings counts
# the snake_case one only, so a turn is never counted twice.
_TOKEN_CAMEL_FIELDS: dict[str, str] = {
    "input": "inputTokens",
    "output": "outputTokens",
    "cache_read": "cacheReadTokens",
    "cache_write": "cacheWriteTokens",
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


def _add_usage(tokens: dict[str, int], usage: Any) -> None:
    """Add one turn's usage to ``tokens``. A snake_case key wins over its camelCase twin."""
    if not isinstance(usage, dict):
        return
    for name, key in _TOKEN_FIELDS.items():
        value = usage.get(key)
        if value is None:
            value = usage.get(_TOKEN_CAMEL_FIELDS[name])
        tokens[name] += _as_int(value)


def collect_usage(runtime: Path) -> dict[str, Any]:
    """Token, tool, ciao-call and cost figures for one engine's runtime directory."""
    tokens = {name: 0 for name in _TOKEN_FIELDS}
    tool_calls = 0
    tool_errors = 0
    provider_errors = 0
    provider_error: str | None = None
    chat_ids: list[str] = []
    for path in _transcripts(runtime):
        chat_ids.append(path.parent.name)
        for turn in _read_transcript(path):
            if turn.get("is_error"):
                provider_errors += 1
                if provider_error is None:
                    response = turn.get("response")
                    provider_error = (response if isinstance(response, str) else "")[:300]
            _add_usage(tokens, turn.get("usage"))
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
        "provider_errors": provider_errors,
        "provider_error": provider_error,
        "ciao_calls": ciao_calls,
        "ciao_errors": ciao_errors,
        "cost_usd": cost_usd,
        "chat_ids": chat_ids,
    }


def _archive_usage_totals(text: str) -> dict[str, Any]:
    """The ``usage_totals`` mapping in an archived transcript's frontmatter, or ``{}``."""
    if not text.startswith("---\n"):
        return {}
    closing = _FENCE_RE.search(text, 4)
    if closing is None:
        return {}
    try:
        block: Any = yaml.safe_load(text[4:closing.start()])
    except yaml.YAMLError:
        return {}
    totals = block.get("usage_totals") if isinstance(block, dict) else None
    return totals if isinstance(totals, dict) else {}


def memory_pass_usage(ws: Path, chat_id: str, archive_path: str | None) -> tuple[dict[str, int], str]:
    """Tokens and final reply of one memory-pass chat.

    A pass that is still live (``attention``, or stopped on timeout) keeps its
    runtime transcript, which is read directly. A pass that archived cleanly has
    lost that file, so its archive's ``usage_totals`` frontmatter is read; the
    archive path is relative to the workspace root.
    """
    from ciao.transcripts import parse_archive_turns

    tokens = {name: 0 for name in _TOKEN_FIELDS}
    live = sorted((ws / ".runtime" / "transcripts" / chat_id).glob("*.json"))
    if live:
        response = ""
        for path in live:
            for turn in _read_transcript(path):
                _add_usage(tokens, turn.get("usage"))
                text = turn.get("response")
                if isinstance(text, str) and text:
                    response = text
        return tokens, response
    if not archive_path:
        return tokens, ""
    target = Path(archive_path)
    if not target.is_absolute():
        target = ws / target
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return tokens, ""
    _add_usage(tokens, _archive_usage_totals(text))
    turns = parse_archive_turns(text)
    response = str(turns[-1].get("assistant") or "").strip() if turns else ""
    return tokens, response


def transcript_path(ws: Path, chat_id: str, provider: str) -> Path:
    """Where the engine keeps a chat's runtime transcript, named by the engine's own slugging."""
    from ciao.models import ChatContext
    from ciao.transcripts import TranscriptStore

    # Only current_path is used here, so the archive root is never written to.
    store = TranscriptStore(ws / ".runtime", ws / ".runtime" / "unused-archive")
    return store.current_path(ChatContext.for_web(chat_id), provider)


def synthetic_transcript(
    chat_id: str,
    turns: Sequence[Mapping[str, str]],
    *,
    provider: str,
    model: str,
    mode: str,
    title: str,
    now: str | None = None,
) -> dict[str, Any]:
    """A normalized transcript in the shape ``TranscriptStore.record_turn`` writes, with zero usage."""
    stamp = now or datetime.now(UTC).isoformat(timespec="seconds")
    return {
        "provider": provider,
        "started_at": stamp,
        "updated_at": stamp,
        "selected_model": model,
        "session_id": "",
        "context_key": chat_id,
        "context_label": title,
        "turns": [
            {
                "timestamp": stamp,
                "input_kind": "text",
                "prompt": str(turn["user"]),
                "mode": mode,
                "resume_session": "",
                "image_count": 0,
                "response": str(turn["assistant"]),
                "is_error": False,
                "effective_model": model,
                "usage": {},
                "quota": {},
                "tool_events": [],
                "tool_calls": [],
            }
            for turn in turns
        ],
    }


def _memory_pass_step(postprocess: Any) -> dict[str, Any]:
    """The ``memory_pass`` step of a chat's postprocess record, or ``{}`` where a level is missing."""
    if not isinstance(postprocess, dict):
        return {}
    steps = postprocess.get("steps")
    if not isinstance(steps, dict):
        return {}
    step = steps.get("memory_pass")
    return step if isinstance(step, dict) else {}


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


def _snapshot_paths(task: EvalTask) -> list[str]:
    """Files whose bytes the snapshot keeps: ``unchanged`` targets, and the queue and ``include_files`` of each proposals check."""
    paths: list[str] = []

    def keep(rel: str) -> None:
        if rel not in paths:
            paths.append(rel)

    for spec in task.checks:
        if spec.get("kind") == "unchanged":
            keep(str(spec["path"]))
        elif spec.get("kind") == "proposals":
            keep(str(spec.get("path", PROPOSALS_PATH)))
            for extra in spec.get("include_files", []):
                keep(str(extra))
    return paths


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


def _base_record(
    task: EvalTask, *, provider: str, model: str, mode: str, label: str, repeat: int
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "label": label,
        "task_id": task.id,
        "tier": task.tier,
        "category": task.category,
        "provider": provider,
        "model": model,
        "mode": mode,
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
        "provider_errors": 0,
        "wall_s": 0.0,
        "timed_out": False,
        "chat_ids": [],
        "memory_pass_status": None,
        "memory_pass_chat_id": None,
        "memory_pass_tokens": None,
    }


def run_task(
    task: EvalTask,
    *,
    provider: str,
    model: str,
    label: str,
    repeat: int,
    mode: str | None = None,
    session_factory: Callable[[EvalTask], EngineSession] | None = None,
    capabilities: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """One graded run of one task. Never raises: a failure becomes ``status="error"``.

    ``mode`` is the permission mode the chats start in (default: the provider's
    entry in ``DEFAULT_MODES``). Without ``session_factory`` the run boots a real
    engine on ``provider``/``model``/``mode``.
    """
    record = _base_record(
        task,
        provider=provider,
        model=model,
        mode=_requested_mode(provider, mode),
        label=label,
        repeat=repeat,
    )
    missing = [name for name in task.requires if name not in capabilities]
    if missing:
        record["status"] = "skipped"
        record["skip_reason"] = f"missing capability: {', '.join(missing)}"
        return record

    factory = session_factory or partial(
        IsolatedEngine.boot, provider=provider, model=model, mode=mode
    )
    started = time.monotonic()
    session: EngineSession | None = None
    try:
        # Validated before any engine boots: an unknown mode is an error record.
        resolve_mode(provider, mode)
        session = factory(task)
        _apply_setup(session.api, task)
        snapshot = take_snapshot(session.agent_root, session.api, _snapshot_paths(task))
        before: dict[str, Any] | None = None
        pass_outcome: MemoryPassOutcome | None = None
        if task.entry["kind"] == "prompt":
            chat_id = session.start_prompt(task.prompt, provider=provider, model=model)
        elif task.entry["kind"] == "delegate":
            task_id, revision = _find_delegate_target(session.api, str(task.entry["task_title"]))
            chat_id = session.delegate(task_id, revision, provider=provider, model=model)
        else:
            chat_id = session.start_transcript(list(task.entry["turns"]), provider=provider, model=model)
            # Read before the archive, which deletes the synthetic chat's runtime transcript.
            before = session.usage()
            pass_outcome = session.run_memory_pass(chat_id, task.timeout_s)
            if pass_outcome.status == "not_queued":
                raise RuntimeError("memory pass was not queued for the archived transcript")
        if pass_outcome is None:
            settled = session.wait_settled(task.timeout_s)
            if not settled:
                session.stop_chat(chat_id)
            answer = session.answer_text()
        else:
            settled = pass_outcome.status == "ok"
            answer = pass_outcome.response
        # Only a task that needs the fake reads the session's call log, so sessions
        # for other tasks never have to carry the attribute.
        ctx = GradeContext(
            agent_root=session.agent_root,
            answer=answer,
            api=session.api,
            snapshot=snapshot,
            run_date=date.today(),
            gws_call_log=session.gws_call_log if task.gws_fake is not None else None,
        )
        results = grade(task, ctx)
        usage = session.usage()
        all_ok = all(r.ok for r in results)
        # The synthetic chat read before the archive can add nothing (it carries no
        # errors), but its counts are summed in so none can be lost to the archive.
        earlier = before or {}
        provider_errors = _as_int(usage["provider_errors"]) + _as_int(earlier.get("provider_errors"))
        status = "passed" if settled and all_ok else "failed"
        error = None
        # A provider failure (quota, auth, transport) that kept the run from passing
        # is an outage, not a model failure. A passing run did the work despite it.
        if provider_errors and status != "passed":
            status = "error"
            provider_error = usage["provider_error"] or earlier.get("provider_error")
            error = f"provider error: {provider_error or '(no message)'}"
        pass_fields: dict[str, Any] = {}
        if pass_outcome is None:
            tokens = _tokens(usage["tokens"])
            timed_out = not settled
        else:
            # A transcript run's tokens are the synthetic chat's (zero) plus the pass's.
            earlier_tokens = earlier.get("tokens") or {}
            tokens = _tokens(
                {
                    name: _as_int(earlier_tokens.get(name)) + pass_outcome.tokens.get(name, 0)
                    for name in _TOKEN_FIELDS
                }
            )
            timed_out = pass_outcome.status == "timeout"
            pass_fields = {
                "memory_pass_status": pass_outcome.status,
                "memory_pass_chat_id": pass_outcome.chat_id,
                "memory_pass_tokens": _tokens(pass_outcome.tokens),
            }
        record.update(
            status=status,
            error=error,
            checks=[asdict(r) for r in results],
            answer=ctx.answer[-ANSWER_TAIL_CHARS:],
            tokens=tokens,
            tool_calls=_as_int(usage["tool_calls"]),
            tool_errors=_as_int(usage["tool_errors"]),
            provider_errors=provider_errors,
            ciao_calls=_as_int(usage["ciao_calls"]),
            ciao_errors=_as_int(usage["ciao_errors"]),
            cost_usd=usage["cost_usd"],
            timed_out=timed_out,
            chat_ids=list(usage["chat_ids"]),
            **pass_fields,
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
    mode: str | None = None,
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
            mode=mode,
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
    """Per (label, provider, model, mode, code revision, task): counts, pass rate and medians.

    The pass rate and the medians are over runs that neither skipped nor errored;
    ``errors`` counts the runs whose provider failed. A report from before the ``mode`` field has no
    mode to group by, so those rows fall into the ``-`` group.
    """
    groups: dict[tuple[str, str, str, str, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("label")),
            str(row.get("provider")),
            str(row.get("model")),
            str(row.get("mode") or "-"),
            str(row.get("code_revision")),
            str(row.get("task_id")),
        )
        groups.setdefault(key, []).append(row)

    summary: list[dict[str, Any]] = []
    for key in sorted(groups):
        label, provider, model, mode, revision, task_id = key
        items = groups[key]
        ran = [r for r in items if r.get("status") != "skipped"]
        scored = [r for r in ran if r.get("status") != "error"]
        passed = sum(1 for r in scored if r.get("status") == "passed")
        summary.append(
            {
                "label": label,
                "provider": provider,
                "model": model,
                "mode": mode,
                "code_revision": revision,
                "task_id": task_id,
                "n": len(items),
                "ran": len(ran),
                "skipped": len(items) - len(ran),
                "errors": len(ran) - len(scored),
                "pass_rate": passed / len(scored) if scored else None,
                "median_context_total": _median([r["tokens"]["context_total"] for r in scored]),
                "median_output": _median([r["tokens"]["output"] for r in scored]),
                "median_wall_s": _median([r.get("wall_s") for r in scored]),
                "median_cost_usd": _median([r.get("cost_usd") for r in scored]),
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
        "| Task | Label | Provider | Model | Mode | Revision | n | Ran | Errors | Pass rate | Median context | Median output | Median wall s | Median cost USD | Tool errors | Timeouts |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summary:
        pass_rate = item["pass_rate"]
        lines.append(
            "| {task} | {label} | {provider} | {model} | {mode} | {rev} | {n} | {ran} | {errors} | {pass_rate} | "
            "{ctx} | {out} | {wall} | {cost} | {terr} | {timeouts} |".format(
                task=item["task_id"],
                label=item["label"],
                provider=item["provider"],
                model=item["model"],
                mode=item["mode"],
                rev=item["code_revision"],
                n=item["n"],
                ran=item["ran"],
                errors=item["errors"],
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
        "| Task | Baseline mode | Candidate mode | Baseline pass | Candidate pass | Delta pass | Delta context % | Delta wall % | Delta cost % |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in comparison:
        base = item["baseline"] or {}
        cand = item["candidate"] or {}
        delta = item["delta"]
        lines.append(
            "| {task} | {bm} | {cm} | {bp} | {cp} | {dp} | {dc} | {dw} | {dco} |".format(
                task=item["task_id"],
                bm=base.get("mode", "-"),
                cm=cand.get("mode", "-"),
                bp=_fmt(base.get("pass_rate")),
                cp=_fmt(cand.get("pass_rate")),
                dp=_fmt(delta["pass_rate"]),
                dc=_fmt(delta["context_total_pct"], 1),
                dw=_fmt(delta["wall_s_pct"], 1),
                dco=_fmt(delta["cost_pct"], 1),
            )
        )
    return "\n".join(lines)
