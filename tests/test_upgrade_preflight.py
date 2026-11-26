"""The 1.10 migration-receipt preflight, its takeover refusal and its installer wiring."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ciao import upgrade_preflight as pf

REPO_ROOT = Path(__file__).resolve().parents[1]


def install(tmp_path: Path, receipts: dict[str, object] | None = None, *, old_vault: bool = False) -> Path:
    """An install dir; a str receipt value is written raw (to model a corrupt file)."""
    (tmp_path / ".runtime" / "migration").mkdir(parents=True)
    for name, body in (receipts or {}).items():
        text = body if isinstance(body, str) else json.dumps(body)
        (tmp_path / ".runtime" / "migration" / name).write_text(text, encoding="utf-8")
    if old_vault:
        (tmp_path / "memory-vault").mkdir()
    return tmp_path


def check(root: Path) -> pf.Finding | None:
    return pf.check(root, root / ".runtime")


MIGRATED: dict[str, object] = {
    "workspace-rooting.json": {"status": "migrated"},
    "logs-split.json": {"status": "migrated"},
}


def test_fresh_install_is_allowed(tmp_path: Path) -> None:
    assert check(install(tmp_path)) is None


def test_born_per_root_install_without_logs_receipt_is_allowed(tmp_path: Path) -> None:
    root = install(tmp_path, {"workspace-rooting.json": {"status": "migrated", "born_per_root": True}})
    assert check(root) is None


def test_fully_migrated_install_is_allowed(tmp_path: Path) -> None:
    root = install(
        tmp_path,
        {
            **MIGRATED,
            "vault-rehome.json": {"status": "migrated"},
            "vault-links.json": {"status": "migrated"},
            "vault-vocabulary.abc.json": {"renamed": []},
            "learnings-20260101-000000.json": {"schema_version": 1},
        },
    )
    assert check(root) is None


def test_receipt_without_status_counts_as_complete(tmp_path: Path) -> None:
    root = install(tmp_path, {**MIGRATED, "vault-rehome.json": {"moves": []}})
    assert check(root) is None


def test_old_layout_without_receipt_is_unfinished(tmp_path: Path) -> None:
    finding = check(install(tmp_path, old_vault=True))
    assert finding == pf.Finding("workspace-rooting.json", "missing")
    assert "workspace-unmigrated" in finding.message
    assert "Open 1.10" in finding.message


@pytest.mark.parametrize("status", ["refused", "partial", "undo-in-progress", "surveyed", "failed"])
def test_unfinished_reroot_status(tmp_path: Path, status: str) -> None:
    finding = check(install(tmp_path, {"workspace-rooting.json": {"status": status}}))
    assert finding == pf.Finding("workspace-rooting.json", f"status:{status}")
    assert status in finding.message


def test_rerooted_install_without_logs_split_is_unfinished(tmp_path: Path) -> None:
    root = install(tmp_path, {"workspace-rooting.json": {"status": "migrated"}})
    assert check(root) == pf.Finding("logs-split.json", "missing")


@pytest.mark.parametrize(
    ("name", "status", "task"),
    [
        ("logs-split.json", "partial", "restart"),
        ("vault-rehome.json", "partial", "unrehomed-people"),
        ("vault-links.json", "partial", "vault-migrate-links"),
        ("vault-links.json", "prepared", "vault-migrate-links"),
    ],
)
def test_partial_receipts_name_their_step(tmp_path: Path, name: str, status: str, task: str) -> None:
    root = install(tmp_path, {**MIGRATED, name: {"status": status}}, old_vault=name == "vault-rehome.json")
    finding = check(root)
    assert finding == pf.Finding(name, f"status:{status}")
    assert task in finding.message


@pytest.mark.parametrize(
    "name",
    [
        "workspace-rooting.json",
        "logs-split.json",
        "vault-rehome.json",
        "vault-links.json",
        "vault-vocabulary.abc.json",
        "learnings-20260101-000000.json",
    ],
)
@pytest.mark.parametrize("body", ["{not json", "[1, 2]"])
def test_unreadable_receipt_is_unfinished(tmp_path: Path, name: str, body: str) -> None:
    root = install(tmp_path, {**MIGRATED, name: body}, old_vault=name == "vault-rehome.json")
    finding = check(root)
    assert finding is not None
    assert finding.receipt == name
    assert finding.problem == "unreadable"


def test_learnings_message_names_the_task(tmp_path: Path) -> None:
    finding = check(install(tmp_path, {**MIGRATED, "learnings-1.json": "x"}))
    assert finding is not None
    assert "learnings-cleanup" in finding.message


def test_main_exit_codes(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    half = tmp_path / "half"
    half.mkdir()
    install(half, {"workspace-rooting.json": {"status": "partial"}})
    run = lambda ws: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "ciao.upgrade_preflight", str(ws)],
        capture_output=True, text=True, cwd=REPO_ROOT, env={"PYTHONPATH": str(REPO_ROOT)},
    )
    assert run(fresh).returncode == 0
    refused = run(half)
    assert refused.returncode == 4
    assert "Finish the 1.10 update first" in refused.stderr


def test_partial_rehome_only_blocks_while_the_shared_vault_exists(tmp_path: Path) -> None:
    receipts = {**MIGRATED, "vault-rehome.json": {"status": "partial"}}
    assert check(install(tmp_path / "after", receipts)) is None  # re-rooted: nothing could clear it
    finding = check(install(tmp_path / "before", {**receipts, "workspace-rooting.json": {"status": "migrated"}}, old_vault=True))
    assert finding == pf.Finding("vault-rehome.json", "status:partial")


def test_status_is_allowlisted_in_the_message(tmp_path: Path) -> None:
    evil = "x\n\x1b[31mrm -rf / `id`"
    finding = check(install(tmp_path, {"workspace-rooting.json": {"status": evil}}))
    assert finding is not None
    assert finding.problem == "status:unknown"
    assert "rm -rf" not in finding.message
    assert check(install(tmp_path / "n", {"workspace-rooting.json": {"status": ["a"]}})).problem == "status:unknown"


def test_reroot_advice_is_the_real_command(tmp_path: Path) -> None:
    finding = check(install(tmp_path, {"workspace-rooting.json": {"status": "refused"}}))
    assert finding is not None
    assert "ciao workspace-reroot --workspace" in finding.message
    assert "--apply" in finding.message
    assert "uncommitted" in finding.message
    assert "more than one workspace" in finding.message


def test_check_never_raises_on_hostile_receipts(tmp_path: Path) -> None:
    nested = "[" * 100000 + "]" * 100000
    root = install(tmp_path, {"workspace-rooting.json": nested})
    finding = check(root)
    assert finding == pf.Finding("workspace-rooting.json", "unreadable")
    big = install(tmp_path / "big", {"workspace-rooting.json": '{"status": "migrated", "pad": "' + "x" * (2 * 1024 * 1024) + '"}'})
    assert check(big) == pf.Finding("workspace-rooting.json", "unreadable")
    assert pf.check(tmp_path / "missing-install", tmp_path / "missing-runtime") is None


def _node_state(root: Path, body: object) -> Path:
    path = root / ".runtime" / "node_state.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return root


@pytest.mark.parametrize("role", ["client", "standby", " Client "])
def test_former_client_is_refused(tmp_path: Path, role: str) -> None:
    root = _node_state(install(tmp_path), {"role": role, "host_url": "https://host.example:8443"})
    finding = check(root)
    assert finding is not None
    assert finding.problem == "former_client"
    assert "client of another engine" in finding.message
    assert f"{root}/.runtime/node_state.json" in finding.message
    assert "host.example" not in finding.message


@pytest.mark.parametrize(
    "body",
    [
        {"role": "host"},
        {"role": "active", "host_url": "https://host.example"},
        {"role": "host", "host_url": "https://host.example"},
        {"role": "client"},
        {"role": "client", "host_url": "ftp://host.example"},
        {"role": "client", "host_url": "https://"},
        {"role": "client", "host_url": "https://user:pw@host.example"},
        {"role": "client", "host_url": 5},
        {"role": "weird", "host_url": "https://host.example"},
        "{not json",
        "[1]",
        "[" * 100000,
    ],
)
def test_other_node_states_are_allowed(tmp_path: Path, body: object) -> None:
    assert check(_node_state(install(tmp_path), body)) is None


def test_no_node_state_is_allowed_and_big_one_is_ignored(tmp_path: Path) -> None:
    assert check(install(tmp_path)) is None
    big = {"role": "client", "host_url": "https://h.example", "pad": "x" * (2 * 1024 * 1024)}
    assert check(_node_state(install(tmp_path / "big"), big)) is None


def _registry(root: Path, entries: list[dict[str, str]]) -> None:
    (root / ".runtime" / "workspaces.json").write_text(json.dumps(entries), encoding="utf-8")


def test_a_vault_kept_in_the_install_folder_is_refused(tmp_path: Path) -> None:
    # 1.10 writes no rooting receipt for a vault that is the install folder.
    root = install(tmp_path)
    _registry(root, [{"name": "personal", "vault_root": "."}])
    (root / "People").mkdir()
    (root / "AGENTS.md").write_text("g", encoding="utf-8")

    finding = check(root)

    assert finding is not None and finding.problem == "old_layout"
    assert "one folder per workspace" in finding.message


def test_a_custom_vault_name_without_workspace_folders_is_refused(tmp_path: Path) -> None:
    root = install(tmp_path)
    _registry(root, [
        {"name": "personal", "vault_root": "notes/personal"},
        {"name": "work", "vault_root": "notes/work"},
    ])
    (root / "notes" / "personal" / "People").mkdir(parents=True)
    (root / "AGENTS.md").write_text("g", encoding="utf-8")

    finding = check(root)

    assert finding is not None and finding.problem == "old_layout"


def test_root_assets_without_a_registry_are_refused(tmp_path: Path) -> None:
    root = install(tmp_path)
    (root / "skills").mkdir()

    finding = check(root)

    assert finding is not None and finding.problem == "old_layout"


def test_a_workspace_folder_clears_the_old_layout_guard(tmp_path: Path) -> None:
    root = install(tmp_path)
    _registry(root, [{"name": "personal", "vault_root": "personal/memory-vault"}])
    (root / "personal" / "memory-vault").mkdir(parents=True)

    assert check(root) is None


def test_a_migrated_receipt_clears_the_old_layout_guard(tmp_path: Path) -> None:
    root = install(tmp_path, MIGRATED)
    _registry(root, [{"name": "personal", "vault_root": "."}])
    (root / "People").mkdir()

    assert check(root) is None


def test_the_old_layout_refusal_exits_four(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = install(tmp_path)
    _registry(root, [{"name": "personal", "vault_root": "."}])

    assert pf.main([str(root)]) == 4
    assert "old shared layout" in capsys.readouterr().err
