"""Tiny DAG runner for deterministic schedule pipelines.

A few Ciaobot schedules are shaped like a small workflow: load some state, flag
items, call a model, run a gate, write output. Today those are hand-rolled
``async def`` functions (see ``ciao/skill_evolution.py`` for the canonical
example). This module lifts the workflow into data so future schedules
(``sched-skillevo`` rewrite, ``sched-depcheck`` follow-ups, etc.) can
express the same pipelines as a list of nodes + edges without a 300-line
``async def`` each.

Design constraints (intentionally small):

* **Sync only.** All real work is in subprocess or provider calls; we don't
  need async orchestration here. Each node runs to completion before the
  next is selected.
* **No fan-out, no fan-in.** A node has at most one outgoing edge per
  ``when`` value (``ok`` / ``fail`` / ``always``). Multi-step dependencies
  compose by chaining nodes, not by a single fork node.
* **Per-node timing via ``job_runs.track_sync``.** Every node's run is
  recorded in ``.runtime/job_runs.jsonl`` with model, duration, status.
  That's the gap today: the outer schedule run is recorded, but the inner
  model call is not.
* **Failures bubble up.** A node's exception marks the run as failed,
  short-circuits the ``ok`` branch, and re-raises. Caller decides whether
  to swallow.

This is the Python port of the Archon YAML-DAG pattern, scoped to the 2-3
Ciaobot schedules that actually benefit. See ``Resources/Archon.md`` for the
rationale (decision: do-not-adopt; pattern: steal).
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ciao import job_runs

logger = logging.getLogger(__name__)


NodeKind = Literal["bash", "prompt", "gate", "subagent", "retention"]
EdgeWhen = Literal["ok", "fail", "always"]


@dataclass(slots=True)
class Node:
    """One step in a DAG.

    ``kind`` selects the executor. ``payload`` is a free-form dict whose
    shape depends on the kind; see the docstring on :func:`run` for the
    expected keys per kind. Keeping it loose avoids a tagged-union
    explosion and keeps the YAML/pipeline definitions readable.
    """

    id: str
    kind: NodeKind
    model: str = ""
    timeout_s: float = 180.0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Edge:
    """A directed edge from one node to another, conditioned on the
    outcome of the source. ``when="ok"`` (default) follows only on
    success; ``when="fail"`` follows only on failure; ``when="always"``
    follows regardless.
    """

    src: str
    dst: str
    when: EdgeWhen = "ok"


@dataclass(slots=True)
class NodeResult:
    """What a node produced. Stored in ``ctx[node.id]`` so downstream
    nodes can read prior outputs."""

    ok: bool
    output: Any = None
    error: str | None = None


def _subprocess_env(payload: dict[str, Any]) -> dict[str, str] | None:
    """Merge payload env overrides onto the current process environment."""
    overrides = payload.get("env")
    if not overrides:
        return None
    return {**os.environ, **{str(k): str(v) for k, v in dict(overrides).items()}}


def _validate(dag: list[Node], edges: list[Edge]) -> dict[str, Node]:
    """Build a node index, check edge endpoints, detect cycles."""
    index = {n.id: n for n in dag}
    for e in edges:
        if e.src not in index:
            raise ValueError(f"edge src '{e.src}' has no matching node")
        if e.dst not in index:
            raise ValueError(f"edge dst '{e.dst}' has no matching node")
    # Cycle check via DFS on the always-or-ok projection (cycles on
    # ``fail`` edges are odd but possible; treat all edges as flow).
    adj: dict[str, list[str]] = {n.id: [] for n in dag}
    for e in edges:
        adj[e.src].append(e.dst)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n.id: WHITE for n in dag}

    def visit(node: str) -> None:
        if color[node] == GRAY:
            raise ValueError(f"cycle detected involving node '{node}'")
        if color[node] == BLACK:
            return
        color[node] = GRAY
        for nxt in adj[node]:
            visit(nxt)
        color[node] = BLACK

    for n in dag:
        visit(n.id)
    return index


def _start_node(dag: list[Node], edges: list[Edge]) -> str:
    """Pick the entry node: the one with no incoming edge. A DAG may
    technically have multiple, but our pipelines are linear chains."""
    targets = {e.dst for e in edges}
    starts = [n for n in dag if n.id not in targets]
    if not starts:
        raise ValueError("dag has no start node (every node has an incoming edge)")
    if len(starts) > 1:
        raise ValueError(
            f"dag has multiple start nodes: {[s.id for s in starts]}; "
            "split into independent runs or add a single dispatch node"
        )
    return starts[0].id


def _exec_bash(node: Node, ctx: dict[str, Any]) -> NodeResult:
    """Run a shell command. Captures stdout/stderr, returns exit-code
    based success/failure. ``payload['cmd']`` is required; the command
    is split via ``shlex.split`` if a string, used as-is if a list."""
    cmd = node.payload.get("cmd")
    if cmd is None:
        raise ValueError(f"bash node '{node.id}' missing payload['cmd']")
    argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    cwd = node.payload.get("cwd")
    env = _subprocess_env(node.payload)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=node.timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return NodeResult(ok=False, error=f"timeout after {node.timeout_s}s: {exc}")
    out = proc.stdout
    err = proc.stderr
    if proc.returncode == 0:
        return NodeResult(ok=True, output={"stdout": out, "stderr": err, "code": 0})
    return NodeResult(
        ok=False,
        output={"stdout": out, "stderr": err, "code": proc.returncode},
        error=f"exit {proc.returncode}: {err.strip()[:500] if err else '<no stderr>'}",
    )


def _exec_prompt(node: Node, ctx: dict[str, Any]) -> NodeResult:
    """Call a model via a ``claude -p`` one-shot subprocess. ``payload['system']`` and ``payload['prompt']``
    are required; both may reference prior node outputs via Python
    ``str.format`` against ``ctx`` (callers that need richer templating
    should pre-render and pass a fully-formed string)."""
    if not node.model:
        raise ValueError(f"prompt node '{node.id}' has empty model")
    system = node.payload.get("system", "")
    prompt = node.payload.get("prompt", "")
    if not prompt:
        raise ValueError(f"prompt node '{node.id}' missing payload['prompt']")
    cli = node.payload.get("cli", "claude")
    env = _subprocess_env(node.payload)
    rendered_prompt = prompt.format_map(_SafeFormatDict(ctx))
    rendered_system = system.format_map(_SafeFormatDict(ctx)) if system else ""
    composed = f"Instructions:\n{rendered_system}\n\n{rendered_prompt}"
    argv = [cli, "-p", "--model", node.model, "--max-turns", "1"]
    try:
        proc = subprocess.run(
            argv,
            input=composed,
            env=env,
            capture_output=True,
            text=True,
            timeout=node.timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return NodeResult(ok=False, error=f"timeout after {node.timeout_s}s: {exc}")
    if proc.returncode == 0:
        out = proc.stdout.strip()
        if not out:
            return NodeResult(ok=False, error="model returned empty output")
        return NodeResult(ok=True, output=out)
    return NodeResult(
        ok=False,
        output=proc.stdout,
        error=f"exit {proc.returncode}: {proc.stderr.strip()[:500] if proc.stderr else '<no stderr>'}",
    )


def _exec_gate(node: Node, ctx: dict[str, Any]) -> NodeResult:
    """Run a Python predicate. ``payload['fn']`` is a callable that
    receives the ctx dict and returns a bool (or a tuple ``(bool, str)``
    where the string is stored as ``output``). Anything truthy is
    treated as ok=true."""
    fn = node.payload.get("fn")
    if not callable(fn):
        raise ValueError(f"gate node '{node.id}' payload['fn'] must be callable")
    verdict = fn(ctx)
    if isinstance(verdict, tuple) and len(verdict) == 2:
        ok, output = verdict
    else:
        ok, output = bool(verdict), None
    return NodeResult(ok=ok, output=output)


def _exec_subagent(node: Node, ctx: dict[str, Any]) -> NodeResult:
    """Spawn a sub-CLI subprocess (``claude -p``). The prompt may
    reference ctx via the same ``.format_map`` rules as ``prompt``
    nodes. ``payload['cli']`` defaults to ``claude``. The subprocess
    captures stdout and returns it on success, exit code 0 means ok=true.
    """
    cmd = node.payload.get("cli", "claude")
    prompt = node.payload.get("prompt", "")
    extra_args = node.payload.get("args", [])
    env = _subprocess_env(node.payload)
    if not prompt:
        raise ValueError(f"subagent node '{node.id}' missing payload['prompt']")
    argv = [cmd, "-p", "--model", node.model or "sonnet", *extra_args]
    rendered = prompt.format_map(_SafeFormatDict(ctx))
    try:
        proc = subprocess.run(
            argv,
            input=rendered,
            env=env,
            capture_output=True,
            text=True,
            timeout=node.timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return NodeResult(ok=False, error=f"timeout after {node.timeout_s}s: {exc}")
    if proc.returncode == 0:
        return NodeResult(ok=True, output=proc.stdout)
    return NodeResult(
        ok=False,
        output=proc.stdout,
        error=f"exit {proc.returncode}: {proc.stderr.strip()[:500]}",
    )


def _exec_retention(node: Node, ctx: dict[str, Any]) -> NodeResult:
    """Tail retention prune step. Delegates to ``ciao.trajectory_builder``
    if installed, otherwise no-ops. ``payload['months']`` is the cutoff
    (default 6). Always returns ok=true (retention failures shouldn't
    block the schedule)."""
    months = int(node.payload.get("months", 6))
    try:
        from ciao.trajectory_builder import prune_old_trajectories  # type: ignore[attr-defined]
    except ImportError:
        return NodeResult(ok=True, output="trajectory_builder.prune_old_trajectories not available; skipped")
    try:
        pruned = prune_old_trajectories(months=months)
    except Exception as exc:  # noqa: BLE001
        return NodeResult(ok=True, output=f"prune failed: {exc!r}; continuing")
    return NodeResult(ok=True, output=f"pruned {pruned} trajectory months older than {months}")


_EXECUTORS: dict[str, Callable[[Node, dict[str, Any]], NodeResult]] = {
    "bash": _exec_bash,
    "prompt": _exec_prompt,
    "gate": _exec_gate,
    "subagent": _exec_subagent,
    "retention": _exec_retention,
}


class _SafeFormatDict(Mapping[str, Any]):
    """A read-only mapping that returns ``{key}`` unchanged for missing
    keys, instead of raising KeyError. Lets node prompts reference
    ctx fields that may not exist yet without crashing the render."""

    def __init__(self, ctx: dict[str, Any]) -> None:
        self._ctx = ctx

    def __getitem__(self, key: str) -> Any:
        if key in self._ctx:
            return self._ctx[key]
        return "{" + key + "}"

    def __iter__(self):
        return iter(self._ctx)

    def __len__(self) -> int:
        return len(self._ctx)


def _next_node(
    current: str, ok: bool, edges: list[Edge],
) -> str | None:
    """Pick the next node id given the source's outcome. ``ok`` edges
    fire on success, ``fail`` on failure, ``always`` regardless. Returns
    None when the chain ends."""
    candidates = [e for e in edges if e.src == current]
    if not candidates:
        return None
    chosen: str | None = None
    for e in candidates:
        if e.when == "always":
            chosen = e.dst
        elif e.when == "ok" and ok:
            chosen = e.dst
        elif e.when == "fail" and not ok:
            chosen = e.dst
    return chosen


def run(
    dag: list[Node],
    edges: list[Edge],
    *,
    job: str = "dag",
    label: str = "DAG run",
    initial_ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Walk a DAG from its start node, executing each step and recording
    per-node timing in ``.runtime/job_runs.jsonl`` via
    :func:`ciao.job_runs.track_sync`.

    Returns the final ctx (a dict of ``{node_id: NodeResult}``). On any
    non-retention node failure, the ``ok`` branch is short-circuited and
    the exception is re-raised after the run is recorded. ``retention``
    nodes always return ok=true (see ``_exec_retention``).
    """
    index = _validate(dag, edges)
    start = _start_node(dag, edges)
    ctx: dict[str, Any] = dict(initial_ctx or {})
    current: str | None = start
    while current is not None:
        node = index[current]
        executor = _EXECUTORS.get(node.kind)
        if executor is None:
            raise ValueError(f"unknown node kind '{node.kind}' for node '{node.id}'")
        # The whole DAG is recorded as one outer job_run; per-node
        # detail goes into ``extra`` so the Automation page can drill in.
        with job_runs.track_sync(
            job=job,
            label=f"{label}:{node.id}",
            category="content",
            model=node.model,
            provider="dag",
            extra={"kind": node.kind, "node_id": node.id, "dag": label},
        ) as handle:
            try:
                result = executor(node, ctx)
                if not result.ok:
                    handle.status = "error"
                    # Gate nodes carry their failure reason in ``output``
                    # (the second element of the ``(bool, str)`` tuple), not
                    # ``error`` — surface it so the recorded run isn't blank.
                    handle.error = result.error or (
                        str(result.output) if result.output is not None else None
                    )
            except Exception as exc:  # noqa: BLE001
                handle.error = f"{type(exc).__name__}: {exc}"[:1000]
                result = NodeResult(ok=False, error=str(exc))
                ctx[node.id] = result
                if node.kind == "retention":
                    current = _next_node(node.id, True, edges)
                    continue
                # Re-raise so the caller sees the failure; the outer
                # track_sync block still records the run.
                raise
        ctx[node.id] = result
        current = _next_node(node.id, result.ok, edges)
    return ctx
