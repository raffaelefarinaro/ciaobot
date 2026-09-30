from __future__ import annotations

from pathlib import Path

from ciao.jsonio import write_private_text
from ciao.os_support.private import is_private


def test_write_private_text_creates_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    write_private_text(path, '{"token": "t"}')
    assert path.read_text(encoding="utf-8") == '{"token": "t"}'
    # a umask of 022 would leave write_text's output at 0644; this must not
    assert is_private(path)


def test_write_private_text_repairs_looser_preexisting_file(tmp_path: Path) -> None:
    """Older installs created secret files before 0600-on-create existed."""
    path = tmp_path / "credentials.json"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o644)
    assert not is_private(path)
    write_private_text(path, "new")
    assert path.read_text(encoding="utf-8") == "new"
    assert is_private(path)
