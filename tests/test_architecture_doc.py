from __future__ import annotations

import re
from pathlib import Path


# Modules being deleted by the parallel legacy-control-surface removal (arch
# review finding 7). ARCHITECTURE.md no longer describes legacy as a supported
# surface, so these have no index entry while they still exist on disk. Remove
# them from this set (and re-add their lines to the doc) only if the deletion
# is abandoned.
_RETIRED_MODULES = {
    "control_surfaces.py",
    "control_surface_benchmark.py",
}


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

    missing = [
        m for m in _expected_modules(repo)
        if m not in layout and m not in _RETIRED_MODULES
    ]

    assert missing == [], (
        "Modules absent from the 'App repo layout' block in "
        "docs/ARCHITECTURE.md (add one line each): "
        f"{missing}"
    )
