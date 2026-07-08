"""DAG structure + gate behaviour for the depcheck pipeline."""

from __future__ import annotations

import json
from pathlib import Path

from ciao.dag import NodeResult, _start_node, _validate
import ciao.dependency_review as depreview
from ciao.dependency_review import (
    AUTO_UPDATE_KEYS,
    TRACKED_TOOLS,
    _extract_json_block,
    _pinned_version,
    _read_baseline,
    _resolve_model,
    apply_auto_updates,
    build_review_dag,
    check_available_updates,
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


def test_pinned_version_extracts_from_python_and_npm_specs() -> None:
    assert _pinned_version("==0.2.111") == "0.2.111"
    assert _pinned_version("^5.0.0") == "5.0.0"
    assert _pinned_version("~1.2.3") == "1.2.3"
    assert _pinned_version("==0.4.0; platform_system == 'Darwin'") == "0.4.0"
    assert _pinned_version("*") is None


def _write_update_tree(root: Path) -> None:
    (root / "web").mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "ciao"\n'
        'version = "0.4.5"\n'
        "dependencies = [\n"
        '  "claude-agent-sdk==0.2.111",\n'
        '  "openai==2.44.0",\n'
        "]\n",
        encoding="utf-8",
    )
    (root / "web" / "package.json").write_text(
        json.dumps(
            {"name": "pwa", "version": "0.1.0", "dependencies": {"vue": "^3.5.0"}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_check_available_updates_flags_newer_and_auto(tmp_path: Path, monkeypatch) -> None:
    _write_update_tree(tmp_path)
    pypi = {"claude-agent-sdk": "0.2.200", "openai": "2.44.0"}
    npm = {"vue": "4.0.0"}
    monkeypatch.setattr(depreview, "get_latest_pypi_version", lambda n: pypi.get(n))
    monkeypatch.setattr(depreview, "get_latest_npm_version", lambda n: npm.get(n))

    updates = check_available_updates(tmp_path)
    by_key = {u.key: u for u in updates}

    # openai has no newer release, so it is omitted.
    assert "openai" not in by_key
    # claude-agent-sdk got a same-major bump and is flagged auto + safe.
    sdk = by_key["claude-agent-sdk"]
    assert (sdk.current, sdk.latest) == ("0.2.111", "0.2.200")
    assert sdk.auto is True and sdk.is_safe is True
    # vue jumped a major version: surfaced but not safe, not auto.
    vue = by_key["vue"]
    assert vue.auto is False and vue.is_safe is False


def test_apply_auto_updates_only_touches_auto_keys(tmp_path: Path, monkeypatch) -> None:
    _write_update_tree(tmp_path)
    pypi = {"claude-agent-sdk": "0.2.200", "openai": "3.0.0"}
    npm = {"vue": "4.0.0"}
    monkeypatch.setattr(depreview, "get_latest_pypi_version", lambda n: pypi.get(n))
    monkeypatch.setattr(depreview, "get_latest_npm_version", lambda n: npm.get(n))

    updates = check_available_updates(tmp_path)
    applied = apply_auto_updates(tmp_path, updates, reinstall=False)

    assert "claude-agent-sdk" in AUTO_UPDATE_KEYS
    assert applied == ["claude-agent-sdk (Python: 0.2.111 -> 0.2.200)"]
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert 'claude-agent-sdk==0.2.200' in pyproject
    # Non-auto deps are left untouched even though newer versions exist.
    assert 'openai==2.44.0' in pyproject
    assert '"vue": "^3.5.0"' in (tmp_path / "web" / "package.json").read_text(
        encoding="utf-8"
    )
