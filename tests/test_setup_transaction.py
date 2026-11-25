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
