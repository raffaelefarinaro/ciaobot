from __future__ import annotations

import json
from pathlib import Path

from ciao.skills_inventory import build_skill_inventory


def _write_skill(root: Path, name: str, description: str = "") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"# {name}\n",
        encoding="utf-8",
    )


def _write_raw_skill(root: Path, name: str, frontmatter: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\n{frontmatter}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_build_skill_inventory_labels_custom_and_stock_sources(tmp_path: Path) -> None:
    _write_skill(tmp_path / "skills", "airtable-projects", "Create Airtable projects")
    _write_skill(tmp_path / ".claude" / "skills", "airtable-projects", "Installed custom")

    inventory = build_skill_inventory(tmp_path)

    assert inventory["counts"] == {"custom": 1, "stock": 0}
    assert inventory["skills"] == [
        {
            "name": "airtable-projects",
            "label": "custom",
            "source": "skills/",
            "source_type": "custom",
            "description": "Create Airtable projects",
            "path": "skills/airtable-projects/SKILL.md",
            "content": "---\nname: airtable-projects\ndescription: Create Airtable projects\n---\n\n# airtable-projects\n",
            "installed_targets": ["claude", "opencode"],
        },
    ]


def test_build_skill_inventory_reads_yaml_block_descriptions(tmp_path: Path) -> None:
    _write_raw_skill(
        tmp_path / "skills",
        "usage-report",
        "description: |\n  Generate monthly product usage reports.\n  Pulls data from BigQuery.",
    )

    inventory = build_skill_inventory(tmp_path)

    assert inventory["skills"][0]["description"] == (
        "Generate monthly product usage reports. Pulls data from BigQuery."
    )


def test_build_skill_inventory_reports_supported_install_targets(tmp_path: Path) -> None:
    _write_skill(tmp_path / "skills", "demo", "Demo")
    _write_skill(tmp_path / ".claude" / "skills", "demo", "Demo")
    _write_skill(tmp_path / ".agents" / "skills", "demo", "Demo")

    inventory = build_skill_inventory(tmp_path)

    assert inventory["skills"][0]["installed_targets"] == ["claude", "opencode"]


def test_build_skill_inventory_can_omit_skill_content(tmp_path: Path) -> None:
    _write_skill(tmp_path / "skills", "demo", "Demo")
    _write_skill(tmp_path / ".claude" / "skills", "demo", "Demo")

    inventory = build_skill_inventory(tmp_path, include_content=False)

    assert inventory["skills"][0]["description"] == "Demo"
    assert "content" not in inventory["skills"][0]


def test_build_skill_inventory_ignores_lock_file(tmp_path: Path) -> None:
    # skills-lock.json is inert after simplification; inventory must not list github skills
    tmp_path.joinpath("skills-lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": {
                    "brainstorming": {
                        "source": "owner/repo",
                        "sourceType": "github",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    inventory = build_skill_inventory(tmp_path)
    assert inventory["counts"] == {"custom": 0, "stock": 0}
    assert inventory["skills"] == []


def test_build_skill_inventory_reports_custom_over_stock(tmp_path: Path) -> None:
    _write_skill(tmp_path / "skills", "humanizer", "Local override")
    inventory = build_skill_inventory(tmp_path)
    assert inventory["counts"] == {"custom": 1, "stock": 0}
    assert inventory["skills"][0]["label"] == "custom"
    assert inventory["skills"][0]["source"] == "skills/"
