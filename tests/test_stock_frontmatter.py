from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import ciao
from ciao.frontmatter import split_frontmatter

STOCK = Path(ciao.__file__).parent / "stock"


def _stock_notes() -> list[Path]:
    return sorted(p for p in STOCK.rglob("*.md") if p.read_text(encoding="utf-8").startswith("---"))


@pytest.mark.parametrize("path", _stock_notes(), ids=lambda p: str(p.relative_to(STOCK)))
def test_every_stock_markdown_frontmatter_parses(path: Path) -> None:
    parts = split_frontmatter(path.read_text(encoding="utf-8"))
    assert parts is not None, f"{path} opens with --- but has no closing fence"
    if "{{" in parts[0]:
        pytest.skip("template placeholders")
    fm = yaml.safe_load(parts[0])
    assert isinstance(fm, dict), path
    if path.name == "SKILL.md":
        for key in ("name", "description"):
            assert isinstance(fm.get(key), str) and fm[key].strip(), f"{path}: {key}"
