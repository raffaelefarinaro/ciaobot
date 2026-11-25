"""Stopped-engine apply, crash recovery and reversal for a vault rebind (#3606).

Every fixture lives under tmp_path; nothing here touches a real install.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path

import pytest

from ciao import vault_rebind
from ciao.vault_rebind import apply, recover, reverse, review, review_reverse
from ciao.workspaces import acquire_registry_at


class Crash(BaseException):
    """A simulated process death; not an Exception, so no handler absorbs it."""


def make_install(tmp_path: Path, *, other: bool = True):
    install, old, new = tmp_path / "install", tmp_path / "old", tmp_path / "new"
    for path in (install / ".runtime", old / "sub", new / "sub"):
        path.mkdir(parents=True)
    for root in (old, new):
        (root / "note.md").write_text("alpha")
        (root / "sub" / "deep.md").write_text("beta")
    rows: list[dict[str, object]] = [
        {"name": "personal", "vault_root": str(old), "future": {"x": [1, 2]}}
    ]
    if other:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        rows.append({"name": "work", "vault_root": str(elsewhere), "model": "m"})
    registry = install / ".runtime" / "workspaces.json"
    registry.write_bytes((json.dumps(rows, indent=2) + "\n").encode())
    return install, old, new, registry


def tree_state(root: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in [root, *root.rglob("*")]
    )


def state_dir(install: Path) -> Path:
    return install / ".runtime" / "vault-rebind"


def rows_of(registry: Path) -> list[dict[str, object]]:
    return json.loads(registry.read_bytes())


def crash_at(monkeypatch: pytest.MonkeyPatch, step: str) -> None:
    def checkpoint(name: str) -> None:
        if name == step:
            raise Crash(step)

    monkeypatch.setattr(vault_rebind, "_checkpoint", checkpoint)


def test_apply_repoints_only_the_vault_root_and_leaves_notes_alone(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    before = registry.read_bytes()
    old_state, new_state = tree_state(old), tree_state(new)
    plan = review(install, "personal", new)
    result = apply(install, "personal", new, token=plan.token)
    rows = rows_of(registry)
    assert rows[0] == {"name": "personal", "vault_root": str(new), "future": {"x": [1, 2]}}
    assert rows[1] == {"name": "work", "vault_root": str(tmp_path / "elsewhere"), "model": "m"}
    assert tree_state(old) == old_state and tree_state(new) == new_state
    backups = list(state_dir(install).glob("*.before.json"))
    assert [b.read_bytes() for b in backups] == [before]
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(state_dir(install).stat().st_mode) == 0o700
    assert not (state_dir(install) / "pending.json").exists()
    assert result["operation"] in {p.name.removeprefix("done-").removesuffix(".json") for p in state_dir(install).glob("done-*.json")}


def test_refuses_stale_token_without_any_mutation(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    before = registry.read_bytes()
    with pytest.raises(ValueError, match="review"):
        apply(install, "personal", new, token="0" * 64)
    assert registry.read_bytes() == before
    assert not state_dir(install).exists()


def test_refuses_when_files_changed_after_review(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    before = registry.read_bytes()
    plan = review(install, "personal", new)
    (new / "note.md").write_text("ALPHA")
    (old / "note.md").write_text("ALPHA")  # still matching, but not what was reviewed
    with pytest.raises(ValueError):
        apply(install, "personal", new, token=plan.token)
    assert registry.read_bytes() == before
    assert not state_dir(install).exists()


def test_refuses_when_destination_was_replaced_by_identical_copy(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    before = registry.read_bytes()
    plan = review(install, "personal", new)
    import shutil

    shutil.rmtree(new)
    shutil.copytree(old, new)
    with pytest.raises(ValueError):
        apply(install, "personal", new, token=plan.token)
    assert registry.read_bytes() == before
    assert not state_dir(install).exists()


@pytest.mark.parametrize("which", ["source", "destination"])
def test_unavailable_root_refuses_and_writes_nothing(tmp_path: Path, which: str) -> None:
    install, old, new, registry = make_install(tmp_path)
    before = registry.read_bytes()
    plan = review(install, "personal", new)
    import shutil

    shutil.rmtree(old if which == "source" else new)
    with pytest.raises(ValueError):
        apply(install, "personal", new, token=plan.token)
    assert registry.read_bytes() == before
    assert not state_dir(install).exists()
    survivor = new if which == "source" else old
    assert sorted(p.name for p in survivor.iterdir()) == ["note.md", "sub"]


def test_unavailable_destination_cannot_be_reviewed(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    with pytest.raises(ValueError):
        review(install, "personal", tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


def test_refuses_while_engine_holds_server_lock(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    before = registry.read_bytes()
    plan = review(install, "personal", new)
    fd = os.open(install / ".runtime" / "server.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="engine"):
            apply(install, "personal", new, token=plan.token)
        with pytest.raises(ValueError, match="engine"):
            recover(install)
    finally:
        os.close(fd)
    assert registry.read_bytes() == before
    assert not state_dir(install).exists()
    apply(install, "personal", new, token=plan.token)  # the refusal left nothing behind


def test_refuses_while_registry_is_claimed_and_releases_engine_lock(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    before = registry.read_bytes()
    plan = review(install, "personal", new)
    owner = acquire_registry_at(install / ".runtime")
    try:
        with pytest.raises(ValueError, match="registry"):
            apply(install, "personal", new, token=plan.token)
    finally:
        owner.close()
    assert registry.read_bytes() == before
    assert not state_dir(install).exists()
    probe = os.open(install / ".runtime" / "server.lock", os.O_RDWR)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # not left held
    finally:
        os.close(probe)


def test_refuses_when_registry_changed_after_review(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    plan = review(install, "personal", new)
    rows = rows_of(registry)
    rows[1]["model"] = "other"
    edited = (json.dumps(rows, indent=2) + "\n").encode()
    registry.write_bytes(edited)
    with pytest.raises(ValueError):
        apply(install, "personal", new, token=plan.token)
    assert registry.read_bytes() == edited
    assert not state_dir(install).exists()


def test_state_must_not_live_inside_a_notes_folder(tmp_path: Path) -> None:
    state = tmp_path / "install" / ".runtime" / "vault-rebind"
    vault_rebind._require_state_outside(state, tmp_path / "old")
    for root in (tmp_path / "install", tmp_path / "install" / ".runtime", state):
        with pytest.raises(ValueError):
            vault_rebind._require_state_outside(state, root)


@pytest.mark.parametrize("step", ["after_backup", "after_journal", "before_replace", "after_replace", "after_record"])
def test_crash_at_each_step_then_recover_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    install, old, new, registry = make_install(tmp_path)
    before = registry.read_bytes()
    plan = review(install, "personal", new)
    with monkeypatch.context() as patch:
        crash_at(patch, step)
        with pytest.raises(Crash):
            apply(install, "personal", new, token=plan.token)
    applied = step in {"after_replace", "after_record"}
    assert registry.read_bytes() != before if applied else registry.read_bytes() == before
    pending = state_dir(install) / "pending.json"
    if step in {"after_journal", "before_replace", "after_replace", "after_record"}:
        assert pending.exists()
        with pytest.raises(ValueError, match="recover"):
            apply(install, "personal", new, token=plan.token)
    expected = "applied" if applied else ("not_applied" if pending.exists() else "none")
    assert recover(install)["state"] == expected
    assert not pending.exists()
    if applied:
        assert rows_of(registry)[0]["vault_root"] == str(new)
        assert len(list(state_dir(install).glob("done-*.json"))) == 1
        assert recover(install)["state"] == "none"
    else:
        assert registry.read_bytes() == before
        assert not list(state_dir(install).glob("done-*.json"))
        apply(install, "personal", new, token=plan.token)
        assert rows_of(registry)[0]["vault_root"] == str(new)


def test_recover_refuses_registry_that_matches_neither_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install, old, new, registry = make_install(tmp_path)
    plan = review(install, "personal", new)
    crash_at(monkeypatch, "before_replace")
    with pytest.raises(Crash):
        apply(install, "personal", new, token=plan.token)
    rows = rows_of(registry)
    rows[1]["model"] = "someone edited this"
    registry.write_bytes((json.dumps(rows, indent=2) + "\n").encode())
    edited = registry.read_bytes()
    with pytest.raises(ValueError, match="differs"):
        recover(install)
    assert registry.read_bytes() == edited
    assert (state_dir(install) / "pending.json").exists()
    assert list(state_dir(install).glob("*.before.json"))


def test_recover_refuses_tampered_journal_and_keeps_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install, old, new, registry = make_install(tmp_path)
    plan = review(install, "personal", new)
    crash_at(monkeypatch, "after_replace")
    with pytest.raises(Crash):
        apply(install, "personal", new, token=plan.token)
    pending = state_dir(install) / "pending.json"
    journal = json.loads(pending.read_text())
    journal["before_sha256"] = "0" * 64
    pending.write_text(json.dumps(journal))
    after = registry.read_bytes()
    with pytest.raises(ValueError):
        recover(install)
    assert registry.read_bytes() == after and pending.exists()
    pending.write_text("{not json")
    with pytest.raises(ValueError):
        recover(install)
    assert registry.read_bytes() == after and pending.exists()


def test_apply_does_not_write_the_registry_when_backup_cannot_be_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install, old, new, registry = make_install(tmp_path)
    before = registry.read_bytes()
    plan = review(install, "personal", new)
    real = vault_rebind._write_private

    def truncating(path: Path, data: bytes) -> None:
        real(path, data[:-1] if path.name.endswith(".before.json") else data)

    monkeypatch.setattr(vault_rebind, "_write_private", truncating)
    with pytest.raises(ValueError, match="backup"):
        apply(install, "personal", new, token=plan.token)
    assert registry.read_bytes() == before
    assert not (state_dir(install) / "pending.json").exists()


def test_reverse_restores_only_the_binding_and_keeps_later_registry_edits(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    result = apply(install, "personal", new, token=review(install, "personal", new).token)
    rows = rows_of(registry)
    rows[0]["added_later"] = True
    rows[1]["model"] = "changed later"
    registry.write_bytes((json.dumps(rows, indent=2) + "\n").encode())
    plan = review_reverse(install, "personal", result["operation"])
    assert plan.destination == old
    old_state, new_state = tree_state(old), tree_state(new)
    reverse(install, "personal", result["operation"], token=plan.token)
    restored = rows_of(registry)
    assert restored[0] == {
        "name": "personal", "vault_root": str(old), "future": {"x": [1, 2]}, "added_later": True,
    }
    assert restored[1]["model"] == "changed later"
    assert tree_state(old) == old_state and tree_state(new) == new_state
    with pytest.raises(ValueError, match="already"):
        review_reverse(install, "personal", result["operation"])


def test_reverse_refuses_when_binding_moved_old_folder_gone_or_notes_diverged(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    result = apply(install, "personal", new, token=review(install, "personal", new).token)
    op = result["operation"]
    # Later notes written in the new folder: refuse, delete nothing.
    (new / "later.md").write_text("written after the rebind")
    with pytest.raises(ValueError):
        review_reverse(install, "personal", op)
    assert (new / "later.md").read_text() == "written after the rebind"
    (new / "later.md").unlink()
    plan = review_reverse(install, "personal", op)
    # Binding changed by someone else.
    rows = rows_of(registry)
    rows[0]["vault_root"] = str(tmp_path / "elsewhere")
    registry.write_bytes((json.dumps(rows, indent=2) + "\n").encode())
    with pytest.raises(ValueError):
        reverse(install, "personal", op, token=plan.token)
    assert rows_of(registry)[0]["vault_root"] == str(tmp_path / "elsewhere")
    # Unknown or malformed ids.
    for bad in ("../x", "nope", ""):
        with pytest.raises(ValueError):
            review_reverse(install, "personal", bad)
    with pytest.raises(ValueError):
        review_reverse(install, "work", op)


def test_reverse_with_old_folder_unavailable_refuses_without_writing(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    result = apply(install, "personal", new, token=review(install, "personal", new).token)
    plan = review_reverse(install, "personal", result["operation"])
    import shutil

    shutil.rmtree(old)
    after = registry.read_bytes()
    with pytest.raises(ValueError):
        reverse(install, "personal", result["operation"], token=plan.token)
    assert registry.read_bytes() == after
    assert not (state_dir(install) / "pending.json").exists()


def test_crash_during_reverse_recovers_like_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install, old, new, registry = make_install(tmp_path)
    result = apply(install, "personal", new, token=review(install, "personal", new).token)
    plan = review_reverse(install, "personal", result["operation"])
    crash_at(monkeypatch, "after_replace")
    with pytest.raises(Crash):
        reverse(install, "personal", result["operation"], token=plan.token)
    assert rows_of(registry)[0]["vault_root"] == str(old)
    assert recover(install)["state"] == "applied"
    with pytest.raises(ValueError, match="already"):
        review_reverse(install, "personal", result["operation"])


def test_legacy_and_relative_roots_are_refused(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    registry.write_bytes(json.dumps({"personal": {"vault_root": str(old)}}).encode())
    with pytest.raises(ValueError):
        review(install, "personal", new)
    registry.write_bytes(json.dumps([{"name": "personal", "vault_root": "old"}]).encode())
    with pytest.raises(ValueError):
        review(install, "personal", new)
    registry.write_bytes(b'[{"name": "personal", "vault_root": "x", "vault_root": "y"}]')
    with pytest.raises(ValueError):
        review(install, "personal", new)
    assert not state_dir(install).exists()


def test_cli_preview_apply_recover_and_reverse(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ciao import cli

    install, old, new, registry = make_install(tmp_path)
    root = ["--root", str(install)]
    assert cli.main(["vault-rebind", "preview", "personal", str(new), *root]) == 0
    preview_out = json.loads(capsys.readouterr().out)
    assert "cloud client" in preview_out["notice"] and registry.read_bytes().count(b"old")
    assert rows_of(registry)[0]["vault_root"] == str(old)
    assert cli.main(["vault-rebind", "apply", "personal", str(new), "--token", "bad", *root]) == 1
    assert "refused" in capsys.readouterr().err and rows_of(registry)[0]["vault_root"] == str(old)
    assert cli.main(["vault-rebind", "apply", "personal", str(new), "--token", preview_out["token"], *root]) == 0
    operation = json.loads(capsys.readouterr().out)["operation"]
    assert cli.main(["vault-rebind", "recover", *root]) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "none"}
    assert cli.main(["vault-rebind", "reverse-preview", "personal", "--id", operation, *root]) == 0
    token = json.loads(capsys.readouterr().out)["token"]
    assert cli.main(["vault-rebind", "reverse", "personal", "--id", operation, "--token", token, *root]) == 0
    assert rows_of(registry)[0]["vault_root"] == str(old)


def test_reverse_roots_come_from_verified_images_not_the_record_fields(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    result = apply(install, "personal", new, token=review(install, "personal", new).token)
    record = state_dir(install) / f"done-{result['operation']}.json"
    data = json.loads(record.read_text())
    data["old_root"] = str(tmp_path / "elsewhere")  # digests still verify; the fields lie
    record.write_text(json.dumps(data))
    after = registry.read_bytes()
    with pytest.raises(ValueError, match="images"):
        review_reverse(install, "personal", result["operation"])
    with pytest.raises(ValueError, match="images"):
        reverse(install, "personal", result["operation"], token="0" * 64)
    assert registry.read_bytes() == after


def test_no_lock_file_is_created_when_a_notes_folder_contains_the_state(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    rows = rows_of(registry)
    rows[0]["vault_root"] = str(install)  # the source notes folder contains .runtime
    registry.write_bytes((json.dumps(rows, indent=2) + "\n").encode())
    with pytest.raises(ValueError, match="contains"):
        apply(install, "personal", new, token="0" * 64)
    runtime = install / ".runtime"
    assert sorted(p.name for p in runtime.iterdir()) == ["workspaces.json"]
    assert not (install / "server.lock").exists()


def real_setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A default-layout install made by the real `ciao setup` plus a copy of its vault."""
    import shutil

    from ciao import cli

    install = tmp_path / "ws"
    assert cli.main(["setup", "--workspace", str(install), "--launch-agents-dir", str(tmp_path / "la")]) == 0
    vault = install / "personal" / "memory-vault"
    cloud = tmp_path / "cloud-notes"
    shutil.copytree(vault, cloud)
    return install, vault, cloud


