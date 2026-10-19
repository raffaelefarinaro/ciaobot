from __future__ import annotations

from pathlib import Path

from ciao.jsonio import extract_json, write_private_text
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


def test_extract_json_parses_fenced_object() -> None:
    raw = '```json\n{"verdict": "revise", "confidence": 3}\n```'
    parsed = extract_json(raw)
    assert parsed == {"verdict": "revise", "confidence": 3}


def test_extract_json_finds_first_object_in_prose() -> None:
    parsed = extract_json('Here is my review:\n{"verdict": "ship"}\nThanks.')
    assert parsed == {"verdict": "ship"}


def test_extract_json_returns_none_on_garbage() -> None:
    assert extract_json("no json here") is None
