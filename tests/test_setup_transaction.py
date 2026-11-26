import json
from pathlib import Path

import pytest

from ciao import setup_transaction


@pytest.mark.parametrize("after_link", [False, True])
@pytest.mark.parametrize("binding", ["env", "registry"])
def test_binding_publication_recovers_interruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_link: bool, binding: str) -> None:
    root, notes = tmp_path / "engine", tmp_path / "notes"
    root.mkdir()
    notes.mkdir()
    real_link = setup_transaction.os.link
    tx = setup_transaction.SetupTransaction(root, notes)
    content = "PWA_AUTH_TOKEN=password\n" if binding == "env" else "[]\n"
    path = root / (".env" if binding == "env" else ".runtime/workspaces.json")
    try:
        tx.begin("password")

        def interrupt(source, target):
            if after_link:
                real_link(source, target)
            raise OSError("interrupted")

        with monkeypatch.context() as patch:
            patch.setattr(setup_transaction.os, "link", interrupt)
            with pytest.raises(OSError, match="interrupted"):
                tx.publish(binding, content)
    finally:
        tx.close()
    tx = setup_transaction.SetupTransaction(root, notes)
    try:
        assert tx.begin("password") == "password"
        tx.publish(binding, content)
        assert path.read_text() == content
    finally:
        tx.close()


def test_binding_replacement_is_refused_even_with_identical_content(tmp_path: Path) -> None:
    root, notes = tmp_path / "engine", tmp_path / "notes"
    root.mkdir()
    notes.mkdir()
    tx = setup_transaction.SetupTransaction(root, notes)
    try:
        tx.begin("password")
        tx.publish("env", "PWA_AUTH_TOKEN=password\n")
        env = root / ".env"
        env.rename(root / "old-env")
        env.write_text("PWA_AUTH_TOKEN=password\n")
        with pytest.raises(ValueError, match="binding changed"):
            tx.verify_binding("env")
    finally:
        tx.close()


def test_generated_password_is_reused_and_wrong_password_is_refused(tmp_path: Path) -> None:
    root, notes = tmp_path / "engine", tmp_path / "notes"
    root.mkdir()
    notes.mkdir()
    tx = setup_transaction.SetupTransaction(root, notes)
    try:
        password = tx.begin(None)
    finally:
        tx.close()
    tx = setup_transaction.SetupTransaction(root, notes)
    try:
        with pytest.raises(ValueError, match="password chosen"):
            tx.begin("wrong")
        assert tx.begin(None) == password
    finally:
        tx.close()


