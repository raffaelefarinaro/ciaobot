from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from ciao.onboarding import (
    OnboardingConflictError,
    OnboardingStateError,
    ensure_receipt,
    ensure_startup_receipt,
    read_receipt,
    write_receipt,
)
from ciao.onboarding import OnboardingProgress
from ciao import cli
from ciao.config import CiaoConfig


def _valid_payload() -> dict[str, object]:
    return {"schema": 1, "revision": 0,
            "installation": {"status": "in_progress", "step": "location"},
            "workspaces": {}}


def test_initialization_is_byte_identical_when_unchanged(tmp_path: Path) -> None:
    first = ensure_receipt(tmp_path, established_install=False)
    before = (tmp_path / "onboarding.json").read_bytes()
    assert ensure_receipt(tmp_path, established_install=True) == first
    assert (tmp_path / "onboarding.json").read_bytes() == before


def test_receipt_write_is_atomic_and_revision_checked(tmp_path: Path) -> None:
    receipt = ensure_receipt(tmp_path, established_install=False)
    updated = write_receipt(
        tmp_path,
        receipt,
        expected_revision=0,
    )
    assert updated.revision == 1
    before = (tmp_path / "onboarding.json").read_bytes()
    with pytest.raises(OnboardingConflictError):
        write_receipt(tmp_path, receipt, expected_revision=0)
    assert (tmp_path / "onboarding.json").read_bytes() == before


def test_global_revision_conflicts_across_installation_and_workspace_updates(tmp_path: Path) -> None:
    receipt = ensure_receipt(tmp_path, established_install=False)
    stale_installation = read_receipt(tmp_path)
    stale_workspace = read_receipt(tmp_path)
    assert stale_installation is not None and stale_workspace is not None
    changed_installation = replace(
        stale_installation, installation=OnboardingProgress("completed", "finish")
    )
    changed_workspace = replace(
        stale_workspace,
        workspaces={"personal": OnboardingProgress("in_progress", "runtime")},
    )
    first = write_receipt(tmp_path, changed_installation, expected_revision=receipt.revision)
    before_stale = (tmp_path / "onboarding.json").read_bytes()
    with pytest.raises(OnboardingConflictError):
        write_receipt(tmp_path, changed_workspace, expected_revision=stale_workspace.revision)
    assert (tmp_path / "onboarding.json").read_bytes() == before_stale
    latest = read_receipt(tmp_path)
    assert latest is not None
    second = write_receipt(
        tmp_path,
        replace(latest, workspaces={"personal": OnboardingProgress("in_progress", "runtime")}),
        expected_revision=first.revision,
    )
    assert second.revision == 2
    assert second.installation.status == "completed"
    assert second.workspaces["personal"].step == "runtime"


