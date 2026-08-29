from __future__ import annotations

import json
import tomllib
from fnmatch import fnmatchcase
from importlib import resources
from pathlib import Path


EXPECTED_AGENTS = {
    "memory.md",
    "researcher.md",
    "secretary.md",
}

EXPECTED_COMMANDS = {
    "critique.md",
    "interrogation.md",
    "remember.md",
}

EXPECTED_SYSTEM_SCHEDULES = {
    "system-memory-curation",
    # The index rebuild folded INTO hygiene: after the re-rooting each agent root
    # owns its own INDEX.md + VOCABULARY.md, so there is no shared artifact left
    # for a global routine to write. What replaced it is the global half of the
    # audit, which stays single because its subject is the global runtime dir.
    "system-install-health",
    "system-workspace-hygiene",
    "system-skill-evolution",
    "system-reviews-hygiene",
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
    assert "<string>{{CIAO_EXECUTABLE}}</string>" in plist
    assert "{{LAUNCHD_PROGRAM_ARGUMENTS}}" in plist
    assert "<string>ciao.cli</string>" not in plist

    schedules = json.loads(stock.joinpath("schedules.json").read_text(encoding="utf-8"))
    assert {entry["schedule_id"] for entry in schedules["schedules"]} == EXPECTED_SYSTEM_SCHEDULES


def test_stock_curation_prompt_consolidation_contract() -> None:
    """The nightly curator consolidates regions itself, with guardrails.

    It used to be forbidden from touching regions, so an over-cap region
    dead-ended at "needs a human pass" and the user was always in the loop.
    The contract now: consolidate at/above ~85%, log every removal to the
    undo file, queue uncertain removals as [review] yes/no questions, never
    promote NEW facts unattended.
    """
    stock = resources.files("ciao.stock")
    schedules = json.loads(stock.joinpath("schedules.json").read_text(encoding="utf-8"))
    prompt = next(
        entry["prompt"]
        for entry in schedules["schedules"]
        if entry["schedule_id"] == "system-memory-curation"
    )

    # Consolidation is allowed, bounded by the undo log.
    assert "consolidate that region now" in prompt
    assert "Workspace/Memory-Consolidations.md" in prompt
    # Uncertain removals become reviewable questions, not deletions.
    assert "[review] Keep" in prompt
    assert "Memory-Proposals.md" in prompt
    # Promotion of new facts stays user-reviewed; over-cap has named options.
    assert "Do not promote new facts into the bounded" in prompt
    assert "CIAO_MEMORY_CHAR_LIMIT" in prompt


def test_stock_curation_prompt_files_discovered_bounded_facts() -> None:
    """A bounded-region fact found by reading transcripts must enter the queue.

    Chats without a session-insights section never ran archive-time routing,
    so the curator is the first to see their facts. Naming them only in the
    nightly reply left them with no review path: nothing to promote or
    dismiss, re-derived from scratch every run. The command example must
    keep both hazard sources out of the shell: the fact travels by file,
    and the source label is a plain chat id — $(), backticks, and quotes
    interpolate even inside double quotes.
    """
    stock = resources.files("ciao.stock")
    schedules = json.loads(stock.joinpath("schedules.json").read_text(encoding="utf-8"))
    prompt = next(
        entry["prompt"]
        for entry in schedules["schedules"]
        if entry["schedule_id"] == "system-memory-curation"
    )

    assert "ciao memory-proposal-add --kind memory --source <chat id> --text-file" in prompt
    assert "<chat title>" not in prompt
    assert "--text-file <fact-file>" in prompt
    assert "no review path" in prompt
    assert "dedupes" in prompt


def test_stock_memory_agent_role_matches_curator_contract() -> None:
    """The spawned memory agent must allow the same guarded consolidation.

    The curation schedule says \"Use the memory agent\", so if the role still
    forbade region writes the two instructions would cancel out.
    """
    role = resources.files("ciao.stock").joinpath("agents/memory.md").read_text(
        encoding="utf-8"
    )

    assert "~3000 memory / ~1375 profile" in role
    assert "Workspace/Memory-Consolidations.md" in role
    assert "[review] Keep" in role
    # New-fact promotion stays reviewed even though consolidation is allowed.
    assert "Promoting NEW facts into a region is a reviewed action" in role


def test_stock_workspace_guide_carries_default_caps() -> None:
    """Seeded guides must carry the shipped default caps, not stale ones."""
    stock = resources.files("ciao.stock")
    guide = stock.joinpath("workspace", "CLAUDE.md").read_text(encoding="utf-8")

    assert "<!-- ciao:memory:start cap=3000 -->" in guide
    assert "<!-- ciao:profile:start cap=1375 -->" in guide


def test_stock_schedules_are_read_only_system_entries() -> None:
    stock = resources.files("ciao.stock")
    schedules = json.loads(stock.joinpath("schedules.json").read_text(encoding="utf-8"))

    for entry in schedules["schedules"]:
        assert entry["scope"] == "system"
        assert entry["editable"] is False
        assert entry["removable"] is False
        assert entry["enabled"] is True
        # Packaged definitions name no workspace. The old "default" sentinel was
        # never a real workspace: the resolver fell through it to the primary
        # one, so every routine curated a single vault. A `per_workspace`
        # definition now gets a real workspace per fanned-out row, and a global
        # one resolves at dispatch.
        assert entry["workspace"] == ""
        assert "last_triggered_on" not in entry
        assert "last_dispatched_at" not in entry


def test_stock_text_does_not_reference_removed_agents() -> None:
    from ciao.sync_skills import LEGACY_REMOVED_STOCK_AGENTS

    stock = Path(resources.files("ciao.stock"))
    for path in stock.rglob("*"):
        if not path.is_file() or path.suffix not in {".md", ".json"}:
            continue
        text = path.read_text(encoding="utf-8")
        for agent in LEGACY_REMOVED_STOCK_AGENTS:
            assert agent not in text, (
                f"{path.relative_to(stock)} references removed agent '{agent}'"
            )


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

    setuptools = data["tool"]["setuptools"]
    # Either an explicit `packages` list (legacy) or a packages.find pattern
    # that actually matches the stock subpackage.
    if "packages" in setuptools and isinstance(setuptools["packages"], list):
        assert "ciao.stock" in setuptools["packages"]
    else:
        find_cfg = setuptools.get("packages", {}).get("find", {})
        includes = find_cfg.get("include", [])
        assert any(fnmatchcase("ciao.stock", include) for include in includes), find_cfg

    package_data = setuptools["package-data"]
    assert "agents/*.md" in package_data["ciao.stock"]
    assert "commands/*.md" in package_data["ciao.stock"]
    assert "skills/.gitkeep" in package_data["ciao.stock"]
    assert "public/*.md" in package_data["ciao.stock"]
    assert "workspace/*.md" in package_data["ciao.stock"]
    assert "schedules.json" in package_data["ciao.stock"]
    assert "schedules/*.md" in package_data["ciao.stock"]
