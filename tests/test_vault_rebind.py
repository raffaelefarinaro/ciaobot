from pathlib import Path

import pytest

from ciao.vault_rebind import authoritative_preview, preview, recovery_state, repoint_registry


def test_preview_matches_files_and_binds_expected_registry(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "note.md").write_text("note")
    (new / "note.md").write_text("note")
    first = preview(old, new, expected_registry=b"before", protected_roots=[])
    assert first.source == old
    assert first.destination == new
    assert first.files == ("note.md",)
    assert first.token != preview(old, new, expected_registry=b"changed", protected_roots=[]).token


def test_preview_refuses_changed_content_and_unsafe_paths(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "note.md").write_text("old")
    (new / "note.md").write_text("new")
    with pytest.raises(ValueError):
        preview(old, new, expected_registry=b"before", protected_roots=[])
    (new / "note.md").write_text("old")
    with pytest.raises(ValueError):
        preview(old, new, expected_registry=b"before", protected_roots=[tmp_path])
    (new / "link").symlink_to(old / "note.md")
    with pytest.raises(ValueError):
        preview(old, new, expected_registry=b"before", protected_roots=[])


def test_registry_repoint_and_reverse_preserve_other_fields() -> None:
    before = [{"name": "one", "vault_root": "old", "future": {"x": 1}}, {"name": "two", "vault_root": "other"}]
    after = repoint_registry(before, "one", expected_root="old", new_root="new")
    assert before[0]["vault_root"] == "old"
    assert after[0] == {"name": "one", "vault_root": "new", "future": {"x": 1}}
    after[1]["future"] = "later change"
    restored = repoint_registry(after, "one", expected_root="new", new_root="old")
    assert restored[1]["future"] == "later change"
    assert restored[0]["vault_root"] == "old"


def test_registry_repoint_refuses_stale_and_ambiguous_entries() -> None:
    row = {"name": "one", "vault_root": "old"}
    with pytest.raises(ValueError):
        repoint_registry([row], "one", expected_root="stale", new_root="new")
    with pytest.raises(ValueError):
        repoint_registry([row, row], "one", expected_root="old", new_root="new")
    assert row["vault_root"] == "old"


def test_preview_refuses_empty_or_missing_notes(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    with pytest.raises(ValueError):
        preview(old, new, expected_registry=b"before", protected_roots=[])
    new.rmdir()
    with pytest.raises(ValueError):
        preview(old, new, expected_registry=b"before", protected_roots=[])
    assert not new.exists()


def test_preview_refuses_file_substitution_during_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "note.md").write_text("same")
    (new / "note.md").write_text("same")
    real_open = os.open
    switched = False

    def replace_at_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal switched
        if path == "note.md" and kwargs.get("dir_fd") is not None and not switched:
            switched = True
            (old / "note.md").unlink()
            (old / "note.md").symlink_to(new / "note.md")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_at_open)
    with pytest.raises((ValueError, OSError)):
        preview(old, new, expected_registry=b"before", protected_roots=[])
    assert switched


def test_preview_token_changes_when_identical_file_is_replaced(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "note.md").write_text("same")
    (new / "note.md").write_text("same")
    first = preview(old, new, expected_registry=b"before", protected_roots=[])
    replacement = new / "replacement"
    replacement.write_text("same")
    replacement.replace(new / "note.md")
    second = preview(old, new, expected_registry=b"before", protected_roots=[])
    assert first.token != second.token


def test_authoritative_preview_uses_registry_and_rejects_stale_state(tmp_path: Path) -> None:
    import json

    install, old, new = tmp_path / "install", tmp_path / "old", tmp_path / "new"
    for path in (install, old, new):
        path.mkdir()
    (install / ".runtime").mkdir()
    (old / "note.md").write_text("same")
    (new / "note.md").write_text("same")
    registry = json.dumps([{"name": "personal", "vault_root": str(old)}]).encode()
    (install / ".runtime" / "workspaces.json").write_bytes(registry)
    result = authoritative_preview(install, "personal", new, expected_registry=registry)
    assert result.source == old
    with pytest.raises(ValueError):
        authoritative_preview(install, "personal", new, expected_registry=b"stale")
    with pytest.raises(ValueError):
        authoritative_preview(install, "unknown", new, expected_registry=registry)