def test_interrupted_completion_never_reauthorizes_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, notes = tmp_path / "engine", tmp_path / "notes"
    root.mkdir()
    notes.mkdir()
    tx = setup_transaction.SetupTransaction(root, notes)
    try:
        tx.begin("password")
        tx.publish("env", f"CIAO_VAULT_ROOT={notes}\nPWA_AUTH_TOKEN=password\n")
        tx.publish("registry", json.dumps([{"name": "personal", "vault_root": str(notes)}]))
        real_unlink = Path.unlink

        def interrupt(path, *args, **kwargs):
            if path == tx.journal:
                raise OSError("interrupted cleanup")
            return real_unlink(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", interrupt)
            with pytest.raises(OSError, match="interrupted cleanup"):
                tx.complete()
        assert not setup_transaction.pending_matches(root, notes)
        from ciao.cli import validate_external_setup_vault
        with pytest.raises(ValueError, match="cannot be rebound"):
            validate_external_setup_vault(root, notes)
    finally:
        tx.close()


def _cli_setup(cli, tmp_path: Path, notes: Path, *extra: str) -> int:
    return cli.main(["setup", "--workspace", str(tmp_path / "engine"), "--notes-folder", str(notes),
        "--launch-agents-dir", str(tmp_path / "agents"), "--app-dir", str(tmp_path / "apps"), *extra])


@pytest.mark.parametrize("replace_inode", [False, True])
def test_journal_that_published_nothing_is_discarded_for_other_notes(tmp_path: Path, replace_inode: bool) -> None:
    root, notes = tmp_path / "engine", tmp_path / "notes"
    root.mkdir()
    notes.mkdir()
    tx = setup_transaction.SetupTransaction(root, notes)
    try:
        assert tx.begin(None)
    finally:
        tx.close()
    if replace_inode:
        notes.rename(tmp_path / "old-notes")
        notes.mkdir()
        other = notes
    else:
        other = tmp_path / "other"
        other.mkdir()
    tx = setup_transaction.SetupTransaction(root, other)
    try:
        assert tx.begin("chosen") == "chosen"
        assert tx.data["notes"] == str(other)
    finally:
        tx.close()


def test_journal_that_published_a_binding_is_never_discarded(tmp_path: Path) -> None:
    root, notes, other = tmp_path / "engine", tmp_path / "notes", tmp_path / "other"
    for folder in (root, notes, other):
        folder.mkdir()
    tx = setup_transaction.SetupTransaction(root, notes)
    try:
        tx.begin("password")
        tx.publish("env", "PWA_AUTH_TOKEN=password\n")
    finally:
        tx.close()
    tx = setup_transaction.SetupTransaction(root, other)
    try:
        with pytest.raises(ValueError, match="does not match"):
            tx.begin("password")
        assert tx.journal.exists()
    finally:
        tx.close()


def test_unpublished_journal_restarts_when_env_changed_by_hand(tmp_path: Path) -> None:
    root, notes = tmp_path / "engine", tmp_path / "notes"
    root.mkdir()
    notes.mkdir()
    (root / ".env").write_text("A=1\n")
    tx = setup_transaction.SetupTransaction(root, notes)
    try:
        tx.begin("password")
    finally:
        tx.close()
    (root / ".env").write_text("A=2\n")
    tx = setup_transaction.SetupTransaction(root, notes)
    try:
        assert tx.begin("password") == "password"
    finally:
        tx.close()


def test_retry_rebuilds_env_from_journaled_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao import cli

    notes = tmp_path / "notes"
    notes.mkdir()
    real_link = setup_transaction.os.link

    def die(source, target):
        raise OSError("killed after journaling the env stage")

    with monkeypatch.context() as patch:
        patch.setattr(setup_transaction.os, "link", die)
        assert _cli_setup(cli, tmp_path, notes, "--auth-token", "password", "--port", "9001") == 1
    assert not (tmp_path / "engine" / ".env").exists()
    monkeypatch.setattr(setup_transaction.os, "link", real_link)
    assert _cli_setup(cli, tmp_path, notes, "--auth-token", "password", "--port", "9002") == 0
    assert "PWA_PORT=9001" in (tmp_path / "engine" / ".env").read_text()


def test_retry_after_registry_writes_ownership_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao import cli, setup_marker

    notes = tmp_path / "notes"
    notes.mkdir()
    ownership = tmp_path / "engine" / ".runtime" / "vault-index-ownership.json"
    real = setup_marker.write_vault_index_ownership

    def die(*args, **kwargs):
        raise OSError("killed before the ownership file")

    with monkeypatch.context() as patch:
        patch.setattr(setup_marker, "write_vault_index_ownership", die)
        assert _cli_setup(cli, tmp_path, notes, "--auth-token", "password") == 1
    assert (tmp_path / "engine" / ".runtime" / "workspaces.json").exists()
    assert not ownership.exists()
    monkeypatch.setattr(setup_marker, "write_vault_index_ownership", real)
    assert _cli_setup(cli, tmp_path, notes, "--auth-token", "password") == 0
    assert json.loads(ownership.read_text())


def test_setup_that_would_write_registry_outside_journal_fails_before_any_binding(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ciao import cli
    from ciao.onboarding import ensure_receipt

    notes = tmp_path / "notes"
    notes.mkdir()
    ensure_receipt(tmp_path / "engine" / ".runtime", established_install=True)
    assert _cli_setup(cli, tmp_path, notes, "--auth-token", "password") == 1
    assert "cannot be recorded" in capsys.readouterr().err
    assert not (tmp_path / "engine" / ".env").exists()
    assert not (tmp_path / "engine" / ".runtime" / "workspaces.json").exists()


def test_interrupted_setup_after_workspace_pointer_leaves_bootstrap_but_cli_retry_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import cli, package_version
    from ciao.config import installed_workspace_env

    notes = tmp_path / "notes"
    notes.mkdir()
    engine = tmp_path / "engine"
    monkeypatch.setattr(package_version, "running_from_app_bundle", lambda: True)
    real_publish = setup_transaction.SetupTransaction.publish

    def die_at_registry(self, name, text, **kwargs):
        if name == "registry":
            raise OSError("killed after the workspace pointer")
        real_publish(self, name, text, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(setup_transaction.SetupTransaction, "publish", die_at_registry)
        assert _cli_setup(cli, tmp_path, notes, "--auth-token", "password") == 1
    # The restarted engine now discovers the workspace: the wizard route that
    # needs bootstrap mode is closed, so PWA_API.md must not promise a PWA retry.
    assert installed_workspace_env({}).get("CIAO_WORKSPACE") == str(engine.resolve())
    assert _cli_setup(cli, tmp_path, notes, "--auth-token", "password") == 0
    assert not setup_transaction.pending_matches(engine.resolve(), notes.resolve())


def test_pin_of_a_journal_published_before_values_were_recorded_is_a_clear_error(tmp_path: Path) -> None:
    root, notes = tmp_path / "engine", tmp_path / "notes"
    root.mkdir()
    notes.mkdir()
    tx = setup_transaction.SetupTransaction(root, notes)
    try:
        tx.begin("password")
        tx.data["env"].update(published="deadbeef")  # journal from before pinning existed
        with pytest.raises(ValueError, match="predates this version"):
            tx.pin("env", {"port": "1", "vault_value": "x"})
    finally:
        tx.close()


@pytest.mark.parametrize("crash_point", ["after_registry", "receipt_lost"])
def test_retry_after_registry_published_still_records_born_per_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_point: str
) -> None:
    from ciao import cli, setup_marker
    from ciao.workspace_reroot import read_receipt

    notes = tmp_path / "notes"
    notes.mkdir()
    runtime = tmp_path / "engine" / ".runtime"
    real = setup_marker.write_vault_index_ownership

    def die(*args, **kwargs):
        raise OSError("killed after the registry")

    with monkeypatch.context() as patch:
        patch.setattr(setup_marker, "write_vault_index_ownership", die)
        assert _cli_setup(cli, tmp_path, notes, "--auth-token", "password") == 1
    assert (runtime / "workspaces.json").exists()
    receipt = runtime / "migration" / "workspace-rooting.json"
    if crash_point == "receipt_lost":
        # The registry was fsynced; the receipt write is not, so a power cut can keep one and lose the other.
        receipt.unlink()
        assert read_receipt(runtime) is None
    monkeypatch.setattr(setup_marker, "write_vault_index_ownership", real)
    assert _cli_setup(cli, tmp_path, notes, "--auth-token", "password") == 0
    migrated = read_receipt(runtime)
    assert migrated is not None and migrated["born_per_root"] is True
    # a retry that finds the receipt must not rewrite it (no stray superseded copies)
    assert [p.name for p in receipt.parent.iterdir() if p.name != receipt.name] == []


def test_registry_publication_keeps_the_owner_view_current(tmp_path: Path) -> None:
    from ciao.workspaces import WorkspaceRegistryConflict, acquire_registry_at

    root, notes = tmp_path / "engine", tmp_path / "notes"
    root.mkdir()
    notes.mkdir()
    tx = setup_transaction.SetupTransaction(root, notes)
    try:
        tx.begin("password")
        with acquire_registry_at(root / ".runtime") as owner:
            tx.publish("registry", "[]\n", owner=owner)
            assert owner.current == b"[]\n"
            owner.replace(b"[1]\n", expected=b"[]\n")  # the compare-and-swap still holds afterwards
            with pytest.raises(WorkspaceRegistryConflict):
                owner.replace(b"[2]\n", expected=b"[]\n")
    finally:
        tx.close()


def test_registry_publication_refuses_an_owner_that_saw_a_registry(tmp_path: Path) -> None:
    from ciao.workspaces import WorkspaceRegistryConflict, acquire_registry_at

    root, notes = tmp_path / "engine", tmp_path / "notes"
    (root / ".runtime").mkdir(parents=True)
    notes.mkdir()
    tx = setup_transaction.SetupTransaction(root, notes)
    try:
        tx.begin("password")
        with acquire_registry_at(root / ".runtime") as owner:
            owner.current = b"[]\n"  # the owner loaded a registry that the journal never saw
            with pytest.raises(WorkspaceRegistryConflict):
                tx.publish("registry", "[3]\n", owner=owner)
    finally:
        tx.close()
