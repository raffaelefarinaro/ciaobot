"""Monthly product adoption report as a DAG pipeline.

Lifts the agentic ``adoption-report`` skill into a :mod:`ciao.dag` pipeline so
the run gets per-node timing in the Automation page and, crucially, an
**automated quality gate** (`autocheck`) that replaces the skill's Phase 2h
"STOP HERE, wait for the user to approve the enrichment" step. The gate
validates the enrichment JSONs against the report text deterministically; if
it fails, the chain halts *before* docs are created, so a bad enrichment can't
silently go out.

Pipeline (sequential — :mod:`ciao.dag` has no fan-out):

    generate ─ok─▶ verify_reports ─ok─▶ strip_images ─ok─▶ cross_product
        ──always──▶ enrich ─ok─▶ autocheck ─ok─▶ create_docs

The chain stops on any step failure; an ``autocheck`` failure deliberately
halts before ``create_docs`` and prints what's wrong for a human to fix. The
run ends at ``create_docs``, which prints the paired source/target doc links —
the human still does the Phase-4 browser copy-paste (that's where the project
ends; the Docs API can't copy the rich content). The per-product enrichment
(name extraction, BigQuery/Salesforce, prev-month tabs, memory) stays agentic
inside the ``enrich`` subagent node, which reads the skill for the detailed
Phase-2 instructions rather than duplicating them here.

Trigger: ``python3 -m ciao.adoption_report --month YYYY-MM`` (mirrors how
``sched-skillevo`` invokes ``ciao.skill_evolution``).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

from ciao.dag import Edge, Node, run as run_dag

logger = logging.getLogger(__name__)

_AUTOMATION_DIR = Path("memory-vault/work/automations/adoption-report")
_CONFIG_PATH = _AUTOMATION_DIR / "config.json"
_SKILL_PATH = Path("skills/adoption-report/SKILL.md")

# Display name → file slug. Order is the report order.
PRODUCTS: dict[str, str] = {
    "Barcode Capture": "barcode_capture",
    "SparkScan": "sparkscan",
    "Barcode Selection": "barcode_selection",
    "Smart Label Capture": "smart_label_capture",
}

VALID_COLORS = frozenset({"green", "red", "yellow", "blue", "orange"})

_GEN_DONE_MARKER = "Completed automatic generation"
_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _month_label(month: str) -> str:
    """'2026-03' → 'March 2026' (the form used in report dir names + doc tabs)."""
    y, m = month.split("-")
    return f"{_MONTHS[int(m) - 1]} {y}"


def _load_config(config_path: Path) -> dict[str, Any]:
    return json.loads(config_path.read_text())


# ── deterministic, unit-tested steps ──────────────────────────────────────


def strip_images_to_text(reports_dir: Path, out_dir: Path, month_label: str) -> list[str]:
    """Phase 2a: report.md files are 2-3MB (base64 charts) and can't be read
    directly. Replace any base64 image line with a '[CHART IMAGE]' marker and
    write clean ``report_text_<slug>.md`` files. Returns the slugs written."""
    written: list[str] = []
    for product, slug in PRODUCTS.items():
        src = reports_dir / f"{product}_{month_label}" / "report.md"
        if not src.exists():
            logger.warning("report.md missing for %s (%s)", product, src)
            continue
        clean_lines = [
            "[CHART IMAGE]\n" if "base64," in line else line
            for line in src.read_text().splitlines(keepends=True)
        ]
        (out_dir / f"report_text_{slug}.md").write_text("".join(clean_lines))
        written.append(slug)
    return written


def _normalize(name: str) -> str:
    """Loose match key: casefold + collapse whitespace. Lets the autocheck
    tolerate spacing/case noise while still catching hallucinated names."""
    return re.sub(r"\s+", " ", name).strip().casefold()


def autocheck_enrichment(
    enrichment_dir: Path,
    *,
    products: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Automated replacement for the Phase 2h human approval gate.

    For each product, validate ``enrichment_<slug>.json`` against
    ``report_text_<slug>.md``:
      * file exists and is valid JSON
      * non-empty ``summary``
      * ``customer_notes`` is a dict; every entry has a non-empty ``note`` and
        a ``color`` in VALID_COLORS
      * every customer-note name actually appears in the report text (catches
        hallucinated / mis-cased names, which would silently fail to color the
        right cell downstream)
      * ``chart_comments`` is a dict with non-empty comments

    Returns ``(ok, message)``. ``ok`` is False if ANY product fails any check;
    the message enumerates every problem so a human can fix and rerun.
    """
    products = products or PRODUCTS
    problems: list[str] = []
    checked = 0

    for product, slug in products.items():
        ej = enrichment_dir / f"enrichment_{slug}.json"
        rt = enrichment_dir / f"report_text_{slug}.md"
        if not ej.exists():
            problems.append(f"[{slug}] missing enrichment file {ej.name}")
            continue
        try:
            data = json.loads(ej.read_text())
        except json.JSONDecodeError as exc:
            problems.append(f"[{slug}] enrichment is not valid JSON: {exc}")
            continue
        checked += 1

        if not str(data.get("summary", "")).strip():
            problems.append(f"[{slug}] empty or missing summary")

        report_norm = _normalize(rt.read_text()) if rt.exists() else ""
        notes = data.get("customer_notes", {})
        if not isinstance(notes, dict):
            problems.append(f"[{slug}] customer_notes is not an object")
            notes = {}
        for name, entry in notes.items():
            if not isinstance(entry, dict):
                problems.append(f"[{slug}] note for '{name}' is not an object")
                continue
            color = entry.get("color", "")
            if color not in VALID_COLORS:
                problems.append(f"[{slug}] '{name}' has invalid color '{color}'")
            if not str(entry.get("note", "")).strip():
                problems.append(f"[{slug}] '{name}' has empty note")
            if report_norm and _normalize(name) not in report_norm:
                problems.append(
                    f"[{slug}] customer '{name}' not found in report text "
                    "(hallucinated or mis-typed name?)"
                )

        charts = data.get("chart_comments", {})
        if not isinstance(charts, dict):
            problems.append(f"[{slug}] chart_comments is not an object")
        else:
            for title, comment in charts.items():
                if not str(comment).strip():
                    problems.append(f"[{slug}] chart '{title}' has empty comment")

    if checked == 0:
        problems.append("no enrichment files found at all")

    if problems:
        return False, f"autocheck FAILED ({len(problems)} issue(s)):\n- " + "\n- ".join(problems)
    return True, f"autocheck passed: {checked} product(s) validated"


