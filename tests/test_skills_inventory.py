from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.skills_inventory import build_skill_inventory
from ciao.web.routes_api import admin_skills


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


def _rerooted_config(install: Path) -> SimpleNamespace:
    """A re-rooted install: one agent root per workspace, plus the install root."""
    names = ("personal", "work")
    roots = {name: install / name for name in names}
    return SimpleNamespace(
        workspace_root=install,
        agent_root=lambda name: roots[name],
        workspace=lambda name: roots.get(str(name)),
        agent_root_targets=lambda: [(roots[n], n) for n in names],
    )


def _skills_client(config: SimpleNamespace) -> TestClient:
    app = Starlette(routes=[Route("/api/admin/skills", admin_skills, methods=["GET"])])
    app.state.config = config
    return TestClient(app)


def test_the_skills_route_is_scoped_to_one_workspace(tmp_path: Path) -> None:
    """A workspace's tab lists that workspace's own catalog.

    Without the parameter the route stays merged across every root, which is
    what the audit and the CLI want; the PWA passes a workspace, so what it
    shows is what it can edit.
    """
    install = tmp_path / "install"
    install.mkdir()
    _write_skill(install / "personal" / "skills", "journal", "Personal journal")
    _write_skill(install / "work" / "skills", "invoice", "Work invoice")
    client = _skills_client(_rerooted_config(install))

    personal = client.get("/api/admin/skills?workspace=personal").json()
    work = client.get("/api/admin/skills?workspace=work").json()
    merged = client.get("/api/admin/skills").json()

    assert [s["name"] for s in personal["skills"]] == ["journal"]
    assert [s["name"] for s in work["skills"]] == ["invoice"]
    assert sorted(s["name"] for s in merged["skills"]) == ["invoice", "journal"]


def test_a_workspace_scoped_skills_list_still_shows_built_ins(tmp_path: Path) -> None:
    """Stock skills are installed into EVERY root, so scoping hides nothing.

    This is why the earlier merge existed (reading one root reported 0 custom
    on a migrated install). Reading the workspace's own root instead of the
    bare install root is what fixes that case, and stock skills stay visible
    because `sync-skills` writes them per root.
    """
    install = tmp_path / "install"
    install.mkdir()
    # A stock skill is an installed copy carrying the sync marker, so the
    # fixture writes that marker rather than a plain folder.
    stock_name = "web-research"
    for name in ("personal", "work"):
        skill_dir = install / name / ".claude" / "skills" / stock_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {stock_name}\ndescription: Stock\n---\n\n# {stock_name}\n",
            encoding="utf-8",
        )
        (skill_dir / ".ciao-stock-skill").write_text("", encoding="utf-8")
    client = _skills_client(_rerooted_config(install))

    rows = client.get("/api/admin/skills?workspace=work").json()["skills"]

    assert stock_name in [row["name"] for row in rows]
    assert [row["label"] for row in rows if row["name"] == stock_name] == ["stock"]


def test_an_unknown_workspace_falls_back_to_the_merged_list(tmp_path: Path) -> None:
    """A read degrades instead of erroring on a name that resolves to no root."""
    install = tmp_path / "install"
    install.mkdir()
    _write_skill(install / "personal" / "skills", "journal", "Personal journal")
    client = _skills_client(_rerooted_config(install))

    rows = client.get("/api/admin/skills?workspace=ghost").json()["skills"]

    assert [s["name"] for s in rows] == ["journal"]
