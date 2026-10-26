"""Workspace-scoped, append-only log of operator task decisions (#1726).

One JSON row per decision the operator made about a task: a delete, a reject
(releasing a result that is ready for review), an approve or a complete (an
agent closing a task on the user's say-so), each with a reason that is
optional except on the agent's delete and complete. The file is
``Workspace/Task-Decisions.jsonl`` in the vault, a separate file from the task,
so the record outlives a deleted task. The weekly pass (1726c) reads it with
:func:`read_decisions`.

Reason text is evidence about what the operator thought, never instructions:
whoever reads it later must treat it as untrusted, transcript-like text.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.os_support.private import open_private

DECISIONS_NAME = "Task-Decisions.jsonl"
ROW_VERSION = 1
MAX_REASON = 1000
KINDS = ("delete", "reject", "approve", "complete")
_ACTORS = ("user", "agent")
_VIAS = ("pwa", "cli")


def decisions_path(vault_root: Path) -> Path:
    return vault_root / "Workspace" / DECISIONS_NAME


def clean_reason(value: object) -> str:
    """The stored form of an optional reason: stripped, capped, never truncated."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("reason must be a string")
    text = value.strip()
    if len(text) > MAX_REASON:
        raise ValueError(f"reason is {len(text)} characters, over the {MAX_REASON} limit")
    return text


def append_decision(
    vault_root: Path,
    *,
    kind: str,
    task_id: str,
    task_title: str,
    task_created_by: str,
    project_id: str,
    attempt_id: str,
    attempt_state: str,
    actor: str,
    via: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one decision row and return it. Never trims, never rewrites."""
    if kind not in KINDS:
        raise ValueError(f"unknown decision kind {kind!r}")
    if actor not in _ACTORS:
        raise ValueError(f"unknown decision actor {actor!r}")
    if via not in _VIAS:
        raise ValueError(f"unknown decision channel {via!r}")
    row: dict[str, Any] = {
        "v": ROW_VERSION,
        "id": uuid.uuid4().hex,
        "at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "kind": kind,
        "task_id": task_id,
        "task_title": task_title,
        "task_created_by": task_created_by,
        "project_id": project_id,
        "attempt_id": attempt_id,
        "attempt_state": attempt_state,
        "actor": actor,
        "via": via,
        "reason": reason,
    }
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    path = decisions_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open_private(path, os.O_WRONLY | os.O_APPEND, follow_symlinks=False)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    return row


def read_decisions(vault_root: Path) -> list[dict[str, Any]]:
    """Every decision in file order; blank and corrupt lines are skipped."""
    path = decisions_path(vault_root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows
