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