# ── generation step (device-specific; not runnable without the generator) ──


def _run_generator(repo_path: Path, timeout_s: float) -> tuple[bool, str]:
    """Phase 1: start the report generator, wait for the completion marker on
    its output, then terminate it. Returns (ok, message). The generator repo
    only exists on the device that owns it; a missing repo is a clear, early
    failure rather than a confusing downstream one."""
    if not repo_path.exists():
        return False, (
            f"generator repo not found at {repo_path}; run this on the device "
            "that owns the report generator"
        )
    venv_py = repo_path / "venv" / "bin" / "python"
    runner = repo_path / "python" / "report_generator" / "run.py"
    if not venv_py.exists() or not runner.exists():
        return False, f"generator venv or run.py missing under {repo_path}"

    proc = subprocess.Popen(
        [str(venv_py), str(runner)],
        cwd=str(repo_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    deadline = time.monotonic() + timeout_s
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if _GEN_DONE_MARKER in line:
                return True, line.strip()
            if time.monotonic() > deadline:
                return False, f"generation timed out after {timeout_s}s"
        return False, "generator exited before printing the completion marker"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def _build_enrich_prompt(month: str, enrichment_dir: Path) -> str:
    """Prompt for the enrich subagent. Points it at the skill's Phase 2 rather
    than duplicating the 200-line spec, so the skill stays the single source of
    truth. Braces escaped for the dag executor's format_map (see depcheck)."""
    month_label = _month_label(month)
    slugs = ", ".join(PRODUCTS.values())
    rendered = f"""You are running Phase 2 (Enrich) of the monthly adoption report for {month_label}.

Read the skill first: {_SKILL_PATH} — follow its "Phase 2: Enrich Reports" section exactly (steps 2a-2g). Do NOT do Phase 1 (already done), Phase 2h (a separate automated check handles approval), or Phase 3+.

Working dir: {enrichment_dir} . The clean report_text_<slug>.md and cross_product_movements.json files are already there.

For EACH of the 4 products ({slugs}):
- extract customer names from report_text_<slug>.md (apply the skill's noise filters)
- query BigQuery/Salesforce for context (skill step 2b, runner + --limit 500)
- read the previous month's tab and agent memory for trends (steps 2c, 2e)
- fold in cross_product_movements.json findings (step 2d)
- write enrichment_<slug>.json with keys: "summary" (2-4 sentences), "customer_notes" ({{name: {{note, color}}}} with color in green/red/yellow/blue/orange), and "chart_comments" ({{chart_title: comment}}).

Customer-note names MUST match the report text exactly (an automated gate rejects names not found in the report). Follow the skill's note/summary/color rules. Write all 4 JSON files, then reply with a one-line-per-product confirmation. Do not ask questions; this is an unattended run."""
    return rendered.replace("{", "{{").replace("}", "}}")


def build_report_dag(
    *,
    month: str,
    config_path: Path = _CONFIG_PATH,
    automation_dir: Path = _AUTOMATION_DIR,
    enrich_model: str = "opus",
    gen_timeout_s: float = 600.0,
    enrich_timeout_s: float = 1800.0,
    docs_timeout_s: float = 600.0,
) -> tuple[list[Node], list[Edge], dict[str, Any]]:
    """Construct the adoption-report DAG. Returns ``(nodes, edges, holder)``."""
    config = _load_config(config_path)
    reports_dir = Path(config["report_generator_path"]) / config["report_output_dir"]
    repo_path = Path(config["report_generator_path"])
    month_label = _month_label(month)
    holder: dict[str, Any] = {"month": month, "links": None, "autocheck": None}

    def generate_node(ctx: dict[str, Any]) -> tuple[bool, str]:
        return _run_generator(repo_path, gen_timeout_s)

    def verify_reports_node(ctx: dict[str, Any]) -> tuple[bool, str]:
        missing = [
            p for p in PRODUCTS
            if not (reports_dir / f"{p}_{month_label}" / "report.md").exists()
        ]
        if missing:
            return False, f"report.md missing for: {', '.join(missing)}"
        return True, f"all 4 reports present for {month_label}"

    def strip_images_node(ctx: dict[str, Any]) -> tuple[bool, str]:
        written = strip_images_to_text(reports_dir, automation_dir, month_label)
        if len(written) != len(PRODUCTS):
            return False, f"stripped only {len(written)}/{len(PRODUCTS)} reports"
        return True, f"wrote report_text for {len(written)} products"

    def autocheck_node(ctx: dict[str, Any]) -> tuple[bool, str]:
        ok, msg = autocheck_enrichment(automation_dir)
        holder["autocheck"] = msg
        return ok, msg

    nodes: list[Node] = [
        Node(id="generate", kind="gate", timeout_s=gen_timeout_s, payload={"fn": generate_node}),
        Node(id="verify_reports", kind="gate", payload={"fn": verify_reports_node}),
        Node(id="strip_images", kind="gate", payload={"fn": strip_images_node}),
        Node(
            id="cross_product",
            kind="bash",
            timeout_s=300,
            payload={"cmd": [
                "python3", str(automation_dir / "cross_product_analysis.py"),
                "--report-dir", str(automation_dir),
            ]},
        ),
        Node(
            id="enrich",
            kind="subagent",
            model=enrich_model,
            timeout_s=enrich_timeout_s,
            payload={"prompt": _build_enrich_prompt(month, automation_dir)},
        ),
        Node(id="autocheck", kind="gate", payload={"fn": autocheck_node}),
        Node(
            id="create_docs",
            kind="bash",
            timeout_s=docs_timeout_s,
            payload={"cmd": [
                "python3", str(automation_dir / "create_report_docs.py"),
                "--month", month,
                "--config", str(config_path),
                "--enrichment-dir", str(automation_dir),
            ]},
        ),
    ]
    edges: list[Edge] = [
        Edge(src="generate", dst="verify_reports", when="ok"),
        Edge(src="verify_reports", dst="strip_images", when="ok"),
        Edge(src="strip_images", dst="cross_product", when="ok"),
        # cross-product analysis is an enrichment aid, not fatal: proceed even
        # if it errors (enrich degrades gracefully without cross-product notes).
        Edge(src="cross_product", dst="enrich", when="always"),
        Edge(src="enrich", dst="autocheck", when="ok"),
        # autocheck fail → chain ends BEFORE create_docs (the automated stand-in
        # for the old human approval gate). No docs from a bad enrichment.
        Edge(src="autocheck", dst="create_docs", when="ok"),
    ]
    return nodes, edges, holder


def run_report(
    *,
    month: str,
    config_path: Path = _CONFIG_PATH,
    enrich_model: str = "opus",
) -> dict[str, Any]:
    """Build and run the adoption-report DAG. Returns the holder. The create_docs
    node's stdout (paired links) lands in the ctx; we surface it on the holder."""
    nodes, edges, holder = build_report_dag(
        month=month, config_path=config_path, enrich_model=enrich_model,
    )
    ctx = run_dag(nodes, edges, job="adoption_report", label=f"adoption:{month}")
    create = ctx.get("create_docs")
    if create is not None and getattr(create, "ok", False):
        out = getattr(create, "output", None)
        holder["links"] = out.get("stdout") if isinstance(out, dict) else None
    return holder


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Monthly adoption report (DAG pipeline). Ends at the paired "
        "doc links; the human does the Phase-4 copy-paste.",
    )
    parser.add_argument("--month", help="YYYY-MM (default: current month)")
    parser.add_argument("--config", type=Path, default=_CONFIG_PATH)
    parser.add_argument("--model", default="opus", help="model for the enrich subagent")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="validate + print the DAG structure without running it",
    )
    parser.add_argument(
        "--autocheck", action="store_true",
        help="run only the enrichment autocheck gate (exit 1 on failure); "
        "the agentic skill calls this in place of the old human approval step",
    )
    args = parser.parse_args(argv)
    month = args.month or date.today().strftime("%Y-%m")

    if args.autocheck:
        ok, msg = autocheck_enrichment(_AUTOMATION_DIR)
        print(msg)
        return 0 if ok else 1

    nodes, edges, _holder = build_report_dag(
        month=month, config_path=args.config, enrich_model=args.model,
    )
    if args.dry_run:
        from ciao.dag import _start_node, _validate

        _validate(nodes, edges)
        start = _start_node(nodes, edges)
        print(f"adoption DAG OK ({month}): {len(nodes)} nodes, {len(edges)} edges, start='{start}'")
        for n in nodes:
            print(f"  - {n.id} ({n.kind}{', model=' + n.model if n.model else ''})")
        for e in edges:
            print(f"  edge {e.src} --{e.when}--> {e.dst}")
        return 0

    holder = run_report(month=month, config_path=args.config, enrich_model=args.model)
    print(f"📊 Adoption report {month}")
    print(holder.get("autocheck") or "(no autocheck record)")
    if holder.get("links"):
        print("\nPaired doc links (copy-paste each source into its target tab):\n")
        print(holder["links"])
    else:
        print("\nNo links produced (chain halted before create_docs — see autocheck above).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