def test_default_layout_relative_root_rebinds_to_absolute_and_reverses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install, vault, cloud = real_setup(tmp_path)
    registry = install / ".runtime" / "workspaces.json"
    assert rows_of(registry)[0]["vault_root"] == "personal/memory-vault"  # what setup really writes
    before_rows = rows_of(registry)
    plan = review(install, "personal", cloud)
    assert plan.source == vault
    result = apply(install, "personal", cloud, token=plan.token)
    after_rows = rows_of(registry)
    assert after_rows[0]["vault_root"] == str(cloud)  # converted to absolute
    assert {k: v for k, v in after_rows[0].items() if k != "vault_root"} == {
        k: v for k, v in before_rows[0].items() if k != "vault_root"
    }
    # The old vault lives inside the install; reversing into it is valid.
    back = review_reverse(install, "personal", result["operation"])
    assert back.destination == vault
    reverse(install, "personal", result["operation"], token=back.token)
    assert rows_of(registry) == before_rows  # the exact relative value is restored


@pytest.mark.parametrize("raw", ["personal/../escape", "../outside/x", "./", "single"])
def test_relative_roots_that_traverse_or_are_ambiguous_are_refused(tmp_path: Path, raw: str) -> None:
    install, old, new, registry = make_install(tmp_path)
    rows = rows_of(registry)
    rows[0]["vault_root"] = raw
    registry.write_bytes((json.dumps(rows, indent=2) + "\n").encode())
    with pytest.raises(ValueError):
        review(install, "personal", new)
    assert not state_dir(install).exists()


