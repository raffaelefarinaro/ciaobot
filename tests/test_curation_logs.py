"""Byte-level primitives for the packaged curation logs (#3101, A1).

These pin what the managed append and the weekly marker may do to
``Workspace/Curation-Log.md`` and ``Workspace/Weekly-Review-Log.md``: existing
bytes survive exactly, limits refuse before anything is published, and a
symlink, FIFO or foreign-owned log is never opened for writing.
"""

from __future__ import annotations

import errno
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
    assert cl.read_log(tmp_path / "Curation-Log.md").data is None


def test_read_refuses_over_limit_existing_log(tmp_path: Path) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"x" * (cl.LOG_MAX_BYTES + 1))
    with pytest.raises(cl.CurationLogError):
        cl.read_log(path)


def test_publish_replaces_existing_log_and_keeps_its_mode(tmp_path: Path) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"old", mode=0o640)
    cl.publish_log(path, b"new bytes", snapshot=cl.read_log(path))
    assert path.read_bytes() == b"new bytes"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["Curation-Log.md"]


def test_publish_new_log_is_private_from_creation(tmp_path: Path) -> None:
    path = tmp_path / "Curation-Log.md"
    cl.publish_log(path, b"fresh", snapshot=cl.LogSnapshot(None, None))
    assert path.read_bytes() == b"fresh"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_publish_refuses_to_overwrite_a_link_that_appeared_after_read(tmp_path: Path) -> None:
    path = tmp_path / "Curation-Log.md"
    target = tmp_path / "elsewhere.md"
    target.write_text("keep", encoding="utf-8")
    cl.publish_log(path, b"first", snapshot=cl.LogSnapshot(None, None))
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(cl.CurationLogError):
        cl.publish_log(path, b"second", snapshot=cl.LogSnapshot(None, None))
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


# ── Identity at read, bounded errors, ACL, aliases (#3101 round 2) ───────


def test_publish_refuses_a_different_inode_swapped_in_between_read_and_publish(tmp_path: Path) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"original")
    snapshot = cl.read_log(path)
    intruder = tmp_path / "intruder.tmp"
    intruder.write_bytes(b"intruder")
    os.replace(intruder, path)

    with pytest.raises(cl.CurationLogError):
        cl.publish_log(path, b"mine", snapshot=snapshot)
    assert path.read_bytes() == b"intruder"


def test_publish_refuses_an_in_place_append_between_read_and_publish(tmp_path: Path) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"original")
    snapshot = cl.read_log(path)
    with path.open("ab") as handle:
        handle.write(b"\n\nintruder")

    with pytest.raises(cl.CurationLogError):
        cl.publish_log(path, b"mine", snapshot=snapshot)
    assert path.read_bytes() == b"original\n\nintruder"


def test_a_hard_link_refuses_at_read_and_at_publish(tmp_path: Path) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"original")
    snapshot = cl.read_log(path)
    os.link(path, tmp_path / "alias.md")

    with pytest.raises(cl.CurationLogError):
        cl.publish_log(path, b"mine", snapshot=snapshot)
    with pytest.raises(cl.CurationLogError):
        cl.read_log(path)
    assert path.read_bytes() == b"original"


def test_a_read_only_log_refuses_before_anything_is_staged(tmp_path: Path) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"original", mode=0o444)
    snapshot = cl.read_log(path)

    with pytest.raises(cl.CurationLogError):
        cl.publish_log(path, b"mine", snapshot=snapshot)
    assert path.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["Curation-Log.md"]


def test_enospc_on_write_maps_to_a_bounded_error_and_keeps_the_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"original")
    snapshot = cl.read_log(path)
    real_write = cl.osf.write_all

    def full_disk(fd: int, data: bytes) -> None:
        raise OSError(errno.ENOSPC, f"No space left: {tmp_path}")

    monkeypatch.setattr(cl.osf, "write_all", full_disk)
    with pytest.raises(cl.CurationLogError) as raised:
        cl.publish_log(path, b"mine", snapshot=snapshot)
    monkeypatch.setattr(cl.osf, "write_all", real_write)

    message = str(raised.value)
    assert "ENOSPC" in message and str(tmp_path) not in message and ".tmp" not in message
    assert path.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["Curation-Log.md"]


