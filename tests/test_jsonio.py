from __future__ import annotations

import stat
from pathlib import Path

from ciao.jsonio import write_private_text


def test_write_private_text_creates_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    write_private_text(path, '{"token": "t"}')
    assert path.read_text(encoding="utf-8") == '{"token": "t"}'
    # a umask of 022 would leave write_text's output at 0644; this must not
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_private_text_repairs_looser_preexisting_file(tmp_path: Path) -> None:
    """Older installs created secret files before 0600-on-create existed."""
    path = tmp_path / "credentials.json"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o644)
    write_private_text(path, "new")
    assert path.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