@pytest.mark.parametrize("payload", [b"{", b'{"schema":99,"revision":0}'])
def test_invalid_receipt_is_not_overwritten(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "onboarding.json"
    path.write_bytes(payload)
    with pytest.raises(OnboardingStateError):
        ensure_receipt(tmp_path, established_install=False)
    assert path.read_bytes() == payload


def test_startup_classification_exempts_durable_legacy_state(tmp_path: Path) -> None:
    (tmp_path / "web_projects.json").write_text("{}")
    receipt = ensure_startup_receipt(
        tmp_path, workspace_root=tmp_path.parent, bootstrap_mode=False
    )
    assert receipt.installation.status == "legacy_exempt"


def test_bootstrap_auth_and_lock_files_are_not_legacy_evidence(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text("{}")
    (tmp_path / "instance.lock").write_text("")
    receipt = ensure_startup_receipt(
        tmp_path, workspace_root=tmp_path.parent, bootstrap_mode=True
    )
    assert receipt.installation.status == "in_progress"


@pytest.mark.parametrize("patch", [
    {"schema": 1.0}, {"schema": True}, {"revision": True}, {"revision": -1},
    {"extra": 1},
])
def test_invalid_receipts_fail_closed(tmp_path: Path, patch: dict[str, object]) -> None:
    payload = _valid_payload() | patch
    original = json.dumps(payload).encode()
    (tmp_path / "onboarding.json").write_bytes(original)
    with pytest.raises(OnboardingStateError):
        read_receipt(tmp_path)
    with pytest.raises(OnboardingStateError):
        ensure_receipt(tmp_path, established_install=False)
    assert (tmp_path / "onboarding.json").read_bytes() == original


@pytest.mark.parametrize("progress", [
    {"status": [], "step": "location"}, {"status": "new", "step": "location"},
    {"status": "in_progress", "step": "bogus"}, [],
])
def test_invalid_progress_fails_closed(tmp_path: Path, progress: object) -> None:
    payload = _valid_payload() | {"installation": progress}
    raw = json.dumps(payload).encode()
    (tmp_path / "onboarding.json").write_bytes(raw)
    with pytest.raises(OnboardingStateError):
        ensure_receipt(tmp_path, established_install=False)
    assert (tmp_path / "onboarding.json").read_bytes() == raw


@pytest.mark.parametrize("invalid", [True, -1])
def test_writer_rejects_invalid_expected_revision(tmp_path: Path, invalid: object) -> None:
    receipt = ensure_receipt(tmp_path, established_install=False)
    before = (tmp_path / "onboarding.json").read_bytes()
    with pytest.raises(OnboardingStateError):
        write_receipt(tmp_path, receipt, expected_revision=cast(int, invalid))
    assert (tmp_path / "onboarding.json").read_bytes() == before


def test_writer_rejects_invalid_receipt_without_changing_bytes(tmp_path: Path) -> None:
    receipt = ensure_receipt(tmp_path, established_install=False)
    before = (tmp_path / "onboarding.json").read_bytes()
    invalid = replace(receipt, schema=2)
    with pytest.raises(OnboardingStateError):
        write_receipt(tmp_path, invalid, expected_revision=0)
    assert (tmp_path / "onboarding.json").read_bytes() == before


@pytest.mark.parametrize("invalid", [
    replace(OnboardingProgress("in_progress", "location"), status=cast(object, [])),
    replace(OnboardingProgress("in_progress", "location"), step=cast(object, [])),
])
def test_writer_rejects_invalid_progress_without_changing_bytes(
    tmp_path: Path, invalid: OnboardingProgress
) -> None:
    receipt = ensure_receipt(tmp_path, established_install=False)
    before = (tmp_path / "onboarding.json").read_bytes()
    malformed = replace(receipt, installation=invalid)
    with pytest.raises(OnboardingStateError):
        write_receipt(tmp_path, malformed, expected_revision=0)
    assert (tmp_path / "onboarding.json").read_bytes() == before


@pytest.mark.parametrize("key", ["", " ", "bad/name", "a" * 65])
def test_writer_rejects_invalid_workspace_keys(tmp_path: Path, key: str) -> None:
    receipt = ensure_receipt(tmp_path, established_install=False)
    before = (tmp_path / "onboarding.json").read_bytes()
    malformed = replace(receipt, workspaces={key: OnboardingProgress("in_progress", "location")})
    with pytest.raises(OnboardingStateError):
        write_receipt(tmp_path, malformed, expected_revision=0)
    assert (tmp_path / "onboarding.json").read_bytes() == before


def test_receipt_file_is_private_on_posix(tmp_path: Path) -> None:
    ensure_receipt(tmp_path, established_install=False)
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "onboarding.json").stat().st_mode) == 0o600


def test_replace_failure_preserves_receipt_and_cleans_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.onboarding as onboarding

    receipt = ensure_receipt(tmp_path, established_install=False)
    target = tmp_path / "onboarding.json"
    before = target.read_bytes()
    def fail_replace(source: Path, destination: Path) -> None:
        assert destination == target
        raise OSError("replace failed")
    monkeypatch.setattr(onboarding.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_receipt(tmp_path, receipt, expected_revision=0)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".onboarding.*.tmp"))


def _stub_setup_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "ensure_workspace_git", lambda root: None)
    monkeypatch.setattr(cli, "ensure_vault_git", lambda root: None)


def test_fresh_setup_creates_pending_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_setup_side_effects(monkeypatch)
    root = tmp_path / "workspace"
    written = cli.setup_workspace(root, auth_token="test-token")
    receipt = read_receipt(root / ".runtime")
    assert receipt is not None and receipt.installation.status == "in_progress"
    assert root / ".runtime" / "onboarding.json" in written


def test_existing_notes_and_unrelated_dotenv_are_still_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_setup_side_effects(monkeypatch)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("existing notes")
    (root / ".env").write_text("UNRELATED=value\n")
    cli.setup_workspace(root, auth_token="test-token")
    receipt = read_receipt(root / ".runtime")
    assert receipt is not None and receipt.installation.status == "in_progress"


@pytest.mark.parametrize("status", ["in_progress", "completed"])
def test_setup_rerun_preserves_receipt_and_reports_creation_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    from ciao.onboarding import Status

    _stub_setup_side_effects(monkeypatch)
    root = tmp_path / "workspace"
    first_written = cli.setup_workspace(root, auth_token="test-token")
    receipt = read_receipt(root / ".runtime")
    assert receipt is not None
    updated = replace(receipt, installation=OnboardingProgress(cast(Status, status), "care"))
    write_receipt(root / ".runtime", updated, expected_revision=0)
    second_written = cli.setup_workspace(root, auth_token="test-token")
    assert root / ".runtime" / "onboarding.json" in first_written
    assert root / ".runtime" / "onboarding.json" not in second_written
    assert read_receipt(root / ".runtime") == replace(updated, revision=1)


