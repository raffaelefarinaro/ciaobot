from __future__ import annotations

import json
import tomllib
from importlib import resources
from pathlib import Path


EXPECTED_AGENTS = {
    "comment-analyzer.md",
    "doc-updater.md",
    "memory.md",
    "pr-test-analyzer.md",
    "researcher.md",
    "secretary.md",
    "silent-failure-hunter.md",
}

EXPECTED_COMMANDS = {
    "critique.md",
    "interrogation.md",
    "remember.md",
}

EXPECTED_SYSTEM_SCHEDULES = {
    "system-memory-curation",
    "system-skill-evolution",
    "system-error-triage",
    "system-weekly-review",
}

PRIVATE_MARKERS = {
    "PrivatePerson",
    "private-person",
    "private.example.com",
    "PrivateCo",
    "/Users/private",
}


def test_stock_package_contains_generic_agents_commands_and_schedules() -> None:
    stock = resources.files("ciao.stock")

    assert {path.name for path in stock.joinpath("agents").iterdir() if path.name.endswith(".md")} == EXPECTED_AGENTS
    assert {path.name for path in stock.joinpath("commands").iterdir() if path.name.endswith(".md")} == EXPECTED_COMMANDS
    assert stock.joinpath("skills").is_dir()
    assert not list(stock.joinpath("skills").glob("*.md"))
    assert stock.joinpath("public", "CLAUDE.md").is_file()
    assert stock.joinpath("workspace", "CLAUDE.md").is_file()
    assert stock.joinpath("workspace", "CIAO_CUSTOMIZATION.md").is_file()
    assert stock.joinpath("deploy", "com.ciao.server.plist.tmpl").is_file()
    assert stock.joinpath("schedules", "weekly-review-template.md").is_file()
    plist = stock.joinpath("deploy", "com.ciao.server.plist.tmpl").read_text(encoding="utf-8")
    assert "<string>ciao.cli</string>" in plist
    assert "<string>run</string>" in plist

    schedules = json.loads(stock.joinpath("schedules.json").read_text(encoding="utf-8"))
    assert {entry["schedule_id"] for entry in schedules["schedules"]} == EXPECTED_SYSTEM_SCHEDULES


def test_stock_schedules_are_read_only_system_entries() -> None:
    stock = resources.files("ciao.stock")
    schedules = json.loads(stock.joinpath("schedules.json").read_text(encoding="utf-8"))

    for entry in schedules["schedules"]:
        assert entry["scope"] == "system"
        assert entry["editable"] is False
        assert entry["removable"] is False
        assert entry["enabled"] is True
        assert entry["workspace"] == "default"
        assert "last_triggered_on" not in entry
        assert "last_dispatched_at" not in entry


def test_stock_package_has_no_private_markers() -> None:
    stock = Path(resources.files("ciao.stock"))
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in stock.rglob("*")
        if path.is_file() and path.suffix in {".md", ".json", ".tmpl"}
    )

    for marker in PRIVATE_MARKERS:
        assert marker not in text


def test_pyproject_packages_stock_data() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert "ciao.stock" in data["tool"]["setuptools"]["packages"]
    package_data = data["tool"]["setuptools"]["package-data"]
    assert "agents/*.md" in package_data["ciao.stock"]
    assert "commands/*.md" in package_data["ciao.stock"]
    assert "skills/.gitkeep" in package_data["ciao.stock"]
    assert "public/*.md" in package_data["ciao.stock"]
    assert "workspace/*.md" in package_data["ciao.stock"]
    assert "schedules.json" in package_data["ciao.stock"]
    assert "schedules/*.md" in package_data["ciao.stock"]