def test_authoritative_preview_refuses_other_workspace_overlap(tmp_path: Path) -> None:
    import json

    install, old, new = tmp_path / "install", tmp_path / "old", tmp_path / "new"
    for path in (install, old, new):
        path.mkdir()
    (install / ".runtime").mkdir()
    (old / "note.md").write_text("same")
    (new / "note.md").write_text("same")
    registry = json.dumps([{"name": "one", "vault_root": str(old)}, {"name": "two", "vault_root": str(new)}]).encode()
    (install / ".runtime" / "workspaces.json").write_bytes(registry)
    with pytest.raises(ValueError):
        authoritative_preview(install, "one", new, expected_registry=registry)


def test_recovery_requires_exact_recorded_before_or_after() -> None:
    assert recovery_state(b"before", before=b"before", after=b"after") == "before"
    assert recovery_state(b"after", before=b"before", after=b"after") == "after"
    with pytest.raises(ValueError):
        recovery_state(b"later edit", before=b"before", after=b"after")
    with pytest.raises(ValueError):
        recovery_state(b"same", before=b"same", after=b"same")


def test_preview_refuses_file_swapped_for_fifo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "note.md").write_text("same")
    (new / "note.md").write_text("same")
    real_open = os.open
    switched = False

    def replace_at_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal switched
        if path == "note.md" and kwargs.get("dir_fd") is not None and not switched:
            switched = True
            (old / "note.md").unlink()
            os.mkfifo(old / "note.md")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_at_open)
    with pytest.raises((ValueError, OSError)):
        preview(old, new, expected_registry=b"before", protected_roots=[])
    assert switched


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.mkdir()
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.rmdir()


def _registry_install(tmp_path: Path, rows) -> tuple[Path, bytes]:
    import json

    install = tmp_path / "install"
    (install / ".runtime").mkdir(parents=True)
    registry = json.dumps(rows).encode()
    (install / ".runtime" / "workspaces.json").write_bytes(registry)
    return install, registry


def test_destination_spelled_with_other_case_still_overlaps_runtime(tmp_path: Path) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem (default APFS)")
    old = tmp_path / "old"
    old.mkdir()
    (old / "note.md").write_text("same")
    install, registry = _registry_install(tmp_path, [{"name": "personal", "vault_root": str(old)}])
    variant = tmp_path / "install" / ".RunTime" / "notes"
    variant.mkdir()
    (variant / "note.md").write_text("same")
    with pytest.raises(ValueError, match="protected root"):
        authoritative_preview(install, "personal", variant, expected_registry=registry)


def test_notes_roots_spelled_with_other_case_overlap(tmp_path: Path) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem (default APFS)")
    old = tmp_path / "Notes"
    old.mkdir()
    (old / "note.md").write_text("same")
    with pytest.raises(ValueError, match="overlap"):
        preview(old, tmp_path / "notes", expected_registry=b"x", protected_roots=[])


def test_other_workspace_root_spelled_with_other_case_is_protected(tmp_path: Path) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem (default APFS)")
    old, other = tmp_path / "old", tmp_path / "Other"
    for path in (old, other):
        path.mkdir()
        (path / "note.md").write_text("same")
    install, registry = _registry_install(
        tmp_path, [{"name": "one", "vault_root": str(old)}, {"name": "two", "vault_root": str(other)}]
    )
    with pytest.raises(ValueError, match="protected root"):
        authoritative_preview(install, "one", tmp_path / "OTHER", expected_registry=registry)


def test_runtime_overlap_check_is_case_insensitive_for_the_source_root(tmp_path: Path) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem (default APFS)")
    install, registry = _registry_install(tmp_path, [{"name": "personal", "vault_root": ".RUNTIME/notes"}])
    (install / ".runtime" / "notes").mkdir()
    new = tmp_path / "new"
    new.mkdir()
    with pytest.raises(ValueError, match="runtime state"):
        authoritative_preview(install, "personal", new, expected_registry=registry)


def test_distinct_siblings_with_similar_names_do_not_overlap(tmp_path: Path) -> None:
    old, new = tmp_path / "notes", tmp_path / "notes-copy"
    for path in (old, new):
        path.mkdir()
        (path / "note.md").write_text("same")
    assert preview(old, new, expected_registry=b"x", protected_roots=[]).files == ("note.md",)


def test_legacy_root_error_names_the_blocking_workspace(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    for path in (old, new):
        path.mkdir()
        (path / "note.md").write_text("same")
    install, registry = _registry_install(
        tmp_path, [{"name": "one", "vault_root": str(old)}, {"name": "legacy-ws", "vault_root": "memory-vault"}]
    )
    with pytest.raises(ValueError, match="workspace 'legacy-ws'.*single-segment"):
        authoritative_preview(install, "one", new, expected_registry=registry)
