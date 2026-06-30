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


def test_build_skill_inventory_labels_custom_and_github_sources(tmp_path: Path) -> None:
    _write_skill(tmp_path / "skills", "airtable-projects", "Create Airtable projects")
    _write_skill(tmp_path / ".claude" / "skills", "airtable-projects", "Installed custom")
    _write_skill(tmp_path / ".claude" / "skills", "brainstorming", "Installed GitHub")

    tmp_path.joinpath("skills-lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": {
                    "brainstorming": {
                        "source": "obra/superpowers",
                        "sourceType": "github",
                        "skillPath": "skills/brainstorming/SKILL.md",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    inventory = build_skill_inventory(tmp_path, pi_skills_dir=tmp_path / "pi-skills")

    assert inventory["counts"] == {"custom": 1, "github": 1}
    assert inventory["skills"] == [
        {
            "name": "airtable-projects",
            "label": "custom",
            "source": "skills/",
            "source_type": "custom",
            "description": "Create Airtable projects",
            "installed_targets": ["claude"],
        },
        {
            "name": "brainstorming",
            "label": "github",
            "source": "obra/superpowers",
            "source_type": "github",
            "description": "Installed GitHub",
            "installed_targets": ["claude"],
        },
    ]


def test_build_skill_inventory_reads_yaml_block_descriptions(tmp_path: Path) -> None:
    _write_raw_skill(
        tmp_path / "skills",
        "adoption-report",
        "description: |\n  Generate monthly product adoption reports.\n  Pulls data from BigQuery.",
    )

    inventory = build_skill_inventory(tmp_path, pi_skills_dir=tmp_path / "pi-skills")

    assert inventory["skills"][0]["description"] == (
        "Generate monthly product adoption reports. Pulls data from BigQuery."
    )


def test_build_skill_inventory_dedupes_custom_over_lock_entry(tmp_path: Path) -> None:
    _write_skill(tmp_path / "skills", "humanizer", "Local override")
    tmp_path.joinpath("skills-lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": {
                    "humanizer": {"source": "blader/humanizer", "sourceType": "github"}
                },
            }
        ),
        encoding="utf-8",
    )

    inventory = build_skill_inventory(tmp_path, pi_skills_dir=tmp_path / "pi-skills")

    assert inventory["counts"] == {"custom": 1, "github": 0}
    assert inventory["skills"][0]["label"] == "custom"
    assert inventory["skills"][0]["source"] == "skills/"
