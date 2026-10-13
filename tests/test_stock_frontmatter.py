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


@pytest.mark.parametrize(
    "path",
    [p for p in _stock_notes() if p.name == "SKILL.md"],
    ids=lambda p: str(p.relative_to(STOCK)),
)
def test_hand_readers_agree_with_yaml_for_stock_skills(path: Path) -> None:
    from ciao.skills_inventory import _read_frontmatter_description
    from ciao.web.commands import _parse_frontmatter

    text = path.read_text(encoding="utf-8")
    parts = split_frontmatter(text)
    assert parts is not None
    expected = yaml.safe_load(parts[0])["description"]
    assert _read_frontmatter_description(path) == expected
    assert _parse_frontmatter(text)["description"] == expected
