"""DAG structure + gate behaviour for the depcheck pipeline."""

from __future__ import annotations

import json
from pathlib import Path

from ciao.dag import NodeResult, _start_node, _validate
from ciao.dependency_review import (
    TRACKED_TOOLS,
    _extract_json_block,
    _read_baseline,
    _resolve_model,
    build_review_dag,
)


def test_dag_structure_is_valid() -> None:
    """The DAG must validate (no dangling edges, no cycle) and have a single
    start node at read_baseline."""
    nodes, edges, _ = build_review_dag(
        baseline_path=Path("/nonexistent/baseline.json"),
        research_model="sonnet",
        research_timeout_s=10,
    )
    _validate(nodes, edges)  # raises on malformed DAG
    assert _start_node(nodes, edges) == "read_baseline"
    ids = {n.id for n in nodes}
    assert ids == {"read_baseline", "installed", "research", "write_baseline"}


def test_resolve_model_uses_ollama_sonnet_override(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("CIAO_OLLAMA_SONNET_MODEL", "kimi-k2.7-code:cloud")
    assert _resolve_model("sonnet") == "kimi-k2.7-code:cloud"
    assert _resolve_model("opus") == "opus"


def test_research_node_gets_ollama_routing_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("CIAO_OLLAMA_API_KEY", "sk-cloud")

    nodes, _edges, _holder = build_review_dag(
        baseline_path=tmp_path / ".runtime" / "dependency_baseline.json",
        research_model="kimi-k2.7-code:cloud",
        research_timeout_s=10,
    )
    research = next(n for n in nodes if n.id == "research")
    env = research.payload["env"]

    assert env["ANTHROPIC_BASE_URL"] == "https://ollama.com"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-cloud"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "kimi-k2.7-code:cloud"


def test_read_baseline_missing_file_is_empty(tmp_path: Path) -> None:
    base = _read_baseline(tmp_path / "missing.json")
    assert base == {"_meta": {}, "tools": {}}


def test_extract_json_block_handles_fenced_and_bare() -> None:
    fenced = 'preamble\n```json\n{"tools": {"openai": {"version": "9.9"}}}\n```\ntail'
    assert _extract_json_block(fenced) == {"tools": {"openai": {"version": "9.9"}}}
    bare = 'noise {"tools": {}, "summary": "ok"} more'
    assert _extract_json_block(bare) == {"tools": {}, "summary": "ok"}
    assert _extract_json_block("no json here") is None


def test_write_gate_merges_and_persists(tmp_path: Path) -> None:
    """The write gate parses research output, carries forward unseen tools,
    and writes the merged baseline to disk."""
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({
        "_meta": {"created": "2026-06-01"},
        "tools": {"openai": {"version": "1.0.0", "notes": "old"}},
    }))

    nodes, edges, holder = build_review_dag(
        baseline_path=baseline_path,
        research_model="sonnet",
        research_timeout_s=10,
    )
    write_fn = next(n for n in nodes if n.id == "write_baseline").payload["fn"]
    read_fn = next(n for n in nodes if n.id == "read_baseline").payload["fn"]

    # Simulate the pipeline up to write: read first, then a research result.
    read_fn({})
    research_output = (
        '```json\n'
        '{"tools": {"openai": {"version": "2.0.0", "release_date": "2026-06-20",'
        ' "notes": "new client"}}, "summary": "openai bumped"}\n'
        '```'
    )
    ctx = {"research": NodeResult(ok=True, output=research_output)}
    ok, msg = write_fn(ctx)

    assert ok is True
    written = json.loads(baseline_path.read_text())
    # Updated tool reflects new version.
    assert written["tools"]["openai"]["version"] == "2.0.0"
    # Summary recorded in meta.
    assert written["_meta"]["last_reviewed_summary"] == "openai bumped"
    assert holder["written"] is not None


def test_write_gate_rejects_unparseable_research(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    nodes, _edges, _holder = build_review_dag(
        baseline_path=baseline_path,
        research_model="sonnet",
        research_timeout_s=10,
    )
    write_fn = next(n for n in nodes if n.id == "write_baseline").payload["fn"]
    ok, msg = write_fn({"research": NodeResult(ok=True, output="sorry, no JSON")})
    assert ok is False
    # Baseline file must NOT be created from garbage research.
    assert not baseline_path.exists()


def test_research_prompt_survives_executor_format_map() -> None:
    """Regression: the subagent executor runs prompt.format_map(SafeFormatDict)
    on the node prompt. Literal JSON braces in the prompt must be escaped so
    this is a no-op instead of crashing with 'Max string recursion exceeded'.
    After format_map the model must see single-brace JSON."""
    from ciao.dag import _SafeFormatDict

    nodes, _edges, _holder = build_review_dag(
        baseline_path=Path("/nonexistent/baseline.json"),
        research_model="sonnet",
        research_timeout_s=10,
    )
    prompt = next(n for n in nodes if n.id == "research").payload["prompt"]
    rendered = prompt.format_map(_SafeFormatDict({}))  # must not raise
    assert '"tools": {' in rendered  # single braces reach the model
    assert "{{" not in rendered


def test_every_tracked_tool_has_repo() -> None:
    for t in TRACKED_TOOLS:
        assert t["repo"].startswith("https://github.com/")
        assert t["key"]
