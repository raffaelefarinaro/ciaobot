"""Weekly dependency-changelog review as a DAG pipeline.

Replaces the hand-rolled ``sched-depcheck`` prompt with a deterministic
:mod:`ciao.dag` pipeline so the run gets per-node timing in the Automation
page and a *gated* baseline write (the old prompt did the write agentically,
so a flaky turn could skip it and silently drift the baseline).

Shape (sequential, because :mod:`ciao.dag` has no fan-out):

    read_baseline ──always──▶ installed ──always──▶ research ──ok──▶ write_baseline
                                                        └─fail─▶ (stop; baseline untouched)

The 7-way release research stays parallel by living *inside* one ``subagent``
node: the spawned ``claude -p`` process fans out its own Agent calls, one per
repo, exactly like the old prompt. The DAG isolates the deterministic
edges around it (load the baseline, persist the merged baseline) so a research
hiccup can't corrupt ``.runtime/dependency_baseline.json``.

Trigger: ``python3 -m ciao.dependency_review`` (the schedule prompt runs this,
mirroring how ``sched-skillevo`` invokes ``ciao.skill_evolution``).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any

from ciao.dag import Edge, Node, run as run_dag

logger = logging.getLogger(__name__)

_DEFAULT_BASELINE = Path(".runtime/dependency_baseline.json")

# Tools we track. ``repo`` is the GitHub releases page the research node reads;
# ``installed`` is the shell snippet that prints the locally-installed version
# (reference only: the baseline, not the install, is the comparison point
# because dependency installs are no longer changed automatically on startup).
TRACKED_TOOLS: tuple[dict[str, str], ...] = (
    {"key": "openai", "repo": "https://github.com/openai/openai-python/releases"},
    {"key": "claude-agent-sdk", "repo": "https://github.com/anthropics/claude-agent-sdk-python/releases"},
    {"key": "notebooklm-py", "repo": "https://github.com/teng-lin/notebooklm-py/releases"},
    {"key": "gws", "repo": "https://github.com/googleworkspace/cli/releases"},
    {"key": "playwright", "repo": "https://github.com/microsoft/playwright-python/releases"},
    {"key": "defuddle", "repo": "https://github.com/kepano/defuddle/releases"},
    {"key": "claude-code", "repo": "https://github.com/anthropics/claude-code/releases"},
)

_INSTALLED_CMD = [
    "bash",
    "-lc",
    "pip show openai claude-agent-sdk notebooklm-py playwright 2>/dev/null "
    "| grep -E '^(Name|Version):' || true; "
    "gws --version 2>/dev/null || true; "
    "defuddle --version 2>/dev/null || true; "
    "claude --version 2>/dev/null || true",
]


def _resolve_model(requested: str) -> str:
    """Map a tier alias to the real model id for the current host.

    When ``CIAO_OLLAMA_SONNET_MODEL`` is set (the personal-workspace
    default routes "sonnet" to an Ollama-cloud model id like
    ``kimi-k2.7-code:cloud``) and the caller asked for ``"sonnet"``,
    return that id instead. The bundled CLI's internal ``sonnet`` alias
    resolves to ``claude-sonnet-4-6``, which the Ollama proxy doesn't
    serve, so passing the literal alias fails. This indirection lets
    ciao schedule depcheck with ``--model sonnet`` and still reach the
    configured Ollama tier. Operators running against real Anthropic
    leave the env var unset and the alias passes through unchanged.
    """
    if requested != "sonnet":
        return requested
    import os

    override = os.environ.get("CIAO_OLLAMA_SONNET_MODEL", "").strip()
    return override or requested


def _read_baseline(path: Path) -> dict[str, Any]:
    """Load the persistent baseline. Missing file → empty baseline (first
    run treats all versions as unknown), matching the old prompt's step 1."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {"_meta": {}, "tools": {}}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("baseline unreadable (%s); treating as empty", exc)
        return {"_meta": {}, "tools": {}}


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model response. Tolerates a
    ```json fenced block or a bare object; returns None if neither parses."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if fenced:
        candidates.append(fenced.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _build_research_prompt(baseline: dict[str, Any], first_run: bool) -> str:
    """Prompt for the research subagent. It fans out one web lookup per repo
    (in parallel, its own Agent calls), compares against the baseline version,
    and returns strict JSON the write gate can persist."""
    tools_lines = []
    base_tools = baseline.get("tools", {})
    for t in TRACKED_TOOLS:
        bv = base_tools.get(t["key"], {}).get("version", "unknown")
        tools_lines.append(f"- {t['key']}: baseline={bv} | releases: {t['repo']}")
    tools_block = "\n".join(tools_lines)
    depth = "the last 2 releases" if first_run else "releases newer than the baseline version"
    rendered = f"""Dependency changelog review. Compare each tool's GitHub releases against our tracked BASELINE version (NOT the installed version).

Tracked tools:
{tools_block}

For each tool, dispatch a parallel web lookup of its releases page and inspect {depth}. For each notable release capture: version, breaking changes, new features relevant to a Python automation server using these SDKs/CLIs, and bug fixes affecting our usage. If nothing notable, say so.

Return ONLY a JSON object in a ```json fenced block with this exact shape:
{{
  "tools": {{
    "<tool-key>": {{
      "version": "<latest reviewed version>",
      "release_date": "<YYYY-MM-DD or empty>",
      "notes": "<one-line summary, or 'Nothing notable.'>",
      "actionable": "<a concrete next step if any, else empty string>"
    }}
  }},
  "summary": "<2-3 sentence overall review>"
}}
Use the exact tool keys listed above. Include every tracked tool, even when nothing changed (carry the baseline version forward)."""
    # The dag subagent/prompt executors run prompt.format_map() against ctx,
    # which would treat the literal JSON braces above as format fields and
    # crash ("Max string recursion exceeded"). This prompt is fully rendered
    # already (no exec-time placeholders), so escape every brace; format_map
    # halves them back to single braces before the model sees them.
    return rendered.replace("{", "{{").replace("}", "}}")


def build_review_dag(
    *,
    baseline_path: Path,
    research_model: str,
    research_timeout_s: float,
) -> tuple[list[Node], list[Edge], dict[str, Any]]:
    """Construct the depcheck DAG plus the mutable holder the gates write into.

    Returns ``(nodes, edges, holder)``. ``holder`` carries the loaded
    baseline and the merged-and-written result so the caller (and tests) can
    inspect them without re-reading disk."""
    holder: dict[str, Any] = {"baseline": {}, "written": None, "research_json": None}

    def read_baseline_node(ctx: dict[str, Any]) -> tuple[bool, str]:
        holder["baseline"] = _read_baseline(baseline_path)
        n = len(holder["baseline"].get("tools", {}))
        return True, f"baseline loaded: {n} tracked tool(s)"

    def write_baseline_node(ctx: dict[str, Any]) -> tuple[bool, str]:
        research = ctx.get("research")
        raw = getattr(research, "output", None) if research else None
        if not raw:
            return False, "no research output to persist"
        parsed = _extract_json_block(raw)
        if not parsed or "tools" not in parsed:
            return False, "research output missing parseable {tools: ...} JSON"
        holder["research_json"] = parsed
        baseline = holder["baseline"] or {"_meta": {}, "tools": {}}
        baseline.setdefault("tools", {})
        for key, info in parsed["tools"].items():
            if not isinstance(info, dict):
                continue
            existing = baseline["tools"].get(key, {})
            merged = {
                "version": info.get("version", existing.get("version", "unknown")),
                "release_date": info.get("release_date", existing.get("release_date", "")),
                "notes": info.get("notes") or existing.get("notes", ""),
            }
            baseline["tools"][key] = merged
        baseline.setdefault("_meta", {})
        baseline["_meta"]["last_reviewed_summary"] = parsed.get("summary", "")
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(baseline, indent=2) + "\n")
        holder["written"] = baseline
        return True, f"baseline written: {len(baseline['tools'])} tool(s)"

    first_run = not bool(_read_baseline(baseline_path).get("tools"))
    research_prompt = _build_research_prompt(_read_baseline(baseline_path), first_run)

    nodes: list[Node] = [
        Node(id="read_baseline", kind="gate", payload={"fn": read_baseline_node}),
        Node(id="installed", kind="bash", timeout_s=60, payload={"cmd": _INSTALLED_CMD}),
        Node(
            id="research",
            kind="subagent",
            model=research_model,
            timeout_s=research_timeout_s,
            payload={"prompt": research_prompt},
        ),
        Node(id="write_baseline", kind="gate", payload={"fn": write_baseline_node}),
    ]
    edges: list[Edge] = [
        # installed is reference-only: continue even if a version probe fails.
        Edge(src="read_baseline", dst="installed", when="always"),
        Edge(src="installed", dst="research", when="always"),
        Edge(src="research", dst="write_baseline", when="ok"),
        # research fail → chain ends; baseline is left untouched.
    ]
    return nodes, edges, holder


def run_review(
    *,
    baseline_path: Path | None = None,
    research_model: str = "sonnet",
    research_timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Build and run the depcheck DAG. Returns the holder (loaded baseline,
    parsed research JSON, written baseline)."""
    baseline_path = baseline_path or _DEFAULT_BASELINE
    nodes, edges, holder = build_review_dag(
        baseline_path=baseline_path,
        research_model=_resolve_model(research_model),
        research_timeout_s=research_timeout_s,
    )
    run_dag(nodes, edges, job="dependency_review", label="depcheck")
    return holder


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Weekly dependency changelog review (DAG pipeline).",
    )
    parser.add_argument("--baseline", type=Path, default=_DEFAULT_BASELINE)
    parser.add_argument(
        "--model",
        default="sonnet",
        help=(
            "model for the research subagent (claude -p). The literal "
            "'sonnet' is rewritten to $CIAO_OLLAMA_SONNET_MODEL when set, "
            "so scheduling with --model sonnet reaches the configured "
            "Ollama tier instead of the bundled CLI's sonnet-4.6 alias."
        ),
    )
    parser.add_argument("--timeout", type=float, default=600.0, help="research node timeout (s)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate + print the DAG structure without calling the model",
    )
    args = parser.parse_args(argv)
    resolved_model = _resolve_model(args.model)

    nodes, edges, _holder = build_review_dag(
        baseline_path=args.baseline,
        research_model=resolved_model,
        research_timeout_s=args.timeout,
    )
    if args.dry_run:
        # _validate raises on a malformed DAG (bad edge, cycle, no start).
        from ciao.dag import _start_node, _validate

        _validate(nodes, edges)
        start = _start_node(nodes, edges)
        print(f"depcheck DAG OK: {len(nodes)} nodes, {len(edges)} edges, start='{start}'")
        for n in nodes:
            print(f"  - {n.id} ({n.kind}{', model=' + n.model if n.model else ''})")
        for e in edges:
            print(f"  edge {e.src} --{e.when}--> {e.dst}")
        return 0

    holder = run_review(
        baseline_path=args.baseline,
        research_model=resolved_model,
        research_timeout_s=args.timeout,
    )
    summary = (holder.get("research_json") or {}).get("summary", "(no summary)")
    print(f"📦 Dependency review\n{summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