def test_eperm_on_fchown_maps_to_a_bounded_error_and_leaves_no_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"original")
    snapshot = cl.read_log(path)
    other_group = snapshot.identity[4] + 1

    def denied(fd: int, gid: int) -> None:
        raise PermissionError(errno.EPERM, str(tmp_path))

    monkeypatch.setattr(cl.osf, "effective_gid", lambda: other_group)
    monkeypatch.setattr(cl.osf, "chown_group", denied)
    with pytest.raises(cl.CurationLogError) as raised:
        cl.publish_log(path, b"mine", snapshot=snapshot)
    assert "EPERM" in str(raised.value) and str(tmp_path) not in str(raised.value)
    assert path.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["Curation-Log.md"]


def test_eacces_on_open_is_bounded_with_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"original")

    def denied(*args: object, **kwargs: object) -> int:
        raise PermissionError(errno.EACCES, f"denied {tmp_path}")

    monkeypatch.setattr(cl.osf, "open_fd", denied)
    with pytest.raises(cl.CurationLogError) as raised:
        cl.read_log(path)
    assert "EACCES" in str(raised.value) and str(tmp_path) not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory"])
def test_text_file_that_is_not_a_regular_file_is_a_usage_error(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "report.md"
    if kind == "fifo":
        os.mkfifo(target)
    elif kind == "symlink":
        real = tmp_path / "real.md"
        real.write_text("x", encoding="utf-8")
        target.symlink_to(real)
    else:
        target.mkdir()
    with pytest.raises(ValueError):
        cl.read_payload_file(target)


def test_text_file_read_is_capped_at_the_payload_limit_plus_one(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    target.write_bytes(b"a" * (cl.PAYLOAD_MAX_BYTES + 50))
    assert len(cl.read_payload_file(target)) == cl.PAYLOAD_MAX_BYTES + 1


def _acl_blob(entries: list[tuple[int, int, int]]) -> bytes:
    import struct

    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", tag, perm, ident) for tag, perm, ident in entries)


BASE_ACL = _acl_blob([(0x01, 6, 0xFFFFFFFF), (0x04, 4, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF)])
NAMED_USER_ACL = _acl_blob(
    [(0x01, 6, 0xFFFFFFFF), (0x02, 6, 1234), (0x04, 4, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF)]
)


def test_a_named_user_acl_refuses_and_base_entries_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"original")
    monkeypatch.setattr(os, "getxattr", lambda fd, name: NAMED_USER_ACL, raising=False)
    with pytest.raises(cl.CurationLogError):
        cl.read_log(path)
    monkeypatch.setattr(os, "getxattr", lambda fd, name: BASE_ACL, raising=False)
    assert cl.read_log(path).data == b"original"


@pytest.mark.parametrize("code", ["ENODATA", "ENOTSUP", "ENOSYS"])
def test_an_absent_or_unsupported_acl_is_no_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"original")
    errno_value = getattr(errno, code)

    def absent(fd: int, name: str) -> bytes:
        raise OSError(errno_value, "no attribute")

    monkeypatch.setattr(os, "getxattr", absent, raising=False)
    assert cl.read_log(path).data == b"original"


def test_an_unparseable_acl_blob_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "Curation-Log.md"
    _write(path, b"original")
    monkeypatch.setattr(os, "getxattr", lambda fd, name: b"\x02\x00\x00", raising=False)
    with pytest.raises(cl.CurationLogError):
        cl.read_log(path)


def test_a_fifo_parent_is_refused_and_creates_nothing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    os.mkfifo(vault / "Workspace")
    with pytest.raises(cl.CurationLogError):
        cl.ensure_workspace_dir(vault)


def test_a_dangling_workspace_symlink_is_refused_and_creates_nothing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Workspace").symlink_to(tmp_path / "nowhere")
    with pytest.raises(cl.CurationLogError):
        cl.ensure_workspace_dir(vault)
    assert not (tmp_path / "nowhere").exists()
    assert sorted(p.name for p in vault.iterdir()) == ["Workspace"]


def test_a_log_aliasing_another_curation_file_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "Curation-State.json"
    state.write_text("{}", encoding="utf-8")
    log = tmp_path / "Curation-Log.md"
    os.link(state, log)
    with pytest.raises(cl.CurationLogError):
        cl.refuse_identity_overlap(log, [state])
