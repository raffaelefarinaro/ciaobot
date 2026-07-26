"""Tests for ciao/dag.py execution and checkpoint resumption."""

from __future__ import annotations

from pathlib import Path
from ciao.dag import Edge, Node, run


def test_dag_checkpointing_and_resumption(tmp_path: Path):
    executed_nodes: list[str] = []

    def gate_step1(ctx: dict) -> bool:
        executed_nodes.append("step1")
        return True

    def gate_step2(ctx: dict) -> bool:
        executed_nodes.append("step2")
        return True

    def gate_step3(ctx: dict) -> bool:
        executed_nodes.append("step3")
        return True

    dag = [
        Node(id="step1", kind="gate", payload={"fn": gate_step1}),
        Node(id="step2", kind="gate", payload={"fn": gate_step2}),
        Node(id="step3", kind="gate", payload={"fn": gate_step3}),
    ]

    edges = [
        Edge(src="step1", dst="step2"),
        Edge(src="step2", dst="step3"),
    ]

    run_id = "test_run_123"
    chk_dir = tmp_path / "checkpoints"

    # 1. Run full DAG initially
    res = run(dag, edges, run_id=run_id, checkpoint_dir=chk_dir)
    assert "step1" in res and res["step1"].ok
    assert "step2" in res and res["step2"].ok
    assert "step3" in res and res["step3"].ok
    assert executed_nodes == ["step1", "step2", "step3"]

    # Checkpoint file exists
    assert (chk_dir / f"{run_id}.json").is_file()

    # 2. Re-run DAG with same run_id: all steps already executed and should be skipped
    executed_nodes.clear()
    res2 = run(dag, edges, run_id=run_id, checkpoint_dir=chk_dir)
    assert "step1" in res2 and res2["step1"].ok
    assert "step2" in res2 and res2["step2"].ok
    assert "step3" in res2 and res2["step3"].ok
    assert executed_nodes == []  # All skipped!
