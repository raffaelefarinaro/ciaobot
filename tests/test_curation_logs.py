"""Byte-level primitives for the packaged curation logs (#3101, A1).

These pin what the managed append and the weekly marker may do to
``Workspace/Curation-Log.md`` and ``Workspace/Weekly-Review-Log.md``: existing
bytes survive exactly, limits refuse before anything is published, and a
symlink, FIFO or foreign-owned log is never opened for writing.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ciao import curation_logs as cl


def _write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)


def test_append_preserves_existing_bytes_exactly_and_adds_separator() -> None:
    existing = b"# Curation log\r\nold line\r\n\xc3\xa9 trailing"
    result = cl.appended_bytes(existing, b"new report")
    assert result == existing + b"\n\n" + b"new report"


def test_append_to_absent_log_starts_with_canonical_heading() -> None:
    result = cl.appended_bytes(None, b"first", heading=cl.LOG_HEADINGS["weekly-review"])
    assert result == b"# Weekly review log\n" + b"\n\n" + b"first"


def test_bytes_appended_counts_separator_and_payload_only() -> None:
    assert cl.bytes_appended(b"x") == len(b"\n\n") + 1


@pytest.mark.parametrize(
    "payload",
    [b"", b"\xef\xbb\xbfbom", b"\xff\xfe", "café".encode("latin-1")],
    ids=["empty", "bom", "invalid-utf8-bytes", "latin1"],
)
def test_payload_refusals_are_usage_errors(payload: bytes) -> None:
    with pytest.raises(ValueError):
        cl.check_payload(payload)


def test_payload_limit_is_inclusive_at_256_kib() -> None:
    cl.check_payload(b"a" * cl.PAYLOAD_MAX_BYTES)
    with pytest.raises(ValueError):
        cl.check_payload(b"a" * (cl.PAYLOAD_MAX_BYTES + 1))


def test_resulting_size_limit_is_inclusive_at_8_mib() -> None:
    payload = b"p" * 100
    separator = len(b"\n\n")
    existing = b"x" * (cl.LOG_MAX_BYTES - separator - len(payload))
    assert len(cl.appended_bytes(existing, payload)) == cl.LOG_MAX_BYTES
    with pytest.raises(cl.CurationLogError):
        cl.appended_bytes(existing + b"x", payload)


def test_marker_inserts_into_existing_frontmatter_preserving_other_lines() -> None:
    text = b"---\r\ntitle: Log\r\nlast_full_pass: 2026-01-01\r\n---\r\nbody line\r\n"
    result = cl.with_full_pass_marker(text, "2026-11-20")
    assert result == b"---\r\ntitle: Log\r\nlast_full_pass: 2026-11-20\r\n---\r\nbody line\r\n"


def test_marker_adds_missing_frontmatter_and_keeps_body_bytes() -> None:
    body = b"# Curation log\n\nentry \xc3\xa9\n\n"
    result = cl.with_full_pass_marker(body, "2026-11-20")
    assert result == b"---\nlast_full_pass: 2026-11-20\n---\n\n" + body


def test_marker_on_missing_log_is_frontmatter_then_canonical_heading() -> None:
    result = cl.with_full_pass_marker(None, "2026-11-20")
    assert result == b"---\nlast_full_pass: 2026-11-20\n---\n\n# Curation log\n"


@pytest.mark.parametrize(
    "text",
    [
        b"---\ntitle: x\nbody without a closing fence\n",
        b"---\nlast_full_pass: 2026-01-01\nlast_full_pass: 2026-01-02\n---\nbody\n",
    ],
    ids=["unclosed-frontmatter", "duplicate-owned-field"],
)
def test_marker_refuses_ambiguous_control_block(text: bytes) -> None:
    with pytest.raises(cl.CurationLogError):
        cl.with_full_pass_marker(text, "2026-11-20")


def test_marker_result_is_bounded_before_publication() -> None:
    frontmatter = b"---\nlast_full_pass: 2026-01-01\n---\n"
    body = b"b" * (cl.LOG_MAX_BYTES - len(frontmatter))
    with pytest.raises(cl.CurationLogError):
        cl.with_full_pass_marker(frontmatter + body + b"b", "2026-11-20")


def test_read_refuses_symlink_leaf(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.md"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "Curation-Log.md"
    link.symlink_to(target)
    with pytest.raises(cl.CurationLogError):
        cl.read_log(link)
    assert target.read_text(encoding="utf-8") == "secret"


def test_read_refuses_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "Curation-Log.md"
    os.mkfifo(fifo)
    with pytest.raises(cl.CurationLogError):
        cl.read_log(fifo)


def test_read_absent_leaf_is_none(tmp_path: Path) -> None:
    assert cl.read_log(tmp_path / "Curation-Log.md") is None


def test_read_refuses_over_limit_existing_log(tmp_path: Path) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"x" * (cl.LOG_MAX_BYTES + 1))
    with pytest.raises(cl.CurationLogError):
        cl.read_log(path)


def test_publish_replaces_existing_log_and_keeps_its_mode(tmp_path: Path) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"old", mode=0o640)
    cl.publish_log(path, b"new bytes", previous=cl.read_log(path))
    assert path.read_bytes() == b"new bytes"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["Curation-Log.md"]


def test_publish_new_log_is_private_from_creation(tmp_path: Path) -> None:
    path = tmp_path / "Curation-Log.md"
    cl.publish_log(path, b"fresh", previous=None)
    assert path.read_bytes() == b"fresh"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_publish_refuses_to_overwrite_a_link_that_appeared_after_read(tmp_path: Path) -> None:
    path = tmp_path / "Curation-Log.md"
    target = tmp_path / "elsewhere.md"
    target.write_text("keep", encoding="utf-8")
    cl.publish_log(path, b"first", previous=None)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(cl.CurationLogError):
        cl.publish_log(path, b"second", previous=None)
    assert target.read_text(encoding="utf-8") == "keep"


def test_ensure_workspace_dir_creates_only_the_one_private_directory(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    workspace = cl.ensure_workspace_dir(vault)
    assert workspace == vault / "Workspace"
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700


def test_ensure_workspace_dir_refuses_missing_vault(tmp_path: Path) -> None:
    with pytest.raises(cl.CurationLogError):
        cl.ensure_workspace_dir(tmp_path / "no-such-vault")


def test_ensure_workspace_dir_converges_a_root_alias_on_the_canonical_vault(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(vault, target_is_directory=True)
    workspace = cl.ensure_workspace_dir(alias)
    assert workspace == vault.resolve() / "Workspace"
    assert workspace.is_dir()


def test_canonical_vault_refuses_an_unresolved_root(tmp_path: Path) -> None:
    with pytest.raises(cl.CurationLogError):
        cl.canonical_vault(tmp_path / "missing")
