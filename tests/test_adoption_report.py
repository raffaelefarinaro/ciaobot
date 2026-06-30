"""DAG structure + autocheck gate for the adoption-report pipeline.

The autocheck gate is the automated replacement for the skill's Phase-2h human
approval, so it gets the most coverage. Generation / BigQuery / Google Docs
steps need external systems and a real monthly run, so they are not exercised
here (structure-only)."""

from __future__ import annotations

import json
from pathlib import Path

from ciao.adoption_report import (
    PRODUCTS,
    VALID_COLORS,
    autocheck_enrichment,
    build_report_dag,
    strip_images_to_text,
)
from ciao.dag import _start_node, _validate


def test_dag_structure_is_valid() -> None:
    nodes, edges, _ = build_report_dag(month="2026-03")
    _validate(nodes, edges)
    assert _start_node(nodes, edges) == "generate"
    assert {n.id for n in nodes} == {
        "generate", "verify_reports", "strip_images",
        "cross_product", "enrich", "autocheck", "create_docs",
    }
    # autocheck only reaches create_docs on success (the human-gate stand-in).
    ac_edges = [e for e in edges if e.src == "autocheck"]
    assert len(ac_edges) == 1 and ac_edges[0].when == "ok" and ac_edges[0].dst == "create_docs"


def _write_enrichment(d: Path, slug: str, notes: dict, summary: str = "ok", charts: dict | None = None) -> None:
    (d / f"enrichment_{slug}.json").write_text(json.dumps({
        "summary": summary,
        "customer_notes": notes,
        "chart_comments": charts if charts is not None else {"Number of Customers": "grew"},
    }))


def _single_product(monkeypatch) -> dict:
    """Shrink PRODUCTS to one product so fixtures stay small. Patches both the
    module global and re-imports the reference used by autocheck."""
    import ciao.adoption_report as ar
    one = {"SparkScan": "sparkscan"}
    monkeypatch.setattr(ar, "PRODUCTS", one)
    return one


def test_autocheck_passes_on_clean_enrichment(tmp_path: Path, monkeypatch) -> None:
    products = _single_product(monkeypatch)
    (tmp_path / "report_text_sparkscan.md").write_text("Top customers: Acme Corp, Globex LLC")
    _write_enrichment(tmp_path, "sparkscan", {
        "Acme Corp": {"note": "renewed deal", "color": "green"},
        "Globex LLC": {"note": "trial churned", "color": "red"},
    })
    ok, msg = autocheck_enrichment(tmp_path, products=products)
    assert ok is True, msg
    assert "validated" in msg


def test_autocheck_flags_hallucinated_name(tmp_path: Path, monkeypatch) -> None:
    products = _single_product(monkeypatch)
    (tmp_path / "report_text_sparkscan.md").write_text("Top customers: Acme Corp")
    _write_enrichment(tmp_path, "sparkscan", {
        "Acme Corp": {"note": "ok", "color": "green"},
        "Nonexistent Inc": {"note": "made up", "color": "blue"},
    })
    ok, msg = autocheck_enrichment(tmp_path, products=products)
    assert ok is False
    assert "Nonexistent Inc" in msg


def test_autocheck_flags_bad_color_and_empty_note(tmp_path: Path, monkeypatch) -> None:
    products = _single_product(monkeypatch)
    (tmp_path / "report_text_sparkscan.md").write_text("Acme Corp and Beta Co")
    _write_enrichment(tmp_path, "sparkscan", {
        "Acme Corp": {"note": "ok", "color": "purple"},   # invalid color
        "Beta Co": {"note": "   ", "color": "green"},      # empty note
    })
    ok, msg = autocheck_enrichment(tmp_path, products=products)
    assert ok is False
    assert "invalid color" in msg
    assert "empty note" in msg


def test_autocheck_flags_missing_file_and_empty_summary(tmp_path: Path, monkeypatch) -> None:
    products = _single_product(monkeypatch)
    # No enrichment file at all.
    ok, msg = autocheck_enrichment(tmp_path, products=products)
    assert ok is False
    assert "missing enrichment file" in msg

    # Now an empty-summary file.
    (tmp_path / "report_text_sparkscan.md").write_text("Acme Corp")
    _write_enrichment(tmp_path, "sparkscan", {"Acme Corp": {"note": "x", "color": "green"}}, summary="  ")
    ok, msg = autocheck_enrichment(tmp_path, products=products)
    assert ok is False
    assert "empty or missing summary" in msg


def test_autocheck_against_real_fixtures() -> None:
    """The last real run's enrichment + report_text files live in the repo.
    They must pass the gate — a regression here means the gate is too strict
    for genuine output."""
    real_dir = Path("memory-vault/work/automations/adoption-report")
    if not (real_dir / "enrichment_sparkscan.json").exists():
        import pytest
        pytest.skip("real fixtures not present on this checkout")
    ok, msg = autocheck_enrichment(real_dir)
    assert ok is True, msg


def test_strip_images_replaces_base64(tmp_path: Path, monkeypatch) -> None:
    products = _single_product(monkeypatch)
    reports = tmp_path / "reports"
    (reports / "SparkScan_March 2026").mkdir(parents=True)
    (reports / "SparkScan_March 2026" / "report.md").write_text(
        "# Title\n| Customer |\n![chart](data:image/png;base64,AAAA)\nplain line\n"
    )
    out = tmp_path / "out"
    out.mkdir()
    written = strip_images_to_text(reports, out, "March 2026")
    assert written == ["sparkscan"]
    text = (out / "report_text_sparkscan.md").read_text()
    assert "[CHART IMAGE]" in text
    assert "base64," not in text
    assert "plain line" in text


def test_valid_colors_are_the_skill_set() -> None:
    assert VALID_COLORS == {"green", "red", "yellow", "blue", "orange"}
