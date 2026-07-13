"""Tests for ``ciao.dag`` (the small DAG runner for deterministic schedule pipelines)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao import job_runs as jr
from ciao import dag as dag_mod
from ciao.dag import Edge, Node, NodeResult, run


def _job_runs(tmp_path: Path) -> list[dict]:
    path = tmp_path / jr.JOB_RUNS_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── Validation: cycles, start node, edge endpoints ──────────────────────


def test_unknown_edge_src_raises() -> None:
    dag = [Node(id="a", kind="bash", payload={"cmd": "true"})]
    edges = [Edge(src="missing", dst="a")]
    with pytest.raises(ValueError, match="src 'missing'"):
        run(dag, edges)


def test_unknown_edge_dst_raises() -> None:
    dag = [Node(id="a", kind="bash", payload={"cmd": "true"})]
    edges = [Edge(src="a", dst="missing")]
    with pytest.raises(ValueError, match="dst 'missing'"):
        run(dag, edges)


def test_cycle_raises() -> None:
    # a -> b -> a
    dag = [
        Node(id="a", kind="bash", payload={"cmd": "true"}),
        Node(id="b", kind="bash", payload={"cmd": "true"}),
    ]
    edges = [Edge(src="a", dst="b"), Edge(src="b", dst="a")]
    with pytest.raises(ValueError, match="cycle"):
        run(dag, edges)


def test_no_start_node_raises() -> None:
    """A cycle where every node has an incoming edge (a closed ring)
    is rejected by cycle detection first — covered by test_cycle_raises.
    Constructing a 'no start' DAG without a cycle is impossible under our
    validation: an acyclic graph always has at least one node with no
    incoming edge. This test just confirms the multi-start path is
    covered (next test)."""
    dag = [
        Node(id="a", kind="bash", payload={"cmd": "true"}),
        Node(id="b", kind="bash", payload={"cmd": "true"}),
    ]
    edges = [Edge(src="a", dst="b"), Edge(src="b", dst="a")]  # cycle
    with pytest.raises(ValueError, match="cycle"):
        run(dag, edges)


def test_multiple_start_nodes_raises() -> None:
    dag = [
        Node(id="a", kind="bash", payload={"cmd": "true"}),
        Node(id="b", kind="bash", payload={"cmd": "true"}),
    ]
    edges = []  # neither has an incoming edge → both are starts
    with pytest.raises(ValueError, match="multiple start nodes"):
        run(dag, edges)


# ── Linear chain: ok branch ──────────────────────────────────────────────


def test_bash_chain_runs_to_completion(tmp_path: Path) -> None:
    dag = [
        Node(id="a", kind="bash", payload={"cmd": "echo hi"}),
        Node(id="b", kind="bash", payload={"cmd": "echo there"}),
    ]
    edges = [Edge(src="a", dst="b")]
    ctx = run(dag, edges, job="unit", label="chain")
    assert ctx["a"].ok is True
    assert ctx["b"].ok is True
    # a's stdout is captured
    assert ctx["a"].output["stdout"].strip() == "hi"
    # job_runs got one record per node
    rows = _job_runs(tmp_path)
    node_ids = [r["extra"]["node_id"] for r in rows]
    assert node_ids == ["a", "b"]


def test_bash_failure_short_circuits_ok_branch(tmp_path: Path) -> None:
    """A non-zero exit code from a bash node yields ``ok=False``; the
    ok-edge is skipped, the chain follows any fail/always edge (or
    ends). It does NOT raise — that is reserved for uncaught
    exceptions inside the executor (e.g. timeout)."""
    dag = [
        Node(id="a", kind="bash", payload={"cmd": "false"}),
        Node(id="b", kind="bash", payload={"cmd": "echo should-not-run"}),
    ]
    edges = [Edge(src="a", dst="b")]
    ctx = run(dag, edges, job="unit", label="short")
    assert ctx["a"].ok is False
    assert ctx["a"].output["code"] != 0
    # b was never reached because the ok edge was skipped
    assert "b" not in ctx
    rows = _job_runs(tmp_path)
    node_ids = [r["extra"]["node_id"] for r in rows]
    assert "a" in node_ids
    assert "b" not in node_ids


def test_fail_branch_only_fires_on_failure() -> None:
    """An edge with ``when='fail'`` should only be traversed if the
    source node failed. Two cases: (1) ``a`` fails → ``cleanup`` runs
    via the fail edge; (2) ``a`` succeeds → ``cleanup`` does NOT run."""
    # case 1: a fails -> cleanup runs (via fail edge), chain ends.
    dag = [
        Node(id="a", kind="bash", payload={"cmd": "false"}),
        Node(id="cleanup", kind="bash", payload={"cmd": "echo cleaned"}),
    ]
    edges = [Edge(src="a", dst="cleanup", when="fail")]
    ctx = run(dag, edges, job="unit", label="fail-branch")
    assert ctx["a"].ok is False
    assert ctx["cleanup"].ok is True
    assert ctx["cleanup"].output["stdout"].strip() == "cleaned"

    # case 2: a succeeds -> cleanup must NOT run.
    dag2 = [
        Node(id="a", kind="bash", payload={"cmd": "true"}),
        Node(id="cleanup", kind="bash", payload={"cmd": "echo cleaned"}),
    ]
    edges2 = [Edge(src="a", dst="cleanup", when="fail")]
    ctx2 = run(dag2, edges2, job="unit", label="fail-branch-skip")
    assert ctx2["a"].ok is True
    assert "cleanup" not in ctx2


def test_always_branch_fires_regardless() -> None:
    """An edge with when='always' should run whether the source succeeded
    or failed."""
    dag = [
        Node(id="a", kind="bash", payload={"cmd": "true"}),
        Node(id="tail", kind="bash", payload={"cmd": "echo always"}),
    ]
    edges = [Edge(src="a", dst="tail", when="always")]
    ctx = run(dag, edges, job="unit", label="always")
    assert ctx["a"].ok is True
    assert ctx["tail"].ok is True
    assert ctx["tail"].output["stdout"].strip() == "always"


# ── Gate kind ────────────────────────────────────────────────────────────


def test_gate_node_evaluates_callable() -> None:
    def is_even(ctx):
        return ctx.get("count", 0) % 2 == 0

    dag = [
        Node(id="a", kind="bash", payload={"cmd": "echo 4"}),
        Node(id="check", kind="gate", payload={"fn": is_even}),
    ]
    edges = [Edge(src="a", dst="check")]
    ctx = run(dag, edges, job="unit", label="gate")
    # gate reads ctx but doesn't write the input; provide it via initial_ctx
    dag2 = [
        Node(id="a", kind="bash", payload={"cmd": "echo done"}),
        Node(id="check", kind="gate", payload={"fn": is_even}),
    ]
    ctx2 = run(dag2, edges, job="unit", label="gate", initial_ctx={"count": 4})
    assert ctx2["check"].ok is True


def test_gate_rejects_non_callable() -> None:
    dag = [Node(id="g", kind="gate", payload={"fn": "not-callable"})]
    with pytest.raises(ValueError, match="must be callable"):
        run(dag, [], job="unit", label="gate-bad")


# ── Prompt kind (smoke) ─────────────────────────────────────────────────


def test_prompt_node_requires_model() -> None:
    dag = [Node(id="p", kind="prompt", model="", payload={"prompt": "hi"})]
    with pytest.raises(ValueError, match="empty model"):
        run(dag, [], job="unit", label="prompt-bad")


def test_prompt_node_requires_prompt_payload() -> None:
    dag = [Node(id="p", kind="prompt", model="kimi-k2.7-code:cloud", payload={})]
    with pytest.raises(ValueError, match="missing payload\\['prompt'\\]"):
        run(dag, [], job="unit", label="prompt-empty")


def test_subagent_node_merges_env_overrides(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")

        class Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Proc()

    monkeypatch.setattr("subprocess.run", fake_run)
    dag = [
        Node(
            id="agent",
            kind="subagent",
            model="kimi-k2.7-code:cloud",
            payload={
                "cli": "claude",
                "prompt": "hi",
                "env": {
                    "ANTHROPIC_BASE_URL": "https://ollama.com",
                    "ANTHROPIC_AUTH_TOKEN": "sk-test",
                },
            },
        )
    ]

    ctx = run(dag, [], job="unit", label="subagent-env")

    assert ctx["agent"].ok is True
    assert captured["argv"][:4] == ["claude", "-p", "--model", "kimi-k2.7-code:cloud"]
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "https://ollama.com"
    assert captured["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-test"
    assert "PATH" in captured["env"]


def test_subagent_failure_uses_stdout_when_stderr_is_empty(monkeypatch) -> None:
    class Proc:
        returncode = 1
        stdout = "Not logged in · Please run /login"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: Proc())
    dag = [
        Node(
            id="agent",
            kind="subagent",
            model="sonnet",
            payload={"cli": "claude", "prompt": "hi"},
        )
    ]

    ctx = run(dag, [], job="unit", label="subagent-stdout-error")

    assert ctx["agent"].ok is False
    assert "Not logged in" in (ctx["agent"].error or "")


# ── Retention kind: never blocks ────────────────────────────────────────


def test_retention_failure_does_not_propagate(monkeypatch, tmp_path) -> None:
    """If prune_old_trajectories raises, the retention node returns
    ok=true and the chain continues. This matches the design: retention
    failures shouldn't block the schedule."""

    def boom(**kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setitem(
        __import__("sys").modules,
        "ciao.trajectory_builder",
        type("M", (), {"prune_old_trajectories": staticmethod(boom)}),
    )
    # Re-import dag so the lazy import resolves to our fake module.
    import importlib
    from ciao import dag as dag_mod
    importlib.reload(dag_mod)

    dag = [Node(id="retain", kind="retention", payload={"months": 6})]
    ctx = dag_mod.run(dag, [], job="unit", label="retention-ok")
    assert ctx["retain"].ok is True
    # chain to a bash node that must still run
    dag2 = [
        Node(id="retain", kind="retention", payload={"months": 6}),
        Node(id="done", kind="bash", payload={"cmd": "echo ok"}),
    ]
    edges2 = [Edge(src="retain", dst="done", when="always")]
    ctx2 = dag_mod.run(dag2, edges2, job="unit", label="retention-then-bash")
    assert ctx2["done"].ok is True


# ── BASH: timeout, command shape ─────────────────────────────────────────


def test_bash_timeout_marks_node_failed(tmp_path: Path) -> None:
    """A bash timeout yields ``ok=False`` (does not raise). The run is
    recorded in job_runs with status ``error`` and the timeout message
    propagated into the row's ``error`` field, so the Automation page
    shows a red row instead of a silent green one."""
    dag = [Node(id="slow", kind="bash", payload={"cmd": "sleep 5"}, timeout_s=0.2)]
    ctx = run(dag, [], job="unit", label="timeout")
    assert ctx["slow"].ok is False
    assert "timeout" in (ctx["slow"].error or "").lower()
    rows = _job_runs(tmp_path)
    slow = next(r for r in rows if r["extra"]["node_id"] == "slow")
    # The runner must flip handle.status to "error" when a node returns
    # NodeResult(ok=False) without raising. Regression for the
    # dag-subagent-status silent-success bug.
    assert slow["status"] == "error"
    assert "timeout" in (slow.get("error") or "").lower()


def test_non_raising_failed_noderesult_marks_job_run_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node that returns ``NodeResult(ok=False, ...)`` without raising
    (the subagent executor's silent-failure path) must still record a
    ``status="error"`` row in job_runs with the error message propagated.
    Regression for dag-subagent-status: previously the row stayed green
    while the node had actually failed."""

    def fail_silently(node, ctx):
        return NodeResult(ok=False, error="subagent exit 1: drift detected")

    monkeypatch.setitem(dag_mod._EXECUTORS, "fake-fail", fail_silently)
    dag = [Node(id="gate", kind="fake-fail")]
    ctx = run(dag, [], job="unit", label="silent-fail")
    assert ctx["gate"].ok is False
    assert ctx["gate"].error == "subagent exit 1: drift detected"
    rows = _job_runs(tmp_path)
    gate = next(r for r in rows if r["extra"]["node_id"] == "gate")
    assert gate["status"] == "error"
    assert "drift detected" in (gate.get("error") or "")


def test_failed_gate_records_output_as_error(tmp_path: Path) -> None:
    """A gate returning ``(False, reason)`` carries its reason in
    ``output``, not ``error``. The recorded job_run must surface that
    reason instead of a blank ``error: null`` row. Regression for the
    depcheck:write_baseline triage gap (gate failed with no message)."""

    def reject(ctx):
        return False, "no research output to persist"

    dag = [Node(id="write_baseline", kind="gate", payload={"fn": reject})]
    ctx = run(dag, [], job="unit", label="depcheck")
    assert ctx["write_baseline"].ok is False
    rows = _job_runs(tmp_path)
    row = next(r for r in rows if r["extra"]["node_id"] == "write_baseline")
    assert row["status"] == "error"
    assert "no research output to persist" in (row.get("error") or "")


def test_bash_missing_cmd_raises() -> None:
    dag = [Node(id="x", kind="bash", payload={})]
    with pytest.raises(ValueError, match="missing payload\\['cmd'\\]"):
        run(dag, [], job="unit", label="bash-no-cmd")


# ── job_runs integration ────────────────────────────────────────────────


def test_per_node_rows_have_model_and_provider(tmp_path) -> None:
    dag = [
        Node(id="a", kind="bash", payload={"cmd": "true"}),
        Node(id="b", kind="bash", payload={"cmd": "true", "model": "kimi-k2.7-code:cloud"}),
    ]
    edges = [Edge(src="a", dst="b")]
    run(dag, edges, job="skillevo", label="int")
    rows = _job_runs(tmp_path)
    assert len(rows) == 2
    for r in rows:
        assert r["provider"] == "dag"
        assert r["category"] == "content"
        assert r["extra"]["dag"] == "int"


# ── Initial ctx propagation ─────────────────────────────────────────────


def test_initial_ctx_available_to_gate() -> None:
    seen: list[dict] = []

    def check(ctx):
        seen.append(dict(ctx))
        return True

    dag = [Node(id="g", kind="gate", payload={"fn": check})]
    run(dag, [], job="unit", label="init-ctx", initial_ctx={"seed": 42})
    assert seen and seen[0].get("seed") == 42
    # The gate also wrote its own result into ctx after the run
    # (verified by a follow-up check that ctx['g'] is set)
    ctx = run(dag, [], job="unit", label="init-ctx-2", initial_ctx={"seed": 7})
    assert ctx["g"].ok is True
