from pathlib import Path

from ciao import workspace_rename
from ciao.onboarding import (
    OnboardingProgress,
    OnboardingReceipt,
    OnboardingStateError,
    ensure_receipt,
    read_receipt,
    write_receipt,
)
from ciao.onboarding_progress import register_new_workspace, rename_workspace_progress
from ciao.setup_marker import RUNTIME_DIR_NAME
from tests.test_workspace_archive import _app
from tests.test_workspace_rename import _Projects, _shared_config, _stores
from tests.test_workspace_settings_routes import _client
import pytest


def _config(root: Path):
    return type("Config", (), {"state_path": root / ".runtime" / "state.json"})()


def test_new_workspace_registration_preserves_installation_and_other_entries(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = tmp_path / ".runtime"
    ensure_receipt(root, established_install=True)
    before = read_receipt(root)
    assert before is not None
    seeded = OnboardingReceipt(before.revision, before.installation, {"existing": OnboardingProgress("completed", "finish")})
    write_receipt(root, seeded, expected_revision=before.revision)
    result = register_new_workspace(config, "fresh")
    assert result.installation == seeded.installation
    assert result.workspaces == {"existing": seeded.workspaces["existing"], "fresh": OnboardingProgress("in_progress", "runtime")}


def test_rename_moves_identity_and_discards_stale_destination(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = tmp_path / ".runtime"
    ensure_receipt(root, established_install=True)
    receipt = read_receipt(root)
    assert receipt is not None
    workspaces = {"old": OnboardingProgress("completed", "finish"), "new": OnboardingProgress("legacy_exempt", "location"), "other": OnboardingProgress("in_progress", "runtime")}
    write_receipt(root, OnboardingReceipt(receipt.revision, receipt.installation, workspaces), expected_revision=receipt.revision)
    result = rename_workspace_progress(config, source="old", destination="new", expected_revision=receipt.revision + 1)
    assert result.workspaces == {"other": workspaces["other"], "new": workspaces["old"]}


def test_workspace_creation_registers_runtime_and_returns_target(tmp_path: Path) -> None:
    client, config, _ = _client(tmp_path)
    runtime = tmp_path / RUNTIME_DIR_NAME
    ensure_receipt(runtime, established_install=True)
    response = client.post("/api/workspaces", json={"name": "fresh", "vault_root": "fresh"})
    assert response.status_code == 201
    assert response.json()["onboarding_target"] == {"kind": "workspace", "name": "fresh"}
    progress = read_receipt(runtime)
    assert progress is not None
    assert progress.installation.status == "legacy_exempt"
    assert progress.workspaces["fresh"] == OnboardingProgress("in_progress", "runtime")


def test_existing_workspace_save_does_not_register_progress(tmp_path: Path) -> None:
    client, _config, _ = _client(tmp_path)
    runtime = tmp_path / RUNTIME_DIR_NAME
    ensure_receipt(runtime, established_install=True)
    created = client.post("/api/workspaces", json={"name": "existing", "vault_root": "existing"})
    assert created.status_code == 201
    before = read_receipt(runtime)
    saved = client.patch("/api/workspaces/existing", json={"vault_root": "existing", "color": "blue"})
    assert saved.status_code == 200
    assert "onboarding_target" not in saved.json()
    assert read_receipt(runtime) == before


def test_workspace_rename_route_moves_progress_entry(tmp_path: Path) -> None:
    client, _config, _ = _client(tmp_path)
    client.app.state.project_chat_manager.evict_workspace_providers = lambda _name: []
    runtime = tmp_path / RUNTIME_DIR_NAME
    ensure_receipt(runtime, established_install=True)
    created = client.post("/api/workspaces", json={"name": "before", "vault_root": "before"})
    assert created.status_code == 201
    response = client.patch("/api/workspaces/before", json={"name": "after", "vault_root": "before"})
    assert response.status_code == 200
    progress = read_receipt(runtime)
    assert progress is not None
    assert "before" not in progress.workspaces
    assert progress.workspaces["after"] == OnboardingProgress("in_progress", "runtime")


def test_receipt_failure_precedes_workspace_mutation(tmp_path: Path) -> None:
    client, config, _ = _client(tmp_path)
    runtime = tmp_path / RUNTIME_DIR_NAME
    runtime.mkdir(parents=True)
    (runtime / "onboarding.json").write_bytes(b"invalid receipt")
    with pytest.raises(OnboardingStateError):
        client.post("/api/workspaces", json={"name": "blocked", "vault_root": "blocked"})
    assert "blocked" not in config.workspaces
    assert not (runtime / "workspaces.json").exists()


def test_registry_failure_removes_live_workspace_and_orphan_cannot_be_targeted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, config, _ = _client(tmp_path)
    runtime = tmp_path / RUNTIME_DIR_NAME
    ensure_receipt(runtime, established_install=False)
    monkeypatch.setattr("ciao.web.routes_api._persist_workspaces", lambda _config: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        client.post("/api/workspaces", json={"name": "orphan", "vault_root": "orphan"})
    assert "orphan" not in config.workspaces
    orphan = read_receipt(runtime)
    assert orphan is not None and "orphan" in orphan.workspaces
    from ciao.onboarding_progress import change_step, OnboardingTargetNotFound
    with pytest.raises(OnboardingTargetNotFound):
        change_step(config, target={"kind": "workspace", "name": "orphan"}, expected_revision=orphan.revision, step="care")


def test_rename_failure_restores_receipt_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _shared_config(tmp_path, ["old", "other"])
    config.persist_workspace_registry()
    runtime = Path(config.state_path).parent
    ensure_receipt(runtime, established_install=True)
    receipt = read_receipt(runtime)
    assert receipt is not None
    write_receipt(runtime, OnboardingReceipt(receipt.revision, receipt.installation, {"old": OnboardingProgress("completed", "finish")}), expected_revision=receipt.revision)
    path = runtime / "onboarding.json"
    before = path.read_bytes()
    projects = _Projects()
    stores = _stores(config, tmp_path)
    monkeypatch.setattr(type(stores[0]), "rename_workspace", lambda *_args: (_ for _ in ()).throw(OSError("failed")))
    with pytest.raises(OSError, match="failed"):
        workspace_rename.rename_workspace(config, old="old", new="new", projects=projects, schedules=stores[0], webhooks=stores[1], imports=stores[2], runs=stores[3])
    assert path.read_bytes() == before


def test_rename_rollback_does_not_clobber_newer_receipt_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _shared_config(tmp_path, ["old", "other"])
    config.persist_workspace_registry()
    runtime = Path(config.state_path).parent
    ensure_receipt(runtime, established_install=True)
    receipt = read_receipt(runtime)
    assert receipt is not None
    write_receipt(runtime, OnboardingReceipt(receipt.revision, receipt.installation, {"old": OnboardingProgress("completed", "finish")}), expected_revision=receipt.revision)
    path = runtime / "onboarding.json"
    projects = _Projects()
    stores = _stores(config, tmp_path)
    def fail_after_new_revision(self, old, new):
        current = read_receipt(runtime)
        assert current is not None
        write_receipt(runtime, OnboardingReceipt(current.revision, current.installation, {**current.workspaces, "unrelated": OnboardingProgress("in_progress", "care")}), expected_revision=current.revision)
        raise OSError("failed")
    monkeypatch.setattr(type(stores[0]), "rename_workspace", fail_after_new_revision)
    with pytest.raises(OSError, match="failed"):
        workspace_rename.rename_workspace(config, old="old", new="new", projects=projects, schedules=stores[0], webhooks=stores[1], imports=stores[2], runs=stores[3])
    resulting = read_receipt(runtime)
    assert resulting is not None and "unrelated" in resulting.workspaces
    assert "old" not in resulting.workspaces and "new" in resulting.workspaces


def test_archive_restore_does_not_register_workspace_progress(tmp_path: Path) -> None:
    client, config, _pcm, _schedules = _app(tmp_path, rerooted=False)
    runtime = Path(config.state_path).parent
    ensure_receipt(runtime, established_install=True)
    receipt = read_receipt(runtime)
    assert receipt is not None
    before = (runtime / "onboarding.json").read_bytes()
    archived = client.post("/api/workspaces/work/archive")
    assert archived.status_code == 200
    row = client.get("/api/workspaces/archived").json()["archived"][0]
    restored = client.post("/api/workspaces/archived/restore", json={"id": row["id"]})
    assert restored.status_code == 200
    assert (runtime / "onboarding.json").read_bytes() == before



def test_rename_drops_a_stale_destination_even_without_source_progress(tmp_path: Path) -> None:
    """Renaming onto a reused name must not adopt that name's old completion."""
    config = _config(tmp_path)
    root = tmp_path / ".runtime"
    ensure_receipt(root, established_install=True)
    receipt = read_receipt(root)
    assert receipt is not None
    seeded = OnboardingReceipt(
        receipt.revision,
        receipt.installation,
        {"reused": OnboardingProgress("completed", "finish")},
    )
    stored = write_receipt(root, seeded, expected_revision=receipt.revision)

    renamed = rename_workspace_progress(
        config, source="brand-new-name", destination="reused", expected_revision=stored.revision
    )

    assert "reused" not in renamed.workspaces
    assert renamed.revision == stored.revision + 1


def test_rename_with_nothing_to_move_does_not_burn_a_revision(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = tmp_path / ".runtime"
    ensure_receipt(root, established_install=True)
    receipt = read_receipt(root)
    assert receipt is not None

    same = rename_workspace_progress(
        config, source="absent", destination="also-absent", expected_revision=receipt.revision
    )

    assert same == receipt


def test_rollback_does_not_delete_a_progress_write_that_landed_after_the_rename(
    tmp_path: Path,
) -> None:
    """A PATCH that lands after the rename wrote must survive the rollback."""
    root = tmp_path / ".runtime"
    ensure_receipt(root, established_install=False)
    original = read_receipt(root)
    assert original is not None
    snapshot = {
        "onboarding_path": root / "onboarding.json",
        "onboarding_bytes": (root / "onboarding.json").read_bytes(),
        "onboarding_revision": original.revision,
        "onboarding_written_revision": original.revision + 1,
    }
    # The rename's own write, then an unrelated PATCH.
    write_receipt(
        root,
        OnboardingReceipt(original.revision, original.installation, {}),
        expected_revision=original.revision,
    )
    patched = write_receipt(
        root,
        OnboardingReceipt(original.revision + 1, OnboardingProgress("in_progress", "care"), {}),
        expected_revision=original.revision + 1,
    )
    after_patch = (root / "onboarding.json").read_bytes()

    workspace_rename._restore_snapshot_files(snapshot)

    assert read_receipt(root) == patched
    assert (root / "onboarding.json").read_bytes() == after_patch
