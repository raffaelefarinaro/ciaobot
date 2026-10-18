from __future__ import annotations

import re
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


STOCK_SKILLS = STOCK / "skills"
DESCRIPTION_LIMIT = 1024
INDEX_LIMIT = 10 * 1024
STALE_NAMES = ("ciao index", "ciao lint", "Memory curation", "host & client", "Proposals panel")


def _stock_skill_files() -> list[Path]:
    return sorted(p for p in STOCK_SKILLS.rglob("*.md"))


@pytest.mark.parametrize(
    "path",
    sorted(STOCK_SKILLS.glob("*/SKILL.md")),
    ids=lambda p: str(p.relative_to(STOCK)),
)
def test_every_stock_skill_description_fits_the_agent_skills_limit(path: Path) -> None:
    parts = split_frontmatter(path.read_text(encoding="utf-8"))
    assert parts is not None
    description = yaml.safe_load(parts[0])["description"]
    assert len(description) <= DESCRIPTION_LIMIT, f"{path}: {len(description)} chars"


def test_stock_skills_use_no_stale_command_or_ui_names() -> None:
    for path in _stock_skill_files():
        text = path.read_text(encoding="utf-8")
        for name in STALE_NAMES:
            assert name not in text, f"{path.relative_to(STOCK)} still says {name!r}"
        assert "proposals panel" not in text.lower(), path


def test_capabilities_index_is_small_and_every_named_reference_ships() -> None:
    root = STOCK_SKILLS / "ciao-capabilities"
    index = (root / "SKILL.md").read_text(encoding="utf-8")
    assert len(index.encode("utf-8")) < INDEX_LIMIT
    named = set(re.findall(r"`(references/[a-z0-9-]+\.md)`", index))
    assert named, "the index names no reference file"
    for rel in named:
        assert (root / rel).is_file(), rel
    on_disk = {f"references/{p.name}" for p in (root / "references").glob("*.md")}
    assert on_disk == named, "every reference file must be named in the index"


def test_capabilities_references_have_no_spliced_sentences() -> None:
    root = STOCK_SKILLS / "ciao-capabilities"
    for path in [root / "SKILL.md", *sorted((root / "references").glob("*.md"))]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"[a-z]\.[A-Z][a-z]+ ", re.sub(r"`[^`]*`|\.md|e\.g\.|i\.e\.", "", text)), path