def test_existing_registry_without_receipt_is_legacy_exempt(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "workspaces.json").write_text("[]")
    receipt = ensure_startup_receipt(runtime, workspace_root=tmp_path, bootstrap_mode=False)
    assert receipt.installation.status == "legacy_exempt"


def test_legacy_workspaces_env_is_exempt_before_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIAO_WORKSPACES", "personal:memory-vault")
    receipt = ensure_startup_receipt(tmp_path / ".runtime", workspace_root=tmp_path, bootstrap_mode=False)
    assert receipt.installation.status == "legacy_exempt"


def test_discovered_private_legacy_dotenv_is_exempt_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "installed"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "PWA_AUTH_TOKEN=private-token\nCIAO_WORKSPACES=personal:memory-vault\n"
    )
    monkeypatch.delenv("CIAO_WORKSPACE", raising=False)
    monkeypatch.delenv("CIAO_WORKSPACES", raising=False)
    # An inherited runtime root would make the receipt land outside the fixture,
    # so the assertion below could pass without exercising this workspace's
    # classification at all.
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)
    monkeypatch.setattr("ciao.install_discovery.discover_workspace", lambda base: workspace)
    config = CiaoConfig.from_env(export=False)
    assert not config.bootstrap_mode
    assert config.workspace_root == workspace.resolve()
    assert config.state_path.parent == workspace.resolve() / ".runtime"
    receipt = ensure_startup_receipt(
        config.state_path.parent,
        workspace_root=config.workspace_root,
        bootstrap_mode=config.bootstrap_mode,
    )
    assert receipt.installation.status == "legacy_exempt"


def test_setup_existing_registry_is_exempt_before_registry_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_setup_side_effects(monkeypatch)
    root = tmp_path / "workspace"
    runtime = root / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "workspaces.json").write_text(
        '[{"name":"personal","vault_root":"memory-vault"}]'
    )
    actual = cli._setup_registry_vaults
    observed: list[str] = []

    def check_before_processing(*args, **kwargs):
        receipt = read_receipt(runtime)
        observed.append(receipt.installation.status if receipt else "missing")
        return actual(*args, **kwargs)

    monkeypatch.setattr(cli, "_setup_registry_vaults", check_before_processing)
    cli.setup_workspace(root, auth_token="private-token")
    assert observed == ["legacy_exempt"]


def test_setup_legacy_dotenv_is_exempt_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_setup_side_effects(monkeypatch)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".env").write_text(
        "PWA_AUTH_TOKEN=private-token\nCIAO_WORKSPACES=personal:memory-vault\n"
    )
    actual = cli._import_legacy_workspaces_for_setup
    observed: list[str] = []

    def check_before_import(path: Path, env: dict[str, str]) -> None:
        receipt = read_receipt(path / ".runtime")
        observed.append(receipt.installation.status if receipt else "missing")
        actual(path, env)

    monkeypatch.setattr(cli, "_import_legacy_workspaces_for_setup", check_before_import)
    cli.setup_workspace(root, auth_token="private-token")
    assert observed == ["legacy_exempt"]


def test_direct_fresh_configured_start_is_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CIAO_WORKSPACES", raising=False)
    receipt = ensure_startup_receipt(tmp_path / ".runtime", workspace_root=tmp_path, bootstrap_mode=False)
    assert receipt.installation.status == "in_progress"


@pytest.mark.parametrize("state_file", ["web_projects.json", "state.json"])
def test_existing_runtime_state_is_exempt_even_without_chats(tmp_path: Path, state_file: str) -> None:
    (tmp_path / ".runtime").mkdir()
    (tmp_path / ".runtime" / state_file).write_text("{}")
    receipt = ensure_startup_receipt(tmp_path / ".runtime", workspace_root=tmp_path, bootstrap_mode=False)
    assert receipt.installation.status == "legacy_exempt"


def test_refused_setup_does_not_create_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_setup_side_effects(monkeypatch)
    monkeypatch.setattr(cli, "_service_platform", lambda: True)
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(cli.service_backend, "current_backend", lambda: type("Backend", (), {
        "agents_dir": lambda self: agents,
        "is_live_agents_dir": lambda self, path: True,
    })())
    monkeypatch.setattr(cli, "_registered_service_workspace", lambda path: tmp_path / "other")
    root = tmp_path / "refused"
    with pytest.raises(RuntimeError):
        cli.setup_workspace(root, launch_agents_dir=agents)
    assert not (root / ".runtime" / "onboarding.json").exists()