def test_relative_root_through_a_symlink_is_refused(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    (install / "personal").symlink_to(old, target_is_directory=True)
    (old / "memory-vault").mkdir()
    rows = rows_of(registry)
    rows[0]["vault_root"] = "personal/memory-vault"
    registry.write_bytes((json.dumps(rows, indent=2) + "\n").encode())
    with pytest.raises(ValueError, match="symlink"):
        review(install, "personal", new)


def test_runtime_and_state_stay_protected_inside_the_install(tmp_path: Path) -> None:
    install, old, new, registry = make_install(tmp_path)
    inside_runtime = install / ".runtime" / "notes"
    inside_runtime.mkdir()
    (inside_runtime / "note.md").write_text("alpha")
    (inside_runtime / "sub").mkdir()
    (inside_runtime / "sub" / "deep.md").write_text("beta")
    with pytest.raises(ValueError, match="protected"):
        review(install, "personal", inside_runtime)  # destination inside .runtime
    rows = rows_of(registry)
    rows[0]["vault_root"] = str(inside_runtime)
    registry.write_bytes((json.dumps(rows, indent=2) + "\n").encode())
    with pytest.raises(ValueError, match="runtime"):
        review(install, "personal", new)  # source inside .runtime
    rows[0]["vault_root"] = str(install)
    registry.write_bytes((json.dumps(rows, indent=2) + "\n").encode())
    with pytest.raises(ValueError):
        review(install, "personal", new)  # notes folder that contains .runtime and its state
    assert not state_dir(install).exists()
