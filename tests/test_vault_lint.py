import subprocess
import sys
from pathlib import Path
import pytest
from ciao import vault_lint

def test_cli_help():
    res = subprocess.run([sys.executable, "scripts/vault-lint.py", "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "Vault hygiene linter" in res.stdout

@pytest.fixture
def temp_vault(tmp_path):
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    people = vault / "People"
    people.mkdir()
    (people / "Alice.md").write_text("Hello [[Bob]]", encoding="utf-8")
    return vault

def test_broken_wikilinks(temp_vault):
    issues = vault_lint.run_validation(temp_vault)
    assert len(issues["broken_links"]) == 1
    assert "Bob" in issues["broken_links"][0]["target"]
    assert "People/Alice.md" in issues["broken_links"][0]["source"]

def test_orphan_detection(temp_vault):
    people = temp_vault / "People"
    (people / "Bob.md").write_text("Profile of Bob", encoding="utf-8")
    (people / "Charlie.md").write_text("Hello", encoding="utf-8")
    
    issues = vault_lint.run_validation(temp_vault)
    assert "People/Charlie.md" in issues["orphans"]
    assert "People/Bob.md" not in issues["orphans"]

def test_duplicate_detection(temp_vault):
    people = temp_vault / "People"
    (people / "Alice-Smith.md").write_text("Alice Smith", encoding="utf-8")
    (people / "AliceSmith.md").write_text("Alice Smith duplicate", encoding="utf-8")
    
    issues = vault_lint.run_validation(temp_vault)
    assert len(issues["duplicates"]) == 1
    assert "People/Alice-Smith.md" in issues["duplicates"][0]
    assert "People/AliceSmith.md" in issues["duplicates"][0]


def test_ignores_wikilinks_in_code_and_escaped(temp_vault):
    """Wikilink syntax inside code spans/fences or backslash-escaped is
    documentation, not a real link, and must not be flagged (issue #129)."""
    (temp_vault / "People" / "Guide.md").write_text(
        "Use `[[Nonexistent]]` in prose.\n\n"
        "```\n[[AlsoNonexistent]]\n```\n\n"
        "Escaped: \\[[EscapedTarget]]\n"
        "Placeholder: [[projects/active/<folder>/<folder>]]\n",
        encoding="utf-8",
    )
    issues = vault_lint.run_validation(temp_vault)
    bad = {b["target"] for b in issues["broken_links"]}
    assert "Nonexistent" not in bad
    assert "AlsoNonexistent" not in bad
    assert "EscapedTarget" not in bad
    assert not any("<folder>" in t for t in bad)


def test_common_stems_not_flagged_as_duplicates(temp_vault):
    """One README/log per project is normal, not a duplicate page (#129)."""
    projects = temp_vault / "projects"
    (projects / "a").mkdir(parents=True)
    (projects / "b").mkdir(parents=True)
    (projects / "a" / "README.md").write_text("A", encoding="utf-8")
    (projects / "b" / "README.md").write_text("B", encoding="utf-8")
    issues = vault_lint.run_validation(temp_vault)
    assert all("README.md" not in "".join(dup) for dup in issues["duplicates"])


def test_excludes_venv_and_tool_dirs(temp_vault):
    """A venv/node_modules checked out in the vault must not be scanned (#129)."""
    venv = temp_vault / "work" / "automations" / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "doc.md").write_text("[[NopeTarget]]", encoding="utf-8")
    issues = vault_lint.run_validation(temp_vault)
    assert not any(b["target"] == "NopeTarget" for b in issues["broken_links"])
