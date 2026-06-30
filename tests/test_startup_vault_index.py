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
