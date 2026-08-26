from __future__ import annotations

import re
from pathlib import Path


# Every top-level ciao/*.py module must be indexed in the "App repo layout"
# block of docs/ARCHITECTURE.md, which the doc opens with ("Read this before
# making any change in ciao/"). The same one-line-per-module rule applies to
# ciao/web/*.py, so a new routes_*.py file has to earn its entry here before
# it can land. This mirrors tests/test_pwa_api_docs.py, which enforces that
# every API route appears in PWA_API.md.
def _layout_block(doc: str) -> str:
    match = re.search(
        r"^## App repo layout\n\n```\n(.*?)\n```\n", doc, re.DOTALL | re.MULTILINE
    )
    assert match, (
        "docs/ARCHITECTURE.md is missing the '## App repo layout' code block"
    )
    return match.group(1)


def _expected_modules(repo: Path) -> list[str]:
    modules = sorted(
        p.name for p in (repo / "ciao").glob("*.py") if p.name != "__init__.py"
    )
    modules += sorted(
        p.name
        for p in (repo / "ciao" / "web").glob("*.py")
        if p.name != "__init__.py"
    )
    return modules


def test_architecture_doc_indexes_every_ciao_module() -> None:
    repo = Path(__file__).resolve().parents[1]
    layout = _layout_block(
        (repo / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    )

    missing = [m for m in _expected_modules(repo) if m not in layout]

    assert missing == [], (
        "Modules absent from the 'App repo layout' block in "
        "docs/ARCHITECTURE.md (add one line each): "
        f"{missing}"
    )


# PR #328 made per-workspace memory curation consolidate bounded regions.
# The bounded-memory section of the doc kept the pre-#328 contract ("a region
# write the unattended nightly curator never performs"), contradicting both
# the schedules section and ciao/stock/schedules.json's own prompt. Readers
# reach for the bounded-memory section first, so pin the corrected contract
# and the guardrails that make the unattended write safe.
def test_architecture_doc_states_the_consolidation_contract() -> None:
    repo = Path(__file__).resolve().parents[1]
    doc = (repo / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")

    assert "the unattended nightly curator never performs" not in doc, (
        "docs/ARCHITECTURE.md still claims memory curation never writes the "
        "bounded regions; consolidation is exactly that write."
    )
    for fragment in (
        "~85% of the cap",  # the threshold that gates consolidation
        "Workspace/Memory-Consolidations.md",  # the undo log
        "the queue itself cannot apply a drop",  # the [review] escape hatch
        "Promoting NEW facts into a region stays user-reviewed",
    ):
        assert fragment in doc, (
            "docs/ARCHITECTURE.md no longer documents the memory-consolidation "
            f"guardrail: {fragment!r}"
        )
