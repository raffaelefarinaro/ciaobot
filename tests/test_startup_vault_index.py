from __future__ import annotations

from pathlib import Path

from ciao.main import _refresh_vault_index


def test_refresh_vault_index_uses_packaged_module_without_workspace_script(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    note = vault / "People" / "Ada.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: person\ntitle: Ada\ntags: [test]\n---\n# Ada\n",
        encoding="utf-8",
    )

    assert not (tmp_path / "scripts" / "vault_index.py").exists()

    assert _refresh_vault_index(tmp_path, vault) is True

    index = vault / "INDEX.md"
    assert index.is_file()
    assert "Ada" in index.read_text(encoding="utf-8")


def test_refresh_vault_index_skips_missing_vault_root(tmp_path: Path, caplog) -> None:
    # Bootstrap mode starts before the wizard created the vault: skip with an
    # INFO log, never raise, and never scaffold the vault preemptively.
    vault = tmp_path / "memory-vault"

    with caplog.at_level("INFO", logger="ciao.main"):
        assert _refresh_vault_index(tmp_path, vault) is False

    assert not vault.exists()
    assert any("skipping index refresh" in r.message for r in caplog.records)
    assert all(r.levelname != "WARNING" for r in caplog.records)
